"""验证清洗结果实际进入图和检索索引，且原字段仍可重建。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import test_text_graphs
import copy


class CleanExportTests(unittest.TestCase):
    def builder(self):
        path = Path(__file__).with_name('build_text_graphs.py')
        spec = importlib.util.spec_from_file_location('clean_export_builder', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_heading_echo_is_metadata_and_next_connects_only_body_paragraphs(self):
        module = self.builder()
        tree = {'chapters': [{'marker': '第1章', 'title': '缓存', 'label': '第1章 缓存',
                             'content': '第1章 缓存\n\n第一段。\n\n## 1.1 扩展\n第二段。\n'}]}
        book = {'book_id': 'test', 'book_title': '书', 'input_path': 'fake.json', 'input_sha256': '0' * 64}
        graph = module.build_graph(tree, book)
        paras = [n for n in graph['nodes'] if n['type'] == 'paragraph']
        text_by_id = {t['id']: t['text'] for t in graph['paragraph_texts']}
        self.assertEqual([text_by_id[n['canonical_text_id']] for n in paras], ['第一段。\n', '第二段。\n'])
        fragments = {f['id']: f for f in graph['source_fragments']}
        self.assertEqual(len(fragments), 2)
        heading = next(n for n in graph['nodes'] if n['type'] == 'chapter')
        by_id = {n['id']: n for n in paras}
        pieces = []
        for item in heading['text_layout']:
            if 'separator' in item:
                pieces.append(item['separator'])
            elif 'source_fragment_id' in item:
                pieces.append(fragments[item['source_fragment_id']]['text'])
            else:
                pieces.append(text_by_id[by_id[item['paragraph_id']]['canonical_text_id']])
        self.assertEqual(''.join(pieces), tree['chapters'][0]['content'])
        next_edges = [e for e in graph['edges'] if e['scope'] == 'field_paragraph_order']
        self.assertEqual([(e['source'], e['target']) for e in next_edges], [(paras[0]['id'], paras[1]['id'])])

    def test_cli_index_excludes_title_image_and_empty_equation_but_keeps_source(self):
        helper = test_text_graphs.TextGraphTests()
        module = self.builder()
        with tempfile.TemporaryDirectory(prefix='clean_test_', dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            content = '# 章一\n\n正文\n\n![图](images/missing.png)\n\n$$\n\\tag{1.1}\n$$\n'
            manifest, _ = helper.fixture(root, content=content)
            result = module.export_manifest(manifest, root / 'export')
            rows = [json.loads(x) for x in (root / 'export/test_book/retrieval_index.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual([x['text'] for x in rows], ['正文\n'])
            g = json.loads((root / 'export/test_book/text_graph.json').read_text(encoding='utf-8'))
            kinds = [f['fragment_kind'] for f in g['source_fragments']]
            self.assertIn('image_only', kinds)
            self.assertIn('empty_math', kinds)
            self.assertEqual(sum(k in ('heading_echo', 'markdown_heading') for k in kinds), 1)
            self.assertEqual(g['assets'][0]['resolution_status'], 'missing')
            self.assertTrue(result['summary']['full_field_reconstruction'])
            issues = (root / 'export/test_book/quality_issues.jsonl').read_text(encoding='utf-8')
            self.assertIn('empty_math', issues)
            self.assertIn('missing', issues)

    def test_empty_equation_recovers_original_image_without_inventing_formula(self):
        helper = test_text_graphs.TextGraphTests()
        module = self.builder()
        with tempfile.TemporaryDirectory(prefix='formula_test_', dir=Path(__file__).parent) as tmp:
            root = Path(tmp)
            formula = '$$\n\\tag{1.1}\n$$'
            manifest, _ = helper.fixture(root, content='说明段落。\n\n' + formula + '\n')
            image = root / 'chunks/one/ocr/images/formula.jpg'
            image.parent.mkdir(parents=True)
            image.write_bytes(b'original-image-bytes')
            (root / 'merged_content_list.json').write_text(json.dumps([
                {'type': 'equation', 'text': formula, 'img_path': 'images/formula.jpg', 'page_idx': 1}
            ]), encoding='utf-8')
            module.export_manifest(manifest, root / 'export')
            g = json.loads((root / 'export/test_book/text_graph.json').read_text(encoding='utf-8'))
            fragment = next(f for f in g['source_fragments'] if f['fragment_kind'] == 'empty_math')
            self.assertTrue(fragment.get('recovery_asset_ids'), 'Formula image was not attached')
            self.assertEqual(fragment['text'], formula + '\n')
            self.assertTrue(fragment['recovery']['formula_image']['latex_unrecovered'])
            self.assertTrue(any(x['path'].endswith('formula.jpg') for x in g['recovery_sources']))
            self.assertEqual(fragment['asset_ids'], [])
            row = json.loads((root / 'export/test_book/retrieval_index.jsonl').read_text(encoding='utf-8'))
            self.assertEqual(row['text'], '说明段落。\n')
            import verify_text_graphs as verifier
            report = verifier.verify_export(manifest, root / 'export')
            self.assertEqual(report['status'], 'passed', report['errors'])

    def test_independent_validator_rejects_body_loss_and_wrong_origin(self):
        import verify_text_graphs as verifier
        module = self.builder()
        tree = {'chapters': [{'title': '缓存', 'content': '# 缓存\n\n第一段。\n\n第二段。\n'}]}
        book = {'book_id': 'check', 'book_title': '书', 'input_path': 'fake.json', 'input_sha256': '0' * 64}
        graph = module.build_graph(tree, book)
        verifier.validate_graph(tree, graph, book)
        corrupt = copy.deepcopy(graph)
        corrupt['source_fragments'][0]['text'] = '篡改标题'
        with self.assertRaises(verifier.IntegrityError):
            verifier.validate_graph(tree, corrupt, book)
        corrupt = copy.deepcopy(graph)
        para = next(n for n in corrupt['nodes'] if n['type'] == 'paragraph')
        para['source_origin']['json_pointer'] = '/chapters/9/content'
        with self.assertRaises(verifier.IntegrityError):
            verifier.validate_graph(tree, corrupt, book)
        corrupt = copy.deepcopy(graph)
        corrupt['paragraph_texts'][0]['text'] = ''
        with self.assertRaises(verifier.IntegrityError):
            verifier.validate_graph(tree, corrupt, book)


if __name__ == '__main__':
    unittest.main()
