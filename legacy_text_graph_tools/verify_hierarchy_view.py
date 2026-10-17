# -*- coding: utf-8 -*-
"""Independent verification of the hierarchy projection and full paragraph preservation."""
import collections,hashlib,json,pathlib
ROOT=pathlib.Path(__file__).resolve().parents[2];OUT=ROOT/'研究方案_20261002/24本文本图_物理段落v4_20261003'
def read(p):return json.loads(p.read_text(encoding='utf8'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    m=read(OUT/'层级数据清单.json');results=[];errors=[];total=0;pending=0;parenttypes=collections.Counter()
    def check(ok,message):
        if not ok:errors.append(message)
    check(len(m['books'])==24,'book_count')
    for b in m['books']:
        bid=b['book_id'];g=read(OUT/b['path']);original=read(OUT/bid/'text_graph.json');lookup={n['id']:n for n in g['nodes']};pars={n['id']:n for n in g['nodes'] if n['type']=='paragraph'};old={n['id']:n for n in original['nodes'] if n['type']=='paragraph'}
        check(sha(OUT/b['path'])==b['sha256'],bid+':hierarchy_hash')
        check(sha(OUT/bid/'text_graph.json')==g['source_graph']['sha256'],bid+':source_hash')
        check(len(lookup)==len(g['nodes']),bid+':duplicate_node')
        check(set(pars)==set(old),bid+':paragraphs_dropped_or_added')
        check(lookup[bid]['type']=='book' and lookup[bid]['parent_id'] is None,bid+':book_root')
        indegree=collections.Counter();visited=set();stack=[bid]
        while stack:
            nid=stack.pop();check(nid not in visited,bid+':repeated_child_or_cycle:'+nid)
            if nid in visited:continue
            visited.add(nid);n=lookup[nid]
            for child in n['children']:
                indegree[child]+=1;check(child in lookup,bid+':missing_child')
                if child in lookup:check(lookup[child]['parent_id']==nid,bid+':parent_mismatch');stack.append(child)
        check(visited==set(lookup),bid+':unreachable_nodes')
        check(all(indegree[nid]==1 for nid in lookup if nid!=bid),bid+':not_single_parent')
        expected={(n['parent_id'],n['id']) for n in g['nodes'] if n['id']!=bid}
        actual={(e['source'],e['target']) for e in g['edges']}
        check(expected==actual and len(expected)==len(g['edges']),bid+':edge_mismatch')
        for nid,n in pars.items():
            check(not n['children'],bid+':paragraph_not_leaf')
            check(n['parent_id'] in lookup and lookup[n['parent_id']]['type'] in ('book','chapter','section','subsection'),bid+':paragraph_parent_type')
            for field in ('text','source_spans','quality_flags','ownership_status','boundary_status','human_verified'):
                check(n[field]==old[nid][field],bid+':content_or_quality_changed:'+field)
            check(n['path_ids'][0]==bid and n['path_ids'][-1]==nid,bid+':broken_path')
            for a,c in zip(n['path_ids'],n['path_ids'][1:]):check(lookup[c]['parent_id']==a,bid+':path_parent_mismatch')
            parenttypes[lookup[n['parent_id']]['type']]+=1
            pending+=not old[nid]['retrieval_eligible']
        total+=len(pars);results.append({'book_id':bid,'paragraphs':len(pars),'reachable_nodes':len(visited),'single_parent_and_leaf_check':'passed'})
    check(total==118213,'full_paragraph_count');check(pending==71358,'all_pending_paragraphs_preserved')
    # A genuine numbered subsection must retain its full chain, rather than flatten into the chapter.
    c=read(OUT/'book_ab313ee3c3dd/hierarchy_graph.json');lookup={n['id']:n for n in c['nodes']}
    representative=next((n for n in c['nodes'] if n['type']=='paragraph' and [lookup[x]['type'] for x in n['path_ids']]==['book','chapter','section','subsection','paragraph']),None)
    check(representative is not None,'no_expected_five_level_example')
    result={'status':'passed' if not errors else 'failed','scope':'structural hierarchy and lossless paragraph preservation, not semantic boundary/ownership certification','paragraphs':total,'pending_paragraphs_included':pending,'paragraphs_by_actual_parent_type':dict(parenttypes),'books':results,'example_path':[{'id':x,'type':lookup[x]['type'],'label':lookup[x].get('label',lookup[x].get('text','')[:100])} for x in representative['path_ids']] if representative else [],'errors':errors,'manifest_sha256':sha(OUT/'层级数据清单.json'),'verifier_sha256':sha(pathlib.Path(__file__)),'human_verified':False,'full_semantic_verification':False}
    (OUT/'层级完整性验收.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('books','example_path')},ensure_ascii=False))
    if errors:raise SystemExit(1)
if __name__=='__main__':main()
