#!/usr/bin/env python3
"""cf-share-screenshot: GNOME/Wayland screenshot -> temporary Cloudflare Quick Tunnel URL.

Python standard library only. External executables: cloudflared, gnome-screenshot,
wl-copy, notify-send (optional). Linux/POSIX only.
"""
import argparse
import fcntl
import hmac
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import urllib.error
import urllib.request

__version__ = '0.1.0'

PORT = 18765
HOST = '127.0.0.1'
TTL = 30 * 60
IDLE = 3 * 60 * 60
CHECK_EVERY = 60
CHECK_JITTER = 60  # additional random delay: 0–60 seconds
MAX_IMAGE = 20 * 1024 * 1024
START_TIMEOUT = 90
APP_DIR = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/tmp/cf-share-screenshot-{os.getuid()}') / 'cf-share-screenshot'
TOKEN_FILE = APP_DIR / 'bearer.token'
LOG = APP_DIR / 'server.log'
# Skip api.trycloudflare.com, which cloudflared mentions in its error messages.
URL_RE = re.compile(r'https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com', re.I)
IMAGE_PATH_RE = re.compile(r'/image/[a-f0-9]{32}\.png')
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


def notify(title, message):
    if shutil.which('notify-send'):
        try:
            subprocess.Popen(['notify-send', '-a', 'cf-share-screenshot', title, message],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def require(cmd):
    if not shutil.which(cmd):
        raise RuntimeError(f'Command {cmd} not found. Install it and try again.')


def read_token():
    try:
        return TOKEN_FILE.read_text(encoding='ascii').strip()
    except FileNotFoundError:
        raise RuntimeError('cf-share-screenshot token not found')


def write_token():
    token = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w', encoding='ascii') as f:
        f.write(token)
    return token


def control(method, path, payload=None, timeout=3):
    token = read_token()
    headers = {'Authorization': 'Bearer ' + token}
    if payload is not None:
        headers['Content-Type'] = 'image/png'
    request = urllib.request.Request(
        f'http://{HOST}:{PORT}{path}', data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get('error', str(exc))
        except (ValueError, OSError):
            message = str(exc)
        raise RuntimeError(message) from exc


def alive():
    try:
        return bool(control('GET', '/status', timeout=0.7).get('running'))
    except (OSError, ValueError, RuntimeError, urllib.error.URLError):
        return False


def ensure_server():
    if alive():
        return False
    require('cloudflared')
    APP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(APP_DIR, 0o700)
    # Serialize client startups so a second client cannot overwrite the token.
    with open(APP_DIR / 'startup.lock', 'a+') as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        if alive():
            return False
        # A foreign service on the fixed port must never receive our token.
        with socket.socket() as sock:
            if sock.connect_ex((HOST, PORT)) == 0:
                raise RuntimeError(
                    f'Port {PORT} is used by another process or the token does not match.')
        write_token()
        notify('cf-share-screenshot', 'Starting the server and the Cloudflare tunnel…')
        with open(LOG, 'ab', buffering=0) as log:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                              '--internal-cmd', 'serve'],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, close_fds=True)
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if alive():
                return True
            time.sleep(0.25)
        raise RuntimeError(f'The server did not start within {START_TIMEOUT}s. Log: {LOG}')


def screenshot():
    require('gnome-screenshot')
    fd, path = tempfile.mkstemp(prefix='cf-share-screenshot-', suffix='.png')
    os.close(fd)
    try:
        result = subprocess.run(['gnome-screenshot', '-a', '-f', path], check=False)
        if result.returncode != 0:
            raise RuntimeError('Screenshot cancelled or gnome-screenshot failed.')
        data = Path(path).read_bytes()
        if not data.startswith(PNG_MAGIC):
            raise RuntimeError('gnome-screenshot did not produce a valid PNG image.')
        if len(data) > MAX_IMAGE:
            raise RuntimeError('The screenshot exceeds the 20 MiB limit.')
        return data
    finally:
        Path(path).unlink(missing_ok=True)


def share_screenshot():
    require('wl-copy')
    # Take the screenshot before starting the tunnel, so cancelling does not launch a server.
    data = screenshot()
    ensure_server()
    ident = secrets.token_hex(16)
    result = control('POST', f'/image/{ident}.png', data, timeout=START_TIMEOUT)
    prompt = f"screenshot at {result['url']}"
    subprocess.run(['wl-copy', '--type', 'text/plain;charset=utf-8'],
                   input=prompt.encode('utf-8'), check=True, timeout=10)
    notify('cf-share-screenshot: URL copied', result['url'])
    print(prompt)


class State:
    def __init__(self):
        self.token = read_token()
        self.lock = threading.RLock()
        self.images = {}  # id -> (png bytes, expiration)
        self.last_upload = time.monotonic()
        self.public_url = None
        self.process = None
        self.generation = 0
        self.stop = threading.Event()
        self.ready = threading.Event()

    def cleanup(self):
        now = time.monotonic()
        with self.lock:
            for key, (_, expiration) in list(self.images.items()):
                if expiration <= now:
                    del self.images[key]

    def terminate_tunnel(self):
        with self.lock:
            proc = self.process
            self.process = None
            self.public_url = None
            self.ready.clear()
            self.generation += 1
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def start_tunnel(self):
        self.terminate_tunnel()
        proc = subprocess.Popen(
            ['cloudflared', 'tunnel', '--no-autoupdate', '--url', f'http://{HOST}:{PORT}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, errors='replace', bufsize=1, start_new_session=True)
        with self.lock:
            self.process = proc
            gen = self.generation
        threading.Thread(target=self.read_tunnel_log, args=(proc, gen), daemon=True).start()

    def read_tunnel_log(self, proc, gen):
        # Keep draining stderr so cloudflared never blocks on a full pipe.
        try:
            for line in proc.stderr:
                print('[cloudflared]', line.rstrip(), file=sys.stderr, flush=True)
                match = URL_RE.search(line)
                if match:
                    with self.lock:
                        if gen == self.generation and self.process is proc:
                            self.public_url = match.group(0).rstrip('/')
                            self.ready.set()
        except (OSError, ValueError):
            pass

    def public_healthy(self):
        with self.lock:
            url = self.public_url
            proc = self.process
        if not url or proc is None or proc.poll() is not None:
            return False
        try:
            request = urllib.request.Request(url + '/status', headers={
                'Cache-Control': 'no-cache', 'Authorization': 'Bearer ' + self.token})
            with urllib.request.urlopen(request, timeout=12) as response:
                return response.status == 200 and json.loads(response.read(1024)).get('ok') is True
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def monitor(self):
        failures = 0
        next_check = 0
        while not self.stop.is_set():
            self.cleanup()
            if time.monotonic() - self.last_upload >= IDLE:
                print('No uploads for 3 hours; shutting down.', flush=True)
                self.stop.set()
                break
            if time.monotonic() >= next_check:
                if self.public_healthy():
                    failures = 0
                else:
                    failures += 1
                    # If there is no URL or the process died, restart immediately.
                    # Otherwise tolerate 2 transient failures before replacing the URL.
                    with self.lock:
                        missing = (not self.public_url or self.process is None
                                   or self.process.poll() is not None)
                    if missing or failures >= 3:
                        print('Restarting the Cloudflare tunnel...', flush=True)
                        try:
                            self.start_tunnel()
                        except OSError as exc:
                            print(f'Failed to start cloudflared: {exc}', file=sys.stderr)
                        failures = 0
                next_check = time.monotonic() + CHECK_EVERY + secrets.randbelow(CHECK_JITTER + 1)
            self.stop.wait(1)


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def make_handler(state):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'cf-share-screenshot/' + __version__

        def respond(self, status, data, mime='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def obj(self, status, value):
            self.respond(status, json.dumps(value).encode('utf-8'))

        def authorized(self):
            # Compare bytes: compare_digest on str raises TypeError for non-ASCII headers.
            return hmac.compare_digest(
                self.headers.get('Authorization', '').encode('utf-8', 'surrogateescape'),
                ('Bearer ' + state.token).encode('ascii'))

        def do_GET(self):
            path = urlsplit(self.path).path
            if IMAGE_PATH_RE.fullmatch(path):
                ident = path[len('/image/'):]
                with state.lock:
                    item = state.images.get(ident)
                    if item and item[1] > time.monotonic():
                        self.respond(200, item[0], 'image/png')
                        return
                self.obj(404, {'error': 'Not found'})
                return
            if path == '/status':
                if not self.authorized():
                    self.obj(401, {'error': 'Unauthorized'})
                    return
                with state.lock:
                    self.obj(200, {'ok': True, 'running': True,
                                   'url': state.public_url,
                                   'tunnel_ready': state.ready.is_set()})
                return
            self.obj(404, {'error': 'Not found'})

        def do_POST(self):
            path = urlsplit(self.path).path
            if not IMAGE_PATH_RE.fullmatch(path):
                self.obj(404, {'error': 'Not found'})
                return
            if not self.authorized():
                self.obj(401, {'error': 'Unauthorized'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                self.obj(400, {'error': 'Invalid Content-Length'})
                return
            if size < len(PNG_MAGIC) or size > MAX_IMAGE:
                self.obj(413, {'error': 'Invalid screenshot size'})
                return
            data = self.rfile.read(size)
            if len(data) != size or not data.startswith(PNG_MAGIC):
                self.obj(400, {'error': 'Image data does not match filename extension'})
                return
            # Don't return a URL until the public endpoint is reachable.
            deadline = time.monotonic() + START_TIMEOUT
            healthy = False
            while time.monotonic() < deadline and not state.stop.is_set():
                if state.ready.wait(timeout=2) and state.public_healthy():
                    healthy = True
                    break
                time.sleep(1)
            if not healthy:
                self.obj(503, {'error': 'The tunnel is not reachable yet. Try again.'})
                return
            with state.lock:
                ident = path[len('/image/'):]
                if ident in state.images:
                    self.obj(409, {'error': 'Image ID already exists'})
                    return
                state.images[ident] = (data, time.monotonic() + TTL)
                state.last_upload = time.monotonic()
                url = state.public_url + path
            self.obj(201, {'url': url})

        def log_message(self, fmt, *args):
            # Never log screenshot IDs or bearer tokens.
            pass

    return Handler


def serve():
    """Run the local server and the tunnel; started in the background by share_screenshot."""
    APP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(APP_DIR, 0o700)
    lockfile = open(APP_DIR / 'server.lock', 'a+')
    try:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('The server is already running.', file=sys.stderr)
        return
    if not TOKEN_FILE.exists():
        write_token()
    state = State()
    public = ThreadingHTTPServer((HOST, PORT), make_handler(state))
    threads = [threading.Thread(target=public.serve_forever, daemon=True),
               threading.Thread(target=state.monitor, daemon=True)]
    for t in threads:
        t.start()
    print(f'cf-share-screenshot listening on {HOST}:{PORT}; Bearer auth', flush=True)

    def shutdown(signum, frame):
        state.stop.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        state.stop.wait()
    finally:
        state.terminate_tunnel()
        public.shutdown()
        public.server_close()
        lockfile.close()


def main():
    parser = argparse.ArgumentParser(
        prog='cf-share-screenshot',
        description='Take a screenshot, share it through a Cloudflare quick tunnel '
                    'and copy the URL to the clipboard.')
    parser.add_argument('--version', action='version', version='%(prog)s ' + __version__)
    # Internal: the background server is started with --internal-cmd serve.
    parser.add_argument('--internal-cmd', choices=['serve'], help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.internal_cmd == 'serve':
            serve()
        else:
            share_screenshot()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        notify('cf-share-screenshot: error', str(exc))
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
