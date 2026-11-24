#!/usr/bin/env python3
"""cf-share-screenshot: GNOME/Wayland screenshot -> temporary Cloudflare Quick Tunnel URL.

Python standard library only. External executables: cloudflared, gnome-screenshot,
wl-copy, notify-send. Linux/POSIX only.
"""
import argparse
import errno
import hmac
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import ssl
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
MAX_IMAGE = 20 * 1024 * 1024
SERVER_START_TIMEOUT = 5  # local HTTP server only; the tunnel may still be starting
TUNNEL_TIMEOUT = 20  # how long an upload waits for cloudflared to report its URL
APP_DIR = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/tmp/cf-share-screenshot-{os.getuid()}') / 'cf-share-screenshot'
TOKEN_FILE = APP_DIR / 'bearer.token'
# Skip api.trycloudflare.com, which cloudflared mentions in its error messages.
URL_RE = re.compile(r'https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com', re.I)
EDGE_HOST = 'api.trycloudflare.com'  # any stable host on Cloudflare's edge
ID_BYTES = 8  # 64-bit random image IDs
IMAGE_PATH_RE = re.compile(rf'/image/[a-f0-9]{{{ID_BYTES * 2}}}\.png')
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
REQUIRED_COMMANDS = ('cloudflared', 'gnome-screenshot', 'wl-copy', 'notify-send')


def say(message, file=None, notify=False, clipboard=False):
    """The single way to tell the user something.

    Writes MESSAGE to FILE (default stdout). With clipboard=True also copies it
    with wl-copy, and with notify=True shows it with notify-send. Each of those
    is silently skipped when its tool is missing or fails.
    """
    if clipboard and shutil.which('wl-copy'):
        try:
            subprocess.run(['wl-copy', '--type', 'text/plain;charset=utf-8'],
                           input=message.encode('utf-8'), check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass
    print(message, file=file or sys.stdout, flush=True)
    if notify and shutil.which('notify-send'):
        try:
            subprocess.run(['notify-send', '-a', 'cf-share-screenshot', 'cf-share-screenshot', message],
                           timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


def check_commands():
    """Fail before doing anything if any required executable is missing."""
    missing = [cmd for cmd in REQUIRED_COMMANDS if not shutil.which(cmd)]
    if missing:
        raise RuntimeError(
            f"Missing commands: {', '.join(missing)}. Install them and try again.")


def read_token():
    try:
        token = TOKEN_FILE.read_text(encoding='ascii').strip()
    except FileNotFoundError:
        raise RuntimeError('cf-share-screenshot token not found')
    if not token:  # an empty token would let anyone in with "Bearer "
        raise RuntimeError(f'The token file {TOKEN_FILE} is empty; delete it and try again.')
    return token


def ensure_token():
    """Return the saved token, creating it once if it does not exist yet.

    The token is reused across server restarts. It is written to a temporary
    file and hard-linked into place, so concurrent callers never see a partial
    file and never replace a token that another process already created.
    """
    TOKEN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(TOKEN_FILE.parent, 0o700)
    if not TOKEN_FILE.exists():
        fd, tmp = tempfile.mkstemp(dir=TOKEN_FILE.parent, prefix='.token-')  # mode 0600
        try:
            with os.fdopen(fd, 'w', encoding='ascii') as f:
                f.write(secrets.token_urlsafe(32))
            try:
                os.link(tmp, TOKEN_FILE)
            except FileExistsError:
                pass  # another process created it first; use theirs
        finally:
            os.unlink(tmp)
    return read_token()


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
        return control('GET', '/status', timeout=0.7).get('ok') is True
    except (OSError, ValueError, RuntimeError, urllib.error.URLError):
        return False


def ensure_server():
    if alive():
        return False
    ensure_token()
    say('Starting the server and the Cloudflare tunnel…', file=sys.stderr, notify=True)
    # The server reports problems through notifications, not a log file.
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                      '--internal-cmd', 'serve'],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    # Wait only for the local HTTP server; the upload itself waits for the tunnel.
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while time.monotonic() < deadline:
        if alive():
            return True
        time.sleep(0.1)
    raise RuntimeError(f'The server did not start within {SERVER_START_TIMEOUT}s '
                       f'(is port {PORT} used by another program?).')


def screenshot():
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
    check_commands()
    # Take the screenshot before starting the tunnel, so cancelling does not launch a server.
    data = screenshot()
    ensure_server()
    # The server holds this request until cloudflared reports its URL (up to TUNNEL_TIMEOUT).
    result = control('POST', '/image', data, timeout=TUNNEL_TIMEOUT + 5)
    say(f"screenshot at {result['url']} (try again in a few seconds if this URL doesn't work)",
        notify=True, clipboard=True)


class State:
    def __init__(self):
        self.token = read_token()
        self.lock = threading.RLock()
        self.images = {}  # id -> (png bytes, expiration)
        self.last_upload = time.monotonic()
        self.public_url = None
        self.process = None
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
        threading.Thread(target=self.read_tunnel_log, args=(proc,), daemon=True).start()

    def read_tunnel_log(self, proc):
        # Keep draining stderr so cloudflared never blocks on a full pipe.
        try:
            for line in proc.stderr:
                match = URL_RE.search(line)
                if match:
                    with self.lock:
                        if self.process is proc:  # ignore a replaced cloudflared
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
        # Never look up the new host name locally: trycloudflare.com has a 60 s
        # negative TTL, so a lookup made before the record exists makes the local
        # resolver answer "no such host" for a minute. Instead connect to a
        # Cloudflare edge address (api.trycloudflare.com always exists) and pass
        # the new host name as SNI and Host.
        host = urlsplit(url).hostname
        conn = None
        try:
            edge_ip = socket.gethostbyname(EDGE_HOST)
            context = ssl.create_default_context()
            conn = http.client.HTTPSConnection(host, 443, timeout=12, context=context)
            conn.sock = context.wrap_socket(
                socket.create_connection((edge_ip, 443), timeout=12), server_hostname=host)
            conn.request('GET', '/status', headers={
                'Cache-Control': 'no-cache', 'Authorization': 'Bearer ' + self.token})
            response = conn.getresponse()
            return response.status == 200 and json.loads(response.read(1024)).get('ok') is True
        except (OSError, ValueError, http.client.HTTPException):
            return False
        finally:
            if conn is not None:
                conn.close()

    def monitor(self):
        failures = 0
        next_check = time.monotonic() + CHECK_EVERY  # serve() already started the tunnel
        while not self.stop.is_set():
            self.cleanup()
            if time.monotonic() - self.last_upload >= IDLE:
                say('No uploads for 3 hours; shutting down.', file=sys.stderr, notify=True)
                self.stop.set()
                break
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + CHECK_EVERY
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
                        say('Restarting the Cloudflare tunnel...', file=sys.stderr, notify=True)
                        try:
                            self.start_tunnel()
                        except OSError as exc:
                            say(f'Failed to start cloudflared: {exc}', file=sys.stderr,
                                notify=True, clipboard=True)
                        failures = 0
            self.stop.wait(1)


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
                self.obj(200, {'ok': True, 'version': __version__})
                return
            self.obj(404, {'error': 'Not found'})

        def do_POST(self):
            if urlsplit(self.path).path != '/image':
                self.obj(404, {'error': 'Not found'})
                return
            if not self.authorized():
                self.obj(401, {'error': 'Unauthorized'})
                return
            try:
                size = int(self.headers.get('Content-Length', ''))
            except ValueError:
                size = -1
            data = self.rfile.read(size) if len(PNG_MAGIC) <= size <= MAX_IMAGE else b''
            if len(data) != size or not data.startswith(PNG_MAGIC):
                self.obj(400, {'error': 'Invalid image'})
                return
            # Wait only until cloudflared reports its URL, then answer at once
            # without checking it; the periodic monitor checks the public URL.
            state.ready.wait(timeout=TUNNEL_TIMEOUT)
            with state.lock:
                if not state.public_url:
                    self.obj(503, {'error': 'The tunnel is not up yet. Try again.'})
                    return
                ident = secrets.token_hex(ID_BYTES) + '.png'
                state.images[ident] = (data, time.monotonic() + TTL)
                state.last_upload = time.monotonic()
                url = f'{state.public_url}/image/{ident}'
            self.obj(201, {'url': url})

        def log_message(self, fmt, *args):
            # Never log screenshot IDs or bearer tokens.
            pass

    return Handler


def serve():
    """Run the local server and the tunnel; started in the background by share_screenshot."""
    # The bound port is the lock: if another server holds it, exit silently.
    # Bind before touching the token so a second server never overwrites it.
    # Requests wait in the listen backlog until the real handler is set below.
    try:
        public = http.server.ThreadingHTTPServer((HOST, PORT), http.server.BaseHTTPRequestHandler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return
        raise
    ensure_token()
    state = State()
    public.RequestHandlerClass = make_handler(state)
    state.start_tunnel()
    threads = [threading.Thread(target=public.serve_forever, daemon=True),
               threading.Thread(target=state.monitor, daemon=True)]
    for t in threads:
        t.start()

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
        say(f'Error: {exc}', file=sys.stderr, notify=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
