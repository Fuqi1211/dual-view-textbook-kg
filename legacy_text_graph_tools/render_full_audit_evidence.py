"""Render fixed diagnostic PDF pages; rendering is not a visual-verification claim."""
import json
from pathlib import Path
import re
import subprocess
from audit_paragraph_boundaries import ROOT,DEFAULT_INPUT,dump,digest

OUT=ROOT/'研究方案_20261002/文本图全面核验_20261002/visual_review'
SPECS=[('V001','book_bef9d155e6d8',[41,42],'cross_page_split'),
       ('V002','book_00849acee112',[25,26],'cross_page_split'),
       ('V003','book_00849acee112',[167],'merged_numbered_paragraphs'),
       ('V004','book_714c23228015',[58,59],'cross_page_candidate_with_formula'),
       ('V005','book_ab313ee3c3dd',[30,31],'wrong_chapter_candidate'),
       ('V006','book_20c6be06f4a3',[732,733],'wrong_chapter_candidate'),
       ('V007','book_bef9d155e6d8',[139],'internal_indent_candidate'),
       ('V008','book_bef9d155e6d8',[500],'short_chapter_title_control')]

def main():
    manifest=json.loads((DEFAULT_INPUT/'manifest.json').read_text('utf-8'))
    books={b['book_id']:b for b in manifest['books']};results=[]
    for cid,bid,pages,reason in SPECS:
        root=ROOT/Path(books[bid]['input_path']).parent
        folder=OUT/cid;folder.mkdir(parents=True,exist_ok=True);evidence=[]
        for page in pages:
            chunks=[]
            for chunk in (root/'chunks').iterdir():
                m=re.fullmatch(r'chunk_(\d+)_(\d+)',chunk.name)
                if m and int(m[1])<=page-1<=int(m[2]):chunks.append((chunk,int(m[1])))
            assert len(chunks)==1,(bid,page,chunks)
            chunk,start=chunks[0];pdfs=list(chunk.glob('**/*_origin.pdf'));assert len(pdfs)==1,pdfs
            pdf=pdfs[0];local=page-start;stem=folder/f'pdf_page_{page}'
            subprocess.run(['pdftoppm','-f',str(local),'-l',str(local),'-singlefile','-scale-to','2200','-png',str(pdf),str(stem)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
            image=stem.with_suffix('.png')
            evidence.append({'pdf_path':pdf.relative_to(ROOT).as_posix(),'pdf_sha256':digest(pdf),
                             'global_pdf_page_1based':page,'local_pdf_page_1based':local,
                             'image_path':image.relative_to(ROOT).as_posix(),'image_sha256':digest(image)})
        results.append({'case_id':cid,'book_id':bid,'book_title':books[bid]['book_title'],
                        'candidate_reason':reason,'evidence':evidence,'review_status':'rendered_pending_visual_review'})
    dump(OUT/'rendered_cases.json',results)
    print('Rendered',len(results),'cases;',sum(len(r['evidence']) for r in results),'pages')

if __name__=='__main__':main()
