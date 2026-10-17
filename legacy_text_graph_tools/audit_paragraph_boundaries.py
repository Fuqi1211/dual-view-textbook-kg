"""Exhaustive, read-only boundary evidence audit; OCR agreement is NOT PDF verification.

All active paragraph occurrences receive a verdict. Exact source matching preserves
punctuation and spelling, removes whitespace and documented Markdown escapes only.
Layout signals are risk candidates, never silently applied as repairs.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import statistics
import time

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / '研究方案_20261002/24本文本图_证据修复v3_20261002'
DEFAULT_OUTPUT = ROOT / '研究方案_20261002/文本图全面核验_20261002/boundary_audit'
NORMALIZATION = 'remove whitespace and escaped Markdown punctuation only; preserve spelling, Unicode and hyphens'
TYPOGRAPHIC_NORMALIZATION = NORMALIZATION + '; second pass maps fullwidth ，（）；：！？ to ASCII counterparts for location only'
MAX_MATCHES = 80


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def dump(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def normalize(text):
    return re.sub(r'\s+', '', re.sub(r'\\([\\`*_{}\[\]()#+.!|>\-])', r'\1', str(text or '')))


def typographic_normalize(text):
    return normalize(text).translate(str.maketrans('，（）；：！？', ',();:!?'))


def text_value(graph, node):
    texts = graph.get('_audit_text_lookup')
    if texts is None:
        source = graph['paragraph_texts']
        texts = {t['id']: t for t in source} if isinstance(source, list) else source
        graph['_audit_text_lookup'] = texts
    ref = node['text_ref']
    value = texts[ref['canonical_text_id']]['text']
    return value[ref.get('start', 0):ref.get('end', len(value))]


class SourceIndex:
    def __init__(self, rows, queries, normalizer=normalize):
        self.normalize = normalizer
        self.records, self.exact = [], defaultdict(list)
        self.starts, self.ends, self.stream_records = [], [], []
        stream, cursor = [], 0
        for ri, row in enumerate(rows):
            fields = [(k, row[k]) for k in ('text', 'code_body', 'table_body') if isinstance(row.get(k), str)]
            for k in ('image_caption', 'image_footnote', 'table_caption', 'table_footnote', 'code_caption', 'code_footnote', 'list_items'):
                if isinstance(row.get(k), list):
                    fields.extend((f'{k}/{j}', v) for j, v in enumerate(row[k]) if isinstance(v, str))
            for field, value in fields:
                q = self.normalize(value)
                if not q:
                    continue
                record = {'record_index': len(self.records), 'row_index': ri, 'field': field,
                          'type': row.get('type'), 'text_level': row.get('text_level'),
                          'page_idx': row.get('page_idx'), 'bbox': row.get('bbox'),
                          'normalized_text': q, 'text': value, 'layout': None, 'layout_risks': []}
                self.records.append(record)
                self.exact[q].append(record['record_index'])
                if row.get('type') not in ('header', 'footer', 'page_number'):
                    self.starts.append(cursor)
                    self.ends.append(cursor + len(q))
                    self.stream_records.append(record['record_index'])
                    cursor += len(q)
                    stream.append(q)
        self.stream = ''.join(stream)
        self.anchors = defaultdict(list)
        needed = {self.normalize(q)[:16] for q in queries if len(self.normalize(q)) >= 16}
        for at in range(max(0, len(self.stream) - 15)):
            key = self.stream[at:at+16]
            if key in needed:
                self.anchors[key].append(at)

    def locate(self, text):
        q = self.normalize(text)
        if not q:
            return []
        matches, seen = [], set()
        def add(indices, offsets, scope):
            key = (tuple(indices), tuple(offsets))
            if key in seen:
                return
            seen.add(key)
            matches.append({'record_indices': indices,
                            'row_indices': list(dict.fromkeys(self.records[i]['row_index'] for i in indices)),
                            'pages_pdf': list(dict.fromkeys(self.records[i]['page_idx'] + 1 for i in indices if type(self.records[i]['page_idx']) is int)),
                            'match_scope': scope, 'normalized_local_range': offsets})
        # Exact header/footer matches must remain visible, even though excluded from body stream.
        for i in self.exact.get(q, []):
            add([i], [0, len(q)], 'full_ocr_block')
        positions = self.anchors.get(q[:16], []) if len(q) >= 16 else None
        if positions is None:
            positions, at = [], self.stream.find(q)
            while at >= 0 and len(positions) <= MAX_MATCHES:
                positions.append(at)
                at = self.stream.find(q, at+1)
        for at in positions:
            if not self.stream.startswith(q, at):
                continue
            left = bisect.bisect_right(self.starts, at)-1
            right = bisect.bisect_left(self.ends, at+len(q))
            if left < 0 or right >= len(self.stream_records):
                continue
            indices = self.stream_records[left:right+1]
            scope = 'multiple_ocr_blocks' if left != right else ('full_ocr_block' if at == self.starts[left] and at+len(q) == self.ends[left] else 'within_ocr_block')
            add(indices, [at-self.starts[left], at+len(q)-self.starts[right]], scope)
            if len(matches) > MAX_MATCHES:
                break
        return matches


def block_lines(block):
    if block.get('lines'):
        return block['lines']
    result = []
    for child in block.get('blocks', []):
        result.extend(block_lines(child))
    return result


def line_content(line):
    return ''.join(str(s.get('content', '')) for s in line.get('spans', []))


def norm_box(box, size):
    return [box[0]/size[0]*1000, box[1]/size[1]*1000,
            box[2]/size[0]*1000, box[3]/size[1]*1000]


def layout_features(block, size):
    lines = [l for l in block_lines(block) if isinstance(l.get('bbox'), list) and line_content(l).strip()]
    if not lines:
        return {'line_count': 0, 'risks': []}
    boxes = [l['bbox'] for l in lines]
    heights = [b[3]-b[1] for b in boxes if b[3] > b[1]]
    em = statistics.median(heights) if heights else 10
    base = statistics.median(b[0] for b in boxes[1:]) if len(boxes)>1 else boxes[0][0]
    right = max(b[2] for b in boxes)
    risks, boundaries = [], []
    if block.get('type') == 'text' and len(lines)>2:
        for i in range(1,len(lines)):
            prev = line_content(lines[i-1]).strip()
            if boxes[i][0]-base > 1.2*em and right-boxes[i-1][2] > em and re.search(r'[。！？.!?][”’"\')）]?$', prev):
                boundaries.append(i)
        if boundaries:
            risks.append('internal_indented_paragraph_start')
    if block.get('type') == 'list':
        starts = [i for i,l in enumerate(lines) if l.get('is_list_start_line') is True]
        if len(starts)>1:
            risks.append('multiple_list_entries_in_one_block')
    else:
        starts = []
    if block.get('type') in ('title','header','footer','page_number','image','table','code','equation'):
        risks.append('layout_block_is_' + block['type'])
    return {'line_count': len(lines), 'block_type': block.get('type'),
            'first_line_bbox': norm_box(boxes[0],size), 'last_line_bbox': norm_box(boxes[-1],size),
            'first_line_text': line_content(lines[0])[:120], 'last_line_text': line_content(lines[-1])[-120:],
            'first_indent_em': round((boxes[0][0]-base)/em,3),
            'last_shortfall_em': round((right-boxes[-1][2])/em,3),
            'internal_candidate_line_indices': boundaries, 'list_start_line_indices': starts,
            'risks': risks}


def attach_layout(index, book_root):
    pages, files = defaultdict(list), []
    for path in sorted((book_root/'chunks').glob('**/*_middle.json')):
        chunk = next((p for p in path.parts if re.fullmatch(r'chunk_\d+_\d+',p)), None)
        if not chunk:
            continue
        start = int(chunk.split('_')[1])
        data = json.loads(path.read_text('utf-8-sig'))
        rel = path.relative_to(ROOT).as_posix()
        files.append({'path':rel,'sha256':digest(path)})
        for pi,page in enumerate(data.get('pdf_info', [])):
            local = page.get('page_idx',pi)
            size = page.get('page_size')
            if not size or min(size)<=0:
                continue
            for bi,block in enumerate(page.get('para_blocks', [])):
                if not isinstance(block.get('bbox'),list) or len(block['bbox'])!=4:
                    continue
                pages[start+local].append({'bbox':norm_box(block['bbox'],size),
                    'middle_path':rel,'json_pointer':f'/pdf_info/{pi}/para_blocks/{bi}',
                    'features':layout_features(block,size)})
    for record in index.records:
        bbox = record.get('bbox')
        if not isinstance(bbox,list) or len(bbox)!=4:
            continue
        candidates=[]
        for block in pages.get(record['page_idx'],[]):
            distance=max(abs(a-b) for a,b in zip(bbox,block['bbox']))
            if distance<=6:
                candidates.append((distance,block))
        candidates.sort(key=lambda x:x[0])
        if candidates and (len(candidates)==1 or candidates[1][0]-candidates[0][0]>1):
            distance,block=candidates[0]
            record['layout']={k:v for k,v in block.items() if k!='bbox'}
            record['layout']['bbox_max_error_per_1000']=round(distance,3)
            record['layout']['link_basis']='unique_near_identical_OCR_bbox; correlated upstream evidence'
            record['layout_risks']=list(block['features']['risks'])
    # Cross-page evidence candidates only. Never call automatic heuristic a confirmed split.
    page_rows=defaultdict(list)
    for r in index.records:
        if r['field']=='text' and r['type']=='text' and r.get('layout') and not r.get('text_level'):
            page_rows[r['page_idx']].append(r)
    pairs=[]
    for page,records in sorted(page_rows.items()):
        following=page_rows.get(page+1)
        if not following:
            continue
        a,b=records[-1],following[0]
        fa,fb=a['layout']['features'],b['layout']['features']
        if not fa.get('line_count') or not fb.get('line_count'):
            continue
        if a['bbox'][3]<680 or b['bbox'][1]>300 or abs(a['bbox'][0]-b['bbox'][0])>120:
            continue
        if re.search(r'[。！？.!?;；:：][”’"\')）]?$',a['text'].strip()):
            continue
        if re.match(r'\s*(?:\d+[.、．)]|[（(]\d+[）)]|[-•])',b['text']):
            continue
        if fb['first_indent_em']>0.9:
            continue
        if fa['last_shortfall_em']>2.5:
            continue
        pair={'left_record':a['record_index'],'right_record':b['record_index'],
              'left_merged_index':a['row_index'],'right_merged_index':b['row_index'],
              'pages_pdf':[page+1,page+2], 'status':'possible_cross_page_continuation',
              'evidence':'last line fills width without terminal punctuation; next page begins without indent',
              'pdf_visual_verified':False}
        pairs.append(pair)
        a['layout_risks'].append('possible_cross_page_continuation_after')
        b['layout_risks'].append('possible_cross_page_continuation_before')
    return files,pairs


def evaluate_match(match, records):
    reasons=[]
    selected=[records[i] for i in match['record_indices']]
    if match['match_scope']!='full_ocr_block':
        reasons.append(match['match_scope'])
    for r in selected:
        if r.get('text_level'):
            reasons.append('ocr_title_used_as_paragraph')
        if r['type'] in ('header','footer','page_number','page_footnote','aside_text') or 'caption' in r['field'] or 'footnote' in r['field']:
            reasons.append('non_body_source_role')
        reasons.extend(r.get('layout_risks',[]))
    if reasons:
        return 'boundary_review_required', sorted(set(reasons))
    if selected and all(r.get('layout') for r in selected):
        return 'ocr_layout_agreement_only', []
    return 'ocr_block_agreement_only', ['no_unique_line_layout_link']


def audit_book(book, input_dir, output_dir):
    started=time.time()
    bid=book['book_id']; out=output_dir/bid; out.mkdir(parents=True,exist_ok=True)
    gp=input_dir/bid/'text_graph.json'
    graph=json.loads(gp.read_text('utf-8'))
    paragraphs=[n for n in graph['nodes'] if n['type']=='paragraph']
    values={n['canonical_text_id']:text_value(graph,n) for n in paragraphs}
    bp=ROOT/Path(book['input_path']).parent
    merged=bp/'merged_content_list.json'
    rows=json.loads(merged.read_text('utf-8-sig'))
    index=SourceIndex(rows,values.values())
    files,pairs=attach_layout(index,bp)
    typographic=SourceIndex(rows,values.values(),typographic_normalize)
    for a,b in zip(typographic.records,index.records):
        assert a['row_index']==b['row_index'] and a['field']==b['field']
        a['layout']=b['layout'];a['layout_risks']=b['layout_risks']
    alignments={}; text_counts=Counter()
    with (out/'text_alignment.jsonl').open('w',encoding='utf-8') as f:
        for tid,value in values.items():
            matches=index.locate(value)
            normalization=NORMALIZATION
            if not matches:
                matches=typographic.locate(value)
                normalization=TYPOGRAPHIC_NORMALIZATION
            if not matches:
                status,reasons='unresolved_source_alignment',['no_exact_normalized_source_match']
            elif len(matches)>1:
                status,reasons='unresolved_physical_occurrence',['multiple_source_occurrences']
            else:
                status,reasons=evaluate_match(matches[0],index.records)
            a={'canonical_text_id':tid,'status':status,'reasons':reasons,
               'matches':matches,'match_candidates_truncated':len(matches)>MAX_MATCHES,
               'normalization':normalization,'pdf_visual_verified':False}
            alignments[tid]=a; text_counts[status]+=1
            f.write(json.dumps(a,ensure_ascii=False)+'\n')
    counts,kinds,risks=Counter(),Counter(),Counter()
    with (out/'paragraph_audit.jsonl').open('w',encoding='utf-8') as f:
        for n in paragraphs:
            a=alignments[n['canonical_text_id']]
            status,reasons=a['status'],list(a['reasons'])
            if n.get('content_kind') in ('fenced_code','math','table'):
                status='non_prose_unit_separate_policy'; reasons.append('typed_'+n['content_kind'])
            if any(x in n.get('quality_flags',[]) for x in ('unclosed_fence_review','unclosed_math_review')):
                status='boundary_review_required'; reasons.append('unclosed_protected_block')
            if len(values[n['canonical_text_id']])>4000:
                reasons.append('long_block_over_4000_chars')
                if status.endswith('_only'):
                    status='boundary_review_required'
            if re.search(r'^\s*#{1,6}\s+',values[n['canonical_text_id']]) and n.get('content_kind')=='text':
                reasons.append('markdown_heading_marker_in_body')
                status='boundary_review_required'
            row={'book_id':bid,'node_id':n['id'],'canonical_text_id':n['canonical_text_id'],
                 'heading_id':n['heading_id'],'content_kind':n.get('content_kind'),
                 'status':status,'reasons':sorted(set(reasons)),
                 'source_candidate_count':len(a['matches']),
                 'text_alignment_key':n['canonical_text_id'],
                 'existing_repair_id':n.get('repair_id'),
                 'pdf_visual_verified':False,'human_verified':False}
            f.write(json.dumps(row,ensure_ascii=False)+'\n')
            counts[status]+=1; kinds[n.get('content_kind','unknown')]+=1;risks.update(set(reasons))
    # Source records retain all candidate locations and line-layout references. No content discarded.
    with (out/'source_blocks.jsonl').open('w',encoding='utf-8') as f:
        for r in index.records:
            f.write(json.dumps({k:v for k,v in r.items() if k!='normalized_text'},ensure_ascii=False)+'\n')
    dump(out/'cross_page_candidates.json',pairs)
    dump(out/'evidence_inputs.json',files)
    summary={'book_id':bid,'book_title':book['book_title'],
       'input_graph_sha256':digest(gp),'merged_source_sha256':digest(merged),
       'paragraph_occurrences':len(paragraphs),'unique_active_texts':len(values),
       'audited_occurrences':sum(counts.values()),'all_occurrences_covered':sum(counts.values())==len(paragraphs),
       'status_counts':dict(counts),'unique_text_status_counts':dict(text_counts),
       'content_kind_counts':dict(kinds),'reason_counts':dict(risks),
       'source_block_records':len(index.records),'layout_linked_source_records':sum(bool(r['layout']) for r in index.records),
       'middle_files_read':len(files),'cross_page_candidates':len(pairs),
       'new_pdf_visually_verified_occurrences':0,'natural_paragraph_corpus_verified':False,
       'elapsed_seconds':round(time.time()-started,2)}
    summary['artifacts']={p.name:digest(p) for p in out.iterdir() if p.is_file() and p.name!='summary.json'}
    dump(out/'summary.json',summary)
    return summary


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--input',type=Path,default=DEFAULT_INPUT)
    ap.add_argument('--output',type=Path,default=DEFAULT_OUTPUT);ap.add_argument('--book-id')
    args=ap.parse_args(); manifest=json.loads((args.input/'manifest.json').read_text('utf-8'))
    books=[b for b in manifest['books'] if not args.book_id or b['book_id']==args.book_id]
    results=[]
    for b in books:
        s=audit_book(b,args.input,args.output);results.append(s)
        print(json.dumps({k:s[k] for k in ('book_id','paragraph_occurrences','status_counts','elapsed_seconds')},ensure_ascii=False),flush=True)
    counts=Counter(); kinds=Counter()
    for s in results:counts.update(s['status_counts']);kinds.update(s['content_kind_counts'])
    result={'schema_version':'full-coverage-boundary-evidence-audit-v1',
            'input_manifest_sha256':digest(args.input/'manifest.json'),'script_sha256':digest(Path(__file__)),
            'coverage_complete':len(results)==len(books) and all(s['all_occurrences_covered'] for s in results),
            'book_count':len(results),'paragraph_occurrences':sum(s['paragraph_occurrences'] for s in results),
            'unique_active_texts_sum':sum(s['unique_active_texts'] for s in results),
            'status_counts':dict(counts),'content_kind_counts':dict(kinds),
            'cross_page_candidates':sum(s['cross_page_candidates'] for s in results),
            'natural_paragraph_corpus_verified':False,'human_verified':False,'api_calls':0,
            'verdict':'exhaustive_evidence_audit_completed; automatic_agreement_is_not_full_PDF_verification',
            'normalization':NORMALIZATION,
            'limits':['OCR content and layout are correlated upstream evidence, not independent ground truth.',
                      'Every occurrence is audited; identical text locations remain ambiguous when not uniquely supported.',
                      'Heuristics identify review candidates, not confirmed errors.',
                      'Code, equations and tables are not certified as natural prose paragraphs.'],
            'books':results}
    dump(args.output/'summary.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='books'},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
