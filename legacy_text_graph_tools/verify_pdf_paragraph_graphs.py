# -*- coding: utf-8 -*-
"""Verify source conservation, retained corrections, page evidence and hierarchy independently."""
import collections,json,pathlib
from extract_pdf_line_geometry import ROOT,V4,AUDIT,OUT,GEOMETRY_VERSION
from build_physical_graphs import read,lines,write,sha,norm

def coverage(graph):
    spans=collections.defaultdict(list)
    for n in graph['nodes']:
        for s in n['source_spans']:spans[s['record_index']].append((s['start'],s['end']))
    result={}
    for ri,ss in spans.items():
        ss.sort();assert all(a[1]==b[0] for a,b in zip(ss,ss[1:])),('gap/overlap',ri)
        result[ri]=(ss[0][0],ss[-1][1])
    return result

def main():
    m=read(OUT/'manifest.json');errors=[];results=[];expected_ids=set();pdf_files=0
    def check(ok,msg):
        if not ok:errors.append(msg)
    check(sha(V4/'manifest.json')==m['previous_manifest_sha256'],'previous_version_manifest_changed')
    for b in m['books']:
        bid=b['book_id'];g=read(OUT/bid/'text_graph.json');old=read(V4/bid/'text_graph.json');h=read(OUT/bid/'hierarchy_graph.json');lookup={n['id']:n for n in h['nodes']};pars={n['id']:n for n in g['nodes'] if n['type']=='paragraph'}
        check(coverage(g)==coverage(old),bid+':source_character_coverage')
        check(g['headings']==old['headings'] and g['assets']==old['assets'],bid+':headings_assets_changed')
        check([n for n in g['nodes'] if n['type']!='paragraph']==[n for n in old['nodes'] if n['type']!='paragraph'],bid+':nonprose_changed')
        check(len({n['id'] for n in g['nodes']})==len(g['nodes']),bid+':duplicate_ids')
        oldpars={n['id']:n for n in old['nodes'] if n['type']=='paragraph'}
        mapping=lines(OUT/bid/'v4_to_v5.jsonl');check({r['v4_node_id'] for r in mapping}==set(oldpars),bid+':old_mapping_incomplete')
        check({nid for r in mapping for nid in r['v5_node_ids']}==set(pars),bid+':new_mapping_incomplete')
        for n in oldpars.values():
            if n.get('evidence_patch_ids') or n['boundary_status'].startswith('pdf_visual'):
                check(n['id'] in pars and all(pars[n['id']][k]==n[k] for k in ('text','source_spans','heading_id')),bid+':prior_visual_correction_lost:'+n['id'])
        # Re-segmentation may change only whitespace at joins, never source wording.
        reconstructed=[]
        for n in g['nodes']:
            if n['type']!='paragraph':continue
            text=n['text']
            for addition in n.get('pdf_visual_text_additions',[]):
                check(bid=='book_ab313ee3c3dd' and addition['record_index']==600 and addition['pdf_page']==46 and text.startswith(addition['text']),bid+':unexpected_visual_addition')
                check(sha(OUT/addition['evidence_image'])==addition['evidence_image_sha256'],bid+':visual_addition_evidence_changed')
                text=text[len(addition['text']):]
            reconstructed.append(text)
        check(norm(''.join(n['text'] for n in old['nodes'] if n['type']=='paragraph'))==norm(''.join(reconstructed)),bid+':text_changed')
        check(set(pars)=={n['id'] for n in h['nodes'] if n['type']=='paragraph'},bid+':hierarchy_missing_paragraphs')
        seen=set();stack=[bid];parents=collections.Counter()
        while stack:
            nid=stack.pop();check(nid not in seen,bid+':cycle_or_duplicate_child')
            if nid in seen:continue
            seen.add(nid);n=lookup[nid]
            if n['type']=='paragraph':check(not n['children'],bid+':paragraph_not_leaf')
            for cid in n['children']:
                parents[cid]+=1;check(lookup[cid]['parent_id']==nid,bid+':parent_mismatch');stack.append(cid)
        check(seen==set(lookup) and all(parents[x]==1 for x in lookup if x!=bid),bid+':hierarchy_connectivity')
        for nid,p in pars.items():
            check(p['included_in_full_corpus_index'] and not p['human_verified'] and not p['natural_paragraph_boundary_verified'],bid+':inclusion_or_verification_claim')
            check(p['text']==lookup[nid]['text'] and p['source_spans']==lookup[nid]['source_spans'],bid+':hierarchy_content_mismatch')
        geom=read(OUT/bid/'geometry_summary.json');check(geom['geometry_version']==GEOMETRY_VERSION and not geom['unresolved_line_pages'],bid+':page_attribution')
        for source in read(OUT/bid/'pdf_inputs.json'):
            check(sha(ROOT/source['path'])==source['sha256'],bid+':original_pdf_hash:'+source['path']);pdf_files+=1
        check(geom['records_with_line_evidence']==geom['paragraph_source_records'],bid+':geometry_coverage')
        check(sum(p['pages'] for p in read(OUT/bid/'pdf_inputs.json'))==geom['rendered_original_pdf_pages'],bid+':page_count')
        check(sha(OUT/bid/'text_graph.json')==b['graph_sha256'],bid+':graph_hash')
        expected_ids.update(pars);results.append({'book_id':bid,'paragraphs':len(pars),'source_records_preserved':len(coverage(g)),'status':'checked'})
    ids=[]
    with (OUT/'全量正文段落.jsonl').open(encoding='utf8') as f:
        for row in f:ids.append(json.loads(row)['id'])
    check(set(ids)==expected_ids and len(ids)==len(expected_ids),'full_corpus_export')
    # Independent source-coordinate regressions established by original-page viewing.
    def book(bid):return [n for n in read(OUT/bid/'text_graph.json')['nodes'] if n['type']=='paragraph']
    def from_record(bid,ri):return [n for n in book(bid) if any(s['record_index']==ri for s in n['source_spans'])]
    c=from_record('book_ab313ee3c3dd',600)
    check(sorted((s['start'],s['end']) for n in c for s in n['source_spans'] if s['record_index']==600)==[(0,54),(54,184)],'PDF_indent_split_Cpp600')
    check(any(n['text'].startswith('第二种方法更好，因为头文件更有可能使原型正确。') and 'sqrt()函数的说明将指出' in n['text'] for n in c),'PDF_omitted_two_line_prefix_restored')
    for bid,ri,pages in [('book_00849acee112',222,[28,29]),('book_00849acee112',258,[32,33]),('book_bef9d155e6d8',367,[45,46])]:
        ns=from_record(bid,ri);check(len(ns)==1 and set(pages)<=set(ns[0]['pdf_pages']),'upstream_crosspage_continuation:'+str(ri))
    check(len(from_record('book_00849acee112',217))==3,'three_list_paragraphs_preserved')
    for bid,a,b in [('book_ab313ee3c3dd',188,189),('book_00849acee112',233,234),('book_bef9d155e6d8',324,327),('book_00849acee112',199,201)]:
        check(any({a,b}<={s['record_index'] for s in n['source_spans']} for n in from_record(bid,a)),'verified_continuation:'+str((a,b)))
    check(not any({519,520}<={s['record_index'] for s in n['source_spans']} for n in from_record('book_00849acee112',519)),'R1_R2_not_merged')
    check(not any({522,526}<={s['record_index'] for s in n['source_spans']} for n in from_record('book_714c23228015',522)),'ML_equation_barrier')
    for bid,ri,count in [('book_20c6be06f4a3',3971,2),('book_dd8183b4cb60',4921,1),('book_68ef7cdaa275',6813,1),('book_714c23228015',2801,2)]:
        check(len(from_record(bid,ri))==count,'formula_IP_hanging_indent_or_true_indent:'+bid+':'+str(ri))
    report={'status':'passed' if not errors else 'failed','books':results,'paragraphs':len(expected_ids),'original_pdf_files_hash_verified':pdf_files,'errors':errors,'scope':'All-book source preservation, hierarchy, original-PDF line-page attribution and selected visual regressions. This does not certify every semantic paragraph boundary or heading assignment.','full_corpus_human_verified':False,'manifest_sha256':sha(OUT/'manifest.json'),'verifier_sha256':sha(pathlib.Path(__file__))}
    write(OUT/'验收结果.json',report);print(json.dumps({k:v for k,v in report.items() if k!='books'},ensure_ascii=False))
    if errors:raise SystemExit(1)

if __name__=='__main__':main()
