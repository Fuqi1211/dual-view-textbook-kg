"""Compile reviewed correction proposals to explicit, identity-guarded operations."""
import hashlib
import json
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / '研究方案_20261002'
WORK = BASE / '文本图修复_v3工作区'
V2 = BASE / '24本文本图_修复版_20261002'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    texts = {x['sample_id']: x for x in read(WORK / 'patch_text.json')['items']}
    formula_source = read(WORK / 'patch_formula_bibliography.json')
    formulas = {x['sample_id']: x for x in formula_source['formula_patches']}
    biblios = {x['sample_id']: x for x in formula_source['bibliography_patches']}
    structures = read(WORK / 'patch_structure.json')
    structural = {x['sample_id']: x for x in structures['items']}
    package = read(BASE / '文本图人工核查包_20261002/核查样本.json')
    samples = {x['sample_id']: x for x in package['items']}
    audit = read(BASE / '文本图AI辅助核验_20261002/AI辅助核验结果.json')
    issues = {x['sample_id'] for x in audit['items'] if x['visual_status'] == 'issue'}
    patches, added_headings, page_overrides, assets = [], [], [], []
    dependencies = [{'path': p.relative_to(ROOT).as_posix(), 'sha256': sha(p)}
                    for p in (WORK / 'patch_text.json', WORK / 'patch_structure.json', WORK / 'patch_formula_bibliography.json')]
    for row in read(V2 / 'manifest.json')['books']:
        book_ids = [sid for sid in sorted(issues) if samples[sid]['book_id'] == row['book_id']]
        if not book_ids:
            continue
        graph = read(V2 / row['book_id'] / 'text_graph.json')
        records = {n['id']: n for n in graph['nodes'] + graph['source_fragments']}
        text_map = {t['id']: t['text'] for t in graph['paragraph_texts']}
        tree = read(ROOT / row['input_path'])

        def value(record):
            return record['text'] if 'text' in record else text_map[record['canonical_text_id']]

        def field(record):
            raw = tree
            for key in record['source_origin']['json_pointer'].strip('/').split('/'):
                raw = raw[int(key)] if isinstance(raw, list) else raw[key]
            return raw.replace('\r\n', '\n').replace('\r', '\n')

        def aliases(sample):
            # Same words alone do not establish physical identity (e.g. int main or formula tags).
            selected = []
            for n in graph['nodes']:
                if n.get('canonical_text_id') != sample['canonical_text_id']:
                    continue
                raw = field(n)
                before = raw[max(0, n['field_start'] - 80):n['field_start']]
                after = raw[n['field_end']:n['field_end'] + 80]
                if n['id'] == sample['record_id'] or (before == sample['context_before'][-80:] and after == sample['context_after'][:80]):
                    selected.append(n['id'])
            assert sample['record_id'] in selected
            return selected

        for heading in structures.get('new_evidence_backed_headings', []):
            if heading.get('book_id') != row['book_id']:
                continue
            evidence = heading.get('evidence_metadata_paths', [])
            if not evidence:
                evidence = sorted({e for x in structural.values() if x['book_id'] == row['book_id']
                                   and x.get('desired_parent', {}).get('heading_id') == heading['proposed_heading_id']
                                   for e in x['evidence_metadata_paths']})
            added_headings.append({'id': heading['proposed_heading_id'], 'book_id': row['book_id'],
                                   'parent_id': heading['parent_heading_id'], 'label': heading['title'],
                                   'pdf_page_start': heading['observed_global_pdf_page_start'],
                                   'pdf_page_end': heading['observed_global_pdf_page_end'],
                                   'evidence': evidence})
        for change in structures.get('heading_page_overrides', []):
            if change.get('book_id') == row['book_id']:
                normalized = dict(change)
                normalized['evidence'] = change.get('evidence', change.get('evidence_metadata_paths', []))
                page_overrides.append(normalized)

        for sid in book_ids:
            sample = samples[sid]
            s, t, f, b = structural.get(sid), texts.get(sid), formulas.get(sid), biblios.get(sid)
            evidence = list(dict.fromkeys((s or {}).get('evidence_metadata_paths', []) + (t or {}).get('evidence', [])
                                         + (f or b or {}).get('evidence_metadata_paths', [])))
            pages = (s or {}).get('exact_global_pdf_pages_1_based') or sorted({read(ROOT / e)['global_pdf_page_1_based'] for e in evidence})

            def append(ids, replacement=None, **options):
                patch = dict(id=sid + '-' + ids[0].split(':')[-1][:16], sample_id=sid, book_id=row['book_id'],
                             record_ids=ids, expected_texts=[value(records[i]) for i in ids],
                             replacement_text=replacement, evidence=evidence, pdf_pages=pages)
                if s and s.get('desired_parent'):
                    patch['heading_id'] = s['desired_parent']['heading_id']
                patch.update(options)
                patches.append(patch)
                return patch

            if s and s.get('merge_groups'):
                for group in s['merge_groups']:
                    merged = group['merged_text_before_independent_ocr_corrections']
                    if sid == 'R061':
                        assert merged.count('ΙO') == 1
                        merged = merged.replace('ΙO', 'I/O')
                    append(group['merge_record_ids_in_order'], merged,
                           heading_id=group['desired_parent']['heading_id'],
                           pdf_pages=group['exact_global_pdf_pages_1_based'])
                continue
            if b:
                hid = 'evidence-heading:' + row['book_id'] + ':bibliography'
                if not any(h['id'] == hid for h in added_headings):
                    added_headings.append({'id': hid, 'book_id': row['book_id'], 'parent_id': row['book_id'],
                                           'label': '参考文献', 'pdf_page_start': min(pages), 'pdf_page_end': max(pages), 'evidence': evidence})
                continuation = {x['source_origin']['json_pointer']: x for x in b['continuation_occurrences']}
                for occurrence in b['apply_to_occurrences']:
                    tail = continuation[occurrence['source_origin']['json_pointer']]
                    original, ending = records[occurrence['record_id']], records[tail['record_id']]
                    gap = field(original)[original['field_end']:ending['field_start']]
                    assert not gap.strip()
                    joined = value(original) + gap + value(ending)
                    patch = append([original['id'], ending['id']], joined, heading_id=hid,
                                   output_type='bibliography_block', content_kind='bibliography', retrieval_eligible=False,
                                   quality_flags=['bibliography_full_OCR_unverified'] + (['bibliography_boundaries_unresolved'] if sid == 'R097' else []))
                    if sid == 'R098':
                        entries = b['entry_splits']
                        patch['split_offsets'] = [entry['block_relative_start'] for entry in entries] + [len(joined)]
                        patch['split_entry_metadata'] = entries
                continue
            if f:
                ids = [x['record_id'] for x in f['apply_to_occurrences']]
            elif s:
                ids = s['record_ids']
            else:
                ids = aliases(sample)
            if sid == 'R007':
                insertion = t['insertions'][0]
                dependency = insertion['upstream_merged_block']
                dependencies.append({'path': dependency['input_path'], 'sha256': dependency['source_file_sha256']})
                for rid in ids:
                    append([rid], insertion['insert_text'].lstrip('\n'), insert_after=True,
                           content_kind='fenced_code', asset_ids=[], upstream_insertion_source=dependency)
                continue
            for rid in ids:
                replacement = (f or t or {}).get('replacement_text')
                options = {}
                if sid in ('R001', 'R022', 'R081', 'R082'):
                    options.update(output_type='metadata', content_kind='table_continuation' if sid == 'R001' else 'heading', retrieval_eligible=False)
                elif sid in ('R089', 'R105', 'R106'):
                    options['content_kind'] = 'code'
                elif f:
                    options['content_kind'] = f['replacement_content_kind']
                    for asset in f['source_assets']:
                        dependencies.append({'path': asset['path'], 'sha256': asset['sha256']})
                if replacement is not None and value(records[rid]) != sample['text']:
                    # Structural companion records (exercise stem etc.) retain their own text.
                    replacement = None
                append([rid], replacement, **options)
            for associated in (s or {}).get('associated_question_records', []):
                append([associated['question_record']['record_id']], None, content_kind='text')
            if sid in ('R105', 'R106'):
                literal_targets = ('/bin/images/image1.gif',) if sid == 'R105' else ('Pictures/Funny1.gif', 'Pictures/Funny2.gif')
                for asset in graph['assets']:
                    if asset['original_target'] in literal_targets:
                        if not any(x['book_id'] == row['book_id'] and x['asset_id'] == asset['id'] for x in assets):
                            assets.append({'sample_id': 'R105' if 'image1.gif' in asset['original_target'] else 'R106',
                                           'book_id': row['book_id'], 'asset_id': asset['id'],
                                           'original_target': asset['original_target'], 'evidence': evidence})
    assert {p['sample_id'] for p in patches} == issues
    # Dependencies and new headings are ordered once, before dependent additions.
    dependencies = list({d['path']: d for d in dependencies}.values())
    for d in dependencies:
        assert sha(ROOT / d['path']) == d['sha256'], d['path']
    outcomes = []
    for sid in sorted(issues):
        limitations = (texts.get(sid) or {}).get('unresolved', []) + (structural.get(sid) or {}).get('unresolved', [])
        if sid in biblios:
            limitations += biblios[sid].get('limitations', [])
        if sid == 'R117':
            limitations += ['公式正文已恢复；原审计未独立确认8.4小节归属。']
        outcomes.append({'sample_id': sid, 'status': 'partially_repaired_quarantined' if sid == 'R097' else 'audited_defect_repaired',
                         'patch_count': sum(p['sample_id'] == sid for p in patches),
                         'limitations': limitations, 'human_verified': False})
    plan = {'schema_version': 'evidence_repair_plan_v3', 'source_manifest_sha256': package['source_manifest_sha256'],
            'source_dependencies': dependencies, 'patches': patches, 'new_headings': added_headings,
            'heading_page_overrides': page_overrides, 'asset_reclassifications': assets, 'sample_outcomes': outcomes,
            'scope_note': '21 audited windows and explicitly matched field copies; no corpus-wide PDF verification.'}
    target = WORK / 'v3_repair_plan.json'
    target.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'patches': len(patches), 'by_sample': dict(Counter(p['sample_id'] for p in patches)),
                      'new_headings': len(added_headings), 'page_overrides': len(page_overrides), 'example_assets': len(assets)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
