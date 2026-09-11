"""Wizard regressions use real PTYs and rendered menus; no VM or model requests."""
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest

import test_e2e

SKILL = Path(__file__).resolve().parents[1] / 'skills/e2e-wizard'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wizard = load('wizard_test_runner', SKILL / 'scripts/wizard-run.py')
collector = load('wizard_test_collector', SKILL / 'scripts/collect-wizard.py')

SCENARIO = {
    'prompts': [
        {'id': 'start', 'prompt': 'Choose a mode?', 'select': 'Standard setup', 'required': True},
        {'id': 'chat', 'prompt': 'Try a quick hello', 'text_from': 'challenge', 'required': True},
        {'id': 'chat-end', 'prompt': 'Another message?', 'text': '', 'require_reply': True, 'required': True},
    ],
    'completed_text': "You're ready! Chat with",
}
VALUES = {'challenge': 'Reply with only the decimal value of 23456 * 37.', 'answer': '867872'}

FIXTURE = r'''
import fcntl, os, signal, struct, subprocess, sys, termios, time, tty
from pathlib import Path
mode = sys.argv[1]
assert os.isatty(0) and os.isatty(1)
with open('/dev/tty', 'rb') as controlling:
    assert os.isatty(controlling.fileno())
assert struct.unpack('HHHH', fcntl.ioctl(0, termios.TIOCGWINSZ, b'\0'*8))[:2] == (48, 160)
tty.setraw(0)
def out(value):
    os.write(1, value.encode())
def line():
    data = b''
    while True:
        part = os.read(0, 1)
        if part == b'\r':
            return data.decode()
        data += part
if mode == 'zero':
    sys.exit(0)
if mode == 'cancel':
    out('■ Setup cancelled.\r\n')
    sys.exit(0)
if mode == 'timeout':
    child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)'])
    Path('descendant.pid').write_text(str(child.pid))
    out('Spinner running\r\n')
    time.sleep(60)
if mode == 'unknown':
    out('◆ May I overwrite your existing gateway?\r\n│ ● Yes\r\n│ ○ No\r\n└\r\n')
    assert not os.read(0, 1), 'unknown prompt was answered'
    sys.exit(1)
# A submitted decoy must never trigger a keystroke while no input is active.
out('◆ Sandbox ready. (2m 11s)\r\n│\r\n◇ Choose a mode?\r\n│ Standard setup\r\n')
import select
assert not select.select([0], [], [], 0.25)[0], 'answered submitted history'
selected = 0
def menu():
    out('\x1b[2J\x1b[H◇ Old submitted prompt\r\n◆ Choose a mode?\r\n')
    for i, label in enumerate(['Advanced', 'Standard setup']):
        out('│ ' + ('●' if selected == i else '○') + ' ' + label + '\r\n')
    out('└\r\n')
menu()
while True:
    key = os.read(0, 1)
    if key == b'\x1b':
        tail = os.read(0, 2)
        assert tail == b'[B'
        selected += 1
        menu()
    elif key == b'\r':
        assert selected == 1, 'wrong option selected'
        break
    else:
        raise AssertionError(repr(key))
out('\x1b[2J\x1b[H◇ Choose a mode?\r\n│ Standard setup\r\n◆ Try a quick hello\r\n│ \r\n└\r\n')
question = line()
out('\x1b[2J\x1b[H◇ Try a quick hello\r\n│ ' + question + '\r\n')
if mode == 'pass':
    out('867872\r\n')
elif mode == 'echo':
    out(question + '\r\n')
elif mode == 'error':
    out('timeout: no reply in 120000ms\r\n')
out('◆ Another message?\r\n│ \r\n└\r\n')
assert line() == ''
out('\x1b[2J\x1b[H◇ Another message?\r\n│ \r\nYou\x27re ready! Chat with pnpm run chat hi.\r\n')
'''


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='nc-wizard-', dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = self.root / 'terminal.py'
        self.fixture.write_text(textwrap.dedent(FIXTURE))

    def terminal(self, timeout=8, idle=3):
        return wizard.WizardTerminal(SCENARIO, VALUES, timeout=timeout, idle_timeout=idle)

    def run_fixture(self, terminal, mode):
        return terminal.run(self.root, [sys.executable, str(self.fixture), mode])

    def test_real_pty_redraw_selects_current_option_and_observes_real_reply(self):
        terminal = self.terminal()
        self.assertEqual(self.run_fixture(terminal, 'pass'), 0)
        self.assertTrue(terminal.reply_verified)
        self.assertEqual([choice['id'] for choice in terminal.choices], ['start', 'chat', 'chat-end'])

    def test_unicode_and_ansi_split_at_every_byte_preserve_active_prompt(self):
        terminal = self.terminal()
        data = '\x1b[2J\x1b[H◆ Choose a mode?\r\n│ ○ Advanced\r\n│ ● Standard setup\r\n└'.encode()
        for byte in data:
            terminal.feed(bytes([byte]))
        self.assertEqual(terminal.active()[0], 'Choose a mode?')
        read_fd, write_fd = os.pipe()
        try:
            self.assertTrue(terminal.answer(write_fd, terminal.active()))
            self.assertEqual(os.read(read_fd, 100), b'\r')
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_completed_spinner_diamond_does_not_own_input(self):
        terminal = self.terminal()
        terminal.feed('◆ Sandbox ready. (2m 11s)\r\n│\r\n'.encode())
        self.assertIsNone(terminal.active())
        terminal.feed('◇ OneCLI vault ready. (29s)\r\n│\r\n'.encode())
        self.assertIsNone(terminal.active())
        terminal.feed('◆ Choose a mode?\r\n│ ● Standard setup\r\n└\r\n'.encode())
        self.assertEqual(terminal.active()[0], 'Choose a mode?')
        terminal.feed('◇ Choose a mode?\r\n│ Standard setup\r\n'.encode())
        self.assertIsNone(terminal.active())

    def test_unknown_prompt_stops_without_supplying_input(self):
        with self.assertRaises(wizard.Failure) as caught:
            self.run_fixture(self.terminal(), 'unknown')
        self.assertEqual(caught.exception.phase, 'prompt')

    def test_cancel_and_zero_exit_never_pass(self):
        for mode in ['cancel', 'zero']:
            with self.subTest(mode=mode), self.assertRaises(wizard.Failure):
                self.run_fixture(self.terminal(), mode)

    def test_echo_or_chat_child_error_never_proves_reply(self):
        for mode in ['echo', 'error']:
            with self.subTest(mode=mode), self.assertRaises(wizard.Failure) as caught:
                self.run_fixture(self.terminal(), mode)
            self.assertEqual(caught.exception.phase, 'reply')

    def test_timeout_kills_foreground_descendant_even_when_it_ignores_term(self):
        with self.assertRaises(wizard.Failure) as caught:
            self.run_fixture(self.terminal(timeout=0.8), 'timeout')
        self.assertEqual(caught.exception.code, 124)
        pid = int((self.root / 'descendant.pid').read_text())
        probe = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
        self.assertTrue(probe.returncode != 0 or probe.stdout.strip().startswith('Z'), probe.stdout)

    def test_missing_earlier_required_prompt_cannot_skip_forward(self):
        terminal = self.terminal()
        terminal.feed('◆ Another message?\r\n│ \r\n└\r\n'.encode())
        with self.assertRaises(wizard.Failure) as caught:
            terminal.answer(-1, terminal.active())
        self.assertEqual(caught.exception.phase, 'proof')

    def test_inherited_product_presets_are_removed_from_child_environment(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {'NANOCLAW_SKIP': 'service,verify', 'ANTHROPIC_API_KEY': 'private', 'GITHUB_TOKEN': 'private', 'NANOCLAW_DISPLAY_NAME': 'preset'}):
            env = wizard.child_environment()
        self.assertFalse(any(key.startswith('NANOCLAW_') or key in ('ANTHROPIC_API_KEY', 'GITHUB_TOKEN') for key in env))


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='nc-wizard-proof-', dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.steps = self.root / 'logs/setup-steps'
        self.steps.mkdir(parents=True)
        self.scenario = json.loads((SKILL / 'scenarios/fresh-cli.json').read_text())
        self.key = self.root / 'key'
        self.secret = 'sk-ant-api03-FAKE_CREDENTIAL_abcdefghijklmnopqrstuvwxyz012345'
        self.key.write_text(self.secret)
        self.key.chmod(0o600)
        self.result = {'schema_version': 1, 'mode': 'wizard', 'run_id': 'regression123', 'commit': 'a'*40,
                       'status': 'pass', 'exit_code': 0, 'wizard_completed': True, 'retained_reply_verified': True,
                       'service': {'type': 'systemd-user', 'checkout_verified': True, 'socket_connected': True}}
        self.progress = self.root / 'logs/setup.log'
        lines = ['## today · setup:auto started', '  invocation: nanoclaw.sh']
        for index, name in enumerate(self.scenario['required_steps'], 1):
            lines += [f'=== [today] {name} [1s] → success ===', f'  raw: logs/setup-steps/{index:02d}-{name}.log', '']
            path = self.steps / f'{index:02d}-{name}.log'
            if name == 'verify':
                content = self.block('VERIFY', {**self.scenario['verify'], 'REGISTERED_GROUPS': '1'})
            elif name == 'service':
                content = self.block('SETUP_SERVICE', {'STATUS': 'success', 'SERVICE_LOADED': 'true', 'SERVICE_TYPE': 'systemd-user', 'PROJECT_PATH': str(self.root), 'SERVICE_UNIT': 'nanoclaw-test'})
            elif name == 'timezone':
                content = self.block('TIMEZONE', {'STATUS': 'success', 'RESOLVED_TZ': 'UTC'})
            else:
                content = '# onecli secrets create --value ' + self.secret + '\nSTATUS: success\n'
            path.write_text(content)
        for key, value in self.scenario['required_inputs'].items():
            lines += [f'=== [today] user-input → {key} ===', f'  value: {value}', '']
        lines.append('## today · completed (total 4m)')
        self.progress.write_text('\n'.join(lines) + '\n')
        (self.root / '.env').write_text('TZ=UTC\nONECLI_API_KEY=synthetic-gateway-token-12345\n')

    @staticmethod
    def block(name, fields):
        return '=== NANOCLAW SETUP: ' + name + ' ===\n' + ''.join(key + ': ' + str(value) + '\n' for key, value in fields.items()) + '=== END ===\n'

    def test_complete_progression_requires_independent_fields(self):
        verify, service = wizard.check_progress(self.root, self.scenario)
        self.assertEqual(verify['REGISTERED_GROUPS'], '1')
        self.assertEqual(service['PROJECT_PATH'], str(self.root))

    def test_zero_exit_footer_is_not_enough_when_step_skipped_or_failed(self):
        original = self.progress.read_text()
        for status in ['skipped', 'failed', 'aborted']:
            with self.subTest(status=status):
                self.progress.write_text(original.replace('service [1s] → success', 'service [1s] → ' + status))
                with self.assertRaises(wizard.Failure):
                    wizard.check_progress(self.root, self.scenario)

    def test_verification_rejects_zero_groups_wrong_image_and_unloaded_service(self):
        for name, before, after in [('verify','CONFIGURED_CHANNELS: ','CONFIGURED_CHANNELS: telegram'), ('verify','REGISTERED_GROUPS: 1','REGISTERED_GROUPS: 0'), ('verify','IMAGE_SOURCE_ACTUAL: local','IMAGE_SOURCE_ACTUAL: unknown'), ('service','SERVICE_LOADED: true','SERVICE_LOADED: false')]:
            path = next(self.steps.glob('*-' + name + '.log'))
            original = path.read_text()
            with self.subTest(name=name, after=after):
                path.write_text(original.replace(before, after))
                with self.assertRaises(wizard.Failure):
                    wizard.check_progress(self.root, self.scenario)
                path.write_text(original)

    def test_live_service_rejects_lookalike_or_later_script_arguments(self):
        from unittest.mock import patch
        expected = str(self.root/'dist/index.js')
        service = {'SERVICE_TYPE':'systemd-user','SERVICE_UNIT':'nanoclaw-test','NODE_PATH':'/usr/bin/node'}
        response = subprocess.CompletedProcess([],0,'12345\n','')
        for argv in [['/usr/bin/node',expected+'.old'], ['/usr/bin/node','elsewhere.js',expected], ['/usr/bin/python3',expected]]:
            with self.subTest(argv=argv), patch.object(wizard,'process_arguments',return_value=argv), patch.object(wizard.subprocess,'run',return_value=response):
                with self.assertRaises(wizard.Failure) as caught:
                    wizard.verify_live_service(self.root,service)
                self.assertEqual(caught.exception.phase,'service')

    def test_forged_external_raw_log_reference_is_rejected(self):
        self.progress.write_text(self.progress.read_text().replace('logs/setup-steps/12-verify.log', '/tmp/unrelated.log'))
        with self.assertRaises(wizard.Failure):
            wizard.check_progress(self.root, self.scenario)

    def test_redaction_covers_raw_argv_wrapped_credentials_and_ansi(self):
        redactor = wizard.Redactor([self.secret, 'synthetic-gateway-token-12345'])
        text = '# onecli --value ' + self.secret + '\n' + self.secret[:25] + '\r\n' + self.secret[25:] + '\n' + '\x1b[31m'.join(self.secret) + '\napiToken=synthetic-gateway-token-12345\n'
        clean = redactor.clean(text)
        self.assertNotIn(self.secret, ''.join(clean.split()))
        self.assertNotIn('synthetic-gateway-token-12345', clean)
        self.assertNotIn('sk-ant-', clean)

    def export(self):
        destination = self.root / 'sanitized'
        wizard.export_evidence(self.root, destination, None, self.result, self.secret)
        return destination

    def archive(self, destination):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w') as archive:
            for path in destination.rglob('*'):
                if path.is_file():
                    archive.add(path, arcname=str(path.relative_to(destination)))
        out.seek(0)
        return out

    def test_sanitized_export_keeps_raw_product_logs_on_target(self):
        destination = self.export()
        self.assertIn(self.secret, next(self.steps.glob('*-auth.log')).read_text())
        for path in destination.rglob('*'):
            if path.is_file():
                self.assertNotIn(self.secret, path.read_text())
                self.assertEqual(path.stat().st_mode & 0o077, 0)
        local = self.root / 'collected'
        result = self.root / 'result.json'
        collector.collect(self.archive(destination), local, result, 'a'*40, 'regression123', 0, self.key)
        self.assertEqual(json.loads(result.read_text())['status'], 'pass')

    def test_export_rejects_symlinked_raw_logs(self):
        next(self.steps.glob('*-auth.log')).unlink()
        (self.steps / '05-auth.log').symlink_to(self.key)
        with self.assertRaises(wizard.Failure):
            self.export()
        self.assertFalse((self.root / 'sanitized').exists())

    def test_collector_rejects_stale_run_corruption_and_unredacted_artifacts(self):
        destination = self.export()
        for mode in ['stale', 'corrupt', 'secret']:
            with self.subTest(mode=mode):
                run_id = 'oldrun123' if mode == 'stale' else 'regression123'
                if mode != 'stale':
                    (destination / 'terminal.txt').write_text('changed' if mode == 'corrupt' else self.secret)
                    if mode == 'secret':
                        import hashlib
                        manifest = json.loads((destination / 'manifest.json').read_text())
                        manifest['files']['terminal.txt'] = hashlib.sha256(self.secret.encode()).hexdigest()
                        (destination / 'manifest.json').write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    collector.collect(self.archive(destination), self.root/'local', self.root/'result.json', 'a'*40, run_id, 0, self.key)
                self.assertFalse((self.root/'result.json').exists())

    def test_collector_rejects_archive_traversal(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as archive:
            item = tarfile.TarInfo('../escape'); item.size = 1
            archive.addfile(item, io.BytesIO(b'x'))
        data.seek(0)
        with self.assertRaises(ValueError):
            collector.collect(data, self.root/'local', self.root/'result.json', 'a'*40, 'regression123', 0, self.key)

    def test_direct_result_also_redacts_generated_gateway_credentials(self):
        from unittest.mock import patch
        generated = 'synthetic-gateway-token-12345'
        self.progress.unlink()
        (self.root/'.env').unlink()
        (self.root/'nanoclaw.sh').write_text('# fixture')
        (self.root/'package.json').write_text('{"name":"nanoclaw"}')
        def wizard_failure(root):
            (root/'.env').write_text('ONECLI_API_KEY=' + generated + '\n')
            raise wizard.Failure('prompt', 'Unknown active wizard prompt: ' + generated)
        result = self.root/'direct-result.json'
        artifacts = self.root/'failure-artifacts'
        response = subprocess.CompletedProcess([], 0)
        with patch.object(wizard.os, 'getuid', return_value=1000), \
                patch.object(wizard.subprocess, 'run', return_value=response), \
                patch.object(wizard.subprocess, 'check_output', return_value='a'*40), \
                patch.object(wizard.WizardTerminal, 'run', side_effect=wizard_failure):
            rc = wizard.main(['--root',str(self.root),'--key-file',str(self.key),
                              '--result-file',str(result),'--artifacts-dir',str(artifacts)])
        self.assertNotEqual(rc, 0)
        for path in (result, artifacts/'result.json'):
            text = path.read_text()
            self.assertNotIn(generated, text)
            self.assertNotIn(self.secret, text)
            self.assertIn('[REDACTED]', json.loads(text)['error'])

    def test_preflight_invalidates_existing_pass_without_running_wizard(self):
        result = self.root / 'old-result.json'
        result.write_text('{"status":"pass"}')
        rc = wizard.main(['--root',str(self.root),'--key-file',str(self.key),'--result-file',str(result),'--artifacts-dir',str(self.root/'preflight-artifacts')])
        self.assertNotEqual(rc, 0)
        self.assertEqual(json.loads(result.read_text())['status'], 'failed')


class WizardDriverTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_e2e.DriverTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        scripts = self.fixture.root / 'skills/e2e-exe-dev/scripts'
        scripts.mkdir(parents=True)
        for name in ['exe-run.sh','e2e-install.sh']:
            shutil.copy2(self.fixture.skill/name, scripts/name)
        self.fixture.driver = scripts/'exe-run.sh'
        self.wizard_skill = self.fixture.root/'skills/e2e-wizard'
        shutil.copytree(SKILL, self.wizard_skill, ignore=shutil.ignore_patterns('__pycache__'))
        # Fake only the remote wizard process; actual lifecycle, SSH transport,
        # archive creation and local evidence validator remain exercised.
        (self.wizard_skill/'scripts/wizard-install.sh').write_text("""#!/usr/bin/env bash
set -euo pipefail
python3 - "$2" <<'PY'
import hashlib,json,os,pathlib,subprocess,sys
root=pathlib.Path.cwd();dest=root/'logs/e2e-wizard';dest.mkdir(parents=True)
rc=int(os.environ.get('MOCK_INSTALL_RC','0'))
result={'schema_version':1,'mode':'wizard','run_id':sys.argv[1],
 'commit':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),
 'status':'failed' if rc else 'pass','exit_code':rc,'wizard_completed':True,
 'retained_reply_verified':True,'service':{'checkout_verified':True,'socket_connected':True}}
files={'terminal.txt':'sanitized output','choices.json':'[]','setup-logs/setup.log':'completed', 'result.json':json.dumps(result)}
manifest={'schema_version':1,'run_id':sys.argv[1],'sanitized':True,'files':{}}
for name,content in files.items():
 path=dest/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
 manifest['files'][name]=hashlib.sha256(content.encode()).hexdigest()
if os.environ.get('MOCK_CORRUPT_EXPORT'): manifest['files']['terminal.txt']='bad'
(dest/'manifest.json').write_text(json.dumps(manifest))
PY
exit "${MOCK_INSTALL_RC:-0}"
""")
        self.result = self.fixture.root/'result.json'

    def run_driver(self, *args):
        return self.fixture.run_driver('--interactive','--result-file',str(self.result),*args)

    def test_success_exports_evidence_before_opt_in_removal(self):
        run=self.run_driver('--rm')
        self.assertEqual(run.returncode,0,run.stderr)
        self.assertEqual(json.loads(self.result.read_text())['mode'],'wizard')
        self.assertTrue(Path(str(self.result)+'.artifacts/terminal.txt').is_file())
        calls=self.fixture.calls()
        export=next(i for i,c in enumerate(calls) if c[0]!='exe.dev' and 'tar -C ~/nanoclaw/logs/e2e-wizard' in ' '.join(c))
        removal=next(i for i,c in enumerate(calls) if c[:2]==['exe.dev','rm'])
        self.assertLess(export,removal)
        self.assertFalse((self.fixture.vm/'observed.json').exists(), 'headless installer ran')

    def test_failed_wizard_is_retained_with_sanitized_result(self):
        self.fixture.env['MOCK_INSTALL_RC']='2'
        run=self.run_driver('--rm')
        self.assertEqual(run.returncode,2,run.stderr)
        self.assertEqual(json.loads(self.result.read_text())['status'],'failed')
        self.fixture.assert_retained()

    def test_failed_artifact_validation_never_exports_pass_or_removes_vm(self):
        self.fixture.env['MOCK_CORRUPT_EXPORT']='1'
        run=self.run_driver('--rm')
        self.assertEqual(run.returncode,74,run.stderr)
        self.assertEqual(json.loads(self.result.read_text())['status'],'failed')
        self.fixture.assert_retained()

    def test_wizard_refuses_cached_install_or_missing_bundle_before_vm_creation(self):
        run=self.run_driver('--base','already-installed')
        self.assertNotEqual(run.returncode,0)
        self.assertEqual(self.fixture.calls(),[])
        (self.wizard_skill/'scenarios/fresh-cli.json').unlink()
        run=self.run_driver()
        self.assertNotEqual(run.returncode,0)
        self.assertEqual(self.fixture.calls(),[])


if __name__ == '__main__':
    unittest.main()
