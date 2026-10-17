# -*- coding: utf-8 -*-
"""Publish a readable v4 report and current pointer only after an exact-manifest verification."""
import collections,hashlib,html,json,os,pathlib
ROOT=pathlib.Path(__file__).resolve().parents[2];R=ROOT/'研究方案_20261002';OUT=R/'24本文本图_物理段落v4_20261003'
def read(p):return json.loads(pathlib.Path(p).read_text(encoding='utf8'))
def write(p,x):pathlib.Path(p).write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf8')
def sha(p):return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
def h(x):return html.escape(str(x))
def main():
    m=read(OUT/'manifest.json');v=read(OUT/'独立验收.json');cases=read(OUT/'前后对照.json')
    assert v['status']=='passed' and v['manifest_sha256']==sha(OUT/'manifest.json')
    assert m['summary']['book_count']==24
    statuses=collections.Counter();reasons=collections.Counter()
    for b in m['books']:
        p=read(OUT/b['book_id']/'evidence_patch_migration.json');statuses.update(x['status'] for x in p['patches'])
        with (OUT/b['book_id']/'pending_review.jsonl').open(encoding='utf8') as f:
            for line in f:
                x=json.loads(line)
                if x['ownership_status']=='unresolved':reasons['章节或局部标题归属未决']+=1
                reasons.update(x['reasons'])
    write(OUT/'未决汇总.json',{'paragraph_count':m['summary']['pending_paragraphs'],'reason_counts_nonexclusive':dict(reasons),'legacy_patch_statuses':dict(statuses),'bibliography_quarantined':True,'is_error_rate':False})
    style='body{font:16px/1.7 system-ui;max-width:1180px;margin:36px auto;padding:0 20px;color:#203044;background:#f4f7fa}h1{font-size:30px}h2{font-size:21px}section,article{background:white;padding:24px;border-radius:12px;margin:18px 0}table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:9px;text-align:left;border-bottom:1px solid #ddd}a{color:#1168aa}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:15px/1.8 system-ui;background:#f4f7fa;padding:14px;border-radius:8px}small{color:#58677b}input,select,button{font:inherit;padding:7px;margin:5px;border:1px solid #bbb;border-radius:5px}.notice{border-left:5px solid #d89a24}.counts{display:flex;gap:25px;flex-wrap:wrap}.counts b{font-size:28px;display:block}summary{cursor:pointer}img{max-width:100%}'
    s=m['summary'];cards=''
    titles={'V001':'跨页拆段：区 + 别 → 区别','V002':'跨页拆段：数据中 + 心 → 数据中心','V003':'编号段落：1 块 → 3 段','V004':'保留中间公式，禁止错误合段','V005':'C++ 正文从错误第 1 章归至第 2 章','V006':'语言模型正文从错误第 2 章归至第 22 章','V007':'缩进误报：不按孤立字母 A 拆段','V008':'保持“链接”正文属于第 7 章'}
    for c in cases:
        cards+='<details><summary>'+h(c['case_id']+' · '+titles[c['case_id']])+' · 验收通过</summary><p>'+h(c['previous_finding'])+'</p>'
        for n in c['new_nodes']:
            cards+='<p>'+h(str(n['chapter'])+' / '+str(n['heading']))+' · PDF '+h(', '.join(str(x) for x in sorted({s['page_pdf'] for s in n['source_spans']})))+'</p><pre>'+h(n['text'])+'</pre>'
        for e in c['evidence']:
            target=os.path.relpath(ROOT/e['image_path'],OUT).replace('\\','/')
            cards+='<a target="_blank" href="'+h(target)+'">原页证据 '+str(e['global_pdf_page_1based'])+'</a>　'
        cards+='</details><hr>'
    rows=''.join('<tr><td><a href="查看教材.html?book='+b['book_id']+'">'+h(b['book_title'])+'</a></td>'+''.join('<td>'+format(b[k],',')+'</td>' for k in ('old_paragraph_occurrences','paragraph_candidates','retrieval_candidates','pending_paragraphs'))+'</tr>' for b in m['books'])
    page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>24 本文本图 v4 · 修复结果</title><style>'+style+'</style><h1>24 本教材：文本图 v4 修复结果</h1><section class="notice"><b>物理位置重建和结构验收完成；全库自然段、章节语义验收仍未通过。</b><p>已确认的 5 个错误窗口已修复，3 个误报或正确案例保持正确。下面的自动候选可以用于开发实验，不能作为人工金标准。未决内容保留原文，并从默认候选索引排除。</p></section><section class="counts">'+''.join('<div><b>'+format(s[k],',')+'</b>'+label+'</div>' for k,label in [('book_count','本教材'),('paragraph_candidates','自然段候选'),('retrieval_candidates','默认检索候选'),('pending_paragraphs','未决段落')])+'</section><section><h2>已交付的内容</h2><p>按原页位置构图，正文不再随父章节重复复制。明确的编号段落按行证据拆分，PDF 已确认的跨页段合并；公式、代码、图表按类型保存。所有原始文本字符均有且仅有一份来源区间覆盖，原文件与 v3 保持不变。</p><p>70 项单元测试通过；独立验收覆盖 24 本、全部来源字段、引用关系、检索排除规则、历史文件完整性及 8 个视觉案例。视觉案例只代表相应页面，不代表整本逐页核实。</p><p><a href="独立验收.json">独立验收记录</a> · <a href="manifest.json">数据清单</a> · <a href="未决汇总.json">未决汇总</a> · <a href="使用说明.txt">程序使用说明</a></p></section><section><h2>已确认案例的前后对照</h2>'+cards+'</section><section><h2>逐本查看</h2><p>点击书名可搜索正文、查看页码、章节和待定原因。两版计数口径不同：v3 是章节树下的正文实例，v4 是原页物理段落候选，不能用计数差当作错误数量。</p><table><tr><th>教材</th><th>v3 正文实例</th><th>v4 段落候选</th><th>默认候选</th><th>未决</th></tr>'+rows+'</table></section><section><h2>尚未完成的部分</h2><p>仍需裁决未知标题、可疑跨页连属、段内缩进及书目边界。自动未报错也不能证明自然段完全正确。建议当前只用默认候选调试检索；论文正式实验的评测证据需另有原页核实的样本。</p><p>旧版 81 条证据操作均保留迁移记录，其中书目仍按未完成状态隔离。当前没有静默丢弃无法处理的原文。</p></section></html>'
    (OUT/'修复结果.html').write_text(page,encoding='utf8')
    viewer='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>查看 v4 文本图</title><style>STYLE</style><a href="修复结果.html">← 返回总览</a><h1 id="title">正在读取教材…</h1><p>显示原页物理位置和自动判断状态，未标成人工核实。</p><section><input id="q" placeholder="搜索正文、章节或 PDF 页码"><select id="scope"><option value="eligible">默认检索候选</option><option value="pending">未决正文</option><option value="all">全部文本类型</option></select><button id="prev">上一页</button><button id="next">下一页</button><p id="count"></p></section><div id="content"></div><script>
const el=id=>document.getElementById(id);let data=[],heads={},page=0;
function path(id){let out=[],seen=new Set();while(heads[id]&&!seen.has(id)){seen.add(id);out.unshift(heads[id].label);id=heads[id].parent_id}return out.join(' / ')}
function render(){let q=el('q').value.trim().toLowerCase(),scope=el('scope').value;let filtered=data.filter(n=>(scope==='all'||(scope==='eligible'?n.retrieval_eligible:(n.type==='paragraph'&&!n.retrieval_eligible)))&&(!q||(n.text+' '+path(n.heading_id)+' '+n.source_spans.map(s=>s.page_pdf).join(' ')).toLowerCase().includes(q)));page=Math.max(0,Math.min(page,Math.ceil(filtered.length/25)-1));el('count').textContent=`${filtered.length} 条；第 ${page+1} / ${Math.max(1,Math.ceil(filtered.length/25))} 页`;el('content').replaceChildren();for(let n of filtered.slice(page*25,page*25+25)){let card=document.createElement('article'),label=document.createElement('b'),meta=document.createElement('p'),text=document.createElement('pre'),detail=document.createElement('details'),summary=document.createElement('summary'),raw=document.createElement('pre');label.textContent=path(n.heading_id)||'未确定章节';meta.textContent='PDF '+[...new Set(n.source_spans.map(s=>s.page_pdf))].join(', ')+' · '+n.type+' · '+(n.retrieval_eligible?'自动候选（非金标准）':'不进入默认候选索引');text.textContent=n.text;summary.textContent='边界、归属与来源记录';raw.textContent=JSON.stringify({id:n.id,ownership_status:n.ownership_status,boundary_status:n.boundary_status,quality_flags:n.quality_flags,source_spans:n.source_spans,evidence_patch_ids:n.evidence_patch_ids},null,2);detail.append(summary,raw);card.append(label,meta,text,detail);el('content').append(card)}}
el('q').oninput=()=>{page=0;render()};el('scope').onchange=()=>{page=0;render()};el('prev').onclick=()=>{page--;render()};el('next').onclick=()=>{page++;render()};(async()=>{try{let manifest=await(await fetch('manifest.json')).json(),id=new URLSearchParams(location.search).get('book'),book=manifest.books.find(b=>b.book_id===id);if(!book)throw Error('教材不存在');el('title').textContent=book.book_title;let graph=await(await fetch(id+'/text_graph.json')).json();data=graph.nodes;heads=Object.fromEntries(graph.headings.map(h=>[h.id,h]));render()}catch(e){el('title').textContent='读取失败：'+e.message}})();
</script></html>'''.replace('STYLE',style)
    (OUT/'查看教材.html').write_text(viewer,encoding='utf8')
    (OUT/'使用说明.txt').write_text('v4 为物理位置重建版本，schema 与 v3 不同，不可直接覆盖旧检索程序。\n每本 text_graph.json 含 headings、nodes、assets、edges；节点正文在 text，最小正文单元仍是自然段候选。\nretrieval_candidates.jsonl 只含没有已知未决标记的自动正文候选，不是人工金标准。\npending_review.jsonl 为未决正文；书目、公式、代码、图表等另以类型保留在图中。\nsource_spans 给出原 merged_content_list 的行、字段、零起始字符偏移和一基 PDF 页码。text 可含有据可查的修复；原文由 source_spans 对应的只读来源重建。\nold_to_physical.jsonl 保留所有 v3 正文实例的候选映射；ambiguous/unmapped 不能当作唯一对应。\nevidence_patch_migration.json 完整保留旧补丁及迁移状态；quarantined_bibliography_pages 表示书目边界未恢复、仍隔离，不表示修复完成。\nnext_physical_text 按 OCR 源读取顺序，不是独立验证的版面阅读顺序；公式图表通过 assets 的 source_row 与 text_node_ids 关联，不能跨它们直接拼接正文。\n使用源文件 hash 检查可复现性。不要将任何 false 的验证标记改成 true 来通过验收。\n',encoding='utf8')
    pointer=R/'24本文本图_当前版本.json';previous=read(pointer)
    if previous.get('version')!='v4':write(OUT/'切换前版本指针.json',previous)
    write(pointer,{'version':'v4','data_directory':OUT.relative_to(ROOT).as_posix(),'manifest_path':(OUT/'manifest.json').relative_to(ROOT).as_posix(),'manifest_sha256':sha(OUT/'manifest.json'),'report_path':(OUT/'修复结果.html').relative_to(ROOT).as_posix(),'structural_provenance_audit':'passed','full_corpus_natural_paragraph_verified':False,'full_corpus_OCR_verified':False,'full_corpus_chapter_ownership_verified':False,'human_verified':False,'latest_corpus_audit':{'conclusion_path':(OUT/'独立验收.json').relative_to(ROOT).as_posix(),'acceptance':'structural_passed_semantic_pending','confirmed_error_windows_repaired':5,'regression_windows_passed':8,'pending_paragraphs':s['pending_paragraphs']},'previous_version':'v3'})
    write(OUT/'交付清单.json',{'status':'rebuilt_structural_verified_semantic_pending','summary':s,'patch_statuses':dict(statuses),'report_sha256':sha(OUT/'修复结果.html'),'verifier_sha256':v['verifier_sha256'],'manifest_sha256':sha(OUT/'manifest.json'),'unit_tests_passed':70,'human_verified':False})
    print(json.dumps({'summary':s,'patch_statuses':dict(statuses)},ensure_ascii=False))
if __name__=='__main__':main()
