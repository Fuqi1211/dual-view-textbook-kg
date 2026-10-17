# -*- coding: utf-8 -*-
"""Apply PDF pixel-supported partitions without dropping source content or prior corrections."""
import collections,copy,hashlib,json,pathlib
from extract_pdf_line_geometry import ROOT,V4,AUDIT,OUT
from build_physical_graphs import read,lines,write,jsonl,sha,join_prose,rel
import build_hierarchy_view as hierarchy

def compact_write(path,obj):
    path.write_text(json.dumps(obj,ensure_ascii=False,separators=(',',':')),encoding='utf8')

def overlap(s,t):
    return s['record_index']==t['record_index'] and max(s['start'],t['start'])<min(s['end'],t['end'])

def source_evidence(node,evidence,records):
    result=[];unresolved=0
    for s in node['source_spans']:
        e=evidence.get(s['record_index'])
        if not e:unresolved+=1;continue
        ls=e['lines']
        for i,l in enumerate(ls):
            start=l.get('raw_start')
            if start is None:unresolved+=1;continue
            end=next((v['raw_start'] for v in ls[i+1:] if v.get('raw_start') is not None),len(records[s['record_index']]['text']))
            if max(start,s['start'])>=min(end,s['end']):continue
            result.append({'record_index':s['record_index'],'raw_start':max(start,s['start']),'raw_end':min(end,s['end']),'pdf_page':l.get('pdf_page'),'pdf_path':l['pdf_path'],'pdf_sha256':e['pdf_sha256'],'local_pdf_page':l.get('local_pdf_page'),'bbox':l.get('ink_bbox',l['ocr_bbox']),'page_size':l.get('page_size'),'pixel_supported':l['pixel_supported'],'page_attribution':l['page_attribution']})
    return result,unresolved

def build(book):
    bid=book['book_id'];folder=OUT/bid;old=read(V4/bid/'text_graph.json');g=copy.deepcopy(old)
    records=lines(AUDIT/'boundary_audit'/bid/'source_blocks.jsonl');proposal=read(folder/'boundary_proposals.json');ev={e['record_index']:e for e in lines(folder/'pdf_line_evidence.jsonl')}
    original_pars=[n for n in old['nodes'] if n['type']=='paragraph'];byrecord=collections.defaultdict(list)
    for n in original_pars:
        for s in n['source_spans']:byrecord[s['record_index']].append(n)
    replacements={};replaced_ids=set();changed_records=[]
    for p in proposal['partitions']:
        if p['old_cuts']==p['cuts']:continue
        ri=p['record_index'];ns=byrecord[ri]
        assert not p['protected_pdf_evidence']
        assert all(len(n['source_spans'])==1 and n['text']==records[ri]['text'][n['source_spans'][0]['start']:n['source_spans'][0]['end']] for n in ns),'Cannot overwrite a prior text correction'
        assert len({n['heading_id'] for n in ns})==1
        made=[]
        for a,b in zip(p['cuts'],p['cuts'][1:]):
            base=copy.deepcopy(next(n for n in ns if n['source_spans'][0]['start']<=a<n['source_spans'][0]['end']))
            base['id']=f'pdfpara:{bid}:{ri}:{a}:{b}';base['text']=records[ri]['text'][a:b]
            base['source_spans']=[dict(base['source_spans'][0],start=a,end=b)]
            base['boundary_status']='original_pdf_pixel_partition';base['layout_repair_applied']=True
            base['boundary_evidence_ref']=f'{bid}/boundary_proposals.json#record={ri}'
            made.append(base)
        replacements[ri]=made;replaced_ids.update(n['id'] for n in ns);changed_records.append(ri)
    nodes=[copy.deepcopy(n) for n in old['nodes'] if n['id'] not in replaced_ids]
    nodes.extend(n for ns in replacements.values() for n in ns)
    nodes.sort(key=lambda n:(n['source_spans'][0]['row_index'],n['source_spans'][0]['record_index'],n['source_spans'][0]['start']))
    owner={n['id']:n for n in nodes};current_record=collections.defaultdict(list)
    for n in nodes:
        if n['type']=='paragraph':
            for s in n['source_spans']:current_record[s['record_index']].append(n['id'])
    removed=set();redirect={};applied=[]
    def resolve(nid):
        while nid in redirect:nid=redirect[nid]
        return owner[nid]
    for j in proposal['joins']:
        a=resolve(current_record[j['left_record']][-1]);b=resolve(current_record[j['right_record']][0])
        if a['id']==b['id']:continue
        assert a['type']==b['type']=='paragraph'
        assert not (a.get('evidence_patch_ids') or b.get('evidence_patch_ids'))
        a['text']=join_prose(a['text'],b['text']);a['source_spans'].extend(b['source_spans'])
        a['quality_flags']=sorted(set(a['quality_flags']+b['quality_flags']))
        a['retrieval_eligible']=a['retrieval_eligible'] and b['retrieval_eligible']
        a['boundary_status']='original_pdf_pixel_continuation';a['layout_repair_applied']=True
        a.setdefault('joined_records',[]).append([j['left_record'],j['right_record']])
        a['boundary_evidence_ref']=f'{bid}/boundary_proposals.json'
        removed.add(b['id']);redirect[b['id']]=a['id'];applied.append(j)
    nodes=[n for n in nodes if n['id'] not in removed]
    for n in nodes:
        if n['type']!='paragraph':continue
        # New IDs for changed text avoid reusing an old content identity after a merge.
        if n.get('joined_records'):
            n['id']='pdfpara:'+bid+':merge:'+hashlib.sha256(json.dumps(n['source_spans'],sort_keys=True).encode()).hexdigest()[:16]
        n['pdf_evidence'],unresolved=source_evidence(n,ev,records)
        n['pdf_pages']=sorted({e['pdf_page'] for e in n['pdf_evidence'] if e['pdf_page'] is not None})
        n['pdf_layout_status']='repaired_from_pdf_pixels' if n.get('layout_repair_applied') else 'pdf_pixels_checked_existing_partition_retained'
        if not n['pdf_evidence']:n['pdf_layout_status']='line_alignment_pending'
        n['unaligned_ocr_lines']=unresolved;n['included_in_full_corpus_index']=True
        n['natural_paragraph_boundary_verified']=False;n['human_verified']=False
        # Original PDF page 46 visibly contains two lines omitted by the upstream OCR.
        # Keep this transcription as a separate, reversible evidence-backed addition.
        if bid=='book_ab313ee3c3dd' and len(n['source_spans'])==1 and n['source_spans'][0]['record_index']==600 and n['source_spans'][0]['start']==0:
            prefix='第二种方法更好，因为头文件更有可能使原型正确。对于C++库中的每个函数，都在一个或多个头文件中提供了其原型。请通过手册或在线帮助查看函数描述来确定应使用哪个头文件。例如，sqrt()函数的说'
            assert n['text'].startswith('明将指出')
            n['text']=prefix+n['text']
            n['pdf_visual_text_additions']=[{'insert_before_raw_offset':0,'record_index':600,'text':prefix,'pdf_page':46,'pdf_path':ev[600]['pdf_path'],'local_pdf_page':ev[600]['local_pdf_page'],'region_normalized_1000':[78,158,924,191],'basis':'assistant_read_original_pdf_two_omitted_lines','evidence_image':'visual_checks/book_ab313ee3c3dd_600.png','evidence_image_sha256':sha(OUT/'visual_checks/book_ab313ee3c3dd_600.png'),'human_verified':False}]
    # Preserve the complete lossless mapping from every old paragraph into the new graph.
    newbyrecord=collections.defaultdict(list)
    for n in nodes:
        if n['type']=='paragraph':
            for s in n['source_spans']:newbyrecord[s['record_index']].append((n,s))
    mapping=[]
    for n in original_pars:
        targets={new['id'] for s in n['source_spans'] for new,t in newbyrecord[s['record_index']] if overlap(s,t)}
        assert targets
        mapping.append({'v4_node_id':n['id'],'v5_node_ids':sorted(targets)})
    g['nodes']=nodes;g['schema_version']='pdf-natural-paragraph-graph-v5'
    g['minimum_prose_node']='original_pdf_natural_paragraph_reconstruction_with_line_provenance'
    g['previous_version']={'path':rel(V4/bid/'text_graph.json'),'sha256':sha(V4/bid/'text_graph.json')}
    g['full_corpus_natural_paragraph_verified']=False
    g['edges']=[e for e in old['edges'] if e['type']=='contains' and e['target'] in {h['id'] for h in old['headings']}]
    g['edges'].extend({'source':n['heading_id'],'target':n['id'],'type':'contains','status':n['ownership_status']} for n in nodes)
    g['edges'].extend({'source':a['id'],'target':b['id'],'type':'next_physical_text'} for a,b in zip(nodes,nodes[1:]))
    compact_write(folder/'text_graph.json',g);jsonl(folder/'v4_to_v5.jsonl',mapping)
    pars=[n for n in nodes if n['type']=='paragraph']
    summary={'book_id':bid,'book_title':book['book_title'],'old_paragraphs':len(original_pars),'paragraphs':len(pars),'changed_source_records':len(changed_records),'added_internal_boundaries':sum(len(set(p['cuts'])-set(p['old_cuts'])) for p in proposal['partitions']),'removed_internal_boundaries':sum(len(set(p['old_cuts'])-set(p['cuts'])) for p in proposal['partitions']),'merged_record_boundaries':len(applied),'cross_page_merges':sum(j['pages'][0]!=j['pages'][1] for j in applied),'paragraphs_with_page_evidence':sum(bool(n['pdf_evidence']) for n in pars),'paragraphs_spanning_pdf_pages':sum(len(n['pdf_pages'])>1 for n in pars),'all_source_content_preserved':True,'human_verified':False,'graph_sha256':sha(folder/'text_graph.json')}
    write(folder/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)
    return summary

def main():
    books=[build(b) for b in read(V4/'manifest.json')['books']]
    summary={k:sum(b[k] for b in books) for k in ('old_paragraphs','paragraphs','changed_source_records','added_internal_boundaries','removed_internal_boundaries','merged_record_boundaries','cross_page_merges','paragraphs_with_page_evidence','paragraphs_spanning_pdf_pages')}
    write(OUT/'manifest.json',{'version':'v5','books':books,'summary':summary,'previous_manifest_sha256':sha(V4/'manifest.json'),'human_verified':False,'full_corpus_natural_paragraph_verified':False})
    hierarchy.OUT=OUT;hb=[hierarchy.build(b) for b in books]
    write(OUT/'层级数据清单.json',{'books':hb,'paragraph_count':summary['paragraphs'],'human_verified':False,'full_corpus_semantic_verified':False})
    with (OUT/'全量正文段落.jsonl').open('w',encoding='utf8') as f:
        for b in books:
            g=read(OUT/b['book_id']/'hierarchy_graph.json')
            for n in g['nodes']:
                if n['type']=='paragraph':f.write(json.dumps(dict(n,book_id=b['book_id'],book_title=b['book_title']),ensure_ascii=False,separators=(',',':'))+'\n')
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
