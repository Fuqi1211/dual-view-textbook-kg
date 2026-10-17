# -*- coding: utf-8 -*-
"""Independent acceptance of v6, including unchanged sources and original-page regressions."""
import collections,json,pathlib
from reconcile_pdf_sources import ROOT,V5,OUT,AUDIT
from build_physical_graphs import read,lines,write,sha,norm
from verify_pdf_paragraph_graphs import coverage

def main():
    manifest=read(OUT/'manifest.json');errors=[];books=[];expected=set();pdfs=0;cache={}
    def check(ok,msg):
        if not ok:errors.append(msg)
    check(len(manifest['books'])==24,'24_books_required');check(sha(V5/'manifest.json')==manifest['previous_manifest_sha256'],'v5_manifest_unchanged')
    for b in manifest['books']:
        bid=b['book_id'];old=read(V5/bid/'text_graph.json');g=read(OUT/bid/'text_graph.json');hg=read(OUT/bid/'hierarchy_graph.json');lookup={n['id']:n for n in hg['nodes']};allnodes={n['id']:n for n in g['nodes']};hs={h['id']:h for h in g['headings']};pars={n['id']:n for n in g['nodes'] if n['type']=='paragraph'};cache[bid]=g
        check(coverage(old)==coverage(g),bid+':source_ranges_lossless');check(g['assets']==old['assets'],bid+':assets_unchanged')
        records=lines(AUDIT/'boundary_audit'/bid/'source_blocks.jsonl')
        for n in g['nodes']:
            if n['type']!='paragraph' and n.get('previous_type')!='paragraph':continue
            if n.get('evidence_patch_ids') or n.get('pdf_visual_text_additions') or n['boundary_status'].startswith('pdf_visual'):continue
            reconstructed=''.join(records[s['record_index']]['text'][s['start']:s['end']] for s in n['source_spans'])
            check(norm(n['text'])==norm(reconstructed),bid+':source_text_reconstruction:'+n['id'])
        for n in old['nodes']:
            if n['type']!='paragraph':check(n['id'] in allnodes and allnodes[n['id']]['text']==n['text'] and allnodes[n['id']]['source_spans']==n['source_spans'],bid+':nonprose_preserved:'+n['id'])
            if n.get('evidence_patch_ids') or n.get('pdf_visual_text_additions') or n['boundary_status'].startswith('pdf_visual'):
                check(n['id'] in allnodes and allnodes[n['id']]['text']==n['text'] and allnodes[n['id']]['source_spans']==n['source_spans'],bid+':prior_evidence_correction_preserved:'+n['id'])
        check(len(allnodes)==len(g['nodes']) and len(hs)==len(g['headings']),bid+':unique_ids')
        for h in hs.values():
            check(bool(h.get('physical_anchor')),bid+':heading_missing_physical_anchor:'+h['id'])
            check(h['parent_id']==bid or h['parent_id'] in hs,bid+':heading_parent_exists')
            p=hs.get(h['parent_id'])
            if p and p.get('physical_anchor') and h.get('physical_anchor'):check(p['physical_anchor']['position']<=h['physical_anchor']['position'],bid+':heading_before_parent:'+h['label'])
            if p:
                later=[x['physical_anchor']['position'] for x in hs.values() if x['parent_id']==p['parent_id'] and x['physical_anchor']['position']>p['physical_anchor']['position']]
                if later:check(h['physical_anchor']['position']<min(later),bid+':heading_outside_parent_scope:'+h['label'])
            a=h.get('physical_anchor',{})
            if a.get('image_sha256'):check(sha(OUT/a['image'])==a['image_sha256'],bid+':visual_evidence_unchanged')
        for n in g['nodes']:check(n['heading_id']==bid or n['heading_id'] in hs,bid+':node_parent_exists:'+n['id'])
        check(set(pars)=={n['id'] for n in hg['nodes'] if n['type']=='paragraph'},bid+':all_paragraphs_in_hierarchy')
        seen=set();stack=[bid];parents=collections.Counter()
        while stack:
            nid=stack.pop();check(nid not in seen,bid+':cycle_or_duplicate_edge')
            if nid in seen:continue
            seen.add(nid);n=lookup[nid]
            if n['type']=='paragraph':check(not n['children'],bid+':paragraph_leaf')
            for cid in n['children']:parents[cid]+=1;check(lookup[cid]['parent_id']==nid,bid+':parent_child_consistent');stack.append(cid)
        check(seen==set(lookup) and all(parents[nid]==1 for nid in lookup if nid!=bid),bid+':single_parent_connected_graph')
        for p in pars.values():
            check(not p['quality_flags'] and p['line_alignment_complete'],bid+':unresolved_current_body_evidence:'+p['id'])
            check(p['included_in_full_corpus_index'] and not p['human_verified'] and not p['natural_paragraph_boundary_verified'],bid+':honest_inclusion_status')
            check(p['text']==lookup[p['id']]['text'],bid+':hierarchy_text_consistent')
            check(p['assignment_basis']==lookup[p['id']]['assignment_basis'],bid+':hierarchy_assignment_provenance')
            if p['line_alignment_complete']:
                for s in p['source_spans']:
                    ee=[e for e in p['pdf_evidence'] if e['record_index']==s['record_index'] and max(e['raw_start'],s['start'])<min(e['raw_end'],s['end'])]
                    check(sum(min(e['raw_end'],s['end'])-max(e['raw_start'],s['start']) for e in ee)==s['end']-s['start'],bid+':line_evidence_full_source_coverage')
            for a in p.get('pdf_visual_text_additions',[]):check(sha(OUT/a['evidence_image'])==a['evidence_image_sha256'],bid+':prior_transcription_evidence')
        mapping=lines(OUT/bid/'v5_to_v6.jsonl');check({r['v5_node_id'] for r in mapping}=={n['id'] for n in old['nodes'] if n['type']=='paragraph'},bid+':old_node_mapping_complete')
        check({nid for r in mapping for nid in r['v6_node_ids']}=={n['id'] for n in g['nodes'] if n['type']=='paragraph' or n.get('previous_type')=='paragraph'},bid+':new_node_mapping_complete')
        for source in read(V5/bid/'pdf_inputs.json'):check(sha(ROOT/source['path'])==source['sha256'],bid+':original_pdf_unchanged');pdfs+=1
        check(sha(OUT/bid/'text_graph.json')==b['graph_sha256'],bid+':graph_hash');expected.update(pars)
        books.append({'book_id':bid,'paragraphs':len(pars),'headings':len(hs),'complete_line_evidence':sum(p['line_alignment_complete'] for p in pars.values()),'specific_review_remaining':sum(bool(p['quality_flags']) for p in pars.values()),'source_nodes_retyped':sum(n.get('previous_type')=='paragraph' for n in g['nodes'])})
    def rec(bid,ri):return [n for n in cache[bid]['nodes'] if n['type']=='paragraph' and any(s['record_index']==ri for s in n['source_spans'])]
    cases={c['case_id']:c for c in lines(OUT/'boundary_visual_review/cases.jsonl')}
    reviews=read(OUT/'boundary_visual_review/visual_decisions.json')
    check(len(reviews)==len(cases)==133 and {c['case_id'] for c in reviews}==set(cases),'all_visual_cases_reviewed')
    for r in reviews:
        c=cases[r['case_id']];bid=c['book_id'];ri=c['record_index'];k=c['raw_offset']
        check(sha(OUT/'boundary_visual_review'/r['evidence_sheet'])==r['image_sha256'],'visual_case_image_hash:'+str(c['case_id']))
        ns=[n for n in cache[bid]['nodes'] if any(s['record_index']==ri for s in n['source_spans'])]
        if r['decision']=='split':check(not any(s['record_index']==ri and s['start']<k<s['end'] for n in ns for s in n['source_spans']),'visual_split_applied:'+str(c['case_id']))
        if r['decision']=='continue':check(any(s['record_index']==ri and s['start']<k<s['end'] for n in ns for s in n['source_spans']),'visual_continuation_retained:'+str(c['case_id']))
        if r['decision'] not in ('split','continue'):check(all(n['type']==r['decision'] for n in ns),'visual_content_type:'+str(c['case_id']))
    for bid,a,b in [('book_20c6be06f4a3',8265,8268),('book_efaf8522b95d',4650,4654)]:check(any({a,b}<={s['record_index'] for s in n['source_spans']} for n in rec(bid,a)),'visual_figure_wrap_repaired:'+str(a))
    for bid,ri,count in [('book_ab313ee3c3dd',600,2),('book_00849acee112',217,3),('book_20c6be06f4a3',3971,2),('book_dd8183b4cb60',4921,1),('book_68ef7cdaa275',6813,1),('book_714c23228015',2801,2),('book_20c6be06f4a3',3260,4),('book_dd8183b4cb60',2178,6),('book_dd8183b4cb60',2245,5)]:check(len(rec(bid,ri))==count,'visual_boundary_regression:'+bid+':'+str(ri))
    for bid,a,b in [('book_ab313ee3c3dd',188,189),('book_00849acee112',233,234),('book_bef9d155e6d8',324,327),('book_00849acee112',199,201)]:check(any({a,b}<={s['record_index'] for s in n['source_spans']} for n in rec(bid,a)),'previous_visual_join_preserved:'+str(a))
    for bid,a,b in [('book_00849acee112',519,520),('book_714c23228015',522,526)]:check(not any({a,b}<={s['record_index'] for s in n['source_spans']} for n in rec(bid,a)),'visual_barrier_preserved:'+str(a))
    for bid,ri,pages in [('book_00849acee112',222,[28,29]),('book_00849acee112',258,[32,33]),('book_bef9d155e6d8',367,[45,46])]:check(len(rec(bid,ri))==1 and set(pages)<=set(rec(bid,ri)[0]['pdf_pages']),'crosspage_record_attribution:'+str(ri))
    g=cache['book_bcdf0a6ec957'];heads={h['id']:h for h in g['headings']}
    for number,page in [('6',215),('8',339)]:
        h=next(h for h in heads.values() if h['type']=='chapter' and h['_number']==number);check(h['physical_anchor']['position'][0]==page,'micro_chapter_banner_'+number);check(any(n['type']=='paragraph' and n['heading_id']==h['id'] and page in n['pdf_pages'] for n in g['nodes']),'micro_chapter_intro_'+number)
    check(not any(h['label'].startswith('3.64位') for h in heads.values()),'local_numbered_item_not_subsection')
    sec=next(h for h in cache['book_79b01559b0f3']['headings'] if h.get('_number')=='6.12');check(sec['physical_anchor']['position'][0]==139,'body_heading_not_repeated_page_header')
    ids=[json.loads(line)['id'] for line in (OUT/'全量正文段落.jsonl').open(encoding='utf-8')];check(set(ids)==expected and len(ids)==len(expected),'full_paragraph_export')
    report={'status':'passed' if not errors else 'failed','books':books,'paragraphs':len(expected),'original_pdf_hashes_checked':pdfs,'visual_cases_checked':len(reviews),'known_review_queue_remaining':sum(b['specific_review_remaining'] for b in books),'errors':errors,'manifest_sha256':sha(OUT/'manifest.json'),'verifier_sha256':sha(pathlib.Path(__file__)),'scope':'Lossless source coverage, original PDF hashes, line evidence coverage, single-parent hierarchy, all structural anchors and parent intervals, all 133 final visual case dispositions, and previous original-page regressions. Automatic checks and assistant visual review; no corpus-wide human-gold claim.','human_verified':False}
    write(OUT/'验收结果.json',report);print(json.dumps({k:v for k,v in report.items() if k!='books'},ensure_ascii=False),flush=True)
    if errors:raise SystemExit(1)
if __name__=='__main__':main()
