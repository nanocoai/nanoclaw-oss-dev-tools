import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wizard = load('pr3792_wizard', ROOT / 'skills/e2e-wizard/scripts/wizard-run.py')
adapter = load('pr3792_adapter', ROOT / 'skills/e2e-wizard/scripts/proxmox-wizard.py')
collector = load('pr3792_collector', ROOT / 'skills/e2e-wizard/scripts/collect-wizard.py')


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pr3792-payload-')
        self.root = Path(self.temp.name)
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        subprocess.run(['git', 'config', 'user.email', 'e2e@example.invalid'], cwd=self.root, check=True)
        subprocess.run(['git', 'config', 'user.name', 'E2E'], cwd=self.root, check=True)
        skill = self.root / '.claude/skills/add-codex/SKILL.md'
        payload = self.root / 'setup/providers/codex.ts'
        skill.parent.mkdir(parents=True)
        payload.parent.mkdir(parents=True)
        skill.write_text('```nc:copy from-branch:providers\nsetup/providers/codex.ts\n```\n')
        payload.write_text('export const pin = true;\n')
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        subprocess.run([
            'git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
            'commit', '-qm', 'fixture',
        ], cwd=self.root, check=True)
        self.commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.root, text=True).strip()
        self.selected = {
            'source': '.claude/skills/add-codex/SKILL.md',
            'auth_source_commit': self.commit,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_payload_receipt_and_mismatch_fail_closed(self):
        receipt = wizard.verify_provider_payload(self.root, self.selected)
        self.assertEqual(receipt['commit'], self.commit)
        self.assertEqual(receipt['file_count'], 1)
        (self.root / 'setup/providers/codex.ts').write_text('changed\n')
        with self.assertRaises(wizard.Failure) as caught:
            wizard.verify_provider_payload(self.root, self.selected)
        self.assertEqual(caught.exception.phase, 'payload')

    def test_missing_payload_fails_closed(self):
        (self.root / 'setup/providers/codex.ts').unlink()
        with self.assertRaises(wizard.Failure):
            wizard.verify_provider_payload(self.root, self.selected)


class AdapterTests(unittest.TestCase):
    def test_handoff_paths_are_run_scoped_and_refuse_reuse(self):
        with tempfile.TemporaryDirectory(prefix='pr3792-handoff-root-') as temporary, \
                patch.object(wizard, 'HANDOFF_ROOT', Path(temporary) / 'auth-handoffs'):
            run_dir, request, response = wizard.create_handoff_paths('run12345')
            self.assertEqual(request, run_dir / 'request.json')
            self.assertEqual(response, run_dir / 'response.json')
            self.assertEqual(run_dir.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(wizard.Failure):
                wizard.create_handoff_paths('run12345')
            wizard.write_json(request, {'run_id': 'run12345', 'nonce': 'nonce'})
            wizard.cleanup_handoff(run_dir, request, response, 'run12345', 'nonce')
            self.assertFalse(run_dir.exists())

    def test_cleanup_never_deletes_unbound_handoff(self):
        with tempfile.TemporaryDirectory(prefix='pr3792-handoff-root-') as temporary, \
                patch.object(wizard, 'HANDOFF_ROOT', Path(temporary) / 'auth-handoffs'):
            run_dir, request, response = wizard.create_handoff_paths('run12345')
            wizard.write_json(request, {'run_id': 'some-other-run', 'nonce': 'nonce'})
            wizard.cleanup_handoff(run_dir, request, response, 'run12345', 'nonce')
            self.assertTrue(request.exists())
            self.assertTrue(run_dir.exists())

    def test_stock_credential_wrapper_keeps_private_credential_file(self):
        text = adapter.wrapper('a' * 32, 1200, 'claude', 'api', None, 'a' * 40, False)
        self.assertIn('--credential-file "$HOME/.nanoclaw-e2e/anthropic_key"', text)
        self.assertNotIn('--supervised-human-auth', text)

    def test_device_code_redactor_removes_wrapped_code(self):
        redactor = wizard.Redactor(['ABCD-EFGH'])
        self.assertNotIn('ABCD', redactor.clean('code ABCD-\nEFGH'))

    def test_pinned_codex_prompt_recognizes_and_redacts_uneven_code(self):
        with tempfile.TemporaryDirectory(prefix='pr3792-handoff-') as temporary:
            handoff = Path(temporary) / 'request.json'
            terminal = wizard.WizardTerminal(
                {'prompts': []}, {}, handoff_method='device', run_id='run12345',
                handoff_path=handoff, handoff_response_path=Path(temporary) / 'response.json',
            )
            prompt = (
                'Welcome to Codex [v0.146.0]\n'
                'Follow these steps to sign in with ChatGPT using device code authorization:\n\n'
                '1. Open this link in your browser and sign in to your account\n'
                '   https://auth.openai.com/codex/device\n\n'
                '2. Enter this one-time code (expires in 15 minutes)\n'
                '   ABCD-EFGHI\n\n'
                'Continue only if you started this login in Codex.\n'
            )
            terminal.feed(prompt.encode())
            self.assertEqual(json.loads(handoff.read_text())['user_code'], 'ABCD-EFGHI')
            rendered = terminal.text()
            sanitized = wizard.Redactor(terminal.private_values).clean(
                rendered + '\nwrapped again: ABCD-\nEFGHI\n'
            )
            self.assertNotIn('ABCD-EFGHI', sanitized)
            self.assertNotIn('EFGHI', sanitized)

    def test_device_handoff_waits_for_complete_code_and_split_ansi(self):
        for code in ('ABCD-EFGH', 'ABCD-EFGHI'):
            with self.subTest(code_shape=tuple(map(len, code.split('-')))):
                with tempfile.TemporaryDirectory(prefix='pr3792-handoff-') as temporary:
                    handoff = Path(temporary) / 'request.json'
                    terminal = wizard.WizardTerminal(
                        {'prompts': []}, {}, handoff_method='device', run_id='run12345',
                        handoff_path=handoff,
                        handoff_response_path=Path(temporary) / 'response.json',
                    )
                    terminal.feed(
                        b'https://auth.openai.com/codex/device\r\n'
                        b'2. Enter this one-time code (expires in 15 minutes)\r\n'
                        b'   \x1b[94mABCD-EFGH'
                    )
                    self.assertFalse(handoff.exists())
                    terminal.feed((code[len('ABCD-EFGH'):] + '\x1b[').encode())
                    self.assertFalse(handoff.exists())
                    terminal.feed(b'0m\r\n')
                    self.assertEqual(json.loads(handoff.read_text())['user_code'], code)
                    self.assertIn(code, terminal.private_values)

    def test_collector_rejects_original_and_uneven_device_codes(self):
        import hashlib
        for code in ('ABCD-EFGH', 'ABCD-EFGHI'):
            with self.subTest(code_shape=tuple(map(len, code.split('-')))):
                files = {
                    'result.json': json.dumps({'schema_version': 1}).encode(),
                    'terminal.txt': ('Use code ' + code).encode(),
                }
                manifest = {'schema_version': 1, 'run_id': 'run', 'sanitized': True, 'files': {}}
                manifest['files'] = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode='w') as archive:
                    for name, data in {**files, 'manifest.json': json.dumps(manifest).encode()}.items():
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                stream.seek(0)
                with tempfile.TemporaryDirectory(prefix='pr3792-collect-') as temporary:
                    with self.assertRaises(collector.ValidationError) as caught:
                        collector.collect(stream, Path(temporary) / 'out', Path(temporary) / 'result.json',
                                          'a' * 40, 'run', 1, None, 'codex', 'device', 'b' * 40)
                    self.assertEqual(caught.exception.code, 'credential-found')

    def test_retained_agent_must_report_codex_through_ncl(self):
        frames = [
            json.dumps({'ok': True, 'data': [{'id': 'group-1'}]}).encode(),
            json.dumps({'ok': True, 'data': {'provider': 'codex'}}).encode(),
        ]
        with patch.object(wizard.subprocess, 'check_output', side_effect=frames):
            self.assertEqual(
                wizard.verify_retained_provider_group(Path('/tmp/nanoclaw'), 'codex'),
                {'group_count': 1, 'provider': 'codex', 'verified_via': 'ncl'},
            )
        frames[-1] = json.dumps({'ok': True, 'data': {'provider': 'claude'}}).encode()
        with patch.object(wizard.subprocess, 'check_output', side_effect=frames):
            with self.assertRaises(wizard.Failure):
                wizard.verify_retained_provider_group(Path('/tmp/nanoclaw'), 'codex')

    def test_claude_subscription_uses_bound_private_response(self):
        scenario = {'prompts': []}
        with tempfile.TemporaryDirectory(prefix='pr3792-handoff-') as temporary:
            handoff = Path(temporary) / 'request.json'
            response = Path(temporary) / 'response.json'
            terminal = wizard.WizardTerminal(
                scenario, {}, handoff_method='subscription', run_id='run12345',
                handoff_path=handoff, handoff_response_path=response,
            )
            read_fd, write_fd = os.pipe()
            try:
                terminal.feed(b'Press Enter to continue, or edit the command first.\n$ claude setup-token')
                terminal.handle_handoff(write_fd)
                self.assertEqual(os.read(read_fd, 1), b'\r')
                terminal.feed(
                    b'Visit https://claude.ai/oauth/authorize?state=private-state\n'
                    b'Paste code here if prompted'
                )
                request = json.loads(handoff.read_text())
                response.write_text(json.dumps({
                    'run_id': request['run_id'], 'nonce': request['nonce'],
                    'authorization_code': 'private-auth-code',
                }))
                response.chmod(0o600)
                terminal.handle_handoff(write_fd)
                self.assertEqual(os.read(read_fd, len('private-auth-code') + 1), b'private-auth-code\r')
                self.assertFalse(response.exists())
                self.assertIn('private-auth-code', terminal.private_values)
                self.assertIn(request['authorization_url'], terminal.private_values)
            finally:
                os.close(read_fd)
                os.close(write_fd)

    def test_claude_subscription_rejects_world_readable_response(self):
        with tempfile.TemporaryDirectory(prefix='pr3792-handoff-') as temporary:
            handoff = Path(temporary) / 'request.json'
            response = Path(temporary) / 'response.json'
            terminal = wizard.WizardTerminal(
                {'prompts': []}, {}, handoff_method='subscription', run_id='run12345',
                handoff_path=handoff, handoff_response_path=response,
            )
            terminal.feed(
                b'claude setup-token\nhttps://claude.ai/oauth/authorize?state=private-state\n'
                b'Paste code here if prompted'
            )
            request = json.loads(handoff.read_text())
            response.write_text(json.dumps({
                'run_id': request['run_id'], 'nonce': request['nonce'],
                'authorization_code': 'private-auth-code',
            }))
            response.chmod(0o644)
            read_fd, write_fd = os.pipe()
            try:
                with self.assertRaises(wizard.Failure):
                    terminal.handle_handoff(write_fd)
            finally:
                os.close(read_fd)
                os.close(write_fd)

    def test_claude_url_waits_for_terminator_across_split_chunks(self):
        with tempfile.TemporaryDirectory(prefix='pr3792-handoff-') as temporary:
            handoff = Path(temporary) / 'request.json'
            terminal = wizard.WizardTerminal(
                {'prompts': []}, {}, handoff_method='subscription', run_id='run12345',
                handoff_path=handoff, handoff_response_path=Path(temporary) / 'response.json',
            )
            terminal.feed(b'claude setup-token\nVisit https://claude.ai/oauth/auth')
            self.assertFalse(handoff.exists())
            terminal.feed(b'orize?state=complete-private-state')
            self.assertFalse(handoff.exists())
            terminal.feed(b'\nPaste code here if prompted')
            self.assertEqual(
                json.loads(handoff.read_text())['authorization_url'],
                'https://claude.ai/oauth/authorize?state=complete-private-state',
            )

    def test_claude_wrapped_token_capture_requires_full_tail(self):
        terminal = wizard.WizardTerminal({'prompts': []}, {})
        token = 'sk-ant-oat' + ('A1_b-' * 22) + 'AA'
        wrapped = token[:47] + '\n' + token[47:93] + '\r\n' + token[93:]
        for offset in range(0, len(wrapped), 17):
            terminal.feed(wrapped[offset:offset + 17].encode())
        self.assertIn(token, terminal.private_values)

    def test_claude_physically_wrapped_url_is_reassembled(self):
        with tempfile.TemporaryDirectory(prefix='pr3792-handoff-') as temporary:
            handoff = Path(temporary) / 'request.json'
            terminal = wizard.WizardTerminal(
                {'prompts': []}, {}, handoff_method='subscription', run_id='run12345',
                handoff_path=handoff, handoff_response_path=Path(temporary) / 'response.json',
            )
            terminal.feed(
                b'claude setup-token\nhttps://claude.ai/oauth/authorize?client_id=abc&\n'
                b'state=complete-private-state\nPaste code here if prompted'
            )
            self.assertEqual(
                json.loads(handoff.read_text())['authorization_url'],
                'https://claude.ai/oauth/authorize?client_id=abc&state=complete-private-state',
            )


if __name__ == '__main__':
    unittest.main()
