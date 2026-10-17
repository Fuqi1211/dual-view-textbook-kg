# -*- coding: utf-8 -*-
"""Reconcile physical source locations, boundaries and structural ownership, losslessly."""
import argparse,bisect,collections,copy,hashlib,json,re,statistics
from reconcile_pdf_sources import ROOT,V5,OUT,AUDIT
from build_physical_graphs import read,lines,write,jsonl,sha,join_prose
from propose_pdf_boundaries import internal_decisions,complex_block,unobstructed
from pdf_paragraph_rules import decide_boundary,END,LIST
from build_pdf_paragraph_graphs import compact_write,overlap
import build_hierarchy_view as hierarchy

def position(e):return (e['pdf_page'],e['bbox'][1]/e['page_size'][1]*1000)

def resolve_cross_page(a,b,obstacles):
    """Check an already joined OCR paragraph at a physical page boundary."""
    if b.get('pdf_page')!=a.get('pdf_page',0)+1:return 'unknown','not_adjacent_pages'
    af=a.get('page_frame');bf=b.get('page_frame')
    if not af or not bf or not af['single_column'] or not bf['single_column']:return 'unknown','column_frame_missing'
    if not all(l.get('pixel_supported') and not l.get('special') for l in (a,b)):return 'unknown','line_pixels_missing'
    if END.search(a['text']) or b.get('list_start') or LIST.match(b['text']):return 'unknown','possible_paragraph_or_list_start'
    aa=a.get('ocr_bbox',a['ink_bbox']);bb=b.get('ocr_bbox',b['ink_bbox'])
    # Faint scans can leave a tiny dark-ink box inside a normal-size text line.
    # Use the OCR line rectangle only when original pixels support both rectangles.
    em=max(af['em'],.8*(aa[3]-aa[1]));bem=max(bf['em'],.8*(bb[3]-bb[1]));indent=(bb[0]-bf['left'])/bem
    if not -.8<=indent<=.85 or not .55<=(bb[3]-bb[1])/max(1,aa[3]-aa[1])<=1.8:return 'unknown','indent_or_font_changed'
    if aa[3]<af['bottom']-2*em or bb[1]>bf['top']+2*bf['em']:return 'unknown','not_page_edge'
    if abs((af['right']-af['left'])/(bf['right']-bf['left'])-1)>.15:return 'unknown','body_width_changed'
    if (af['right']-aa[2])/em<=1.65:return 'continue','full_line_nonterminal_and_flush_next_page'
    pw,ph=a['page_size'];cy=(aa[1]+aa[3])/2/ph*1000;end=aa[2]/pw*1000;margin=3*em/ph*1000
    for row in obstacles:
        box=row.get('bbox')
        if row.get('page_idx')==a['pdf_page']-1 and row.get('type') in ('image','table') and box and box[0]>=end-2*em/pw*1000 and box[1]-margin<=cy<=box[3]+margin:
            return 'continue','right_side_figure_or_table_wrap_and_flush_next_page'
    return 'unknown','short_line_without_obstruction'

def paragraph_column_frame(e,line):
    """Estimate a column only from several pixel-supported lines of this paragraph."""
    page=line['pdf_page'];target=line['ocr_bbox'];em=max(1,target[3]-target[1])
    same=[l for l in e['lines'] if l.get('pdf_page')==page and l.get('pixel_supported') and not l.get('special') and abs(l['ocr_bbox'][0]-target[0])<=3*em and l['ocr_bbox'][2]-l['ocr_bbox'][0]>=8*em]
    if len(same)<2:return None
    boxes=[l['ocr_bbox'] for l in same];heights=[b[3]-b[1] for b in boxes];em=statistics.median(heights)*.8
    left=min(b[0] for b in boxes);right=max(b[2] for b in boxes)
    if max(b[0] for b in boxes)-left>3*em:return None
    return {'left':left,'right':right,'em':em,'top':min(b[1] for b in boxes),'bottom':max(b[3] for b in boxes),'single_column':True,'basis':'multiple_pixel_supported_lines_of_same_paragraph_column'}

def evidence_for(n,ev,records):
    result=[];complete=True;rawlines=[]
    for s in n['source_spans']:
        e=ev.get(s['record_index']);covered=[]
        if not e:complete=False;continue
        if e.get('alignment_status')=='previous_partial_alignment_retained':complete=False
        for i,l in enumerate(e['lines']):
            start=l.get('raw_start')
            if start is None:continue
            end=l.get('raw_end',next((v['raw_start'] for v in e['lines'][i+1:] if v.get('raw_start') is not None),len(records[s['record_index']]['text'])))
            a=max(start,s['start']);b=min(end,s['end'])
            if a>=b:continue
            if l.get('pdf_page') is None or not l.get('page_size'):complete=False;continue
            result.append({'record_index':s['record_index'],'raw_start':a,'raw_end':b,'pdf_page':l['pdf_page'],'pdf_path':l['pdf_path'],'pdf_sha256':e['pdf_sha256'],'local_pdf_page':l['local_pdf_page'],'bbox':l.get('ink_bbox',l['ocr_bbox']),'page_size':l['page_size'],'pixel_supported':l['pixel_supported'],'page_attribution':l['page_attribution']})
            rawlines.append(dict(l,record_index=s['record_index'],clipped_start=a,clipped_end=b));covered.append((a,b))
        if sum(b-a for a,b in covered)!=s['end']-s['start']:complete=False
    return result,complete,rawlines

def build(book):
    bid=book['book_id'];folder=OUT/bid;old=read(V5/bid/'text_graph.json');g=copy.deepcopy(old);scope=read(folder/'heading_scopes.json');headings=copy.deepcopy(scope['headings']);records=lines(AUDIT/'boundary_audit'/bid/'source_blocks.jsonl');rows=read(ROOT/old['source']['path']);ev={x['record_index']:x for x in lines(folder/'pdf_line_evidence.jsonl')}
    cases={c['case_id']:c for c in lines(OUT/'boundary_visual_review/cases.jsonl')}
    visual=[]
    for review in read(OUT/'boundary_visual_review/visual_decisions.json'):
        c=cases[review['case_id']]
        if c['book_id']!=bid:continue
        assert sha(OUT/'boundary_visual_review'/review['evidence_sheet'])==review['image_sha256']
        visual.append(dict(c,**review))
    visual_by_key={(c['record_index'],c['raw_offset']):c for c in visual}
    frames={l['pdf_page']:l['page_frame'] for e in ev.values() for l in e['lines'] if l.get('page_frame') and l.get('pdf_page')}
    for e in ev.values():
        for l in e['lines']:
            if 'page_frame' not in l and l.get('pdf_page') in frames:l['page_frame']=frames[l['pdf_page']]
    hs={h['id']:h for h in headings};anchored=sorted([h for h in headings if h.get('physical_anchor')],key=lambda h:(h['physical_anchor']['position'],h['level']))
    points=[tuple(h['physical_anchor']['position']) for h in anchored]
    def owner(pos):
        i=bisect.bisect_right(points,tuple(pos))-1
        return anchored[i]['id'] if i>=0 else bid
    def chapter(hid):
        seen=set()
        while hid in hs and hid not in seen:
            seen.add(hid)
            if hs[hid]['type']=='chapter':return hid
            hid=hs[hid]['parent_id']
    ledger=[];nodes=[];split_count=0;decisions={ri:internal_decisions(e) for ri,e in ev.items() if e.get('is_paragraph_source')};line_cache={}
    obstacles=[r for r in rows if r.get('type') in ('image','table')];cross_resolved=[]
    for ri,ds in decisions.items():
        for d in ds:
            review=visual_by_key.get((ri,d['raw_offset']))
            if review and review['decision'] in ('split','continue'):
                d.update(decision=review['decision'],basis='assistant_original_pdf_visual_review',visual_case_id=review['case_id'])
                cross_resolved.append({'record_index':ri,'raw_offset':d['raw_offset'],'decision':d['decision'],'basis':d['basis'],'case_id':review['case_id'],'evidence_image':'boundary_visual_review/'+review['evidence_sheet'],'image_sha256':review['image_sha256']})
                continue
            if d['decision']!='unknown':continue
            kind,basis=resolve_cross_page(d['previous_line'],d['current_line'],obstacles)
            if kind=='unknown' and basis in ('column_frame_missing','indent_or_font_changed','body_width_changed','short_line_without_obstruction'):
                a,b=copy.deepcopy(d['previous_line']),copy.deepcopy(d['current_line']);fa=paragraph_column_frame(ev[ri],a);fb=paragraph_column_frame(ev[ri],b)
                if fa:a['page_frame']=fa
                if fb:b['page_frame']=fb
                if fa or fb:
                    alternative,why=resolve_cross_page(a,b,obstacles)
                    if alternative=='continue':kind=alternative;basis='paragraph_column_'+why;d['column_frame_evidence']={'previous':a.get('page_frame'),'current':b.get('page_frame')}
            if kind=='continue':
                d.update(decision=kind,basis=basis);cross_resolved.append({'record_index':ri,'raw_offset':d['raw_offset'],'basis':basis,'previous_line':d['previous_line'],'current_line':d['current_line']})
    for original in old['nodes']:
        n=copy.deepcopy(original);made=[n];protected=bool(n.get('evidence_patch_ids') or n.get('pdf_visual_text_additions') or n['boundary_status'].startswith('pdf_visual'))
        source_types={records[s['record_index']]['type'] for s in n['source_spans']};rids={s['record_index'] for s in n['source_spans']}
        metadata_role='watermark' if source_types=={'seal'} else 'table_of_contents' if (bid=='book_ab313ee3c3dd' and rids<={148,150}) or (bid=='book_59ac4a326ae7' and rids=={6707}) else 'publisher_book_list' if (bid=='book_68ef7cdaa275' and rids=={26}) or (bid=='book_20c6be06f4a3' and rids=={9238}) else None
        typed_reviews=[c for c in visual if rids=={c['record_index']} and c['decision'] not in ('continue','split')]
        if typed_reviews:metadata_role=typed_reviews[0]['decision']
        if metadata_role and n['type']=='paragraph':
            n.update(type=metadata_role,previous_type='paragraph',content_type_basis='original_source_seal_type' if metadata_role=='watermark' else 'explicit_numbered_contents_or_publisher_book_list',included_in_full_graph=True)
            ledger.append({'operation':'correct_content_type','node_id':n['id'],'old_type':'paragraph','new_type':metadata_role,'source_spans':n['source_spans']})
        if n['type']=='paragraph' and len(n['source_spans'])==1 and not protected:
            s=n['source_spans'][0];ri=s['record_index'];a,b=s['start'],s['end'];cuts={a,b};reasons={}
            if n['text']==records[ri]['text'][a:b]:
                for d in decisions.get(ri,[]):
                    k=d['raw_offset']
                    if k and a<k<b and d['decision']=='split':cuts.add(k);reasons[k]=d['basis']
                if (bid,ri) in {('book_20c6be06f4a3',3260),('book_dd8183b4cb60',2178),('book_dd8183b4cb60',2245)}:
                    for l in ev[ri]['lines']:
                        k=l.get('raw_start')
                        start=l.get('list_start') and (ri==3260 or LIST.match(l['text']))
                        if k is not None and a<k<b and (start or re.match(r'^4-48',l['text'])):cuts.add(k);reasons[k]='assistant_original_pdf_numbered_list_or_next_exercise'
                ee,ok,ll=evidence_for(n,ev,records)
                for prior,current in zip(ee,ee[1:]):
                    if owner(position(prior))!=owner(position(current)) and a<current['raw_start']<b:
                        cuts.add(current['raw_start']);reasons[current['raw_start']]='original_pdf_structural_heading_between_lines'
                if len(cuts)>2:
                    made=[];cuts=sorted(cuts)
                    for aa,bb in zip(cuts,cuts[1:]):
                        p=copy.deepcopy(n);p['id']=f'pdfpara6:{bid}:{ri}:{aa}:{bb}';p['text']=records[ri]['text'][aa:bb];p['source_spans']=[dict(s,start=aa,end=bb)];p['boundary_status']='pdf_v6_line_partition';p['layout_repair_applied']=True;made.append(p)
                    split_count+=len(cuts)-2;ledger.append({'operation':'split','old_node_id':n['id'],'new_node_ids':[p['id'] for p in made],'record_index':ri,'cuts':cuts,'reasons':reasons})
        for n in made:
            for c in visual:
                spans=n['source_spans']
                if n['type']!='paragraph' or len(spans)!=1 or spans[0]['record_index']!=c['record_index']:continue
                s=spans[0];kind=c.get('left_type') if s['end']<=c['raw_offset'] else c.get('right_type') if s['start']>=c['raw_offset'] else None
                if kind:
                    n.update(type=kind,previous_type='paragraph',included_in_full_graph=True,content_type_basis='assistant_original_pdf_visual_review',visual_case_id=c['case_id'])
                    ledger.append({'operation':'correct_content_type','node_id':n['id'],'old_type':'paragraph','new_type':kind,'source_spans':spans,'case_id':c['case_id']})
            ee,complete,ll=evidence_for(n,ev,records)
            if ee:pos=position(ee[0]);newowner=owner(pos)
            else:
                s=n['source_spans'][0];pos=(s['page_pdf'] or 1,(s.get('bbox') or [0,0,0,0])[1]);newowner=owner(pos)
            n['previous_heading_id']=n['heading_id'];n['heading_id']=newowner;n['chapter_id']=chapter(newowner)
            n['ownership_status']='pdf_scope' if ee and complete else 'source_position_scope' if n['type']!='paragraph' else 'source_alignment_pending'
            n['assignment_basis']='original_pdf_heading_interval' if ee else 'original_source_position_fallback'
            n['assignment_evidence']={'position':list(pos),'start_heading':newowner,'end_heading':anchored[bisect.bisect_right(points,tuple(pos))]['id'] if bisect.bisect_right(points,tuple(pos))<len(anchored) else None,'line_alignment_complete':complete}
            if n.get('previous_type')=='paragraph':n.update(pdf_evidence=ee,pdf_pages=sorted({e['pdf_page'] for e in ee}),quality_flags=[],review_status='content_type_corrected_source_preserved')
            if n['type']=='paragraph':
                n['pdf_evidence']=ee;n['pdf_pages']=sorted({e['pdf_page'] for e in ee});n['line_alignment_complete']=complete;n['included_in_full_corpus_index']=True;n['pdf_layout_status']='repaired_from_pdf_pixels' if n.get('layout_repair_applied') else 'pdf_source_reconciled';n['quality_flags_history']=n.get('quality_flags',[]);n['quality_flags']=[]
                n['unaligned_ocr_lines']=sum(l.get('raw_start') is None for s in n['source_spans'] for l in ev.get(s['record_index'],{}).get('lines',[]));n['natural_paragraph_boundary_verified']=False;n['human_verified']=False
                line_cache[n['id']]=ll
                if not complete:n['quality_flags'].append('source_alignment_incomplete')
                if ee and len({owner(position(e)) for e in ee})>1:n['quality_flags'].append('paragraph_crosses_heading_anchor')
                if n['previous_heading_id']!=newowner or original.get('ownership_status')=='unresolved':ledger.append({'operation':'assign_structural_parent','node_id':n['id'],'old_parent':n['previous_heading_id'],'new_parent':newowner,'evidence':n['assignment_evidence']})
            nodes.append(n)
    # A paragraph may continue across source blocks after a spurious heading is folded away.
    pars=[n for n in nodes if n['type']=='paragraph'];removed=set();joins=[]
    for a,b in zip(pars,pars[1:]):
        if a['id'] in removed:continue
        if not a['line_alignment_complete'] or not b['line_alignment_complete'] or a['heading_id']!=b['heading_id']:continue
        if any(n.get('evidence_patch_ids') or n.get('pdf_visual_text_additions') or n['boundary_status'].startswith('pdf_visual') for n in (a,b)):continue
        ar=a['source_spans'][-1];br=b['source_spans'][0]
        if ar['record_index']==br['record_index'] or ar['row_index']>=br['row_index']:continue
        if not unobstructed(rows,ar['row_index'],br['row_index']):continue
        al=line_cache[a['id']];bl=line_cache[b['id']]
        if not al or not bl or complex_block({'lines':al}) or complex_block({'lines':bl}):continue
        prev=al[-1];cur=copy.deepcopy(bl[0]);delta=cur['pdf_page']-prev['pdf_page'];af=prev.get('page_frame');bf=cur.get('page_frame')
        if delta not in (0,1) or not af or not bf or not af['single_column'] or not bf['single_column']:continue
        if not prev['pixel_supported'] or not cur['pixel_supported'] or END.search(prev['text']):continue
        if LIST.match(cur['text']) or re.match(r'^\s*[A-Za-z]\d+[.．、:]|^\s*(?:例|习题|练习)\s*\d',cur['text']):continue
        if delta:
            if prev['ink_bbox'][3]<af['bottom']-2*af['em'] or cur['ink_bbox'][1]>bf['top']+2*bf['em']:continue
            if abs((af['right']-af['left'])/(bf['right']-bf['left'])-1)>.15:continue
            cur['ink_bbox'][0]+=af['left']-bf['left'];cur['ink_bbox'][2]+=af['left']-bf['left']
        kind,basis=decide_boundary(prev,cur,af['left'],af['right'],af['em'],af['em']*1.4,bool(delta))
        if kind!='continue':continue
        original_a=a['id'];original_b=b['id'];a['text']=join_prose(a['text'],b['text']);a['source_spans']+=b['source_spans'];a['pdf_evidence']+=b['pdf_evidence'];a['pdf_pages']=sorted(set(a['pdf_pages']+b['pdf_pages']));a['quality_flags']=sorted(set(a['quality_flags']+b['quality_flags']));a['quality_flags_history']=sorted(set(a['quality_flags_history']+b['quality_flags_history']));a['boundary_status']='pdf_v6_continuation';a['layout_repair_applied']=True;a['pdf_layout_status']='repaired_from_pdf_pixels';line_cache[original_a]+=line_cache[original_b];removed.add(original_b)
        joins.append({'operation':'merge','left_node_id':original_a,'right_node_id':original_b,'pages':[prev['pdf_page'],cur['pdf_page']],'basis':basis,'lines':[prev,cur]})
    # Original-page visual evidence identifies prose continuing around an intervening figure.
    for left_ri,right_ri,case_id in [(8265,8268,59)] if bid=='book_20c6be06f4a3' else [(4650,4654,122)] if bid=='book_efaf8522b95d' else []:
        a=next(n for n in nodes if n['type']=='paragraph' and n['id'] not in removed and any(s['record_index']==left_ri for s in n['source_spans']))
        b=next(n for n in nodes if n['type']=='paragraph' and n['id'] not in removed and any(s['record_index']==right_ri for s in n['source_spans']))
        assert a is not b and a['heading_id']==b['heading_id']
        a['text']=join_prose(a['text'],b['text']);a['source_spans']+=b['source_spans'];a['pdf_evidence']+=b['pdf_evidence'];a['pdf_pages']=sorted(set(a['pdf_pages']+b['pdf_pages']));a['quality_flags_history']=sorted(set(a['quality_flags_history']+b['quality_flags_history']));line_cache[a['id']]+=line_cache[b['id']];removed.add(b['id'])
        a.update(boundary_status='pdf_v6_continuation',layout_repair_applied=True,pdf_layout_status='repaired_from_pdf_pixels')
        joins.append({'operation':'merge','left_node_id':a['id'],'right_node_id':b['id'],'basis':'assistant_original_pdf_prose_continues_around_figure','case_id':case_id})
    nodes=[n for n in nodes if n['id'] not in removed]
    for n in nodes:
        if n['type']!='paragraph':continue
        if n['boundary_status']=='pdf_v6_continuation':
            prior=n['id'];n['id']='pdfpara6:'+bid+':merge:'+hashlib.sha256(json.dumps(n['source_spans'],sort_keys=True).encode()).hexdigest()[:16];line_cache[n['id']]=line_cache[prior]
        ll=line_cache[n['id']];open_reasons=[]
        if not n['line_alignment_complete']:open_reasons.append('source_alignment_incomplete')
        # Each inherited warning gets a current-node disposition, not a blanket deletion.
        scoped=[]
        for s in n['source_spans']:
            scoped.extend(d for d in decisions.get(s['record_index'],[]) if d['raw_offset'] is not None and s['start']<d['raw_offset']<s['end'])
        remaining_splits=[d for d in scoped if d['decision']=='split']
        if remaining_splits:open_reasons.append('protected_or_mixed_content_split_candidate')
        list_starts=sum(bool(l.get('list_start') and LIST.match(l['text'])) for l in ll)
        if list_starts>1:open_reasons.append('multiple_visible_list_items')
        cross=[d for d in scoped if d['previous_line'].get('pdf_page')!=d['current_line'].get('pdf_page')]
        if any(d['decision']=='unknown' for d in cross):open_reasons.append('cross_page_geometry_ambiguous')
        if 'paragraph_crosses_heading_anchor' in n['quality_flags']:open_reasons.append('paragraph_crosses_heading_anchor')
        n['quality_flags']=sorted(set(open_reasons));n['layout_review_status']='specific_evidence_review_needed' if open_reasons else 'automated_original_pdf_checks_passed'
        n['quality_flag_dispositions']=[{'old_flag':flag,'status':'specific_check_pending' if open_reasons else 'current_node_rechecked','current_reasons':n['quality_flags'],'basis':'current_source_span_pdf_lines_and_structural_interval'} for flag in n['quality_flags_history']]
        n['review_status']='automated_pdf_evidence_not_human_gold'
        reviews=[c['case_id'] for c in visual if any(s['record_index']==c['record_index'] for s in n['source_spans'])]
        if reviews:n['assistant_visual_review_cases']=reviews
        for addition in n.get('pdf_visual_text_additions',[]):
            if addition['evidence_image'].startswith('visual_checks/'):addition['evidence_image']='../'+V5.name+'/'+addition['evidence_image']
    g['nodes']=nodes;g['headings']=headings;g['local_title_archive']=scope['local_title_archive'];g['schema_version']='pdf-natural-paragraph-graph-v6';g['previous_version']={'path':(V5/bid/'text_graph.json').relative_to(ROOT).as_posix(),'sha256':sha(V5/bid/'text_graph.json')};g['full_corpus_natural_paragraph_verified']=False
    g['edges']=[{'source':h['parent_id'],'target':h['id'],'type':'contains'} for h in headings]+[{'source':n['heading_id'],'target':n['id'],'type':'contains','status':n['ownership_status']} for n in nodes]+[{'source':a['id'],'target':b['id'],'type':'next_physical_text'} for a,b in zip(nodes,nodes[1:])]
    compact_write(folder/'text_graph.json',g);jsonl(folder/'repair_ledger.jsonl',ledger+joins);jsonl(folder/'cross_page_review.jsonl',cross_resolved)
    ps=[n for n in nodes if n['type']=='paragraph'];byrecord=collections.defaultdict(list)
    for n in nodes:
        if n['type']!='paragraph' and n.get('previous_type')!='paragraph':continue
        for s in n['source_spans']:byrecord[s['record_index']].append((n,s))
    jsonl(folder/'v5_to_v6.jsonl',[{'v5_node_id':n['id'],'v6_node_ids':sorted({p['id'] for s in n['source_spans'] for p,t in byrecord[s['record_index']] if overlap(s,t)})} for n in old['nodes'] if n['type']=='paragraph'])
    summary={'book_id':bid,'book_title':book['book_title'],'old_paragraphs':sum(n['type']=='paragraph' for n in old['nodes']),'paragraphs':len(ps),'added_internal_boundaries':split_count,'merged_record_boundaries':len(joins),'cross_page_warnings_resolved':len(cross_resolved),'paragraphs_with_page_evidence':sum(bool(n['pdf_evidence']) for n in ps),'paragraphs_with_complete_line_alignment':sum(n['line_alignment_complete'] for n in ps),'structural_headings':len(headings),'headings_located':len(anchored),'paragraph_parent_changes':sum(x['operation']=='assign_structural_parent' for x in ledger),'review_needed':sum(bool(n['quality_flags']) for n in ps),'review_reasons':dict(collections.Counter(f for n in ps for f in n['quality_flags'])),'all_source_content_preserved':True,'graph_sha256':sha(folder/'text_graph.json')}
    write(folder/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True);return summary

def main():
    p=argparse.ArgumentParser();p.add_argument('--book',action='append');a=p.parse_args();books=[]
    for b in read(V5/'manifest.json')['books']:
        if a.book and b['book_id'] not in a.book:continue
        books.append(build(b))
    summary={k:sum(b[k] for b in books) for k in ('old_paragraphs','paragraphs','added_internal_boundaries','merged_record_boundaries','paragraphs_with_page_evidence','paragraphs_with_complete_line_alignment','structural_headings','headings_located','paragraph_parent_changes','review_needed')}
    write(OUT/'manifest.json',{'version':'v6','books':books,'summary':summary,'previous_manifest_sha256':sha(V5/'manifest.json'),'human_verified':False,'full_corpus_natural_paragraph_verified':False});hierarchy.OUT=OUT;hb=[hierarchy.build(b) for b in books];write(OUT/'层级数据清单.json',{'books':hb,'paragraph_count':summary['paragraphs'],'human_verified':False})
    with (OUT/'全量正文段落.jsonl').open('w',encoding='utf-8') as f:
        for b in books:
            for n in read(OUT/b['book_id']/'hierarchy_graph.json')['nodes']:
                if n['type']=='paragraph':f.write(json.dumps(dict(n,book_id=b['book_id'],book_title=b['book_title']),ensure_ascii=False,separators=(',',':'))+'\n')
    print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
