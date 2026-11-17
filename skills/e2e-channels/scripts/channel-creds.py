#!/usr/bin/env python3
"""Find each NanoClaw channel's test credentials, or report the channel as skipped.

For channel X the loader reads the variable names from ../channels.json and looks
in this order, using the first source that holds a complete, valid set:

  1. a file given with --channel-env-file
  2. environment variables NC_E2E_<X>_<VAR> (how CI provides them)
  3. ~/.nanoclaw-e2e/channels/<x>.env plus <x>.tester.env (an operator's machine)

A channel with nothing complete is "skipped" with the missing variable names; it
never fails and never passes. Values are kept in memory, never printed or logged:
every report names variables only.
"""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

SKILL_DIR = Path(__file__).resolve().parents[1]
MANIFEST = SKILL_DIR / 'channels.json'
# What op-create-items.py writes into a new vault field; it never counts as a value.
PLACEHOLDER_VALUE = 'NC_E2E_PLACEHOLDER'
PLACEHOLDER = re.compile(
    r'^(op://.*|<.*>|\$\{.*\}|nc_e2e_placeholder.*|changeme|change-me|todo|tbd|xxx+|placeholder|replace[-_ ]?me|'
    r'(paste|your|insert)[-_ ].*)$',
    re.IGNORECASE,
)
EXIT_SKIPPED = 3


class ManifestError(Exception):
    pass


def load_manifest(path=MANIFEST):
    data = json.loads(Path(path).read_text())
    channels = data.get('channels')
    if not isinstance(channels, dict) or not channels:
        raise ManifestError(f'{path}: no channels')
    for name, spec in channels.items():
        if not re.fullmatch(r'[a-z][a-z0-9-]*', name):
            raise ManifestError(f'{path}: bad channel name {name!r}')
        for key in ('bot', 'tester'):
            if not isinstance(spec.get(key), list):
                raise ManifestError(f'{path}: {name}.{key} must be a list')
        for var in all_variables(spec):
            if not re.fullmatch(r'[A-Z][A-Z0-9_]*', var):
                raise ManifestError(f'{path}: {name}: bad variable name {var!r}')
    return data


def all_variables(spec):
    """Every variable a channel can use, in manifest order, without duplicates."""
    names = list(spec.get('bot', []))
    for group in spec.get('bot_any_of', []):
        names.extend(group)
    names.extend(spec.get('tester', []))
    return list(dict.fromkeys(names))


def env_prefix(channel):
    return 'NC_E2E_' + channel.upper().replace('-', '_') + '_'


def e2e_home():
    return Path(os.environ.get('NC_E2E_HOME') or Path.home() / '.nanoclaw-e2e')


def read_env_file(path):
    """Return (values, problem). Refuses files other users can read."""
    path = Path(path).expanduser()
    try:
        info = path.stat()
    except FileNotFoundError:
        return {}, None
    except OSError as error:
        return {}, f'{path}: unreadable ({error.strerror})'
    if not stat.S_ISREG(info.st_mode):
        return {}, f'{path}: not a regular file'
    if os.name == 'posix' and info.st_mode & 0o077:
        return {}, f'{path}: permissions {stat.S_IMODE(info.st_mode):04o}, want 0600; ignored'
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].lstrip()
        key, sep, value = line.partition('=')
        key = key.strip()
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        values[key] = value
    return values, None


def check_value(var, value, shapes):
    """Return None if usable, else a short reason that never contains the value."""
    if value is None or not value.strip():
        return 'empty'
    value = value.strip()
    if value.startswith('op://'):
        return 'unresolved 1Password reference'
    if PLACEHOLDER.match(value):
        return 'placeholder'
    shape = shapes.get(var)
    if shape == 'service_account':
        try:
            parsed = json.loads(value)
        except ValueError:
            return 'not service-account JSON'
        if not isinstance(parsed, dict) or parsed.get('type') != 'service_account':
            return 'not service-account JSON'
    elif shape and not re.search(shape, value):
        return f'does not match {shape}'
    return None


def pick_set(spec, values, shapes):
    """Return (chosen variable names or None, missing names, invalid {var: reason})."""
    missing, invalid = [], {}

    def usable(var):
        if var not in values or values[var] is None:
            return False
        reason = check_value(var, values[var], shapes)
        if reason in ('empty', 'placeholder'):
            return False  # not filled in yet: reported as missing
        if reason:
            invalid[var] = reason
            return False
        return True

    needed = list(spec.get('bot', [])) + list(spec.get('tester', []))
    for var in needed:
        if not usable(var) and var not in invalid:
            missing.append(var)
    chosen_group = []
    groups = spec.get('bot_any_of', [])
    if groups:
        complete = [group for group in groups if all(usable(var) for var in group)]
        if complete:
            chosen_group = complete[0]
            for group in groups:
                if group is not chosen_group:
                    for var in group:
                        invalid.pop(var, None)
        else:
            missing.append('one of ' + ' | '.join('+'.join(group) for group in groups))
    if missing or any(var in invalid for var in needed):
        return None, missing, invalid
    chosen = list(spec.get('bot', [])) + chosen_group + list(spec.get('tester', []))
    return chosen, [], {}


def find_session(channel, spec):
    if not spec.get('session'):
        return None, None
    candidates = []
    from_env = os.environ.get(env_prefix(channel) + 'SESSION_DIR')
    if from_env:
        candidates.append(Path(from_env).expanduser())
    candidates.append(e2e_home() / 'channels' / channel / 'session')
    for path in candidates:
        if path.is_dir() and any(path.iterdir()):
            return path, None
    return None, 'missing linked-device session (' + ' or '.join(str(p) for p in candidates) + ')'


def sources_for(channel, spec, cli_file=None):
    """Yield (label, values, problem) in lookup order."""
    names = all_variables(spec)
    if cli_file:
        values, problem = read_env_file(cli_file)
        if not values and problem is None and not Path(cli_file).expanduser().exists():
            problem = f'{cli_file}: not found'
        yield f'file {cli_file}', values, problem
    prefix = env_prefix(channel)
    yield 'environment', {var: os.environ.get(prefix + var) for var in names}, None
    base = e2e_home() / 'channels'
    merged, problems = {}, []
    for path in (base / f'{channel}.env', base / f'{channel}.tester.env'):
        values, problem = read_env_file(path)
        merged.update(values)
        if problem:
            problems.append(problem)
    yield f'file {base / channel}.env', merged, '; '.join(problems) or None


def resolve(channel, manifest, cli_file=None):
    """Return (report, values). values is None unless the channel is ready."""
    channels = manifest['channels']
    if channel not in channels:
        raise KeyError(channel)
    spec = channels[channel]
    shapes = spec.get('shapes', {})
    report = {'channel': channel, 'wave': spec.get('wave')}
    if spec.get('blocked'):
        report.update(status='skipped', reason=f"blocked: {spec['blocked']}")
        return report, None
    if spec.get('provisioned_in_run') or not all_variables(spec) and not spec.get('session'):
        report.update(status='ready', source='none needed', variables=[])
        return report, {}
    session, session_problem = find_session(channel, spec)
    if not all_variables(spec):
        if session_problem:
            report.update(status='skipped', reason=session_problem, missing=['session'])
            return report, None
        report.update(status='ready', source=f'session {session}', variables=[], session=str(session))
        return report, {}
    missing_everywhere = None
    invalid_seen, problems, partial_sources = {}, [], []
    for label, values, problem in sources_for(channel, spec, cli_file):
        if problem:
            problems.append(problem)
        chosen, missing, invalid = pick_set(spec, values, shapes)
        if chosen is not None:
            if session_problem:
                report.update(status='skipped', reason=session_problem, source=label)
                return report, None
            report.update(status='ready', source=label, variables=chosen)
            if session:
                report['session'] = str(session)
            return report, {var: values[var].strip() for var in chosen}
        for var, why in invalid.items():
            invalid_seen.setdefault(var, f'{why} in {label}')
        if missing_everywhere is None:
            missing_everywhere = list(missing)
        else:
            missing_everywhere = [var for var in missing_everywhere if var in missing]
        if any(values.get(var) and not check_value(var, values[var], shapes) for var in all_variables(spec)):
            partial_sources.append(label)
    parts = []
    if missing_everywhere:
        parts.append('missing ' + ', '.join(missing_everywhere))
    if invalid_seen:
        parts.append('invalid ' + ', '.join(f'{var} ({why})' for var, why in invalid_seen.items()))
    if not parts:
        parts.append('no single source holds a complete set (partial in: ' + ', '.join(partial_sources) + ')')
    if session_problem:
        parts.append(session_problem)
    parts.extend(problems)
    report.update(status='skipped', reason='; '.join(parts),
                  missing=missing_everywhere or [], invalid=sorted(invalid_seen))
    if problems:
        report['problems'] = problems
    return report, None


def parse_env_file_args(items, channels):
    files = {}
    for item in items or []:
        channel, sep, path = item.partition('=')
        if sep and channel in channels:
            files[channel] = path
        elif len(channels) == 1:
            files[channels[0]] = item
        else:
            raise SystemExit(f'--channel-env-file {item!r}: use CHANNEL=PATH when checking several channels')
    return files


def selected(args, manifest):
    names = list(manifest['channels'])
    if args.all:
        return names
    if not args.channels:
        raise SystemExit('name one or more channels, or pass --all')
    unknown = [c for c in args.channels if c not in manifest['channels']]
    if unknown:
        raise SystemExit('unknown channel(s): ' + ', '.join(unknown) + '; known: ' + ', '.join(names))
    return list(dict.fromkeys(args.channels))


def write_private(path, text):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
        with os.fdopen(fd, 'w') as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        os.umask(old)


def env_line(var, value, shapes):
    if shapes.get(var) == 'service_account':
        value = json.dumps(json.loads(value), separators=(',', ':'))
    if '\n' in value or '\r' in value:
        raise SystemExit(f'{var}: value spans several lines; store it on one line')
    return f'{var}={value}\n'


def cmd_list(args, manifest):
    rows = []
    for name, spec in manifest['channels'].items():
        rows.append({
            'channel': name, 'wave': spec.get('wave'),
            'bot': spec['bot'], 'bot_any_of': spec.get('bot_any_of', []), 'tester': spec['tester'],
            'session': bool(spec.get('session')), 'public_url': bool(spec.get('public_url')),
            'provisioned_in_run': bool(spec.get('provisioned_in_run')), 'blocked': spec.get('blocked'),
        })
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        needs = 'provisioned in run' if row['provisioned_in_run'] else ', '.join(
            row['bot'] + ['|'.join('+'.join(g) for g in row['bot_any_of'])] * bool(row['bot_any_of'])
            + row['tester'] + ['session'] * row['session']) or 'nothing'
        print(f"wave {row['wave']}  {row['channel']:<15} {needs}")
    return 0


def cmd_check(args, manifest):
    channels = selected(args, manifest)
    files = parse_env_file_args(args.channel_env_file, channels)
    reports = [resolve(name, manifest, files.get(name))[0] for name in channels]
    for report in reports:
        detail = report.get('source') if report['status'] == 'ready' else report.get('reason')
        print(f"{report['channel']}: {report['status']} ({detail})")
    if args.json_out:
        write_private(args.json_out, json.dumps({'channels': reports}, indent=2) + '\n')
    if args.github_output:
        ready = [r['channel'] for r in reports if r['status'] == 'ready']
        with open(args.github_output, 'a') as handle:
            handle.write(f"ready={json.dumps(ready)}\n")
            if len(reports) == 1:
                handle.write(f"status={reports[0]['status']}\n")
    if args.require_ready and any(r['status'] != 'ready' for r in reports):
        return EXIT_SKIPPED
    return 0


def cmd_refs(args, manifest):
    vault = manifest.get('vault', 'nanoclaw-e2e')
    for name in selected(args, manifest):
        spec = manifest['channels'][name]
        for var in all_variables(spec):
            print(f'{env_prefix(name)}{var}=op://{vault}/{name}/{var}')
    return 0


def cmd_template(args, manifest):
    vault = manifest.get('vault', 'nanoclaw-e2e')
    name = args.channel
    if name not in manifest['channels']:
        raise SystemExit(f'unknown channel: {name}')
    spec = manifest['channels'][name]
    bot = [v for v in all_variables(spec) if v not in spec['tester']]
    names = {'bot': bot, 'tester': list(spec['tester']), 'all': all_variables(spec)}[args.side]
    target = f'~/.nanoclaw-e2e/channels/{name}' + ('.tester' if args.side == 'tester' else '') + '.env'
    print(f'# {name} ({args.side}). Fill with: (umask 077; op inject -i <this file> -o {target})')
    if spec.get('bot_any_of') and args.side != 'tester':
        print('# Alternatives: ' + ' | '.join('+'.join(g) for g in spec['bot_any_of'])
              + '. Keep the unused fields empty in 1Password.')
    for var in names:
        print(f'{var}=op://{vault}/{name}/{var}')
    return 0


def cmd_write_env(args, manifest):
    if args.channel not in manifest['channels']:
        raise SystemExit(f'unknown channel: {args.channel}')
    spec = manifest['channels'][args.channel]
    report, values = resolve(args.channel, manifest, args.channel_env_file)
    if report['status'] != 'ready':
        print(f"{args.channel}: skipped ({report['reason']}); nothing written", file=sys.stderr)
        return EXIT_SKIPPED
    tester = set(spec['tester'])
    keep = [v for v in report['variables'] if (v in tester) == (args.side == 'tester')]
    text = ''.join(env_line(var, values[var], spec.get('shapes', {})) for var in keep)
    write_private(args.out, text)
    print(f"{args.channel}: wrote {len(keep)} {args.side} variable(s) from {report['source']} to {args.out}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--manifest', default=str(MANIFEST), help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('list', help='show what each channel needs')
    p.add_argument('--json', action='store_true')
    p.set_defaults(run=cmd_list)

    for command, run, text in (('check', cmd_check, 'report ready or skipped per channel'),
                               ('refs', cmd_refs, 'print NC_E2E_* = op:// reference lines for CI')):
        p = sub.add_parser(command, help=text)
        p.add_argument('channels', nargs='*')
        p.add_argument('--all', action='store_true')
        if command == 'check':
            p.add_argument('--channel-env-file', action='append', metavar='[CHANNEL=]PATH')
            p.add_argument('--json-out', metavar='PATH')
            p.add_argument('--github-output', metavar='PATH', help='append ready= and status= lines')
            p.add_argument('--require-ready', action='store_true',
                           help=f'exit {EXIT_SKIPPED} if any channel is skipped')
        p.set_defaults(run=run)

    p = sub.add_parser('template', help='print an op inject template for one channel')
    p.add_argument('channel')
    p.add_argument('--side', choices=('bot', 'tester', 'all'), default='bot')
    p.set_defaults(run=cmd_template)

    p = sub.add_parser('write-env', help='write one channel\'s resolved values to a 0600 file')
    p.add_argument('channel')
    p.add_argument('--out', required=True)
    p.add_argument('--side', choices=('bot', 'tester'), default='bot')
    p.add_argument('--channel-env-file', metavar='PATH')
    p.set_defaults(run=cmd_write_env)

    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
    except (OSError, ValueError, ManifestError) as error:
        print(f'manifest error: {error}', file=sys.stderr)
        return 2
    return args.run(args, manifest)


if __name__ == '__main__':
    sys.exit(main())
