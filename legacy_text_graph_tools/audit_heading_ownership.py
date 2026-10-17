"""Full-coverage chapter/section ownership audit against OCR heading positions.

Uses boundary source alignment, but independently constructs title-position
intervals. Ancestor field copies are distinguished from direct ownership.
Unknown/ambiguous intervening headings block positive conclusions.
"""
from __future__ import annotations
import argparse
import bisect
from collections import Counter,defaultdict
import json
from pathlib import Path
import re
import time
from audit_paragraph_boundaries import ROOT,DEFAULT_INPUT,digest,dump,typographic_normalize

BASE=ROOT/'研究方案_20261002/文本图全面核验_20261002'


def key(text):
    return typographic_normalize(re.sub(r'^\s*#{1,6}\s*','',str(text or '')))


def is_structured_title(text):
    return bool(re.match(r'^(?:第[0-9一二三四五六七八九十百]+[章节篇部]|\d+(?:\.\d+)+|附录|Chapter\s+\d+)',text,re.I))


class HeadingIndex:
    def __init__(self, headings, rows, parents):
        self.headings={h['id']:h for h in headings};self.parents=parents
        self.anchors={};self.events={};by_text=defaultdict(list)
        # Only OCR-declared headings; arbitrary repeated body mentions are not anchors.
        for i,r in enumerate(rows):
            if r.get('type')=='text' and r.get('text_level') and isinstance(r.get('text'),str):
                by_text[key(r['text'])].append(i)
                self.events[i]={'status':'unmapped_ocr_title','row_index':i,'text':r['text'],
                                'page_pdf':r['page_idx']+1 if isinstance(r.get('page_idx'),int) else None}
        position_to_ids=defaultdict(list)
        for h in headings:
            labels={key(h.get('label')),key(str(h.get('marker') or '')+' '+str(h.get('title') or ''))}-{''}
            exact=sorted({i for label in labels for i in by_text.get(label,[])})
            basis='complete_label'
            # A title-only fallback remains exact. Short/generic title fragments are not accepted.
            minimum_title_length=2 if h['type']=='chapter' else 4
            if not exact and len(key(h.get('title')))>=minimum_title_length:
                exact=list(by_text.get(key(h['title']),[]));basis='full_title_only'
            status='unique_ocr_heading' if len(exact)==1 else ('ambiguous_ocr_heading' if exact else 'heading_not_located')
            a={'heading_id':h['id'],'label':h.get('label'),'status':status,
               'candidate_merged_indices':exact,'basis':basis,'pdf_visual_verified':False,
               'declared_pdf_range':[h.get('pdf_page_start'),h.get('pdf_page_end')]}
            self.anchors[h['id']]=a
            if len(exact)==1:position_to_ids[exact[0]].append(h['id'])
        for i,ids in position_to_ids.items():
            if len(ids)==1:
                self.events[i].update(status='mapped_ocr_title',heading_id=ids[0])
            else:
                self.events[i].update(status='ambiguous_graph_heading',heading_ids=ids)
                for hid in ids:self.anchors[hid]['status']='ambiguous_graph_heading'
        self.positions=sorted(self.events)
        self.chapter_events={i:e for i,e in self.events.items()
                             if (e.get('heading_id') and self.headings[e['heading_id']]['type']=='chapter')
                             or re.match(r'^\s*(?:第[0-9一二三四五六七八九十百]+章|Chapter\s+\d+|附录)',e.get('text',''),re.I)}
        self.chapter_positions=sorted(self.chapter_events)

    def ancestors(self,hid):
        result=set();current=self.parents.get(hid)
        while current and current not in result:
            result.add(current);current=self.parents.get(current)
        return result

    def owner_at(self,row):
        at=bisect.bisect_right(self.positions,row)-1
        if at<0:return {'status':'no_preceding_ocr_title'}
        return self.events[self.positions[at]]

    def chapter_of(self,hid):
        seen=set()
        while hid and hid not in seen:
            seen.add(hid)
            if self.headings.get(hid,{}).get('type')=='chapter':return hid
            hid=self.parents.get(hid)
        return None

    def evaluate_chapter(self,current,rows):
        expected=self.chapter_of(current);owners=[]
        for row in rows:
            at=bisect.bisect_right(self.chapter_positions,row)-1
            if at<0:return {'status':'unresolved_chapter_evidence','chapter_ids':[]}
            event=self.chapter_events[self.chapter_positions[at]]
            if event['status']!='mapped_ocr_title':return {'status':'unresolved_chapter_evidence','chapter_ids':[]}
            owners.append(self.chapter_of(event['heading_id']))
        owners=sorted(set(o for o in owners if o))
        if not expected or not owners:status='unresolved_chapter_evidence'
        elif len(owners)>1:status='crosses_chapter_boundary_candidate'
        elif owners[0]==expected:status='ocr_chapter_supported'
        else:status='chapter_conflict_candidate'
        return {'status':status,'chapter_ids':owners}

    def evaluate(self,current,rows):
        events=[self.owner_at(row) for row in rows]
        unresolved=[e for e in events if e['status']!='mapped_ocr_title']
        owners=sorted({e['heading_id'] for e in events if e['status']=='mapped_ocr_title'})
        if unresolved:
            return {'status':'unresolved_heading_evidence','observed_owner_ids':owners,
                    'reasons':sorted({e['status'] for e in unresolved}),
                    'barrier_merged_indices':sorted({e['row_index'] for e in unresolved if 'row_index' in e})}
        if len(owners)>1:
            return {'status':'crosses_heading_boundary_candidate','observed_owner_ids':owners,'reasons':['multiple_distinct_heading_intervals']}
        if not owners:
            return {'status':'unresolved_heading_evidence','observed_owner_ids':[],'reasons':['no_source_position']}
        owner=owners[0]
        if owner==current:
            status='ocr_direct_owner_supported'; reasons=[]
        elif current in self.ancestors(owner):
            status='ocr_ancestor_coverage_supported'; reasons=['ancestor_field_copy_not_direct_owner']
        elif owner in self.ancestors(current):
            status='unresolved_heading_evidence';reasons=['current_subsection_not_anchored_in_OCR']
        else:
            status='ownership_conflict_candidate';reasons=['current_heading_differs_from_OCR_interval']
        return {'status':status,'observed_owner_ids':owners,'reasons':reasons}


def audit_book(book,input_dir,boundary_dir,output_dir):
    started=time.time();bid=book['book_id'];out=output_dir/bid;out.mkdir(parents=True,exist_ok=True)
    gp=input_dir/bid/'text_graph.json';graph=json.loads(gp.read_text('utf-8'))
    paragraphs=[n for n in graph['nodes'] if n['type']=='paragraph']
    headings=[n for n in graph['nodes'] if n['type'] in ('chapter','section')]
    parents={e['target']:e['source'] for e in graph['edges'] if e['type']=='contains'}
    source=ROOT/Path(book['input_path']).parent/'merged_content_list.json'
    rows=json.loads(source.read_text('utf-8-sig'));idx=HeadingIndex(headings,rows,parents)
    align_path=boundary_dir/bid/'text_alignment.jsonl'
    align={x['canonical_text_id']:x for x in (json.loads(l) for l in align_path.open(encoding='utf-8'))}
    counts, reasons, range_counts,chapter_counts=Counter(),Counter(),Counter(),Counter();examples=[]
    by_id={h['id']:h for h in headings}
    with (out/'paragraph_ownership.jsonl').open('w',encoding='utf-8') as f:
        for n in paragraphs:
            a=align[n['canonical_text_id']]
            results=[idx.evaluate(n['heading_id'],m['row_indices']) for m in a['matches']]
            if not results:
                result={'status':'unresolved_source_alignment','observed_owner_ids':[],
                        'reasons':['no_exact_source_location']}
            elif a['match_candidates_truncated']:
                result={'status':'unresolved_physical_occurrence','observed_owner_ids':[],
                        'reasons':['source_matches_exceed_recorded_candidate_cap']}
            elif len({(r['status'],tuple(r['observed_owner_ids'])) for r in results})>1:
                result={'status':'unresolved_physical_occurrence',
                        'observed_owner_ids':sorted({h for r in results for h in r['observed_owner_ids']}),
                        'reasons':['source_candidates_have_different_heading_results']}
            else:result=results[0]
            chapter_results=[idx.evaluate_chapter(n['heading_id'],m['row_indices']) for m in a['matches']]
            if not chapter_results:chapter={'status':'unresolved_source_alignment','chapter_ids':[]}
            elif a['match_candidates_truncated'] or len({(r['status'],tuple(r['chapter_ids'])) for r in chapter_results})>1:
                chapter={'status':'unresolved_physical_occurrence','chapter_ids':sorted({h for r in chapter_results for h in r['chapter_ids']})}
            else:chapter=chapter_results[0]
            pages=sorted({p for m in a['matches'] for p in m['pages_pdf']})
            h=by_id[n['heading_id']];start,end=h.get('pdf_page_start'),h.get('pdf_page_end')
            if pages and isinstance(start,int) and isinstance(end,int):
                range_status='all_candidate_pages_outside_declared_range' if all(p<start or p>end for p in pages) else ('some_candidate_pages_outside_declared_range' if any(p<start or p>end for p in pages) else 'candidate_pages_within_declared_range')
            else:range_status='range_not_comparable'
            row={'book_id':bid,'node_id':n['id'],'canonical_text_id':n['canonical_text_id'],
                 'heading_id':n['heading_id'],**result,'candidate_pages_pdf':pages,
                 'chapter_status':chapter['status'],'observed_chapter_ids':chapter['chapter_ids'],
                 'page_range_check':range_status,'source_match_count':len(a['matches']),
                 'pdf_visual_verified':False,'human_verified':False}
            f.write(json.dumps(row,ensure_ascii=False)+'\n')
            counts[row['status']]+=1;reasons.update(row['reasons']);range_counts[range_status]+=1;chapter_counts[chapter['status']]+=1
            if row['status'] in ('ownership_conflict_candidate','crosses_heading_boundary_candidate') and len(examples)<300:
                examples.append(row)
    with (out/'heading_anchors.jsonl').open('w',encoding='utf-8') as f:
        for a in idx.anchors.values():f.write(json.dumps(a,ensure_ascii=False)+'\n')
    dump(out/'ocr_title_events.json',list(idx.events.values()))
    dump(out/'conflict_examples.json',examples)
    hc=Counter(a['status'] for a in idx.anchors.values())
    summary={'book_id':bid,'book_title':book['book_title'],'input_graph_sha256':digest(gp),
             'source_alignment_sha256':digest(align_path),'merged_source_sha256':digest(source),
             'paragraph_occurrences':len(paragraphs),'audited_occurrences':sum(counts.values()),
             'heading_count':len(headings),'heading_anchor_status_counts':dict(hc),
             'status_counts':dict(counts),'reason_counts':dict(reasons),'page_range_counts':dict(range_counts),
             'chapter_status_counts':dict(chapter_counts),
             'coverage_complete':len(idx.anchors)==len(headings) and sum(counts.values())==len(paragraphs),
             'pdf_semantic_ownership_verified':False,'elapsed_seconds':round(time.time()-started,2)}
    summary['artifacts']={p.name:digest(p) for p in out.iterdir() if p.is_file() and p.name!='summary.json'}
    dump(out/'summary.json',summary);return summary


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',type=Path,default=DEFAULT_INPUT)
    ap.add_argument('--boundary',type=Path,default=BASE/'boundary_audit')
    ap.add_argument('--output',type=Path,default=BASE/'heading_audit');ap.add_argument('--book-id')
    args=ap.parse_args();manifest=json.loads((args.input/'manifest.json').read_text('utf-8'))
    books=[b for b in manifest['books'] if not args.book_id or args.book_id==b['book_id']]
    results=[];counts=Counter();hc=Counter();ranges=Counter();chapters=Counter()
    for b in books:
        s=audit_book(b,args.input,args.boundary,args.output);results.append(s)
        counts.update(s['status_counts']);hc.update(s['heading_anchor_status_counts']);ranges.update(s['page_range_counts'])
        chapters.update(s['chapter_status_counts'])
        print(json.dumps({k:s[k] for k in ('book_id','heading_count','status_counts','elapsed_seconds')},ensure_ascii=False),flush=True)
    result={'schema_version':'full-coverage-heading-evidence-audit-v1',
            'input_manifest_sha256':digest(args.input/'manifest.json'),'script_sha256':digest(Path(__file__)),
            'book_count':len(results),'paragraph_occurrences':sum(s['paragraph_occurrences'] for s in results),
            'heading_count':sum(s['heading_count'] for s in results),'status_counts':dict(counts),
            'heading_anchor_status_counts':dict(hc),'page_range_counts':dict(ranges),
            'chapter_status_counts':dict(chapters),
            'coverage_complete':len(results)==len(books) and all(s['coverage_complete'] for s in results),
            'pdf_semantic_ownership_verified':False,'human_verified':False,
            'limits':['Uses independently indexed OCR headings, not independent OCR extraction.',
                      'Unmapped or ambiguous OCR titles block supported ownership conclusions.',
                      'Inherited page ranges never resolve source-location ambiguity.',
                      'Ancestor coverage differs from direct section ownership.',
                      'Conflict candidates require PDF review before repair.'],
            'books':results}
    dump(args.output/'summary.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='books'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
