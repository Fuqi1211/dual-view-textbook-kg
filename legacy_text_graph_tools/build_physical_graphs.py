# -*- coding: utf-8 -*-
"""Rebuild physical occurrences from OCR positions; never equate automation with gold labels."""
import argparse, bisect, collections, hashlib, html, json, pathlib, re

ROOT = pathlib.Path(__file__).resolve().parents[2]
R = ROOT / '研究方案_20261002'
V3 = R / '24本文本图_证据修复v3_20261002'
AUDIT = R / '文本图全面核验_20261002'
OUT = R / '24本文本图_物理段落v4_20261003'
TRANS = str.maketrans('，（）；：！？．', ',();:!?.')

def read(p):
    return json.loads(pathlib.Path(p).read_text(encoding='utf-8'))

def lines(p):
    with pathlib.Path(p).open(encoding='utf-8') as f:
        return [json.loads(s) for s in f if s.strip()]

def write(p, obj):
    pathlib.Path(p).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(p).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')

def jsonl(p, objects):
    with pathlib.Path(p).open('w', encoding='utf-8') as f:
        for o in objects:
            f.write(json.dumps(o, ensure_ascii=False, separators=(',', ':'))+'\n')

def sha(p):
    h=hashlib.sha256()
    with pathlib.Path(p).open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def rel(p): return pathlib.Path(p).relative_to(ROOT).as_posix()

def normalized_positions(s):
    chars=[];positions=[];i=0
    # Exact escape set/order used by the prior audit; offsets must share its coordinate system.
    while i<len(s):
        if s[i]=='\\' and i+1<len(s) and s[i+1] in '\\`*_{}[]()#+.!|>-':i+=1
        c=s[i]
        if not c.isspace():chars.append(c.translate(TRANS));positions.append(i)
        i+=1
    return ''.join(chars),positions

def norm(s): return normalized_positions(s)[0]

def split_at_markers(s, markers):
    offsets=[]
    for m in markers:
        if not m or s.count(m)!=1: return [(0,len(s))]
        offsets.append(s.index(m))
    if offsets!=sorted(set(offsets)): return [(0,len(s))]
    cuts=sorted(set([0,*offsets,len(s)]))
    return list(zip(cuts,cuts[1:]))

def join_prose(a,b):
    a=a.rstrip();b=b.lstrip()
    return a+(' ' if a and b and a[-1].isascii() and b[0].isascii() and a[-1].isalnum() and b[0].isalnum() else '')+b

def can_merge(a,b,between):
    if any(n.get('type')!='paragraph' or n.get('ownership_status')!='anchored' for n in (a,b)): return False
    if a.get('heading_id')!=b.get('heading_id'): return False
    if any(r.get('type') not in ('header','footer','page_number') for r in between): return False
    if re.search(r'[。！？!?;；：:]\s*$',a['text']): return False
    if re.match(r'\s*(?:\d+[.、]|[（(]\d+[)）])',b['text']): return False
    return True

def raw_id(bid,ri,a,b): return f'physical:{bid}:{ri}:{a}:{b}'

def load_block(record,cache):
    layout=record.get('layout') or {}
    if not layout.get('middle_path'): return None
    path=layout['middle_path']
    if path not in cache: cache[path]=read(ROOT/path)
    x=cache[path]
    for part in layout['json_pointer'].strip('/').split('/'):
        x=x[int(part)] if isinstance(x,list) else x[part]
    return x

def split_layout(record,cache):
    s=record['text']; features=(record.get('layout') or {}).get('features',{})
    indices=features.get('list_start_line_indices',[])
    if len(indices)<2: return [(0,len(s))],None
    block=load_block(record,cache)
    if not block: return [(0,len(s))],None
    ns,positions=normalized_positions(s);cuts=[]
    for i in indices:
        line=block.get('lines',[])[i]
        token=norm(''.join(str(t.get('content',t.get('text',''))) for t in line.get('spans',[])))
        # Only use complete unambiguous line matches, never just a numbered prefix.
        if not token or ns.count(token)!=1: return [(0,len(s))],None
        cuts.append(positions[ns.index(token)])
    if cuts!=sorted(set(cuts)): return [(0,len(s))],None
    cuts=sorted(set([0,*[c for c in cuts if s[:c].strip()],len(s)]))
    return list(zip(cuts,cuts[1:])), 'ocr_list_line_boundaries'

def build_book(book,plan):
    bid=book['book_id'];dest=OUT/bid;dest.mkdir(parents=True,exist_ok=True)
    source=ROOT/book['input_path'];merged=source.parent/'merged_content_list.json'
    rows=read(merged);records=lines(AUDIT/'boundary_audit'/bid/'source_blocks.jsonl')
    old=read(V3/bid/'text_graph.json')
    oldheads={n['id']:n for n in old['nodes'] if n['type'] in ('chapter','section')}
    parents={e['target']:e['source'] for e in old['edges'] if e['type']=='contains' and e['target'] in oldheads}
    heads={k:{f:n[f] for f in ('id','type','label','title','marker','level') if f in n} for k,n in oldheads.items()}
    for k,h in heads.items(): h.update(parent_id=parents.get(k,bid), anchor_status='unlocated')
    events=read(AUDIT/'heading_audit'/bid/'ocr_title_events.json')
    byrow={e['row_index']:e for e in events};contexts={};current=None;anchored=None;chapter=None
    def chapter_of(hid):
        seen=set()
        while hid in heads and hid not in seen:
            seen.add(hid)
            if heads[hid]['type']=='chapter':return hid
            hid=heads[hid].get('parent_id')
        return None
    # Physical title events delimit scopes even where the legacy tree missed a title.
    for ri,row in enumerate(rows):
        event=byrow.get(ri)
        if event:
            hid=event.get('heading_id')
            if hid in heads:
                current=anchored=hid;chapter=chapter_of(hid)
                heads[hid].update(anchor_status='unique_ocr_title',source_row=ri,page_pdf=row.get('page_idx',0)+1)
            else:
                label=event['text'];match=re.match(r'^\s*(\d+(?:\.\d+)+)\s*\D',label)
                # Missing numbered headings can be recovered only under a consistent chapter.
                cm=re.search(r'第\s*(\d+)\s*章',heads.get(chapter,{}).get('label',''))
                numbered=bool(match and cm and match.group(1).split('.')[0]==cm.group(1))
                confirmed=(bid=='book_bef9d155e6d8' and ri in (338,342)) or (bid=='book_ab313ee3c3dd' and ri==373)
                hid=f'physical-heading:{bid}:{ri}'
                parent=anchored or chapter or bid
                if numbered:
                    prefix=match.group(1).rsplit('.',1)[0]
                    candidates=[h for h in heads.values() if h.get('source_row',ri)>=0 and h.get('source_row',ri)<ri and re.match(r'^'+re.escape(prefix)+r'(?![\d.])',h.get('label','').strip())]
                    parent=max(candidates,key=lambda h:h['source_row'])['id'] if candidates else chapter
                if confirmed and bid=='book_bef9d155e6d8' and ri==342: parent=f'physical-heading:{bid}:338'
                heads[hid]={'id':hid,'type':'section' if numbered or confirmed else 'unresolved_heading','label':label,'parent_id':parent,'source_row':ri,'page_pdf':row.get('page_idx',0)+1,'anchor_status':'pdf_visual_case' if confirmed else ('numbered_ocr_title' if numbered else 'unresolved_ocr_title')}
                current=hid
                if numbered or confirmed: anchored=hid
        status='anchored' if current and heads[current]['anchor_status']!='unresolved_ocr_title' else 'unresolved'
        contexts[ri]=(current or bid,status,chapter)
    nodes=[];cache={};record_nodes=collections.defaultdict(list)
    code_evidence=[p for p in plan['patches'] if p['book_id']==bid and p.get('content_kind') in ('code','fenced_code') and not p.get('insert_after')]
    cross=read(AUDIT/'boundary_audit'/bid/'cross_page_candidates.json')
    cross_records={c[k] for c in cross for k in ('left_record','right_record')}
    for record in records:
        ri=record['record_index'];rowi=record['row_index'];s=record['text'];hid,status,cid=contexts[rowi]
        field=record['field'];typ=record['type'];kind='paragraph'
        if rowi in byrow and field=='text':kind='heading_text'
        elif typ in ('header','footer','page_number','page_footnote','aside_text','discarded'):kind='metadata'
        elif 'caption' in field:kind='caption'
        elif 'footnote' in field:kind='footnote'
        elif typ in ('equation','code','table','image'):kind=typ
        if kind=='paragraph' and any(record['page_idx']+1 in p['pdf_pages'] and norm(''.join(p['expected_texts'])) in norm(s) for p in code_evidence):kind='code'
        spans=[(0,len(s))];basis='ocr_block';features=(record.get('layout') or {}).get('features',{})
        risks=list(record.get('layout_risks',[])) if kind=='paragraph' else []
        if kind=='paragraph':
            spans,layout_basis=split_layout(record,cache)
            if layout_basis:basis=layout_basis
            if bid=='book_00849acee112' and rowi==1666:
                spans=split_at_markers(s,['2.超时','3.收到ACK']);basis='pdf_visual_V003';assert len(spans)==3
                risks=[]
            if bid=='book_bef9d155e6d8' and rowi==1872:
                spans=[(0,len(s))];basis='pdf_visual_V007_no_split';risks=[x for x in risks if 'internal' not in x]
        for a,b in spans:
            n={'id':raw_id(bid,ri,a,b),'type':kind,'text':s[a:b], 'heading_id':hid,'chapter_id':cid,'ownership_status':status,'boundary_status':basis,'source_spans':[{'record_index':ri,'row_index':rowi,'field':field,'start':a,'end':b,'page_pdf':record['page_idx']+1,'bbox':record.get('bbox')}], 'quality_flags':list(risks),'human_verified':False,'pdf_ocr_verified':False}
            if kind=='paragraph' and ri in cross_records:n['quality_flags'].append('cross_page_boundary_pending')
            if kind=='paragraph' and not record.get('layout'):n['quality_flags'].append('missing_line_geometry')
            nodes.append(n);record_nodes[ri].append(n)
    # Merge the two visually established windows; other candidates remain explicit until evidence suffices.
    repairlog=[]
    def merge_nodes(ns,basis,force=False):
        ns=sorted(ns,key=lambda n:(n['source_spans'][0]['row_index'],n['source_spans'][0]['start']))
        if len(ns)<2:return ns[0] if ns else None
        for a,b in zip(ns,ns[1:]):
            between=rows[a['source_spans'][-1]['row_index']+1:b['source_spans'][0]['row_index']]
            if not force and not can_merge(a,b,between):return None
            if force and any(r.get('type') not in ('header','footer','page_number') and not (r.get('type')=='discarded' and r.get('bbox') and (r['bbox'][3]<80 or r['bbox'][1]>950)) and not (r.get('type')=='aside_text' and r.get('bbox') and (r['bbox'][0]>900 or r['bbox'][2]<80)) for r in between):return None
        first=ns[0]
        for n in ns[1:]:
            first['text']=join_prose(first['text'],n['text']);first['source_spans']+=n['source_spans'];n['_removed']=True
        first['boundary_status']=basis
        first['quality_flags']=[x for x in first['quality_flags'] if 'cross_page' not in x]
        repairlog.append({'operation':'merge','node_id':first['id'],'basis':basis,'source_records':[s['record_index'] for s in first['source_spans']]})
        return first
    known={'book_bef9d155e6d8':(344,347,'pdf_visual_V001'),'book_00849acee112':(218,220,'pdf_visual_V002')}
    if bid in known:
        a,b,basis=known[bid];ns=[n for n in nodes if n['type']=='paragraph' and n['source_spans'][0]['row_index'] in (a,b)]
        assert len(ns)==2 and merge_nodes(ns,basis,force=True),basis
    # Preserve every earlier PDF correction: apply only page-constrained unique matches,
    # archive unresolved patches and remove affected pages from the default candidate index.
    patches=[p for p in plan['patches'] if p['book_id']==bid];migrations=[];pending_pages=set()
    applied={}
    for p in patches:
        key=(p['sample_id'],tuple(p.get('expected_texts',[])),p.get('replacement_text'),p.get('heading_id'))
        if key in applied:
            migrations.append({'patch_id':p['id'],'status':'duplicate_legacy_occurrence','same_physical_result_as':applied[key]});continue
        active=[n for n in nodes if not n.get('_removed') and any(s['page_pdf'] in p['pdf_pages'] for s in n['source_spans'])]
        targets=[];replacement=p.get('replacement_text');migration='pending'
        if p.get('insert_after') and p.get('upstream_insertion_source'):
            rowi=int(p['upstream_insertion_source']['json_pointer'].strip('/'))
            targets=[n for n in active if n['type']=='code' and n['source_spans'][0]['row_index']==rowi]
            if len(targets)==1:targets[0]['text']=replacement;migration='applied_existing_physical_code'
        elif p['sample_id'] in ('R097','R098'):
            targets=[n for n in active if n['type']=='paragraph']
            for n in targets:n['type']='bibliography_block';n['quality_flags'].append('bibliography_pending_entry_boundaries')
            migration='quarantined_bibliography_pages'
        else:
            expected_text=''.join(p.get('expected_texts',[]))
            if p.get('content_kind')=='heading':expected_text=re.sub(r'^\s*#+\s*','',expected_text)
            expected=norm(expected_text)
            if p['sample_id'] in ('R004','R061'):
                parts=[]
                for t in p['expected_texts']:
                    nt=norm(t);matches=[n for n in active if nt and nt in norm(n['text'])]
                    if len(matches)==1:parts.append(matches[0])
                if len(parts)==len(p['expected_texts']) and len({n['id'] for n in parts})>1:
                    merged_node=merge_nodes(parts,'pdf_visual_'+p['sample_id'],force=True)
                    if merged_node:active=[n for n in active if not n.get('_removed')]
            hits=[]
            for n in active:
                ns,pos=normalized_positions(n['text'])
                if expected and ns.count(expected)==1:hits.append((n,pos[ns.index(expected)],pos[ns.index(expected)+len(expected)-1]+1))
            if len(hits)==1:
                n,a,b=hits[0];targets=[n]
                if replacement is not None:n['text']=n['text'][:a]+replacement.strip()+n['text'][b:]
                migration='applied_unique_page_constrained_match'
            elif replacement and norm(replacement):
                targets=[n for n in active if norm(replacement) in norm(n['text'])]
                if len(targets)==1:migration='already_present_in_physical_source'
                else:targets=[]
        if migration!='pending':
            for n in targets:
                n.setdefault('evidence_patch_ids',[]).append(p['id'])
                if p.get('heading_id') in heads and p['sample_id'] not in ('R097','R098'):
                    n.update(heading_id=p['heading_id'],chapter_id=chapter_of(p['heading_id']),ownership_status='pdf_evidence')
                if p.get('output_type')=='metadata':n['type']='metadata'
                if p.get('content_kind') in ('math','code','fenced_code'):n['type']='equation' if p['content_kind']=='math' else 'code'
            applied[key]=p['id']
        else:pending_pages.update(p['pdf_pages'])
        migrations.append({'patch_id':p['id'],'sample_id':p['sample_id'],'status':migration,'node_ids':[n['id'] for n in targets],'pdf_pages':p['pdf_pages'],'evidence':p['evidence']})
    nodes=[n for n in nodes if not n.get('_removed')]
    # Rebuild physical maps after merges. A source span belongs to exactly one node.
    record_nodes=collections.defaultdict(list)
    for n in nodes:
        for s in n['source_spans']:record_nodes[s['record_index']].append(n['id'])
        if n['type']=='paragraph' and any(s['page_pdf'] in pending_pages for s in n['source_spans']):n['quality_flags'].append('legacy_evidence_patch_pending_on_page')
        n['retrieval_eligible']=n['type']=='paragraph' and n['ownership_status'] in ('anchored','pdf_evidence') and not n['quality_flags']
        n['review_status']='automated_candidate_not_gold'
    # Non-textual equation/image/table rows are preserved as physical assets with source coordinates.
    assets=[];row_nodes=collections.defaultdict(list)
    for n in nodes:
        for s in n['source_spans']:row_nodes[s['row_index']].append(n['id'])
    for ri,row in enumerate(rows):
        if row.get('type') in ('image','table','equation','code'):
            assets.append({'id':f'asset:{bid}:{ri}','type':row['type'],'source_row':ri,'page_pdf':row.get('page_idx',0)+1,'bbox':row.get('bbox'),'img_path':row.get('img_path'),'heading_id':contexts[ri][0],'text_node_ids':list(dict.fromkeys(row_nodes[ri]))})
    alignments={a['canonical_text_id']:a for a in lines(AUDIT/'boundary_audit'/bid/'text_alignment.jsonl')}
    span_index=collections.defaultdict(list)
    for n in nodes:
        for s in n['source_spans']:span_index[s['record_index']].append((s['start'],s['end'],n['id']))
    normalized_map={r['record_index']:normalized_positions(r['text'])[1] for r in records}
    migration_by_id={m['patch_id']:m for m in migrations}
    for m in migrations:
        if m.get('same_physical_result_as'):m['node_ids']=migration_by_id[m['same_physical_result_as']].get('node_ids',[])
    bridge=[]
    for n in old['nodes']:
        if n['type']!='paragraph':continue
        a=alignments.get(n.get('canonical_text_id'),{})
        matches=a.get('matches',[]);candidates=[]
        for match in matches:
            ids=set();ris=match['record_indices'];lo,hi=match.get('normalized_local_range',[0,0])
            for j,ri in enumerate(ris):
                pos=normalized_map[ri]
                start=pos[lo] if j==0 and lo<len(pos) else 0
                end=pos[hi-1]+1 if j==len(ris)-1 and 0<hi<=len(pos) else len(records[ri]['text'])
                ids.update(nid for a,b,nid in span_index[ri] if a<end and start<b)
            ids=sorted(ids)
            candidates.append(ids)
        if not candidates and n.get('repair_id') in migration_by_id:
            ids=migration_by_id[n['repair_id']].get('node_ids',[])
            if ids:candidates=[ids]
        bridge.append({'old_node_id':n['id'],'old_heading_id':n.get('heading_id'),'source_alignment_status':a.get('status','no_alignment'),'mapping_status':'unique_source_candidate' if len(candidates)==1 and not a.get('match_candidates_truncated') else ('ambiguous_source_candidates' if candidates else 'unmapped'),'candidate_node_sets':candidates})
    edges=[]
    for h in heads.values():edges.append({'source':h['parent_id'],'target':h['id'],'type':'contains','status':'candidate' if h['anchor_status'] in ('unlocated','unresolved_ocr_title') else 'anchored'})
    for n in nodes:edges.append({'source':n['heading_id'],'target':n['id'],'type':'contains','status':n['ownership_status']})
    for a,b in zip(nodes,nodes[1:]):edges.append({'source':a['id'],'target':b['id'],'type':'next_physical_text'})
    graph={'schema_version':'physical-text-graph-v4','book_id':bid,'book_title':book['book_title'],'source':{'path':rel(merged),'sha256':sha(merged),'text_records_path':rel(AUDIT/'boundary_audit'/bid/'source_blocks.jsonl'),'text_records_sha256':sha(AUDIT/'boundary_audit'/bid/'source_blocks.jsonl')},'human_verified':False,'full_corpus_natural_paragraph_verified':False,'minimum_prose_node':'natural_paragraph_candidate_with_preserved_raw_spans','headings':list(heads.values()),'nodes':nodes,'assets':assets,'edges':edges}
    write(dest/'text_graph.json',graph)
    jsonl(dest/'retrieval_candidates.jsonl',(n for n in nodes if n['retrieval_eligible']))
    jsonl(dest/'pending_review.jsonl',({'node_id':n['id'],'pages_pdf':sorted(set(s['page_pdf'] for s in n['source_spans'])),'ownership_status':n['ownership_status'],'boundary_status':n['boundary_status'],'reasons':n['quality_flags'],'text_preview':n['text'][:140]} for n in nodes if n['type']=='paragraph' and not n['retrieval_eligible']))
    jsonl(dest/'old_to_physical.jsonl',bridge)
    write(dest/'evidence_patch_migration.json',{'patches':migrations,'source_patches':patches,'merges':repairlog})
    summary={'book_id':bid,'book_title':book['book_title'],'old_paragraph_occurrences':sum(n['type']=='paragraph' for n in old['nodes']),'physical_text_nodes':len(nodes),'paragraph_candidates':sum(n['type']=='paragraph' for n in nodes),'retrieval_candidates':sum(n['retrieval_eligible'] for n in nodes),'pending_paragraphs':sum(n['type']=='paragraph' and not n['retrieval_eligible'] for n in nodes),'source_text_records':len(records),'assets':len(assets),'split_source_records':sum(len(set(v))>1 for v in record_nodes.values()),'merged_nodes':len(repairlog),'pending_legacy_patches':sum(m['status']=='pending' for m in migrations),'patch_statuses':dict(collections.Counter(m['status'] for m in migrations))}
    write(dest/'summary.json',summary)
    summary['artifacts']=[{'path':rel(f),'sha256':sha(f)} for f in sorted(dest.iterdir()) if f.is_file()]
    print(json.dumps(summary|{'artifacts':len(summary['artifacts'])},ensure_ascii=False),flush=True)
    return summary

def report(manifest):
    s=manifest['summary'];rows=''.join('<tr><td>'+html.escape(b['book_title'])+'</td>'+''.join('<td>'+str(b[k])+'</td>' for k in ('old_paragraph_occurrences','paragraph_candidates','retrieval_candidates','pending_paragraphs'))+'<td><a href="'+b['book_id']+'/pending_review.jsonl">未决</a> · <a href="'+b['book_id']+'/text_graph.json">文本图</a></td></tr>' for b in manifest['books'])
    page='''<!doctype html><meta charset="utf-8"><title>24本文本图 v4</title><style>body{font:16px/1.7 system-ui;max-width:1250px;margin:40px auto;color:#203044;background:#f4f7fa}h1{font-size:30px}section{background:white;padding:24px;border-radius:12px;margin:18px 0}table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:9px;text-align:left;border-bottom:1px solid #ddd}a{color:#1168aa}</style><h1>24 本教材 · 物理位置文本图 v4</h1><section><b>已按物理位置重建；全库自然段与章节语义验收仍未通过。</b><p>这份版本修复原章节树复制正文的问题，以原始 OCR 页码、行几何和标题位置重新构图。自动候选可用于开发试验；未决内容保留原文并从默认候选索引排除。自动未报错不代表人工金标准。</p><p>共 BOOKS 本；自然段候选 PARA 个；默认检索候选 ELIG 个；未决段落 PEND 个。</p><p><a href="manifest.json">总清单</a> · <a href="独立验收.json">独立结构与案例验收</a> · <a href="前后对照.json">确认案例前后对照</a></p></section><section><h2>处理规则与限制</h2><p>一个原页位置只对应一份正文。父章节通过层级关系访问段落，不复制正文。确认的跨页段合并，明确编号列表按行证据拆分；公式、代码、图表保留类型与坐标。所有节点保留原文片段和旧节点候选映射。</p><p>待定跨页、缩进异常、未知标题和无法安全迁移的旧补丁均列入未决。历史补丁完整保留，未落地补丁所在页不进入默认候选索引。未逐页视觉验收的段落不可称作已核实自然段。此版不作为论文标注金标准。</p></section><section><table><tr><th>教材</th><th>v3重复实例</th><th>v4段落候选</th><th>检索候选</th><th>未决</th><th>文件</th></tr>ROWS</table></section>'''
    for k,v in {'BOOKS':s['book_count'],'PARA':s['paragraph_candidates'],'ELIG':s['retrieval_candidates'],'PEND':s['pending_paragraphs'],'ROWS':rows}.items():page=page.replace(k,str(v))
    (OUT/'修复结果.html').write_text(page,encoding='utf-8')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--book',action='append');args=parser.parse_args()
    plan=read(R/'文本图修复_v3工作区/v3_repair_plan.json');source=read(V3/'manifest.json')
    OUT.mkdir(exist_ok=True);books=[]
    for b in source['books']:
        if not args.book or b['book_id'] in args.book:books.append(build_book(b,plan))
    manifest={'schema_version':'physical-graph-manifest-v4','status':'rebuilt_with_pending_semantic_verification','source_v3_manifest_sha256':sha(V3/'manifest.json'),'implementation_sha256':sha(__file__),'repair_plan_sha256':sha(R/'文本图修复_v3工作区/v3_repair_plan.json'),'human_verified':False,'full_corpus_natural_paragraph_verified':False,'full_corpus_chapter_ownership_verified':False,'summary':{'book_count':len(books),**{k:sum(b[k] for b in books) for k in ('old_paragraph_occurrences','physical_text_nodes','paragraph_candidates','retrieval_candidates','pending_paragraphs','source_text_records','assets','split_source_records','merged_nodes','pending_legacy_patches')}},'books':books}
    write(OUT/'manifest.json',manifest);report(manifest)

if __name__=='__main__':main()
