"""Exercise the shared terminal through its real HTTP server and controlling PTY."""
import base64
import hashlib
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / 'skills/shared-terminal'
SERVER = SKILL / 'scripts/server.py'
CONTROL = SKILL / 'scripts/control.py'


@unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'Requires a native POSIX PTY')
class SharedTerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='shared-terminal-test-')
        self.root = Path(self.temp.name)
        self.state_path = self.root / 'session.json'
        self.output = (self.root / 'server-output.txt').open('w+')
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER), '--cwd', str(self.root), '--state-file', str(self.state_path)],
            stdout=self.output, stderr=subprocess.STDOUT, start_new_session=True,
        )
        deadline = time.monotonic() + 8
        while not self.state_path.exists() and self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.025)
        if not self.state_path.exists():
            startup_status = self.process.poll()
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.output.seek(0)
            diagnostic = self.output.read(2048)
            self.output.close()
            self.temp.cleanup()
            self.fail(f'Shared terminal did not create its private state file '
                      f'(startup exit={startup_status}): {diagnostic}')
        self.state = json.loads(self.state_path.read_text())
        self.url = self.state['url']
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.wait_text('shared-terminal$')

    def tearDown(self):
        try:
            if self.process.poll() is None:
                try:
                    self.request('/stop', {})
                except (OSError, urllib.error.URLError):
                    self.process.terminate()
                self.process.wait(timeout=8)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            self.output.close()
            self.temp.cleanup()

    def request(self, path, data=None, *, auth=True, headers=None):
        request_headers = {'Origin': self.url}
        if auth:
            request_headers['Authorization'] = 'Bearer ' + self.state['token']
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        with self.opener.open(urllib.request.Request(self.url + path, data=body, headers=request_headers), timeout=5) as response:
            return json.loads(response.read())

    def wait_text(self, expected):
        deadline = time.monotonic() + 8
        text = ''
        while time.monotonic() < deadline:
            text = self.request('/screen')['text']
            if expected in text:
                return text
            time.sleep(0.025)
        self.fail('Expected public terminal output did not arrive: ' + expected)

    def send(self, text):
        return self.request('/input', {'data': text})

    def control(self, *args, input=None):
        return subprocess.run(
            [sys.executable, str(CONTROL), '--state-file', str(self.state_path), *args],
            input=input, capture_output=True, text=True, timeout=8,
        )

    def test_real_tty_accepts_input_and_receives_window_size(self):
        self.request('/resize', {'cols': 93, 'rows': 27})
        self.send("test -t 0 && test -t 1 && test -t 2 && printf 'tty=%s\\n' yes; stty size; printf 'sum=%s\\n' \"$((41*37))\"\r")
        text = self.wait_text('sum=1517')
        self.assertRegex(text, r'(?m)^tty=yes\s*$')
        self.assertRegex(text, r'(?m)^27 93\s*$')
        metadata = self.request('/health')
        self.assertEqual((metadata['cols'], metadata['rows']), (93, 27))

    def test_named_keys_and_stdin_control_an_interactive_program(self):
        self.send("read -r reply; printf 'answer=%s\\n' \"$reply\"\r")
        result = self.control('send', input='hello operator')
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.control('send', '--key', 'enter')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_text('answer=hello operator')
        result = self.control('match', '--text', 'answer=hello operator')
        self.assertEqual(json.loads(result.stdout), {'matches': True, 'alive': True})
        self.assertNotIn(self.state['token'], result.stdout + result.stderr)

    def test_input_guard_rejects_a_stale_prompt_without_execution(self):
        result = self.control('send', '--expect', 'a prompt that is not visible', input="touch should-not-exist\r")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no input was sent', result.stderr.lower())
        self.send("printf 'guard=%s\\n' checked\r")
        self.wait_text('guard=checked')
        self.assertFalse((self.root / 'should-not-exist').exists())

    def test_requires_session_access_and_rejects_cross_origin_control(self):
        attempts = [('/screen', None, {'auth': False}, 401),
                    ('/input', {'data': 'bad'}, {'auth': False}, 401),
                    ('/input', {'data': 'bad'}, {'headers': {'Origin': 'https://example.invalid'}}, 403),
                    ('/health', None, {'headers': {'Host': 'example.invalid'}}, 403),
                    ('/session', {'token': 'not-the-token'}, {'auth': False}, 401),
                    ('/session', {'token': '\u2603'}, {'auth': False}, 401)]
        for path, body, options, code in attempts:
            with self.subTest(path=path, code=code), self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path, body, **options)
            self.assertEqual(error.exception.code, code)
            error.exception.close()
        self.assertTrue(self.request('/health')['alive'])

    def test_browser_cookie_handshake_reads_and_controls_the_same_pty(self):
        cookies = http.cookiejar.CookieJar()
        browser = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(cookies))
        request = urllib.request.Request(self.url + '/session', data=json.dumps({'token': self.state['token']}).encode(),
            headers={'Origin': self.url, 'Content-Type': 'application/json'})
        with browser.open(request, timeout=5) as response:
            self.assertIn('HttpOnly', response.headers['Set-Cookie'])
            self.assertIn('SameSite=Strict', response.headers['Set-Cookie'])
        command = urllib.request.Request(self.url + '/input', data=json.dumps({'data': "printf 'browser=%s\\n' connected\r"}).encode(),
            headers={'Origin': self.url, 'Content-Type': 'application/json'})
        with browser.open(command, timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.wait_text('browser=connected')
        with browser.open(self.url + '/screen', timeout=5) as response:
            self.assertIn('browser=connected', json.load(response)['text'])

    def test_sse_reconnect_recovers_a_coherent_screen_for_an_expired_cursor(self):
        self.send("printf 'reconnect=%s\\n' ready\r")
        self.wait_text('reconnect=ready')
        request = urllib.request.Request(self.url + '/events', headers={
            'Authorization': 'Bearer ' + self.state['token'],
            'Last-Event-ID': str(self.request('/health')['sequence'] + 1000),
        })
        with self.opener.open(request, timeout=5) as response:
            self.assertEqual(response.headers['Content-Type'], 'text/event-stream')
            while True:
                line = response.readline().decode().strip()
                if line.startswith('data: '):
                    message = json.loads(line[6:])
                    break
            self.assertTrue(message['reset'])
            self.assertIn('reconnect=ready', base64.b64decode(message['data']).decode())

    def test_private_access_files_and_server_output_do_not_expose_the_session_token(self):
        access = Path(self.state['access_file'])
        for path in (self.state_path, access):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.output.flush()
        self.output.seek(0)
        self.assertNotIn(self.state['token'], self.output.read())
        self.state_path.chmod(0o644)
        try:
            result = self.control('status')
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(self.state['token'], result.stdout + result.stderr)
        finally:
            self.state_path.chmod(0o600)

    def test_stop_removes_access_files_and_terminates_the_foreground_job(self):
        program = 'import os,time;print("job="+str(os.getpid()),flush=True);time.sleep(30)'
        self.send(shlex.join([sys.executable, '-c', program]) + '\r')
        deadline = time.monotonic() + 8
        match = None
        while time.monotonic() < deadline:
            match = re.search(r'(?m)^job=(\d+)\s*$', self.request('/screen')['text'])
            if match:
                break
            time.sleep(0.025)
        self.assertIsNotNone(match)
        job_pid = int(match[1])
        result = self.control('stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.process.wait(timeout=5), 0)
        self.assertFalse(self.state_path.exists())
        self.assertFalse(Path(self.state['access_file']).exists())
        status = subprocess.run(['ps', '-p', str(job_pid), '-o', 'stat='], capture_output=True, text=True, timeout=5)
        self.assertTrue(status.returncode != 0 or status.stdout.strip().startswith('Z'), 'Foreground job remains active')

    def test_natural_shell_exit_can_be_closed_without_signalling_another_session(self):
        self.send('exit\r')
        deadline = time.monotonic() + 5
        while self.request('/health')['alive'] and time.monotonic() < deadline:
            time.sleep(0.025)
        self.assertFalse(self.request('/health')['alive'])
        result = self.control('stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.process.wait(timeout=5), 0)


class SharedTerminalAssetsTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform in ('darwin', 'linux'), 'Requires a native POSIX PTY')
    def test_loopback_server_starts_when_host_name_resolution_is_unavailable(self):
        spec = importlib.util.spec_from_file_location('shared_terminal_server', SERVER)
        server_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server_module)
        with mock.patch('socket.getfqdn', side_effect=OSError('No name resolver')):
            server = server_module.Server(0, 'test-session-access')
            try:
                self.assertEqual(server.server_name, '127.0.0.1')
                self.assertGreater(server.server_port, 0)
                self.assertEqual(server.origin, f'http://127.0.0.1:{server.server_port}')
            finally:
                server.server_close()

    def test_vendored_assets_match_the_pinned_package_manifest(self):
        manifest = json.loads((SKILL / 'assets/vendor-manifest.json').read_text())
        for name, expected in manifest['sha256'].items():
            with self.subTest(asset=name):
                self.assertEqual(hashlib.sha256((SKILL / 'assets' / name).read_bytes()).hexdigest(), expected)
        self.assertEqual(list(SKILL.rglob('*.tgz')), [])


if __name__ == '__main__':
    unittest.main()
