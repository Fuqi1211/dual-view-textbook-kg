"""离线文本图行为测试；临时材料用完自动清理。"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name('build_text_graphs.py')
ROOT = Path(__file__).resolve().parents[2]


class TextGraphTests(unittest.TestCase):
    def builder(self):
        self.assertTrue(SCRIPT.is_file(), 'Paragraph graph exporter has not been implemented')
        spec = importlib.util.spec_from_file_location('text_graph_builder', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def build(self, tree):
        return self.builder().build_graph(tree, {
            'book_id': 'book-test', 'book_dir': 'fixture', 'book_title': '测试书',
            'input_path': 'fixture/content_tree.json', 'input_sha256': '0' * 64,
        })

    def restored(self, graph, heading):
        nodes = {n['id']: n for n in graph['nodes']}
        texts = {t['id']: t['text'] for t in graph['paragraph_texts']}
        pieces = []
        for item in heading['text_layout']:
            if 'separator' in item:
                pieces.append(item['separator'])
            else:
                node = nodes[item['paragraph_id']]
                pieces.append(texts[node['canonical_text_id']])
        return ''.join(pieces)

    def test_repeated_paragraphs_keep_occurrences_pages_and_field_order(self):
        # 合并出处节点、覆盖子节页码、建立全局顺序都会使本测试失败。
        content = '相同段落\r\n\r\n相同段落\r\n'
        graph = self.build({'chapters': [{
            'marker': '第1章', 'title': '章', 'label': '原始标签', 'level': 1,
            'pdf_page_start': 10, 'pdf_page_end': 12,
            'content': content, 'children': [
                {'title': '甲', 'pdf_page_start': 30, 'pdf_page_end': 31, 'content': '相同段落\n'},
                {'title': '乙', 'pdf_page_start': 50, 'pdf_page_end': 52, 'content': '相同段落\n'},
            ]}]})
        headings = [n for n in graph['nodes'] if n['type'] in ('chapter', 'section')]
        paragraphs = [n for n in graph['nodes'] if n['type'] == 'paragraph']
        self.assertEqual([self.restored(graph, n) for n in headings],
                         ['相同段落\n\n相同段落\n', '相同段落\n', '相同段落\n'])
        self.assertEqual(len(paragraphs), 4)
        self.assertEqual(len({n['id'] for n in paragraphs}), 4)
        self.assertEqual(len(graph['paragraph_texts']), 1)
        self.assertEqual([n['pdf_page_start'] for n in paragraphs], [10, 10, 30, 50])
        self.assertEqual([n['source_origin']['json_pointer'] for n in headings],
                         ['/chapters/0', '/chapters/0/children/0', '/chapters/0/children/1'])
        self.assertEqual(headings[0]['label'], '原始标签')
        seq = [e for e in graph['edges'] if e['type'] == 'next' and e.get('scope') == 'field_paragraph_order']
        self.assertEqual([(e['source'], e['target']) for e in seq], [(paragraphs[0]['id'], paragraphs[1]['id'])])
        self.assertNotEqual(seq[0]['source'], seq[0]['target'])
        self.assertTrue(graph['summary']['full_field_reconstruction'])
        self.assertFalse(graph['pdf_ocr_verified'])
        self.assertFalse(graph['semantic_verified'])
        self.assertIs(graph.get('physical_occurrence_verified'), False)

    def test_blank_fields_and_missing_pages_reconstruct_without_fake_nodes(self):
        graph = self.build({'chapters': [{'title': '空章', 'content': '', 'children': [
            {'title': '空白', 'content': ' \r\n\t\r\n'},
            {'title': '正文', 'content': '甲\r乙\r\n丙'},
        ]}]})
        headings = [n for n in graph['nodes'] if n['type'] in ('chapter', 'section')]
        self.assertEqual([self.restored(graph, n) for n in headings], ['', ' \n\t\n', '甲\n乙\n丙'])
        self.assertEqual(graph['summary']['empty_fields'], 1)
        self.assertEqual(graph['summary']['whitespace_only_fields'], 1)
        self.assertEqual(graph['summary']['page_missing_headings'], 3)
        self.assertEqual(graph['summary']['normalized_field_chars'], 9)
        self.assertEqual(graph['summary']['paragraph_count'], 1)
        self.assertEqual(graph['summary']['separator_chars'], 4)
        empty = self.build({'chapters': [{'content': ' \n\n'}]})
        self.assertEqual(empty['paragraph_texts'], [])
        self.assertEqual(empty['summary']['paragraph_count'], 0)
        self.assertTrue(empty['summary']['full_field_reconstruction'])

    def test_code_math_lists_tables_are_indivisible_and_offsets_are_exact(self):
        # 在代码或公式空行处分块、按长度截断、丢换行都会失败。
        content = '\n普通\n\n```python\nx = 1\n\nx = 2\n```\n\n$$\na+b\n\nc+d\n$$\n\n- 一\n- 二\n\n|a|b|\n|---|---|\n|1|2|\n\n' + '长' * 9000
        graph = self.build({'chapters': [{'content': content}]})
        paragraphs = [n for n in graph['nodes'] if n['type'] == 'paragraph']
        texts = {t['id']: t['text'] for t in graph['paragraph_texts']}
        self.assertEqual(len(paragraphs), 6)
        self.assertEqual([n['content_kind'] for n in paragraphs], ['text', 'fenced_code', 'math', 'list', 'table', 'text'])
        self.assertEqual([texts[n['canonical_text_id']] for n in paragraphs[:3]],
                         ['普通\n', '```python\nx = 1\n\nx = 2\n```\n', '$$\na+b\n\nc+d\n$$\n'])
        self.assertEqual(len(texts[paragraphs[-1]['canonical_text_id']]), 9000)
        for p in paragraphs:
            self.assertEqual(content[p['field_start']:p['field_end']], texts[p['canonical_text_id']])
            self.assertEqual(p['source_origin']['json_pointer'], '/chapters/0/content')
            self.assertIsNone(p['pdf_page_start'])
        heading = next(n for n in graph['nodes'] if n['type'] == 'chapter')
        self.assertEqual(self.restored(graph, heading), content)
        self.assertEqual(graph, self.build({'chapters': [{'content': content}]}))

    def test_only_lf_blank_lines_split_markdown_paragraphs(self):
        graph = self.build({'chapters': [{'content': '甲\u2028\u2028乙\n\n尾'}]})
        paragraphs = [n for n in graph['nodes'] if n['type'] == 'paragraph']
        texts = {t['id']: t['text'] for t in graph['paragraph_texts']}
        self.assertEqual([texts[n['canonical_text_id']] for n in paragraphs], ['甲\u2028\u2028乙\n', '尾'])

    def fixture(self, directory, *, content='正文\n\n正文\n'):
        source = directory / 'input.json'
        source.write_text(json.dumps({'chapters': [{'title': '章一', 'content': content}]}, ensure_ascii=False), encoding='utf-8')
        row = {'book_id': 'test_book', 'book_title': '测试', 'book_dir': 'fixture',
               'input_path': source.relative_to(ROOT).as_posix(),
               'input_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
        manifest = directory / 'input_manifest.json'
        manifest.write_text(json.dumps({'books': [row]}, ensure_ascii=False), encoding='utf-8')
        return manifest, row

    def test_cli_single_book_writes_verified_hashes_and_deduplicated_index(self):
        self.builder()
        with tempfile.TemporaryDirectory(prefix='paragraph_test_', dir=SCRIPT.parent) as temp:
            folder = Path(temp)
            manifest, row = self.fixture(folder)
            output = folder / 'new_output'
            run = subprocess.run([sys.executable, '-B', str(SCRIPT), '--manifest', str(manifest), '--output', str(output)], capture_output=True, encoding='utf-8', env={**os.environ, 'PYTHONUTF8': '1'})
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('1/1', run.stdout)
            result = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(result['api_calls'], 0)
            self.assertEqual(result.get('dedup_scope'), 'within_each_book')
            self.assertEqual(result['books'][0]['input_sha256'], row['input_sha256'])
            for artifact in result['books'][0]['artifacts']:
                self.assertEqual(hashlib.sha256((output / artifact['path']).read_bytes()).hexdigest(), artifact['sha256'])
            lines = (output / 'test_book' / 'retrieval_index.jsonl').read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 1)
            index = json.loads(lines[0])
            self.assertEqual(index['text'], '正文\n')
            self.assertEqual(len(index['occurrence_ids']), 2)
            outline = (output / 'test_book' / '目录文本图.md').read_text(encoding='utf-8')
            self.assertIn('章一', outline)
            self.assertNotIn('正文\n\n正文', outline)

    def test_existing_output_is_untouched(self):
        module = self.builder()
        with tempfile.TemporaryDirectory(prefix='paragraph_test_', dir=SCRIPT.parent) as temp:
            folder = Path(temp)
            manifest, _ = self.fixture(folder)
            output = folder / 'existing'
            output.mkdir()
            sentinel = output / 'sentinel'
            sentinel.write_bytes(b'original')
            with self.assertRaises(FileExistsError):
                module.export_manifest(manifest, output)
            self.assertEqual(sentinel.read_bytes(), b'original')
            self.assertEqual(list(output.iterdir()), [sentinel])

    def test_all_inputs_preflight_hash_and_root_containment_before_writing(self):
        module = self.builder()
        with tempfile.TemporaryDirectory(prefix='paragraph_test_', dir=SCRIPT.parent) as temp:
            folder = Path(temp)
            manifest, good = self.fixture(folder)
            bad = dict(good, book_id='second', input_sha256='f' * 64)
            for bad_row in [bad, dict(bad, input_path='../outside.json'), dict(bad, book_id='../escape')]:
                manifest.write_text(json.dumps({'books': [good, bad_row]}), encoding='utf-8')
                output = folder / 'must_not_exist'
                with self.assertRaises(ValueError):
                    module.export_manifest(manifest, output)
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
