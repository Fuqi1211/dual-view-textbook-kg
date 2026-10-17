from __future__ import annotations

import http.client
import json
import threading
import unittest
import tempfile
from pathlib import Path


class HTTPTests(unittest.TestCase):
    def test_http_dispatch_contract(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('dual_kg.http'), '需要本机 HTTP 入口')
        from dual_kg.http import create_server
        server = create_server(port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            conn.request('GET', '/health')
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())['status'], 'ok')
            conn.request('POST', '/v1/concepts/build', '{}', {'Content-Type': 'application/json'})
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            response.read()
            conn.request('POST', '/v1/structure/export', '{}', {'Origin': 'https://example.org'})
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            conn.request('GET', '/missing')
            response = conn.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            from examples.make_fixture import make_fixture
            temporary_root = Path(__file__).resolve().parents[1] / '.test_tmp'
            temporary_root.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(dir=temporary_root) as tmp:
                root = Path(tmp)
                make_fixture(root / 'fixture')
                request = json.dumps(dict(project_root=str(root / 'fixture'), book_id='demo_book',
                                          output_dir=str(root / 'export')))
                conn.request('POST', '/v1/structure/export', request, {'Content-Type': 'application/json'})
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())['paragraph_count'], 1)
                conn.request('POST', '/v1/structure/export', request, {'Content-Type': 'application/json'})
                response = conn.getresponse()
                self.assertEqual(response.status, 409)
                response.read()
            conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
