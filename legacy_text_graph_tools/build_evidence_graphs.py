"""Export all 24 books as v3 after evidence/identity preflight. Never overwrite v2."""
import argparse
import hashlib
import json
from pathlib import Path
from evidence_repair import repair_graph

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '研究方案_20261002'
SOURCE = BASE / '24本文本图_修复版_20261002'


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def local(value):
    p = (ROOT / value).resolve()
    p.relative_to(ROOT)
    if not p.is_file():
        raise ValueError('Missing local evidence: ' + value)
    return p


def verify_graph(graph):
    nodes = {n['id']: n for n in graph['nodes']}
    assert len(nodes) == len(graph['nodes'])
    archived = {n['id']: n for n in graph['source_record_archive']}
    assert not (nodes.keys() & archived.keys())
    sources = dict(nodes, **archived)
    texts = {t['id']: t['text'] for t in graph['paragraph_texts']}
    fragments = {f['id']: f for f in graph['source_fragments']}
    for tid, value in texts.items():
        assert tid == 'text:' + hashlib.sha256(value.encode('utf-8')).hexdigest()
    for edge in graph['edges']:
        assert edge['source'] in nodes and edge['target'] in nodes, edge
    fields = 0
    for node in nodes.values():
        if 'canonical_text_id' in node:
            assert node['canonical_text_id'] in texts
            assert node['text_ref']['end'] == len(texts[node['canonical_text_id']])
            assert node['heading_id'] in nodes
        if 'source_text_layout' not in node:
            continue
        parts = []
        cursor = 0
        for entry in node['source_text_layout']:
            assert entry['field_start'] == cursor
            if 'separator' in entry:
                value = entry['separator']
            elif 'paragraph_id' in entry:
                source = sources[entry['paragraph_id']]
                value = texts[source['canonical_text_id']]
            else:
                value = fragments[entry['source_fragment_id']]['text']
            assert len(value) == entry['field_end'] - entry['field_start']
            parts.append(value)
            cursor = entry['field_end']
        whole = ''.join(parts)
        assert len(whole) == node['normalized_chars']
        assert hashlib.sha256(whole.encode('utf-8')).hexdigest() == node['normalized_text_sha256']
        fields += 1
    for patch in graph['repairs']:
        target = nodes[patch['output_record_id']]
        assert target['repair_id'] == patch['id']
        if patch.get('replacement_text') is not None:
            assert texts[target['canonical_text_id']] == patch['replacement_text']
        if not patch.get('insert_after'):
            assert all(i in archived or i in fragments for i in patch['record_ids'])
    return {'source_fields_reconstructed': fields, 'edges_checked': len(graph['edges']),
            'nodes_checked': len(nodes), 'repairs_checked': len(graph['repairs'])}


def export(plan_path, output):
    if output.exists():
        raise FileExistsError('New output directory required: ' + str(output))
    plan = read(plan_path)
    source_manifest = read(SOURCE / 'manifest.json')
    assert sha(SOURCE / 'manifest.json') == plan['source_manifest_sha256']
    assert len(source_manifest['books']) == 24
    for dependency in plan.get('source_dependencies', []):
        assert sha(local(dependency['path'])) == dependency['sha256']
    evidence_hashes = {}
    for patch in plan['patches'] + plan.get('new_headings', []) + plan.get('heading_page_overrides', []) + plan.get('asset_reclassifications', []):
        for value in patch['evidence']:
            metadata_path = local(value)
            if value in evidence_hashes:
                continue
            meta = read(metadata_path)
            assert meta['source_manifest_sha256'] == plan['source_manifest_sha256']
            assert sha(local(meta['source_pdf'])) == meta['source_pdf_sha256']
            assert sha(local(meta['render_path'])) == meta['render_sha256']
            evidence_hashes[value] = sha(metadata_path)
    for row in source_manifest['books']:
        assert sha(local(row['input_path'])) == row['input_sha256']
        for artifact in row['artifacts']:
            if artifact['path'].endswith('/text_graph.json'):
                assert sha(SOURCE / artifact['path']) == artifact['sha256']
    output.mkdir(parents=True)
    results, validations = [], []
    for index, row in enumerate(source_manifest['books'], 1):
        path = SOURCE / row['book_id'] / 'text_graph.json'
        graph = repair_graph(read(path), [p for p in plan['patches'] if p['book_id'] == row['book_id']],
                             [h for h in plan.get('new_headings', []) if h['book_id'] == row['book_id']],
                             [h for h in plan.get('heading_page_overrides', []) if h['book_id'] == row['book_id']])
        graph['source_graph'] = {'path': path.relative_to(ROOT).as_posix(), 'sha256': sha(path)}
        graph['repair_plan'] = {'path': plan_path.relative_to(ROOT).as_posix(), 'sha256': sha(plan_path)}
        graph['source_asset_archive'] = []
        targets = {a['asset_id']: a for a in plan.get('asset_reclassifications', []) if a['book_id'] == row['book_id']}
        for asset in graph['assets']:
            if asset['id'] not in targets:
                continue
            assert asset['original_target'] == targets[asset['id']]['original_target']
            graph['source_asset_archive'].append(dict(asset))
            asset.update(asset_kind='code_example_literal', resolution_status='not_applicable_example_literal',
                         original_resolution_status=asset['resolution_status'],
                         evidence=targets[asset['id']]['evidence'], media_asset_expected=False)
        if targets:
            for record in graph['nodes']:
                if set(record.get('asset_ids', [])) & set(targets):
                    record['example_literal_asset_ids'] = [a for a in record['asset_ids'] if a in targets]
                    record['asset_ids'] = [a for a in record['asset_ids'] if a not in targets]
                    record['quality_flags'] = [f for f in record.get('quality_flags', []) if f not in ('image_missing', 'image_unsafe_path')]
        validation = verify_graph(graph)
        graph['summary']['source_field_reconstruction_verified'] = True
        folder = output / row['book_id']
        folder.mkdir()
        write(folder / 'text_graph.json', graph)
        texts = {t['id']: t for t in graph['paragraph_texts']}
        occurrences, evidence_occurrences = {}, {}
        for node in graph['nodes']:
            if node['type'] == 'paragraph' and node['retrieval_eligible']:
                occurrences.setdefault(node['canonical_text_id'], []).append(node['id'])
                if node.get('evidence'):
                    evidence_occurrences.setdefault(node['canonical_text_id'], []).append(node['id'])
        with (folder / 'retrieval_index.jsonl').open('x', encoding='utf-8', newline='\n') as stream:
            for tid, ids in occurrences.items():
                record = {'book_id': row['book_id'], 'canonical_text_id': tid, 'text': texts[tid]['text'],
                          'occurrence_ids': ids, 'dedup_scope': 'within_each_book',
                          'same_text_is_not_independent_evidence': True,
                          'AI_evidence_repaired_occurrence_ids': evidence_occurrences.get(tid, []), 'human_verified': False}
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        with (folder / 'quality_issues.jsonl').open('x', encoding='utf-8', newline='\n') as stream:
            repaired_sources = {rid for p in graph['repairs'] if not p.get('insert_after') for rid in p['record_ids']}
            for node in graph['nodes'] + graph['source_fragments']:
                if node.get('quality_flags') and node['id'] not in repaired_sources:
                    stream.write(json.dumps({k: node.get(k) for k in ('id', 'type', 'source_origin', 'quality_flags', 'retrieval_eligible')}, ensure_ascii=False) + '\n')
        write(folder / '修复记录.json', graph['repairs'])
        artifacts = [{'path': p.relative_to(output).as_posix(), 'sha256': sha(p), 'bytes': p.stat().st_size}
                     for p in sorted(folder.iterdir())]
        results.append({**{k: row[k] for k in ('book_id', 'book_title', 'input_path', 'input_sha256')},
                        'summary': graph['summary'], 'artifacts': artifacts})
        validations.append(dict(book_id=row['book_id'], **validation))
        print(f'[{index}/24] {row["book_title"]}: repairs={len(graph["repairs"])} source reconstruction OK', flush=True)
    aggregate = {'book_count': 24}
    for key in results[0]['summary']:
        aggregate[key] = all(r['summary'][key] for r in results) if isinstance(results[0]['summary'][key], bool) else sum(r['summary'][key] for r in results)
    manifest = {'schema_version': 'paragraph-text-graph-manifest-v3', 'status': 'evidence_repaired_not_corpus_verified',
                'source_manifest': {'path': (SOURCE / 'manifest.json').relative_to(ROOT).as_posix(), 'sha256': sha(SOURCE / 'manifest.json')},
                'repair_plan': {'path': plan_path.relative_to(ROOT).as_posix(), 'sha256': sha(plan_path)},
                'implementation_sha256': {name: sha(Path(__file__).with_name(name)) for name in ('evidence_repair.py', 'build_evidence_graphs.py')},
                'evidence_metadata_sha256': evidence_hashes, 'pdf_ocr_verified': False, 'semantic_verified': False,
                'human_verified': False, 'physical_occurrence_verified': False, 'api_calls': 0,
                'summary': aggregate, 'books': results, 'sample_outcomes': plan['sample_outcomes']}
    write(output / '构建核验.json', validations)
    write(output / 'manifest.json', manifest)
    print(json.dumps(aggregate, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    export(args.plan.resolve(), args.output.resolve())
