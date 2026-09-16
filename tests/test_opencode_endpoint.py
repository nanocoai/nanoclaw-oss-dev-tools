"""Custom endpoint prompt order, key transport and acceptance (no live inference)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_opencode
import test_wizard

options = test_opencode.options
wizard = test_opencode.wizard
ENDPOINT = 'https://models.example.test:8443/v1'
KEY = 'synthetic+/endpoint=key:12345'


class EndpointTests(unittest.TestCase):
    def test_custom_endpoint_requires_explicit_key_selection_in_all_launchers(self):
        import test_windows
        import test_wizard_proxmox
        common = ['--provider', 'opencode', '--auth-method', 'custom',
                  '--opencode-base-url', ENDPOINT, '--opencode-model', 'openai/model']
        fixture = test_wizard.WizardDriverTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        run = subprocess.run(['bash', str(fixture.fixture.driver), '--interactive',
                              '--result-file', str(fixture.result), *common],
                             cwd=fixture.fixture.checkout, env=fixture.fixture.env,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 66, run.stderr)
        self.assertIn('explicit --credential-file', run.stderr)
        self.assertFalse(fixture.fixture.calls())
        prox = test_wizard_proxmox.WizardProxmoxTests()
        prox.setUp(); self.addCleanup(prox.doCleanups)
        run = subprocess.run([sys.executable, str(test_wizard_proxmox.DRIVER), *common,
                              '--host', 'root@pve.example.test', '--template', 'local:vztmpl/debian.tar.zst',
                              '--storage', 'local-lvm', '--bridge', 'vmbr0', '--result-file', str(prox.report)],
                             cwd=prox.checkout, env=prox.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 66, run.stderr)
        self.assertIn('explicit --credential-file', run.stderr)
        self.assertFalse(prox.commands())
        windows = test_windows.WindowsRunnerTests()
        windows.setUp(); self.addCleanup(windows.doCleanups)
        code = test_windows.driver.main(['--root', str(windows.checkout), *common,
                                        '--result-file', str(windows.report)])
        self.assertNotEqual(code, 0)
        self.assertIn('explicit --credential-file', json.loads(windows.report.read_text())['error'])
        windows.launched.assert_not_called()

    def test_custom_and_local_provider_defaults_and_api_schemes(self):
        for backend, scheme in [('local', None), ('custom', None), ('custom', 'openai'),
                                ('custom', 'google'), ('custom', 'anthropic'),
                                ('custom', 'openrouter'), ('custom', 'deepseek')]:
            with self.subTest(backend=backend, scheme=scheme):
                self.assertEqual(options.validate_model('opencode', backend, (scheme or 'openai') + '/model',
                                                        ENDPOINT, scheme), scheme or 'openai')
        self.assertEqual(options.validate_model('opencode', 'local', 'openai/model', 'http://[::1]:8000/v1'), 'openai')

    def test_rejects_incomplete_or_conflicting_connection_before_setup(self):
        for backend, model, url, scheme in [
            ('custom', 'openai/model', None, None),
            ('custom', 'anthropic/model', ENDPOINT, None),
            ('custom', 'other/model', ENDPOINT, 'other'),
            ('local', 'google/model', ENDPOINT, 'google'),
            ('openrouter', 'openrouter/model', ENDPOINT, None),
            ('deepseek', 'deepseek/model', None, 'deepseek'),
        ]:
            with self.subTest(backend=backend, model=model), self.assertRaises(options.DiscoveryError):
                options.validate_model('opencode', backend, model, url, scheme)
        for url in ('file:///tmp/models', 'https://user:password@example.test/v1',
                    'https://example.test/v1?api_key=secret', 'https://example.test/v1#secret',
                    'https://example.test/v1\n', 'http://example.test:invalid/v1',
                    'http://example.test:99999/v1', 'http://example.test:0/v1', 'https:///v1'):
            with self.subTest(url=url), self.assertRaises(options.DiscoveryError):
                options.validate_model('opencode', 'custom', 'openai/model', url)

    def test_api_key_is_opaque_and_control_characters_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'key'
            path.write_text(KEY + '\n'); path.chmod(0o600)
            self.assertEqual(wizard.credential_for(path, 'opencode-api-key'), KEY)
            self.assertNotIn(KEY, wizard.Redactor([KEY]).clean('API key: ' + KEY))
            for value in ('short', KEY + '\x1b[A', KEY + '\nsecond-line', 'key with spaces'):
                path.write_text(value)
                with self.subTest(value=value), self.assertRaises(wizard.Failure):
                    wizard.credential_for(path, 'opencode-api-key')

    def test_saved_endpoint_and_api_scheme_must_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = ('OPENCODE_PROVIDER=openai\nOPENCODE_MODEL=openai/model\n'
                   'OPENCODE_SMALL_MODEL=openai/model\nOPENCODE_BASE_URL=' + ENDPOINT + '\n')
            (root / '.env').write_text(env)
            with patch.object(wizard, 'verify_retained_provider_group', return_value={'provider': 'opencode'}):
                receipt = wizard.verify_opencode_target(root, 'custom', 'openai/model', ENDPOINT)
                self.assertEqual(receipt['base_url'], ENDPOINT)
                self.assertEqual(receipt['model_provider'], 'openai')
                for extra in ('OPENCODE_BASE_URL=https://wrong.example.test/v1\n', 'OPENCODE_PROVIDER=anthropic\n'):
                    (root / '.env').write_text(env + extra)
                    with self.assertRaises(wizard.Failure):
                        wizard.verify_opencode_target(root, 'custom', 'openai/model', ENDPOINT)

    def test_real_pty_uses_key_before_catalog_for_openai_and_after_model_for_anthropic(self):
        fixture = r'''
import os, sys, tty
backend, scheme, endpoint, key = sys.argv[1:]
tty.setraw(0)
def out(value): os.write(1, value.encode())
def line():
    data = b''
    while True:
        char = os.read(0, 1)
        if char == b'\r': return data.decode()
        data += char

def text(prompt, expected):
    out('\x1b[2J\x1b[H◆ ' + prompt + '\r\n│ \r\n└\r\n')
    assert line() == expected, prompt

def select(prompt, label):
    out('\x1b[2J\x1b[H◆ ' + prompt + '\r\n│ ● ' + label + '\r\n└\r\n')
    assert line() == '', prompt

select('Which model backend should OpenCode use?', 'Local or self-hosted' if backend == 'local' else 'Something else')
if backend == 'custom': text('OpenCode provider id', scheme)
text('OpenAI-compatible base URL (include /v1)' if backend == 'local' else
     'Custom API base URL (leave blank for OpenCode native configuration)', endpoint)
if scheme == 'openai':
    out('\x1b[2J\x1b[H◆ Does this endpoint work without an API key?\r\n│ ● Yes / ○ No\r\n└\r\n')
    assert os.read(0, 3) == b'\x1b[B', 'keyless default was accepted'
    out('\x1b[2J\x1b[H◆ Does this endpoint work without an API key?\r\n│ ○ Yes / ● No\r\n└\r\n')
    assert line() == ''
    text('API key', key)
select('Which default model should OpenCode use?', 'Enter a model id manually')
text('Model id in provider/model form', scheme + '/model')
if scheme != 'openai': text('API key', key)
out('\x1b[2J\x1b[HFixture complete\r\n')
'''
        for backend, scheme in [('local', 'openai'), ('custom', 'openai'), ('custom', 'anthropic')]:
            with self.subTest(backend=backend, scheme=scheme), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); script = root / 'endpoint-pty.py'; script.write_text(fixture)
                scenario = json.loads((test_wizard.SKILL / 'scenarios/fresh-cli.json').read_text())
                values = {'auth_prompt': 'Which model backend should OpenCode use?', 'credential': KEY,
                          'auth_option': 'Local or self-hosted' if backend == 'local' else 'Something else'}
                wizard.configure_opencode_scenario(scenario, values, backend, scheme + '/model', ENDPOINT,
                                                    scheme if backend == 'custom' else None)
                scenario['prompts'] = [p for p in scenario['prompts'] if p['id'] in ('auth', 'credential')
                                       or p['id'].startswith('opencode-')]
                scenario['completed_text'] = 'Fixture complete'
                terminal = wizard.WizardTerminal(scenario, values, timeout=12, idle_timeout=3)
                terminal.reply_verified = True  # This fixture qualifies prompts only.
                terminal.run(root, [sys.executable, str(script), backend, scheme, ENDPOINT, KEY])
                self.assertNotIn(KEY, json.dumps(terminal.choices))
                ids = [item['id'] for item in terminal.choices]
                self.assertEqual(ids.index('credential') < ids.index('opencode-model'), scheme == 'openai')


class EndpointEvidenceTests(unittest.TestCase):
    block = staticmethod(test_wizard.EvidenceTests.block)
    export = test_wizard.EvidenceTests.export
    archive = test_wizard.EvidenceTests.archive

    def setUp(self):
        test_opencode.OpenCodeEvidenceTests.setUp(self)
        self.result.update(auth_method='custom', opencode_model='openai/model',
                           opencode_base_url=ENDPOINT, opencode_provider='openai')
        self.result['opencode_target_receipt'].update(backend='custom', model='openai/model',
                                                     base_url=ENDPOINT, model_provider='openai')

    def collect(self, destination, endpoint=ENDPOINT, scheme='openai'):
        test_wizard.collector.collect(self.archive(destination), self.root / 'collected', self.root / 'result.json',
            'a' * 40, 'regression123', 0, self.key, 'opencode', 'custom', 'a' * 40,
            opencode_model='openai/model', opencode_base_url=endpoint, opencode_provider=scheme)

    def test_collection_binds_endpoint_and_scheme_then_accepts_matching_proof(self):
        destination = self.export()
        for endpoint, scheme in [('https://wrong.example.test/v1', 'openai'), (ENDPOINT, 'anthropic')]:
            with self.subTest(endpoint=endpoint, scheme=scheme), self.assertRaises(test_wizard.collector.ValidationError) as caught:
                self.collect(destination, endpoint, scheme)
            self.assertEqual(caught.exception.code, 'provider-selection-mismatch')
        self.collect(destination)

    def test_incorrect_saved_endpoint_is_rejected_even_with_matching_invocation(self):
        self.result['opencode_target_receipt']['base_url'] = 'https://wrong.example.test/v1'
        with self.assertRaises(test_wizard.collector.ValidationError) as caught:
            self.collect(self.export())
        self.assertEqual(caught.exception.code, 'acceptance-evidence-missing')


if __name__ == '__main__':
    unittest.main()
