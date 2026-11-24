import http.client
import json
import secrets
import tempfile
import socket
import threading
import time
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
        self.token = app.ensure_token()
        self.state = app.State()
        self.server = app.http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), app.make_handler(self.state))
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

    def forbid_http_check(self):
        """Uploads must trust cloudflared's URL without checking it over HTTP."""
        patcher = patch.object(app.State, 'public_healthy')
        check = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: check.assert_not_called())

    def test_status_requires_token(self):
        status, _ = self.request('GET', '/status', auth=False)
        self.assertEqual(status, 401)
        status, body = self.request('GET', '/status')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ok': True, 'version': app.__version__})

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
        self.assertEqual(self.request('GET', f'/image/{secrets.token_hex(8)}.png')[0], 404)
        self.assertEqual(self.request('POST', '/upload', PNG)[0], 404)
        # The client no longer chooses the image ID.
        self.assertEqual(self.request('POST', f'/image/{secrets.token_hex(8)}.png', PNG)[0], 404)

    def test_upload_requires_token_and_png(self):
        self.make_tunnel_ready()
        self.assertEqual(self.request('POST', '/image', PNG, auth=False)[0], 401)
        for body in (b'', b'GIF89a' + b'\0' * 32, PNG[:5]):
            self.assertEqual(self.request('POST', '/image', body)[0], 400, body)
        self.assertEqual(self.state.images, {})

    def test_upload_and_download(self):
        self.forbid_http_check()
        self.make_tunnel_ready()
        status, body = self.request('POST', '/image', PNG, headers={'Content-Type': 'image/png'})
        self.assertEqual(status, 201)
        url = json.loads(body)['url']
        self.assertRegex(url, r'^https://example\.trycloudflare\.com/image/[0-9a-f]{16}\.png$')
        status, body = self.request('GET', url[len(PUBLIC_URL):], auth=False)
        self.assertEqual(status, 200)
        self.assertEqual(body, PNG)

    def test_each_upload_gets_a_new_id(self):
        self.make_tunnel_ready()
        urls = {json.loads(self.request('POST', '/image', PNG)[1])['url'] for _ in range(3)}
        self.assertEqual(len(urls), 3)

    def test_upload_sent_before_tunnel_is_ready_waits_for_it(self):
        self.forbid_http_check()
        result = {}

        def upload():
            result['response'] = self.request('POST', '/image', PNG)

        uploader = threading.Thread(target=upload)
        uploader.start()
        time.sleep(0.5)
        self.assertTrue(uploader.is_alive(), 'upload must wait for the tunnel')
        self.assertEqual(self.state.images, {})
        self.make_tunnel_ready()  # cloudflared reported its URL
        uploader.join(timeout=10)
        status, body = result['response']
        self.assertEqual(status, 201)
        self.assertTrue(json.loads(body)['url'].startswith(PUBLIC_URL + '/image/'))

    def test_upload_fails_when_tunnel_never_comes_up(self):
        with patch.object(app, 'TUNNEL_TIMEOUT', 0.5):
            status, body = self.request('POST', '/image', PNG)
        self.assertEqual(status, 503)
        self.assertEqual(self.state.images, {})

    def test_expired_images_are_removed(self):
        ident = secrets.token_hex(8) + '.png'
        self.state.images[ident] = (PNG, 0)
        self.assertEqual(self.request('GET', '/image/' + ident, auth=False)[0], 404)
        self.state.cleanup()
        self.assertNotIn(ident, self.state.images)


class MonitorTests(unittest.TestCase):
    def test_no_restart_right_after_start(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(app, 'TOKEN_FILE', Path(tmp) / 'bearer.token'):
            app.ensure_token()
            state = app.State()
        with patch.object(app, 'say') as say, \
                patch.object(app.State, 'start_tunnel') as start, \
                patch.object(app.State, 'public_healthy') as check:
            monitor = threading.Thread(target=state.monitor)
            monitor.start()
            time.sleep(1.5)
            state.stop.set()
            monitor.join(timeout=5)
        say.assert_not_called()
        start.assert_not_called()
        check.assert_not_called()


class PortLockTests(unittest.TestCase):
    def test_serve_exits_silently_when_port_is_taken(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as busy:
            busy.bind(('127.0.0.1', 0))
            busy.listen()
            app_dir = Path(tmp) / 'app'
            with patch.object(app, 'PORT', busy.getsockname()[1]), \
                    patch.object(app, 'TOKEN_FILE', app_dir / 'bearer.token'):
                self.assertIsNone(app.serve())
            self.assertFalse(app_dir.exists(), 'a second server must not touch the token')
