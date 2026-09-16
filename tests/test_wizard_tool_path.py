"""Post-wizard verification must find tools installed in the child shell."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_wizard

wizard = test_wizard.wizard


class InstalledToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.root = self.home / 'checkout'
        self.bin = self.home / '.local/bin'
        self.bin.mkdir(parents=True)
        (self.root / 'bin').mkdir(parents=True)
        node = shutil.which('node')
        self.assertIsNotNone(node, 'Provider-resolver regressions require Node')
        (self.bin / 'node').symlink_to(node)
        self.write_executable(self.root / 'bin/ncl', '#!/bin/sh\nexec pnpm exec tsx src/cli/client.ts "$@"\n')
        self.cli = self.root / 'fixture-cli.cjs'
        self.cli.write_text('''
const args = process.argv.slice(2);
let data;
if (args.includes('sessions')) data = [{agent_group_id: 'group-1', agent_provider: 'opencode'}];
else if (args.includes('config')) data = {provider: 'opencode'};
else if (args.includes('secrets')) data = [{name: 'Anthropic', hostPattern: 'api.anthropic.com'}];
else data = [{id: 'group-1'}];
process.stdout.write(JSON.stringify({ok: true, data}));
''')
        resolver = self.root / 'dist/providers/provider-name.js'
        resolver.parent.mkdir(parents=True)
        resolver.write_text("export function resolveProviderName(session, config) { return session || config || 'claude'; }\n")
        self.write_executable(self.bin / 'onecli', '#!/bin/sh\nexec node "' + str(self.cli) + '" "$@"\n')
        self.environment = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin'}

    def write_executable(self, path, text):
        path.write_text(text)
        path.chmod(0o700)

    def install_pnpm(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.write_executable(directory / 'pnpm', '#!/bin/sh\nexec node "' + str(self.cli) + '" "$@"\n')

    def test_child_installed_node_pnpm_and_onecli_work_in_verifier_only(self):
        self.install_pnpm(self.bin)
        with patch.dict(os.environ, self.environment, clear=True):
            before = dict(os.environ)
            failed = subprocess.run([str(self.root / 'bin/ncl'), 'groups', 'list', '--json'],
                                    cwd=self.root, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn('pnpm', failed.stderr)
            receipt = wizard.verify_retained_provider_group(self.root, 'opencode')
            self.assertEqual(receipt['effective_providers'], ['opencode'])
            terminal = type('Terminal', (), {
                'handoff_emitted': True,
                'handoff_response_submitted': True,
                'text': lambda _: 'Got token: [fixture]\nSaving it to your OneCLI vault',
            })()
            self.assertEqual(wizard.verify_claude_target(terminal, True)['vault']['entry_count'], 1)
            self.assertEqual(dict(os.environ), before)
            self.assertEqual(wizard.child_environment()['PATH'], '/usr/bin:/bin')
            with self.assertRaises(wizard.Failure):
                wizard.verify_retained_provider_group(self.root, 'claude')

    def test_recovers_pnpm_from_npm_global_prefix(self):
        prefix = self.home / 'npm-prefix'
        self.install_pnpm(prefix / 'bin')
        self.write_executable(self.bin / 'npm', '#!/bin/sh\nprintf "%s\\n" "' + str(prefix) + '"\n')
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(wizard.verify_retained_provider_group(self.root, 'opencode')['provider'], 'opencode')

    def test_missing_pnpm_still_fails_verification(self):
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaises(wizard.Failure) as caught:
                wizard.verify_retained_provider_group(self.root, 'opencode')
        self.assertEqual(caught.exception.phase, 'verify')
