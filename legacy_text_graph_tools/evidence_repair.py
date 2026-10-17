"""Apply explicit page-backed corrections while retaining immutable source records."""
import copy
import hashlib
import json
import re
from collections import Counter, defaultdict


def identifier(prefix, value):
    return prefix + ':' + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def text_id(value):
    return 'text:' + hashlib.sha256(value.encode('utf-8')).hexdigest()


def repair_graph(graph, patches, new_headings=(), heading_page_overrides=()):
    if graph['schema_version'] != 'paragraph-text-graph-v2':
        raise ValueError('Repairs require the frozen v2 source graph, not an already repaired graph')
    out = copy.deepcopy(graph)
    nodes = {n['id']: n for n in out['nodes']}
    texts = {t['id']: t for t in out['paragraph_texts']}
    fragments = {f['id']: f for f in out['source_fragments']}
    source_records = dict(nodes, **fragments)
    archive, repairs, replacements, bibliography_edges = {}, [], {}, []
    patches = copy.deepcopy(patches)
    explicit_ids = {i for p in patches for i in p['record_ids']}
    added_heading_ids = set()
    for h in new_headings:
        if h['id'] in nodes or h['parent_id'] not in nodes or not h.get('evidence'):
            raise ValueError('Invalid evidence-backed new heading')
        parent = nodes[h['parent_id']]
        nodes[h['id']] = dict(h, type='section', title=h['label'], tree_depth=parent.get('tree_depth', 0) + 1,
                             title_path=parent.get('title_path', [out['book_title']]) + [h['label']],
                             provenance='AI_PDF_evidence_added_heading', full_section_end_verified=False)
        added_heading_ids.add(h['id'])
    for change in heading_page_overrides:
        heading = nodes[change['heading_id']]
        original = {k: heading.get(k) for k in ('pdf_page_start', 'pdf_page_end')}
        assert original == {k: change['original_' + k] for k in original}
        heading['source_pdf_page_range'] = original
        heading.update(pdf_page_start=change['corrected_pdf_page_start'], pdf_page_end=change['corrected_pdf_page_end'],
                       page_range_repair_evidence=change['evidence'], page_range_status='extended_to_cover_observed_page_not_full_section_audit')

    def record_text(record):
        return record['text'] if 'text' in record else texts[record['canonical_text_id']]['text']

    def heading_key(value):
        return re.sub(r'\s+', '', re.sub(r'^#{1,6}\s*', '', value).strip())

    heading_labels = defaultdict(list)
    for n in nodes.values():
        if n['type'] in ('chapter', 'section'):
            for label in (n.get('label'), str(n.get('marker') or '') + ' ' + str(n.get('title') or '')):
                if label:
                    heading_labels[heading_key(label)].append(n['id'])
    # A whitespace-only variation of an existing numbered heading is structural.
    # Do not infer headings from shortness, a leading number, or OCR text_level alone.
    for n in list(nodes.values()):
        if n['type'] != 'paragraph' or n['id'] in explicit_ids or n['content_kind'] != 'text':
            continue
        value = record_text(n)
        key = heading_key(value)
        if len(value.strip().splitlines()) == 1 and re.match(r'^(?:\d+(?:\.\d+)+|第.+章)', key) and key in heading_labels:
            patches.append({'id': 'rule-numbered-heading-' + n['id'].split(':')[1],
                            'record_ids': [n['id']], 'expected_texts': [value],
                            'replacement_text': None, 'output_type': 'metadata',
                            'content_kind': 'heading', 'retrieval_eligible': False,
                            'evidence': [], 'rule_evidence_heading_ids': sorted(set(heading_labels[key])),
                            'reason': 'Exact existing numbered heading after whitespace/ATX normalization; not PDF validation'})

    consumed = set()
    # Preflight all targets before changing the graph.
    for p in patches:
        ids = p['record_ids']
        if not ids or len(ids) != len(set(ids)):
            raise ValueError('Empty/duplicate repair target: ' + p['id'])
        if not p.get('evidence') and not p.get('rule_evidence_heading_ids'):
            raise ValueError('Missing repair evidence: ' + p['id'])
        if any(i not in source_records for i in ids):
            raise ValueError('Unknown repair target: ' + p['id'])
        if [record_text(source_records[i]) for i in ids] != p['expected_texts']:
            raise ValueError('Stale text precondition: ' + p['id'])
        if not p.get('insert_after'):
            if consumed.intersection(ids):
                raise ValueError('Overlapping repair targets: ' + p['id'])
            consumed.update(ids)
        if len(ids) > 1:
            origins = {json.dumps(source_records[i]['source_origin'], sort_keys=True) for i in ids}
            if len(origins) != 1 or [source_records[i]['field_start'] for i in ids] != sorted(source_records[i]['field_start'] for i in ids):
                raise ValueError('Merge must preserve one original field and source order')
            for left_id, right_id in zip(ids, ids[1:]):
                left, right = source_records[left_id], source_records[right_id]
                if left['heading_id'] != right['heading_id'] or left['field_end'] > right['field_start']:
                    raise ValueError('Invalid merge source order')
                layout = nodes[left['heading_id']]['text_layout']
                gap = [e for e in layout if e['field_start'] >= left['field_end'] and e['field_end'] <= right['field_start']]
                if any('separator' not in e for e in gap) or sum(len(e['separator']) for e in gap) != right['field_start'] - left['field_end']:
                    raise ValueError('Merge would skip source content')
        if p.get('new_heading'):
            h = p['new_heading']
            if h['parent_id'] not in nodes:
                raise ValueError('Missing new heading parent')
            if h['id'] not in nodes:
                parent = nodes[h['parent_id']]
                nodes[h['id']] = dict(h, type='section', label=h['label'], title=h['label'],
                                     tree_depth=parent.get('tree_depth', 0) + 1,
                                     title_path=parent.get('title_path', [out['book_title']]) + [h['label']],
                                     evidence=p['evidence'], provenance='AI_PDF_evidence_added_heading')
        heading = p.get('heading_id', source_records[ids[0]]['heading_id'])
        if heading not in nodes or nodes[heading]['type'] not in ('chapter', 'section'):
            raise ValueError('Invalid repaired heading')

    for p in patches:
        old = [source_records[i] for i in p['record_ids']]
        first = old[0]
        value = p.get('replacement_text')
        if value is None:
            if len(old) != 1:
                raise ValueError('Merge requires explicit corrected text')
            value = record_text(first)
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Empty corrected text')
        parent_id = p.get('heading_id', first['heading_id'])
        parent = nodes[parent_id]
        output_type = p.get('output_type', 'paragraph')
        new_id = identifier('repaired-' + output_type, [out['book_id'], p['id'], p['record_ids'], value, parent_id])
        canonical = text_id(value)
        texts.setdefault(canonical, {'id': canonical, 'text': value, 'chars': len(value), 'storage_semantics': 'exact_text_deduplication_only'})
        repaired = copy.deepcopy(first)
        for key in ('text', 'fragment_kind', 'matched_heading_ids', 'recovery', 'recovery_original_span', 'recovery_asset_ids'):
            repaired.pop(key, None)
        repaired.update(id=new_id, type=output_type, heading_id=parent_id, title_path=parent['title_path'],
                        canonical_text_id=canonical, text_ref={'canonical_text_id': canonical, 'start': 0, 'end': len(value)},
                        content_kind=p.get('content_kind', first.get('content_kind', 'math')),
                        retrieval_eligible=p.get('retrieval_eligible', output_type == 'paragraph'),
                        source_record_ids=p['record_ids'], repair_id=p['id'],
                        provenance='AI_page_evidence_repair' if p.get('evidence') else 'deterministic_heading_role_repair',
                        evidence=p.get('evidence', []), human_verified=False,
                        source_spans=[{k: n[k] for k in ('id', 'source_origin', 'field_start', 'field_end')} for n in old],
                        quality_flags=list(p.get('quality_flags', [])),
                        boundary_source='PDF_visual_verified_merge' if len(old) > 1 else first.get('boundary_source', 'source_fragment'),
                        original_heading_id=first['heading_id'],
                        original_heading_page_range={k: first.get(k) for k in ('pdf_page_start', 'pdf_page_end')},
                        offset_scope='original_source_anchor; corrected text uses text_ref',
                        reading_order_offset=first['field_start'] + (0.5 if p.get('insert_after') else 0))
        if p.get('insert_after'):
            repaired['reading_order_offset'] = first['field_end'] - 0.5
            repaired['upstream_insertion_source'] = p['upstream_insertion_source']
            repaired['boundary_source'] = 'PDF_visual_code_block'
        if p.get('pdf_pages'):
            repaired.update(pdf_page_start=min(p['pdf_pages']), pdf_page_end=max(p['pdf_pages']),
                            exact_pdf_pages=p['pdf_pages'], page_scope='evidence_backed_global_PDF_pages_for_repair')
        if p.get('content_kind') in ('heading', 'table_continuation', 'bibliography'):
            repaired['boundary_source'] = 'not_a_natural_paragraph'
        if p.get('asset_ids') is not None:
            repaired['asset_ids'] = p['asset_ids']
        else:
            repaired['asset_ids'] = list(dict.fromkeys(a for n in old for a in n.get('asset_ids', []) + n.get('recovery_asset_ids', [])))
        if p.get('insert_after'):
            repaired['source_span_role'] = 'insertion_anchor_not_source_text'
        else:
            for n in old:
                if n['id'] in nodes:
                    archive[n['id']] = nodes.pop(n['id'])
                replacements[n['id']] = new_id
        nodes[new_id] = repaired
        if p.get('split_offsets'):
            bounds = p['split_offsets']
            if bounds != sorted(set(bounds)) or bounds[0] != 0 or bounds[-1] != len(value):
                raise ValueError('Bibliography boundaries must exactly partition corrected text')
            for index, (left, right) in enumerate(zip(bounds, bounds[1:])):
                part = value[left:right]
                if not part.strip():
                    raise ValueError('Empty bibliography entry')
                tid = text_id(part)
                texts.setdefault(tid, {'id': tid, 'text': part, 'chars': len(part), 'storage_semantics': 'exact_text_deduplication_only'})
                entry_id = identifier('bibliography-entry', [new_id, left, right, tid])
                nodes[entry_id] = dict(id=entry_id, type='bibliography_entry', heading_id=parent_id,
                                       parent_block_id=new_id, index=index, canonical_text_id=tid,
                                       text_ref={'canonical_text_id': tid, 'start': 0, 'end': len(part)},
                                       parent_text_span={'start': left, 'end': right},
                                       retrieval_eligible=False, human_verified=False, content_kind='bibliography',
                                       boundary_source='PDF_visual_checked_citation_label', evidence=p['evidence'],
                                       source_origin=first['source_origin'], source_record_ids=p['record_ids'],
                                       quality_flags=['bibliography_full_OCR_unverified'])
                if p.get('split_entry_metadata'):
                    entry_metadata = p['split_entry_metadata'][index]
                    nodes[entry_id]['reference_label'] = entry_metadata['label']
                    nodes[entry_id]['exact_pdf_pages'] = entry_metadata['global_pdf_pages_1_based']
                    nodes[entry_id]['original_part_spans'] = entry_metadata['original_spans']
                bibliography_edges.append({'source': new_id, 'target': entry_id, 'type': 'contains', 'scope': 'bibliography_entries'})
        repairs.append(dict(p, output_record_id=new_id))

    # Distinguish source-field reconstruction from the corrected reading graph.
    for n in nodes.values():
        if 'text_layout' in n:
            n['source_text_layout'] = n.pop('text_layout')
            n['source_paragraph_count'] = n.pop('paragraph_count')
        if n['type'] == 'paragraph' and n.get('repair_id') is None:
            n['provenance'] = 'inherited_v2_unreviewed'
    edges = [e for e in out['edges'] if e['scope'] in ('original_heading_hierarchy', 'sibling_heading_order')] + bibliography_edges
    new_heading_ids = {p['new_heading']['id'] for p in patches if p.get('new_heading')} | added_heading_ids
    for hid in sorted(new_heading_ids):
        edges.append({'source': nodes[hid]['parent_id'], 'target': hid, 'type': 'contains', 'scope': 'evidence_added_heading'})
    groups = defaultdict(list)
    for n in nodes.values():
        if n['type'] in ('paragraph', 'metadata', 'bibliography_block'):
            edges.append({'source': n['heading_id'], 'target': n['id'], 'type': 'contains', 'scope': 'corrected_reading_graph'})
            if n['type'] == 'paragraph':
                groups[(n['heading_id'], n['source_origin']['json_pointer'])].append(n)
    for group in groups.values():
        group.sort(key=lambda n: (n.get('reading_order_offset', n['field_start']), n['id']))
        for i, n in enumerate(group):
            n['index'] = i
            if i:
                edges.append({'source': group[i - 1]['id'], 'target': n['id'], 'type': 'next', 'scope': 'corrected_reading_order_within_source_field'})
    out.update(schema_version='paragraph-text-graph-v3', status='evidence_repaired_not_corpus_verified',
               paragraph_rule='v2 source layout preserved; explicit PDF-backed repairs; conservative exact heading role rules',
               nodes=list(nodes.values()), edges=edges, paragraph_texts=list(texts.values()),
               source_record_archive=list(archive.values()), repairs=repairs,
               source_summary=out.pop('summary'))
    out['summary'] = {
        'active_paragraph_count': sum(n['type'] == 'paragraph' for n in nodes.values()),
        'retrieval_eligible_paragraph_count': sum(n['type'] == 'paragraph' and n['retrieval_eligible'] for n in nodes.values()),
        'indexed_text_count': len({n['canonical_text_id'] for n in nodes.values() if n['type'] == 'paragraph' and n['retrieval_eligible']}),
        'archived_source_record_count': len(archive), 'repair_count': len(repairs),
        'pdf_evidence_repair_count': sum(bool(p.get('evidence')) for p in patches),
        'rule_role_repair_count': sum(not p.get('evidence') for p in patches),
        'bibliography_block_count': sum(n['type'] == 'bibliography_block' for n in nodes.values()),
        'bibliography_entry_count': sum(n['type'] == 'bibliography_entry' for n in nodes.values()),
        'metadata_node_count': sum(n['type'] == 'metadata' for n in nodes.values()),
        'source_field_reconstruction_verified': False,
    }
    return out


def reconstruct_source(graph, heading_id):
    texts = {t['id']: t['text'] for t in graph['paragraph_texts']}
    records = {n['id']: n for n in graph['nodes']}
    records.update({n['id']: n for n in graph.get('source_record_archive', [])})
    fragments = {f['id']: f for f in graph['source_fragments']}
    heading = records[heading_id]
    result = []
    for entry in heading.get('source_text_layout', heading.get('text_layout', [])):
        if 'separator' in entry:
            result.append(entry['separator'])
        elif 'paragraph_id' in entry:
            result.append(texts[records[entry['paragraph_id']]['canonical_text_id']])
        else:
            result.append(fragments[entry['source_fragment_id']]['text'])
    return ''.join(result)
