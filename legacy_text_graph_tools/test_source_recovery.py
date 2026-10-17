"""只用小型临时资料检验确定性上游恢复。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).with_name('source_recovery.py')


class RecoveryTests(unittest.TestCase):
    def load(self):
        self.assertTrue(MODULE.exists(), 'RecoveryIndex has not been implemented')
        spec = importlib.util.spec_from_file_location('source_recovery_test', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def image(self, root, name, data=b'fixture-image', chunk='chunk_0000_0019'):
        path = root / 'chunks' / chunk / 'book' / 'ocr' / 'images' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_lazy_and_exact_missing_equation_image(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            recovery = module.RecoveryIndex(root)
            body = '$$\n\\tag{1.1}\n$$'
            self.write(root / 'merged_content_list.json', [{'type': 'equation', 'text': body,
                'img_path': 'images/e.jpg', 'page_idx': 15, 'bbox': [1, 2, 3, 4]}])
            self.image(root, 'e.jpg')
            result = recovery.recover(body + '\n')
            self.assertEqual(result['status'], 'recovered')
            self.assertEqual(result['matches'][0]['exact_match_span'], [0, len(body)])
            self.assertEqual(result['formula_image']['source_merged_indices'], [0])
            self.assertTrue(result['formula_image']['resolved_path'].endswith('images/e.jpg'))
            self.assertTrue(result['formula_image']['latex_unrecovered'])
            self.assertIsNone(recovery.recover(body.replace('1.1', '1.2'))['formula_image'])

    def test_multiple_equations_require_consistent_image_bytes(self):
        module = self.load()
        body = '$$\n\\tag{1}\n$$'
        for second_bytes, expected in [(b'same', 'recovered'), (b'different', 'ambiguous')]:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                self.write(root / 'merged_content_list.json', [
                    {'type': 'equation', 'text': body, 'img_path': 'images/a.jpg'},
                    {'type': 'equation', 'text': body, 'img_path': 'images/b.jpg'}])
                self.image(root, 'a.jpg', b'same')
                self.image(root, 'b.jpg', second_bytes)
                result = module.RecoveryIndex(root).recover(body)
                self.assertEqual(result['status'], expected)
                self.assertEqual(result['formula_image'] is None, expected == 'ambiguous')

    def test_duplicate_basename_conflict_and_complete_formula_are_not_repaired(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);body = '$$\n\\tag{1}\n$$'
            self.write(root / 'merged_content_list.json', [{'type': 'equation', 'text': body, 'img_path': 'images/a.jpg'}])
            self.image(root, 'a.jpg', b'first')
            self.image(root, 'a.jpg', b'second', 'chunk_0020_0039')
            self.assertEqual(module.RecoveryIndex(root).recover(body)['status'], 'ambiguous')
            self.assertIsNone(module.RecoveryIndex(root).recover('$$x=1\\tag{1}$$')['formula_image'])

    def list_fixture(self, root, text, lines, *, list_type='list'):
        self.write(root / 'merged_content_list.json', [{'type': 'text', 'text': text, 'page_idx': 20}])
        old = 'C:/old/book/chunks/chunk_0020_0039/book/ocr/book_content_list.json'
        self.write(root / 'chunk_index.json', [{'start': 20, 'end': 39, 'content_list': old}])
        self.write(root / 'chunks/chunk_0020_0039/book/ocr/book_middle.json', {'pdf_info': [
            {'para_blocks': [{'type': list_type, 'lines': lines}]}]})

    def line(self, text, start=False):
        return {'spans': [{'type': 'text', 'content': text}], 'is_list_start_line': start}

    def test_list_boundaries_require_full_render_alignment(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);text = 'A\\* entry   \nB second item'
            self.list_fixture(root, text, [self.line('A* entry'), self.line('B second', True), self.line('item')])
            result = module.RecoveryIndex(root).recover(text + '\n')
            self.assertEqual(result['break_offsets'], [text.index('\n') + 1])
            self.assertEqual(result['boundary_source'], 'ocr_list_line_start_render_alignment')
            self.assertEqual(result['boundary_status'], 'verified')
            self.assertNotIn('NFKC', result['boundary_normalization'])

    def test_hyphen_spelling_or_nonlist_cannot_create_automatic_boundaries(self):
        module = self.load()
        for first, kind in [('Over-mars', 'list'), ('Overmars', 'text')]:
            with self.subTest(first=first, kind=kind), tempfile.TemporaryDirectory() as temp:
                root = Path(temp);text = 'Other item   \nOvermars'
                self.list_fixture(root, text, [self.line('Other item'), self.line(first, True)], list_type=kind)
                result = module.RecoveryIndex(root).recover(text)
                self.assertEqual(result['break_offsets'], [])
                if kind == 'list':
                    self.assertEqual(result['boundary_status'], 'needs_review')
                    self.assertEqual(result['candidate_break_offsets'], [text.index('\n') + 1])

    def test_unsafe_image_reference_is_not_followed(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);body = '$$\\tag{1}$$'
            self.write(root / 'merged_content_list.json', [{'type': 'equation', 'text': body,
                'img_path': '../outside.jpg'}])
            result = module.RecoveryIndex(root).recover(body)
            self.assertIsNone(result['formula_image'])
            self.assertNotEqual(result['status'], 'recovered')

    def test_explicit_ocr_headings_only_and_source_hashes(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.write(root / 'merged_content_list.json', [
                {'type': 'text', 'text': '6.4.1 明确标题', 'text_level': 1},
                {'type': 'text', 'text': '6.4.2 没有标题标注'},
                {'type': 'text', 'text': '多行\n正文', 'text_level': 1},
                {'type': 'text', 'text': 'False标签不是级别', 'text_level': False}])
            recovery = module.RecoveryIndex(root)
            self.assertEqual(recovery.records(), [])
            self.assertEqual(recovery.heading_texts(), ['6.4.1 明确标题'])
            self.assertEqual(recovery.records()[0]['path'], 'merged_content_list.json')
            self.assertEqual(len(recovery.records()[0]['sha256']), 64)


if __name__ == '__main__':
    unittest.main()
