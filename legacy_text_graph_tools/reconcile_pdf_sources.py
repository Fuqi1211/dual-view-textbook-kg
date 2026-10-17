# -*- coding: utf-8 -*-
import argparse,collections,copy,json,pathlib,re,unicodedata,time
import fitz,numpy as np
from extract_pdf_line_geometry import ROOT,OUT as V5,AUDIT,page_line_index,attribute_line_pages,measure_line
from build_physical_graphs import read,lines,write,jsonl,sha,TRANS
OUT=ROOT/'研究方案_20261002/24本文本图_版面归属修复v6_20261003'

def normalized(text):
    chars=[];pos=[];i=0
    while i<len(text):
        if text[i]=='\\':i+=1;continue
        for c in unicodedata.normalize('NFKC',text[i]).translate(TRANS):
            if not c.isspace():chars.append(c);pos.append(i)
        i+=1
    return ''.join(chars),pos

def aligned_line_ranges(record,line_texts):
    target,positions=normalized(record)
    if not target:return None
    ns=[normalized(t)[0] for t in line_texts];whole=''.join(ns);at=whole.find(target)
    if at<0:
        # A printed line-end hyphen may be a soft wrap OR part of a genuine word.
        # Match each discretionary hyphen independently instead of dropping all of them.
        pat=''.join('('+re.escape(t[:-1])+r'-?)' if i+1<len(ns) and re.search(r'[A-Za-z]-$',t) and re.match(r'[A-Za-z]',ns[i+1]) else '('+re.escape(t)+')' for i,t in enumerate(ns))
        match=re.fullmatch(pat,target)
        if match:ns=list(match.groups());whole=''.join(ns);at=0
        else:
            ns=[t[:-1] if i+1<len(ns) and re.search(r'[A-Za-z]-$',t) and re.match(r'[A-Za-z]',ns[i+1]) else t for i,t in enumerate(ns)]
            whole=''.join(ns);at=whole.find(target)
    if at<0 or whole.find(target,at+1)>=0:return None
    result=[];cursor=0
    for i,text in enumerate(ns):
        a=max(cursor,at)-at;b=min(cursor+len(text),at+len(target))-at
        if a<b:
            result.append((i,0 if a==0 else positions[a],len(record) if b==len(target) else positions[b]))
        cursor+=len(text)
    return result

def catalog_book(bid):
    catalog=[]
    for d in read(AUDIT/'boundary_audit'/bid/'evidence_inputs.json'):
        mp=ROOT/d['path'];m=read(mp);pages=m['pdf_info'];idx=page_line_index(pages);base=int(re.search(r'/chunk_(\d+)_',d['path']).group(1));pdfpath=mp.with_name(mp.name.replace('_middle.json','_origin.pdf')).relative_to(ROOT).as_posix()
        for pi,page in enumerate(pages):
            for bi,block in enumerate(page.get('para_blocks',[])):
                raw=[l for l in block.get('lines',[]) if l.get('bbox')]
                if not raw:continue
                items=[]
                for line,actual in zip(raw,attribute_line_pages(raw,pi,pages,idx)):
                    text=''.join(str(s.get('content','')) for s in line.get('spans',[]))
                    items.append({'text':text,'raw_line':line,'pdf_path':pdfpath,'pdf_page':None if actual is None else base+actual+1,'local_pdf_page':None if actual is None else actual+1,'page_size':None if actual is None else pages[actual]['page_size']})
                catalog.append({'index':len(catalog),'middle_path':d['path'],'json_pointer':f'/pdf_info/{pi}/para_blocks/{bi}','block_type':block['type'],'bbox':block.get('bbox'),'start_page':base+pi+1,'lines':items,'normalized_text':normalized(''.join(l['text'] for l in items))[0]})
    return catalog

def reconcile(book,repair=False):
    bid=book['book_id'];folder=OUT/bid;folder.mkdir(parents=True,exist_ok=True);started=time.time()
    records=lines(AUDIT/'boundary_audit'/bid/'source_blocks.jsonl');graph=read(V5/bid/'text_graph.json')
    needed={s['record_index'] for n in graph['nodes'] if n['type'] in ('paragraph','heading_text') for s in n['source_spans']}
    pars={s['record_index'] for n in graph['nodes'] if n['type']=='paragraph' for s in n['source_spans']}
    previous={e['record_index']:e for e in lines((OUT if repair else V5)/bid/'pdf_line_evidence.jsonl')};catalog=lines(folder/'pdf_block_catalog.jsonl') if repair else catalog_book(bid)
    bypointer={(b['middle_path'],b['json_pointer']):b for b in catalog};bytext=collections.defaultdict(list);prefix=collections.defaultdict(set)
    for b in catalog:
        b['normalized_text']=normalized(''.join(l['text'] for l in b['lines']))[0]
        bytext[b['normalized_text']].append(b)
        for l in b['lines']:
            t=normalized(l['text'])[0]
            if len(t)>=16:prefix[t[:16]].add(b['index'])
    counts=collections.Counter();mapped=[];evidence=[];jobs=collections.defaultdict(list);context={}
    if repair:
        existing=lines(folder/'source_reconciliation.jsonl');needed={a['record_index'] for a in existing if a['status']=='text_alignment_unresolved'}
        context={a['record_index']:a for a in existing if a.get('middle_path')}
        mapped=[a for a in existing if a['record_index'] not in needed];evidence=[e for ri,e in previous.items() if ri not in needed]
        counts.update(a['status'] for a in mapped)
    pdfhash={p['path']:p['sha256'] for p in read(V5/bid/'pdf_inputs.json')}
    for r in records:
        ri=r['record_index']
        if ri not in needed:continue
        layout=r.get('layout') or {};oldblock=bypointer.get((layout.get('middle_path'),layout.get('json_pointer')));chosen=None;ranges=None;method=None
        if oldblock:
            ranges=aligned_line_ranges(r['text'],[l['text'] for l in oldblock['lines']])
            if ranges:chosen=oldblock;method='complete_record_matches_original_block'
        if not chosen:
            target=normalized(r['text'])[0];candidates=bytext.get(target,[])
            if not candidates and len(target)>=16:candidates=[catalog[i] for i in prefix.get(target[:16],[]) if aligned_line_ranges(r['text'],[l['text'] for l in catalog[i]['lines']])]
            if len(candidates)>1:
                candidates=[b for b in candidates if b['start_page']==r['page_idx']+1]
            if len(candidates)>1 and r['field'].startswith('list_items/'):
                neighbors=[]
                for other,entry in context.items():
                    if records[other]['row_index']!=r['row_index']:continue
                    block=bypointer.get((entry['middle_path'],entry['json_pointer']))
                    if block:neighbors.append((other,block['index']))
                lower=max((x for x in neighbors if x[0]<ri),default=(0,-1))[1]
                upper=min((x for x in neighbors if x[0]>ri),default=(10**12,10**12))[1]
                if upper==10**12:
                    for nxt in range(ri+1,min(ri+4,len(records))):
                        entry=context.get(nxt)
                        if not entry or records[nxt]['page_idx']!=r['page_idx']:continue
                        block=bypointer.get((entry['middle_path'],entry['json_pointer']))
                        if block:upper=block['index'];break
                scoped=[b for b in candidates if lower<b['index']<upper]
                if len(scoped)==1:candidates=scoped
            if len(candidates)==1:
                ranges=aligned_line_ranges(r['text'],[l['text'] for l in candidates[0]['lines']])
                if ranges:chosen=candidates[0];method='unique_full_record_relocated_in_original_pdf'
        audit={'record_index':ri,'source_row':r['row_index'],'field':r['field'],'old_page':r['page_idx']+1,'old_layout':layout,'status':method or 'text_alignment_unresolved'}
        if chosen:
            audit.update(middle_path=chosen['middle_path'],json_pointer=chosen['json_pointer'],block_type=chosen['block_type'],actual_pages=sorted({chosen['lines'][i]['pdf_page'] for i,_,_ in ranges if chosen['lines'][i]['pdf_page'] is not None}))
            ls=[];prior=previous.get(ri,{});oldlines={(normalized(l['text'])[0],tuple(l['ocr_bbox']),l.get('pdf_page')):l for l in prior.get('lines',[])}
            for i,start,end in ranges:
                l=chosen['lines'][i];raw=l['raw_line'];key=(normalized(l['text'])[0],tuple(raw['bbox']),l['pdf_page']);reuse=oldlines.get(key)
                item=copy.deepcopy(reuse) if reuse else {'text':l['text'],'ocr_bbox':raw['bbox'],'pixel_supported':False}
                item.update(raw_start=start,raw_end=end,pdf_page=l['pdf_page'],pdf_path=l['pdf_path'],local_pdf_page=l['local_pdf_page'],page_size=l['page_size'],page_attribution='whole_record_text_and_preproc_line_match')
                ls.append(item)
                if not reuse and l['local_pdf_page'] is not None:jobs[(l['pdf_path'],l['local_pdf_page'])].append((item,raw,l['page_size']))
            e={'record_index':ri,'row_index':r['row_index'],'pdf_page':audit['actual_pages'][0] if audit['actual_pages'] else None,'pdf_path':ls[0]['pdf_path'],'pdf_sha256':pdfhash[ls[0]['pdf_path']],'local_pdf_page':ls[0]['local_pdf_page'],'lines':ls,'alignment_status':method,'source_text_sha256':__import__('hashlib').sha256(r['text'].encode()).hexdigest(),'is_paragraph_source':ri in pars}
            evidence.append(e)
            context[ri]=audit
        elif ri in previous:
            e=copy.deepcopy(previous[ri]);e['alignment_status']='previous_partial_alignment_retained';e['is_paragraph_source']=True;evidence.append(e)
        counts[audit['status']]+=1;mapped.append(audit)
    documents={}
    try:
        for (pdf,local),items in jobs.items():
            if pdf not in documents:documents[pdf]=fitz.open(ROOT/pdf)
            pix=documents[pdf][local-1].get_pixmap(matrix=fitz.Matrix(1.5,1.5),colorspace=fitz.csGRAY,alpha=False);pixels=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width)
            for item,raw,size in items:item.update(measure_line(raw,pixels,size))
    finally:
        for d in documents.values():d.close()
    jsonl(folder/'source_reconciliation.jsonl',mapped);jsonl(folder/'pdf_line_evidence.jsonl',evidence)
    # Catalog is the independent physical title/line index used by the ownership pass.
    jsonl(folder/'pdf_block_catalog.jsonl',catalog)
    summary={'book_id':bid,'book_title':book['book_title'],'status_counts':dict(counts),'reconciled_records':len(mapped),'paragraph_source_records':len(pars),'new_pixel_pages':len(jobs),'elapsed_seconds':round(time.time()-started,1)}
    write(folder/'source_reconciliation_summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True);return summary

def main():
    p=argparse.ArgumentParser();p.add_argument('--book',action='append');p.add_argument('--resume',action='store_true');p.add_argument('--repair-unresolved',action='store_true');args=p.parse_args();out=[]
    for b in read(V5/'manifest.json')['books']:
        if args.book and b['book_id'] not in args.book:continue
        cached=OUT/b['book_id']/'source_reconciliation_summary.json';out.append(read(cached) if args.resume and cached.exists() else reconcile(b,args.repair_unresolved))
    write(OUT/'原页重定位汇总.json',out)
if __name__=='__main__':main()
