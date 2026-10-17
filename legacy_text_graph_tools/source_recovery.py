"""只依据精确上游匹配恢复公式原图或核验 OCR 列表边界，不改正文。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re


BOUNDARY_NORMALIZATION = 'remove documented Markdown punctuation escapes and whitespace only; preserve all hyphens, spelling and Unicode characters'


def _lf(text):
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _align(text):
    text = re.sub(r'\\([\\`*_{}\[\]()#+.!|>\-])', r'\1', text)
    return re.sub(r'\s+', '', text)


def _missing_equation(text):
    text = text.strip()
    if not (text.startswith('$$') and text.endswith('$$')):
        return False
    body = text[2:-2]
    body = re.sub(r'\\(?:tag\*?|label)\{[^{}]*\}', '', body)
    return not body.strip()


class RecoveryIndex:
    """book_root 指向一本书；初始化不读文件，records() 返回实际读取的来源。"""

    def __init__(self, book_root):
        self.root = Path(book_root).resolve()
        self._text_index = None
        self._heading_texts = []
        self._images = None
        self._json_cache = {}
        self._files = {}

    def _safe(self, path):
        path = Path(path).resolve()
        path.relative_to(self.root)
        return path

    def _bytes(self, path, kind):
        path = self._safe(path)
        raw = path.read_bytes()
        relative = path.relative_to(self.root).as_posix()
        digest = hashlib.sha256(raw).hexdigest()
        previous = self._files.get(relative)
        if previous and previous['sha256'] != digest:
            raise ValueError('Recovery source changed during use: ' + relative)
        self._files[relative] = {'path': relative, 'sha256': digest, 'bytes': len(raw), 'kind': kind}
        return raw

    def _json(self, path, kind):
        path = self._safe(path)
        if path not in self._json_cache:
            self._json_cache[path] = json.loads(self._bytes(path, kind).decode('utf-8-sig'))
        return self._json_cache[path]

    def records(self):
        return [dict(self._files[k]) for k in sorted(self._files)]

    @property
    def source_files(self):
        return self.records()

    def heading_texts(self):
        """仅返回 OCR 明确赋予正整数 text_level 的完整单行标题。"""
        self._load_index()
        return list(self._heading_texts)

    def _load_index(self):
        if self._text_index is not None:
            return
        self._text_index = {}
        path = self.root / 'merged_content_list.json'
        if not path.is_file():
            return
        data = self._json(path, 'merged_content_list')
        if not isinstance(data, list):
            return
        for i, row in enumerate(data):
            if isinstance(row, dict) and isinstance(row.get('text'), str) and row['text'].strip():
                source = _lf(row['text'])
                self._text_index.setdefault(source.strip(), []).append((i, row, source))
                heading = source.strip()
                if (row.get('type') == 'text' and type(row.get('text_level')) is int
                        and row['text_level'] > 0 and '\n' not in heading
                        and heading not in self._heading_texts):
                    self._heading_texts.append(heading)

    def _image_paths(self, reference):
        if not isinstance(reference, str):
            return []
        ref = PurePosixPath(reference.replace('\\', '/'))
        if ref.is_absolute() or '..' in ref.parts or ':' in reference or len(ref.parts) != 2 or ref.parts[0] != 'images':
            return []
        if self._images is None:
            self._images = {}
            for path in sorted(self.root.glob('chunks/**/images/*')):
                if path.is_file():
                    try:
                        self._safe(path)
                    except ValueError:
                        continue
                    self._images.setdefault(path.name, []).append(path)
        return self._images.get(ref.name, [])

    def _formula(self, rows, result):
        candidates = []
        unresolved = False
        for index, row, _source, _span in rows:
            paths = self._image_paths(row.get('img_path'))
            if not paths:
                unresolved = True
            for path in paths:
                raw = self._bytes(path, 'equation_image')
                candidates.append({'merged_index': index, 'img_path': row['img_path'],
                    'resolved_path': path.relative_to(self.root).as_posix(),
                    'sha256': hashlib.sha256(raw).hexdigest()})
        result['formula_image_candidates'] = candidates
        hashes = {x['sha256'] for x in candidates}
        if unresolved or len(hashes) != 1:
            result['status'] = 'ambiguous' if len(rows) > 1 or len(hashes) > 1 else 'unresolved'
            result['notes'].append('Missing or inconsistent equation images; no candidate selected.')
            return
        first = candidates[0]
        result['formula_image'] = {
            'img_path': first['img_path'], 'resolved_path': first['resolved_path'],
            'resolved_paths': sorted({x['resolved_path'] for x in candidates}),
            'sha256': first['sha256'], 'source_merged_indices': sorted({x['merged_index'] for x in candidates}),
            'latex_unrecovered': True, 'selection_basis': 'all exact text matches have identical image bytes',
        }
        result['status'] = 'recovered'

    def _middle_sources(self, page):
        path = self.root / 'chunk_index.json'
        if type(page) is not int or not path.is_file():
            return []
        result = []
        entries = self._json(path, 'chunk_index')
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or type(entry.get('start')) is not int or type(entry.get('end')) is not int:
                continue
            if not entry['start'] <= page <= entry['end']:
                continue
            old = str(entry.get('content_list', '')).replace('\\', '/')
            if '/chunks/' not in old:
                continue
            relative = 'chunks/' + old.split('/chunks/', 1)[1]
            try:
                original = self._safe(self.root / relative)
            except ValueError:
                continue
            if not original.name.endswith('_content_list.json'):
                continue
            middle = original.with_name(original.name.replace('_content_list.json', '_middle.json'))
            if middle.is_file():
                result.append((middle, page - entry['start']))
        return result

    def _boundaries(self, rows, result):
        candidates = []
        for index, row, source, span in rows:
            segments = source.splitlines(keepends=True)
            if len(segments) < 2 or any(not x.strip() for x in segments):
                continue
            # 只调查上游显式硬换行列表或长块，不扩展到任意正文换行。
            if len(source) < 2000 and '  \n' not in source:
                continue
            for middle, local_page in self._middle_sources(row.get('page_idx')):
                data = self._json(middle, 'ocr_middle')
                pages = data.get('pdf_info', []) if isinstance(data, dict) else []
                if not 0 <= local_page < len(pages):
                    continue
                for bi, block in enumerate(pages[local_page].get('para_blocks', [])):
                    if block.get('type') != 'list':
                        continue
                    groups = []
                    group = []
                    valid = True
                    for line in block.get('lines', []):
                        if line.get('is_list_start_line') is True and group:
                            groups.append(group)
                            group = []
                        spans = line.get('spans', [])
                        if any(s.get('type') != 'text' or not isinstance(s.get('content'), str) for s in spans):
                            valid = False
                            break
                        group.append(''.join(s['content'] for s in spans))
                    if group:
                        groups.append(group)
                    if not valid or len(groups) != len(segments) or len(groups) < 2:
                        continue
                    rendered = [' '.join(g) for g in groups]
                    anchor = _align(groups[0][0])[:24]
                    if not anchor or not _align(segments[0]).startswith(anchor):
                        continue
                    mismatches = [i for i, (a, b) in enumerate(zip(rendered, segments)) if _align(a) != _align(b)]
                    offsets = []
                    at = span[0]
                    for segment in segments[:-1]:
                        at += len(segment)
                        offsets.append(at)
                    candidates.append({'merged_index': index,
                        'middle_path': middle.relative_to(self.root).as_posix(),
                        'json_pointer': f'/pdf_info/{local_page}/para_blocks/{bi}',
                        'break_offsets': offsets, 'entry_count': len(groups),
                        'mismatch_count': len(mismatches), 'mismatch_entry_indices': mismatches[:20],
                        'verified': not mismatches})
        result['boundary_candidates'] = candidates
        verified = [c for c in candidates if c['verified']]
        if verified and len(verified) == len(candidates) and len({tuple(c['break_offsets']) for c in verified}) == 1:
            result['break_offsets'] = verified[0]['break_offsets']
            result['boundary_source'] = 'ocr_list_line_start_render_alignment'
            result['boundary_status'] = 'verified'
            result['boundary_normalization'] = BOUNDARY_NORMALIZATION
            if result['status'] == 'matched':
                result['status'] = 'recovered'
        elif candidates:
            result['boundary_status'] = 'needs_review'
            sets = {tuple(c['break_offsets']) for c in candidates}
            result['candidate_break_offsets'] = list(next(iter(sets))) if len(sets) == 1 else []
            result['notes'].append('OCR list metadata found, but full conservative render alignment did not establish automatic boundaries.')

    def recover(self, text, field_origin=None):
        if not isinstance(text, str):
            raise TypeError('text must be a string')
        result = {'status': 'not_found', 'matches': [], 'formula_image': None,
            'break_offsets': [], 'candidate_break_offsets': [], 'boundary_source': None,
            'boundary_status': 'not_applicable', 'boundary_candidates': [], 'notes': [],
            'offset_unit': 'Unicode code points after CRLF/CR to LF',
            'match_policy': 'exact normalized source string; outside-match layout whitespace retained'}
        normalized = _lf(text)
        if not normalized.strip():
            return result
        self._load_index()
        rows = []
        for index, row, source in self._text_index.get(normalized.strip(), []):
            at = normalized.find(source)
            if at < 0 or normalized[:at].strip() or normalized[at + len(source):].strip():
                continue
            span = [at, at + len(source)]
            rows.append((index, row, source, span))
            result['matches'].append({'merged_index': index, 'merged_path': 'merged_content_list.json',
                'type': row.get('type'), 'page_idx': row.get('page_idx'), 'bbox': row.get('bbox'),
                'img_path': row.get('img_path'), 'text_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(),
                'exact_match_span': span})
        if not rows:
            return result
        result['status'] = 'matched'
        equation_rows = [r for r in rows if r[1].get('type') == 'equation']
        if _missing_equation(normalized) and equation_rows:
            if len(equation_rows) != len(rows):
                result['status'] = 'ambiguous'
                result['notes'].append('Identical text has mixed source types; no equation selected.')
            else:
                self._formula(equation_rows, result)
        elif not _missing_equation(normalized):
            self._boundaries(rows, result)
        return result
