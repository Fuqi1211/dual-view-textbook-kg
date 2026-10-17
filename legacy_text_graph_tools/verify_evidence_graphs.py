"""Independent v3 audit against original JSON, frozen v2, plan and JSONL index.

Does not import the exporter or its verifier. Only writes 独立验收.json inside
the explicitly supplied, existing output directory. A passed report is a
structural/provenance audit, not a new OCR or human semantic verification.
"""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def within(base, value):
    path = (base / value).resolve()
    path.relative_to(base.resolve())
    require(path.is_file(), 'Missing file: ' + str(path))
    return path


def by_id(rows, label):
    result = {x['id']: x for x in rows}
    require(len(result) == len(rows), 'Duplicate IDs: ' + label)
    return result


def pointer(document, value):
    current = document
    for token in value.split('/')[1:]:
        token = token.replace('~1', '/').replace('~0', '~')
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def normalized(value):
    require(value is None or isinstance(value, str), 'Source content is not string/null')
    return (value or '').replace('\r\n', '\n').replace('\r', '\n')


def edge_key(edge):
    return tuple(edge[k] for k in ('source', 'target', 'type', 'scope'))


def record_text(record, texts):
    return record['text'] if 'text' in record else texts[record['canonical_text_id']]['text']


def sample_ids(item):
    result = set(item.get('sample_ids', []))
    if item.get('sample_id'):
        result.add(item['sample_id'])
    return result


def audit_graph(graph, old, tree, patches, asset_changes, index_rows, input_path,
                new_headings=(), heading_page_overrides=()):
    """Pure, independent assertions; inputs may also be supplied by tests."""
    require(graph['schema_version'] == 'paragraph-text-graph-v3', 'Not a v3 graph')
    nodes = by_id(graph['nodes'], 'active nodes')
    archived = by_id(graph['source_record_archive'], 'archive')
    fragments = by_id(graph['source_fragments'], 'source fragments')
    texts = by_id(graph['paragraph_texts'], 'text storage')
    old_nodes = by_id(old['nodes'], 'v2 nodes')
    old_fragments = by_id(old['source_fragments'], 'v2 fragments')
    old_texts = by_id(old['paragraph_texts'], 'v2 texts')
    originals = dict(old_nodes, **old_fragments)
    retained = dict(nodes, **archived)
    new_heading_map = {h['id']: h for h in new_headings}
    for p in patches:
        if p.get('new_heading'):
            new_heading_map[p['new_heading']['id']] = p['new_heading']
    for hid, planned in new_heading_map.items():
        require(hid not in old_nodes and hid in nodes, 'Added heading collides with/missing from graph')
        heading = nodes[hid]
        require(all(heading.get(k) == v for k, v in planned.items()), 'Added heading differs from plan')
        require(heading['type'] == 'section' and heading['title'] == planned['label'], 'Added heading type/title wrong')
        parent = nodes[planned['parent_id']]
        require(heading['title_path'] == parent.get('title_path', [graph['book_title']]) + [planned['label']], 'Added heading title path wrong')
        require(heading.get('evidence') and heading['provenance'] == 'AI_PDF_evidence_added_heading', 'Added heading lacks evidence provenance')
    override_map = {x['heading_id']: x for x in heading_page_overrides}
    require(len(override_map) == len(heading_page_overrides), 'Duplicate heading page override')
    for hid, change in override_map.items():
        heading, original = nodes[hid], old_nodes[hid]
        old_range = {k: original.get(k) for k in ('pdf_page_start', 'pdf_page_end')}
        require(old_range == {k: change['original_' + k] for k in old_range}, 'Page override stale original range')
        require(heading['source_pdf_page_range'] == old_range, 'Original heading page range not preserved')
        require(all(heading[k] == change['corrected_' + k] for k in old_range), 'Page override not applied')
        require(heading['page_range_repair_evidence'] == change['evidence'] and heading['page_range_status'] == 'extended_to_cover_observed_page_not_full_section_audit', 'Page override evidence/scope mismatch')
    require(not (nodes.keys() & archived.keys()), 'Archive record leaked into active nodes')
    require(graph['source_fragments'] == old['source_fragments'], 'Original source fragments changed')
    for tid, row in texts.items():
        require(tid == 'text:' + digest(row['text']), 'Text hash mismatch: ' + tid)
        require(row['chars'] == len(row['text']), 'Text chars mismatch: ' + tid)
    for tid, row in old_texts.items():
        require(tid in texts and texts[tid] == row, 'Original text storage changed: ' + tid)
    for rid, row in archived.items():
        require(rid in old_nodes and row == old_nodes[rid], 'Archived record differs from v2: ' + rid)
    for rid, row in old_nodes.items():
        require(rid in retained, 'Lost original node: ' + rid)
        if row['type'] == 'paragraph' and rid in nodes:
            for key in ('canonical_text_id', 'field_start', 'field_end', 'source_origin', 'heading_id', 'retrieval_eligible', 'content_kind'):
                require(nodes[rid][key] == row[key], 'Unarchived source paragraph altered: ' + rid)

    field_count = 0
    for hid, old_heading in old_nodes.items():
        if 'text_layout' not in old_heading:
            continue
        heading = nodes[hid]
        require('text_layout' not in heading, 'Ambiguous corrected text_layout retained')
        require(heading['source_text_layout'] == old_heading['text_layout'], 'Source layout changed: ' + hid)
        for key in ('label', 'title', 'marker', 'source_origin', 'content_origin', 'title_path'):
            require(heading.get(key) == old_heading.get(key), 'Original heading identity changed: ' + hid)
        if hid not in override_map:
            require(all(heading.get(k) == old_heading.get(k) for k in ('pdf_page_start', 'pdf_page_end')), 'Unplanned original heading page change')
        require(heading['source_origin']['input_path'] == input_path, 'Wrong source book')
        raw_heading = pointer(tree, heading['source_origin']['json_pointer'])
        raw = normalized(raw_heading.get('content'))
        pieces, cursor = [], 0
        for entry in heading['source_text_layout']:
            start, end = entry['field_start'], entry['field_end']
            require(start == cursor and end >= start, 'Source layout gap/overlap: ' + hid)
            if 'separator' in entry:
                value = entry['separator']
                require(not value.strip(), 'Non-whitespace separator')
            else:
                rid = entry.get('paragraph_id', entry.get('source_fragment_id'))
                record = retained[rid] if 'paragraph_id' in entry else fragments[rid]
                value = record_text(record, texts)
                require(record['field_start'] == start and record['field_end'] == end, 'Record/layout offsets differ')
                require(record['source_origin']['json_pointer'] == heading['source_origin']['json_pointer'] + '/content', 'Wrong record source field')
            require(value == raw[start:end], 'Reconstructed span differs from original JSON: ' + hid)
            pieces.append(value)
            cursor = end
        whole = ''.join(pieces)
        require(whole == raw and cursor == len(raw), 'Full source field differs from original JSON: ' + hid)
        require(len(raw) == heading['normalized_chars'] and digest(raw) == heading['normalized_text_sha256'], 'Original field summary mismatch')
        field_count += 1
    expected_field_count = 0
    def count_headings(rows):
        nonlocal expected_field_count
        for row in rows:
            expected_field_count += 1
            count_headings(row.get('children') or [])
    count_headings(tree['chapters'])
    require(field_count == expected_field_count, 'Original tree headings missing from source layout audit')

    assets = by_id(graph.get('assets', []), 'assets')
    edges = [edge_key(e) for e in graph['edges']]
    require(len(set(edges)) == len(edges), 'Duplicate graph edge')
    edge_set = set(edges)
    for source, target, kind, scope in edges:
        require(source in nodes and target in nodes, 'Edge points outside active nodes')
    source_scopes = {'original_heading_hierarchy', 'sibling_heading_order'}
    require({e for e in edges if e[3] in source_scopes} == {edge_key(e) for e in old['edges'] if e['scope'] in source_scopes}, 'Original heading hierarchy/order changed')
    for nid, node in nodes.items():
        if node.get('parent_id'):
            require(node['parent_id'] in nodes, 'Missing node parent: ' + nid)
        if 'heading_id' in node:
            require(node['heading_id'] in nodes and nodes[node['heading_id']]['type'] in ('chapter', 'section'), 'Invalid heading reference: ' + nid)
        if 'canonical_text_id' in node:
            tid = node['canonical_text_id']
            require(tid in texts, 'Missing canonical text')
            require(node['text_ref'] == {'canonical_text_id': tid, 'start': 0, 'end': len(texts[tid]['text'])}, 'Bad corrected text_ref')
        for aid in node.get('asset_ids', []) + node.get('example_literal_asset_ids', []):
            require(aid in assets, 'Missing asset reference')
        if node['type'] in ('metadata', 'bibliography_block', 'bibliography_entry'):
            require(node.get('retrieval_eligible') is False, 'Non-body node eligible for retrieval')
        if node.get('content_kind') in ('heading', 'table_continuation', 'bibliography'):
            require(not node.get('retrieval_eligible'), 'Heading/bibliography classified as default body')

    repairs = by_id(graph['repairs'], 'repairs')
    expected_plan = by_id(patches, 'plan patches')
    require(set(expected_plan) <= set(repairs), 'Plan patch missing from repaired graph')
    for pid, planned in expected_plan.items():
        require(all(repairs[pid].get(k) == v for k, v in planned.items()), 'Applied patch differs from plan: ' + pid)
    for pid, applied in repairs.items():
        old_records = [originals[rid] for rid in applied['record_ids']]
        require([record_text(r, old_texts) for r in old_records] == applied['expected_texts'], 'Patch original text precondition differs')
        out = nodes[applied['output_record_id']]
        value = applied.get('replacement_text')
        if value is None:
            require(len(old_records) == 1, 'Unspecified merged text')
            value = record_text(old_records[0], old_texts)
        require(record_text(out, texts) == value, 'Patch text was not applied: ' + pid)
        require(out['repair_id'] == pid and out['source_record_ids'] == applied['record_ids'], 'Repair provenance mismatch')
        require(out['source_origin'] == old_records[0]['source_origin'], 'Corrected record changed original source field')
        require(out['field_start'] == old_records[0]['field_start'] and out['field_end'] == old_records[0]['field_end'], 'Corrected record anchor offsets differ')
        require(out['offset_scope'] == 'original_source_anchor; corrected text uses text_ref', 'Corrected coordinates misrepresented as original slice')
        require(out['human_verified'] is False, 'AI repair marked human verified')
        require(out['source_spans'] == [{k: r[k] for k in ('id', 'source_origin', 'field_start', 'field_end')} for r in old_records], 'Original repair spans changed')
        require(out['type'] == applied.get('output_type', 'paragraph'), 'Wrong repaired node type')
        require(out['retrieval_eligible'] == applied.get('retrieval_eligible', out['type'] == 'paragraph'), 'Repaired retrieval eligibility differs from plan')
        require(out['content_kind'] == applied.get('content_kind', old_records[0].get('content_kind', 'math')), 'Repaired content kind differs from plan')
        require(out['heading_id'] == applied.get('heading_id', old_records[0]['heading_id']), 'Wrong repaired chapter assignment')
        if applied.get('pdf_pages'):
            require(out['exact_pdf_pages'] == applied['pdf_pages'], 'Repair physical pages differ from plan')
        if applied.get('insert_after'):
            require(out['source_span_role'] == 'insertion_anchor_not_source_text', 'Inserted code claims source-text coordinates')
            require(out['upstream_insertion_source'] == applied['upstream_insertion_source'], 'Insertion lost upstream evidence')
        else:
            require(all(rid in archived or rid in fragments for rid in applied['record_ids']), 'Superseded record still active')
        for left, right in zip(old_records, old_records[1:]):
            require(left['source_origin'] == right['source_origin'], 'Cross-field merge')
            raw = normalized(pointer(tree, left['source_origin']['json_pointer']))
            require(left['field_end'] <= right['field_start'] and not raw[left['field_end']:right['field_start']].strip(), 'Merge skipped source content')
        if pid not in expected_plan:
            require(not applied.get('evidence') and applied.get('rule_evidence_heading_ids'), 'Unplanned PDF repair')
            require(out['type'] == 'metadata' and out['content_kind'] == 'heading', 'Unplanned non-heading rule')
            def title_key(text):
                return re.sub(r'\s+', '', re.sub(r'^#{1,6}\s*', '', text).strip())
            key = title_key(value)
            require(len(value.strip().splitlines()) == 1 and re.match(r'^(?:\d+(?:\.\d+)+|第.+章)', key), 'Unsafe generic heading match')
            labels = []
            for hid in applied['rule_evidence_heading_ids']:
                require(hid in old_nodes or hid in new_heading_map, 'Rule cites an unplanned heading')
                h = old_nodes.get(hid, nodes[hid])
                labels.extend([str(h.get('label') or ''), str(h.get('marker') or '') + ' ' + str(h.get('title') or '')])
            require(any(title_key(label) == key for label in labels), 'Generic heading differs from original structure')
        entries = sorted([n for n in nodes.values() if n.get('parent_block_id') == out['id']], key=lambda n: n['index']) if applied.get('split_offsets') else []
        if applied.get('split_offsets'):
            bounds = applied['split_offsets']
            require(len(entries) == len(bounds) - 1 and bounds[0] == 0 and bounds[-1] == len(value), 'Bibliography coverage incomplete')
            for index, (entry, left, right) in enumerate(zip(entries, bounds, bounds[1:])):
                require(entry['index'] == index and entry['parent_text_span'] == {'start': left, 'end': right}, 'Bibliography boundaries differ')
                require(record_text(entry, texts) == value[left:right], 'Bibliography split changed text')
                if applied.get('split_entry_metadata'):
                    meta = applied['split_entry_metadata'][index]
                    require(entry['reference_label'] == meta['label'] and entry['exact_pdf_pages'] == meta['global_pdf_pages_1_based'] and entry['original_part_spans'] == meta['original_spans'], 'Bibliography entry provenance changed')

    expected_archive = {rid for p in repairs.values() if not p.get('insert_after') for rid in p['record_ids'] if rid in old_nodes}
    require(set(archived) == expected_archive, 'Unplanned archival or missing replaced original record')
    expected_new = {p['output_record_id'] for p in repairs.values()} | set(new_heading_map)
    expected_new.update(n['id'] for n in nodes.values() if n['type'] == 'bibliography_entry' and n.get('parent_block_id') in expected_new)
    require(set(nodes) == (set(old_nodes) - expected_archive) | expected_new, 'Unplanned active node insertion/removal')

    # Reading order is independently derived from original spans and insertion
    # anchors, never from the exporter's reading_order_offset or node index.
    groups = defaultdict(list)
    for node in nodes.values():
        if node['type'] == 'paragraph':
            origin = node['source_origin']
            groups[(node['heading_id'], origin['input_path'], origin['json_pointer'])].append(node)
    expected_next = set()
    for group in groups.values():
        def order(node):
            repair = repairs.get(node.get('repair_id'))
            if repair and repair.get('insert_after'):
                anchor = originals[repair['record_ids'][0]]
                return (anchor['field_end'], -1, node['id'])
            return (node['field_start'], 0, node['id'])
        ordered = sorted(group, key=order)
        for i, node in enumerate(ordered):
            require(node['index'] == i, 'Active reading order/index inconsistent')
            if i:
                expected_next.add((ordered[i - 1]['id'], node['id'], 'next', 'corrected_reading_order_within_source_field'))
    require({e for e in edges if e[3] == 'corrected_reading_order_within_source_field'} == expected_next, 'Active next edges differ from original source order/insertion anchors')
    expected_contains = {(n['heading_id'], n['id'], 'contains', 'corrected_reading_graph') for n in nodes.values() if n['type'] in ('paragraph', 'metadata', 'bibliography_block')}
    require({e for e in edges if e[3] == 'corrected_reading_graph'} == expected_contains, 'Active contains edges incomplete')
    expected_bibliography = {(n['parent_block_id'], n['id'], 'contains', 'bibliography_entries') for n in nodes.values() if n['type'] == 'bibliography_entry'}
    require({e for e in edges if e[3] == 'bibliography_entries'} == expected_bibliography, 'Bibliography entry edges incomplete')
    expected_added = {(h['parent_id'], hid, 'contains', 'evidence_added_heading') for hid, h in new_heading_map.items()}
    require({e for e in edges if e[3] == 'evidence_added_heading'} == expected_added, 'Added heading hierarchy differs from plan')
    require(all(e[3] in source_scopes | {'corrected_reading_graph', 'corrected_reading_order_within_source_field', 'bibliography_entries', 'evidence_added_heading'} for e in edges), 'Unknown active edge scope')

    expected_index = defaultdict(set)
    for node in nodes.values():
        if node['type'] == 'paragraph' and node['retrieval_eligible']:
            expected_index[node['canonical_text_id']].add(node['id'])
    actual_index, seen_occurrences = {}, set()
    for row in index_rows:
        tid, occurrences = row['canonical_text_id'], row['occurrence_ids']
        require(tid not in actual_index, 'Duplicate canonical retrieval record')
        require(len(occurrences) == len(set(occurrences)) and not seen_occurrences.intersection(occurrences), 'Duplicate indexed occurrence')
        require(row['book_id'] == graph['book_id'] and row['text'] == texts[tid]['text'], 'Indexed book/text mismatch')
        require(row.get('human_verified') is False and row.get('same_text_is_not_independent_evidence') is True, 'Index verification/occurrence semantics incorrect')
        require(set(row['AI_evidence_repaired_occurrence_ids']) == {i for i in occurrences if nodes[i].get('evidence')}, 'Index evidence occurrence list mismatch')
        actual_index[tid] = set(occurrences)
        seen_occurrences.update(occurrences)
    require(dict(expected_index) == actual_index, 'Retrieval omissions, extras, or archived/non-body leakage')
    old_assets = by_id(old.get('assets', []), 'v2 assets')
    asset_archive = by_id(graph.get('source_asset_archive', []), 'asset archive')
    changed_asset_ids = {c['asset_id'] for c in asset_changes}
    require(set(assets) == set(old_assets), 'Asset inventory changed')
    require(set(asset_archive) == changed_asset_ids, 'Original reclassified asset archive incomplete')
    for aid in set(assets) - changed_asset_ids:
        require(assets[aid] == old_assets[aid], 'Unplanned asset mutation')
    for change in asset_changes:
        aid = change['asset_id']
        a = assets[aid]
        require(a['original_target'] == change['original_target'], 'Wrong placeholder target')
        require(a['asset_kind'] == 'code_example_literal' and a['resolution_status'] == 'not_applicable_example_literal' and a['media_asset_expected'] is False, 'Placeholder still classified as missing media')
        require(asset_archive[aid] == old_assets[aid], 'Original asset record not retained')
        require(not any(aid in n.get('asset_ids', []) for n in nodes.values()), 'Placeholder remains active media reference')
    return {'source_fields_compared_to_original_JSON': field_count, 'active_nodes_checked': len(nodes), 'archived_nodes_checked': len(archived), 'edges_checked': len(edges), 'indexed_texts_checked': len(actual_index), 'indexed_occurrences_checked': len(seen_occurrences), 'plan_patches_checked': len(patches), 'rule_patches_checked': len(repairs) - len(patches), 'placeholder_assets_checked': len(asset_changes), 'added_headings_checked': len(new_heading_map), 'heading_page_overrides_checked': len(override_map)}


def run(output):
    require(output.is_dir(), 'Output must already exist')
    report = {'schema_version': 'independent-v3-audit-v1', 'verifier': 'verify_evidence_graphs.py; no exporter imports', 'verifier_sha256': file_sha(Path(__file__)), 'created_at_UTC': datetime.now(timezone.utc).isoformat(), 'output': str(output), 'status': 'failed', 'errors': [], 'books': [], 'limitations': ['Structural and provenance audit only; no new PDF visual or full OCR/semantic verification.', 'AI evidence labels are not human verification.']}
    try:
        manifest_path = output / 'manifest.json'
        manifest = read_json(manifest_path)
        report['manifest_sha256'] = file_sha(manifest_path)
        require(manifest['schema_version'] == 'paragraph-text-graph-manifest-v3', 'Wrong manifest schema')
        require(len(manifest['books']) == 24 and len({b['book_id'] for b in manifest['books']}) == 24, 'Expected exactly 24 distinct books')
        for key in ('pdf_ocr_verified', 'semantic_verified', 'human_verified', 'physical_occurrence_verified'):
            require(manifest[key] is False, 'Manifest overstates verification: ' + key)
        old_manifest_path = within(ROOT, manifest['source_manifest']['path'])
        require(file_sha(old_manifest_path) == manifest['source_manifest']['sha256'], 'Frozen manifest hash mismatch')
        old_manifest = read_json(old_manifest_path)
        plan_path = within(ROOT, manifest['repair_plan']['path'])
        require(file_sha(plan_path) == manifest['repair_plan']['sha256'], 'Repair plan hash mismatch')
        plan = read_json(plan_path)
        for dependency in plan.get('source_dependencies', []):
            require(file_sha(within(ROOT, dependency['path'])) == dependency['sha256'], 'Plan source dependency hash mismatch')
        require(plan['source_manifest_sha256'] == manifest['source_manifest']['sha256'], 'Plan source mismatch')
        require(manifest['sample_outcomes'] == plan['sample_outcomes'], 'Sample outcome claims changed')
        outcomes = plan['sample_outcomes']
        outcome_ids = set(outcomes) if isinstance(outcomes, dict) else set().union(*(sample_ids(x) for x in outcomes))
        require(len(outcome_ids) == 21, 'Expected 21 sample outcome IDs')
        plan_actions = plan['patches'] + plan.get('asset_reclassifications', []) + plan.get('new_headings', []) + plan.get('heading_page_overrides', [])
        covered = set().union(*(sample_ids(p) for p in plan_actions))
        require(outcome_ids <= covered, 'Sample outcome has no plan patch/asset action: ' + str(sorted(outcome_ids - covered)))
        require(len(plan.get('asset_reclassifications', [])) == 3, 'Expected three placeholder asset reclassifications')
        for name, expected in manifest['implementation_sha256'].items():
            require(file_sha(within(Path(__file__).parent, name)) == expected, 'Implementation hash mismatch: ' + name)
        cache = {}
        for value, expected in manifest['evidence_metadata_sha256'].items():
            ep = within(ROOT, value)
            require(file_sha(ep) == expected, 'Evidence metadata hash mismatch')
            meta = read_json(ep)
            require(meta['source_manifest_sha256'] == plan['source_manifest_sha256'], 'Evidence references different frozen graph')
            for key in ('source_pdf', 'render_path'):
                path = within(ROOT, meta[key])
                if path not in cache:
                    cache[path] = file_sha(path)
                hash_key = 'source_pdf_sha256' if key == 'source_pdf' else 'render_sha256'
                require(cache[path] == meta[hash_key], 'Evidence PDF/image hash mismatch')
        plan_evidence = {p for item in plan_actions for p in item.get('evidence', [])}
        require(plan_evidence <= set(manifest['evidence_metadata_sha256']), 'Plan evidence not frozen in manifest')
        old_books = {x['book_id']: x for x in old_manifest['books']}
        require(set(old_books) == {x['book_id'] for x in manifest['books']}, 'Book roster changed')
        for row in manifest['books']:
            bid = row['book_id']
            result = {'book_id': bid, 'book_title': row['book_title'], 'status': 'failed'}
            try:
                old_row = old_books[bid]
                require(row['input_path'] == old_row['input_path'] and row['input_sha256'] == old_row['input_sha256'], 'Original input identity changed')
                inp = within(ROOT, row['input_path'])
                require(file_sha(inp) == row['input_sha256'], 'Original tree hash changed')
                paths = []
                for artifact in row['artifacts']:
                    path = within(output, artifact['path'])
                    require(path.parent == output / bid, 'Artifact outside its book folder')
                    require(file_sha(path) == artifact['sha256'] and path.stat().st_size == artifact['bytes'], 'Artifact hash/length mismatch: ' + str(path))
                    paths.append(artifact['path'])
                require(len(paths) == len(set(paths)), 'Repeated manifest artifact')
                for name in ('text_graph.json', 'retrieval_index.jsonl', '修复记录.json', 'quality_issues.jsonl'):
                    require((bid + '/' + name) in paths, 'Required artifact missing: ' + name)
                graph = read_json(output / bid / 'text_graph.json')
                require(graph['book_id'] == bid and row['summary'] == graph['summary'], 'Manifest book ID/summary differs from graph')
                old_path = within(ROOT, graph['source_graph']['path'])
                expected_old = next(x for x in old_row['artifacts'] if x['path'].endswith('/text_graph.json'))
                require(old_path == (old_manifest_path.parent / expected_old['path']).resolve(), 'Wrong frozen source graph')
                require(file_sha(old_path) == graph['source_graph']['sha256'] == expected_old['sha256'], 'Frozen source graph hash changed')
                require(graph['repair_plan'] == manifest['repair_plan'], 'Graph uses different repair plan')
                require(read_json(output / bid / '修复记录.json') == graph['repairs'], 'Repair log differs from graph')
                with (output / bid / 'retrieval_index.jsonl').open(encoding='utf-8') as stream:
                    index_rows = [json.loads(line) for line in stream if line.strip()]
                result.update(audit_graph(graph, read_json(old_path), read_json(inp), [p for p in plan['patches'] if p['book_id'] == bid], [p for p in plan.get('asset_reclassifications', []) if p['book_id'] == bid], index_rows, row['input_path'], [p for p in plan.get('new_headings', []) if p['book_id'] == bid], [p for p in plan.get('heading_page_overrides', []) if p['book_id'] == bid]))
                result['status'] = 'passed'
            except Exception as exc:
                result['error'] = type(exc).__name__ + ': ' + str(exc)
                report['errors'].append({'book_id': bid, 'error': result['error']})
            report['books'].append(result)
            print(bid + ': ' + result['status'], flush=True)
        report['sample_outcome_count'] = len(outcome_ids)
        report['plan_patch_count'] = len(plan['patches'])
        report['placeholder_asset_count'] = len(plan.get('asset_reclassifications', []))
    except Exception as exc:
        report['errors'].append({'scope': 'manifest/preflight', 'error': type(exc).__name__ + ': ' + str(exc)})
    report['passed_books'] = sum(b['status'] == 'passed' for b in report['books'])
    report['failed_books'] = sum(b['status'] != 'passed' for b in report['books'])
    report['error_count'] = len(report['errors'])
    report['status'] = 'passed' if report['passed_books'] == 24 and not report['errors'] else 'failed'
    target = output / '独立验收.json'
    target.write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
    print(json.dumps({k: report[k] for k in ('status', 'passed_books', 'failed_books', 'error_count')}, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.exit(run(args.output.resolve()))
