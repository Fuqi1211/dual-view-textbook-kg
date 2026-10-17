# -*- coding: utf-8 -*-
"""Locate structural headings in the original PDF line catalogue, not OCR event order."""
import argparse,collections,copy,difflib,json,re,unicodedata
from reconcile_pdf_sources import ROOT,V5,OUT,AUDIT,normalized
from build_physical_graphs import read,lines,write,jsonl,sha
from build_hierarchy_view import heading_role

def chinese_number(s):
    if s.isdigit():return int(s)
    digits=dict(zip('零一二三四五六七八九',range(10)));total=0;unit=0
    for c in s:
        if c in digits:unit=digits[c]
        elif c in '十百':total+=(unit or 1)*{'十':10,'百':100}[c];unit=0
        else:return None
    return total+unit

def numbered_title(text):
    t=unicodedata.normalize('NFKC',text).strip()
    t=re.sub(r'^(\d+\.\d+)-(\d+)',r'\1.\2',t)
    m=re.match(r'^第\s*([零一二三四五六七八九十百\d\s]+)\s*章\s*(.*)$',t)
    if m:return ('chapter',str(chinese_number(re.sub(r'\s','',m[1]))),m[2].strip())
    m=re.match(r'^附录\s*([A-Z])\s*(.*)$',t)
    if m:return ('chapter',m[1],m[2].strip())
    m=re.match(r'^((?:\d{1,2}|[A-Z])(?:\s*\.\s*\d{1,2})+)(?=\s|[^\d/])(.*)$',t)
    if not m:return None
    marker=re.sub(r'\s','',m[1]);title=m[2].strip().lstrip('.').strip()
    if not title or title.startswith('/') or re.match(r'^位系统',title):return None
    return ('section',marker,title)

def norm(t):return normalized(t)[0].lower()
def similarity(a,b):return difflib.SequenceMatcher(None,norm(a),norm(b)).ratio()

def physical_titles(catalog):
    result=[];repeated=collections.Counter(norm(''.join(l['text'] for l in b['lines'])) for b in catalog if b['block_type']=='title')
    for b in catalog:
        if not b['lines']:continue
        txt=''.join(l['text'] for l in b['lines'])
        if b['block_type']!='title' and not (b['block_type']=='text' and len(b['lines'])<=2 and len(norm(txt))<85 and re.match(r'^[*★\s]*(?:\d|[A-Z]\.|第)',txt)):continue
        first=b['lines'][0];size=first['page_size']
        if first['pdf_page'] is None or not size:continue
        box=b['bbox'];y=box[1]/size[1]*1000;text=''.join(l['text'] for l in b['lines'])
        # Margin text is not a body heading. Body titles near the top remain eligible.
        if y<40 or (y<110 and repeated[norm(text)]>2):continue
        result.append(dict(index=b['index'],text=text,parsed=numbered_title(text.lstrip('*★☆ ')),position=[first['pdf_page'],round(y,3)],bbox=box,page_size=size,pdf_path=first['pdf_path'],local_pdf_page=first['local_pdf_page'],middle_path=b['middle_path'],json_pointer=b['json_pointer'],block_type=b['block_type']))
    # Process overview diagrams repeat several later section headings in parallel columns.
    occurrences=collections.defaultdict(set)
    for t in result:occurrences[norm(t['text'])].add(t['position'][0])
    max_height=collections.defaultdict(float)
    for t in result:max_height[norm(t['text'])]=max(max_height[norm(t['text'])],(t['bbox'][3]-t['bbox'][1])/t['page_size'][1])
    result=[t for t in result if not (t['position'][1]<110 and (t['bbox'][3]-t['bbox'][1])/t['page_size'][1]<max_height[norm(t['text'])]*.92)]
    repeated_sections=collections.defaultdict(set)
    for t in result:
        p=t['parsed']
        if p and p[0]=='section' and len(occurrences[norm(t['text'])])>1:repeated_sections[t['position'][0]].add(p[1])
    overview={page for page,markers in repeated_sections.items() if len(markers)>=3}
    return [t for t in result if not (t['position'][0] in overview and len(occurrences[norm(t['text'])])>1)]

def head_number(h):
    p=numbered_title(h['label']) or numbered_title(h.get('marker','')+' '+h.get('title',''))
    return p[1] if p else None

def reconcile(book):
    bid=book['book_id'];g=read(V5/bid/'text_graph.json');catalog=lines(OUT/bid/'pdf_block_catalog.jsonl');titles=physical_titles(catalog)
    records=lines(AUDIT/'boundary_audit'/bid/'source_blocks.jsonl');mapping=lines(OUT/bid/'source_reconciliation.jsonl')
    pointerrows=collections.defaultdict(list)
    for m in mapping:
        if m.get('middle_path'):pointerrows[m['middle_path'],m['json_pointer']].append(m['source_row'])
    for t in titles:t['source_rows']=pointerrows[t['middle_path'],t['json_pointer']]
    original={h['id']:h for h in g['headings']};canonical=[copy.deepcopy(h) for h in g['headings'] if not h['id'].startswith('physical-heading:') and heading_role(h)]
    def chapter_of(h):
        seen=set()
        while h and h['id'] not in seen:
            if h['type']=='chapter':return h['id']
            seen.add(h['id']);h=original.get(h.get('parent_id'))
    for h in canonical:
        h['old_anchor_status']=h.get('anchor_status');h['old_source_row']=h.get('source_row');h['original_parent_id']=h['parent_id'];h['_chapter']=chapter_of(h);h['_number']=head_number(h)
        h.pop('physical_anchor',None)
    chapters=[h for h in canonical if h['type']=='chapter'];byid={h['id']:h for h in canonical};used={}
    def anchor(h,t,basis,score=1):
        h['physical_anchor']={k:v for k,v in t.items() if k not in ('parsed','source_rows')};h['physical_anchor'].update(basis=basis,match_score=round(score,4),human_verified=False)
        h['page_pdf']=t['position'][0];h['source_row']=min(t['source_rows']) if t.get('source_rows') else h.get('source_row');h['anchor_status']='pdf_structural_title_located';used[t['index']]=h['id']
    def choose(h,candidates,basis):
        if not candidates:return False
        ranked=[]
        for t in candidates:
            parsed=t['parsed'];title=parsed[2] if parsed else t['text'];score=max(similarity(h['title'],title),similarity(h['label'],t['text']))
            if h['type']=='chapter' and h['_number'] and parsed and parsed[:2]==('chapter',h['_number']):score=max(score,.91)
            if h['type']!='chapter' and h.get('_number') and parsed and parsed[:2]==('section',h['_number']) and t.get('block_type')=='title':score=max(score,.8)
            if h['old_source_row'] in t['source_rows']:score+=.08
            ranked.append((score,t))
        ranked.sort(key=lambda a:(-a[0],a[1]['position']))
        if ranked[0][0]<.56:return False
        if len(ranked)>1 and ranked[0][0]-ranked[1][0]<.035:
            tied=[t for score,t in ranked if ranked[0][0]-score<.035]
            if h['type']=='chapter' and len({norm(t['text']) for t in tied})==1 and max(t['position'][0] for t in tied)-min(t['position'][0] for t in tied)<=3:
                anchor(h,min(tied,key=lambda t:t['position']),basis+'_earliest_repeated_body_title',ranked[0][0]);return True
            return False
        anchor(h,ranked[0][1],basis,ranked[0][0]);return True
    # Chapter numbers may be Chinese in print; canonical OCR trees used Arabic numbers.
    for h in chapters:
        descendants=[v for v in canonical if v['_chapter']==h['id'] and v['type']!='chapter' and v.get('page_pdf')]
        first=min((v['page_pdf'] for v in descendants),default=None)
        c=[t for t in titles if t.get('block_type')=='title' and t['parsed'] and t['parsed'][:2]==('chapter',h['_number'])] if h['_number'] else []
        if first:c=[t for t in c if first-25<=t['position'][0]<=first]
        if not choose(h,c,'chapter_number_and_body_title'):
            c=[t for t in titles if t.get('block_type')=='title' and norm(t['text']) in (norm(h['title']),norm(h['label'])) and (first is None or first-25<=t['position'][0]<=first)]
            if not choose(h,c,'unique_body_chapter_title'):
                c=[t for t in titles if t.get('block_type')=='title' and max(similarity(t['text'],h['title']),similarity(t['text'],h['label']))>=.78 and (first is None or first-25<=t['position'][0]<=first)]
                choose(h,c,'near_exact_body_chapter_title')
        if h.get('physical_anchor') and h['_number'] and h['title'].startswith('第'):
            t=next(t for t in titles if t['index']==h['physical_anchor']['index'])
            if t['parsed'] and t['parsed'][2]:h['title']=t['parsed'][2];h['label']='第'+h['_number']+'章 '+h['title']
    # PDF image-only chapter banner read directly on page 215 (printed page 204).
    if bid=='book_bcdf0a6ec957':
        h=next(h for h in chapters if h['_number']=='6');t=next(t for t in titles if t['position'][0]==215)
        visual=dict(t,index=-6,text='第6章 输入/输出技术',position=[215,153.0],bbox=[.205*t['page_size'][0],.153*t['page_size'][1],.829*t['page_size'][0],.195*t['page_size'][1]],source_rows=[],json_pointer=None)
        anchor(h,visual,'assistant_read_original_pdf_image_only_banner');h['physical_anchor']['image']='visual_checks/book_bcdf0a6ec957_215.png'
    checked={
        'book_00849acee112':[(1,19,'计算机网络和因特网'),(3,139,'运输层'),(6,303,'链路层和局域网'),(8,403,'计算机网络中的安全'),(9,457,'多媒体网络')],
        'book_3f9468271781':[(3,59,'问题归约知识表示及其搜索技术'),(8,204,'人工神经网络'),(9,230,'机器学习基础'),(10,256,'模式识别')],
        'book_d0e37f34c683':[(1,14,'大数据概述'),(9,180,'图计算'),(12,227,'大数据在生物医学领域的应用')],
        'book_bef9d155e6d8':[(10,657,'系统级I/O')],
        'book_90a5a9d8cda2':[(1,17,'引论')],
        'book_90f1b914e55e':[(1,10,'网络编程概述'),(2,38,'基于TCP套接字的编程'),(7,158,'阻塞式/非阻塞式I/O')],
        'book_efaf8522b95d':[(3,115,'输入输出系统'),(4,186,'存储系统'),(5,274,'流水技术与向量处理'),(6,345,'并行处理技术')],
    }
    for number,page,title in checked.get(bid,[]):
        h=next(h for h in chapters if h['_number']==str(number));line=next(l for b in catalog for l in b['lines'] if l['pdf_page']==page);size=line['page_size']
        image=f'visual_checks/{bid}_{page}.png';visual=dict(index=-100-number,text=f'第{number}章 {title}',position=[page,80.0],bbox=None,page_size=size,pdf_path=line['pdf_path'],local_pdf_page=line['local_pdf_page'],source_rows=[],middle_path=None,json_pointer=None)
        h['original_label']=h['label'];h.update(label=visual['text'],title=title)
        anchor(h,visual,'assistant_read_original_pdf_chapter_banner')
        h['physical_anchor'].update(image=image,image_sha256=sha(OUT/image),region_normalized_1000=[40,75,970,280],position_note='chapter begins on this page; banner region, not a measured OCR line box')
    # Match numbered sections inside their real chapter interval. This also resolves
    # OCR variants such as I/O versus 1/0 without changing source prose.
    for ci,ch in enumerate(chapters):
        start=ch.get('physical_anchor',{}).get('position',[0,0]);end=next((n['physical_anchor']['position'] for n in chapters[ci+1:] if n.get('physical_anchor')),[10**9,0])
        for h in [v for v in canonical if v['_chapter']==ch['id'] and v['type']!='chapter']:
            candidates=[t for t in titles if start<=t['position']<end and t['index'] not in used]
            numbered=[t for t in candidates if t['parsed'] and t['parsed'][:2]==('section',h['_number'])] if h['_number'] else []
            numbered_titles=[t for t in numbered if t['block_type']=='title']
            if choose(h,numbered_titles or numbered,'section_number_and_title_in_chapter'):continue
            exact=[t for t in candidates if norm(t['text']) in (norm(h['label']),norm(h['title']))]
            if choose(h,exact,'unique_title_within_chapter'):continue
            near=[t for t in candidates if similarity(h['label'],t['text'])>=(.77 if len(norm(h['label']))>=7 else .88) and len(norm(h['label']))>=5 and not any(v['id']!=h['id'] and v['_chapter']==ch['id'] and (similarity(v['label'],t['text'])>=.98 or (t['parsed'] and v['_number']==t['parsed'][1] and similarity(v['title'],t['parsed'][2])>=.75)) for v in canonical)]
            if choose(h,near,'near_exact_numbered_title_within_chapter'):continue
            # Preserve explicitly evidenced legacy fixes if the actual source title is present.
            prior=[t for t in candidates if h['old_source_row'] in t['source_rows'] and similarity(t['text'],h['label'])>=.75]
            choose(h,prior,'original_anchor_rechecked_in_pdf')
    for h in canonical:
        if h['type']!='chapter' and h['_chapter'] is None:
            choose(h,[t for t in titles if norm(t['text'])==norm(h['label']) and t['index'] not in used],'independent_book_backmatter_title')
    if bid=='book_ab313ee3c3dd':
        for marker,page,y,title in [('4.3.3',102,737,'string类的其他操作'),('10.6.2',390,281,'作用域内枚举(C++11)'),('16.3.3',698,690,'对矢量可执行的其他操作'),('16.7.1',738,416,'vector、valarray和array'),('17.3.2',774,256,'流状态'),('18.1',813,464,'复习前面介绍过的C++11功能')]:
            h=next(h for h in canonical if h['_number']==marker);line=next(l for b in catalog for l in b['lines'] if l['pdf_page']==page);image=f'visual_checks/{bid}_{page}.png'
            visual=dict(index=-1000-int(marker.replace('.','')),text=marker+' '+title,position=[page,float(y)],bbox=None,page_size=line['page_size'],pdf_path=line['pdf_path'],local_pdf_page=line['local_pdf_page'],source_rows=[],middle_path=None,json_pointer=None)
            h['original_label']=h['label'];h.update(label=visual['text'],title=title);anchor(h,visual,'assistant_read_original_pdf_section_title')
            h['physical_anchor'].update(image=image,image_sha256=sha(OUT/image),region_normalized_1000=[70,y-5,930,y+28],position_note='section title read on original page; region is a visual locator')
    # Collapse duplicate OCR headings onto canonical titles; retain genuinely missing
    # numbered titles only when their chapter exists and their number is unique there.
    duplicate={};extra=[];known={(h['_chapter'],h['_number']) for h in canonical if h['_number']}
    for old in g['headings']:
        if not old['id'].startswith('physical-heading:') or heading_role(old) is None:continue
        p=numbered_title(old['label'])
        if not p or p[0]!='section':continue
        ch=next((h for h in chapters if h['_number']==p[1].split('.')[0]),None)
        if not ch:continue
        existing=next((h for h in canonical if h['_chapter']==ch['id'] and h['_number']==p[1]),None)
        if existing:duplicate[old['id']]=existing['id'];continue
        c=[t for t in titles if old.get('source_row') in t['source_rows'] and t['parsed'] and t['parsed'][:2]==p[:2]]
        if len(c)==1:
            if c[0]['index'] in used:duplicate[old['id']]=used[c[0]['index']];continue
            if ch.get('physical_anchor') and c[0]['position']<ch['physical_anchor']['position']:continue
            h=copy.deepcopy(old);h.update(_chapter=ch['id'],_number=p[1],original_parent_id=old['parent_id'],old_anchor_status=old.get('anchor_status'));anchor(h,c[0],'additional_numbered_pdf_title');extra.append(h);known.add((ch['id'],p[1]))
    canonical.extend(extra);byid={h['id']:h for h in canonical}
    retired=[]
    for h in canonical:
        p=byid.get(h['parent_id'])
        if not h.get('physical_anchor') and p and p.get('physical_anchor') and norm(h.get('title',''))==norm(p.get('title','')):
            duplicate[h['id']]=p['id'];retired.append(dict(h,retirement_reason='duplicate_child_title_of_located_parent'))
    canonical=[h for h in canonical if h['id'] not in duplicate];byid={h['id']:h for h in canonical}
    # Number depth defines chapter/section/subsection, independent of erroneous input levels.
    for h in canonical:
        if h['type']=='chapter':h['level']=1;h['parent_id']=bid
        elif h['_number']:
            h['level']=len(h['_number'].split('.'));prefix=h['_number'].rsplit('.',1)[0]
            parent=next((p for p in canonical if p['_chapter']==h['_chapter'] and p['_number']==prefix and p['type']!='chapter'),None)
            h['parent_id']=parent['id'] if parent else h['_chapter'] or bid
        else:h['parent_id']=h['parent_id'] if h['parent_id'] in byid else h['_chapter'] or bid;h.setdefault('level',2)
        if not h.get('physical_anchor'):h['anchor_status']='physical_anchor_unresolved';h.pop('source_row',None)
    if bid=='book_ab313ee3c3dd':
        # The printed edition itself repeats some numbers. Preserve the printed marker
        # but keep these headings at their physical depth, rather than inventing .1 levels.
        for oldnum,printed,parentnum,page in [('16.4.4.1','16.4.4','16.4',720),('16.4.5.1','16.4.5','16.4',725),('G.4.1','G.4','G',902),('18.8.2.1','18.8.2','18.8',851),('18.8.2','18.8.2','18.1',813)]:
            h=next((h for h in canonical if h['_number']==oldnum and h['physical_anchor']['position'][0]==page),None)
            if not h:continue
            par=next(h for h in canonical if h['_number']==parentnum and (h['type']=='chapter' if parentnum=='G' else h['type']!='chapter'))
            h['original_label']=h['label'];h['printed_marker']=printed;h['label']=printed+' '+h['title'];h['parent_id']=par['id'];h['level']=par['level']+1;h['numbering_note']='Printed duplicate/inconsistent number retained; parent follows original PDF position and heading typography.';h['numbering_evidence_image']=f'visual_checks/{bid}_{page}.png'
    if bid=='book_d96f472c8179':
        h=next(h for h in canonical if h['_number']=='9.2');h['original_label']=h['label'];h.update(label='9.2 调度算法',title='调度算法')
    if bid=='book_9f3cc4860e73':
        h=next(h for h in canonical if h['type']=='chapter' and h['_number']=='14');h['original_label']=h['label'];h.update(label='第14章 异常处理',title='异常处理')
    diagnostics={'book_id':bid,'book_title':book['book_title'],'structural_headings':len(canonical),'located':sum(bool(h.get('physical_anchor')) for h in canonical),'missing_chapters':[{'id':h['id'],'label':h['label']} for h in chapters if not h.get('physical_anchor')],'missing_sections':[{'id':h['id'],'label':h['label'],'chapter':h['_chapter'],'old_row':h.get('old_source_row')} for h in canonical if h['type']!='chapter' and not h.get('physical_anchor')],'duplicate_ocr_headings':len(duplicate)}
    write(OUT/bid/'heading_scopes.json',{'headings':canonical,'duplicate_heading_redirects':duplicate,'retired_duplicate_structural_headings':retired,'local_title_archive':[h for h in g['headings'] if h['id'] not in byid],'diagnostics':diagnostics})
    print(json.dumps({k:v for k,v in diagnostics.items() if k!='missing_sections'},ensure_ascii=False),flush=True);return diagnostics

def main():
    p=argparse.ArgumentParser();p.add_argument('--book',action='append');a=p.parse_args();result=[]
    for b in read(V5/'manifest.json')['books']:
        if a.book and b['book_id'] not in a.book:continue
        result.append(reconcile(b))
    write(OUT/'章节定位汇总.json',result)
if __name__=='__main__':main()
