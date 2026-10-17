"""无损隔离可明确识别的结构片段；不猜改 OCR，不解释语义。"""
from __future__ import annotations

import re


_FENCE = re.compile(r'^[ \t]*(`{3,}|~{3,})([^\n]*)\n?$')
_ATX = re.compile(r'^ {0,3}#{1,6}(?:[ \t]+|$)(.*?)[ \t]*\n?$')
_NUMBERED = re.compile(r'^(?:第[0-9一二三四五六七八九十百千零〇]+[章节篇部]|[A-Za-z]?\d|[一二三四五六七八九十]+[、．.]|附录)')
_PREPROCESSOR = re.compile(r'^[ \t]*#[ \t]*(?:include|define|undef|if|ifdef|ifndef|elif|else|endif|line|error|warning|pragma)\b')


def _key(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _structured_extra_heading(value):
    # OCR 的 text_level 不是标题真值；额外候选必须还有明确结构编号。
    if re.search(r'[。！？.!?;；:]$', value) or re.search(r'[{};；=]', value):
        return False
    if re.search(r'\b[A-Za-z_]\w*\s*\([^)]*\)', value):
        return False
    if re.match(r'^(?:第[0-9一二三四五六七八九十百千零〇两]+[章节篇部]|附录)(?:\s|$|[\u3400-\u9fff])', value):
        return True
    if re.match(r'^(?:Chapter\s+\d+|Appendix\s+[A-Z])(?:\s|$)', value, re.I):
        return True
    # 小数测量句（如 1.5 seconds elapsed）不凭编号删除；英文标题须大写起始。
    return bool(re.match(r'^(?:\d+(?:\.\d+)+|[A-Z](?:\.\d+)+)\s+[A-Z\u3400-\u9fff]', value))


def _code_heading(value):
    # OCR 有时给单行代码加 ##；保守保留可检索正文，不抹掉标记。
    if _PREPROCESSOR.match(value):
        return True
    if re.search(r'[\u3400-\u9fff]', value):
        return False
    return bool(re.search(r'[;{}=]|\b[A-Za-z_]\w*\s*\([^)]*\)', value))


def _heading_maps(candidates, own_id):
    full, numbered, own = {}, {}, {}
    for candidate in candidates:
        heading_id = candidate['id']
        marker, title = _key(candidate.get('marker')), _key(candidate.get('title'))
        labels = {_key(candidate.get('label')), _key(marker + ' ' + title)} - {''}
        for label in labels:
            full.setdefault(label, []).append(heading_id)
            if marker and _NUMBERED.match(marker) and label != title:
                numbered.setdefault(label, []).append(heading_id)
        if title:
            full.setdefault(title, []).append(heading_id)
            if heading_id == own_id:
                own.setdefault(title, []).append(heading_id)
    return full, numbered, own


def _balanced_end(text, start, opening, closing):
    depth = 0
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _only_images(text):
    cursor, count = 0, 0
    while cursor < len(text):
        if text[cursor].isspace():
            cursor += 1
            continue
        if not text.startswith('![', cursor):
            return False
        end = _balanced_end(text, cursor + 1, '[', ']')
        if end is None or end == len(text) or text[end] not in '([':
            return False
        opening = text[end]
        end = _balanced_end(text, end, opening, ')' if opening == '(' else ']')
        if end is None:
            return False
        count += 1
        cursor = end
    return count > 0


def _table_kind(text):
    lines = text.split('\n')
    if lines and not lines[-1]:
        lines.pop()
    def cells(line):
        return [part.strip() for part in re.split(r'(?<!\\)\|', line.strip().strip('|'))]
    found = []
    for i in range(1, len(lines)):
        header, delimiter = cells(lines[i - 1]), cells(lines[i])
        if len(header) >= 2 and len(header) == len(delimiter) and all(re.fullmatch(r':?-{3,}:?', c) for c in delimiter):
            found.append(i)
    if not found:
        return 'text', []
    first = found[0]
    columns = len(cells(lines[first]))
    if first == 1 and all('|' in line and len(cells(line)) == columns for line in lines[2:]):
        return 'table', []
    return 'mixed', ['mixed_table_text_review']


def _plain_span(text, start, end):
    value = text[start:end]
    if _only_images(value):
        return {'kind': 'source_fragment', 'start': start, 'end': end,
                'fragment_kind': 'image_only', 'matched_heading_ids': [], 'quality_flags': []}
    kind, flags = _table_kind(value)
    if kind == 'text' and re.match(r'\s*(?:[-+*]|\d+[.)])\s+', value):
        kind = 'list'
    if re.search(r'(?:\$\$|\\\[|\\\])', value):
        flags.append('mixed_or_unmatched_math_review')
    if any((atx := _ATX.match(line)) and not _PREPROCESSOR.match(line)
           and not _code_heading(atx.group(1)) for line in value.splitlines()):
        flags.append('potential_heading_review')
    if len(value) > 4000:
        flags.append('long_block_review')
    return {'kind': 'paragraph', 'start': start, 'end': end,
            'content_kind': kind, 'quality_flags': flags}


def _empty_math(body):
    body = re.sub(r'\\(?:tag\*?|label)\s*\{[^{}]*\}', '', body)
    body = re.sub(r'\\(?:begin|end)\s*\{(?:aligned|align\*?|equation\*?|gathered|gather\*?|displaymath)\}', '', body)
    body = re.sub(r'\\(?:notag|nonumber|displaystyle|qquad|quad)\b', '', body)
    body = re.sub(r'\\[,;:!]|\\\\|&', '', body)
    return not body.strip()


def clean_spans(text, heading_candidates, own_heading_id, *, extra_heading_texts=()):
    """返回连续半开区间；所有偏移均针对传入的原字符串。"""
    if not isinstance(text, str):
        raise TypeError('text must be str')
    if '\r' in text:
        raise ValueError('text must already use LF newlines')
    full, numbered, own = _heading_maps(heading_candidates, own_heading_id)
    extra_headings = {key for value in extra_heading_texts
                      if (key := _key(value)) and _structured_extra_heading(key)}
    lines = [(m.start(), m.end(), m.group()) for m in re.finditer(r'[^\n]*(?:\n|$)', text) if m.group()]
    spans = []
    cursor = 0
    plain_start = None
    first_content = True

    def flush(end):
        nonlocal plain_start
        if plain_start is not None:
            spans.append(_plain_span(text, plain_start, end))
            plain_start = None

    while cursor < len(lines):
        start, end, line = lines[cursor]
        stripped = line.strip()
        if not stripped:
            flush(start)
            separator_end = end
            cursor += 1
            while cursor < len(lines) and not lines[cursor][2].strip():
                separator_end = lines[cursor][1]
                cursor += 1
            spans.append({'kind': 'separator', 'start': start, 'end': separator_end})
            continue
        fence = _FENCE.match(line)
        math_open = '$$' if stripped.startswith('$$') else ('\\[' if stripped.startswith('\\[') else None)
        if math_open:
            same_line_close = '$$' if math_open == '$$' else '\\]'
            remainder = stripped[len(math_open):]
            if same_line_close in remainder and remainder.split(same_line_close, 1)[1].strip():
                math_open = None
        if fence or math_open:
            flush(start)
            first_content = False
            kind = 'fenced_code' if fence else 'math'
            flags = []
            close = None
            if fence:
                marker = fence.group(1)
                closing = re.compile(r'^[ \t]*' + re.escape(marker[0]) + '{' + str(len(marker)) + r',}[ \t]*\n?$')
                for candidate in range(cursor + 1, len(lines)):
                    if closing.fullmatch(lines[candidate][2]):
                        close = candidate
                        break
            else:
                closing = '$$' if math_open == '$$' else '\\]'
                tail = stripped[len(math_open):]
                if closing in tail and not tail.split(closing, 1)[1].strip():
                    close = cursor
                else:
                    for candidate in range(cursor + 1, len(lines)):
                        if lines[candidate][2].strip().endswith(closing):
                            close = candidate
                            break
            if close is None:
                close = len(lines) - 1
                flags.append('unclosed_fence_review' if fence else 'unclosed_math_review')
            end = lines[close][1]
            value = text[start:end]
            if len(value) > 4000:
                flags.append('long_block_review')
            if not fence and 'unclosed_math_review' not in flags and _empty_math(value.strip()[len(math_open):-len(closing)]):
                spans.append({'kind': 'source_fragment', 'start': start, 'end': end,
                              'fragment_kind': 'empty_math', 'matched_heading_ids': [],
                              'quality_flags': flags + ['empty_math_review']})
            else:
                spans.append({'kind': 'paragraph', 'start': start, 'end': end,
                              'content_kind': kind, 'quality_flags': flags})
            cursor = close + 1
            continue
        preprocessor = _PREPROCESSOR.match(line)
        atx = None if preprocessor else _ATX.match(line.rstrip('\n'))
        code_heading = bool(atx and _code_heading(atx.group(1)))
        line_key = _key(stripped)
        matched = []
        fragment_kind = None
        if preprocessor or code_heading:
            pass
        elif atx:
            heading_text = re.sub(r'[ \t]+#+[ \t]*$', '', atx.group(1))
            matched = full.get(_key(heading_text), [])
            if matched or _structured_extra_heading(_key(heading_text)):
                fragment_kind = 'markdown_heading'
        elif line_key in numbered:
            matched = numbered[line_key]
            fragment_kind = 'heading_echo'
        elif first_content and line_key in own:
            matched = own[line_key]
            fragment_kind = 'heading_echo'
        elif line_key in extra_headings:
            fragment_kind = 'ocr_heading'
        if fragment_kind:
            flush(start)
            spans.append({'kind': 'source_fragment', 'start': start, 'end': end,
                          'fragment_kind': fragment_kind, 'matched_heading_ids': list(dict.fromkeys(matched)),
                          'quality_flags': []})
        elif plain_start is None:
            plain_start = start
        first_content = False
        cursor += 1
    flush(len(text))
    return spans
