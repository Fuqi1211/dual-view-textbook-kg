# -*- coding: utf-8 -*-
import argparse,collections,copy,json,pathlib,re,statistics
import numpy as np
from extract_pdf_line_geometry import ROOT,V4,AUDIT,OUT
from build_physical_graphs import read,lines,write,jsonl
from pdf_paragraph_rules import decide_boundary,LIST,END

def frame(e,page):
    ls=[l for l in e['lines'] if l.get('pdf_page')==page and l.get('pixel_supported') and not l.get('special')]
    if not ls:return None
    pf=ls[0].get('page_frame')
    if len(ls)==1:return (pf['left'],pf['right'],pf['em'],pf['em']*1.4) if pf else None
    bs=[l['ink_bbox'] for l in ls];em=statistics.median(b[3]-b[1] for b in bs)
    # Body-line alignment must dominate an outdented exercise number.
    left=float(np.median([b[0] for b in bs]));right=max(b[2] for b in bs)
    if pf and pf['single_column'] and abs(left-pf['left'])<=1.25*em:left=pf['left']
    gaps=[b[1]-a[1] for a,b in zip(bs,bs[1:]) if .65*em<b[1]-a[1]<2.5*em]
    return left,right,em,statistics.median(gaps) if gaps else em*1.4

def complex_block(e):
    texts=[l['text'] for l in e['lines']]
    text='\n'.join(texts)
    math_only=len(re.findall(r'[\u4e00-\u9fff]',text))<max(4,.1*len(text)) and bool(re.search(r'[∧∀∃⇔⇒≤≥]|\\(?:frac|sum|begin|forall|exists)|\w+\([^)]*\)\s*[,=]',text))
    return math_only or bool(re.search(r'#\s*include|\bint\s*main|\bdef\s+\w+\s*\(|<IMG\s|//.*(?:cpp|\.h)', text,re.I)) or (len(texts)>5 and sum(bool(re.search(r'\d\s*$',t)) for t in texts)>.55*len(texts))

def internal_decisions(e):
    if complex_block(e):return []
    result=[]
    for previous,current in zip(e['lines'],e['lines'][1:]):
        pa,pb=previous.get('pdf_page'),current.get('pdf_page');f=frame(e,pa)
        if pa is None or pb is None or not f:
            kind,basis='unknown','unresolved_page_or_frame'
        elif pa==pb:
            kind,basis=decide_boundary(previous,current,*f)
        elif pb==pa+1:
            af=previous.get('page_frame');bf=current.get('page_frame');shifted=copy.deepcopy(current)
            if af and bf and af['single_column'] and bf['single_column']:
                shifted['ink_bbox'][0]+=af['left']-bf['left'];shifted['ink_bbox'][2]+=af['left']-bf['left']
                kind,basis=decide_boundary(previous,shifted,af['left'],af['right'],af['em'],af['em']*1.4,True)
            else:kind,basis='unknown','cross_page_frame_unresolved'
        else:kind,basis='unknown','nonadjacent_page_reading_order'
        if current.get('raw_start') is None:kind,basis='unknown','line_start_not_uniquely_aligned'
        result.append({'raw_offset':current.get('raw_start'),'decision':kind,'basis':basis,'previous_line':previous,'current_line':current})
    return result

def unobstructed(rows,start,end):
    for row in rows[start+1:end]:
        if row.get('type') in ('header','footer','page_number','page_footnote'):continue
        b=row.get('bbox')
        if row.get('type')=='discarded' and b and (b[3]<90 or b[1]>930):continue
        if row.get('type')=='aside_text' and b and (b[0]>900 or b[2]<80):continue
        return False
    return True

def propose(book):
    bid=book['book_id'];folder=OUT/bid;g=read(V4/bid/'text_graph.json');hg=read(V4/bid/'hierarchy_graph.json');parents={n['id']:n['parent_id'] for n in hg['nodes'] if n['type']=='paragraph'}
    records=lines(AUDIT/'boundary_audit'/bid/'source_blocks.jsonl');evidence={x['record_index']:x for x in lines(folder/'pdf_line_evidence.jsonl')};rows=read(ROOT/g['source']['path'])
    record_nodes=collections.defaultdict(list);protected=set()
    for n in g['nodes']:
        if n['type']!='paragraph':continue
        for s in n['source_spans']:record_nodes[s['record_index']].append(n)
        if n.get('evidence_patch_ids') or n['boundary_status'].startswith('pdf_visual'):
            protected.update(s['record_index'] for s in n['source_spans'])
    partitions=[];changes=[]
    for ri,ns in record_nodes.items():
        oldcuts=sorted({s['start'] for n in ns for s in n['source_spans'] if s['record_index']==ri}|{len(records[ri]['text'])});cuts=set(oldcuts);decisions=internal_decisions(evidence[ri]) if ri in evidence else []
        if ri not in protected:
            for d in decisions:
                offset=d['raw_offset']
                if offset is None or offset==0:continue
                if d['decision']=='split':cuts.add(offset)
                if d['decision']=='continue' and offset in cuts:cuts.remove(offset)
        newcuts=sorted(cuts)
        x={'record_index':ri,'source_row':records[ri]['row_index'],'old_cuts':oldcuts,'cuts':newcuts,'decisions':decisions,'protected_pdf_evidence':ri in protected,'pdf_page':records[ri]['page_idx']+1,'text_preview':records[ri]['text'][:160]}
        partitions.append(x)
        if oldcuts!=newcuts:changes.append({'operation':'split_or_join_within_record',**x})
    ordered=sorted(record_nodes,key=lambda ri:(records[ri]['row_index'],ri));joins=[];checks=[]
    for left_ri,right_ri in zip(ordered,ordered[1:]):
        a=evidence.get(left_ri);b=evidence.get(right_ri)
        if not a or not b or not a['lines'] or not b['lines']:continue
        if complex_block(a) or complex_block(b):continue
        ln=record_nodes[left_ri][-1];rn=record_nodes[right_ri][0]
        if left_ri in protected or right_ri in protected:continue
        if records[right_ri]['row_index']<=records[left_ri]['row_index']:continue
        if not unobstructed(rows,records[left_ri]['row_index'],records[right_ri]['row_index']):continue
        if parents[ln['id']]!=parents[rn['id']]:continue
        previous=a['lines'][-1];current=copy.deepcopy(b['lines'][0])
        if previous.get('pdf_page') is None or current.get('pdf_page') is None:continue
        pages=current['pdf_page']-previous['pdf_page']
        if pages not in (0,1):continue
        af=previous.get('page_frame');bf=current.get('page_frame')
        if not af or not bf or not af['single_column'] or not bf['single_column']:continue
        if not previous.get('pixel_supported') or not current.get('pixel_supported'):continue
        if END.search(previous['text']):continue
        if LIST.match(current['text']) or re.match(r'^\s*[A-Za-z]\d+[.．、:]|^\s*(?:例|习题|练习)\s*\d',current['text']):continue
        if current.get('special') or previous.get('special'):continue
        em=af['em'];step=em*1.4
        if pages:
            if previous['ink_bbox'][3]<af['bottom']-em*2 or current['ink_bbox'][1]>bf['top']+bf['em']*2:continue
            if abs((af['right']-af['left'])/(bf['right']-bf['left'])-1)>.15:continue
            current['ink_bbox'][0]+=af['left']-bf['left'];current['ink_bbox'][2]+=af['left']-bf['left']
        kind,basis=decide_boundary(previous,current,af['left'],af['right'],em,step,bool(pages))
        # Across OCR blocks, only flush/full-line continuation triggers an automatic merge.
        item={'left_record':left_ri,'right_record':right_ri,'left_row':records[left_ri]['row_index'],'right_row':records[right_ri]['row_index'],'pages':[previous['pdf_page'],current['pdf_page']],'decision':kind,'basis':basis,'previous_line':previous,'current_line':current,'parent_id':parents[ln['id']]}
        checks.append(item)
        if kind=='continue':joins.append(item);changes.append({'operation':'merge_cross_page' if pages else 'merge_same_page',**item})
    write(folder/'boundary_proposals.json',{'partitions':partitions,'joins':joins,'cross_block_checks':checks,'changes':changes,'status':'proposed_from_original_pdf_pixels_not_human_verified'})
    print(book['book_title'],'changed records',sum(x['operation']=='split_or_join_within_record' for x in changes),'joins',len(joins),flush=True)
    return {'book_id':bid,'book_title':book['book_title'],'changes':len(changes),'joins':len(joins),'cross_page_joins':sum(x['pages'][0]!=x['pages'][1] for x in joins),'internal_changed_records':sum(x['operation']=='split_or_join_within_record' for x in changes)}
def main():
    p=argparse.ArgumentParser();p.add_argument('--book',action='append');args=p.parse_args();summaries=[]
    for b in read(V4/'manifest.json')['books']:
        if not args.book or b['book_id'] in args.book:summaries.append(propose(b))
    write(OUT/'边界修复建议汇总.json',summaries)
if __name__=='__main__':main()
