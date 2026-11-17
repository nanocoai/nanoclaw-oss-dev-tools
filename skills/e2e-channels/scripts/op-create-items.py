#!/usr/bin/env python3
"""Create the nanoclaw-e2e 1Password items for each channel, with placeholder fields.

For every channel in ../channels.json that stores variables, this makes sure an
item titled with the channel name exists in the vault, with one concealed field
per variable (section "bot" or "tester"), labelled with the variable name, so
op://nanoclaw-e2e/<channel>/<VAR> resolves. New fields get the value
NC_E2E_PLACEHOLDER, which channel-creds.py treats as missing, so the channel
keeps reporting skipped until a real value replaces it.

It is safe to run again: an existing item only gains the fields it lacks, and
an existing field is never changed. Nothing read from the vault is printed.
Run it signed in to an account that can write the vault (`op signin`); the CI
service account is read-only.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location('channel_creds', SCRIPTS / 'channel-creds.py')
creds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(creds)

CATEGORY = 'Secure Note'
TAG = 'nanoclaw-e2e'


class OpError(Exception):
    pass


class Op:
    def __init__(self, binary, account=None):
        self.binary = binary
        self.account = account

    def run(self, *args):
        cmd = [self.binary, *args]
        if self.account:
            cmd += ['--account', self.account]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return proc.returncode, proc.stdout, proc.stderr.strip()

    def check_vault(self, vault):
        code, _, err = self.run('vault', 'get', vault, '--format', 'json')
        if code:
            raise OpError(f'cannot open vault {vault!r}: {err or "op exited " + str(code)}')

    def get_item(self, vault, title):
        """Return (item id, field labels) or None if no item has that title."""
        code, out, err = self.run('item', 'get', title, '--vault', vault, '--format', 'json')
        if code:
            lowered = err.lower()
            if "isn't an item" in lowered or 'not found' in lowered or 'no item' in lowered:
                return None
            raise OpError(f'{title}: {err or "op exited " + str(code)}')
        try:
            item = json.loads(out)
        except ValueError:
            raise OpError(f'{title}: op returned unreadable JSON') from None
        labels = {field.get('label') for field in item.get('fields', []) if field.get('label')}
        return item.get('id') or title, labels

    def create_item(self, vault, title, assignments):
        code, _, err = self.run('item', 'create', '--vault', vault, '--category', CATEGORY,
                                '--title', title, '--tags', TAG, *assignments)
        if code:
            raise OpError(f'{title}: create failed: {err or "op exited " + str(code)}')

    def add_fields(self, vault, item_id, title, assignments):
        code, _, err = self.run('item', 'edit', item_id, '--vault', vault, *assignments)
        if code:
            raise OpError(f'{title}: edit failed: {err or "op exited " + str(code)}')


def planned_fields(spec):
    """(section, variable) for every variable the channel stores, in manifest order."""
    tester = set(spec['tester'])
    return [('tester' if var in tester else 'bot', var) for var in creds.all_variables(spec)]


def assignment(section, var):
    return f'{section}.{var}[password]={creds.PLACEHOLDER_VALUE}'


def note_for(name, spec):
    lines = [f'NanoClaw e2e credentials for the {name} channel. Fields are referenced as '
             f'op://<vault>/{name}/<VARIABLE>. Replace each {creds.PLACEHOLDER_VALUE} with the real value.']
    if spec.get('bot_any_of'):
        lines.append('Fill one alternative and leave the others as placeholders: '
                     + ' | '.join('+'.join(group) for group in spec['bot_any_of']) + '.')
    if spec.get('session'):
        lines.append('This channel also needs a linked-device session, stored separately.')
    return 'notesPlain=' + ' '.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('channels', nargs='*', help='channels to set up (default: all)')
    parser.add_argument('--vault', help='vault name (default: the manifest vault, nanoclaw-e2e)')
    parser.add_argument('--account', help='1Password account (sign-in address, ID or shorthand)')
    parser.add_argument('--dry-run', action='store_true', help='show what would change, write nothing')
    parser.add_argument('--op', default='op', help=argparse.SUPPRESS)
    parser.add_argument('--manifest', default=str(creds.MANIFEST), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    manifest = creds.load_manifest(args.manifest)
    vault = args.vault or manifest.get('vault', 'nanoclaw-e2e')
    names = args.channels or list(manifest['channels'])
    unknown = [n for n in names if n not in manifest['channels']]
    if unknown:
        parser.error('unknown channel(s): ' + ', '.join(unknown))
    if not shutil.which(args.op):
        print('1Password CLI (op) not found; install it and run `op signin`.', file=sys.stderr)
        return 2

    op = Op(args.op, args.account)
    try:
        op.check_vault(vault)
    except OpError as error:
        print(error, file=sys.stderr)
        return 2

    failures = 0
    for name in names:
        spec = manifest['channels'][name]
        fields = planned_fields(spec)
        if not fields:
            print(f'{name}: nothing stored (provisioned in run or session only)')
            continue
        try:
            found = op.get_item(vault, name)
            if found is None:
                if not args.dry_run:
                    op.create_item(vault, name, [note_for(name, spec)]
                                   + [assignment(section, var) for section, var in fields])
                verb = 'would create' if args.dry_run else 'created'
                print(f'{name}: {verb} with {len(fields)} placeholder field(s)')
                continue
            item_id, labels = found
            missing = [(section, var) for section, var in fields if var not in labels]
            if not missing:
                print(f'{name}: up to date')
                continue
            if not args.dry_run:
                op.add_fields(vault, item_id, name, [assignment(section, var) for section, var in missing])
            verb = 'would add' if args.dry_run else 'added'
            print(f'{name}: {verb} ' + ', '.join(var for _, var in missing))
        except OpError as error:
            failures += 1
            print(f'error: {error}', file=sys.stderr)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
