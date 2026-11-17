"""Offline tests for the e2e-channels credential loader and its CI workflow."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/e2e-channels/scripts/channel-creds.py'
MANIFEST = ROOT / 'skills/e2e-channels/channels.json'
WORKFLOW = ROOT / '.github/workflows/channel-e2e.yml'

SLACK = {
    'SLACK_BOT_TOKEN': 'xoxb-bot-secret-111',
    'SLACK_APP_TOKEN': 'xapp-app-secret-222',
    'SLACK_TESTER_USER_TOKEN': 'xoxp-user-secret-333',
}


class ChannelCredsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / 'e2e-home'
        (self.home / 'channels').mkdir(parents=True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('NC_E2E_')}
        self.env['NC_E2E_HOME'] = str(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def run_creds(self, *args, env=None):
        merged = dict(self.env)
        merged.update(env or {})
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=merged,
                              capture_output=True, text=True, check=False)

    def check(self, *args, env=None):
        out = Path(self.tmp.name) / 'result.json'
        proc = self.run_creds('check', *args, '--json-out', str(out), env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for value in SLACK.values():
            self.assertNotIn(value, proc.stdout + proc.stderr)
            self.assertNotIn(value, out.read_text())
        return {r['channel']: r for r in json.loads(out.read_text())['channels']}

    def write_file(self, path, values, mode=0o600):
        path = Path(path)
        path.write_text(''.join(f'{k}={v}\n' for k, v in values.items()))
        path.chmod(mode)
        return path

    def prefixed(self, channel, values):
        prefix = 'NC_E2E_' + channel.upper().replace('-', '_') + '_'
        return {prefix + k: v for k, v in values.items()}

    def test_manifest_covers_every_nanoclaw_channel(self):
        manifest = json.loads(MANIFEST.read_text())
        expected = {'cli', 'emacs', 'slack', 'discord', 'telegram', 'mattermost', 'matrix', 'teams',
                    'gchat', 'webex', 'github', 'linear', 'resend', 'whatsapp-cloud', 'whatsapp',
                    'signal', 'wechat', 'imessage', 'deltachat', 'dial'}
        self.assertEqual(set(manifest['channels']), expected)
        self.assertEqual(manifest['vault'], 'nanoclaw-e2e')

    def test_channels_without_secrets_are_ready(self):
        reports = self.check('cli', 'emacs', 'matrix', 'mattermost')
        for name, report in reports.items():
            with self.subTest(channel=name):
                self.assertEqual(report['status'], 'ready')

    def test_missing_credentials_skip_and_name_the_variable(self):
        reports = self.check('slack')
        self.assertEqual(reports['slack']['status'], 'skipped')
        self.assertIn('missing SLACK_BOT_TOKEN', reports['slack']['reason'])
        self.assertEqual(reports['slack']['missing'], list(SLACK))

    def test_environment_variables_are_used(self):
        reports = self.check('slack', env=self.prefixed('slack', SLACK))
        self.assertEqual(reports['slack']['status'], 'ready')
        self.assertEqual(reports['slack']['source'], 'environment')
        self.assertEqual(reports['slack']['variables'], list(SLACK))

    def test_one_missing_variable_is_named_alone(self):
        partial = dict(SLACK)
        del partial['SLACK_APP_TOKEN']
        reports = self.check('slack', env=self.prefixed('slack', partial))
        self.assertEqual(reports['slack']['status'], 'skipped')
        self.assertEqual(reports['slack']['missing'], ['SLACK_APP_TOKEN'])

    def test_cli_file_wins_over_environment_and_default_file(self):
        self.write_file(self.home / 'channels/slack.env', SLACK)
        cli = self.write_file(Path(self.tmp.name) / 'cli.env', SLACK)
        reports = self.check('slack', '--channel-env-file', str(cli), env=self.prefixed('slack', SLACK))
        self.assertEqual(reports['slack']['source'], f'file {cli}')

    def test_first_complete_source_wins_without_mixing(self):
        partial = {'SLACK_BOT_TOKEN': SLACK['SLACK_BOT_TOKEN']}
        self.write_file(self.home / 'channels/slack.env', SLACK)
        reports = self.check('slack', env=self.prefixed('slack', partial))
        self.assertEqual(reports['slack']['source'], f"file {self.home / 'channels/slack'}.env")

    def test_tester_values_may_live_in_their_own_default_file(self):
        bot = {k: v for k, v in SLACK.items() if 'TESTER' not in k}
        self.write_file(self.home / 'channels/slack.env', bot)
        self.write_file(self.home / 'channels/slack.tester.env',
                        {'SLACK_TESTER_USER_TOKEN': SLACK['SLACK_TESTER_USER_TOKEN']})
        self.assertEqual(self.check('slack')['slack']['status'], 'ready')

    def test_values_split_across_sources_are_not_combined(self):
        env = self.prefixed('slack', {'SLACK_BOT_TOKEN': SLACK['SLACK_BOT_TOKEN']})
        self.write_file(self.home / 'channels/slack.env',
                        {k: v for k, v in SLACK.items() if k != 'SLACK_BOT_TOKEN'})
        report = self.check('slack', env=env)['slack']
        self.assertEqual(report['status'], 'skipped')
        self.assertIn('no single source', report['reason'])

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions')
    def test_group_readable_file_is_ignored(self):
        self.write_file(self.home / 'channels/slack.env', SLACK, mode=0o644)
        report = self.check('slack')['slack']
        self.assertEqual(report['status'], 'skipped')
        self.assertIn('want 0600', report['reason'])

    def test_unresolved_1password_reference_skips(self):
        values = dict(SLACK, SLACK_APP_TOKEN='op://nanoclaw-e2e/slack/SLACK_APP_TOKEN')
        report = self.check('slack', env=self.prefixed('slack', values))['slack']
        self.assertEqual(report['status'], 'skipped')
        self.assertIn('SLACK_APP_TOKEN (unresolved 1Password reference', report['reason'])

    def test_placeholders_and_wrong_shapes_skip(self):
        for bad in ('<paste here>', 'changeme', 'PASTE_TOKEN_HERE', 'xoxp-wrong-kind'):
            with self.subTest(value=bad):
                values = dict(SLACK, SLACK_BOT_TOKEN=bad)
                report = self.check('slack', env=self.prefixed('slack', values))['slack']
                self.assertEqual(report['status'], 'skipped')
                self.assertIn('SLACK_BOT_TOKEN', report['reason'])
                self.assertNotIn(bad, json.dumps(report))

    def test_service_account_json_shape(self):
        base = {'GOOGLE_CHAT_PROJECT_NUMBER': '1234', 'GOOGLE_CHAT_ENDPOINT_URL': 'https://x.example/hook',
                'GOOGLE_CHAT_BOT_USER_ID': 'users/1', 'GCHAT_TESTER_CLIENT_ID': 'cid',
                'GCHAT_TESTER_CLIENT_SECRET': 'csec', 'GCHAT_TESTER_REFRESH_TOKEN': 'rt'}
        good = dict(base, GCHAT_CREDENTIALS='{"type": "service_account", "private_key": "k"}')
        bad = dict(base, GCHAT_CREDENTIALS='{"type": "authorized_user"}')
        self.assertEqual(self.check('gchat', env=self.prefixed('gchat', good))['gchat']['status'], 'ready')
        self.assertEqual(self.check('gchat', env=self.prefixed('gchat', bad))['gchat']['status'], 'skipped')

    def test_any_of_alternatives(self):
        base = {'LINEAR_WEBHOOK_SECRET': 'w', 'LINEAR_TEAM_KEY': 'ENG', 'LINEAR_TESTER_API_KEY': 't'}
        with_key = dict(base, LINEAR_API_KEY='lin_api_x', LINEAR_CLIENT_ID='', LINEAR_CLIENT_SECRET='')
        report = self.check('linear', env=self.prefixed('linear', with_key))['linear']
        self.assertEqual(report['status'], 'ready')
        self.assertIn('LINEAR_API_KEY', report['variables'])
        self.assertNotIn('LINEAR_CLIENT_ID', report['variables'])
        half = dict(base, LINEAR_CLIENT_ID='id')
        report = self.check('linear', env=self.prefixed('linear', half))['linear']
        self.assertEqual(report['status'], 'skipped')
        self.assertIn('one of LINEAR_CLIENT_ID+LINEAR_CLIENT_SECRET | LINEAR_API_KEY', report['reason'])

    def test_session_channels_need_a_session_directory(self):
        self.assertIn('linked-device session', self.check('whatsapp')['whatsapp']['reason'])
        session = self.home / 'channels/whatsapp/session'
        session.mkdir(parents=True)
        (session / 'creds.json').write_text('{}')
        self.assertEqual(self.check('whatsapp')['whatsapp']['status'], 'ready')

    def test_blocked_channels_skip_with_reason(self):
        report = self.check('imessage', env=self.prefixed(
            'imessage', {'PHOTON_PROJECT_ID': 'p', 'PHOTON_PROJECT_SECRET': 's'}))['imessage']
        self.assertEqual(report['status'], 'skipped')
        self.assertTrue(report['reason'].startswith('blocked:'))

    def test_require_ready_exit_code(self):
        proc = self.run_creds('check', 'cli', 'slack', '--require-ready')
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.run_creds('check', 'cli', '--require-ready').returncode, 0)

    def test_github_output_lists_ready_channels(self):
        out = Path(self.tmp.name) / 'gh-output'
        self.run_creds('check', 'cli', 'slack', '--github-output', str(out))
        self.assertIn('ready=["cli"]', out.read_text())

    def test_write_env_writes_private_bot_side_only(self):
        out = Path(self.tmp.name) / 'out/slack.env'
        proc = self.run_creds('write-env', 'slack', '--out', str(out), env=self.prefixed('slack', SLACK))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = out.read_text()
        self.assertIn('SLACK_BOT_TOKEN=' + SLACK['SLACK_BOT_TOKEN'], text)
        self.assertNotIn('TESTER', text)
        self.assertNotIn(SLACK['SLACK_BOT_TOKEN'], proc.stdout + proc.stderr)
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)

    def test_write_env_skipped_writes_nothing(self):
        out = Path(self.tmp.name) / 'slack.env'
        proc = self.run_creds('write-env', 'slack', '--out', str(out))
        self.assertEqual(proc.returncode, 3)
        self.assertFalse(out.exists())
        self.assertIn('missing SLACK_BOT_TOKEN', proc.stderr)

    def test_refs_cover_every_variable_with_vault_paths(self):
        manifest = json.loads(MANIFEST.read_text())
        proc = self.run_creds('refs', '--all')
        lines = proc.stdout.splitlines()
        for name, spec in manifest['channels'].items():
            names = spec['bot'] + [v for g in spec.get('bot_any_of', []) for v in g] + spec['tester']
            for var in names:
                prefix = 'NC_E2E_' + name.upper().replace('-', '_') + '_'
                self.assertIn(f'{prefix}{var}=op://nanoclaw-e2e/{name}/{var}', lines)

    def test_unknown_channel_is_a_usage_error(self):
        proc = self.run_creds('check', 'carrier-pigeon')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('unknown channel', proc.stderr)


class ChannelWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()

    def test_never_runs_on_pull_requests(self):
        trigger = self.text.split('\npermissions:')[0]
        self.assertNotIn('pull_request', trigger)
        self.assertIn('workflow_dispatch', trigger)
        self.assertIn('schedule', trigger)

    def test_uses_protected_environment_and_service_account(self):
        self.assertIn('environment: channel-e2e', self.text)
        self.assertIn('secrets.OP_SERVICE_ACCOUNT_TOKEN', self.text)
        self.assertEqual(self.text.count('${{ secrets.'), 1)

    def test_actions_are_pinned_to_commits(self):
        for ref in re.findall(r'uses:\s*(\S+)', self.text):
            with self.subTest(ref=ref):
                self.assertRegex(ref, r'@[0-9a-f]{40}$')

    def test_secret_load_failure_cannot_fail_the_job(self):
        block = self.text.split('- name: Load secrets from 1Password')[1].split('- id: check')[0]
        self.assertIn('continue-on-error: true', block)
        self.assertIn('export-env: true', block)


if __name__ == '__main__':
    unittest.main()
