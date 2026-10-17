"""Build a factual static report from actual full-coverage audit outputs."""
from collections import Counter,defaultdict
from datetime import datetime,timezone
import html
import json
import os
from pathlib import Path
import urllib.parse
from audit_paragraph_boundaries import ROOT,DEFAULT_INPUT,dump,digest

BASE=ROOT/'研究方案_20261002/文本图全面核验_20261002'
STATUS={
 'ocr_layout_agreement_only':'OCR文本框与版面相符（未逐页视觉确认）',
 'ocr_block_agreement_only':'仅OCR文本框边界相符',
 'boundary_review_required':'边界或正文角色存在风险，待复核',
 'unresolved_physical_occurrence':'同文多处，具体位置未确定',
 'unresolved_source_alignment':'未建立完整文字定位',
 'non_prose_unit_separate_policy':'代码、公式等非普通自然段',
 'ocr_direct_owner_supported':'OCR标题区间支持直接归属',
 'ocr_ancestor_coverage_supported':'支持祖先章节覆盖，未当作直接归属',
 'unresolved_heading_evidence':'缺少明确的小节标题证据',
 'ownership_conflict_candidate':'小节归属冲突候选（非确认错误数）',
 'crosses_heading_boundary_candidate':'跨标题边界候选',
 'ocr_chapter_supported':'OCR章标题区间支持所属章',
 'chapter_conflict_candidate':'所属章冲突候选（非确认错误数）',
 'unresolved_chapter_evidence':'缺少明确的章标题证据',
 'crosses_chapter_boundary_candidate':'跨章候选'}
CSS='''body{margin:0;background:#f4f5f7;color:#17212e;font:16px/1.65 "Microsoft YaHei",sans-serif}main{max-width:1180px;margin:auto;padding:36px 28px}h1{font-size:32px;margin:0 0 10px}h2{margin:30px 0 12px;font-size:23px}h3{margin:0 0 10px}a{color:#165d9a}a:hover{text-decoration:none}.muted{color:#536575}.banner{border-left:6px solid #b33a27;background:#fff1ed;padding:22px 26px;border-radius:8px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:22px 0}.card,details,.panel{background:white;padding:18px 22px;border:1px solid #dce1e5;border-radius:8px}.card strong{display:block;font-size:28px}.warn{color:#9b382b}table{width:100%;border-collapse:collapse;background:white;font-size:14px}th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #e3e8ed;vertical-align:top}th{background:#eaf0f5;position:sticky;top:0}details{margin:12px 0}summary{cursor:pointer;font-weight:600}.images{display:flex;gap:12px;overflow:auto}.images img{width:340px;max-width:100%;height:auto;align-self:start;border:1px solid #d3d9df}.evidence{border-left:3px solid #8ba8bb;padding-left:15px;white-space:pre-wrap;font-size:14px}code{overflow-wrap:anywhere;font-size:12px}input{padding:10px;width:100%;box-sizing:border-box;border:1px solid #aebcc8;border-radius:6px;margin:10px 0}small{font-size:12px}.scroll{overflow-x:auto}.badge{display:inline-block;background:#edf2f7;border-radius:5px;padding:2px 9px;font-size:13px}footer{margin-top:36px;color:#526476;font-size:13px}@media(max-width:720px){.cards{grid-template-columns:repeat(2,1fr)}main{padding:20px 14px}h1{font-size:26px}}'''
def esc(x):return html.escape(str(x),quote=True)
def read(p):return json.loads(p.read_text('utf-8'))
def readl(p):return [json.loads(l) for l in p.open(encoding='utf-8')]
def href(path,current=BASE):return urllib.parse.quote(Path(os.path.relpath(path,current)).as_posix(),safe='/#')
def link(path,label,current=BASE):return f'<a href="{href(path,current)}">{esc(label)}</a>'
def page(title,body):return f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)}</title><style>{CSS}</style><main>{body}</main></html>'
def count_table(counts):return '<table><tr><th>核验状态</th><th>段落出现实例</th></tr>'+''.join(f'<tr><td>{esc(STATUS.get(k,k))}</td><td>{v:,}</td></tr>' for k,v in counts.items())+'</table>'

def build_book(book):
    bid=book['book_id'];bd=BASE/'boundary_audit'/bid;hd=BASE/'heading_audit'/bid
    b,h=read(bd/'summary.json'),read(hd/'summary.json')
    boundary=readl(bd/'paragraph_audit.jsonl');ownership=readl(hd/'paragraph_ownership.jsonl')
    sets=defaultdict(set)
    for r in boundary:sets['boundary:'+r['status']].add(r['canonical_text_id'])
    for r in ownership:
        sets['section:'+r['status']].add(r['canonical_text_id'])
        sets['chapter:'+r['chapter_status']].add(r['canonical_text_id'])
    # The machine-readable results enumerate all nodes; this readable page samples 30 unique risk texts.
    g=read(DEFAULT_INPUT/bid/'text_graph.json');nodes={n['id']:n for n in g['nodes']};texts={t['id']:t['text'] for t in g['paragraph_texts']}
    brows={r['node_id']:r for r in boundary};chosen=[];seen=set()
    for r in ownership:
        if r['chapter_status']!='chapter_conflict_candidate' or r['canonical_text_id'] in seen:continue
        seen.add(r['canonical_text_id']);chosen.append((r,brows[r['node_id']]))
        if len(chosen)>=15:break
    for r in boundary:
        if r['status']!='boundary_review_required' or r['canonical_text_id'] in seen:continue
        seen.add(r['canonical_text_id']);chosen.append((None,r))
        if len(chosen)>=30:break
    body=f'<p>{link(BASE/"开始查看.html","← 返回总报告")}</p><h1>{esc(book["book_title"])}</h1><p>所有 {b["paragraph_occurrences"]:,} 个出现实例、{h["heading_count"]:,} 个章节节点均有核验记录。下表为自动证据状态；未宣称全书原页验收通过。</p>'
    body+='<h2>完整数据</h2><p>'+ ' · '.join([link(bd/'paragraph_audit.jsonl','全部段落边界结论'),link(hd/'paragraph_ownership.jsonl','全部章节归属结论'),link(hd/'heading_anchors.jsonl','全部章节标题锚点'),link(bd/'source_blocks.jsonl','OCR版面证据'),link(bd/'text_alignment.jsonl','全文定位候选'),link(bd/'cross_page_candidates.json','跨页候选')])+'</p>'
    body+='<div class="panel"><h3>自然段边界</h3>'+count_table(b['status_counts'])+'</div><h2>所属章</h2>'+count_table(h['chapter_status_counts'])+'<h2>小节直接归属 / 祖先覆盖</h2>'+count_table(h['status_counts'])
    body+='<h2>风险示例</h2><p>这里只展示最多30种文本供浏览，完整清单见上方文件。以下自动候选尚未逐一PDF确认，不能按其数量报告错误率。</p>'
    for i,(o,r) in enumerate(chosen,1):
        n=nodes[r['node_id']];text=texts[r['canonical_text_id']]
        body+=f'<details><summary>{i}. {esc(" / ".join(n.get("title_path",[])))}</summary><p>{esc(STATUS.get(r["status"],r["status"]))}</p>'
        if o:body+=f'<p>{esc(STATUS.get(o["chapter_status"],o["chapter_status"]))}；候选PDF页：{esc(o["candidate_pages_pdf"])}</p>'
        body+=f'<div class="evidence">{esc(text[:2200])}{"…（完整原文见图文件）" if len(text)>2200 else ""}</div><p><code>{esc(n["id"])}</code></p><p>原因：{esc(", ".join(r["reasons"]))}</p></details>'
    path=BASE/f'{bid}.html';path.write_text(page(book['book_title'],body),encoding='utf-8')
    return {'book_id':bid,'book_title':book['book_title'],'paragraph_occurrences':b['paragraph_occurrences'],
       'unique_texts':b['unique_active_texts'],'heading_count':h['heading_count'],
       'boundary_status_counts':b['status_counts'],'section_status_counts':h['status_counts'],
       'chapter_status_counts':h['chapter_status_counts'],'unique_text_counts_by_status':{k:len(v) for k,v in sets.items()},
       'cross_page_candidates':b['cross_page_candidates'],'report_path':path.name}

def main():
    b=read(BASE/'boundary_audit/summary.json');h=read(BASE/'heading_audit/summary.json')
    v=read(BASE/'独立完整性验收.json');inventory=read(BASE/'inputs_inventory/layout_inputs_inventory.json')
    visual=read(BASE/'visual_review/已核实案例.json');manifest=read(DEFAULT_INPUT/'manifest.json')
    assert v['status']=='passed' and b['book_count']==h['book_count']==24
    books=[build_book(book) for book in manifest['books']]
    confirmed=[r for r in visual if r['outcome'].startswith('confirmed_')]
    conclusion={'schema_version':'corpus-paragraph-chapter-audit-conclusion-v1',
       'created_at':datetime.now(timezone.utc).isoformat(),'source_manifest_sha256':digest(DEFAULT_INPUT/'manifest.json'),
       'automatic_audit_complete':True,'full_pdf_visual_verification_complete':False,
       'corpus_acceptance':'not_passed_confirmed_counterexamples','human_verified':False,
       'book_count':24,'paragraph_occurrences':b['paragraph_occurrences'],'unique_texts_sum':b['unique_active_texts_sum'],
       'heading_count':h['heading_count'],'OCR_layout_pages':inventory['layout_totals']['pages'],
       'PDF_pages_actually_visually_reviewed_this_round':sum(len(x['evidence']) for x in visual),
       'confirmed_error_windows':len(confirmed),'boundary_status_counts':b['status_counts'],
       'section_status_counts':h['status_counts'],'chapter_status_counts':h['chapter_status_counts'],
       'cross_page_risk_candidates':b['cross_page_candidates'],
       'interpretation':['自动覆盖全部段落实例和章节节点，不等于全部自然段已经原页确认。',
                         '出现实例包含章节字段副本；风险数量不是独立物理错误数或错误率。',
                         '原PDF视觉确认了5个新的错误窗口，足以否定当前全库验收通过。',
                         '规则候选及未决项仍须处理；本轮未自动改写v3。'],
       'books':books}
    dump(BASE/'全面核验结论.json',conclusion)
    body='<div class="banner"><span class="badge">全量自动核查已完成 · 原页全面验收尚未完成</span><h1>当前 v3 验收未通过</h1><p>原页复核确认了新的跨页拆段、多个自然段粘连和跨章错挂。结构连通与来源可追溯已经通过，并不代表自然段与章节语义都正确。</p><strong>本页不会把“规则未报错”标为“原书已核验”。</strong></div>'
    body+=f'<div class="cards"><div class="card"><strong>24 / 24</strong>书籍自动核查覆盖</div><div class="card"><strong>{b["paragraph_occurrences"]:,}</strong>段落出现实例</div><div class="card"><strong>{h["heading_count"]:,}</strong>章节 / 小节节点</div><div class="card"><strong>{len(confirmed)}</strong>新确认错误窗口</div></div>'
    body+=f'<div class="panel"><h3>核查覆盖与证据边界</h3><p>读取了 {inventory["totals"]["middle"]} 份OCR版面文件，覆盖 {inventory["layout_totals"]["pages"]:,} 页；每个段落实例均有边界与归属结论。逐书去重文本总数为 {b["unique_active_texts_sum"]:,}。</p><p><strong>本轮实际看过的PDF原页为{sum(len(x["evidence"]) for x in visual)}页，组成{len(visual)}个诊断窗口，覆盖{len(set(x["book_id"] for x in visual))}本书。</strong>其中{len(confirmed)}个窗口确认错误，其余用于识别误报和公式情形；这是有目的诊断，不是随机准确率评估。OCR正文与版面来自同一上游，二者一致也可能共同出错。</p><p>代码与公式等特殊内容单列；同文多处、标题缺失及无法对齐内容保留未决。父子章节字段副本计作多个出现实例，不能当成多份独立原书证据。</p></div>'
    body+='<h2>自然段边界：全量自动结果</h2>'+count_table(b['status_counts'])
    body+='<h2>所属章：全量自动结果</h2>'+count_table(h['chapter_status_counts'])
    body+='<details><summary>展开小节直接归属与祖先覆盖结果</summary>'+count_table(h['status_counts'])+'</details>'
    body+=f'<p class="muted">另列 {b["cross_page_candidates"]:,} 对跨页风险候选。它们可能包含公式、图表或版面切换，未经原页复核不能直接合并。</p>'
    body+='<h2>已查看的原页与结论</h2>'
    for r in visual:
        flag='确认错误' if r['outcome'].startswith('confirmed_') else '规则对照 / 不能直接改动'
        body+=f'<details><summary>{esc(r["case_id"])} · {esc(r["book_title"])} · {flag}</summary><p>{esc(r["finding"])}</p><p class="muted">AI原页视觉复核；未代填人工标签。下方列出同文相关实例，其中可能同时包含正确归属和错误归属副本。</p><div class="images">'
        for e in r['evidence']:
            image=ROOT/e['image_path'];pdf=ROOT/e['pdf_path']
            body+=f'<div><p>{link(pdf,"PDF页 "+str(e["global_pdf_page_1based"]))}（分卷内第{e["local_pdf_page_1based"]}页）</p><a href="{href(image)}"><img loading="lazy" src="{href(image)}" alt="{esc(r["case_id"])} PDF {e["global_pdf_page_1based"]}"></a></div>'
        body+='</div>'
        for t in r['text_examples']:body+=f'<div class="evidence">{esc(t["text"][:1600])}</div>'
        body+='<details><summary>相关节点及当前章节路径</summary>'+''.join(f'<p>{esc(" / ".join(n["title_path"]))}<br><code>{esc(n["node_id"])}</code></p>' for n in r['related_occurrences'])+'</details></details>'
    body+='<h2>逐书结果与完整清单</h2><input id="filter" placeholder="输入书名筛选"><div class="scroll"><table id="books"><tr><th>书籍</th><th>段落实例</th><th>边界风险候选</th><th>所属章冲突候选</th><th>跨页候选对</th></tr>'
    for r in books:body+=f'<tr data-book="{esc(r["book_title"])}"><td>{link(BASE/r["report_path"],r["book_title"])}</td><td>{r["paragraph_occurrences"]:,}</td><td>{r["boundary_status_counts"].get("boundary_review_required",0):,}</td><td>{r["chapter_status_counts"].get("chapter_conflict_candidate",0):,}</td><td>{r["cross_page_candidates"]:,}</td></tr>'
    body+='</table></div><script>document.getElementById("filter").addEventListener("input",function(){let q=this.value.toLowerCase();document.querySelectorAll("tr[data-book]").forEach(r=>r.hidden=!r.dataset.book.toLowerCase().includes(q));});</script>'
    body+='<h2>可复核文件</h2><p>'+' · '.join([link(BASE/'全面核验结论.json','汇总结论JSON'),link(BASE/'独立完整性验收.json','核验输出的独立完整性检查'),link(BASE/'inputs_inventory/layout_inputs_inventory.json','24本原始版面清单'),link(BASE/'visual_review/已核实案例.json','原页复核案例'),link(BASE/'测试记录.txt','程序测试记录')])+'</p>'
    body+='<div class="panel"><h3>下一阶段的验收条件</h3><p>先恢复物理页上的段落与章节位置，再重建章节到段落的直接包含关系；父章节需要汇总正文时使用祖先覆盖关系。对跨页、框内列表、公式和代码分别处理。只有风险与未决项按原页关闭后，才能把“全部自然段及章节归属已核验”设为真。</p><p>本轮保留v3、原文和人工标签。新结果是完整的检查账本与未通过结论，不是已修复的新版本。</p></div><footer>审计对象：v3；全量自动核查与PDF诊断结果分别保存。页面由实际输出生成。</footer>'
    (BASE/'开始查看.html').write_text(page('24本文本图全面核验结果',body),encoding='utf-8')
    (BASE/'README.md').write_text('''# 24 本文本图核验\n\n结论：v3 尚未通过自然段边界与章节归属验收。全量自动核查已覆盖所有节点；完整原页逐段核验尚未完成。\n\n打开 `开始查看.html` 查看各书统计、确认错误、误报控制及完整数据。\n\n- `boundary_audit/<book_id>/paragraph_audit.jsonl`：每个段落实例的边界结论。\n- `heading_audit/<book_id>/paragraph_ownership.jsonl`：每个实例的所属章、所属小节结论。\n- `heading_audit/<book_id>/heading_anchors.jsonl`：每个标题的原OCR锚点。\n- `boundary_audit/<book_id>/text_alignment.jsonl`：每种正文文本的全部已记录定位候选。\n- `source_blocks.jsonl`：原OCR行框证据及坐标。\n- `visual_review/已核实案例.json`：真正看过原PDF后的诊断，非人工金标。\n- `独立完整性验收.json`：验证所有节点均被覆盖、定位字符串可重建、v3和原文未修改；不代表内容质量通过。\n\nOCR版面与正文来自同一上游，自动一致不能证明原书正确。数量以出现实例计，含父子字段副本；风险数不等于独立错误数或错误率。未根据规则批量猜改原文。\n''',encoding='utf-8')
    current=ROOT/'研究方案_20261002/24本文本图_当前版本.json';state=read(current)
    state['full_corpus_chapter_ownership_verified']=False
    state['latest_corpus_audit']={'conclusion_path':(BASE/'全面核验结论.json').relative_to(ROOT).as_posix(),
       'report_path':(BASE/'开始查看.html').relative_to(ROOT).as_posix(),
       'automatic_coverage_complete':True,'full_pdf_visual_verification_complete':False,
       'acceptance':'not_passed_confirmed_counterexamples','confirmed_error_windows':len(confirmed)}
    dump(current,state)
    print(json.dumps({k:v for k,v in conclusion.items() if k not in ('books','interpretation')},ensure_ascii=False))

if __name__=='__main__':main()
