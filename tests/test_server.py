import http.client
import json
import secrets
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import cf_share_screenshot as app

PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 32
PUBLIC_URL = 'https://example.trycloudflare.com'


class ServerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        token_file = Path(tmp.name) / 'bearer.token'
        patcher = patch.object(app, 'TOKEN_FILE', token_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token = app.write_token()
        self.state = app.State()
        self.server = app.ThreadingHTTPServer(('127.0.0.1', 0), app.make_handler(self.state))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, auth=True, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        all_headers = dict(headers or {})
        if auth:
            all_headers['Authorization'] = 'Bearer ' + self.token
        conn.request(method, path, body=body, headers=all_headers)
        response = conn.getresponse()
        return response.status, response.read()

    def make_tunnel_ready(self):
        self.state.public_url = PUBLIC_URL
        self.state.ready.set()
        patcher = patch.object(app.State, 'public_healthy', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_status_requires_token(self):
        status, _ = self.request('GET', '/status', auth=False)
        self.assertEqual(status, 401)
        status, body = self.request('GET', '/status')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['running'])

    def test_wrong_and_non_ascii_tokens_are_rejected(self):
        for value in ('Bearer wrong', 'Bearer ąż'):
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
            self.addCleanup(conn.close)
            conn.putrequest('GET', '/status')
            conn.putheader('Authorization', value.encode('utf-8'))
            conn.endheaders()
            self.assertEqual(conn.getresponse().status, 401, value)

    def test_unknown_paths_and_images_are_404(self):
        self.assertEqual(self.request('GET', '/')[0], 404)
        self.assertEqual(self.request('GET', f'/image/{secrets.token_hex(16)}.png')[0], 404)
        self.assertEqual(self.request('POST', '/upload', PNG)[0], 404)

    def test_upload_requires_token_and_png(self):
        path = f'/image/{secrets.token_hex(16)}.png'
        self.assertEqual(self.request('POST', path, PNG, auth=False)[0], 401)
        self.assertEqual(self.request('POST', path, b'GIF89a' + b'\0' * 32)[0], 400)

    def test_upload_and_download(self):
        self.make_tunnel_ready()
        path = f'/image/{secrets.token_hex(16)}.png'
        status, body = self.request('POST', path, PNG, headers={'Content-Type': 'image/png'})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)['url'], PUBLIC_URL + path)
        status, body = self.request('GET', path, auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG)
        self.assertEqual(self.request('POST', path, PNG)[0], 409)

    def test_expired_images_are_removed(self):
        ident = secrets.token_hex(16) + '.png'
        self.state.images[ident] = (PNG, 0)
        self.assertEqual(self.request('GET', '/image/' + ident, auth=False)[0], 404)
        self.state.cleanup()
        self.assertNotIn(ident, self.state.images)
