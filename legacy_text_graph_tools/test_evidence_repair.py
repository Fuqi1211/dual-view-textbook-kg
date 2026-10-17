import copy
import unittest

from build_text_graphs import build_graph
from evidence_repair import repair_graph, reconstruct_source


def fixture(content, label='1.2 数据处理'):
    return build_graph({'chapters': [{'label': label, 'marker': '1.2', 'title': '数据处理',
                                    'content': content, 'children': []}]},
                       {'book_id': 'test_book', 'book_title': '测试', 'input_path': 'test.json', 'input_sha256': 'fixture'})


def paragraphs(graph):
    return [n for n in graph['nodes'] if n['type'] == 'paragraph' and n['retrieval_eligible']]


def text_of(graph, node):
    return next(t['text'] for t in graph['paragraph_texts'] if t['id'] == node['canonical_text_id'])


def patch(graph, ids, replacement=None, **kwargs):
    nodes = {n['id']: n for n in graph['nodes']}
    return dict(id='test-fix', record_ids=ids,
                expected_texts=[text_of(graph, nodes[i]) for i in ids],
                replacement_text=replacement, evidence=['fixture-page-evidence'], **kwargs)


class EvidenceRepairTests(unittest.TestCase):
    def test_ocr_repair_retains_raw_reconstruction_and_does_not_mutate_input(self):
        g = fixture('接固定的电平。\n')
        original = copy.deepcopy(g)
        p = paragraphs(g)[0]
        out = repair_graph(g, [patch(g, [p['id']], '接固定的高电平。\n')])
        self.assertEqual(text_of(out, paragraphs(out)[0]), '接固定的高电平。\n')
        self.assertEqual(reconstruct_source(out, p['heading_id']), '接固定的电平。\n')
        self.assertEqual(g, original)
        self.assertNotEqual(paragraphs(out)[0]['id'], p['id'])

    def test_cross_page_merge_has_one_active_paragraph_and_valid_order_edges(self):
        g = fixture('段落前半（本章后面\n\n的习题）。\n\n下一段。\n')
        old = paragraphs(g)
        out = repair_graph(g, [patch(g, [n['id'] for n in old[:2]], '段落前半（本章后面的习题）。\n')])
        self.assertEqual(len(paragraphs(out)), 2)
        self.assertEqual(reconstruct_source(out, old[0]['heading_id']), '段落前半（本章后面\n\n的习题）。\n\n下一段。\n')
        ids = {n['id'] for n in out['nodes']}
        self.assertTrue(all(e['source'] in ids and e['target'] in ids for e in out['edges']))
        merged = next(n for n in paragraphs(out) if n['id'] != old[-1]['id'])
        self.assertTrue(any(e['source'] == merged['id'] and e['target'] == old[-1]['id'] and e['type'] == 'next' for e in out['edges']))

    def test_stale_patch_is_rejected_before_applying(self):
        g = fixture('原文。\n')
        p = patch(g, [paragraphs(g)[0]['id']], '修正文。\n')
        p['expected_texts'] = ['不匹配。\n']
        with self.assertRaises(ValueError):
            repair_graph(g, [p])

    def test_numbered_heading_whitespace_variant_is_not_body(self):
        g = fixture('1.2数据处理\n\n实际内容。\n')
        out = repair_graph(g, [])
        self.assertEqual([text_of(out, n) for n in paragraphs(out)], ['实际内容。\n'])
        h = next(n for n in g['nodes'] if n['type'] == 'chapter')
        self.assertEqual(reconstruct_source(out, h['id']), '1.2数据处理\n\n实际内容。\n')

    def test_code_and_numbered_prose_are_not_misclassified(self):
        g = fixture('## int main()\n\n1.2倍的数据处理速度。\n')
        out = repair_graph(g, [])
        self.assertEqual(len(paragraphs(out)), 2)

    def test_quarantined_bibliography_leaves_default_retrieval_but_keeps_source(self):
        g = fixture('Author 2020. Title. Other 2021. Title.\n')
        p = paragraphs(g)[0]
        out = repair_graph(g, [patch(g, [p['id']], None, output_type='bibliography_block', retrieval_eligible=False)])
        self.assertEqual(paragraphs(out), [])
        self.assertEqual(reconstruct_source(out, p['heading_id']), 'Author 2020. Title. Other 2021. Title.\n')

    def test_verified_bibliography_boundaries_preserve_all_characters(self):
        value = '[A 2020] First.\n[B 2021] Second.\n'
        g = fixture(value)
        p = patch(g, [paragraphs(g)[0]['id']], value, output_type='bibliography_block',
                  retrieval_eligible=False, split_offsets=[0, value.index('[B'), len(value)])
        out = repair_graph(g, [p])
        entries = [n for n in out['nodes'] if n['type'] == 'bibliography_entry']
        self.assertEqual(len(entries), 2)
        self.assertEqual(''.join(text_of(out, n) for n in entries), value)
        self.assertEqual(paragraphs(out), [])

    def test_merge_cannot_skip_intervening_source_content(self):
        g = fixture('第一段。\n\n中间证据。\n\n末段。\n')
        old = paragraphs(g)
        p = patch(g, [old[0]['id'], old[2]['id']], '第一段。末段。\n')
        with self.assertRaises(ValueError):
            repair_graph(g, [p])

    def test_merge_keeps_assets_from_both_source_parts(self):
        g = fixture('上半段。\n\n下半段。\n')
        old = paragraphs(g)
        old[0]['asset_ids'] = ['asset:first']
        old[1]['asset_ids'] = ['asset:second']
        out = repair_graph(g, [patch(g, [n['id'] for n in old], '上半段。下半段。\n')])
        self.assertEqual(paragraphs(out)[0]['asset_ids'], ['asset:first', 'asset:second'])

    def test_insertion_precedes_content_starting_at_anchor_end(self):
        g = fixture('说明。\n$$x$$\n下一段。\n')
        anchor = paragraphs(g)[0]
        p = patch(g, [anchor['id']], '```cpp\nint x;\n```\n', insert_after=True,
                  upstream_insertion_source={'path': 'fixture-ocr'}, content_kind='fenced_code')
        out = repair_graph(g, [p])
        inserted = next(n for n in paragraphs(out) if n.get('repair_id'))
        successors = [e['target'] for e in out['edges'] if e['type'] == 'next' and e['source'] == anchor['id']]
        self.assertEqual(successors, [inserted['id']])


if __name__ == '__main__':
    unittest.main()
