# -*- coding: utf-8 -*-
import collections,json,re
import fitz
from PIL import Image,ImageDraw,ImageFont
from build_layout_ownership_v6 import *

def main():
    cases=[]
    for bk in read(OUT/'manifest.json')['books']:
        bid=bk['book_id'];g=read(OUT/bid/'text_graph.json');ev={e['record_index']:e for e in lines(OUT/bid/'pdf_line_evidence.jsonl')};resolved={(x['record_index'],x['raw_offset']) for x in lines(OUT/bid/'cross_page_review.jsonl')};seen=set()
        for n in g['nodes']:
            if n['type']!='paragraph' or 'cross_page_geometry_ambiguous' not in n['quality_flags']:continue
            for s in n['source_spans']:
                for d in internal_decisions(ev[s['record_index']]):
                    a,b=d['previous_line'],d['current_line'];key=(s['record_index'],d['raw_offset'])
                    if d['decision']!='unknown' or a['pdf_page']==b['pdf_page'] or key in resolved or key in seen or d['raw_offset'] is None or not s['start']<d['raw_offset']<s['end']:continue
                    seen.add(key);cases.append({'case_id':len(cases)+1,'book_id':bid,'node_id':n['id'],'record_index':s['record_index'],'raw_offset':d['raw_offset'],'text':n['text'],'previous_line':a,'current_line':b,'old_basis':d['basis']})
    folder=OUT/'boundary_visual_review';folder.mkdir(exist_ok=True);jsonl(folder/'cases.jsonl',cases);font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',18);documents={};tiles=[]
    for c in cases:
        tile=Image.new('RGB',(1100,360),'white');draw=ImageDraw.Draw(tile);draw.text((8,4),f"#{c['case_id']}  {c['book_id']}  r{c['record_index']} @ {c['raw_offset']}  PDF {c['previous_line']['pdf_page']} -> {c['current_line']['pdf_page']}",font=font,fill='black')
        for j,l in enumerate([c['previous_line'],c['current_line']]):
            path=l['pdf_path']
            if path not in documents:documents[path]=fitz.open(ROOT/path)
            p=documents[path][l['local_pdf_page']-1];pw,ph=l['page_size'];box=l['ocr_bbox'];em=max(8,box[3]-box[1]);y0=max(0,box[1]-(2.4 if j==0 else .9)*em);y1=min(ph,box[3]+(.8 if j==0 else 2.4)*em)
            clip=fitz.Rect(0,y0/ph*p.rect.height,p.rect.width,y1/ph*p.rect.height);pix=p.get_pixmap(matrix=fitz.Matrix(1080/p.rect.width,1080/p.rect.width),clip=clip)
            im=Image.frombytes('RGB',[pix.width,pix.height],pix.samples);im.thumbnail((1080,150));tile.paste(im,(10,30+j*165));draw.line((8,190,1090,190),fill='#d44',width=2)
        tiles.append(tile)
    for start in range(0,len(tiles),6):
        sheet=Image.new('RGB',(2200,1080),'#ddd')
        for j,t in enumerate(tiles[start:start+6]):sheet.paste(t,((j%2)*1100,(j//2)*360))
        sheet.save(folder/f'sheet_{start//6+1:02}.jpg',quality=94)
    for d in documents.values():d.close()
    print(json.dumps({'cases':len(cases),'sheets':(len(cases)+5)//6},ensure_ascii=False))
if __name__=='__main__':main()
