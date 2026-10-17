"""图片重定位必须保留歧义，不凭文件名覆盖不同图片。"""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import hashlib


class AssetTests(unittest.TestCase):
    def module(self):
        path = Path(__file__).with_name('text_graph_assets.py')
        self.assertTrue(path.exists(), 'Asset resolver is not implemented')
        spec = importlib.util.spec_from_file_location('text_graph_assets', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def image_file(self, root, chunk, name, data):
        path = root / 'book' / 'chunks' / chunk / 'ocr' / 'images' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_single_local_image_is_relocated_and_inline_text_remains_available(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.image_file(root, 'part1', 'a.png', b'image-a')
            resolver = module.AssetResolver(root / 'book', root)
            ids = resolver.references('图见 ![说明](images/a.png) 后面的文字。')
            self.assertEqual(len(ids), 1)
            row = resolver.records()[0]
            self.assertEqual(row['resolution_status'], 'resolved')
            self.assertEqual(root / row['resolved_path'], path)
            self.assertEqual(row['original_target'], 'images/a.png')
            self.assertEqual(row.get('sha256'), hashlib.sha256(b'image-a').hexdigest())

    def test_duplicate_names_only_resolve_if_bytes_equal(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.image_file(root, 'one', 'same.png', b'same-image')
            self.image_file(root, 'two', 'same.png', b'same-image')
            self.image_file(root, 'one', 'conflict.png', b'first-image')
            self.image_file(root, 'two', 'conflict.png', b'different-image')
            resolver = module.AssetResolver(root / 'book', root)
            resolver.references('![x](images/same.png)\n![y](images/conflict.png)')
            a, b = resolver.records()
            self.assertEqual(a['resolution_status'], 'resolved_identical_copies')
            self.assertEqual(len(a['candidate_paths']), 2)
            self.assertEqual(b['resolution_status'], 'ambiguous')
            self.assertIsNone(b['resolved_path'])

    def test_external_missing_and_escape_paths_are_not_fetched_or_followed(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'book').mkdir()
            (root / 'outside.png').write_bytes(b'private')
            resolver = module.AssetResolver(root / 'book', root)
            resolver.references('![a](https://example.com/a.png) ![b](images/missing.png) ![c](../outside.png)')
            rows = resolver.records()
            self.assertEqual([a['resolution_status'] for a in rows], ['external_not_fetched', 'missing', 'unsafe_path'])
            self.assertTrue(all(a['resolved_path'] is None for a in rows))


if __name__ == '__main__':
    unittest.main()
