"""Exercise the wizard adapter across the real lifecycle's SSH boundary."""
import json
import shutil
from pathlib import Path
import subprocess
import sys

from test_e2e import PYTHON, Sandbox

DRIVER = Path(__file__).resolve().parents[1] / 'skills/e2e-wizard/scripts/proxmox-wizard.py'


class WizardProxmoxTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.key = self.root / 'credential'
        self.key.write_text('sk-ant-api03-fake-private-fixture')
        self.key.chmod(0o600)
        self.report = self.root / 'result.json'
        self.calls = self.root / 'calls.jsonl'
        self.git('remote', 'add', 'origin', 'https://github.com/example/nanoclaw.git')
        self.env.update(MOCK_CALLS=str(self.calls), MOCK_STATE=str(self.root / 'state.json'), MOCK_COMMIT=self.commit)
        self.executable('ssh', PYTHON + r'''
import hashlib, io, json, os, shlex, sys, tarfile
from pathlib import Path
args = shlex.split(sys.argv[-1])
data = sys.stdin.buffer.read().decode()
with open(os.environ['MOCK_CALLS'], 'a') as out:
    out.write(json.dumps({'args': args, 'input': data}) + '\n')
state = Path(os.environ['MOCK_STATE'])
mode = os.environ.get('MOCK_MODE', 'pass')
if args == ['id', '-u']: print('0')
elif args == ['pveversion']: print('pve-manager/9.2.18/test')
elif args[:3] == ['pvesh', 'get', '/cluster/resources']: print('[{"vmid":101}]')
elif args[:3] == ['pvesh', 'get', '/cluster/nextid']: print('"108"')
elif args[:2] == ['pvesm', 'path']: print('/templates/debian.tar.zst')
elif args[:2] == ['pct', 'create']: state.write_text(json.dumps(dict(zip(args[4::2], args[5::2]))))
elif args[:2] == ['pct', 'config']:
    fields = json.loads(state.read_text())
    for key in ('hostname', 'description', 'unprivileged', 'features'):
        print(key + ': ' + fields['--' + key])
elif args[:2] == ['pct', 'exec']:
    script = args[-1]
    if 'exec runuser' in script and mode == 'product-failure': sys.exit(2)
    if script.startswith('cat /opt/nanoclaw/logs/e2e/result.json') or script.startswith('tar -C /opt/nanoclaw/logs/e2e-wizard'):
        failed = mode == 'product-failure'
        run_id = json.loads(state.read_text())['--description'].removeprefix('nanoclaw-e2e-')
        result = {'schema_version': 1, 'mode': 'wizard', 'run_id': run_id,
                  'commit': os.environ['MOCK_COMMIT'], 'status': 'failed' if failed else 'pass',
                  'exit_code': 2 if failed else 0, 'ping': 'ok', 'phase': 'wizard' if failed else 'complete',
                  'wizard_completed': not failed, 'retained_reply_verified': not failed,
                  'service': {'checkout_verified': True, 'socket_connected': True}}
        if script.startswith('cat '): print(json.dumps(result))
        else:
            if mode == 'missing-proof': result['retained_reply_verified'] = False
            files = {'result.json': json.dumps(result).encode(), 'choices.json': b'[]',
                     'terminal.txt': b'rendered terminal', 'setup-logs/setup.log': b'progress'}
            manifest = {'schema_version': 1, 'sanitized': True, 'run_id': run_id,
                        'files': {k: hashlib.sha256(v).hexdigest() for k,v in files.items()}}
            if mode == 'corrupt-export': files['terminal.txt'] = b'changed after hashing'
            files['manifest.json'] = json.dumps(manifest).encode()
            with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
                for name, content in files.items():
                    item = tarfile.TarInfo(name); item.size = len(content)
                    archive.addfile(item, io.BytesIO(content))
elif args[:2] not in (['test', '-r'], ['ip', 'link'], ['pct', 'start']): sys.exit('unexpected boundary')
''')

    def run_driver(self, *extra, mode='pass'):
        self.env['MOCK_MODE'] = mode
        return subprocess.run([sys.executable, str(DRIVER), '--host', 'root@pve.example.test',
            '--template', 'local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst',
            '--storage', 'local-lvm', '--bridge', 'vmbr0', '--key-file', str(self.key),
            '--result-file', str(self.report), *extra], cwd=self.checkout, env=self.env,
            text=True, capture_output=True, timeout=20)

    def commands(self):
        return [json.loads(x) for x in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_pass_requires_validated_local_wizard_evidence(self):
        run = self.run_driver()
        self.assertEqual(run.returncode, 0, run.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['mode'], 'wizard')
        self.assertTrue(report['wizard']['retained_reply_verified'])
        self.assertEqual(report['guest']['ctid'], 108)
        self.assertTrue((Path(str(self.report) + '.artifacts') / 'terminal.txt').is_file())
        self.assertFalse(any(c['args'][:2] in (['pct', 'stop'], ['pct', 'destroy'], ['pct', 'clone']) for c in self.commands()))

    def test_product_failure_retains_guest_and_sanitized_artifacts(self):
        run = self.run_driver(mode='product-failure')
        self.assertEqual(run.returncode, 2, run.stderr)
        report = json.loads(self.report.read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['wizard']['status'], 'failed')
        self.assertTrue(report['guest']['creation_confirmed'])

    def test_corrupt_export_and_missing_proof_never_pass(self):
        for mode in ('corrupt-export', 'missing-proof'):
            with self.subTest(mode=mode):
                run = self.run_driver(mode=mode)
                self.assertEqual(run.returncode, 74, run.stderr)
                self.assertEqual(json.loads(self.report.read_text())['status'], 'failed')
                self.assertFalse(Path(str(self.report) + '.artifacts').exists())

    def test_dry_run_does_not_connect_or_read_credential(self):
        self.key.unlink()
        run = self.run_driver('--dry-run')
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.commands(), [])
        self.assertEqual(json.loads(self.report.read_text())['status'], 'planned')

    def test_existing_guest_is_rejected_before_mutation(self):
        run = self.run_driver('--ctid', '101')
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(any(c['args'][0] == 'pct' for c in self.commands()))

    def test_prompt_settings_are_not_forwarded_and_credential_stays_in_stdin(self):
        self.env['NANOCLAW_ONECLI_API_TOKEN'] = 'must-not-forward'
        run = self.run_driver()
        self.assertEqual(run.returncode, 0, run.stderr)
        calls = self.commands()
        self.assertTrue(any(c['input'] == self.key.read_text() for c in calls))
        self.assertNotIn(self.key.read_text(), run.stdout + run.stderr + self.report.read_text() + json.dumps([c['args'] for c in calls]))
        self.assertNotIn('must-not-forward', json.dumps(calls))
        installer = next(c['input'] for c in calls if 'WIZARD_BUNDLE' in c['input'])
        self.assertIn('wizard-install.sh', installer)
        self.assertNotIn('--step', installer)

    def test_invalid_artifact_destination_replaces_stale_pass(self):
        artifact = Path(str(self.report) + '.artifacts')
        artifact.mkdir()
        self.report.write_text('{"status":"pass"}')
        run = self.run_driver()
        self.assertEqual(run.returncode, 64)
        self.assertEqual(json.loads(self.report.read_text())['status'], 'failed')
        self.assertEqual(self.commands(), [])

    def test_unsupported_installer_replaces_stale_pass_without_ssh(self):
        self.report.write_text('{"status":"pass","run_id":"old"}')
        run = self.run_driver('--installer', str(self.root/'unused-installer'))
        self.assertEqual(run.returncode, 64, run.stderr)
        self.assertEqual(json.loads(self.report.read_text())['status'], 'failed')
        self.assertEqual(self.commands(), [])

    def test_missing_bundle_replaces_stale_pass_without_ssh(self):
        copied = self.root/'copied-skills'
        shutil.copytree(DRIVER.parents[2], copied, ignore=shutil.ignore_patterns('__pycache__'))
        (copied/'e2e-wizard/scenarios/fresh-cli.json').unlink()
        self.report.write_text('{"status":"pass","run_id":"old"}')
        run = subprocess.run([sys.executable, str(copied/'e2e-wizard/scripts/proxmox-wizard.py'),
            '--host','root@pve.example.test','--template','local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst',
            '--storage','local-lvm','--bridge','vmbr0','--result-file',str(self.report)],
            cwd=self.checkout, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 74, run.stderr)
        self.assertEqual(json.loads(self.report.read_text())['status'], 'failed')
        self.assertEqual(self.commands(), [])
