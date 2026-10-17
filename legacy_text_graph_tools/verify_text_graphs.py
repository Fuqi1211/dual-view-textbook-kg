"""独立验收冻结原树与段落文本图；不导入转换器或清洗器。

只读输入及导出目录，以新建文件方式写报告，任何异常均使 CLI 非零退出。
校验物理不变量和声明的检索资格，不把逐字一致等同于 OCR/语义正确。
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
PAGE_KEYS = ('toc_page_start', 'toc_page_end', 'pdf_page_start', 'pdf_page_end')
NODE_TYPES = {'book', 'chapter', 'section', 'paragraph'}
SCHEMAS = {'paragraph-text-graph-v1', 'paragraph-text-graph-v2'}


class IntegrityError(ValueError):
    """输入或产物违反一个必须成立的不变量。"""


def require(condition, message):
    if not condition:
        raise IntegrityError(message)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'Duplicate JSON object key: ' + str(key))
        result[key] = value
    return result


def parse_json(raw, context='JSON'):
    try:
        return json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as exc:
        raise IntegrityError(context + ': ' + str(exc)) from exc


def load_json(path: Path):
    raw = path.read_bytes()
    return parse_json(raw.decode('utf-8-sig'), str(path)), digest_bytes(raw), len(raw)


def normalized_content(raw, pointer):
    value = raw.get('content', '')
    if value is None:
        value = ''
    require(isinstance(value, str), 'Non-string content at ' + pointer)
    # This is the sole permitted transformation of the original content field.
    return value.replace('\r\n', '\n').replace('\r', '\n')


def rooted_path(root: Path, relative, context):
    require(isinstance(relative, str) and relative, context + ': missing relative path')
    require(not Path(relative).is_absolute(), context + ': absolute path is disallowed')
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise IntegrityError(context + ': path escapes root') from exc
    return candidate


def id_map(values, context):
    require(isinstance(values, list), context + ' must be an array')
    result = {}
    for row in values:
        require(isinstance(row, dict), context + ': each item must be an object')
        identifier = row.get('id')
        require(isinstance(identifier, str) and identifier, context + ': invalid id')
        require(identifier not in result, context + ': duplicate id ' + identifier)
        result[identifier] = row
    return result


def check_origin(origin, book, pointer, context):
    require(isinstance(origin, dict), context + ': missing source origin')
    require(origin.get('input_path') == book['input_path'], context + ': wrong input_path')
    require(origin.get('json_pointer') == pointer, context + ': wrong json_pointer')


def check_pages(row, raw, context):
    for key in PAGE_KEYS:
        require(key in row and row[key] == raw.get(key), context + ': changed/missing ' + key)


def check_interval(row, start, end, context):
    require(type(row.get('field_start')) is int and type(row.get('field_end')) is int,
            context + ': offsets must be integer code-point positions')
    require(row['field_start'] == start and row['field_end'] == end,
            context + ': offsets disagree with text_layout')


def check_recovery(payload, original_field, book, source_hashes, json_cache):
    """回查恢复声明的原始证据，不执行恢复/清洗算法。"""
    recovery = payload.get('recovery')
    if recovery is None:
        require(not payload.get('recovery_asset_ids'), 'Recovery asset has no recovery evidence')
        require(payload.get('boundary_source', 'markdown_structure') == 'markdown_structure',
                'Recovered boundary has no recovery evidence')
        return
    require(isinstance(recovery, dict), 'Recovery record must be an object')
    span = payload.get('recovery_original_span')
    require(isinstance(span, dict), 'Recovery record missing pre-split original field span')
    left, right = span.get('field_start'), span.get('field_end')
    require(type(left) is int and type(right) is int
            and 0 <= left <= payload['field_start'] < payload['field_end'] <= right <= len(original_field),
            'Recovery original span does not enclose this source record')
    original_block = original_field[left:right]
    book_root = rooted_path(ROOT, book['input_path'], 'recovery book root').parent

    def source_path(relative):
        path = rooted_path(book_root, relative, 'book-relative recovery evidence')
        project_relative = path.relative_to(ROOT).as_posix()
        require(project_relative in source_hashes, 'Recovery evidence file is not in recovery_sources: ' + project_relative)
        return path, project_relative

    def source_json(relative):
        path, project_relative = source_path(relative)
        if project_relative not in json_cache:
            value, digest, _ = load_json(path)
            require(digest == source_hashes[project_relative], 'Recovery JSON changed after hashing')
            json_cache[project_relative] = value
        return json_cache[project_relative]

    matches = recovery.get('matches')
    require(isinstance(matches, list), 'Recovery matches must be an array')
    matched_rows = {}
    for match in matches:
        require(isinstance(match, dict), 'Recovery match must be an object')
        data = source_json(match.get('merged_path'))
        index = match.get('merged_index')
        require(isinstance(data, list) and type(index) is int and 0 <= index < len(data),
                'Recovery merged index is outside original source')
        raw = data[index]
        require(isinstance(raw, dict) and isinstance(raw.get('text'), str), 'Recovery source row has no text')
        text = raw['text'].replace('\r\n', '\n').replace('\r', '\n')
        exact_span = match.get('exact_match_span')
        require(isinstance(exact_span, list) and len(exact_span) == 2
                and all(type(position) is int for position in exact_span), 'Invalid exact_match_span')
        start, end = exact_span
        require(0 <= start < end <= len(original_block)
                and original_block[start:end] == text
                and not original_block[:start].strip() and not original_block[end:].strip(),
                'Recovery exact source match fails against ORIGINAL pre-split field block')
        require(match.get('text_sha256') == digest_bytes(text.encode('utf-8')), 'Recovery match text hash mismatch')
        for key in ('type', 'page_idx', 'bbox', 'img_path'):
            require(match.get(key) == raw.get(key), 'Recovery match changed source ' + key)
        require(index not in matched_rows, 'Duplicate recovery merged_index')
        matched_rows[index] = raw
    require(recovery.get('status') in {'not_found', 'matched', 'recovered', 'ambiguous', 'unresolved'},
            'Unknown recovery status')
    require(bool(matches) == (recovery['status'] != 'not_found'), 'Recovery status disagrees with source matches')
    image = recovery.get('formula_image')
    if image is None:
        require(not payload.get('recovery_asset_ids'), 'Recovery asset ids exist without a recovered formula image')
    if image is not None:
        require(isinstance(image, dict) and recovery['status'] == 'recovered', 'Formula image is not declared recovered')
        require(matches and all(row.get('type') == 'equation' for row in matched_rows.values()),
                'Formula recovery sources are not exclusively equations')
        indices = image.get('source_merged_indices')
        require(isinstance(indices, list) and len(indices) == len(set(indices))
                and set(indices) == set(matched_rows), 'Formula image source indices do not cover declared exact matches')
        require(image.get('latex_unrecovered') is True, 'Image recovery must not claim restored LaTeX')
        paths = image.get('resolved_paths')
        require(isinstance(paths, list) and paths and all(isinstance(path, str) for path in paths)
                and len(paths) == len(set(paths)) and image.get('resolved_path') in paths,
                'Invalid recovered formula image paths')
        for relative in paths:
            _path, project_relative = source_path(relative)
            require(source_hashes[project_relative] == image.get('sha256'), 'Recovered formula image SHA-256 mismatch')
        require(image.get('img_path') in {row.get('img_path') for row in matched_rows.values()},
                'Recovered image target absent from exact OCR source matches')
        candidates = recovery.get('formula_image_candidates')
        require(isinstance(candidates, list) and candidates, 'Recovered formula lacks candidate provenance')
        seen_indices, seen_paths = set(), set()
        for candidate in candidates:
            require(isinstance(candidate, dict) and candidate.get('merged_index') in matched_rows,
                    'Formula candidate does not refer to a matched OCR source')
            row = matched_rows[candidate['merged_index']]
            require(candidate.get('img_path') == row.get('img_path'), 'Formula candidate changed OCR image target')
            path, project_relative = source_path(candidate.get('resolved_path'))
            require(path.name == Path(str(row['img_path']).replace('\\', '/')).name,
                    'Recovered image basename differs from OCR image target')
            require(candidate.get('sha256') == image.get('sha256') == source_hashes[project_relative],
                    'Recovered formula candidate has differing bytes')
            seen_indices.add(candidate['merged_index'])
            seen_paths.add(candidate['resolved_path'])
        require(seen_indices == set(matched_rows) and seen_paths == set(paths),
                'Recovered formula omitted a declared candidate/source')
    breaks = recovery.get('break_offsets')
    require(isinstance(breaks, list) and all(type(position) is int for position in breaks),
            'Recovery break_offsets must be an integer array')
    if breaks:
        require(breaks == sorted(set(breaks)) and all(0 < p < len(original_block) for p in breaks),
                'Invalid recovered boundary offsets')
        require(all(original_block[p - 1] == '\n' for p in breaks), 'Recovered boundary is not at a source line break')
        require(recovery.get('boundary_status') == 'verified'
                and recovery.get('boundary_source') == 'ocr_list_line_start_render_alignment',
                'Unverified recovery boundary used for splitting')
        candidates = recovery.get('boundary_candidates')
        require(isinstance(candidates, list) and candidates, 'Verified boundaries have no source candidates')
        for candidate in candidates:
            require(isinstance(candidate, dict) and candidate.get('verified') is True
                    and candidate.get('mismatch_count') == 0 and candidate.get('break_offsets') == breaks,
                    'Verified boundary candidates disagree')
            require(candidate.get('merged_index') in matched_rows, 'Boundary candidate source was not exactly matched')
            data = source_json(candidate.get('middle_path'))
            pointer = candidate.get('json_pointer')
            require(isinstance(pointer, str) and pointer.startswith('/'), 'Invalid OCR boundary JSON pointer')
            for token in pointer.split('/')[1:]:
                token = token.replace('~1', '/').replace('~0', '~')
                if isinstance(data, list):
                    require(token.isdigit() and int(token) < len(data), 'OCR boundary pointer outside source array')
                    data = data[int(token)]
                else:
                    require(isinstance(data, dict) and token in data, 'OCR boundary pointer missing in source')
                    data = data[token]
            require(isinstance(data, dict) and data.get('type') == 'list', 'Boundary evidence is not an OCR list block')
        bounds = [0] + breaks + [len(original_block)]
        require((payload['field_start'] - left, payload['field_end'] - left) in set(zip(bounds, bounds[1:])),
                'Recovered paragraph interval is not a declared verified list interval')
        require(payload.get('boundary_source') == recovery['boundary_source'], 'Paragraph lost recovered boundary provenance')
    else:
        require(payload.get('boundary_source', 'markdown_structure') == 'markdown_structure',
                'Paragraph claims recovered boundaries without verified offsets')


def source_headings(tree, book_title):
    require(isinstance(tree, dict), 'Original tree must be an object')
    require(isinstance(tree.get('chapters'), list) and tree['chapters'],
            'Original tree needs nonempty chapters')
    records = []
    sibling_sequences = []

    def walk(children, parent_pointer, prefix, ancestors, depth):
        require(isinstance(children, list), 'children must be an array at ' + prefix)
        sequence = []
        for index, raw in enumerate(children):
            pointer = prefix + '/' + str(index)
            require(isinstance(raw, dict), 'Original heading is not an object: ' + pointer)
            label = raw.get('label')
            display = str(label) if label is not None else ' '.join(
                str(raw.get(key) or '').strip() for key in ('marker', 'title')).strip()
            titles = ancestors + [display]
            records.append({'pointer': pointer, 'parent': parent_pointer, 'raw': raw,
                            'depth': depth, 'title_path': titles,
                            'text': normalized_content(raw, pointer)})
            sequence.append(pointer)
            grandchildren = raw.get('children', [])
            if grandchildren is None:
                grandchildren = []
            walk(grandchildren, pointer, pointer + '/children', titles, depth + 1)
        sibling_sequences.append(sequence)

    walk(tree['chapters'], '', '/chapters', [book_title], 1)
    return records, sibling_sequences


def validate_graph(tree: dict, graph: dict, book: dict, retrieval_rows=None, quality_rows=None) -> dict:
    """校验一个内存图；成功返回计数，首个不变量失败抛 IntegrityError。

    retrieval_rows 可为生成器，便于逐行检查大型 JSONL。未提供时明确不计索引通过。
    不调用任何段落切分/分类函数，而是把产物声明的每一范围直接与原字段比较。
    """
    require(isinstance(graph, dict), 'Graph must be an object')
    schema = graph.get('schema_version')
    require(schema in SCHEMAS, 'Unsupported graph schema: ' + str(schema))
    version_two = schema.endswith('-v2')
    for key in ('book_id', 'book_title', 'input_path', 'input_sha256'):
        require(graph.get(key) == book.get(key), 'Graph disagrees with frozen manifest: ' + key)
    for flag in ('pdf_ocr_verified', 'semantic_verified', 'physical_occurrence_verified'):
        require(graph.get(flag) is False, 'Unjustified verification flag: ' + flag)
    require(graph.get('api_calls') == 0, 'Offline export must declare zero API calls')
    require(graph.get('minimum_node') == 'paragraph', 'minimum_node is not paragraph')

    nodes = id_map(graph.get('nodes'), 'nodes')
    texts = id_map(graph.get('paragraph_texts'), 'paragraph_texts')
    fragments = id_map(graph.get('source_fragments', []), 'source_fragments')
    assets = id_map(graph.get('assets', []), 'assets')
    recovery_sources = graph.get('recovery_sources', [])
    require(isinstance(recovery_sources, list), 'recovery_sources must be an array')
    recovery_hashes = []
    recovery_paths = {}
    for source in recovery_sources:
        require(isinstance(source, dict), 'Recovery source must be an object')
        path = rooted_path(ROOT, source.get('path'), 'recovery source')
        relative = path.relative_to(ROOT).as_posix()
        require(relative not in recovery_paths, 'Duplicate recovery source: ' + relative)
        actual_hash = file_digest(path)
        require(actual_hash == source.get('sha256'), 'Recovery input SHA-256 mismatch: ' + relative)
        if 'bytes' in source:
            require(source['bytes'] == path.stat().st_size, 'Recovery input byte count mismatch: ' + relative)
        recovery_paths[relative] = actual_hash
        recovery_hashes.append({'path': relative, 'sha256': actual_hash, 'bytes': path.stat().st_size})
    if version_two:
        require('source_fragments' in graph and 'assets' in graph, 'v2 missing fragments/assets')
    require(not set(nodes).intersection(fragments), 'Source fragments must not be graph nodes')
    require(all(row.get('type') in NODE_TYPES for row in nodes.values()),
            'Graph has a node type outside book/chapter/section/paragraph')
    roots = [row for row in nodes.values() if row['type'] == 'book']
    require(len(roots) == 1 and roots[0]['id'] == book['book_id'], 'Incorrect book root')
    check_origin(roots[0].get('source_origin'), book, '', 'book')
    require(roots[0].get('title') == book['book_title'], 'Book title differs from manifest')
    for key in ('source_book_title', 'toc_pages_pdf', 'toc_to_pdf_offset'):
        original_key = 'book_title' if key == 'source_book_title' else key
        require(roots[0].get(key) == tree.get(original_key), 'Changed book metadata: ' + key)

    for identifier, record in texts.items():
        value = record.get('text')
        require(isinstance(value, str), 'Canonical text is not a string: ' + identifier)
        require(identifier == 'text:' + digest_bytes(value.encode('utf-8')),
                'Canonical text id is not its SHA-256: ' + identifier)
        require(type(record.get('chars')) is int and record['chars'] == len(value),
                'Incorrect canonical character count: ' + identifier)
        require('\r' not in value, 'Canonical text contains non-normalized CR: ' + identifier)

    records, sequences = source_headings(tree, book['book_title'])
    headings = [row for row in nodes.values() if row['type'] in ('chapter', 'section')]
    heading_by_pointer = {}
    for heading in headings:
        origin = heading.get('source_origin')
        require(isinstance(origin, dict), 'Heading missing source_origin')
        pointer = origin.get('json_pointer')
        require(isinstance(pointer, str) and pointer not in heading_by_pointer,
                'Duplicate/invalid heading source pointer: ' + str(pointer))
        heading_by_pointer[pointer] = heading
    require(set(heading_by_pointer) == {r['pointer'] for r in records},
            'Exported headings do not exactly cover original headings')

    expected_edges = set()
    used_paragraphs, used_fragments, used_texts = set(), set(), set()
    expected_index = defaultdict(list)
    quality_flags, isolated_flags, fragment_kinds, fragment_flags = Counter(), Counter(), Counter(), Counter()
    content_kinds = Counter()
    used_assets = set()
    expected_issues = {}
    recovery_json_cache = {}
    counts = Counter()
    field_comparisons = []
    for record in records:
        pointer, raw, original = record['pointer'], record['raw'], record['text']
        heading = heading_by_pointer[pointer]
        heading_id = heading['id']
        require(heading['type'] == ('chapter' if record['depth'] == 1 else 'section'),
                'Wrong heading type at ' + pointer)
        require(heading.get('tree_depth') == record['depth'], 'Wrong tree_depth at ' + pointer)
        require(heading.get('title_path') == record['title_path'], 'Wrong title_path at ' + pointer)
        for key in ('marker', 'title', 'label', 'level'):
            require(heading.get(key) == raw.get(key), 'Changed heading metadata: ' + pointer + ':' + key)
        check_pages(heading, raw, pointer)
        check_origin(heading.get('source_origin'), book, pointer, pointer)
        check_origin(heading.get('content_origin'), book, pointer + '/content', pointer)
        raw_hash = digest_bytes(original.encode('utf-8'))
        require(heading.get('normalized_chars') == len(original), 'Wrong field length at ' + pointer)
        require(heading.get('normalized_text_sha256') == raw_hash, 'Wrong field hash at ' + pointer)
        if version_two:
            require(heading.get('has_non_whitespace_content') is bool(original.strip()),
                    'Wrong non-whitespace field flag at ' + pointer)
        parent_id = book['book_id'] if not record['parent'] else heading_by_pointer[record['parent']]['id']
        expected_edges.add((parent_id, heading_id, 'contains', 'original_heading_hierarchy'))
        layout = heading.get('text_layout')
        require(isinstance(layout, list), 'Missing text_layout at ' + pointer)
        reconstructed = []
        cursor, paragraph_index = 0, 0
        previous_paragraph = None
        for item in layout:
            require(isinstance(item, dict), 'text_layout item is not an object at ' + pointer)
            start, end = item.get('field_start'), item.get('field_end')
            require(type(start) is int and type(end) is int and start == cursor and start < end <= len(original),
                    'text_layout has gap/overlap/invalid interval at ' + pointer)
            keys = [key for key in ('separator', 'paragraph_id', 'source_fragment_id') if key in item]
            require(len(keys) == 1, 'Ambiguous or missing layout payload at ' + pointer)
            kind = keys[0]
            if kind == 'separator':
                value = item['separator']
                require(isinstance(value, str) and not value.strip(), 'Non-whitespace separator at ' + pointer)
                counts['separator_chars'] += len(value)
            elif kind == 'source_fragment_id':
                identifier = item[kind]
                require(identifier in fragments and identifier not in used_fragments,
                        'Missing/reused source fragment at ' + pointer + ': ' + str(identifier))
                fragment = fragments[identifier]
                used_fragments.add(identifier)
                require(fragment.get('heading_id') == heading_id, 'Fragment belongs to wrong heading')
                check_interval(fragment, start, end, identifier)
                check_origin(fragment.get('source_origin'), book, pointer + '/content', identifier)
                value = fragment.get('text')
                require(isinstance(value, str), 'Fragment text is not a string: ' + identifier)
                require(isinstance(fragment.get('fragment_kind'), str) and fragment['fragment_kind'],
                        'Missing fragment_kind: ' + identifier)
                require(fragment.get('retrieval_eligible') is False, 'Source fragment must be excluded from retrieval')
                matches = fragment.get('matched_heading_ids')
                require(isinstance(matches, list) and len(matches) == len(set(matches))
                        and all(identifier in nodes and nodes[identifier]['type'] in ('chapter', 'section')
                                for identifier in matches), 'Fragment has invalid matched_heading_ids')
                if 'asset_refs' in fragment:
                    require(isinstance(fragment['asset_refs'], list), 'asset_refs is not an array: ' + identifier)
                for key in PAGE_KEYS:
                    if key in fragment:
                        require(fragment[key] == raw.get(key), 'Changed fragment page metadata: ' + identifier)
                fragment_kinds[fragment['fragment_kind']] += 1
                counts['source_fragment_chars'] += len(value)
                counts['heading_fragment_count'] += int(fragment['fragment_kind'] in ('heading_echo', 'markdown_heading', 'ocr_heading'))
                counts['image_only_fragment_count'] += int(fragment['fragment_kind'] == 'image_only')
                counts['empty_math_fragment_count'] += int(fragment['fragment_kind'] == 'empty_math')
            else:
                identifier = item[kind]
                require(identifier in nodes and identifier not in used_paragraphs,
                        'Missing/reused paragraph at ' + pointer + ': ' + str(identifier))
                paragraph = nodes[identifier]
                require(paragraph['type'] == 'paragraph', 'Layout points to a non-paragraph graph node')
                used_paragraphs.add(identifier)
                require(paragraph.get('heading_id') == heading_id, 'Paragraph belongs to wrong heading')
                require(paragraph.get('index') == paragraph_index, 'Non-contiguous paragraph index: ' + identifier)
                check_interval(paragraph, start, end, identifier)
                check_origin(paragraph.get('source_origin'), book, pointer + '/content', identifier)
                check_pages(paragraph, raw, identifier)
                require(paragraph.get('page_scope') == 'original_heading_range_not_exact_paragraph_location',
                        'Paragraph page scope incorrectly claims exact physical location')
                require(paragraph.get('title_path') == record['title_path'], 'Changed paragraph title_path')
                canonical = paragraph.get('canonical_text_id')
                require(canonical in texts, 'Unknown paragraph canonical_text_id: ' + str(canonical))
                used_texts.add(canonical)
                value = texts[canonical]['text']
                require(bool(value.strip()), 'Whitespace-only graph paragraph: ' + identifier)
                reference = paragraph.get('text_ref')
                require(isinstance(reference, dict) and reference.get('canonical_text_id') == canonical
                        and reference.get('start') == 0 and reference.get('end') == len(value),
                        'Broken canonical text_ref: ' + identifier)
                require(isinstance(paragraph.get('content_kind'), str) and paragraph['content_kind'],
                        'Missing paragraph content_kind: ' + identifier)
                require(paragraph['content_kind'] not in {'heading', 'heading_echo', 'markdown_heading',
                                                         'image', 'image_only', 'empty_formula', 'empty_math'},
                        'Non-body fragment incorrectly represented as a paragraph')
                content_kinds[paragraph['content_kind']] += 1
                if version_two:
                    flags = paragraph.get('quality_flags')
                    require(isinstance(flags, list) and all(isinstance(flag, str) for flag in flags),
                            'quality_flags must be a string array: ' + identifier)
                    require(len(flags) == len(set(flags)), 'Duplicate quality_flags: ' + identifier)
                    require(type(paragraph.get('retrieval_eligible')) is bool,
                            'retrieval_eligible must be boolean: ' + identifier)
                    eligible = paragraph['retrieval_eligible']
                else:
                    flags, eligible = [], True
                quality_flags.update(flags)
                if eligible:
                    expected_index[canonical].append(identifier)
                    counts['retrieval_eligible_paragraphs'] += 1
                else:
                    isolated_flags.update(flags)
                    counts['retrieval_ineligible_paragraphs'] += 1
                counts['paragraph_occurrence_chars'] += len(value)
                expected_edges.add((heading_id, identifier, 'contains', 'paragraph_occurrence_in_field'))
                if previous_paragraph is not None:
                    expected_edges.add((previous_paragraph, identifier, 'next', 'field_paragraph_order'))
                previous_paragraph = identifier
                paragraph_index += 1
            if version_two and kind != 'separator':
                payload = fragment if kind == 'source_fragment_id' else paragraph
                flags = payload.get('quality_flags')
                require(isinstance(flags, list) and all(isinstance(flag, str) for flag in flags)
                        and len(flags) == len(set(flags)), 'Invalid quality_flags: ' + payload['id'])
                if kind == 'source_fragment_id':
                    fragment_flags.update(flags)
                asset_ids = payload.get('asset_ids')
                require(isinstance(asset_ids, list) and all(isinstance(asset, str) for asset in asset_ids)
                        and len(asset_ids) == len(set(asset_ids)), 'Invalid asset_ids: ' + payload['id'])
                for asset_id in asset_ids:
                    require(asset_id in assets, 'Dangling asset reference: ' + asset_id)
                    require(assets[asset_id].get('original_target') in value,
                            'Asset target absent from referencing source text: ' + asset_id)
                    used_assets.add(asset_id)
                recovery_ids = payload.get('recovery_asset_ids', [])
                require(isinstance(recovery_ids, list) and all(isinstance(asset, str) for asset in recovery_ids)
                        and len(recovery_ids) == len(set(recovery_ids)), 'Invalid recovery_asset_ids: ' + payload['id'])
                for asset_id in recovery_ids:
                    require(asset_id in assets, 'Dangling recovery asset reference: ' + asset_id)
                    recovered_path = assets[asset_id].get('resolved_path')
                    require(recovered_path in recovery_paths, 'Recovery asset lacks frozen recovery source hash')
                    require(isinstance(payload.get('recovery'), dict), 'Recovery asset lacks provenance record')
                    used_assets.add(asset_id)
                    counts['recovery_asset_reference_count'] += 1
                check_recovery(payload, original, book, recovery_paths, recovery_json_cache)
                if payload.get('recovery') is not None:
                    counts['recovery_records_checked'] += 1
                    counts['recovery_matches_checked'] += len(payload['recovery']['matches'])
                if kind == 'source_fragment_id':
                    counts['recovered_formula_image_count'] += int(bool(recovery_ids))
                else:
                    counts['ocr_boundary_recovered_paragraph_count'] += int(
                        payload.get('boundary_source', 'markdown_structure') != 'markdown_structure')
                if flags:
                    expected_issues[payload['id']] = {
                        key: payload[key] for key in ('id', 'source_origin', 'field_start', 'field_end',
                                                      'quality_flags', 'retrieval_eligible')}
                    expected_issues[payload['id']]['record_kind'] = payload.get('fragment_kind', payload.get('content_kind'))
            # Compare each actual exported string with the immutable raw-field slice,
            # not just with another exporter-produced hash or summary.
            require(value == original[start:end], 'DIRECT_RAW_CHARACTER_MISMATCH at ' + pointer
                    + ' [' + str(start) + ',' + str(end) + ')')
            reconstructed.append(value)
            counts['direct_raw_compared_parts'] += 1
            counts['direct_raw_compared_chars'] += len(value)
            cursor = end
        require(cursor == len(original), 'text_layout does not cover whole field at ' + pointer)
        restored = ''.join(reconstructed)
        require(restored == original, 'DIRECT_RAW_FIELD_RECONSTRUCTION_MISMATCH at ' + pointer)
        require(heading.get('paragraph_count') == paragraph_index, 'Wrong heading paragraph_count at ' + pointer)
        counts['normalized_field_chars'] += len(original)
        counts['empty_fields'] += int(not original)
        counts['whitespace_only_fields'] += int(bool(original) and not original.strip())
        counts['nonparagraph_only_fields'] += int(bool(original.strip()) and paragraph_index == 0)
        counts['page_missing_headings'] += int(raw.get('pdf_page_start') is None or raw.get('pdf_page_end') is None)
        field_comparisons.append({'json_pointer': pointer + '/content', 'chars': len(original),
                                  'raw_lf_sha256': raw_hash,
                                  'reconstructed_sha256': digest_bytes(restored.encode('utf-8')),
                                  'direct_character_equal': True})

    require(used_paragraphs == {n['id'] for n in nodes.values() if n['type'] == 'paragraph'},
            'Orphan paragraph nodes not covered by original fields')
    require(used_fragments == set(fragments), 'Orphan source fragments not covered by original fields')
    require(used_texts == set(texts), 'Orphan canonical paragraph text')
    if version_two:
        require(used_assets == set(assets), 'Orphan assets not referenced by source records')
    asset_file_hashes = []
    book_root = rooted_path(ROOT, book['input_path'], 'asset book root').parent
    for identifier, asset in assets.items():
        target = asset.get('original_target')
        require(isinstance(target, str) and target, 'Missing original asset target')
        require(identifier == 'asset:' + digest_bytes(target.encode('utf-8')), 'Incorrect asset id')
        require(asset.get('image_semantics_verified') is False, 'Asset claims unperformed semantic verification')
        status = asset.get('resolution_status')
        require(status in {'resolved', 'resolved_identical_copies', 'ambiguous', 'missing',
                           'external_not_fetched', 'unsafe_path'}, 'Unknown asset resolution status')
        candidates = asset.get('candidate_paths')
        require(isinstance(candidates, list) and all(isinstance(p, str) for p in candidates)
                and len(candidates) == len(set(candidates)), 'Invalid/duplicate asset candidate_paths')
        hashes = []
        for candidate in candidates:
            path = rooted_path(ROOT, candidate, 'asset candidate')
            try:
                path.relative_to(book_root)
            except ValueError as exc:
                raise IntegrityError('Asset candidate escapes this book directory') from exc
            value = file_digest(path)
            if candidate in recovery_paths:
                require(value == recovery_paths[candidate], 'Recovery asset changed during verification')
            hashes.append(value)
            asset_file_hashes.append({'asset_id': identifier, 'path': candidate, 'sha256': value,
                                      'bytes': path.stat().st_size})
        if 'candidate_sha256' in asset:
            require(asset['candidate_sha256'] == hashes, 'Asset candidate hashes differ from actual files')
        if status.startswith('resolved'):
            require(asset.get('resolved_path') in candidates, 'Resolved asset is not one of its candidates')
            require((status == 'resolved' and len(candidates) == 1)
                    or (status == 'resolved_identical_copies' and len(candidates) > 1 and len(set(hashes)) == 1),
                    'Resolved status disagrees with asset candidates')
            require(asset.get('sha256') == hashes[candidates.index(asset['resolved_path'])],
                    'Resolved asset SHA-256 differs from its actual file')
        else:
            require(asset.get('resolved_path') is None, 'Unresolved asset has a resolved_path')
            require((status == 'ambiguous' and len(candidates) > 1 and len(set(hashes)) > 1)
                    or (status != 'ambiguous' and not candidates), 'Unresolved asset candidates inconsistent')
    for sequence in sequences:
        for left, right in zip(sequence, sequence[1:]):
            expected_edges.add((heading_by_pointer[left]['id'], heading_by_pointer[right]['id'],
                                'next', 'sibling_heading_order'))
    require(isinstance(graph.get('edges'), list), 'edges must be an array')
    actual_edges = set()
    for edge in graph['edges']:
        require(isinstance(edge, dict), 'Edge is not an object')
        require(edge.get('source') in nodes and edge.get('target') in nodes, 'Dangling graph edge endpoint')
        require(edge.get('source') != edge.get('target'), 'Self edge is not part of original structure')
        key = tuple(edge.get(k) for k in ('source', 'target', 'type', 'scope'))
        require(key not in actual_edges, 'Duplicate graph edge: ' + str(key))
        actual_edges.add(key)
    require(actual_edges == expected_edges,
            'Edges disagree with original hierarchy/order; missing=' + str(len(expected_edges - actual_edges))
            + ', unexpected=' + str(len(actual_edges - expected_edges)))

    index_count = 0
    if retrieval_rows is not None:
        seen_index = set()
        for row in retrieval_rows:
            require(isinstance(row, dict), 'Index row must be an object')
            canonical = row.get('canonical_text_id')
            require(canonical in expected_index and canonical not in seen_index,
                    'Index has orphan, ineligible or duplicate canonical text: ' + str(canonical))
            require(row.get('book_id') == book['book_id'], 'Index row belongs to wrong book')
            require(row.get('text') == texts[canonical]['text'], 'Index text differs from canonical text')
            occurrences = row.get('occurrence_ids')
            require(isinstance(occurrences, list) and all(isinstance(p, str) for p in occurrences),
                    'Index occurrence_ids must be a string array')
            require(len(occurrences) == len(set(occurrences)), 'Duplicate index occurrence reference')
            require(set(occurrences) == set(expected_index[canonical]),
                    'Index references missing/ineligible/wrong-text paragraph occurrences')
            require(row.get('same_text_is_not_independent_evidence') is True,
                    'Index lost repeated-text evidence disclaimer')
            seen_index.add(canonical)
            index_count += 1
        require(seen_index == set(expected_index), 'Index omits eligible canonical paragraph texts')

    quality_count = 0
    if quality_rows is not None:
        seen_issues = set()
        for row in quality_rows:
            require(isinstance(row, dict), 'Quality issue row is not an object')
            identifier = row.get('id')
            require(identifier in expected_issues and identifier not in seen_issues,
                    'Orphan/duplicate quality issue row: ' + str(identifier))
            require(row == expected_issues[identifier], 'Quality issue differs from graph record: ' + identifier)
            seen_issues.add(identifier)
            quality_count += 1
        require(seen_issues == set(expected_issues), 'Quality issue file omits flagged records')

    counts.update({'book_count': 1, 'chapter_count': len(tree['chapters']), 'heading_count': len(headings),
                   'section_count': len(headings) - len(tree['chapters']),
                   'paragraph_count': len(used_paragraphs), 'unique_paragraph_texts': len(texts),
                   'source_fragment_count': len(fragments), 'graph_node_count': len(nodes),
                   'graph_edge_count': len(actual_edges), 'direct_raw_compared_fields': len(records),
                   'stored_paragraph_chars': sum(len(row['text']) for row in texts.values()),
                   'retrieval_unique_texts': len(expected_index), 'retrieval_index_rows': index_count})
    counts.update({'eligible_paragraph_count': counts['retrieval_eligible_paragraphs'],
                   'indexed_text_count': len(expected_index), 'image_asset_count': len(assets),
                   'resolved_image_asset_count': sum(a['resolution_status'].startswith('resolved') for a in assets.values()),
                   'unresolved_image_asset_count': sum(not a['resolution_status'].startswith('resolved') for a in assets.values()),
                   'quality_issue_records': len(expected_issues), 'quality_issue_rows': quality_count,
                   'recovery_source_file_count': len(recovery_sources)})
    # Always emit zero-valued quarantine counts, including books without quarantine.
    for key in ('retrieval_ineligible_paragraphs', 'retrieval_eligible_paragraphs',
                'source_fragment_chars', 'separator_chars', 'empty_fields', 'whitespace_only_fields',
                'heading_fragment_count', 'image_only_fragment_count', 'empty_math_fragment_count',
                'nonparagraph_only_fields', 'recovery_asset_reference_count', 'recovered_formula_image_count',
                'ocr_boundary_recovered_paragraph_count', 'recovery_records_checked', 'recovery_matches_checked'):
        counts.setdefault(key, 0)
    counts['stored_text_chars'] = counts['stored_paragraph_chars'] + counts['separator_chars'] + counts['source_fragment_chars']
    require(counts['direct_raw_compared_chars'] == counts['normalized_field_chars'],
            'Global direct character comparison did not cover all content')
    summary = graph.get('summary')
    require(isinstance(summary, dict), 'Graph summary must be an object')
    checked_summary_keys = []
    for key in ('chapter_count', 'heading_count', 'section_count', 'paragraph_count',
                'unique_paragraph_texts', 'empty_fields', 'whitespace_only_fields',
                'page_missing_headings', 'normalized_field_chars', 'stored_paragraph_chars',
                'separator_chars', 'stored_text_chars', 'source_fragment_count', 'source_fragment_chars',
                'nonparagraph_only_fields', 'heading_fragment_count', 'image_only_fragment_count',
                'empty_math_fragment_count', 'eligible_paragraph_count', 'indexed_text_count',
                'image_asset_count', 'resolved_image_asset_count', 'unresolved_image_asset_count',
                'recovered_formula_image_count', 'ocr_boundary_recovered_paragraph_count'):
        if key in summary:
            require(type(summary[key]) is int and summary[key] == counts[key], 'Incorrect graph summary: ' + key)
            checked_summary_keys.append(key)
    require(summary.get('full_field_reconstruction') is True, 'Missing field reconstruction declaration')
    return {'status': 'passed', 'schema_version': schema, 'counts': dict(counts),
            'direct_raw_character_comparison': True, 'field_comparisons': field_comparisons,
            'index_validated': retrieval_rows is not None, 'checked_summary_keys': checked_summary_keys,
            'quality_issues_validated': quality_rows is not None, 'asset_file_hashes': asset_file_hashes,
            'recovery_source_hashes': recovery_hashes,
            'quality_flags': dict(quality_flags), 'quarantined_paragraph_flags': dict(isolated_flags),
            'source_fragment_quality_flags': dict(fragment_flags),
            'source_fragment_kinds': dict(fragment_kinds), 'paragraph_content_kinds': dict(content_kinds)}


def jsonl_rows(path):
    with path.open('r', encoding='utf-8-sig') as stream:
        for number, line in enumerate(stream, 1):
            require(bool(line.strip()), 'Empty JSONL line ' + str(number))
            yield parse_json(line, str(path) + ':' + str(number))


def verify_export(manifest_path: Path, output: Path) -> dict:
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    report = {'schema_version': 'independent-text-graph-verification-v1',
              'started_at_utc': datetime.now(timezone.utc).isoformat(), 'status': 'failed',
              'verifier_sha256': file_digest(Path(__file__)), 'errors': [], 'books': [],
              'hashes': {'inputs': [], 'outputs': []}, 'counts': {},
              'direct_raw_character_comparison': False,
              'not_verified': ['PDF/OCR correctness', 'physical page occurrence identity',
                               'semantic truth', 'correctness of cleaning/eligibility judgments']}
    try:
        manifest, source_manifest_hash, source_manifest_size = load_json(manifest_path)
        exported, export_manifest_hash, export_manifest_size = load_json(output / 'manifest.json')
        report['hashes']['source_manifest'] = {'path': str(manifest_path), 'sha256': source_manifest_hash,
                                              'bytes': source_manifest_size}
        report['hashes']['export_manifest'] = {'path': str(output / 'manifest.json'),
                                              'sha256': export_manifest_hash, 'bytes': export_manifest_size}
        require(isinstance(manifest, dict) and isinstance(manifest.get('books'), list) and manifest['books'],
                'Input manifest needs nonempty books array')
        require(isinstance(exported, dict), 'Export manifest must be an object')
        require(exported.get('schema_version') in {'paragraph-text-graph-manifest-v1',
                                                   'paragraph-text-graph-manifest-v2'}, 'Unknown export manifest schema')
        for flag in ('pdf_ocr_verified', 'semantic_verified', 'physical_occurrence_verified'):
            require(exported.get(flag) is False, 'Export manifest has unjustified verification flag: ' + flag)
        require(exported.get('api_calls') == 0, 'Export manifest API calls must be zero')
        implementation_hashes = exported.get('implementation_sha256', {})
        require(isinstance(implementation_hashes, dict), 'implementation_sha256 must be an object')
        report['hashes']['implementation'] = []
        for name, expected_hash in implementation_hashes.items():
            require(isinstance(name, str) and Path(name).name == name, 'Unsafe implementation filename')
            actual_hash = file_digest(Path(__file__).with_name(name))
            report['hashes']['implementation'].append({'path': name, 'sha256': actual_hash,
                                                       'expected_sha256': expected_hash})
            require(actual_hash == expected_hash, 'Implementation source changed since export: ' + name)
        if 'build_text_graphs.py' in implementation_hashes:
            require(exported.get('exporter_sha256') == implementation_hashes['build_text_graphs.py'],
                    'exporter_sha256 disagrees with recorded implementation hash')
        source_metadata = exported.get('source_manifest')
        require(isinstance(source_metadata, dict) and source_metadata.get('sha256') == source_manifest_hash,
                'Export was not produced from this frozen manifest hash')
        require(Path(source_metadata.get('path', '')).resolve() == manifest_path,
                'Export source manifest path differs from requested manifest')
        require(isinstance(exported.get('books'), list), 'Export manifest books must be an array')
        input_rows, output_rows = {}, {}
        for rows, destination, label in ((manifest['books'], input_rows, 'input'),
                                         (exported['books'], output_rows, 'output')):
            for row in rows:
                require(isinstance(row, dict), label + ' book is not an object')
                identifier = row.get('book_id')
                require(isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', identifier),
                        label + ' has unsafe book_id')
                require(identifier.casefold() not in {key.casefold() for key in destination},
                        label + ' has duplicate book_id')
                destination[identifier] = row
        require(set(input_rows) == set(output_rows), 'Export book list differs from frozen manifest')
        report['expected_book_count'] = len(input_rows)
        artifact_paths = set()
        totals = Counter()
        for identifier, row in input_rows.items():
            result = {'book_id': identifier, 'status': 'failed', 'errors': []}
            try:
                outrow = output_rows[identifier]
                for key, value in row.items():
                    require(outrow.get(key) == value, 'Changed frozen book metadata: ' + key)
                source = rooted_path(ROOT, row.get('input_path'), identifier)
                tree, source_hash, source_size = load_json(source)
                input_info = {'book_id': identifier, 'path': row['input_path'], 'sha256': source_hash,
                              'bytes': source_size, 'expected_sha256': row.get('input_sha256')}
                report['hashes']['inputs'].append(input_info)
                require(source_hash == row.get('input_sha256'), 'Frozen input SHA-256 mismatch')
                artifacts = outrow.get('artifacts')
                require(isinstance(artifacts, list) and artifacts, 'No artifact hash declarations')
                declared = set()
                for artifact in artifacts:
                    require(isinstance(artifact, dict), 'Artifact declaration is not an object')
                    path = rooted_path(output, artifact.get('path'), identifier + ' artifact')
                    relative = path.relative_to(output).as_posix()
                    require(relative.startswith(identifier + '/'), 'Artifact is outside its book directory')
                    require(relative not in artifact_paths, 'Duplicate artifact path')
                    artifact_paths.add(relative)
                    declared.add(relative)
                    actual_hash, actual_size = file_digest(path), path.stat().st_size
                    report['hashes']['outputs'].append({'book_id': identifier, 'path': relative,
                        'sha256': actual_hash, 'bytes': actual_size, 'expected_sha256': artifact.get('sha256')})
                    require(actual_hash == artifact.get('sha256'), 'Artifact SHA-256 mismatch: ' + relative)
                    require(actual_size == artifact.get('bytes'), 'Artifact byte count mismatch: ' + relative)
                required = {identifier + '/text_graph.json', identifier + '/retrieval_index.jsonl',
                            identifier + '/目录文本图.md'}
                is_v2 = exported['schema_version'].endswith('-v2')
                if is_v2:
                    required.add(identifier + '/quality_issues.jsonl')
                require(required.issubset(declared), 'Required graph/index/outline artifacts are not hashed')
                graph_path = output / identifier / 'text_graph.json'
                graph, parsed_graph_hash, _ = load_json(graph_path)
                expected_graph_hash = next(a['sha256'] for a in artifacts if a['path'] == identifier + '/text_graph.json')
                require(parsed_graph_hash == expected_graph_hash, 'Graph changed while loading')
                require(graph.get('schema_version') == ('paragraph-text-graph-v2' if is_v2 else 'paragraph-text-graph-v1'),
                        'Manifest and graph schema versions disagree')
                checked = validate_graph(tree, graph, row, jsonl_rows(output / identifier / 'retrieval_index.jsonl'),
                                         jsonl_rows(output / identifier / 'quality_issues.jsonl') if is_v2 else None)
                require(outrow.get('summary') == graph.get('summary'), 'Manifest/graph summary mismatch')
                require(file_digest(source) == source_hash, 'Frozen source changed during verification')
                # Every book artifact is hashed again after parsing, including the streamed index.
                for artifact in artifacts:
                    require(file_digest(rooted_path(output, artifact['path'], 'artifact')) == artifact['sha256'],
                            'Artifact changed during verification: ' + artifact['path'])
                result.update(checked)
                totals.update(checked['counts'])
            except Exception as exc:
                result['errors'].append(type(exc).__name__ + ': ' + str(exc))
                report['errors'].append(identifier + ': ' + result['errors'][-1])
            report['books'].append(result)
            print(f"[{len(report['books'])}/{len(input_rows)}] {identifier} {result['status']}", flush=True)
        observed_files = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}
        unlisted = observed_files - artifact_paths - {'manifest.json'}
        require(not unlisted, 'Unlisted files in export directory: ' + ', '.join(sorted(unlisted)))
        report['counts'] = dict(totals)
        if not report['errors']:
            summary = exported.get('summary')
            require(isinstance(summary, dict), 'Missing export summary')
            require(summary.get('book_count') == len(input_rows), 'Wrong total book_count')
            require(summary.get('full_field_reconstruction') is True, 'Missing total reconstruction declaration')
            for key, value in summary.items():
                if key in totals:
                    require(type(value) is int and value == totals[key], 'Wrong aggregate summary: ' + key)
            require(file_digest(manifest_path) == source_manifest_hash, 'Frozen manifest changed during verification')
            require(file_digest(output / 'manifest.json') == export_manifest_hash, 'Export manifest changed during verification')
            report['direct_raw_character_comparison'] = True
            report['status'] = 'passed'
    except Exception as exc:
        report['errors'].append(type(exc).__name__ + ': ' + str(exc))
    report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
    report['passed_book_count'] = sum(book['status'] == 'passed' for book in report['books'])
    report['failed_book_count'] = sum(book['status'] != 'passed' for book in report['books'])
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args(argv)
    report_path = args.report.resolve()
    if report_path.exists() or report_path.is_symlink():
        parser.error('Report must be a new file; existing paths are never overwritten')
    if not report_path.parent.is_dir():
        parser.error('Report parent directory must already exist')
    result = verify_export(args.manifest, args.output)
    # x mode also protects against a file appearing after preflight.
    with report_path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'status': result['status'], 'report': str(report_path),
                      'passed_books': result['passed_book_count'],
                      'failed_books': result['failed_book_count'],
                      'errors': len(result['errors'])}, ensure_ascii=False))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
