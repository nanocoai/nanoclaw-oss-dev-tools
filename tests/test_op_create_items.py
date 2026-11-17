"""Offline tests for op-create-items.py against a fake 1Password CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills/e2e-channels/scripts/op-create-items.py'
CREDS = ROOT / 'skills/e2e-channels/scripts/channel-creds.py'
MANIFEST = ROOT / 'skills/e2e-channels/channels.json'

FAKE_OP = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    state_path = os.environ['FAKE_OP_STATE']
    state = json.load(open(state_path))
    args = sys.argv[1:]
    with open(os.environ['FAKE_OP_LOG'], 'a') as log:
        log.write(json.dumps(args) + '\\n')
    if '--account' in args:
        i = args.index('--account'); del args[i:i + 2]
    def opt(name):
        return args[args.index(name) + 1]
    def save():
        json.dump(state, open(state_path, 'w'))
    def parse(assignments):
        fields = []
        for a in assignments:
            left, value = a.split('=', 1)
            if left == 'notesPlain':
                fields.append({'label': 'notesPlain', 'value': value}); continue
            section, rest = left.split('.', 1)
            label = rest.split('[')[0]
            fields.append({'label': label, 'section': {'label': section}, 'type': 'CONCEALED', 'value': value})
        return fields
    if args[:2] == ['vault', 'get']:
        if args[2] != state['vault']:
            sys.stderr.write(f'"{args[2]}" isn\\'t a vault in this account\\n'); sys.exit(1)
        print('{}'); sys.exit(0)
    if args[:2] == ['item', 'get']:
        item = state['items'].get(args[2])
        if item is None:
            sys.stderr.write(f'[ERROR] "{args[2]}" isn\\'t an item in the "{opt("--vault")}" vault\\n'); sys.exit(1)
        print(json.dumps({'id': 'id-' + args[2], 'title': args[2], 'fields': item})); sys.exit(0)
    if args[:2] == ['item', 'create']:
        title = opt('--title')
        rest = [a for a in args if '=' in a]
        state['items'][title] = parse(rest); save(); sys.exit(0)
    if args[:2] == ['item', 'edit']:
        title = args[2][len('id-'):]
        state['items'][title].extend(parse([a for a in args[3:] if '=' in a])); save(); sys.exit(0)
    sys.stderr.write('unexpected: ' + ' '.join(args)); sys.exit(9)
''')


class OpCreateItemsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.op = base / 'op'
        self.op.write_text(FAKE_OP)
        self.op.chmod(0o755)
        self.state = base / 'state.json'
        self.log = base / 'log.jsonl'
        self.log.write_text('')
        self.save_state({})
        self.env = dict(os.environ, FAKE_OP_STATE=str(self.state), FAKE_OP_LOG=str(self.log))
        self.manifest = json.loads(MANIFEST.read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def save_state(self, items, vault='nanoclaw-e2e'):
        self.state.write_text(json.dumps({'vault': vault, 'items': items}))

    def items(self):
        return json.loads(self.state.read_text())['items']

    def run_script(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), '--op', str(self.op), *args],
                              env=self.env, capture_output=True, text=True, check=False)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_creates_one_item_per_channel_with_every_variable(self):
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        items = self.items()
        for name, spec in self.manifest['channels'].items():
            names = spec['bot'] + [v for g in spec.get('bot_any_of', []) for v in g] + spec['tester']
            with self.subTest(channel=name):
                if not names:
                    self.assertNotIn(name, items)
                    continue
                fields = {f['label']: f for f in items[name] if f['label'] != 'notesPlain'}
                self.assertEqual(set(fields), set(names))
                for var, field in fields.items():
                    self.assertEqual(field['value'], 'NC_E2E_PLACEHOLDER')
                    self.assertEqual(field['section']['label'],
                                     'tester' if var in spec['tester'] else 'bot')
        create = [c for c in self.calls() if c[:2] == ['item', 'create']][0]
        self.assertIn('Secure Note', create)
        self.assertIn('--tags', create)

    def test_rerun_changes_nothing(self):
        self.run_script()
        before = self.items()
        self.log.write_text('')
        proc = self.run_script()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.items(), before)
        self.assertFalse([c for c in self.calls() if c[1] in ('create', 'edit')])
        self.assertIn('slack: up to date', proc.stdout)

    def test_existing_values_are_kept_and_only_missing_fields_added(self):
        self.save_state({'slack': [{'label': 'SLACK_BOT_TOKEN', 'value': 'xoxb-real-secret-123',
                                    'section': {'label': 'bot'}}]})
        proc = self.run_script('slack')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        fields = {f['label']: f['value'] for f in self.items()['slack']}
        self.assertEqual(fields['SLACK_BOT_TOKEN'], 'xoxb-real-secret-123')
        self.assertEqual(fields['SLACK_APP_TOKEN'], 'NC_E2E_PLACEHOLDER')
        self.assertIn('added SLACK_APP_TOKEN, SLACK_TESTER_USER_TOKEN', proc.stdout)
        edit = [c for c in self.calls() if c[:2] == ['item', 'edit']][0]
        self.assertFalse(any('SLACK_BOT_TOKEN' in a for a in edit))
        self.assertNotIn('xoxb-real-secret-123', proc.stdout + proc.stderr)

    def test_dry_run_writes_nothing(self):
        proc = self.run_script('--dry-run', 'slack', 'cli')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.items(), {})
        self.assertIn('slack: would create with 3 placeholder field(s)', proc.stdout)
        self.assertIn('cli: nothing stored', proc.stdout)

    def test_missing_vault_stops_before_any_item_call(self):
        self.save_state({}, vault='other')
        proc = self.run_script()
        self.assertEqual(proc.returncode, 2)
        self.assertIn('cannot open vault', proc.stderr)
        self.assertFalse([c for c in self.calls() if c[0] == 'item'])

    def test_account_and_vault_flags_are_passed(self):
        self.save_state({}, vault='alt')
        proc = self.run_script('--vault', 'alt', '--account', 'my.1password.com', 'discord')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for call in self.calls():
            self.assertEqual(call[-2:], ['--account', 'my.1password.com'])
        self.assertIn('discord', self.items())

    def test_placeholder_items_make_the_loader_skip_not_fail(self):
        self.run_script('slack', 'linear')
        env = {k: v for k, v in os.environ.items() if not k.startswith('NC_E2E_')}
        env['NC_E2E_HOME'] = self.tmp.name
        for name in ('slack', 'linear'):
            for field in self.items()[name]:
                if field['label'] != 'notesPlain':
                    env['NC_E2E_' + name.upper() + '_' + field['label']] = field['value']
        env['NC_E2E_LINEAR_LINEAR_API_KEY'] = 'lin_api_real'
        for var in ('LINEAR_WEBHOOK_SECRET', 'LINEAR_TEAM_KEY', 'LINEAR_TESTER_API_KEY'):
            env['NC_E2E_LINEAR_' + var] = 'real-' + var.lower()
        out = Path(self.tmp.name) / 'result.json'
        proc = subprocess.run([sys.executable, str(CREDS), 'check', 'slack', 'linear', '--json-out', str(out)],
                              env=env, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        reports = {r['channel']: r for r in json.loads(out.read_text())['channels']}
        self.assertEqual(reports['slack']['status'], 'skipped')
        self.assertEqual(reports['slack']['missing'],
                         ['SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'SLACK_TESTER_USER_TOKEN'])
        self.assertEqual(reports['linear']['status'], 'ready')
        self.assertNotIn('LINEAR_CLIENT_ID', reports['linear']['variables'])


if __name__ == '__main__':
    unittest.main()
