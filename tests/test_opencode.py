"""OpenCode bundled provider contracts and rendered wizard inputs (offline)."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_provider_options
import test_wizard

options = test_provider_options.provider_options
wizard = test_wizard.wizard


class BundledProviderTests(unittest.TestCase):
    git = test_provider_options.ProviderOptionsTests.git
    skill = test_provider_options.ProviderOptionsTests.skill

    def setUp(self):
        test_provider_options.ProviderOptionsTests.setUp(self)
        self.skill('add-opencode', 'opencode', 'OpenCode', 'Open-source provider router', True, 'unused')
        self.skill_dir = self.root / '.claude/skills/add-opencode'
        path = self.skill_dir / 'SKILL.md'
        path.write_text(path.read_text().split('```')[0] + '''```nc:copy when:opencode_core_ready=yes
payload/setup/providers/opencode.ts -> setup/providers/opencode.ts
payload/scripts/opencode-auth.ts -> scripts/opencode-auth.ts
```
''')
        registration = self.skill_dir / 'payload/setup/providers/opencode.ts'
        registration.parent.mkdir(parents=True)
        registration.write_text('''registerSetupProvider({
value: 'opencode', label: 'OpenCode', hint: 'Open-source provider router',
runAuth: async () => { const auth = await import('../../scripts/opencode-auth.js');
await auth.runOpenCodeSetupAuth(); },
});
''')
        auth = self.skill_dir / 'payload/scripts/opencode-auth.ts'
        auth.parent.mkdir(parents=True)
        # Same delegated backend choice and nested ChatGPT choice as main f5967d3c.
        auth.write_text('''const backend = await brightSelect<Backend>({
message: 'Which model backend should OpenCode use?', options: [
{ value: 'chatgpt', label: 'ChatGPT subscription' },
{ value: 'local', label: 'Local or self-hosted' },
{ value: 'openrouter', label: 'OpenRouter', hint: 'API key stored in OneCLI' },
{ value: 'deepseek', label: 'DeepSeek', hint: 'API key stored in OneCLI' },
{ value: 'custom', label: 'Something else' },
...(options.allowSkip === false ? [] : [{value: 'skip' as const, label: 'Skip for now'}]),
] }); setupLog.userInput('opencode_backend', backend);
const method = await brightSelect({message: 'How would you like to connect ChatGPT?',
options: [{value: 'device', label: 'Device pairing'}, {value: 'browser', label: 'Browser sign-in'}]});
setupLog.userInput('opencode_chatgpt_auth_method', method);
''')
        self.git('add', '.')
        self.git('commit', '-m', 'bundled OpenCode')
        self.commit = self.git('rev-parse', 'HEAD')

    def test_backend_discovery_uses_bundled_commit_and_delegated_auth(self):
        selected = options.discover(self.root, 'opencode')['selected']
        self.assertEqual(selected['auth_source_commit'], self.commit)
        self.assertEqual(selected['payload_kind'], 'bundled')
        self.assertEqual(selected['auth_input_key'], 'opencode_backend')
        self.assertEqual(selected['auth_source'], '.claude/skills/add-opencode/payload/scripts/opencode-auth.ts')
        methods = {item['value']: item for item in selected['auth_methods']}
        self.assertNotIn('device', methods)
        self.assertEqual(methods['openrouter']['credential_kind'], 'opencode-api-key')
        for backend in ('custom', 'local'):
            self.assertEqual(methods[backend]['automation'], 'credential-file')
        self.assertFalse(methods['skip']['usable_for_e2e'])
        # Dirty edits and an unrelated branch cannot silently alter the tested payload.
        (self.skill_dir / 'payload/scripts/opencode-auth.ts').write_text('broken')
        self.assertEqual(options.discover(self.root, 'opencode')['selected'], selected)
        with self.assertRaises(options.DiscoveryError):
            options.discover(self.root, 'opencode', payload_ref='provider-payload')

    def test_installed_payload_compares_each_destination_to_bundled_source(self):
        selected = options.discover(self.root, 'opencode')['selected']
        for entry in selected['payload_files']:
            target = self.root / entry['destination']
            target.parent.mkdir(exist_ok=True, parents=True)
            shutil.copyfile(self.root / entry['source'], target)
        receipt = wizard.verify_provider_payload(self.root, selected)
        self.assertEqual(receipt['commit'], self.commit)
        self.assertEqual(receipt['file_count'], 2)
        (self.root / 'scripts/opencode-auth.ts').write_text('tampered')
        with self.assertRaises(wizard.Failure):
            wizard.verify_provider_payload(self.root, selected)

    def test_unsafe_or_mixed_copy_sources_are_rejected(self):
        for declaration in ('payload/x -> ../escape', '/absolute -> safe'):
            with self.subTest(declaration=declaration), self.assertRaises(options.DiscoveryError):
                options.copy_entries('```nc:copy\n' + declaration + '\n```', 'skill/SKILL.md')
        with self.assertRaises(options.DiscoveryError):
            options.copy_entries('```nc:copy\na -> b\n```\n```nc:copy from-branch:foo\nx -> y\n```', 'skill/SKILL.md')


class OpenCodeWizardTests(unittest.TestCase):
    def test_model_validation_rejects_missing_mismatched_and_shell_inputs(self):
        options.validate_model('opencode', 'openrouter', 'openrouter/vendor/model:free')
        options.validate_model('opencode', 'deepseek', 'deepseek/model')
        for backend, model in [('openrouter', None), ('deepseek', 'openrouter/model'),
                               ('openrouter', 'openrouter/$(secret)'), ('chatgpt', 'openai/model')]:
            with self.subTest(backend=backend, model=model), self.assertRaises(options.DiscoveryError):
                options.validate_model('opencode', backend, model)
        with self.assertRaises(options.DiscoveryError):
            options.validate_model('claude', 'api', 'openrouter/model')

    def test_private_api_key_file_and_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'key'
            value = 'sk-or-v1-synthetic-opencode-credential-1234567890'
            path.write_text(value + '\n'); path.chmod(0o600)
            self.assertEqual(wizard.credential_for(path, 'opencode-api-key'), value)
            self.assertNotIn(value, wizard.Redactor([value]).clean('API key: ' + value))
            path.chmod(0o644)
            with self.assertRaises(wizard.Failure):
                wizard.credential_for(path, 'opencode-api-key')

    def test_real_pty_answers_backend_model_and_password_in_order(self):
        scenario = json.loads((test_wizard.SKILL / 'scenarios/fresh-cli.json').read_text())
        values = {'auth_prompt': 'Which model backend should OpenCode use?', 'auth_option': 'OpenRouter',
                  'credential': 'synthetic-opencode-key-12345678'}
        wizard.configure_opencode_scenario(scenario, values, 'openrouter', 'openrouter/vendor/model')
        scenario['prompts'] = [p for p in scenario['prompts'] if p['id'] in
                               ('auth', 'opencode-model-choice', 'opencode-model', 'credential')]
        scenario['completed_text'] = 'Fixture complete'
        fixture = r'''
import os, tty
assert os.isatty(0)
tty.setraw(0)
def out(s): os.write(1, s.encode())
def line():
    data = b''
    while True:
        c = os.read(0, 1)
        if c == b'\r': return data.decode()
        data += c
out('◆ Which model backend should OpenCode use?\r\n│ ● OpenRouter\r\n│ ○ DeepSeek\r\n└\r\n')
assert line() == ''
# A catalog larger than the screen must scroll before the manual choice exists.
out('\x1b[2J\x1b[H◆ Which default model should OpenCode use?\r\n│ ● openrouter/vendor/first\r\n└\r\n')
assert os.read(0, 3) == b'\x1b[B'
out('\x1b[2J\x1b[H◆ Which default model should OpenCode use?\r\n│ ● Enter a model id manually\r\n└\r\n')
assert line() == ''
out('\x1b[2J\x1b[H◆ Model id in provider/model form\r\n│ \r\n└\r\n')
assert line() == 'openrouter/vendor/model'
out('\x1b[2J\x1b[H◆ API key\r\n│ \r\n└\r\n')
assert line() == 'synthetic-opencode-key-12345678'
out('\x1b[2J\x1b[HFixture complete\r\n')
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); script = root / 'pty.py'; script.write_text(fixture)
            terminal = wizard.WizardTerminal(scenario, values, timeout=10, idle_timeout=3)
            # This fixture tests auth input only; the runner independently requires a retained reply.
            terminal.reply_verified = True
            terminal.run(root, [sys.executable, str(script)])
            self.assertEqual([c['id'] for c in terminal.choices],
                             ['auth', 'opencode-model-choice', 'opencode-model', 'credential'])
            self.assertEqual(terminal.choices[-1]['value'], '[REDACTED]')

    def test_defaults_and_effective_retained_provider_are_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            text = ('OPENCODE_PROVIDER=openrouter\nOPENCODE_MODEL=openrouter/vendor/model\n'
                    'OPENCODE_SMALL_MODEL=openrouter/vendor/model\nOPENCODE_BASE_URL=native\n')
            (root / '.env').write_text(text)
            with patch.object(wizard, 'verify_retained_provider_group', return_value={'provider': 'opencode'}) as retained:
                receipt = wizard.verify_opencode_target(root, 'openrouter', 'openrouter/vendor/model')
                retained.assert_called_once_with(root, 'opencode')
                self.assertEqual(receipt['model'], 'openrouter/vendor/model')
                for suffix in ('OPENCODE_AUTH_MODE=chatgpt\n', 'OPENCODE_MODEL=openrouter/wrong\n'):
                    (root / '.env').write_text(text + suffix)
                    with self.assertRaises(wizard.Failure):
                        wizard.verify_opencode_target(root, 'openrouter', 'openrouter/vendor/model')


class OpenCodeEvidenceTests(unittest.TestCase):
    block = staticmethod(test_wizard.EvidenceTests.block)
    export = test_wizard.EvidenceTests.export
    archive = test_wizard.EvidenceTests.archive

    def setUp(self):
        test_wizard.EvidenceTests.setUp(self)
        self.result.update(provider='opencode', auth_method='openrouter',
                           opencode_model='openrouter/vendor/model')
        self.result['provider_payload_receipt'] = {
            'commit': 'a' * 40, 'paths': {'scripts/opencode-auth.ts': 'f' * 64},
            'file_count': 1,
        }
        self.result['opencode_target_receipt'] = {
            'backend': 'openrouter', 'model': 'openrouter/vendor/model',
            'retained_agent': {'provider': 'opencode', 'group_count': 1,
                               'effective_providers': ['opencode']},
        }

    def collect(self, destination, model='openrouter/vendor/model'):
        test_wizard.collector.collect(
            self.archive(destination), self.root / 'collected', self.root / 'result.json',
            'a' * 40, 'regression123', 0, self.key, 'opencode', 'openrouter', 'a' * 40,
            opencode_model=model,
        )

    def test_export_requires_matching_model_payload_and_effective_provider(self):
        destination = self.export()
        with self.assertRaises(test_wizard.collector.ValidationError) as caught:
            self.collect(destination, model='openrouter/wrong')
        self.assertEqual(caught.exception.code, 'provider-selection-mismatch')
        self.collect(destination)
        result = json.loads((self.root / 'result.json').read_text())
        self.assertEqual(result['opencode_target_receipt']['retained_agent']['effective_providers'], ['opencode'])

    def test_missing_payload_or_wrong_runtime_cannot_be_accepted(self):
        original = json.loads(json.dumps(self.result))
        for change in ('payload', 'runtime'):
            with self.subTest(change=change):
                self.result = json.loads(json.dumps(original))
                if change == 'payload':
                    del self.result['provider_payload_receipt']
                else:
                    self.result['opencode_target_receipt']['retained_agent']['effective_providers'] = ['claude']
                destination = self.export()
                with self.assertRaises(test_wizard.collector.ValidationError) as caught:
                    self.collect(destination)
                self.assertEqual(caught.exception.code, 'acceptance-evidence-missing')
                shutil.rmtree(destination)


class OpenCodeLauncherTests(unittest.TestCase):
    @staticmethod
    def endpoint_flags(base_url, model_provider):
        return ((['--opencode-base-url', base_url] if base_url else [])
                + (['--opencode-provider', model_provider] if model_provider else []))

    def test_exe_transports_model_and_accepts_bound_opencode_evidence(self):
        self.check_exe()

    def test_exe_quotes_custom_endpoint_and_binds_it_to_evidence(self):
        self.check_exe('custom', 'openai/model', "http://models.example.test:8000/v1/$(printf${IFS}oops)'", 'openai')

    def test_proxmox_bundled_selection_needs_no_payload_remote(self):
        self.check_proxmox()

    def test_proxmox_transports_custom_endpoint_and_provider(self):
        self.check_proxmox('custom', 'anthropic/model', 'https://models.example.test/v1', 'anthropic')

    def test_windows_transports_model_and_validates_receipt(self):
        self.check_windows()

    def test_windows_transports_custom_endpoint_with_default_provider(self):
        self.check_windows('custom', 'openai/model', 'https://models.example.test/v1')

    def add_bundle(self, checkout):
        fixture = BundledProviderTests()
        fixture.setUp()
        try:
            shutil.copytree(fixture.skill_dir, checkout / '.claude/skills/add-opencode')
        finally:
            fixture.doCleanups()

    def check_exe(self, backend='openrouter', model='openrouter/vendor/model', base_url=None, model_provider=None):
        driver = test_wizard.WizardDriverTests()
        driver.setUp(); self.addCleanup(driver.doCleanups)
        f = driver.fixture
        self.add_bundle(f.checkout)
        f.commit = f.commit_change('OpenCode bundle')
        f.git('push', 'origin', 'HEAD:main')
        f.env['MOCK_COMMIT'] = f.commit
        path = driver.wizard_skill / 'scripts/wizard-install.sh'
        script = path.read_text()
        script = script.replace("if result['provider']=='codex':", "if result['provider'] in ('codex', 'opencode'):")
        script = script.replace("files={'terminal.txt'", """if result['provider']=='opencode':
 result['opencode_model']=options['--opencode-model']
 result['opencode_base_url']=options.get('--opencode-base-url','native')
 result['opencode_provider']=options.get('--opencode-provider','openai' if options['--auth-method'] in ('custom','local') else options['--auth-method'])
 result['opencode_target_receipt']={'backend':options['--auth-method'],'model':options['--opencode-model'],
  'base_url':result['opencode_base_url'],'model_provider':result['opencode_provider'],
  'retained_agent':{'provider':'opencode','group_count':1,'effective_providers':['opencode']}}
files={'terminal.txt'""")
        script = script.replace("manifest={'schema_version'", """if result['provider']=='opencode': files['opencode-target-receipt.json']=json.dumps(result['opencode_target_receipt'])
manifest={'schema_version'""")
        path.write_text(script)
        extra = self.endpoint_flags(base_url, model_provider)
        run = driver.run_driver('--provider', 'opencode', '--auth-method', backend,
                                '--opencode-model', model, *extra)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(driver.result.read_text())
        self.assertEqual(result['opencode_model'], model)
        self.assertEqual(result['opencode_base_url'], base_url or 'native')
        self.assertEqual(result['auth_source_commit'], f.commit)

    def check_proxmox(self, backend='deepseek', model='deepseek/model', base_url=None, model_provider=None):
        import test_wizard_proxmox
        f = test_wizard_proxmox.WizardProxmoxTests()
        f.setUp(); self.addCleanup(f.doCleanups)
        self.add_bundle(f.checkout)
        f.commit = f.commit_change('OpenCode bundle')
        run = f.run_driver('--dry-run', '--opencode-model', model, *self.endpoint_flags(base_url, model_provider),
                           provider='opencode', auth_method=backend)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(f.commands())
        report = json.loads(f.report.read_text())
        self.assertEqual(report['auth_source_commit'], f.commit)
        adapter = test_wizard.load('opencode_proxmox', test_wizard.SKILL / 'scripts/proxmox-wizard.py')
        command = adapter.wrapper('regression123', 1200, 'opencode', backend, None,
                                  f.commit, opencode_model=model, opencode_base_url=base_url, opencode_provider=model_provider)
        self.assertNotIn('git remote add e2e-payload', command)
        import shlex
        argv = shlex.split(command.splitlines()[-1])
        self.assertEqual(argv[argv.index('--opencode-model') + 1], model)
        if base_url:
            self.assertEqual(argv[argv.index('--opencode-base-url') + 1], base_url)
        checked = subprocess.run(['bash', '-n'], input=command, text=True, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def check_windows(self, backend='deepseek', model='deepseek/model', base_url=None, model_provider=None):
        import test_windows
        f = test_windows.WindowsRunnerTests()
        f.setUp(); self.addCleanup(f.doCleanups)
        self.add_bundle(f.checkout)
        f.commit = f.commit_change('OpenCode bundle')
        def run_wizard(command, root):
            result_code = f.fake_wizard(command, root)
            args = dict(zip(command[2::2], command[3::2]))
            self.assertEqual(args['--opencode-model'], model)
            self.assertEqual(args.get('--opencode-base-url'), base_url)
            self.assertEqual(args.get('--opencode-provider'), model_provider)
            self.assertNotIn('--payload-ref', args)
            dest = Path(args['--artifacts-dir'])
            result = json.loads((dest / 'result.json').read_text())
            actual_provider = model_provider or ('openai' if backend in ('custom','local') else backend)
            result.update(opencode_model=model, opencode_base_url=base_url or 'native', opencode_provider=actual_provider,
                          provider_payload_receipt={'commit': f.commit, 'paths': {'scripts/opencode-auth.ts': 'f'*64}, 'file_count': 1},
                          opencode_target_receipt={'backend': backend, 'model': model,
                              'model_provider': actual_provider, 'base_url': base_url or 'native',
                              'retained_agent': {'provider': 'opencode', 'group_count': 1, 'effective_providers': ['opencode']}})
            for name, content in {'result.json': result, 'provider-payload-receipt.json': result['provider_payload_receipt'],
                                  'opencode-target-receipt.json': result['opencode_target_receipt']}.items():
                (dest / name).write_text(json.dumps(content))
            manifest = json.loads((dest / 'manifest.json').read_text())
            manifest['files'] = {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in dest.rglob('*') if p.is_file() and p.name != 'manifest.json'}
            (dest / 'manifest.json').write_text(json.dumps(manifest))
            return result_code
        f.launched.side_effect = run_wizard
        self.assertEqual(f.run_driver('--opencode-model', model, *self.endpoint_flags(base_url, model_provider),
                                     provider='opencode', auth_method=backend), 0)
        self.assertEqual(json.loads(f.report.read_text())['wizard']['opencode_model'], model)


if __name__ == '__main__':
    unittest.main()
