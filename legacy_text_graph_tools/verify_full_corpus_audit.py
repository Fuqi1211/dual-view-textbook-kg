"""Independent audit-output validation. Does not import either audit implementation.
Checks complete node coverage, source candidate strings, raw data hashes and
unsupported verification claims. It does not certify OCR/PDF semantic correctness.
"""
import bisect
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[2]
INPUT=ROOT/'研究方案_20261002/24本文本图_证据修复v3_20261002'
BASE=ROOT/'研究方案_20261002/文本图全面核验_20261002'

def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1048576),b''):h.update(b)
    return h.hexdigest()

def read(path):return json.loads(path.read_text('utf-8-sig'))
def lines(path):return [json.loads(s) for s in path.open(encoding='utf-8') if s.strip()]
def require(ok,msg):
    if not ok:raise AssertionError(msg)
def norm(text,fold=False):
    s=re.sub(r'\s+','',re.sub(r'\\([\\`*_{}\[\]()#+.!|>\-])',r'\1',str(text or '')))
    return s.translate(str.maketrans('，（）；：！？',',();:!?')) if fold else s

def coverage(rows,expected,key):
    ids=[r[key] for r in rows]
    require(len(ids)==len(set(ids)),'duplicate audit row')
    require(set(ids)==set(expected),'missing or extra audit IDs')

def source_value(row,path):
    parts=path.split('/');value=row[parts[0]]
    if len(parts)>1:value=value[int(parts[1])]
    return value

def verify_book(book):
    bid=book['book_id'];gp=INPUT/bid/'text_graph.json';g=read(gp)
    # Re-check all frozen artifacts, including the retrieval index and repair records.
    for a in book['artifacts']:
        p=INPUT/a['path'];require(sha(p)==a['sha256'],'v3 artifact changed: '+a['path'])
    require(sha(ROOT/book['input_path'])==book['input_sha256'],'original source tree changed')
    ps={n['id']:n for n in g['nodes'] if n['type']=='paragraph'}
    hs={n['id']:n for n in g['nodes'] if n['type'] in ('chapter','section')}
    ts={t['id']:t['text'] for t in g['paragraph_texts']}
    ids={p['canonical_text_id'] for p in ps.values()}
    bd=BASE/'boundary_audit'/bid;hd=BASE/'heading_audit'/bid
    bs,os=read(bd/'summary.json'),read(hd/'summary.json')
    for folder,summary in ((bd,bs),(hd,os)):
        require(summary['input_graph_sha256']==sha(gp),'wrong audited graph version')
        for name,h in summary['artifacts'].items():require(sha(folder/name)==h,'audit artifact changed '+name)
    br=lines(bd/'paragraph_audit.jsonl');hr=lines(hd/'paragraph_ownership.jsonl')
    coverage(br,ps,'node_id');coverage(hr,ps,'node_id')
    ar=lines(bd/'text_alignment.jsonl');coverage(ar,ids,'canonical_text_id')
    anchors=lines(hd/'heading_anchors.jsonl');coverage(anchors,hs,'heading_id')
    for records in (br,hr):
        for row in records:
            p=ps[row['node_id']]
            require(row['canonical_text_id']==p['canonical_text_id'] and row['heading_id']==p['heading_id'],'wrong audited node identity')
            require(row['pdf_visual_verified'] is False and row['human_verified'] is False,'automatic result promoted to visual/human truth')
    require(dict(Counter(r['status'] for r in br))==bs['status_counts'],'boundary counts differ')
    require(dict(Counter(r['status'] for r in hr))==os['status_counts'],'ownership counts differ')
    require(dict(Counter(r['chapter_status'] for r in hr))==os['chapter_status_counts'],'chapter counts differ')
    require(sum(bs['status_counts'].values())==sum(os['status_counts'].values())==len(ps),'count total mismatch')
    source=Path(book['input_path']).with_name('merged_content_list.json');raw=read(ROOT/source)
    require(sha(ROOT/source)==bs['merged_source_sha256']==os['merged_source_sha256'],'OCR source changed')
    blocks=lines(bd/'source_blocks.jsonl')
    for i,b in enumerate(blocks):
        require(b['record_index']==i,'nonsequential source record')
        r=raw[b['row_index']]
        require(source_value(r,b['field'])==b['text'],'source record text differs from actual OCR')
        require(b['page_idx']==r.get('page_idx') and b['bbox']==r.get('bbox'),'source location mismatch')
    # Reconstruct every recorded candidate, independently of SourceIndex.
    candidate_count=0
    for a in ar:
        fold='second pass' in a['normalization'];q=norm(ts[a['canonical_text_id']],fold)
        require(a['pdf_visual_verified'] is False,'unjustified source match verification')
        for m in a['matches']:
            rs=[blocks[i] for i in m['record_indices']];start,end=m['normalized_local_range']
            strings=[norm(r['text'],fold) for r in rs]
            actual=strings[0][start:end] if len(rs)==1 else strings[0][start:]+''.join(strings[1:-1])+strings[-1][:end]
            require(actual==q,'candidate does not reconstruct queried text')
            require(m['row_indices']==list(dict.fromkeys(r['row_index'] for r in rs)),'candidate row mismatch')
            require(m['pages_pdf']==list(dict.fromkeys(r['page_idx']+1 for r in rs if isinstance(r['page_idx'],int))),'candidate page mismatch')
            if m['match_scope']=='full_ocr_block':require(len(rs)==1 and start==0 and end==len(strings[0]),'false full-block claim')
            candidate_count+=1
    # Verify every OCR title event and supported direct/ancestor assertion against source positions.
    events=read(hd/'ocr_title_events.json');positions=[e['row_index'] for e in events]
    require(set(positions)=={i for i,r in enumerate(raw) if r.get('type')=='text' and r.get('text_level') and isinstance(r.get('text'),str)},'OCR title omitted or invented')
    emap={e['row_index']:e for e in events};positions=sorted(positions)
    for e in events:require(raw[e['row_index']]['text']==e['text'],'title event text mismatch')
    parents={e['target']:e['source'] for e in g['edges'] if e['type']=='contains'}
    align={r['canonical_text_id']:r for r in ar}
    supported=0
    for r in hr:
        if r['status'] not in ('ocr_direct_owner_supported','ocr_ancestor_coverage_supported'):continue
        require(align[r['canonical_text_id']]['matches'],'supported without source')
        for m in align[r['canonical_text_id']]['matches']:
            for at in m['row_indices']:
                ei=bisect.bisect_right(positions,at)-1;require(ei>=0,'supported before first title')
                e=emap[positions[ei]];require(e['status']=='mapped_ocr_title','supported across unknown title barrier')
                owner=e['heading_id']
                if r['status']=='ocr_direct_owner_supported':require(owner==r['heading_id'],'false direct owner')
                else:
                    ancestors=set();v=parents.get(owner)
                    while v and v not in ancestors:ancestors.add(v);v=parents.get(v)
                    require(r['heading_id'] in ancestors,'false ancestor coverage')
        supported+=1
    # Verify frozen layout input hashes once per book.
    evidence_inputs=read(bd/'evidence_inputs.json')
    for e in evidence_inputs:require(sha(ROOT/e['path'])==e['sha256'],'middle layout source changed')
    return {'book_id':bid,'paragraphs':len(ps),'headings':len(hs),'text_alignments':len(ar),
            'reconstructed_source_candidates':candidate_count,'ownership_support_assertions_checked':supported,
            'source_blocks_checked':len(blocks),'layout_files_checked':len(evidence_inputs),'status':'passed'}

def self_tests():
    coverage([{'id':'a'}],['a'],'id')
    for rows in ([],[{'id':'a'},{'id':'a'}],[{'id':'b'}]):
        try:coverage(rows,['a'],'id')
        except AssertionError:continue
        raise AssertionError('negative coverage fixture accepted')
    require(norm('A-B')!=norm('AB'),'punctuation erased')
    require(norm('送人')!=norm('送入'),'OCR word difference erased')
    require(norm('你好，',True)==norm('你好,',True),'typographic matching broken')

def main():
    self_tests();manifest=read(INPUT/'manifest.json');results=[];errors=[]
    for book in manifest['books']:
        try:r=verify_book(book)
        except Exception as e:r={'book_id':book['book_id'],'status':'failed','error':str(e)};errors.append(r)
        results.append(r);print(json.dumps(r,ensure_ascii=False),flush=True)
    visual=read(BASE/'visual_review/已核实案例.json')
    for v in visual:
        require(v['human_verified'] is False,'AI visual review mislabeled human')
        for e in v['evidence']:
            require(sha(ROOT/e['pdf_path'])==e['pdf_sha256'],'visual PDF changed')
            require(sha(ROOT/e['image_path'])==e['image_sha256'],'visual render changed')
    out={'scope':'audit completeness and source consistency; not semantic correctness',
         'source_manifest_sha256':sha(INPUT/'manifest.json'),'verifier_sha256':sha(Path(__file__)),
         'status':'passed' if not errors and len(results)==24 else 'failed',
         'book_count':len(results),'errors':errors,'books':results,
         'paragraphs_checked':sum(r.get('paragraphs',0) for r in results),
         'headings_checked':sum(r.get('headings',0) for r in results),
         'automatic_audit_does_not_equal_full_visual_verification':True,
         'visual_diagnostic_cases':len(visual),'visual_pages':sum(len(v['evidence']) for v in visual),
         'source_v3_unchanged':not errors,'negative_fixture_tests':6}
    (BASE/'独立完整性验收.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in out.items() if k!='books'},ensure_ascii=False),flush=True)
    if errors:raise SystemExit(1)

if __name__=='__main__':main()
