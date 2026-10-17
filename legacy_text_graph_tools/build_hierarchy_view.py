# -*- coding: utf-8 -*-
"""Materialize book/chapter/section/subsection/paragraph hierarchy without dropping prose."""
import collections,hashlib,html,json,pathlib,re
ROOT=pathlib.Path(__file__).resolve().parents[2]
OUT=ROOT/'研究方案_20261002/24本文本图_物理段落v4_20261003'
def read(p):return json.loads(p.read_text(encoding='utf8'))
def write(p,x):p.write_text(json.dumps(x,ensure_ascii=False,separators=(',',':')),encoding='utf8')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def heading_role(h):
    if h['type']=='chapter':return 'chapter'
    if h['type']=='unresolved_heading':return None
    # Numbered headings newly recovered from physical pages are true structural candidates.
    # Unnumbered local titles remain metadata and must not become fictitious subsections.
    if h['id'].startswith('physical-heading:'):
        match=re.match(r'^\s*(\d+(?:\.\d+)+)(?=\s|[\u4e00-\u9fffA-Za-z]|$)',h['label'])
        if not match:return None
        return 'section' if len(match.group(1).split('.'))==2 else 'subsection'
    return 'section' if h.get('level',2)==2 else 'subsection'

def build(book):
    bid=book['book_id'];folder=OUT/bid;source=folder/'text_graph.json';g=read(source)
    sourceheads={h['id']:h for h in g['headings']};roles={hid:heading_role(h) for hid,h in sourceheads.items()}
    selected={hid for hid,role in roles.items() if role}
    def ancestor(hid):
        seen=set()
        while hid in sourceheads and hid not in seen:
            if hid in selected:return hid
            seen.add(hid);hid=sourceheads[hid]['parent_id']
        return bid
    nodes=[{'id':bid,'type':'book','label':book['book_title'],'parent_id':None,'children':[]}];lookup={bid:nodes[0]}
    for h in g['headings']:
        if h['id'] not in selected:continue
        n={**h,'type':roles[h['id']],'source_heading_type':h['type'],'parent_id':ancestor(h['parent_id']),'children':[]}
        nodes.append(n);lookup[n['id']]=n
    counts=collections.Counter();remapped=0
    for p in g['nodes']:
        if p['type']!='paragraph':continue
        parent=ancestor(p['heading_id']);remapped+=parent!=p['heading_id']
        n={**p,'parent_id':parent,'source_heading_id':p['heading_id'],'heading_id':parent,'children':[],'included_in_full_hierarchy':True,'assignment_basis':'existing_physical_heading' if parent==p['heading_id'] else 'nearest_structural_ancestor_of_local_title','assignment_verified':False}
        if g.get('schema_version')=='pdf-natural-paragraph-graph-v6' and parent==p['heading_id']:n['assignment_basis']=p['assignment_basis']
        if parent!=p['heading_id']:n['local_title_context']=sourceheads.get(p['heading_id'],{}).get('label')
        nodes.append(n);lookup[n['id']]=n;counts[lookup[parent]['type']]+=1
    for n in nodes:
        if n['parent_id'] is not None:lookup[n['parent_id']]['children'].append(n['id'])
    def order(hid,seen=None):
        n=lookup[hid]
        if g.get('schema_version')=='pdf-natural-paragraph-graph-v6':
            if n.get('physical_anchor'):return tuple(n['physical_anchor']['position'])+(-1,)
            if n['type']=='paragraph' and n.get('pdf_evidence'):
                e=n['pdf_evidence'][0];return (e['pdf_page'],e['bbox'][1]/e['page_size'][1]*1000,n['source_spans'][0]['record_index'])
        if n['type']=='paragraph':return min((s['row_index'],s['record_index'],s['start']) for s in n['source_spans'])
        if '_sort' in n:return tuple(n['_sort'])
        seq=[(n['source_row'],-1,-1)] if n.get('source_row') is not None else []
        seq.extend(order(k) for k in n['children'])
        position=min(seq) if seq else (10**12,0,0);n['_sort']=position
        n['children'].sort(key=lambda k:order(k));return position
    order(bid)
    if g.get('schema_version')=='pdf-natural-paragraph-graph-v6':
        for n in nodes:n['children'].sort(key=order)
    for n in nodes:
        n.pop('_sort',None)
        path=[];cur=n['id'];seen=set()
        while cur and cur not in seen:
            seen.add(cur);path.append(cur);cur=lookup[cur]['parent_id']
        assert cur is None,'cycle'
        n['path_ids']=list(reversed(path))
        if n['type']=='paragraph':
            n['chapter_id']=next((x for x in n['path_ids'] if lookup[x]['type']=='chapter'),None)
            n['section_id']=next((x for x in n['path_ids'] if lookup[x]['type']=='section'),None)
            n['subsection_id']=next((x for x in reversed(n['path_ids']) if lookup[x]['type']=='subsection'),None)
    graph={'schema_version':'book-heading-paragraph-hierarchy-v1','root_id':bid,'book_title':book['book_title'],'source_graph':{'path':'text_graph.json','sha256':sha(source)},'hierarchy_pattern':['book','chapter','section','subsection','paragraph'],'scope_note':'Preserve real heading depth. Paragraphs before a subsection remain under the real section/chapter; unassigned/frontmatter may be directly under book. Local OCR titles are context, not fabricated structural nodes.','human_verified':False,'semantic_assignment_verified':False,'nodes':nodes,'edges':[{'source':n['parent_id'],'target':n['id'],'type':'contains'} for n in nodes if n['parent_id'] is not None],'local_title_archive':[h for h in g['headings'] if h['id'] not in selected],'nonprose_source':'text_graph.json contains all code, formulas, figures, tables, bibliography, captions and metadata; not deleted by this structural view.'}
    target=folder/'hierarchy_graph.json';write(target,graph)
    return {'book_id':bid,'book_title':book['book_title'],'paragraphs':sum(n['type']=='paragraph' for n in nodes),'heading_counts':dict(collections.Counter(n['type'] for n in nodes if n['type']!='paragraph')),'paragraphs_by_parent_type':dict(counts),'local_title_context_folded_into_structural_parent':remapped,'path':str(target.relative_to(OUT)).replace('\\','/'),'sha256':sha(target)}

def report(manifest):
    links=''.join('<li><a href="层级查看.html?book='+b['book_id']+'">'+html.escape(b['book_title'])+'</a>：'+format(b['paragraphs'],',')+' 段</li>' for b in manifest['books'])
    css='body{font:16px/1.7 system-ui;max-width:1150px;margin:30px auto;padding:0 20px;color:#203044;background:#f5f7fa}section{background:#fff;padding:24px;border-radius:10px;margin:18px 0}details{margin:8px 0 8px 18px;border-left:2px solid #d4dce5;padding-left:14px}summary{cursor:pointer}article{padding:12px 18px;background:#eef3f7;margin:10px 0;border-radius:8px}pre{white-space:pre-wrap;font:16px/1.8 system-ui}a{color:#1766a0}small{color:#65748a}select,button,input{font:inherit;padding:6px;margin:5px}'
    (OUT/'层级结构入口.html').write_text('<!doctype html><meta charset="utf-8"><title>书名—章—节—小节—段落</title><style>'+css+'</style><h1>完整层级文本图</h1><section><b>书名 → 章 → 节 → 小节 → 自然段</b><p>24 本、118,213 个正文段落全部纳入，每段只有一个父节点。点击标题逐层展开，直到自然段。原书更深的标题层级保留；章前、节前正文挂在真实所属层级，不编造小节。</p><p>局部加粗标题、编号条目等不再自动充当新的章节层级，它们保留为段落上下文。待核验标记保留，完整显示不代表已经确认全部边界和归属。</p><p><a href="层级完整性验收.json">完整性验收</a> · <a href="层级数据清单.json">数据下载清单</a> · <a href="修复结果.html">修复记录与原页证据</a></p></section><section><ul>'+links+'</ul></section>',encoding='utf8')
    viewer='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>章节树与自然段</title><style>CSS</style><a href="层级结构入口.html">← 24 本教材</a><h1 id="title">读取中…</h1><p>全部正文保留。点击章、节、小节逐层展开；展开的小节内显示自然段。</p><section><input id="query" placeholder="查找章节标题"><button id="search">展开匹配章节</button><button id="reset">收起全部</button><p id="status"></p></section><section id="tree"></section><script>
const el=id=>document.getElementById(id);let nodes={},root=null,desc={},details={};const names={book:'书',chapter:'章',section:'节',subsection:'小节',paragraph:'自然段'};
function total(id){if(desc[id]!==undefined)return desc[id];let n=nodes[id];return desc[id]=n.type==='paragraph'?1:n.children.reduce((s,c)=>s+total(c),0)}
function item(id){let n=nodes[id];if(n.type==='paragraph'){let a=document.createElement('article'),meta=document.createElement('small'),p=document.createElement('pre');meta.textContent='自然段 · PDF '+[...new Set(n.source_spans.map(s=>s.page_pdf))].join(', ')+(n.ownership_status==='unresolved'?' · 归属待核验':'')+(n.quality_flags.length?' · 边界等质量标记保留':'');p.textContent=n.text;a.append(meta,p);return a}let d=document.createElement('details'),s=document.createElement('summary'),box=document.createElement('div');details[id]=d;s.textContent=(names[n.type]||n.type)+' · '+n.label+'（'+total(id)+' 段）';d.append(s,box);d.ontoggle=()=>{if(d.open&&!d.dataset.loaded){d.dataset.loaded='1';if(n.type==='book'){for(let c of n.children.filter(c=>nodes[c].type!=='paragraph'))box.append(item(c));let direct=n.children.filter(c=>nodes[c].type==='paragraph');if(direct.length){let group=document.createElement('details'),label=document.createElement('summary');label.textContent='未挂到章节点的内容（含书前内容、归属待核验） · '+direct.length+' 段';group.append(label);group.ontoggle=()=>{if(group.open&&!group.dataset.loaded){group.dataset.loaded='1';for(let c of direct)group.append(item(c))}};box.append(group)}}else{for(let c of n.children)box.append(item(c))}}};return d}
function reset(){details={};el('tree').replaceChildren(item(root));details[root].open=true}
async function expand(id){let path=nodes[id].path_ids;for(let ancestor of path){let d=details[ancestor];if(d){d.open=true;d.dispatchEvent(new Event('toggle'))}}}
el('reset').onclick=reset;el('search').onclick=async()=>{let q=el('query').value.trim();if(!q)return;let found=Object.values(nodes).filter(n=>n.type!=='paragraph'&&n.label.includes(q));for(let n of found.slice(0,30))await expand(n.id);el('status').textContent='匹配 '+found.length+' 个标题，已展开前 '+Math.min(found.length,30)+' 个'};
(async()=>{try{let m=await(await fetch('层级数据清单.json')).json(),id=new URLSearchParams(location.search).get('book'),b=m.books.find(x=>x.book_id===id);if(!b)throw Error('教材不存在');let g=await(await fetch(b.path)).json();nodes=Object.fromEntries(g.nodes.map(n=>[n.id,n]));root=g.root_id;el('title').textContent=g.book_title;el('status').textContent=total(root)+' 个自然段，包含全部待核验段落；每段只有一个父节点。';reset()}catch(e){el('title').textContent=e.message}})();
</script></html>'''.replace('CSS',css)
    (OUT/'层级查看.html').write_text(viewer,encoding='utf8')
def main():
    source=read(OUT/'manifest.json');books=[]
    for b in source['books']:
        result=build(b);books.append(result);print(result['book_title'],result['paragraphs'],flush=True)
    m={'schema_version':'hierarchy-manifest-v1','source_manifest_sha256':sha(OUT/'manifest.json'),'implementation_sha256':sha(pathlib.Path(__file__)),'paragraph_count':sum(b['paragraphs'] for b in books),'books':books,'human_verified':False,'full_corpus_semantic_verified':False}
    write(OUT/'层级数据清单.json',m);report(m)
if __name__=='__main__':main()
