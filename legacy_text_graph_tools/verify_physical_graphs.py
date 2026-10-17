# -*- coding: utf-8 -*-
"""Independent v4 structural/provenance regression check; does not import builder."""
import collections,hashlib,json,pathlib,re
ROOT=pathlib.Path(__file__).resolve().parents[2]
R=ROOT/'研究方案_20261002';OUT=R/'24本文本图_物理段落v4_20261003';AUDIT=R/'文本图全面核验_20261002'
def read(p):return json.loads(pathlib.Path(p).read_text(encoding='utf8'))
def jl(p):
    with pathlib.Path(p).open(encoding='utf8') as f:return [json.loads(s) for s in f if s.strip()]
def sha(p):
    h=hashlib.sha256()
    with pathlib.Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
def write(p,x):pathlib.Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf8')

def main():
    manifest=read(OUT/'manifest.json');errors=[];results=[];graphs={}
    def check(ok,msg):
        if not ok:errors.append(msg)
    check(len(manifest['books'])==24,'not_all_24_books')
    check(sha(R/'24本文本图_证据修复v3_20261002/manifest.json')==manifest['source_v3_manifest_sha256']=='70db88f2a9e9921806c424a0bd6746dea46e63eea328112458f57e4b366866d9','v3_manifest_changed')
    v3=read(R/'24本文本图_证据修复v3_20261002/manifest.json')
    for b in manifest['books']:
        bid=b['book_id'];folder=OUT/bid;g=read(folder/'text_graph.json')
        if bid in ('book_bef9d155e6d8','book_00849acee112','book_ab313ee3c3dd','book_20c6be06f4a3','book_714c23228015'):graphs[bid]=g
        for f in b['artifacts']:check(sha(ROOT/f['path'])==f['sha256'],bid+':artifact_hash:'+f['path'])
        check(sha(ROOT/g['source']['path'])==g['source']['sha256'],bid+':raw_source_changed')
        check(sha(ROOT/g['source']['text_records_path'])==g['source']['text_records_sha256'],bid+':audit_source_changed')
        rows=read(ROOT/g['source']['path']);records=jl(ROOT/g['source']['text_records_path']);coverage=collections.defaultdict(list)
        for dependency in read(AUDIT/'boundary_audit'/bid/'evidence_inputs.json'):
            check(sha(ROOT/dependency['path'])==dependency['sha256'],bid+':line_geometry_changed:'+dependency['path'])
        for r in records:
            value=rows[r['row_index']]
            for key in r['field'].split('/'):value=value[int(key)] if isinstance(value,list) else value[key]
            check(value==r['text'],bid+':source_record_text:'+str(r['record_index']))
        ids={n['id'] for n in g['nodes']};heads={h['id']:h for h in g['headings']};valid=ids|set(heads)|{bid}
        check(len(ids)==len(g['nodes']),bid+':duplicate_node_id')
        check(len(heads)==len(g['headings']),bid+':duplicate_heading_id')
        for h in heads.values():
            seen=set();cur=h['id']
            while cur in heads and cur not in seen:seen.add(cur);cur=heads[cur]['parent_id']
            check(cur==bid,bid+':heading_cycle_or_orphan:'+h['id'])
        for n in g['nodes']:
            check(n['heading_id'] in valid,bid+':orphan_node:'+n['id'])
            check(n['human_verified'] is False and n['pdf_ocr_verified'] is False,bid+':unjustified_verification_flag')
            for s in n['source_spans']:
                r=records[s['record_index']]
                check(s['row_index']==r['row_index'] and s['field']==r['field'] and s['page_pdf']==r['page_idx']+1,bid+':bad_coordinates:'+n['id'])
                check(0<=s['start']<s['end']<=len(r['text']),bid+':invalid_span:'+n['id'])
                coverage[s['record_index']].append((s['start'],s['end']))
            if n['retrieval_eligible']:check(n['type']=='paragraph' and not n['quality_flags'] and n['ownership_status'] in ('anchored','pdf_evidence'),bid+':index_leak:'+n['id'])
            spans=n['source_spans']
            if len(spans)>1:
                for a,c in zip(spans,spans[1:]):
                    intervening=rows[a['row_index']+1:c['row_index']]
                    check(not any(row.get('type') in ('equation','image','table','code') or row.get('text_level') for row in intervening),bid+':merge_skips_substantive_content:'+n['id'])
        for r in records:
            spans=sorted(coverage[r['record_index']]);end=0
            for a,c in spans:check(a==end,bid+':overlap_or_gap:'+str(r['record_index']));end=c
            check(end==len(r['text']),bid+':uncovered_record:'+str(r['record_index']))
        for e in g['edges']:check(e['source'] in valid and e['target'] in valid,bid+':broken_edge')
        migration=read(folder/'evidence_patch_migration.json');node_lookup={n['id']:n for n in g['nodes']};patches={p['id']:p for p in migration['source_patches']}
        def normalized(s):return re.sub(r'\s+','',re.sub(r'\\([\\`*_{}\[\]()#+.!|>\-])',r'\1',s)).translate(str.maketrans('，（）；：！？．',',();:!?.'))
        check(len(migration['patches'])==len(patches),bid+':legacy_patch_inventory')
        for result in migration['patches']:
            patch=patches[result['patch_id']];check(result['status']!='pending',bid+':unmigrated_legacy_patch:'+patch['id'])
            targets=result.get('node_ids',[]);check(set(targets)<=ids,bid+':legacy_patch_broken_reference')
            if patch.get('replacement_text') and result['status'] not in ('pending','quarantined_bibliography_pages'):
                combined=''.join(node_lookup[n]['text'] for n in targets if n in node_lookup)
                check(normalized(patch['replacement_text']) in normalized(combined),bid+':legacy_corrected_text_missing:'+patch['id'])
        index=jl(folder/'retrieval_candidates.jsonl');check({n['id'] for n in index}=={n['id'] for n in g['nodes'] if n['retrieval_eligible']},bid+':index_mismatch')
        bridge=jl(folder/'old_to_physical.jsonl');check(len(bridge)==b['old_paragraph_occurrences'],bid+':bridge_coverage')
        for x in bridge:
            for candidate in x['candidate_node_sets']:check(set(candidate)<=ids,bid+':broken_bridge')
        original=next(x for x in v3['books'] if x['book_id']==bid)
        check(sha(ROOT/original['input_path'])==original['input_sha256'],bid+':original_tree_changed')
        for f in original['artifacts']:check(sha(R/'24本文本图_证据修复v3_20261002'/f['path'])==f['sha256'],bid+':v3_artifact_changed')
        results.append({'book_id':bid,'source_records_covered_once':len(records),'nodes':len(ids),'legacy_occurrences_bridged_or_explicitly_unresolved':len(bridge)})
        print(bid,'checked',len(records),'raw text fields',flush=True)
    visual=read(AUDIT/'visual_review/已核实案例.json');comparisons=[]
    def at(bid,ri):return [n for n in graphs[bid]['nodes'] if any(s['row_index']==ri for s in n['source_spans'])]
    for case in visual:
        cid=case['case_id'];bid=case['book_id'];passed=False;nodes=[]
        for e in case['evidence']:
            check(sha(ROOT/e['pdf_path'])==e['pdf_sha256'],cid+':pdf_changed')
            check(sha(ROOT/e['image_path'])==e['image_sha256'],cid+':render_changed')
        if cid in ('V001','V002'):
            a,b=(344,347) if cid=='V001' else (218,220);nodes=at(bid,a)
            passed=len(nodes)==1 and len(at(bid,b))==1 and nodes[0]['id']==at(bid,b)[0]['id'] and ('之间的区别' in nodes[0]['text'] if cid=='V001' else '数据中心每个' in nodes[0]['text'])
        elif cid=='V003':
            nodes=at(bid,1666);passed=len(nodes)==3 and all(n['text'].startswith(p) for n,p in zip(nodes,['1.从上层','2.超时','3.收到ACK']))
        elif cid=='V004':
            nodes=at(bid,522)+at(bid,526);passed=len(nodes)==2 and nodes[0]['id']!=nodes[1]['id']
        elif cid in ('V005','V006','V008'):
            rowi,chapter_num=(374,2) if cid=='V005' else ((8216,22) if cid=='V006' else (6432,7))
            nodes=at(bid,rowi);heads={h['id']:h for h in graphs[bid]['headings']}
            passed=len(nodes)==1 and bool(re.search(r'第\s*'+str(chapter_num)+r'\s*章',heads.get(nodes[0]['chapter_id'],{}).get('label','')))
        elif cid=='V007':nodes=at(bid,1872);passed=len(nodes)==1
        check(passed,cid+':regression_failed')
        heads={h['id']:h for h in graphs[bid]['headings']}
        comparisons.append({'case_id':cid,'book_id':bid,'passed':bool(passed),'previous_finding':case['finding'],'old_occurrences':case.get('related_occurrences',[]),'new_nodes':[{'id':n['id'],'text':n['text'],'chapter':heads.get(n['chapter_id'],{}).get('label'),'heading':heads.get(n['heading_id'],{}).get('label'),'source_spans':n['source_spans'],'boundary_status':n['boundary_status']} for n in nodes],'evidence':case['evidence'],'human_verified':False})
    write(OUT/'前后对照.json',comparisons)
    result={'status':'passed' if not errors else 'failed','scope':'source-coverage, references, indexes, legacy integrity, eight visually established regression windows; NOT full-corpus semantic verification','full_corpus_natural_paragraph_verified':False,'full_corpus_chapter_ownership_verified':False,'human_verified':False,'manifest_sha256':sha(OUT/'manifest.json'),'verifier_sha256':sha(__file__),'books':results,'visual_cases_passed':sum(c['passed'] for c in comparisons),'errors':errors}
    write(OUT/'独立验收.json',result);print(json.dumps({'status':result['status'],'errors':errors[:30],'error_count':len(errors),'cases_passed':result['visual_cases_passed']},ensure_ascii=False))
    if errors:raise SystemExit(1)

if __name__=='__main__':main()
