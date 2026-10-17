from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import build_concepts, build_structure, export_process, export_structure, inspect_run, prepare_concepts

ROUTES = {
    '/v1/structure/export': export_structure,
    '/v1/structure/build': build_structure,
    '/v1/concepts/prepare': prepare_concepts,
    '/v1/concepts/build': build_concepts,
    '/v1/runs/export-process': export_process,
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == '/health':
            self._send(200, dict(status='ok'))
        elif url.path == '/v1/runs/status':
            output = parse_qs(url.query).get('output_dir', [])
            if len(output) != 1:
                self._send(400, dict(error='output_dir is required exactly once'))
            else:
                self._execute(inspect_run, dict(output_dir=output[0]))
        else:
            self._send(404, dict(error='Unknown endpoint'))

    def do_POST(self):
        if self.headers.get('Origin'):
            self._send(403, dict(error='Browser-origin requests are not supported'))
            return
        function = ROUTES.get(urlsplit(self.path).path)
        if function is None:
            self._send(404, dict(error='Unknown endpoint'))
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 1048576:
                raise ValueError('JSON body must be 1..1048576 bytes')
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('JSON body must be an object')
            if 'runtime' in payload or 'projection' in payload:
                raise ValueError('runtime and projection are Python-only objects')
            if 'allow_api' in payload and not isinstance(payload['allow_api'], bool):
                raise ValueError('allow_api must be a JSON boolean')
        except (ValueError, UnicodeError) as exc:
            self._send(400, dict(error=str(exc)))
            return
        self._execute(function, payload)

    def _execute(self, function, payload):
        try:
            result = function(**payload)
        except FileExistsError:
            self._send(409, dict(error='Output already exists'))
        except (ValueError, TypeError, KeyError, FileNotFoundError) as exc:
            self._send(400, dict(error_type=type(exc).__name__, error=str(exc)))
        except RuntimeError as exc:
            if 'run lock' in str(exc):
                self._send(409, dict(error='Another builder holds this run lock'))
            else:
                self._send(500, dict(error_type=type(exc).__name__, error='Build failed; inspect the run directory'))
        except Exception as exc:
            self._send(500, dict(error_type=type(exc).__name__, error='Request failed; inspect the run directory'))
        else:
            self._send(200, result)


def create_server(*, port=8765):
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)
