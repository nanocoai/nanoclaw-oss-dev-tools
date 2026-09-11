#!/usr/bin/env python3
"""Drive the public NanoClaw wizard through a real PTY; never perform its setup work."""

import argparse
import codecs
import datetime
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import secrets
import shlex
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import time

MAX_LOG_BYTES = 32 * 1024 * 1024
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])')
TOKEN = re.compile(r'sk-ant-[A-Za-z0-9_-]+')


class Failure(Exception):
    def __init__(self, phase, message, code=1):
        self.phase, self.code = phase, code
        super().__init__(message)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(6))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, indent=2)
            out.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Redactor:
    """Redact whole rendered/log texts, so chunk boundaries cannot split secrets."""
    def __init__(self, values):
        self.values = sorted({v for v in values if v and len(v) >= 8}, key=len, reverse=True)

    def clean(self, text):
        text = ANSI.sub('', text)
        for value in self.values:
            # Terminal wrapping can introduce whitespace inside a long credential.
            text = re.sub(r'\s*'.join(map(re.escape, value)), '[REDACTED]', text)
        text = TOKEN.sub('[REDACTED]', text)
        text = re.sub(r'(?im)((?:[\w-]*(?:token|password|secret|api.?key)[\w-]*)["\x27]?\s*[:=]\s*)[^\s,}\n]+', r'\1[REDACTED]', text)
        text = re.sub(r'(?i)(--(?:value|token|password|api-key)\s+)(?:"[^"]*"|\x27[^\x27]*\x27|\S+)', r'\1[REDACTED]', text)
        compact = re.sub(r'\s+', '', text)
        if any(re.sub(r'\s+', '', v) in compact for v in self.values):
            raise Failure('redaction', 'Credential remained after sanitization', 74)
        return text


def read_limited(path):
    if path.is_symlink() or not path.is_file():
        raise Failure('evidence', 'Missing or unsafe evidence file: ' + path.name)
    with path.open('rb') as source:
        data = source.read(MAX_LOG_BYTES + 1)
    if len(data) > MAX_LOG_BYTES:
        raise Failure('evidence', 'Evidence file exceeds size limit: ' + path.name)
    return data.decode('utf-8', errors='replace')


def private_values(root, credential):
    values = [credential]
    # Generated gateway credentials may appear in raw step output. Read only
    # known local configuration; these files themselves are never exported.
    for path in [root / '.env', Path.home() / '.config/onecli/config.json']:
        if not path.is_file() or path.is_symlink():
            continue
        text = read_limited(path)
        if path.suffix == '.json':
            def visit(value):
                if isinstance(value, dict):
                    for key, val in value.items():
                        if isinstance(val, str) and re.search(r'token|secret|password|api.?key', key, re.I):
                            values.append(val)
                        else:
                            visit(val)
                elif isinstance(value, list):
                    for val in value:
                        visit(val)
            try:
                visit(json.loads(text))
            except ValueError:
                raise Failure('redaction', 'Cannot parse gateway configuration', 74)
        else:
            for line in text.splitlines():
                key, sep, value = line.partition('=')
                if sep and re.search(r'TOKEN|SECRET|PASSWORD|API_KEY', key):
                    values.append(value.strip().strip('\"\x27'))
    return values


def child_environment():
    # No NANOCLAW_SKIP, provider/channel auth, display-name presets, or other
    # inherited product settings may silently perform/bypass wizard choices.
    allowed = ('HOME', 'USER', 'LOGNAME', 'PATH', 'SHELL', 'TMPDIR',
               'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS',
               'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(TERM='xterm-256color', COLORTERM='truecolor', LANG='C.UTF-8', LC_ALL='C.UTF-8', TZ='UTC')
    return env


class WizardTerminal:
    def __init__(self, scenario, values, timeout=1200, idle_timeout=180, columns=160, rows=48):
        import pyte
        self.scenario, self.values = scenario, values
        self.timeout, self.idle_timeout = timeout, idle_timeout
        self.columns, self.rows = columns, rows
        self.screen = pyte.HistoryScreen(columns, rows, history=30000)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.choices, self.seen = [], set()
        self.reply_verified = False
        self.output_bytes = 0
        self.last_prompt_index = -1

    def text(self):
        history = [''.join(line[x].data for x in range(self.columns)).rstrip()
                   for line in self.screen.history.top]
        return '\n'.join(history + [line.rstrip() for line in self.screen.display])

    def feed(self, data):
        self.output_bytes += len(data)
        if self.output_bytes > MAX_LOG_BYTES or len(self.screen.history.top) >= 30000:
            raise Failure('terminal', 'Terminal evidence limit exceeded')
        self.stream.feed(self.decoder.decode(data))

    def active(self):
        lines = self.screen.display
        indices = [i for i, line in enumerate(lines) if re.match(r'^\s*[◆◇■●]\s+', line)]
        if not indices:
            return None
        start = indices[-1]
        # Completed spinners can leave a diamond in older rendered rows. Only
        # the most recent component can own input, and active clack prompts
        # have a closing corner below their input/options.
        if not re.match(r'^\s*◆\s+', lines[start]) or not any(
                re.match(r'^\s*└', line) for line in lines[start + 1:]):
            return None
        # A wrapped prompt can span lines before its options/input. The known
        # prompt must be a complete prefix, and matching uses only live screen.
        block = '\n'.join(lines[start:]).strip()
        header = re.sub(r'^\s*◆\s+', '', lines[start]).strip()
        for line in lines[start + 1:]:
            if re.match(r'^\s*[│└◇◆■]', line) or re.search('[●○]', line):
                break
            if line.strip():
                header += ' ' + line.strip()
        return header, block

    def answer(self, fd, active):
        header, block = active
        matches = []
        for index, prompt in enumerate(self.scenario['prompts']):
            message = prompt.get('prompt') or self.values[prompt['prompt_from']]
            if header == message:
                matches.append((index, prompt))
        if len(matches) != 1:
            raise Failure('prompt', 'Unknown active wizard prompt: ' + header)
        index, prompt = matches[0]
        prompt_id = prompt['id']
        if prompt_id in self.seen or index < self.last_prompt_index:
            raise Failure('prompt', 'Repeated or out-of-order prompt: ' + prompt_id)
        missing = [p['id'] for p in self.scenario['prompts'][:index]
                   if p.get('required') and p['id'] not in self.seen]
        if missing:
            raise Failure('proof', 'Wizard skipped required prompts: ' + ', '.join(missing))
        if prompt.get('require_reply'):
            transcript = self.text()
            question = self.values['challenge']
            # Only accept a standalone computed answer AFTER the submitted
            # question, never numbers in typed input, earlier ping, or headers.
            position = transcript.rfind(question)
            reply = transcript[position + len(question):] if position >= 0 else ''
            expected = self.values['answer']
            if not re.search(r'(?m)^\s*(?:│\s*)?(?:\*\*)?' + re.escape(expected) + r'(?:\*\*)?\s*$', reply):
                raise Failure('reply', 'No matching retained-agent reply', 2)
            self.reply_verified = True
        if 'select' in prompt or 'select_from' in prompt:
            desired = prompt.get('select') or self.values[prompt['select_from']]
            options = []
            for line in block.splitlines()[1:]:
                for marker, label in re.findall(r'([●○])\s*([^●○]+)', line):
                    options.append((marker, label.strip().rstrip('/').strip()))
            targets = [i for i, (_, label) in enumerate(options)
                       if label == desired or label.startswith(desired + ' (')]
            selected = [i for i, (marker, _) in enumerate(options) if marker == '●']
            if len(targets) != 1 or len(selected) != 1:
                raise Failure('prompt', 'Missing or ambiguous choice in ' + prompt_id)
            if targets[0] != selected[0]:
                os.write(fd, b'\x1b[B' if targets[0] > selected[0] else b'\x1b[A')
                return False
            value = desired
            data = b'\r'
        else:
            value = prompt.get('text', self.values.get(prompt.get('text_from'), ''))
            data = value.encode('utf-8') + b'\r'
        # Never export input bytes. Credential choices record only the prompt id.
        self.choices.append({'id': prompt_id, 'at': now(),
                             'value': '[REDACTED]' if prompt.get('text_from') == 'credential' else value})
        self.seen.add(prompt_id)
        self.last_prompt_index = index
        os.write(fd, data)
        return True

    def run(self, root, command=None):
        command = command or ['bash', 'nanoclaw.sh']
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(root)
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', self.rows, self.columns, 0, 0))
                os.execvpe(command[0], command, child_environment())
            except BaseException:
                os._exit(127)
        start = last_output = time.monotonic()
        stable_at, active_state, submitted = start, None, None
        status, last_action = None, start
        try:
            while True:
                clock = time.monotonic()
                if clock - start > self.timeout:
                    raise Failure('timeout', 'Wizard exceeded total timeout', 124)
                if clock - last_output > self.idle_timeout:
                    raise Failure('timeout', 'Wizard stopped producing output', 124)
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        chunk = b''
                    if chunk:
                        self.feed(chunk)
                        last_output = clock
                if status is None:
                    done, raw_status = os.waitpid(pid, os.WNOHANG)
                    if done:
                        status = os.waitstatus_to_exitcode(raw_status)
                if status is not None and clock - last_output > 0.15:
                    break
                active = self.active()
                if active != active_state:
                    active_state, stable_at = active, clock
                if submitted and (not active or active[0] != submitted):
                    submitted = None
                if active and not submitted and clock - stable_at >= 0.15 and clock - last_action >= 0.15:
                    if self.answer(fd, active):
                        submitted = active[0]
                    last_action = clock
                # A clack cancellation is a failure even if the process exits0.
                if re.search(r'(?im)(?:^\s*■|Setup cancell?ed\.)', '\n'.join(self.screen.display)):
                    raise Failure('cancelled', 'Wizard cancelled', 130)
            if status != 0:
                raise Failure('wizard', 'Public wizard exited unsuccessfully', status if status and status > 0 else 1)
            missing = [p['id'] for p in self.scenario['prompts'] if p.get('required') and p['id'] not in self.seen]
            if missing:
                raise Failure('proof', 'Missing required prompt proof: ' + ', '.join(missing))
            if not self.reply_verified or self.scenario['completed_text'] not in self.text():
                raise Failure('proof', 'Missing wizard completion or retained reply')
            return status
        finally:
            def signal_group(sig):
                nonlocal status
                try:
                    os.killpg(pid, sig)
                except ProcessLookupError:
                    return
                except PermissionError:
                    # Darwin can return EPERM for an exited, unreaped group
                    # leader. Reap only our child and retry; a live group's
                    # permission error still fails rather than being ignored.
                    if status is not None:
                        raise
                    done, raw = os.waitpid(pid, os.WNOHANG)
                    if not done:
                        raise
                    status = os.waitstatus_to_exitcode(raw)
                    try:
                        os.killpg(pid, sig)
                    except ProcessLookupError:
                        pass
            try:
                # Only our PTY process group; do not touch product services.
                signal_group(signal.SIGTERM)
                if status is None:
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline:
                        done, raw = os.waitpid(pid, os.WNOHANG)
                        if done:
                            status = os.waitstatus_to_exitcode(raw)
                            break
                        time.sleep(0.02)
                    else:
                        signal_group(signal.SIGKILL)
                        if status is None:
                            _, raw = os.waitpid(pid, 0)
                            status = os.waitstatus_to_exitcode(raw)
                # A descendant can ignore TERM after the immediate child exits.
                signal_group(signal.SIGKILL)
            finally:
                os.close(fd)



def progression(text):
    steps, inputs = {}, {}
    records = re.split(r'(?m)^=== ', text)[1:]
    for record in records:
        header, _, body = record.partition('\n')
        fields = dict(re.findall(r'^  ([\w-]+): (.*)$', body, re.M))
        step = re.match(r'\[[^\]]+\] ([\w-]+) \[[^\]]+\] → ([\w-]+) ===$', header)
        choice = re.match(r'\[[^\]]+\] user-input → ([\w-]+) ===$', header)
        if step:
            name, status = step.groups()
            if status in ('failed', 'aborted'):
                raise Failure('product-step', 'Wizard step failed: ' + name)
            if name in steps:
                raise Failure('proof', 'Repeated setup step: ' + name)
            steps[name] = {'status': status, **fields}
        elif choice:
            inputs[choice[1]] = fields.get('value')
    return steps, inputs


def status_block(text, name):
    blocks = re.findall(r'^=== NANOCLAW SETUP: ' + re.escape(name) + r' ===\r?\n(.*?)^=== END ===', text, re.M | re.S)
    if len(blocks) != 1:
        raise Failure('proof', 'Missing or ambiguous ' + name + ' status block')
    return dict(re.findall(r'^([A-Z_]+):[ \t]*([^\r\n]*)', blocks[0], re.M))


def check_progress(root, scenario):
    text = read_limited(root / 'logs/setup.log')
    if 'invocation: nanoclaw.sh' not in text or not re.search(r'^## .+ · completed \(total .+\)$', text, re.M) or ' · aborted at ' in text:
        raise Failure('proof', 'Public wizard has no clean completion footer')
    steps, inputs = progression(text)
    for name in scenario['required_steps']:
        if steps.get(name, {}).get('status') != 'success':
            raise Failure('proof', 'Required wizard step was not successful: ' + name)
    for name, value in scenario['required_inputs'].items():
        if inputs.get(name) != value:
            raise Failure('proof', 'Missing or incorrect wizard choice: ' + name)
    def raw(name, block):
        relative = steps[name].get('raw', '')
        path = root / relative
        if not relative.startswith('logs/setup-steps/') or not path.resolve().is_relative_to((root / 'logs/setup-steps').resolve()):
            raise Failure('proof', 'Invalid step log reference: ' + name)
        return status_block(read_limited(path), block)
    verify = raw('verify', 'VERIFY')
    for field, value in scenario['verify'].items():
        if verify.get(field) != value:
            raise Failure('verify', 'Final verification mismatch: ' + field)
    if not verify.get('REGISTERED_GROUPS', '').isdigit() or int(verify['REGISTERED_GROUPS']) < 1:
        raise Failure('verify', 'No retained registered agent')
    if verify.get('WIRING') or verify.get('SLACK_INSTALL'):
        raise Failure('verify', 'Setup left pending or external channel work')
    timezone = raw('timezone', 'TIMEZONE')
    if timezone.get('RESOLVED_TZ') != 'UTC' or not re.search(r'^TZ=[\"\']?UTC[\"\']?$', read_limited(root / '.env'), re.M):
        raise Failure('verify', 'UTC was not persisted by the wizard')
    service = raw('service', 'SETUP_SERVICE')
    if service.get('STATUS') != 'success' or service.get('SERVICE_LOADED') != 'true':
        raise Failure('service', 'Wizard did not load its service')
    if Path(service.get('PROJECT_PATH', '')).resolve() != root.resolve():
        raise Failure('service', 'Service belongs to another checkout')
    return verify, service


def process_arguments(pid):
    proc = Path('/proc') / str(pid) / 'cmdline'
    if proc.exists():
        return [os.fsdecode(part) for part in proc.read_bytes().split(b'\0') if part]
    query = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=15)
    if query.returncode:
        raise Failure('service', 'Current service process disappeared')
    return shlex.split(query.stdout.strip())


def verify_live_service(root, service):
    kind = service.get('SERVICE_TYPE')
    if kind in ('systemd-user', 'systemd-system'):
        unit = service.get('SERVICE_UNIT', '')
        if not re.fullmatch(r'[a-zA-Z0-9_.@-]+', unit) or unit.startswith('-'):
            raise Failure('service', 'Invalid service unit')
        prefix = ['systemctl'] + (['--user'] if kind == 'systemd-user' else [])
        query = subprocess.run(prefix + ['show', unit, '-p', 'MainPID', '--value'], capture_output=True, text=True, timeout=15)
        pid = query.stdout.strip()
    elif kind == 'launchd':
        label = service.get('SERVICE_LABEL', '')
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', label):
            raise Failure('service', 'Invalid LaunchAgent label')
        query = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'], capture_output=True, text=True, timeout=15)
        match = re.search(r'^\s*pid = (\d+)\s*$', query.stdout, re.M)
        pid = match[1] if match else ''
    else:
        raise Failure('service', 'Wizard service is not loaded by a supported manager')
    if query.returncode != 0 or not pid.isdigit() or int(pid) <= 1:
        raise Failure('service', 'Service has no current process')
    argv = process_arguments(pid)
    if (len(argv) < 2 or Path(argv[1]).resolve() != (root / 'dist/index.js').resolve()
            or not service.get('NODE_PATH') or Path(argv[0]).resolve() != Path(service['NODE_PATH']).resolve()):
        raise Failure('service', 'Current service process does not run this exact checkout entrypoint')
    sock = root / 'data/cli.sock'
    if sock.is_symlink() or not stat.S_ISSOCK(sock.stat().st_mode):
        raise Failure('service', 'Missing current checkout CLI socket')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(sock))
    return {'type': kind, 'pid': int(pid), 'checkout_verified': True, 'socket_connected': True}


def export_evidence(root, destination, terminal, result, credential):
    if destination.exists():
        raise Failure('export', 'Artifact directory already exists', 74)
    redactor = Redactor(private_values(root, credential))
    files = {'terminal.txt': redactor.clean(terminal.text()) if terminal else '',
             'choices.json': redactor.clean(json.dumps(terminal.choices if terminal else [], indent=2)) + '\n'}
    logs = [root / 'logs/setup.log'] + sorted((root / 'logs/setup-steps').glob('*.log'))
    total = 0
    for path in logs:
        if not path.exists():
            continue
        content = read_limited(path)
        total += len(content.encode('utf-8'))
        if total > MAX_LOG_BYTES:
            raise Failure('export', 'Setup evidence exceeds size limit', 74)
        files[str(Path('setup-logs') / path.relative_to(root / 'logs'))] = redactor.clean(content)
    if (root / 'logs/setup.log').is_file():
        progress = read_limited(root / 'logs/setup.log')
        result['product_failures'] = re.findall(r'^=== \[.*?\] ([\w-]+) \[.*?\] → (?:failed|aborted) ===$', progress, re.M)
        result['product_aborts'] = re.findall(r'^## .*? · aborted at ([\w-]+) \(', progress, re.M)
    files['result.json'] = redactor.clean(json.dumps(result, indent=2)) + '\n'
    destination.mkdir(parents=True, mode=0o700)
    manifest = {'schema_version': 1, 'run_id': result['run_id'], 'sanitized': True, 'files': {}}
    for name, content in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode('utf-8')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
        manifest['files'][name] = hashlib.sha256(data).hexdigest()
    write_json(destination / 'manifest.json', manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--key-file', type=Path, default=Path.home() / '.nanoclaw-e2e/anthropic_key')
    parser.add_argument('--result-file', type=Path)
    parser.add_argument('--artifacts-dir', type=Path)
    parser.add_argument('--run-id', default=secrets.token_hex(16))
    parser.add_argument('--timeout', type=float, default=1200)
    parser.add_argument('--idle-timeout', type=float, default=180)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    result_path = args.result_file or root / 'logs/e2e/result.json'
    artifacts = args.artifacts_dir or root / 'logs/e2e-wizard'
    result = {'schema_version': 1, 'mode': 'wizard', 'run_id': args.run_id, 'status': 'running',
              'phase': 'preflight', 'commit': None, 'exit_code': None, 'started_at': now()}
    write_json(result_path, result)  # Invalidate stale success before any check.
    terminal, credential = None, ''
    previous_signals = {}
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    for signum in (signal.SIGTERM, signal.SIGHUP):
        previous_signals[signum] = signal.signal(signum, interrupted)
    try:
        if args.timeout <= 0 or args.idle_timeout <= 0 or not re.fullmatch(r'[a-zA-Z0-9-]{8,64}', args.run_id):
            raise Failure('preflight', 'Invalid timeout or run identity', 64)
        if artifacts.exists():
            raise Failure('preflight', 'Use a new artifact directory for each run', 64)
        if os.getuid() == 0:
            raise Failure('preflight', 'Run the fresh scenario as a regular user', 64)
        if not (root / 'nanoclaw.sh').is_file() or json.loads((root / 'package.json').read_text()).get('name') != 'nanoclaw':
            raise Failure('preflight', 'Run from the NanoClaw checkout under test', 65)
        for path in ['.env', 'data/v2.db', 'logs/setup.log', 'data/cli.sock']:
            if (root / path).exists():
                raise Failure('preflight', 'Fresh scenario refuses prior product state: ' + path, 65)
        if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], cwd=root).returncode:
            raise Failure('preflight', 'Checkout has tracked edits', 65)
        result['commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        if args.key_file.is_symlink() or args.key_file.stat().st_mode & 0o077:
            raise Failure('preflight', 'Credential file must be private (0600)', 66)
        credential = re.sub(r'\s+', '', read_limited(args.key_file))
        if not 16 <= len(credential) <= 1024 or not re.fullmatch(r'sk-ant-(?:api|oat)[A-Za-z0-9_-]+', credential):
            raise Failure('preflight', 'Unsupported Anthropic credential shape', 66)
        oauth = credential.startswith('sk-ant-oat')
        left, right = 10000 + secrets.randbelow(80000), 11 + secrets.randbelow(88)
        values = {'credential': credential, 'display_name': 'Wizard Tester',
                  'auth_option': 'Paste an OAuth token I already have' if oauth else 'Paste an Anthropic API key',
                  'credential_prompt': 'Paste your OAuth token' if oauth else 'Paste your API key',
                  'challenge': f'Reply with only the decimal value of {left} * {right}.',
                  'answer': str(left * right)}
        scenario = json.loads((Path(__file__).resolve().parent.parent / 'scenarios/fresh-cli.json').read_text())
        result['scenario'] = scenario['name']
        result['scenario_source_commit'] = scenario['source_commit']
        result['phase'] = 'wizard'
        write_json(result_path, result)
        # The product writes the credential into its raw auth log. Protect the
        # parent directory while leaving the product log contents untouched.
        (root / 'logs').mkdir(mode=0o700, exist_ok=True)
        (root / 'logs').chmod(0o700)
        terminal = WizardTerminal(scenario, values, args.timeout, args.idle_timeout)
        terminal.run(root)
        verify, service = check_progress(root, scenario)
        live = verify_live_service(root, service)
        result.update(status='pass', phase='complete', exit_code=0, ping='ok',
                      service_type=live['type'], service=live, verification=verify,
                      wizard_completed=True, retained_reply_verified=True)
    except KeyboardInterrupt:
        result.update(status='failed', phase='cancelled', exit_code=130, error='Driver interrupted')
    except Failure as error:
        result.update(status='failed', phase=error.phase, exit_code=error.code, error=str(error))
    except Exception as error:
        # Exception strings can contain subprocess arguments or configuration.
        result.update(status='failed', phase=result['phase'], exit_code=1, error='Driver error: ' + type(error).__name__)
    result['finished_at'] = now()
    result_redactor = Redactor([credential])
    try:
        result_redactor = Redactor(private_values(root, credential))
        export_evidence(root, artifacts, terminal, result, credential)
        result['artifacts'] = str(artifacts)
    except Exception as error:
        result.update(status='failed', phase='export', exit_code=74, error='Sanitized evidence export failed: ' + type(error).__name__)
    safe_result = json.loads(result_redactor.clean(json.dumps(result)))
    write_json(result_path, safe_result)
    print('[e2e-wizard] ' + safe_result['status'] + ' phase=' + safe_result['phase'] + ' result=' + str(result_path))
    for signum, handler in previous_signals.items():
        signal.signal(signum, handler)
    return safe_result['exit_code']


if __name__ == '__main__':
    sys.exit(main())
