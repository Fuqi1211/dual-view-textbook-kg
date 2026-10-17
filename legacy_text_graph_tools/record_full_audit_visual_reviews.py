"""Record the root assistant's actual inspection of 13 rendered PDF pages.
These are diagnostic counterexamples/controls, not a representative accuracy sample.
"""
import json
from pathlib import Path
from audit_paragraph_boundaries import ROOT,DEFAULT_INPUT,dump

BASE=ROOT/'研究方案_20261002/文本图全面核验_20261002'
REVIEWS={
 'V001':('confirmed_boundary_error','原书 PDF 41 页末“控制器和适配器之间的区”与 42 页首“别主要在于……”构成同一自然段。v3 仍保留两个正文节点。','merge_same_natural_paragraph',
 ['text:91c9c98bb354eb44fa63928c089f743097476db22d5a1762317f4a368b37e1cd','text:4a79d28bdbd927fe69af74b4e1a2c5722ad8e551960807bc14c0ea79db1fb64d']),
 'V002':('confirmed_boundary_error','原书 PDF 25 页末“大型数据中”与 26 页首“心每个都有10万台以上的服务器。”是同一自然段；v3 把续页部分当成独立段落。','merge_same_natural_paragraph',
 ['text:c382e15559ebdb8fa04e4d86a708269134dbd47b73b68d4f21ec2001cd711f7f','text:47c4d9280f186b0c255d9bacc6eae54e591dd54bcd3caa3fef0f9acd2f80fc99']),
 'V003':('confirmed_boundary_error','PDF 167 页图3-24框内有3个独立编号段落：1.从上层收到数据、2.超时、3.收到ACK。上游OCR把它们合为一个text框，v3 又保留为一个最小段落节点。','split_three_numbered_paragraphs',[]),
 'V004':('plain_merge_not_supported','PDF 58 页末和59页首之间有公式(2.35)，后文解释公式变量。不能把两个文字块直接拼接并跳过公式；应保留公式及其解释的连接。该规则候选不能升级为已确认的普通跨页拆段。','preserve_equation_context_pending_policy',
 ['text:610b98eafda5afe1f9d7bdf19e07f73a215bde8cafb14a523729f97d6484286c','text:f1da5d87e9fd40c46dba758a24ae7da8502c5492835f4262d52764133252441f']),
 'V005':('confirmed_ownership_error','PDF 30页是“第2章 开始学习C++”；31页页眉也是第2章。“如果已经使用过C语言进行编程……”正文位于该章“C语言输入和输出”框内，v3 中至少一份出现实例却直接挂在第1章“预备知识”下。不是合理祖先章节副本。','reparent_wrong_chapter_occurrences',[]),
 'V006':('confirmed_ownership_error','PDF 733页明示“第22章 自然语言处理”和“22.1 语言模型”。“形式语，如Java和Python等编程语……”正文在该节，v3 中一份出现实例却挂在“第2章 智能Agent / 2.2 好的行为：理性的概念 / 2.2.1 理性”。','reparent_wrong_chapter_occurrences',[]),
 'V007':('boundary_candidate_not_confirmed','PDF 139页练习答案2.32的末尾没有独立自然段“A”。OCR末尾杂字符及异常行框触发了内部缩进规则；这不是新自然段边界的可靠证据。图中另有“这个练习说明……”自然段，需按实际正文分别检查，不能在A前盲拆。','OCR_noise_requires_text_review',[]),
 'V008':('current_chapter_supported_control','PDF 500页章首清楚写第7章“链接”，该页正文属于第7章。初版标题匹配遗漏了短标题“链接”，错误沿用了上一章；已修正核验程序，未修改v3图。这个控制例用于防止把核验器误报当成数据错误。','no_graph_reparent_needed',[])
}

def main():
    rendered=json.loads((BASE/'visual_review/rendered_cases.json').read_text('utf-8'))
    graphs={};result=[]
    for case in rendered:
        cid,bid=case['case_id'],case['book_id']
        if bid not in graphs:graphs[bid]=json.loads((DEFAULT_INPUT/bid/'text_graph.json').read_text('utf-8'))
        g=graphs[bid];texts={x['id']:x['text'] for x in g['paragraph_texts']};nodes={x['id']:x for x in g['nodes']}
        outcome,finding,action,tids=REVIEWS[cid]
        if cid=='V003':tids=[tid for tid,text in texts.items() if text.startswith('1.从上层收到数据。当从上层接收到数据后，SR发送方检查')]
        if cid=='V005':tids=[nodes['paragraph:3dc9d3f5bbbf18624df3d7c294db86432ab46263caada798b197c533256b613b']['canonical_text_id']]
        if cid=='V006':tids=[nodes['paragraph:f154b4f1ee80eb3404803d9b0b8df16647962dec3e207573cfe181f55641a3e9']['canonical_text_id']]
        if cid=='V007':tids=[tid for tid,text in texts.items() if text.startswith('2.32 这个函数会给出正确的值')]
        if cid=='V008':tids=[nodes['paragraph:e420501b628709bb0e25a4710a45a901e3f7d98c690f22bda06923e1da967ae7']['canonical_text_id']]
        occurrences=[]
        for n in g['nodes']:
            if n['type']=='paragraph' and n['canonical_text_id'] in tids:
                occurrences.append({'node_id':n['id'],'canonical_text_id':n['canonical_text_id'],
                    'heading_id':n['heading_id'],'title_path':n['title_path'],'source_origin':n.get('source_origin'),
                    'field_start':n.get('field_start'),'field_end':n.get('field_end')})
        result.append({**case,'review_status':'visually_reviewed_by_root_assistant','outcome':outcome,
                       'finding':finding,'recommended_action':action,'reviewer':'root_assistant_PDF_visual_inspection',
                       'human_verified':False,'sample_design':'purposive_diagnostic_not_accuracy_sample',
                       'text_examples':[{'canonical_text_id':tid,'text':texts[tid]} for tid in tids],
                       'related_occurrences':occurrences,'graph_modified':False})
    dump(BASE/'visual_review/已核实案例.json',result)
    print('Saved',len(result),'visual reviews;',sum(r['outcome'].startswith('confirmed_') for r in result),'confirmed error windows')

if __name__=='__main__':main()
