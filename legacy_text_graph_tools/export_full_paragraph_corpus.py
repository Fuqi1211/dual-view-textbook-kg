# -*- coding: utf-8 -*-
"""Export every v4 prose candidate, retaining uncertainty instead of filtering it out."""
import hashlib,json,pathlib
ROOT=pathlib.Path(__file__).resolve().parents[2]
OUT=ROOT/'研究方案_20261002/24本文本图_物理段落v4_20261003'
def read(p):return json.loads(p.read_text(encoding='utf8'))
def sha(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()
def main():
    m=read(OUT/'manifest.json');books=[];expected=set();pending=0
    target=OUT/'全量正文段落.jsonl'
    with target.open('w',encoding='utf8') as f:
        for b in m['books']:
            g=read(OUT/b['book_id']/'text_graph.json');count=0
            for n in g['nodes']:
                if n['type']!='paragraph':continue
                assert n['id'] not in expected
                expected.add(n['id']);count+=1;pending+=not n['retrieval_eligible']
                row={**n,'book_id':b['book_id'],'book_title':b['book_title'],'included_in_full_corpus_index':True,'quality_status':'automatic_candidate' if n['retrieval_eligible'] else 'review_pending'}
                f.write(json.dumps(row,ensure_ascii=False,separators=(',',':'))+'\n')
            assert count==b['paragraph_candidates']
            books.append({'book_id':b['book_id'],'paragraph_count':count})
    observed=set();reopened_pending=0
    with target.open(encoding='utf8') as f:
        for line in f:
            n=json.loads(line);assert n['id'] not in observed;observed.add(n['id'])
            assert n['included_in_full_corpus_index'] and not n['human_verified']
            reopened_pending+=n['quality_status']=='review_pending'
    assert observed==expected and len(expected)==m['summary']['paragraph_candidates']
    assert pending==reopened_pending==m['summary']['pending_paragraphs']
    for b in m['books']:
        graph=next(a for a in b['artifacts'] if a['path'].endswith('/text_graph.json'))
        assert sha(ROOT/graph['path'])==graph['sha256']
    export={'schema_version':'full-paragraph-corpus-v1','file':target.name,'sha256':sha(target),'paragraph_count':len(expected),'review_pending_included':pending,'exclusion_count_among_paragraph_nodes':0,'books':books,'verification':'all_v4_paragraph_ids_included_exactly_once; original_graph_hashes_unchanged','field_note':'retrieval_eligible retains its original conservative-index meaning; included_in_full_corpus_index governs this full export. No pending label has been cleared.','scope':'All paragraph-type nodes. Code, formulas, figures, tables, bibliography and metadata remain in each complete text_graph.json.','human_verified':False}
    (OUT/'全量正文清单.json').write_text(json.dumps(export,ensure_ascii=False,indent=2),encoding='utf8')
    p=OUT/'查看教材.html';s=p.read_text(encoding='utf8')
    if 'value="paragraph"' not in s:
        s=s.replace('<select id="scope">','<select id="scope"><option value="paragraph">全部正文（含待核验）</option>',1)
        needle="scope==='all'||(scope==='eligible'?n.retrieval_eligible:(n.type==='paragraph'&&!n.retrieval_eligible))"
        assert needle in s
        s=s.replace(needle,"scope==='all'||(scope==='paragraph'?n.type==='paragraph':(scope==='eligible'?n.retrieval_eligible:(n.type==='paragraph'&&!n.retrieval_eligible)))",1)
    s=s.replace('默认检索候选','保守检索候选').replace('不进入默认候选索引','完整数据已包含；保守索引未纳入')
    p.write_text(s,encoding='utf8')
    p=OUT/'修复结果.html';s=p.read_text(encoding='utf8')
    s=s.replace('默认检索候选','保守检索候选').replace('默认候选索引','保守候选索引').replace('默认候选','保守候选')
    if '全量正文段落.jsonl' not in s:
        banner='<section><h2>完整内容入口：118,213 个正文段落全部保留</h2><p>71,358 个待核验段落也已纳入全量文件。待核验表示自动判断尚不确定，不等于内容错误或不能用于检索。逐本查看现默认显示全部正文；质量标记用于后续修复，不删除内容。用于论文实验语料与作为人工金标准是两回事。</p><p><a href="全量正文段落.jsonl" download>下载全量正文段落</a> · <a href="全量正文清单.json">完整性检查与格式说明</a></p><p>下面的“保守候选”只是另一种筛选模式，不代表项目全部可用内容。公式、代码、图表仍在每本完整文本图中。</p></section>'
        s=s.replace('<section class="notice">',banner+'<section class="notice">',1)
    p.write_text(s,encoding='utf8')
    p=OUT/'使用说明.txt';s=p.read_text(encoding='utf8')
    prefix='全量使用入口：全量正文段落.jsonl 包含 24 本的全部 118,213 个 paragraph 节点，其中包括 71,358 个待核验节点，不按质量状态过滤。\n逐本查看页面现默认显示全部正文。完整图中的公式、代码、图表等类型仍保留。\n全量文件的 included_in_full_corpus_index=true 表示已纳入；原 retrieval_eligible 仅表示是否满足此前保守索引规则，不能用它再次过滤全量文件。质量标记、来源和人工核实状态原样保留。\n\n'
    if not s.startswith('全量使用入口：'):p.write_text(prefix+s,encoding='utf8')
    print(json.dumps({'included':len(expected),'pending_included':pending,'lost':0,'graphs_unchanged':True},ensure_ascii=False))
if __name__=='__main__':main()
