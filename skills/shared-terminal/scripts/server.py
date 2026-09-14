#!/usr/bin/env python3
"""A private, loopback-only shared PTY for supervised local workflows."""
import argparse
import base64
import codecs
import collections
import fcntl
import http.cookies
import http.server
import json
import os
from pathlib import Path
import pty
import secrets
import signal
import socket
import socketserver
import stat
import struct
import sys
import termios
import threading

import pyte

ASSETS = Path(__file__).resolve().parent.parent / 'assets'
MAX_REPLAY = 2 * 1024 * 1024
MAX_INPUT = 65536


class Session:
    def __init__(self, cwd):
        self.cv = threading.Condition(threading.RLock())
        self.screen = pyte.HistoryScreen(120, 36, history=2500)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.events = collections.deque()
        self.seq = self.size = 0
        self.alive = True
        self.stopping = False
        self.exit_code = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.chdir(cwd)
            env = {key: os.environ[key] for key in (
                'HOME', 'USER', 'LOGNAME', 'PATH', 'TMPDIR', 'SSH_AUTH_SOCK',
            ) if key in os.environ}
            env.update(TERM='xterm-256color', COLORTERM='truecolor', LANG='en_US.UTF-8',
                       HISTFILE='/dev/null', HISTSIZE='0', PS1='shared-terminal$ ',
                       SHELL='/bin/bash')
            os.execve('/bin/bash', ['bash', '--noprofile', '--norc', '-i'], env)
        self.resize(120, 36)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        try:
            while True:
                data = os.read(self.fd, 65536)
                if not data:
                    break
                with self.cv:
                    self.stream.feed(self.decoder.decode(data))
                    self.seq += 1
                    self.events.append((self.seq, data))
                    self.size += len(data)
                    while self.size > MAX_REPLAY and len(self.events) > 1:
                        self.size -= len(self.events.popleft()[1])
                    self.cv.notify_all()
        except OSError:
            pass
        finally:
            _, status = os.waitpid(self.pid, 0)
            with self.cv:
                self.exit_code = os.waitstatus_to_exitcode(status)
                self.alive = False
                self.cv.notify_all()

    def text(self):
        return '\n'.join(self.screen.display).rstrip()

    def metadata(self):
        return {'alive': self.alive, 'pid': self.pid, 'sequence': self.seq,
                'cols': self.screen.columns, 'rows': self.screen.lines,
                'exit_code': self.exit_code}

    def resize(self, cols, rows):
        if type(cols) is not int or type(rows) is not int or not (20 <= cols <= 400 and 5 <= rows <= 150):
            raise ValueError()
        with self.cv:
            if not self.alive or self.stopping:
                raise ValueError()
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            self.screen.resize(lines=rows, columns=cols)

    def write(self, text, expected=None):
        if not isinstance(text, str) or not 0 < len(text.encode('utf-8')) <= MAX_INPUT:
            raise ValueError()
        with self.cv:
            if not self.alive or self.stopping:
                raise ValueError()
            if expected is not None and (not isinstance(expected, str) or expected not in self.text()):
                return False
            data = text.encode('utf-8')
            while data:
                data = data[os.write(self.fd, data):]
            return True

    def stop(self):
        with self.cv:
            if self.stopping:
                return
            self.stopping = True
            groups = {self.pid} if self.alive else set()
            try:
                # The foreground job can have a separate process group from bash.
                foreground = os.tcgetpgrp(self.fd)
                if self.alive and foreground > 1:
                    groups.add(foreground)
            except OSError:
                pass
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
            for group in groups:
                try:
                    os.killpg(group, sig)
                except ProcessLookupError:
                    pass
            self.reader.join(timeout=0.5)
            if not self.reader.is_alive():
                break
        os.close(self.fd)
        self.reader.join(timeout=1)
        with self.cv:
            self.events.clear()
            self.screen.reset()
            self.size = 0


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port, token):
        super().__init__(('127.0.0.1', port), Handler)
        self.token = token
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.cookie_name = f'shared_terminal_{self.server_port}'
        self.session = None

    def server_bind(self):
        # This numeric loopback service has no DNS dependency. HTTPServer would
        # resolve its FQDN here, which can block startup on an offline resolver.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address

    def handle_error(self, request, client_address):
        # No request data or terminal content in diagnostics.
        pass


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def valid_host(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def authenticated(self):
        authorization = self.headers.get('Authorization', '')
        if authorization.startswith('Bearer '):
            return authorization[7:].isascii() and secrets.compare_digest(authorization[7:], self.server.token)
        try:
            cookies = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
            cookie = cookies.get(self.server.cookie_name)
            return cookie is not None and cookie.value.isascii() and secrets.compare_digest(cookie.value, self.server.token)
        except http.cookies.CookieError:
            return False

    def reply(self, data, kind='application/json', code=200, cookie=None):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        if code >= 400:
            self.close_connection = True
            self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.valid_host():
            return self.reply({'error': 'host'}, code=403)
        if self.headers.get('Origin') not in (None, self.server.origin):
            return self.reply({'error': 'origin'}, code=403)
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/style.css': ('style.css', 'text/css'),
                 '/app.js': ('app.js', 'text/javascript'),
                 '/xterm.js': ('xterm.js', 'text/javascript'),
                 '/xterm.css': ('xterm.css', 'text/css'),
                 '/addon-fit.js': ('addon-fit.js', 'text/javascript'),
                 '/addon-web-links.js': ('addon-web-links.js', 'text/javascript')}
        if self.path in files:
            name, kind = files[self.path]
            return self.reply((ASSETS / name).read_bytes(), kind)
        if not self.authenticated():
            return self.reply({'error': 'session access required'}, code=401)
        session = self.server.session
        if self.path in ('/health', '/screen'):
            with session.cv:
                data = session.metadata()
                if self.path == '/screen':
                    data['text'] = session.text()
            return self.reply(data)
        if self.path != '/events':
            return self.reply({'error': 'not found'}, code=404)
        try:
            cursor = int(self.headers.get('Last-Event-ID', 0))
            if cursor < 0:
                raise ValueError()
        except ValueError:
            return self.reply({'error': 'invalid event cursor'}, code=400)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.end_headers()
        try:
            first = True
            while True:
                with session.cv:
                    gap = (session.events and cursor < session.events[0][0] - 1) or cursor > session.seq
                    if gap:
                        # The bounded replay has expired; redraw a coherent live
                        # screen rather than replaying a suffix of an ANSI stream.
                        raw = ('\x1b[2J\x1b[H' + '\r\n'.join(session.screen.display)
                               + f'\x1b[{session.screen.cursor.y + 1};{session.screen.cursor.x + 1}H').encode()
                        cursor = session.seq
                        payload = {'data': base64.b64encode(raw).decode(), 'reset': True}
                    else:
                        chunks = [(seq, data) for seq, data in session.events if seq > cursor]
                        payload = None
                        if chunks:
                            cursor = chunks[-1][0]
                            payload = {'data': base64.b64encode(b''.join(data for _, data in chunks)).decode(),
                                       'reset': first and self.headers.get('Last-Event-ID') is None}
                    alive = session.alive
                    if payload is None and alive:
                        session.cv.wait(timeout=5)
                if payload is not None:
                    self.wfile.write(f'id: {cursor}\ndata: {json.dumps(payload)}\n\n'.encode())
                    first = False
                elif not alive:
                    self.wfile.write(b'event: exit\ndata: {}\n\n')
                    self.wfile.flush()
                    break
                else:
                    self.wfile.write(b': keepalive\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            pass
        self.close_connection = True

    def do_POST(self):
        if (not self.valid_host() or self.headers.get('Origin') != self.server.origin
                or self.headers.get('Content-Type') != 'application/json'):
            return self.reply({'error': 'origin'}, code=403)
        if self.path != '/session' and not self.authenticated():
            return self.reply({'error': 'session access required'}, code=401)
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= 131072:
                raise ValueError()
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError()
            if self.path == '/session':
                token = data.get('token')
                if not isinstance(token, str) or not token.isascii() or not secrets.compare_digest(token, self.server.token):
                    return self.reply({'error': 'session access required'}, code=401)
                return self.reply({'ok': True}, cookie=f'{self.server.cookie_name}={self.server.token}; HttpOnly; SameSite=Strict; Path=/')
            if self.path == '/input':
                if not self.server.session.write(data['data'], data.get('expected')):
                    return self.reply({'error': 'expected text is not visible; input not sent'}, code=409)
            elif self.path == '/resize':
                self.server.session.resize(data['cols'], data['rows'])
            elif self.path == '/stop':
                self.reply({'ok': True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            else:
                return self.reply({'error': 'not found'}, code=404)
        except (ValueError, KeyError, TypeError, OSError):
            return self.reply({'error': 'invalid request'}, code=400)
        self.reply({'ok': True})


def private_file(path, body):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as file:
        file.write(body)
        identity = os.fstat(file.fileno())
    return identity.st_dev, identity.st_ino


def remove_owned(path, identity):
    try:
        current = path.lstat()
        if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cwd', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--state-file', type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.cwd.is_dir() or not 0 <= args.port <= 65535:
        parser.error('Use an existing working directory and a valid local port.')
    state = args.state_file.absolute()
    access = state.with_suffix('.access.md')
    if state.exists() or state.is_symlink() or access.exists() or access.is_symlink():
        parser.error('Use new state and access paths for each session.')
    server = Server(args.port, secrets.token_urlsafe(32))
    identities = []
    try:
        session = Session(str(args.cwd.resolve()))
        server.session = session
        values = {'schema_version': 1, 'url': server.origin, 'server_pid': os.getpid(),
                  'pty_pid': session.pid, 'token': server.token, 'access_file': str(access)}
        identities.append((state, private_file(state, json.dumps(values) + '\n')))
        body = ('# Shared terminal\n\n[Open the private session]('
                + server.origin + '/#token=' + server.token + ')\n\n'
                'This link grants access to the local terminal. Keep it private.\n')
        identities.append((access, private_file(access, body)))
        print('Shared terminal ready. Private access file: ' + str(access), flush=True)
        def stop(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, stop)
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        if server.session:
            server.session.stop()
        for path, identity in identities:
            remove_owned(path, identity)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print('Shared terminal could not start or close: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
