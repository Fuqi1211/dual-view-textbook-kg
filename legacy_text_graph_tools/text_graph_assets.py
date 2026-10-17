"""只用本书现存文件恢复图片引用，保留无法消歧的目标。"""
from collections import defaultdict
import hashlib
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


IMAGE_PATTERN = re.compile(r'!\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+["\'][^"\']*["\'])?\s*\)')
HTML_IMAGE_PATTERN = re.compile(r'<img\b[^>]*?\bsrc\s*=\s*["\']([^"\']+)["\']', re.I)


class AssetResolver:
    def __init__(self, book_root, project_root):
        self.book_root = Path(book_root).resolve()
        self.project_root = Path(project_root).resolve()
        self._index = None
        self._records = {}
        self._hashes = {}

    def _inside(self, path):
        try:
            path.resolve().relative_to(self.book_root)
            return True
        except ValueError:
            return False

    def _image_index(self):
        if self._index is None:
            self._index = defaultdict(list)
            chunks = self.book_root / 'chunks'
            if chunks.is_dir():
                for path in chunks.rglob('*'):
                    if path.is_file() and 'images' in [p.lower() for p in path.parts] and self._inside(path):
                        self._index[path.name].append(path.resolve())
            for paths in self._index.values():
                paths.sort(key=lambda p: str(p))
        return self._index

    def _hash(self, path):
        if path not in self._hashes:
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1048576), b''):
                    digest.update(block)
            self._hashes[path] = digest.hexdigest()
        return self._hashes[path]

    def _resolve(self, target):
        row = {'id': 'asset:' + hashlib.sha256(target.encode('utf-8')).hexdigest(),
               'original_target': target, 'resolution_status': 'missing',
               'resolved_path': None, 'candidate_paths': [], 'image_semantics_verified': False}
        url = urlsplit(target)
        if url.scheme or url.netloc:
            row['resolution_status'] = 'external_not_fetched'
            return row
        relative = Path(unquote(url.path).replace('\\', '/'))
        direct = (self.book_root / relative).resolve()
        if relative.is_absolute() or not self._inside(direct):
            row['resolution_status'] = 'unsafe_path'
            return row
        candidates = [direct] if direct.is_file() else self._image_index().get(relative.name, [])
        row['candidate_paths'] = [p.relative_to(self.project_root).as_posix() for p in candidates]
        if len(candidates) == 1:
            row.update(resolution_status='resolved', resolved_path=row['candidate_paths'][0],
                       sha256=self._hash(candidates[0]))
        elif candidates:
            hashes = [self._hash(p) for p in candidates]
            row['candidate_sha256'] = hashes
            if len(set(hashes)) == 1:
                row.update(resolution_status='resolved_identical_copies', resolved_path=row['candidate_paths'][0],
                           sha256=hashes[0])
            else:
                row['resolution_status'] = 'ambiguous'
        return row

    def references(self, text):
        targets = [m.group(1) or m.group(2) for m in IMAGE_PATTERN.finditer(text)]
        targets += [m.group(1) for m in HTML_IMAGE_PATTERN.finditer(text)]
        ids = []
        for target in targets:
            if target not in self._records:
                self._records[target] = self._resolve(target)
            value = self._records[target]['id']
            if value not in ids:
                ids.append(value)
        return ids

    def records(self):
        return list(self._records.values())
