# -*- coding: utf-8 -*-
"""Local-only, read-only original PDF page preview; accepts known book IDs and page numbers."""
import http.server,urllib.parse,re,pathlib,threading
import fitz
from extract_pdf_line_geometry import ROOT,OUT
from build_physical_graphs import read
BOOKS={b['book_id'] for b in read(OUT/'manifest.json')['books']};CACHE=OUT/'page_previews';CACHE.mkdir(exist_ok=True)
LOCK=threading.Lock()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q=urllib.parse.urlparse(self.path)
        if q.path=='/health':
            self.send_response(200);self.end_headers();self.wfile.write(b'pdf-page-preview-v1');return
        try:
            params=urllib.parse.parse_qs(q.query);bid=params['book'][0];page=int(params['page'][0])
            if q.path!='/page' or bid not in BOOKS or page<1:raise ValueError()
            selected=None
            for source in read(OUT/bid/'pdf_inputs.json'):
                match=re.search(r'/chunk_(\d+)_\d+/',source['path']);start=int(match.group(1)) if match else -1
                if start>=0 and start<page<=start+source['pages']:selected=(source,page-start);break
            if selected is None:raise ValueError()
            source,local=selected;target=CACHE/(source['sha256']+'_'+str(local)+'.jpg')
            with LOCK:
                if not target.exists():
                    with fitz.open(ROOT/source['path']) as d:
                        p=d[local-1];scale=1400/p.rect.width
                        pix=p.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False)
                        target.write_bytes(pix.tobytes('jpeg',jpg_quality=85))
            body=target.read_bytes();self.send_response(200);self.send_header('Content-Type','image/jpeg');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','public,max-age=86400');self.end_headers();self.wfile.write(body)
        except (KeyError,ValueError):self.send_error(404,'Known book and PDF page required')
        except Exception:self.send_error(500,'Page preview unavailable')
if __name__=='__main__':http.server.ThreadingHTTPServer(('127.0.0.1',8766),Handler).serve_forever()
