"""从冻结 JSON 书单导出纯离线段落文本图。

最小节点是原 JSON 字段中的段落出现实例；仅完全相同的段落文本共享存储。
不同节点未必对应 PDF 不同物理位置，父章节可能重复子节正文。
正文仅 CRLF/CR→LF；明确标题与附件移到来源元数据，长段不按字数截断。
代码、数学块保护；可核验的 OCR 标题、公式原图和列表边界记录上游证据。
段落保留末行换行，额外空白行保存在 heading.text_layout，不另建节点。
页码来自原 heading，仅为范围提示，不是段落精确 PDF 定位。
本模块仅使用 Python 标准库，不加载模型、不请求网络或 API。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paragraph_cleaning import clean_spans
from text_graph_assets import AssetResolver
from source_recovery import RecoveryIndex


ROOT = Path(__file__).resolve().parents[2]
PAGE_KEYS = ('toc_page_start', 'toc_page_end', 'pdf_page_start', 'pdf_page_end')
STATUS = 'structural_unverified'
RULE = 'paragraph-v2;typed_code_math;headings_images_empty_math_to_metadata;no_length_split;lossless_layout'


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def stable_id(prefix: str, parts) -> str:
    raw = json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return prefix + ':' + sha256_bytes(raw)






def build_graph(tree: dict, book: dict, *, asset_resolver=None, recovery_index=None) -> dict:
    if not isinstance(tree, dict) or not isinstance(tree.get('chapters'), list) or not tree['chapters']:
        raise ValueError('Book must have a nonempty chapters array')
    book_id = book['book_id']
    title = book['book_title']
    nodes = [{'id': book_id, 'type': 'book', 'title': title,
              'source_book_title': tree.get('book_title'),
              'source_origin': {'input_path': book['input_path'], 'json_pointer': ''},
              'toc_pages_pdf': tree.get('toc_pages_pdf'),
              'toc_to_pdf_offset': tree.get('toc_to_pdf_offset')}]
    edges = []
    texts = {}
    headings = []
    paragraphs = []
    fragments = []
    separator_chars = 0
    ocr_headings = recovery_index.heading_texts() if recovery_index else []
    recovery_cache = {}

    def candidates(raw, pointer):
        rows = [{'id': stable_id('heading', [book_id, pointer]),
                 **{k: raw.get(k) for k in ('label', 'marker', 'title')}}]
        for index, child in enumerate(raw.get('children') or []):
            rows.extend(candidates(child, pointer + '/children/' + str(index)))
        return rows

    def edge(source, target, relation, scope):
        edges.append({'source': source, 'target': target, 'type': relation, 'scope': scope})

    def walk_siblings(children, parent_id, pointer, ancestors, depth):
        nonlocal separator_chars
        previous = None
        for i, raw in enumerate(children):
            if not isinstance(raw, dict):
                raise ValueError('Heading must be an object')
            node_pointer = pointer + '/' + str(i)
            heading_id = stable_id('heading', [book_id, node_pointer])
            label = raw.get('label')
            display = str(label) if label is not None else ' '.join(str(raw.get(k) or '').strip() for k in ('marker', 'title')).strip()
            title_path = ancestors + [display]
            content = raw.get('content', '')
            if content is None:
                content = ''
            if not isinstance(content, str):
                raise ValueError('content must be a string or null at ' + node_pointer)
            content = content.replace('\r\n', '\n').replace('\r', '\n')
            heading = {'id': heading_id, 'type': 'chapter' if depth == 1 else 'section',
                       'marker': raw.get('marker'), 'title': raw.get('title'), 'label': label,
                       'level': raw.get('level'), 'tree_depth': depth, 'title_path': title_path,
                       'source_origin': {'input_path': book['input_path'], 'json_pointer': node_pointer},
                       'content_origin': {'input_path': book['input_path'], 'json_pointer': node_pointer + '/content'},
                       'normalized_chars': len(content), 'normalized_text_sha256': sha256_bytes(content.encode('utf-8')),
                       'has_non_whitespace_content': bool(content.strip()),
                       'text_layout': [], **{k: raw.get(k) for k in PAGE_KEYS}}
            nodes.append(heading)
            headings.append(heading)
            edge(parent_id, heading_id, 'contains', 'original_heading_hierarchy')
            if previous is not None:
                edge(previous, heading_id, 'next', 'sibling_heading_order')
            previous = heading_id
            previous_paragraph = None
            reconstruction = []
            paragraph_index = 0
            cleaned = clean_spans(content, candidates(raw, node_pointer), heading_id,
                                  extra_heading_texts=ocr_headings)
            augmented = []
            for span in cleaned:
                value = content[span['start']:span['end']]
                investigate = (span.get('fragment_kind') == 'empty_math' or
                               (span['kind'] == 'paragraph' and len(value) > 4000 and
                                span.get('content_kind') not in ('fenced_code', 'math', 'table')))
                if recovery_index and investigate:
                    if value not in recovery_cache:
                        recovery_cache[value] = recovery_index.recover(value)
                    recovery = recovery_cache[value]
                    span['recovery'] = recovery
                    span['recovery_original_span'] = {'field_start': span['start'], 'field_end': span['end']}
                    breaks = recovery.get('break_offsets', [])
                    if breaks:
                        if recovery.get('boundary_status') != 'verified' or breaks != sorted(set(breaks)) or not all(0 < p < len(value) for p in breaks):
                            raise ValueError('Invalid recovered OCR boundaries')
                        bounds = [0] + breaks + [len(value)]
                        for left, right in zip(bounds, bounds[1:]):
                            if not value[left:right].strip():
                                raise ValueError('Recovered boundary creates empty paragraph')
                            part = {**span, 'start': span['start'] + left, 'end': span['start'] + right,
                                    'content_kind': 'list', 'boundary_source': recovery['boundary_source'],
                                    'quality_flags': [f for f in span.get('quality_flags', []) if f != 'long_block_review']}
                            if right - left > 4000:
                                part['quality_flags'].append('long_block_review')
                            augmented.append(part)
                        continue
                    if recovery.get('boundary_status') == 'needs_review':
                        span['quality_flags'] = list(span.get('quality_flags', [])) + ['ocr_list_boundaries_review']
                augmented.append(span)
            cleaned = augmented
            for span in cleaned:
                part_kind, start, end = span['kind'], span['start'], span['end']
                value = content[start:end]
                if part_kind == 'separator':
                    heading['text_layout'].append({'separator': value, 'field_start': start, 'field_end': end})
                    separator_chars += len(value)
                    reconstruction.append(value)
                    continue
                asset_ids = asset_resolver.references(value) if asset_resolver and span.get('content_kind') != 'fenced_code' else []
                if part_kind == 'source_fragment':
                    fragment_id = stable_id('source-fragment', [book_id, node_pointer, start, end, value])
                    recovery = span.get('recovery')
                    recovery_asset_ids = []
                    if asset_resolver and recovery and recovery.get('formula_image'):
                        image_ref = recovery['formula_image']['img_path']
                        recovery_asset_ids = asset_resolver.references('![](' + image_ref + ')')
                    fragments.append({'id': fragment_id, 'heading_id': heading_id,
                                      'fragment_kind': span['fragment_kind'], 'text': value,
                                      'field_start': start, 'field_end': end,
                                      'source_origin': {'input_path': book['input_path'], 'json_pointer': node_pointer + '/content'},
                                      'matched_heading_ids': span.get('matched_heading_ids', []),
                                      'quality_flags': span.get('quality_flags', []),
                                      'asset_ids': asset_ids, 'recovery_asset_ids': recovery_asset_ids,
                                      'recovery': recovery,
                                      'recovery_original_span': span.get('recovery_original_span'),
                                      'retrieval_eligible': False})
                    heading['text_layout'].append({'source_fragment_id': fragment_id, 'field_start': start, 'field_end': end})
                    reconstruction.append(value)
                    continue
                canonical = 'text:' + sha256_bytes(value.encode('utf-8'))
                if canonical in texts and texts[canonical]['text'] != value:
                    raise ValueError('SHA-256 collision in paragraph text storage')
                texts.setdefault(canonical, {'id': canonical, 'text': value, 'chars': len(value),
                                             'storage_semantics': 'exact_text_deduplication_only'})
                paragraph_id = stable_id('paragraph', [book_id, node_pointer, paragraph_index, start, end, canonical])
                paragraph = {'id': paragraph_id, 'type': 'paragraph', 'heading_id': heading_id,
                             'occurrence_scope': 'original_JSON_field_not_verified_physical_occurrence',
                             'index': paragraph_index, 'field_start': start, 'field_end': end,
                             'canonical_text_id': canonical,
                             'text_ref': {'canonical_text_id': canonical, 'start': 0, 'end': len(value)},
                             'content_kind': span['content_kind'], 'title_path': title_path,
                             'quality_flags': span.get('quality_flags', []),
                             'retrieval_eligible': not any('unclosed' in flag for flag in span.get('quality_flags', [])),
                             'asset_ids': asset_ids,
                             'recovery': span.get('recovery'),
                             'recovery_original_span': span.get('recovery_original_span'),
                             'boundary_source': span.get('boundary_source', 'markdown_structure'),
                             'source_origin': {'input_path': book['input_path'], 'json_pointer': node_pointer + '/content'},
                             'page_scope': 'original_heading_range_not_exact_paragraph_location',
                             **{k: raw.get(k) for k in PAGE_KEYS}}
                nodes.append(paragraph)
                paragraphs.append(paragraph)
                edge(heading_id, paragraph_id, 'contains', 'paragraph_occurrence_in_field')
                if previous_paragraph is not None:
                    edge(previous_paragraph, paragraph_id, 'next', 'field_paragraph_order')
                previous_paragraph = paragraph_id
                heading['text_layout'].append({'paragraph_id': paragraph_id, 'field_start': start, 'field_end': end})
                reconstruction.append(texts[canonical]['text'])
                paragraph_index += 1
            if ''.join(reconstruction) != content:
                raise ValueError('Full field reconstruction failed at ' + node_pointer)
            heading['paragraph_count'] = paragraph_index
            children = raw.get('children', [])
            if children is None:
                children = []
            if not isinstance(children, list):
                raise ValueError('children must be an array at ' + node_pointer)
            walk_siblings(children, heading_id, node_pointer + '/children', title_path, depth + 1)

    walk_siblings(tree['chapters'], book_id, '/chapters', [title], 1)
    summary = {'chapter_count': len(tree['chapters']), 'heading_count': len(headings),
               'section_count': sum(n['type'] == 'section' for n in headings),
               'paragraph_count': len(paragraphs), 'unique_paragraph_texts': len(texts),
               'empty_fields': sum(n['normalized_chars'] == 0 for n in headings),
               'whitespace_only_fields': sum(n['normalized_chars'] > 0 and not n['has_non_whitespace_content'] for n in headings),
               'nonparagraph_only_fields': sum(n['has_non_whitespace_content'] and n['paragraph_count'] == 0 for n in headings),
               'page_missing_headings': sum(n['pdf_page_start'] is None or n['pdf_page_end'] is None for n in headings),
               'normalized_field_chars': sum(n['normalized_chars'] for n in headings),
               'stored_paragraph_chars': sum(n['chars'] for n in texts.values()),
               'separator_chars': separator_chars,
               'source_fragment_count': len(fragments),
               'source_fragment_chars': sum(len(f['text']) for f in fragments),
               'heading_fragment_count': sum(f['fragment_kind'] in ('heading_echo', 'markdown_heading', 'ocr_heading') for f in fragments),
               'image_only_fragment_count': sum(f['fragment_kind'] == 'image_only' for f in fragments),
               'empty_math_fragment_count': sum(f['fragment_kind'] == 'empty_math' for f in fragments),
               'recovered_formula_image_count': sum(bool(f['recovery_asset_ids']) for f in fragments),
               'ocr_boundary_recovered_paragraph_count': sum(n['boundary_source'] != 'markdown_structure' for n in paragraphs),
               'eligible_paragraph_count': sum(n['retrieval_eligible'] for n in paragraphs),
               'indexed_text_count': len({n['canonical_text_id'] for n in paragraphs if n['retrieval_eligible']}),
               'stored_text_chars': sum(n['chars'] for n in texts.values()) + separator_chars + sum(len(f['text']) for f in fragments),
               'full_field_reconstruction': True}
    assets = asset_resolver.records() if asset_resolver else []
    asset_by_id = {a['id']: a for a in assets}
    for item in paragraphs + fragments:
        for asset_id in item['asset_ids']:
            status = asset_by_id[asset_id]['resolution_status']
            if not status.startswith('resolved'):
                flag = 'image_' + status
                if flag not in item['quality_flags']:
                    item['quality_flags'].append(flag)
    summary.update(image_asset_count=len(assets),
                   resolved_image_asset_count=sum(a['resolution_status'].startswith('resolved') for a in assets),
                   unresolved_image_asset_count=sum(not a['resolution_status'].startswith('resolved') for a in assets))
    recovery_sources = []
    if recovery_index:
        for row in recovery_index.records():
            recovery_sources.append({**row, 'path': (Path(book['input_path']).parent / row['path']).as_posix()})
    return {'schema_version': 'paragraph-text-graph-v2', 'book_id': book_id, 'book_title': title,
            'input_path': book['input_path'], 'input_sha256': book['input_sha256'],
            'status': STATUS, 'pdf_ocr_verified': False, 'semantic_verified': False,
            'physical_occurrence_verified': False,
            'dedup_scope': 'within_each_book',
            'api_calls': 0, 'minimum_node': 'paragraph', 'paragraph_rule': RULE,
            'offset_unit': 'Unicode code points after CRLF/CR to LF only',
            'nodes': nodes, 'edges': edges, 'paragraph_texts': list(texts.values()),
            'source_fragments': fragments, 'assets': assets,
            'recovery_sources': recovery_sources, 'summary': summary}


def write_outline(graph: dict, path: Path):
    lines = ['# ' + str(graph['book_title']) + '：目录文本图', '',
             '- 状态：structural_unverified；PDF/OCR 与语义均未核验；API 调用 0。',
             '- 最小节点为原 JSON 字段中的段落出现实例。同文不同字段不合并节点；相同正文只共享文本库。',
             '- 物理出处未经验证：父章节可能重复子节正文，不同节点不保证是 PDF 不同物理位置。',
             '- 段落按空行分界，fenced code 与 $$ 数学块内部不拆；长段不按字数切开。',
             '- 段落保留末行换行；额外空白行存在 text_layout 中，可逐字重建换行规范化原字段。',
             '- contains/next 仅表示原层级、同级顺序或同字段段落顺序，不是概念/先修关系。',
             '- 页码保留原章节范围，不表示段落精确 PDF 位置。后续引用需去重/选择最细章节，同文多处不可累加为独立证据。',
             '- retrieval_index.jsonl 每份 canonical 正文一行，并返回其全部 occurrence_ids。', '',
             '- v2：标题回声、纯图片与缺主体公式存入 source_fragments，不作为正文段落或默认检索行。',
             '- assets 记录图片重定位；无法确认的 OCR 内容只报告，不补造。', '',
             '## 完整章节树', '']
    for node in graph['nodes']:
        if node['type'] not in ('chapter', 'section'):
            continue
        label = node['label'] if node['label'] is not None else node['title_path'][-1]
        label = str(label or '（无标题）').replace('\n', ' ').replace('\r', ' ')
        page = lambda key: '缺失' if node[key] is None else str(node[key])
        lines.append('  ' * (node['tree_depth'] - 1) + '- ' + label
                     + ' ｜字数 ' + str(node['normalized_chars'])
                     + '｜段落 ' + str(node['paragraph_count'])
                     + '｜TOC ' + page('toc_page_start') + '–' + page('toc_page_end')
                     + '｜PDF ' + page('pdf_page_start') + '–' + page('pdf_page_end')
                     + '｜`' + node['source_origin']['json_pointer'] + '`')
    lines.extend(['', '## 汇总', '', '```json', json.dumps(graph['summary'], ensure_ascii=False, indent=2), '```', ''])
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write('\n'.join(lines))


def export_manifest(manifest_path: Path, output: Path) -> dict:
    manifest_path, output = Path(manifest_path).resolve(), Path(output).resolve()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Output already exists: ' + str(output))
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode('utf-8-sig'))
    books = manifest.get('books') if isinstance(manifest, dict) else None
    if not isinstance(books, list) or not books:
        raise ValueError('Manifest requires a nonempty books array')
    validated = []
    ids = set()
    for row in books:
        if not isinstance(row, dict) or any(k not in row for k in ('book_id', 'book_dir', 'book_title', 'input_path', 'input_sha256')):
            raise ValueError('Invalid manifest book entry')
        book_id = row['book_id']
        if not isinstance(book_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', book_id):
            raise ValueError('Unsafe book_id')
        if book_id.casefold() in ids or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])', book_id):
            raise ValueError('Duplicate or reserved book_id: ' + book_id)
        ids.add(book_id.casefold())
        if not isinstance(row['input_path'], str) or Path(row['input_path']).is_absolute():
            raise ValueError('input_path must be relative to project root')
        source = (ROOT / row['input_path']).resolve()
        try:
            source.relative_to(ROOT)
        except ValueError:
            raise ValueError('Input path escapes project root: ' + row['input_path'])
        if not source.is_file():
            raise ValueError('Input file does not exist: ' + row['input_path'])
        if not isinstance(row['input_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', row['input_sha256']):
            raise ValueError('Invalid input_sha256: ' + book_id)
        if file_sha256(source) != row['input_sha256']:
            raise ValueError('Input SHA-256 mismatch: ' + book_id)
        validated.append((dict(row), source))
    output.mkdir(parents=True, exist_ok=False)
    results = []
    for index, (row, source) in enumerate(validated, 1):
        raw = source.read_bytes()
        if sha256_bytes(raw) != row['input_sha256']:
            raise ValueError('Input changed after preflight: ' + row['book_id'])
        graph = build_graph(json.loads(raw.decode('utf-8-sig')), row,
                            asset_resolver=AssetResolver(source.parent, ROOT),
                            recovery_index=RecoveryIndex(source.parent))
        folder = output / row['book_id']
        folder.mkdir()
        graph_path = folder / 'text_graph.json'
        with graph_path.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(graph, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        outline_path = folder / '目录文本图.md'
        write_outline(graph, outline_path)
        occurrences = {}
        for node in graph['nodes']:
            if node['type'] == 'paragraph' and node['retrieval_eligible']:
                occurrences.setdefault(node['canonical_text_id'], []).append(node['id'])
        retrieval_path = folder / 'retrieval_index.jsonl'
        with retrieval_path.open('x', encoding='utf-8', newline='\n') as stream:
            for text in graph['paragraph_texts']:
                if text['id'] not in occurrences:
                    continue
                item = {'book_id': row['book_id'], 'canonical_text_id': text['id'],
                        'dedup_scope': 'within_each_book',
                        'text': text['text'], 'occurrence_ids': occurrences[text['id']],
                        'same_text_is_not_independent_evidence': True}
                stream.write(json.dumps(item, ensure_ascii=False) + '\n')
        issue_path = folder / 'quality_issues.jsonl'
        with issue_path.open('x', encoding='utf-8', newline='\n') as stream:
            for item in graph['nodes'] + graph['source_fragments']:
                if item.get('quality_flags'):
                    issue = {k: item[k] for k in ('id', 'source_origin', 'field_start', 'field_end', 'quality_flags', 'retrieval_eligible')}
                    issue['record_kind'] = item.get('fragment_kind', item.get('content_kind'))
                    stream.write(json.dumps(issue, ensure_ascii=False) + '\n')
        artifacts = [{'path': p.relative_to(output).as_posix(), 'sha256': file_sha256(p), 'bytes': p.stat().st_size}
                     for p in (graph_path, outline_path, retrieval_path, issue_path)]
        results.append({**row, 'artifacts': artifacts, 'summary': graph['summary']})
        print(f"[{index}/{len(validated)}] {row['book_id']} paragraphs={graph['summary']['paragraph_count']} unique={graph['summary']['unique_paragraph_texts']} reconstruction=ok", flush=True)
    summary = {'book_count': len(results)}
    for key, value in results[0]['summary'].items():
        summary[key] = all(r['summary'][key] for r in results) if isinstance(value, bool) else sum(r['summary'][key] for r in results)
    result = {'schema_version': 'paragraph-text-graph-manifest-v2', 'status': STATUS,
              'pdf_ocr_verified': False, 'semantic_verified': False, 'api_calls': 0,
              'physical_occurrence_verified': False,
              'dedup_scope': 'within_each_book',
              'source_manifest': {'path': str(manifest_path), 'sha256': sha256_bytes(manifest_bytes)},
              'exporter_sha256': file_sha256(Path(__file__)), 'paragraph_rule': RULE,
              'implementation_sha256': {name: file_sha256(Path(__file__).with_name(name)) for name in
                                        ('build_text_graphs.py', 'paragraph_cleaning.py', 'text_graph_assets.py', 'source_recovery.py')},
              'summary': summary, 'books': results}
    # 只有全部成功才落总清单；失败保留局部产物，拒绝原地续写。
    with (output / 'manifest.json').open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    export_manifest(args.manifest, args.output)


if __name__ == '__main__':
    main()
