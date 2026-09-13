import importlib.util
import io
import json
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
    def test_wrapper_binds_guest_local_providers_ref_without_credential(self):
        commit = 'b6faffcfd83ee477ed8f477724985c78cce450eb'
        text = adapter.wrapper('a' * 32, 1200, 'codex', 'device', 'ignored', commit)
        self.assertIn('git cat-file -e ' + commit + '^{commit}', text)
        self.assertIn('update-ref refs/heads/providers ' + commit, text)
        self.assertIn('NANOCLAW_CHANNELS_REMOTE=e2e-payload', text)
        self.assertIn('--payload-ref ' + commit, text)
        self.assertNotIn('--credential-file', text)

    def test_device_code_redactor_removes_wrapped_code(self):
        redactor = wizard.Redactor(['ABCD-EFGH'])
        self.assertNotIn('ABCD', redactor.clean('code ABCD-\nEFGH'))

    def test_collector_rejects_unredacted_device_code(self):
        files = {
            'result.json': json.dumps({'schema_version': 1}).encode(),
            'terminal.txt': b'Use code ABCD-EFGH',
        }
        manifest = {'schema_version': 1, 'run_id': 'run', 'sanitized': True, 'files': {}}
        import hashlib
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
                wizard.verify_retained_codex_group(Path('/tmp/nanoclaw')),
                {'group_count': 1, 'provider': 'codex', 'verified_via': 'ncl'},
            )
        frames[-1] = json.dumps({'ok': True, 'data': {'provider': 'claude'}}).encode()
        with patch.object(wizard.subprocess, 'check_output', side_effect=frames):
            with self.assertRaises(wizard.Failure):
                wizard.verify_retained_codex_group(Path('/tmp/nanoclaw'))


if __name__ == '__main__':
    unittest.main()
