"""Windows adapter boundaries; real Git and shared artifact validation, no VM."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_e2e import Sandbox

DRIVER = Path(__file__).resolve().parents[1] / 'skills/e2e-windows/scripts/windows-run.py'
spec = importlib.util.spec_from_file_location('windows_driver', DRIVER)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class WindowsEnvironmentTests(unittest.TestCase):
    def test_only_regular_wsl2_linux_ext4_without_endpoint_overrides(self):
        good = ('Linux', '6.18.33.2-microsoft-standard-WSL2', 1000, 'ext4', {})
        driver.check_linux(*good)
        for index, value in [(0, 'Darwin'), (1, '4.4.0-Microsoft'), (2, 0),
                             (3, '9p'), (3, 'drvfs'), (3, 'overlay')]:
            with self.subTest(index=index, value=value):
                args = list(good); args[index] = value
                with self.assertRaises(driver.Failure):
                    driver.check_linux(*args)
        for name in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
            with self.subTest(name=name), self.assertRaises(driver.Failure):
                driver.check_linux(*good[:4], {name: 'remote-override'})

    def test_windows_and_linux_must_prove_same_local_desktop_engine(self):
        linux = {'ID': 'local-id', 'OSType': 'linux', 'OperatingSystem': 'Docker Desktop',
                 'KernelVersion': '6.18-microsoft-standard-WSL2'}
        windows = {'status': 'passed', 'dockerEngineID': 'local-id',
                   'dockerEndpoint': 'npipe:////./pipe/dockerDesktopLinuxEngine',
                   'operatingSystem': 'Docker Desktop', 'osType': 'linux',
                   'kernelVersion': '6.18-microsoft-standard-WSL2'}
        driver.check_engines(linux, windows)
        for target, changes in [('linux', {'ID': 'different'}), ('linux', {'ID': ''}),
                                ('linux', {'OperatingSystem': 'Ubuntu'}),
                                ('linux', {'KernelVersion': '6.8.0-cloud'}),
                                ('windows', {'dockerEndpoint': 'tcp://remote:2375'}),
                                ('windows', {'kernelVersion': 'different-kernel'}),
                                ('windows', {'status': 'running'}),
                                ('windows', {'osType': 'windows'})]:
            with self.subTest(target=target, changes=changes), self.assertRaises(driver.Failure):
                driver.check_engines(linux | changes if target == 'linux' else linux,
                                     windows | changes if target == 'windows' else windows)

    def test_bind_probe_requires_exact_container_answer(self):
        driver.check_bind_result('unique-marker', 'unique-marker\n')
        for output in ('', 'Hello from Docker!', 'cat /probe/marker.txt', 'other-marker'):
            with self.subTest(output=output), self.assertRaises(driver.Failure):
                driver.check_bind_result('unique-marker', output)

    def test_command_failure_does_not_export_child_output(self):
        with self.assertRaises(driver.Failure) as caught:
            driver.call([sys.executable, '-c', "import sys; print('private-value',file=sys.stderr);sys.exit(1)"])
        self.assertNotIn('private-value', str(caught.exception))

    def test_windows_artifact_directory_rejected_before_docker_probe(self):
        def filesystem(argv, **kwargs):
            self.assertEqual(argv[0], 'findmnt')
            return '9p' if argv[-1] == '/mnt/c/artifacts' else 'ext4'
        with patch.object(driver, 'call', side_effect=filesystem), \
             patch.object(driver.platform, 'system', return_value='Linux'), \
             patch.object(driver.platform, 'release', return_value='6.18-microsoft-standard-WSL2'), \
             patch.object(driver.os, 'getuid', return_value=1000), \
             patch.dict(os.environ, {'HOME': '/home/test'}, clear=True):
            with self.assertRaisesRegex(driver.Failure, 'ext4'):
                driver.qualification(Path('/opt/nanoclaw'), Path('/home/test/results'), Path('/mnt/c/artifacts'))


class WindowsRunnerTests(Sandbox):
    def setUp(self):
        super().setUp()
        (self.checkout / 'nanoclaw.sh').write_text('exit 0\n')
        self.commit = self.commit_change('wizard source')
        self.key = self.root / 'credential'
        self.key.write_text('sk-ant-api03-fake-private-fixture')
        self.key.chmod(0o600)
        self.report = self.root / 'windows.json'
        self.artifacts = Path(str(self.report) + '.artifacts')
        self.qualify = patch.object(driver, 'qualification', return_value={'status': 'passed'})
        self.qualification = self.qualify.start()
        self.addCleanup(self.qualify.stop)
        self.launch = patch.object(driver, 'run_wizard', side_effect=self.fake_wizard)
        self.launched = self.launch.start()
        self.addCleanup(self.launch.stop)
        self.fixture_mode = 'pass'

    def fake_wizard(self, command, root):
        self.assertEqual(root, self.checkout.resolve())
        self.assertEqual(command[:2], ['bash', str(driver.WIZARD / 'wizard-install.sh')])
        self.assertNotIn('--step', command)
        self.assertNotIn(self.key.read_text(), json.dumps(command))
        options = dict(zip(command[2::2], command[3::2]))
        destination = Path(options['--artifacts-dir']); destination.mkdir()
        failed = self.fixture_mode == 'product-failure'
        code = 2 if failed else 0
        result = {'schema_version': 1, 'mode': 'wizard', 'run_id': options['--run-id'],
                  'commit': self.commit, 'status': 'failed' if failed else 'pass',
                  'exit_code': code, 'wizard_completed': not failed,
                  'retained_reply_verified': not failed,
                  'service': {'checkout_verified': True, 'socket_connected': True}}
        if self.fixture_mode == 'missing-reply':
            result['retained_reply_verified'] = False
        if self.fixture_mode == 'wrong-commit':
            result['commit'] = '0' * 40
        files = {'result.json': json.dumps(result), 'choices.json': '[]',
                 'terminal.txt': 'computed answer', 'setup-logs/setup.log': 'progress'}
        if self.fixture_mode == 'secret-leak':
            files['terminal.txt'] = self.key.read_text()
        manifest = {'schema_version': 1, 'sanitized': True, 'run_id': options['--run-id'],
                    'files': {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}}
        if self.fixture_mode == 'corrupt-export':
            files['terminal.txt'] = 'modified after hashing'
        for name, content in (files | {'manifest.json': json.dumps(manifest)}).items():
            path = destination / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
        return code

    def run_driver(self, *args):
        return driver.main(['--root', str(self.checkout), '--key-file', str(self.key),
                            '--result-file', str(self.report), *args])

    def test_pass_requires_shared_collector_acceptance(self):
        self.assertEqual(self.run_driver(), 0)
        result = json.loads(self.report.read_text())
        self.assertEqual(result['status'], 'pass')
        self.assertTrue(result['wizard']['retained_reply_verified'])
        self.assertEqual(result['commit'], self.commit)
        self.assertTrue((self.artifacts / 'manifest.json').is_file())
        self.assertEqual(self.report.stat().st_mode & 0o777, 0o600)
        self.assertTrue(Path(result['retained_work']).is_dir())

    def test_environment_only_does_not_read_key_or_launch_wizard(self):
        self.key.unlink()
        self.assertEqual(self.run_driver('--preflight-only'), 0)
        result = json.loads(self.report.read_text())
        self.assertEqual(result['status'], 'qualified')
        self.assertFalse(result['wizard_started'])
        self.launched.assert_not_called()
        self.assertFalse(self.artifacts.exists())

    def test_environment_failure_replaces_stale_pass_before_wizard(self):
        self.report.write_text(json.dumps({'schema_version': 1, 'mode': 'windows-wsl-wizard', 'status': 'pass'}))
        self.qualification.side_effect = driver.Failure('Docker integration is disabled')
        self.assertEqual(self.run_driver(), 1)
        result = json.loads(self.report.read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['wizard_started'])
        self.launched.assert_not_called()

    def test_product_failure_preserves_sanitized_evidence_and_exit_code(self):
        self.fixture_mode = 'product-failure'
        self.assertEqual(self.run_driver(), 2)
        result = json.loads(self.report.read_text())
        self.assertEqual(result['wizard']['status'], 'failed')
        self.assertTrue((self.artifacts / 'terminal.txt').exists())
        self.assertTrue(self.key.exists())

    def test_invalid_export_or_missing_proof_cannot_pass(self):
        for mode in ('missing-reply', 'wrong-commit', 'corrupt-export', 'secret-leak'):
            with self.subTest(mode=mode):
                self.fixture_mode = mode
                self.assertEqual(self.run_driver(), 74)
                result = json.loads(self.report.read_text())
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['phase'], 'export')
                self.assertFalse(self.artifacts.exists())
                self.assertNotIn(self.key.read_text(), self.report.read_text())

    def test_prior_product_state_rejected_before_environment_probe(self):
        (self.checkout / '.env').write_text('existing configuration')
        self.assertEqual(self.run_driver(), 1)
        self.qualification.assert_not_called()
        self.launched.assert_not_called()
        self.assertEqual((self.checkout / '.env').read_text(), 'existing configuration')

    def test_wrong_ref_and_tracked_edits_rejected(self):
        self.assertEqual(self.run_driver('--ref', 'HEAD~1'), 1)
        (self.checkout / 'nanoclaw.sh').write_text('changed\n')
        self.assertEqual(self.run_driver(), 1)
        self.qualification.assert_not_called()

    def test_unsafe_key_rejected_without_wizard(self):
        self.key.chmod(0o644)
        self.assertEqual(self.run_driver(), 1)
        self.key.unlink(); self.key.symlink_to(self.checkout / 'package.json')
        self.assertEqual(self.run_driver(), 1)
        self.launched.assert_not_called()

    def test_unrelated_output_file_preserved(self):
        self.report.write_text('existing document')
        with self.assertRaises(SystemExit):
            self.run_driver()
        self.assertEqual(self.report.read_text(), 'existing document')
        self.launched.assert_not_called()

    def test_credential_path_cannot_be_used_as_result_even_when_missing(self):
        self.key.unlink()
        with self.assertRaises(SystemExit):
            self.run_driver('--result-file', str(self.key))
        self.assertFalse(self.key.exists())
        self.launched.assert_not_called()


class WindowsCancellationTests(unittest.TestCase):
    def test_term_reaches_child_cleanup_handler(self):
        # Real processes prove the wrapper forwards termination; the shared
        # wizard tests independently cover cleanup of its PTY descendants.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ready, cleaned = root / 'ready', root / 'cleaned'
            child = root / 'child.py'
            child.write_text('import signal,time\nfrom pathlib import Path\n'
                f'def stop(*args):\n Path({str(cleaned)!r}).write_text("yes"); raise SystemExit(130)\n'
                'signal.signal(signal.SIGTERM,stop)\n'
                f'Path({str(ready)!r}).write_text("yes")\n'
                'while True: time.sleep(0.02)\n')
            wrapper = root / 'wrapper.py'
            wrapper.write_text('import importlib.util,sys\nfrom pathlib import Path\n'
                f's=importlib.util.spec_from_file_location("driver",{str(DRIVER)!r})\n'
                'm=importlib.util.module_from_spec(s);s.loader.exec_module(m)\n'
                f'try: m.run_wizard([sys.executable,{str(child)!r}],Path({str(root)!r}))\n'
                'except KeyboardInterrupt: sys.exit(130)\n')
            process = subprocess.Popen([sys.executable, str(wrapper)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                import time
                for _ in range(150):
                    if ready.exists(): break
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                process.send_signal(signal.SIGTERM)
                self.assertEqual(process.wait(timeout=12), 130)
                self.assertTrue(cleaned.exists())
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()


if __name__ == '__main__':
    unittest.main()
