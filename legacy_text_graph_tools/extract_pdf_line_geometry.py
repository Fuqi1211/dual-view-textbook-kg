# -*- coding: utf-8 -*-
"""Read original PDF pixels, using OCR solely to locate/text-align individual lines."""
import argparse,collections,hashlib,json,pathlib,re,time
import fitz,numpy as np
from build_physical_graphs import ROOT,read,lines,write,jsonl,sha,normalized_positions
R=ROOT/'研究方案_20261002';V4=R/'24本文本图_物理段落v4_20261003';AUDIT=R/'文本图全面核验_20261002'
OUT=R/'24本文本图_PDF自然段修复v5_20261003'
GEOMETRY_VERSION=3

def line_key(line):
    return (tuple(line.get('bbox',[])),tuple((s.get('type'),json.dumps(s.get('content',''),ensure_ascii=False,sort_keys=True)) for s in line.get('spans',[])))

def block_lines(block):
    yield from block.get('lines',[])
    for child in block.get('blocks',[]):yield from block_lines(child)

def page_line_index(pages):
    index=collections.defaultdict(set)
    for pi,page in enumerate(pages):
        for block in page.get('preproc_blocks',[]):
            for line in block_lines(block):index[line_key(line)].add(pi)
    return index

def attribute_line_pages(ls,start,pages,index=None):
    index=page_line_index(pages) if index is None else index
    result=[];cursor=start
    for line in ls:
        if not any(s.get('cross_page') for s in line.get('spans',[])):
            result.append(start);continue
        candidates=sorted(p for p in index.get(line_key(line),[]) if p>start and p>=cursor)
        # A repeated, ambiguous line needs review; page reset alone is not proof.
        target=candidates[0] if len(candidates)==1 else None
        result.append(target)
        if target is not None:cursor=target
    return result

def measure_line(line,pixels,size):
    h,w=pixels.shape;pw,ph=size;b=line['bbox'];sx=w/pw;sy=h/ph
    x0=max(0,int(b[0]*sx)-2);x1=min(w,int(b[2]*sx)+3);y0=max(0,int(b[1]*sy)-1);y1=min(h,int(b[3]*sy)+2)
    crop=pixels[y0:y1,x0:x1];ink=crop<170
    result={'ocr_bbox':b,'text':''.join(str(s.get('content','')) for s in line.get('spans',[])),'pixel_supported':False,'list_start':bool(line.get('is_list_start_line')),'special':not any(s.get('type')=='text' for s in line.get('spans',[]))}
    if crop.size==0:return result
    cols=np.flatnonzero(ink.sum(axis=0)>=max(2,int(crop.shape[0]*.12)))
    rr=np.flatnonzero(ink.sum(axis=1)>=max(2,int(crop.shape[1]*.008)))
    if len(cols)<3 or len(rr)<3:return result
    box=[(x0+int(cols[0]))/sx,(y0+int(rr[0]))/sy,(x0+int(cols[-1])+1)/sx,(y0+int(rr[-1])+1)/sy]
    result.update(ink_bbox=[round(x,3) for x in box],pixel_supported=True,ink_pixels=int(ink.sum()))
    return result

def align_line_starts(record,ls):
    ns,positions=normalized_positions(record['text']);cursor=0
    for line in ls:
        text=normalized_positions(line['text'])[0];offset=None
        # A unique local prefix is enough to locate a boundary; never infer an OCR character.
        for count in (32,20,12,8):
            token=text[:count]
            if len(token)<min(8,len(text)) or len(token)<3:continue
            found=ns.find(token,cursor)
            if found>=0 and ns.find(token,found+1)<0:offset=found;break
        if offset is not None:
            line['raw_start']=positions[offset];cursor=offset+max(1,min(len(text),8))
        else:line['raw_start']=None

def book_geometry(book):
    bid=book['book_id'];folder=OUT/bid;folder.mkdir(parents=True,exist_ok=True)
    g=read(V4/bid/'text_graph.json');paragraph_rids={s['record_index'] for n in g['nodes'] if n['type']=='paragraph' for s in n['source_spans']}
    records=lines(AUDIT/'boundary_audit'/bid/'source_blocks.jsonl');groups=collections.defaultdict(lambda:collections.defaultdict(list));missing=[]
    for r in records:
        if r['record_index'] not in paragraph_rids:continue
        layout=r.get('layout') or {}
        if not layout.get('middle_path'):missing.append(r['record_index']);continue
        parts=layout['json_pointer'].strip('/').split('/');groups[layout['middle_path']][int(parts[1])].append(r)
    output=[];pdfs=[];pages=[];started=time.time()
    for dependency in read(AUDIT/'boundary_audit'/bid/'evidence_inputs.json'):
        mp=ROOT/dependency['path'];middle=read(mp);pdf=mp.with_name(mp.name.replace('_middle.json','_origin.pdf'))
        document=fitz.open(pdf);pdfhash=sha(pdf);pdfs.append({'path':pdf.relative_to(ROOT).as_posix(),'sha256':pdfhash,'pages':len(document),'middle_sha256':sha(mp)})
        assert len(document)==len(middle['pdf_info'])
        jobs=collections.defaultdict(list);index=page_line_index(middle['pdf_info'])
        for start,localrecords in groups.get(dependency['path'],{}).items():
            for r in localrecords:
                block=middle
                for key in r['layout']['json_pointer'].strip('/').split('/'):block=block[int(key)] if isinstance(block,list) else block[key]
                original=[line for line in block.get('lines',[]) if line.get('bbox')]
                attributed=attribute_line_pages(original,start,middle['pdf_info'],index)
                x={'record_index':r['record_index'],'row_index':r['row_index'],'pdf_page':r['page_idx']+1,'pdf_path':pdf.relative_to(ROOT).as_posix(),'pdf_sha256':pdfhash,'local_pdf_page':start+1,'page_size':middle['pdf_info'][start]['page_size'],'lines':[],'source_text_sha256':hashlib.sha256(r['text'].encode()).hexdigest(),'pixel_measurement_complete':True,'visual_reviewed':False}
                for line,target in zip(original,attributed):
                    item={'text':''.join(str(s.get('content','')) for s in line.get('spans',[])),'ocr_bbox':line['bbox'],'pixel_supported':False,'page_attribution':'unresolved_cross_page' if target is None else 'exact_preproc_line_match' if target!=start else 'source_page','pdf_page':None if target is None else r['page_idx']+1+target-start,'local_pdf_page':None if target is None else target+1,'pdf_path':x['pdf_path'],'upstream_cross_page':any(s.get('cross_page') for s in line.get('spans',[]))}
                    x['lines'].append(item)
                    if target is not None:jobs[target].append((item,line,x))
                align_line_starts(r,x['lines']);output.append(x)
        for pi,info in enumerate(middle['pdf_info']):
            page=document[pi];pix=page.get_pixmap(matrix=fitz.Matrix(1.5,1.5),colorspace=fitz.csGRAY,alpha=False)
            pixels=np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width)
            pageitems=jobs[pi]
            for item,line,x in pageitems:
                item.update(measure_line(line,pixels,info['page_size']));item['page_size']=info['page_size']
            good=[l['ink_bbox'] for l,_,_ in pageitems if l['pixel_supported'] and not l['special'] and len(l['text'])>=15]
            if good:
                heights=[b[3]-b[1] for b in good];em=float(np.median(heights));wide=[b for b in good if b[2]-b[0]>em*15]
                if wide:
                    left=float(np.percentile([b[0] for b in wide],15));right=float(np.percentile([b[2] for b in wide],95))
                    frame={'left':left,'right':right,'em':em,'top':min(b[1] for b in good),'bottom':max(b[3] for b in good),'single_column':bool(np.percentile([b[0] for b in wide],80)-left<4*em)}
                    for l,_,x in pageitems:
                        l['page_frame']=frame
                        if pi+1==x['local_pdf_page']:x['page_frame']=frame
            pages.append({'pdf_path':pdf.relative_to(ROOT).as_posix(),'local_pdf_page':pi+1,'pixel_sha256':hashlib.sha256(pix.samples).hexdigest(),'width':pix.width,'height':pix.height,'paragraph_records':len({x['record_index'] for _,_,x in pageitems})})
        document.close()
    output.sort(key=lambda x:x['record_index']);jsonl(folder/'pdf_line_evidence.jsonl',output);write(folder/'pdf_inputs.json',pdfs);write(folder/'rendered_page_coverage.json',pages)
    summary={'geometry_version':GEOMETRY_VERSION,'book_id':bid,'book_title':book['book_title'],'paragraph_source_records':len(paragraph_rids),'records_with_line_evidence':len(output),'missing_layout_records':missing,'rendered_original_pdf_pages':len(pages),'measured_lines':sum(len(x['lines']) for x in output),'pixel_supported_lines':sum(l['pixel_supported'] for x in output for l in x['lines']),'cross_page_lines_relocated':sum(l['page_attribution']=='exact_preproc_line_match' for x in output for l in x['lines']),'unresolved_line_pages':sum(l['pdf_page'] is None for x in output for l in x['lines']),'elapsed_seconds':round(time.time()-started,1),'human_verified':False}
    write(folder/'geometry_summary.json',summary);print(json.dumps(summary|{'missing_layout_records':len(missing)},ensure_ascii=False),flush=True)
    return summary

def main():
    p=argparse.ArgumentParser();p.add_argument('--book',action='append');p.add_argument('--resume',action='store_true');args=p.parse_args();OUT.mkdir(exist_ok=True);summaries=[]
    for b in read(V4/'manifest.json')['books']:
        if args.book and b['book_id'] not in args.book:continue
        cached=OUT/b['book_id']/'geometry_summary.json'
        summaries.append(read(cached) if args.resume and cached.exists() and read(cached).get('geometry_version')==GEOMETRY_VERSION else book_geometry(b))
    write(OUT/'PDF几何覆盖.json',{'books':summaries,'original_pdf_pages_rendered':sum(b['rendered_original_pdf_pages'] for b in summaries),'pixel_supported_lines':sum(b['pixel_supported_lines'] for b in summaries),'status':'pixel_measurement_not_human_verification','implementation_sha256':sha(pathlib.Path(__file__))})
if __name__=='__main__':main()
