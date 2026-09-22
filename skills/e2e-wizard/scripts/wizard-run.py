#!/usr/bin/env python3
"""Drive the public NanoClaw wizard through a real PTY; never perform its setup work."""

import argparse
import codecs
import datetime
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pty
import re
import secrets
import shlex
import shutil
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import termios
import time

MAX_LOG_BYTES = 32 * 1024 * 1024
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])')
TOKEN = re.compile(r'sk-ant-[A-Za-z0-9_-]+')
DEVICE_CODE = re.compile(r'\b[A-Z0-9]{4,8}(?:-[A-Z0-9]{4,8})+\b')
DEVICE_URL = 'https://auth.openai.com/codex/device'
DEVICE_CODE_PROMPT = re.compile(
    r'Enter this one-time code[^\r\n]*\r?\n[ \t]*('
    + DEVICE_CODE.pattern + r')[ \t]*\r?\n'
)
CLAUDE_AUTH_ENDPOINT = r'https://(?:claude\.ai/oauth/authorize|claude\.com/cai/oauth/authorize)'
CLAUDE_URL_CHARS = r'[A-Za-z0-9._~:/?#\[\]@!$&\x27()*+,;=%-]'
CLAUDE_AUTH_URL = re.compile(
    r'(' + CLAUDE_AUTH_ENDPOINT + CLAUDE_URL_CHARS + r'+'
    r'(?:\r?\n[ \t]*(?!Paste code here if prompted)' + CLAUDE_URL_CHARS + r'+)*)'
    r'(?:\r?\n[ \t]*)+Paste code here if prompted'
)
CLAUDE_AUTH_PRIVATE = re.compile(
    CLAUDE_AUTH_ENDPOINT + r'(?:' + CLAUDE_URL_CHARS + r'|\s){0,8192}'
)
CLAUDE_OAUTH_CAPTURE = re.compile(r'sk-ant-oat(?:[A-Za-z0-9_-]|\s){80,700}AA')
HANDOFF_ROOT = Path.home() / '.nanoclaw-e2e/auth-handoffs'
GATEWAYS = ('onecli', 'iron-proxy')
IRON_CONTROL_DIR = 'data/session-materials/iron-control'
CODEX_CLI_PACKAGE = '@openai/codex'
# Set for Codex device pairing: the wizard child sees ~/.local/bin first so the
# skill's pinned CLI (installed there by the driver) wins over a host copy.
PREFER_LOCAL_BIN = False


class Failure(Exception):
    def __init__(self, phase, message, code=1):
        self.phase, self.code = phase, code
        super().__init__(message)


def find_claude_auth_urls(text):
    """Return complete supported auth URLs only after the known code prompt."""
    urls = []
    for captured in CLAUDE_AUTH_URL.findall(text):
        compact = re.sub(r'\s+', '', captured).rstrip('.,)')
        if re.fullmatch(CLAUDE_AUTH_ENDPOINT + CLAUDE_URL_CHARS + r'+', compact):
            urls.append(compact)
    return urls


def redact_claude_auth_urls(text):
    """Redact supported auth endpoints even when output is wrapped or incomplete."""
    return CLAUDE_AUTH_PRIVATE.sub('[AUTHORIZATION URL REDACTED]', text)


def contains_authorization_url(text):
    """Detect supported authorization URLs even when whitespace-wrapped."""
    return re.search(CLAUDE_AUTH_ENDPOINT, re.sub(r'\s+', '', text)) is not None


def provider_discovery():
    path = Path(__file__).resolve().with_name('provider-options.py')
    if not path.is_file():
        raise Failure('preflight', 'Provider discovery helper is missing', 66)
    spec = importlib.util.spec_from_file_location('nanoclaw_e2e_provider_options', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def branch_payload_commit(selected):
    """The commit of a branch-owned payload (None for bundled or installed providers)."""
    kind = selected.get('payload_kind')
    if kind == 'branch':
        return selected['auth_source_commit']
    if kind == 'mixed':
        return next(source['commit'] for source in selected['payload_sources'] if source['kind'] == 'branch')
    return None


def selected_auth(root, provider, auth_method, payload_ref, expected_auth_source_commit=None,
                  supervised_human_auth=False, expected_payload_commit=None):
    module = provider_discovery()
    try:
        report = module.discover(root, provider, payload_ref)
    except module.DiscoveryError as error:
        raise Failure('preflight', str(error), 65)
    selected = report['selected']
    if (expected_auth_source_commit is not None
            and selected['auth_source_commit'] != expected_auth_source_commit):
        raise Failure('preflight', 'Provider authentication source changed after operator selection', 65)
    if expected_payload_commit is not None and branch_payload_commit(selected) != expected_payload_commit:
        raise Failure('preflight', 'Provider payload branch changed after operator selection', 65)
    methods = [item for item in selected['auth_methods'] if item['value'] == auth_method]
    if len(methods) != 1:
        raise Failure('preflight', f'Authentication method is not offered for {provider}: {auth_method}', 64)
    method = methods[0]
    if not method['usable_for_e2e']:
        raise Failure('preflight', 'Skipping provider authentication cannot produce an E2E pass', 64)
    if method['automation'] == 'human-handoff' and not supervised_human_auth:
        raise Failure(
            'preflight',
            f'{method["label"]} requires --supervised-human-auth',
            64,
        )
    if method['automation'] not in ('credential-file', 'human-handoff'):
        raise Failure(
            'preflight',
            f'{method["label"]} is unsupported by this supervised PTY runner',
            64,
        )
    return selected, method


def credential_for(path, kind):
    if kind is None:
        if path is not None:
            raise Failure('preflight', 'Human handoff authentication must not use --credential-file', 66)
        return ''
    if path is None:
        raise Failure('preflight', 'The selected authentication method requires --credential-file', 66)
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise Failure('preflight', 'Credential file is unreadable', 66)
    if path.is_symlink() or not stat.S_ISREG(mode) or mode & 0o077:
        raise Failure('preflight', 'Credential file must be a private regular file (0600)', 66)
    raw = read_limited(path)
    value = raw.strip() if kind == 'opencode-api-key' else re.sub(r'\s+', '', raw)
    shapes = {
        'anthropic-oauth': r'sk-ant-oat[A-Za-z0-9_-]+',
        'anthropic-api-key': r'sk-ant-api[A-Za-z0-9_-]+',
        'openai-api-key': r'sk-[A-Za-z0-9_-]+',
        'opencode-api-key': r'[\x21-\x7e]+',
    }
    pattern = shapes.get(kind)
    minimum = 8 if kind == 'opencode-api-key' else 16
    if not pattern or not minimum <= len(value) <= 1024 or not re.fullmatch(pattern, value):
        raise Failure('preflight', 'Credential does not match the selected provider authentication method', 66)
    return value


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(6))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out, indent=2)
            out.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_handoff_paths(run_id):
    """Create this run's private handoff directory without touching other runs."""
    HANDOFF_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if HANDOFF_ROOT.is_symlink() or not HANDOFF_ROOT.is_dir():
        raise Failure('preflight', 'Authentication handoff root is unsafe', 66)
    HANDOFF_ROOT.chmod(0o700)
    run_dir = HANDOFF_ROOT / run_id
    try:
        run_dir.mkdir(mode=0o700)
    except FileExistsError:
        raise Failure('preflight', 'Authentication handoff identity already exists', 66)
    return run_dir, run_dir / 'request.json', run_dir / 'response.json'


def cleanup_handoff(run_dir, request_path, response_path, run_id, nonce=None):
    """Remove only files that can be proven to belong to this invocation."""
    if run_dir is None or run_dir.is_symlink() or run_dir.parent != HANDOFF_ROOT:
        return
    for path in (request_path, response_path):
        if path is None or path.parent != run_dir or path.is_symlink() or not path.is_file():
            continue
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError, UnicodeError):
            continue
        if document.get('run_id') == run_id and (nonce is None or document.get('nonce') == nonce):
            path.unlink(missing_ok=True)
    try:
        run_dir.rmdir()
    except OSError:
        pass


def payload_source_commits(selected):
    """Map each payload branch (None = bundled) to the commit its files come from.

    A single-block payload comes entirely from the auth source commit; a mixed
    payload names one commit per block.
    """
    if selected.get('payload_kind') == 'mixed':
        return {source['branch']: source['commit'] for source in selected['payload_sources']}
    return None


def verify_provider_payload(root, selected):
    module = provider_discovery()
    commits = payload_source_commits(selected)
    try:
        # The SKILL.md is read from the tree that bundles it: the NanoClaw
        # revision when any block is bundled, else the checked-out HEAD.
        if commits is not None:
            skill_tree = commits[None] if None in commits else 'HEAD'
        else:
            skill_tree = selected['auth_source_commit'] if selected.get('payload_kind') == 'bundled' else 'HEAD'
        skill = module.tree_read(root, skill_tree, selected['source'])
        entries = module.copy_entries(skill, selected['source'])
    except module.DiscoveryError as error:
        raise Failure('payload', str(error), 65)
    receipt = {'commit': selected['auth_source_commit'], 'paths': {}}
    if commits is not None:
        receipt['sources'] = {source['kind'] if not source['branch'] else source['branch']: source['commit']
                              for source in selected['payload_sources']}
    for entry in entries:
        relative = entry['destination']
        installed = root / relative
        if installed.is_symlink() or not installed.is_file():
            raise Failure('payload', 'Installed provider payload is incomplete: ' + relative, 65)
        if commits is None:
            commit = selected['auth_source_commit']
        elif entry['branch'] in commits:
            commit = commits[entry['branch']]
        else:
            raise Failure('payload', 'Provider payload names a source the selection did not resolve: ' + relative, 65)
        expected = subprocess.run(
            ['git', 'show', commit + ':' + entry['source']], cwd=root,
            capture_output=True, timeout=30,
        )
        if expected.returncode or installed.read_bytes() != expected.stdout:
            raise Failure('payload', 'Installed provider payload does not match selected commit: ' + relative, 65)
        receipt['paths'][relative] = hashlib.sha256(expected.stdout).hexdigest()
    receipt['file_count'] = len(entries)
    canonical = json.dumps(receipt['paths'], sort_keys=True, separators=(',', ':')).encode()
    receipt['combined_sha256'] = hashlib.sha256(canonical).hexdigest()
    return receipt


def configure_opencode_scenario(scenario, values, backend, model, base_url=None, model_provider=None):
    runtime_provider = provider_discovery().validate_model('opencode', backend, model, base_url, model_provider)
    values.update(credential_prompt='API key', opencode_model=model,
                  opencode_base_url=base_url, opencode_provider=runtime_provider)
    index = next(i for i, item in enumerate(scenario['prompts']) if item['id'] == 'credential')
    credential = scenario['prompts'][index]
    model_prompts = [
        {'id': 'opencode-model-choice', 'prompt': 'Which default model should OpenCode use?',
         'select': 'Enter a model id manually', 'required': True},
        {'id': 'opencode-model', 'prompt': 'Model id in provider/model form',
         'text_from': 'opencode_model', 'required': True},
    ]
    connection_prompts = []
    if backend == 'custom':
        connection_prompts.append({'id': 'opencode-provider', 'prompt': 'OpenCode provider id',
                                   'text_from': 'opencode_provider', 'required': True})
    if base_url:
        connection_prompts.append({
            'id': 'opencode-endpoint', 'prompt': ('OpenAI-compatible base URL (include /v1)' if backend == 'local'
                else 'Custom API base URL (leave blank for OpenCode native configuration)'),
            'text_from': 'opencode_base_url', 'required': True,
        })
    if runtime_provider == 'openai' and base_url:
        connection_prompts.append({'id': 'opencode-key-required', 'prompt': 'Does this endpoint work without an API key?',
                                   'select': 'No', 'required': True})
        # The product needs the key before it can query a guarded /models catalog.
        scenario['prompts'][index:index + 1] = connection_prompts + [credential] + model_prompts
    else:
        scenario['prompts'][index:index + 1] = connection_prompts + model_prompts + [credential]


def gateway_seam_present(root):
    """NanoClaw refs with the credential-gateway seam (setup/gateways/, nanocoai/nanoclaw#3815)."""
    return (root / 'setup/gateways/step.ts').is_file()


def seam_scenario(scenario, provider):
    """Adapt the bundled scenario to a gateway-seam ref.

    The seam wizard installs the gateway through its skill (no `onecli` runner
    step) and hands authentication to the provider's own hook: OpenCode and
    Codex log an `auth` step themselves, Claude's gateway auth script does not.
    """
    scenario['required_steps'] = [
        step for step in scenario['required_steps']
        if step != 'onecli' and not (step == 'auth' and provider == 'claude')
    ]
    statuses = scenario.get('required_step_statuses')
    if provider == 'claude' and statuses:
        statuses.pop('auth', None)
    return scenario


def env_gateway(text):
    stamped = None
    for line in text.splitlines():
        key, separator, value = line.partition('=')
        if separator and key.strip() == 'NANOCLAW_GATEWAY_PROVIDER':
            stamped = value.strip().strip('"\'').lower()
    return stamped


def inspect_gateway(root, requested, seam, service_pid=None):
    """Return (installed kind or None, problem or None) for the finished wizard.

    On a seam ref the installed kind is the NANOCLAW_GATEWAY_PROVIDER stamp
    installGateway writes to .env; the running service must not select
    another kind through its environment. Older refs only ever install OneCLI
    (the scenario requires its `onecli` step).
    """
    if not seam:
        return 'onecli', None if requested == 'onecli' else 'This ref installs OneCLI only'
    stamped = env_gateway(read_limited(root / '.env')) if (root / '.env').is_file() else None
    if stamped != requested:
        return stamped, 'Wizard stamped gateway %s, not the requested %s' % (stamped or 'none', requested)
    environ = Path('/proc') / str(service_pid) / 'environ'
    if service_pid and environ.exists():
        for entry in environ.read_bytes().split(b'\0'):
            key, separator, value = entry.partition(b'=')
            if separator and key == b'NANOCLAW_GATEWAY_PROVIDER':
                selected = value.decode('utf-8', 'replace').strip().strip('"\'').lower()
                if selected != requested:
                    return selected, 'Service environment selects gateway %s, not %s' % (selected, requested)
    return requested, None


def pinned_codex_cli(root):
    """The exact Codex CLI pin the tested add-codex skill merges into container/cli-tools.json."""
    skill = root / '.claude/skills/add-codex/SKILL.md'
    if skill.is_symlink() or not skill.is_file():
        raise Failure('preflight', 'The tested ref has no add-codex skill to read the Codex CLI pin from', 65)
    for body in re.findall(r'```nc:json-merge into:container/cli-tools\.json[^\n]*\n(.*?)\n```', read_limited(skill), re.S):
        try:
            entry = json.loads(body)
        except ValueError:
            continue
        version = entry.get('version') if isinstance(entry, dict) and entry.get('name') == CODEX_CLI_PACKAGE else None
        if isinstance(version, str) and re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?', version):
            return version
    raise Failure('preflight', 'Could not read an exact Codex CLI pin from the add-codex skill', 65)


def codex_cli_version(executable, environment):
    try:
        output = subprocess.run([executable, '--version'], env=environment, capture_output=True, text=True,
                                timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r'(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)', output or '')
    return match[1] if match else None


def install_host_codex_cli(version):
    """Install the pinned Codex CLI for the login the tested payload spawns on the host.

    Runs when the wizard reaches the provider auth prompt (Node and npm exist
    by then). The payload at this ref has no pinned-CLI fallback of its own,
    and a host copy of another version writes a login file the payload's
    adapter may not understand (exe.dev's 0.155.1 vs the 0.146.0 pin on
    2026-09-22), so the exact pin is installed under ~/.local and preferred
    unless the host already has that very version.
    """
    environment = verification_environment()
    prefix = Path.home() / '.local'
    pinned = prefix / 'bin' / 'codex'
    # The wizard resolves `codex` with ~/.local/bin first (child_environment),
    # so the copy that matters is the one at that path, not any other on PATH.
    host = shutil.which('codex', path=environment['PATH'])
    host_version = codex_cli_version(host, environment) if host else None
    if os.access(pinned, os.X_OK) and codex_cli_version(str(pinned), environment) == version:
        return {'installed_by_driver': False, 'version_requested': version, 'host_version': host_version,
                'executable': str(pinned)}
    try:
        subprocess.run(
            ['npm', 'install', '-g', '--prefix', str(prefix), '--no-fund', '--no-audit',
             CODEX_CLI_PACKAGE + '@' + version],
            env=environment, capture_output=True, text=True, timeout=900, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise Failure('auth', 'Could not install the pinned Codex CLI on the host for device pairing', 1)
    if not os.access(pinned, os.X_OK) or codex_cli_version(str(pinned), environment) != version:
        raise Failure('auth', 'Pinned Codex CLI install did not produce the requested version', 1)
    # nanoclaw.sh may put npm's global prefix ahead of ~/.local/bin when it has
    # to recover pnpm; verification_environment() mirrors that order, so a copy
    # it resolves elsewhere would shadow the pin inside the wizard.
    resolved = shutil.which('codex', path=verification_environment()['PATH'])
    if resolved and Path(resolved).resolve() != pinned.resolve() \
            and codex_cli_version(resolved, environment) != version:
        raise Failure('auth', 'Another Codex CLI on the wizard PATH would shadow the pinned one: ' + resolved, 1)
    return {'installed_by_driver': True, 'version_requested': version, 'prefix': str(prefix),
            'host_version': host_version, 'executable': str(pinned)}


def verify_iron_codex_credential(root):
    """The Iron equivalent of the OneCLI vault listing: the adapter's own has('codex').

    Iron stores a ChatGPT session as a token broker plus two static secrets
    and records their ids in its metadata file. Values are never read; the
    adapter's `has` re-checks isolation and broker health through Iron Control.
    """
    metadata = root / IRON_CONTROL_DIR / 'codex.json'
    if metadata.is_symlink() or not metadata.is_file():
        raise Failure('auth', 'Iron Proxy holds no Codex credential metadata', 1)
    try:
        state = json.loads(read_limited(metadata))
    except ValueError:
        raise Failure('auth', 'Iron Proxy Codex credential metadata is unreadable', 1)
    secret_ids = state.get('secretIds') if isinstance(state, dict) else None
    if not isinstance(secret_ids, list) or len(secret_ids) != 2 or not state.get('brokerId'):
        raise Failure('auth', 'Iron Proxy does not hold one dedicated Codex ChatGPT session', 1)
    check = root / 'logs' / 'e2e-wizard-iron-codex-check.mts'
    check.write_text(
        "import { createCredentialStore } from '../.claude/skills/add-iron-proxy/scripts/credential-store.ts';\n"
        "const has = await createCredentialStore(process.argv[2]).has('codex');\n"
        "process.stdout.write(JSON.stringify({ has }));\n"
    )
    try:
        report = json.loads(subprocess.check_output(
            ['pnpm', 'exec', 'tsx', str(check), str(root)], cwd=root, text=True, timeout=120,
            env=verification_environment(), stderr=subprocess.DEVNULL,
        ))
    except (OSError, subprocess.SubprocessError, ValueError):
        raise Failure('auth', 'Could not verify the Iron Proxy Codex credential through the adapter', 1)
    finally:
        check.unlink(missing_ok=True)
    if report.get('has') is not True:
        raise Failure('auth', 'Iron Proxy adapter reports no usable Codex credential', 1)
    # Key names must not look like credentials to the redactor (no 'secret').
    return {'gateway': 'iron-proxy', 'has_codex': True, 'static_entries': 2, 'broker': True,
            'verified_via': "add-iron-proxy credential-store has('codex')"}


def gateway_trust_mount_observed(root):
    """Whether one of this install's agent containers carried the read-only gateway-trust CA mount (Iron)."""
    trust_root = str((root / 'data' / 'gateway-trust').resolve()) + os.sep
    try:
        ids = subprocess.run(['docker', 'ps', '-aq', '--filter', 'name=^ncl-'], capture_output=True, text=True,
                             timeout=10, env=child_environment()).stdout.split()
        if not ids:
            return False
        mounts = subprocess.run(['docker', 'inspect', '--format', '{{json .Mounts}}', *ids],
                                capture_output=True, text=True, timeout=15, env=child_environment()).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in mounts.splitlines():
        try:
            entries = json.loads(line)
        except ValueError:
            continue
        for mount in entries if isinstance(entries, list) else []:
            if str(mount.get('Source', '')).startswith(trust_root) and mount.get('RW') is False:
                return True
    return False


def verify_opencode_target(root, backend, model, base_url=None, model_provider=None, gateway='onecli'):
    # Read only provider-owned non-secret defaults; never export the .env file.
    runtime_provider = provider_discovery().validate_model('opencode', backend, model, base_url, model_provider)
    expected = {'OPENCODE_PROVIDER': runtime_provider, 'OPENCODE_MODEL': model,
                'OPENCODE_SMALL_MODEL': model, 'OPENCODE_BASE_URL': base_url or 'native'}
    actual = {}
    for line in read_limited(root / '.env').splitlines():
        key, separator, value = line.partition('=')
        if separator and key in {*expected, 'OPENCODE_AUTH_MODE'}:
            actual[key] = value.strip().strip('"\'')
    if any(actual.get(key) != value for key, value in expected.items()) or actual.get('OPENCODE_AUTH_MODE'):
        raise Failure('verify', 'OpenCode backend/model defaults do not match the selected run', 1)
    receipt = {'backend': backend, 'model': model, 'model_provider': runtime_provider, 'base_url': base_url or 'native',
               'retained_agent': verify_retained_provider_group(root, 'opencode')}
    if gateway == 'iron-proxy':
        receipt['gateway_trust_mount_observed'] = gateway_trust_mount_observed(root)
    return receipt


def verify_codex_target(root, terminal, method, require_fallback=False, gateway='onecli',
                        host_codex_absent_before=None, host_codex_cli=None):
    if method['value'] != 'device':
        raise Failure('preflight', 'This supervised adapter supports only Codex device pairing', 64)
    manifest = json.loads(read_limited(root / 'container/cli-tools.json'))
    matches = [item for item in manifest if item.get('name') == '@openai/codex']
    if len(matches) != 1 or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?', matches[0].get('version', '')):
        raise Failure('payload', 'Installed manifest has no exact Codex CLI pin', 65)
    version = matches[0]['version']
    fallback_proof = f'Preparing the pinned Codex CLI ({version}) for sign-in' in terminal.text()
    if require_fallback and not fallback_proof:
        raise Failure('auth', 'Missing proof that the manifest-pinned Codex CLI fallback ran', 1)
    if not terminal.handoff_emitted:
        raise Failure('auth', 'Device handoff was never observed', 1)
    if (Path.home() / '.codex/auth.json').exists():
        raise Failure('auth', 'Isolated Codex login left a personal auth file behind', 1)
    if gateway == 'iron-proxy':
        vault = verify_iron_codex_credential(root)
    else:
        try:
            secrets_report = json.loads(subprocess.check_output(
                ['onecli', 'secrets', 'list'], text=True, timeout=30, env=verification_environment(),
            ))
        except (OSError, subprocess.SubprocessError, ValueError):
            raise Failure('auth', 'Could not verify the OneCLI Codex vault entry', 1)
        entries = secrets_report.get('data', [])
        matching = [item for item in entries if (
            str(item.get('name', '')).lower() == 'codex'
            and str(item.get('hostPattern', '')).lower() == 'chatgpt.com'
        )]
        if len(matching) != 1:
            raise Failure('auth', 'OneCLI does not contain exactly one dedicated Codex session', 1)
        vault = {'name': 'Codex', 'host_pattern': 'chatgpt.com', 'entry_count': 1}
    if host_codex_absent_before is None:
        host_codex_absent_before = shutil.which('codex') is None
    return {
        'host_codex_absent_before_wizard': host_codex_absent_before,
        'host_codex_cli': host_codex_cli,
        'gateway': gateway,
        'personal_auth_absent_before_wizard': True,
        'personal_auth_absent_after_wizard': True,
        'cli_package': '@openai/codex',
        'cli_version': version,
        'cli_fallback_required': require_fallback,
        'fallback_proof': fallback_proof,
        'auth_method': 'device',
        'device_handoff_observed': True,
        'vault': vault,
    }


def verification_environment():
    """Find tools installed by the wizard without changing its parent shell.

    nanoclaw.sh restores ~/.local/bin and npm's global prefix after bootstrap.
    Its child PATH cannot propagate back to this verifier, so replay those
    read-only lookups for the post-wizard CLI checks as well.
    """
    environment = os.environ.copy()
    paths = environment.get('PATH', os.defpath).split(os.pathsep)
    local_bin = str(Path.home() / '.local/bin')
    if local_bin not in paths:
        paths.insert(0, local_bin)
    environment['PATH'] = os.pathsep.join(paths)
    npm = shutil.which('npm', path=environment['PATH'])
    if not shutil.which('pnpm', path=environment['PATH']) and npm:
        try:
            prefix = subprocess.run([npm, 'config', 'get', 'prefix'], env=environment,
                                    capture_output=True, text=True, timeout=30, check=True).stdout.strip()
            directory = Path(prefix) / 'bin'
            if prefix and directory.is_absolute() and os.access(directory / 'pnpm', os.X_OK):
                environment['PATH'] = str(directory) + os.pathsep + environment['PATH']
        except (OSError, subprocess.SubprocessError):
            pass  # The required CLI check below still fails if tools are missing.
    return environment


def resolve_installed_provider_names(root, pairs):
    """Resolve provider pairs through the exact NanoClaw build under test."""
    resolver = root / 'dist/providers/provider-name.js'
    program = (
        "import { pathToFileURL } from 'node:url';"
        "const modulePath = process.argv[1];"
        "const pairs = JSON.parse(process.argv[2]);"
        "const loaded = await import(pathToFileURL(modulePath).href);"
        "process.stdout.write(JSON.stringify("
        "pairs.map(([sessionProvider, configProvider]) => "
        "loaded.resolveProviderName(sessionProvider, configProvider))));"
    )
    try:
        resolved = json.loads(subprocess.check_output(
            ['node', '--input-type=module', '-e', program, str(resolver), json.dumps(pairs)],
            cwd=root, text=True, timeout=30, env=verification_environment(),
        ))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise Failure('verify', 'Could not run the installed NanoClaw provider resolver', 1)
    if (not isinstance(resolved, list) or len(resolved) != len(pairs)
            or not all(isinstance(item, str) and item for item in resolved)):
        raise Failure('verify', 'Installed NanoClaw provider resolver returned invalid output', 1)
    return resolved


def verify_retained_provider_group(root, provider):
    command = root / 'bin/ncl'
    environment = verification_environment()
    try:
        listed = json.loads(subprocess.check_output(
            [str(command), 'groups', 'list', '--json'], cwd=root, text=True, timeout=30, env=environment,
        ))
        groups = listed.get('data')
        if not isinstance(groups, list) or len(groups) != 1 or not groups[0].get('id'):
            raise ValueError()
        config_frame = json.loads(subprocess.check_output(
            [str(command), 'groups', 'config', 'get', '--id', str(groups[0]['id']), '--json'],
            cwd=root, text=True, timeout=30, env=environment,
        ))
        config = config_frame.get('data')
        sessions_frame = json.loads(subprocess.check_output(
            [str(command), 'sessions', 'list', '--json'], cwd=root, text=True, timeout=30, env=environment,
        ))
        sessions = sessions_frame.get('data')
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        raise Failure('verify', 'Could not verify the retained agent provider through ncl', 1)
    group_id = str(groups[0]['id'])
    if not isinstance(config, dict) or not isinstance(sessions, list):
        raise Failure('verify', 'Retained agent provider data has an invalid shape', 1)
    retained = [item for item in sessions if (
        isinstance(item, dict) and str(item.get('agent_group_id')) == group_id
    )]
    if not retained:
        raise Failure('verify', 'Retained agent has no session to verify', 1)
    configured = config.get('provider')
    session_providers = [item.get('agent_provider') for item in retained]
    resolved = resolve_installed_provider_names(
        root, [[session_provider, configured] for session_provider in session_providers],
    )
    if any(item != provider for item in resolved):
        raise Failure('verify', 'Retained agent resolves to the wrong provider', 1)
    return {
        'group_count': 1,
        'provider': provider,
        'configured_provider': configured,
        'session_providers': session_providers,
        'effective_providers': resolved,
        'verified_via': 'ncl and exact installed resolveProviderName',
    }


def verify_claude_target(terminal, host_claude_absent_before):
    if not terminal.handoff_emitted:
        raise Failure('auth', 'Claude subscription handoff was not completed', 1)
    if 'Got token:' not in terminal.text() or 'Saving it to your OneCLI vault' not in terminal.text():
        raise Failure('auth', 'Missing proof that Claude setup-token was parsed and vaulted', 1)
    try:
        report = json.loads(subprocess.check_output(
            ['onecli', 'secrets', 'list'], text=True, timeout=30, env=verification_environment(),
        ))
    except (OSError, subprocess.SubprocessError, ValueError):
        raise Failure('auth', 'Could not verify the OneCLI Anthropic vault entry', 1)
    entries = report.get('data', [])
    matching = [item for item in entries if (
        str(item.get('name', '')).lower() == 'anthropic'
        and str(item.get('hostPattern', '')).lower() == 'api.anthropic.com'
    )]
    if len(matching) != 1:
        raise Failure('auth', 'OneCLI does not contain exactly one Anthropic subscription entry', 1)
    return {
        'host_claude_absent_before_wizard': host_claude_absent_before,
        'auth_method': 'subscription',
        'authorization_handoff_observed': True,
        'authorization_response_submitted': terminal.handoff_response_submitted,
        'token_capture_and_vault_proof': True,
        'vault': {'name': 'Anthropic', 'host_pattern': 'api.anthropic.com', 'entry_count': 1},
    }


class Redactor:
    """Redact whole rendered/log texts, so chunk boundaries cannot split secrets."""
    def __init__(self, values):
        self.values = sorted({v for v in values if v and len(v) >= 8}, key=len, reverse=True)

    def clean(self, text):
        text = ANSI.sub('', text)
        text = redact_claude_auth_urls(text)
        for value in self.values:
            # Terminal wrapping can introduce whitespace inside a long credential.
            text = re.sub(r'\s*'.join(map(re.escape, value)), '[REDACTED]', text)
        text = TOKEN.sub('[REDACTED]', text)
        text = DEVICE_CODE.sub('[REDACTED]', text)
        text = re.sub(r'(?im)((?:[\w-]*(?:token|password|secret|api.?key)[\w-]*)["\x27]?\s*[:=]\s*)[^\s,}\n]+', r'\1[REDACTED]', text)
        text = re.sub(r'(?i)(--(?:value|token|password|api-key)\s+)(?:"[^"]*"|\x27[^\x27]*\x27|\S+)', r'\1[REDACTED]', text)
        compact = re.sub(r'\s+', '', text)
        if any(re.sub(r'\s+', '', v) in compact for v in self.values):
            raise Failure('redaction', 'Credential remained after sanitization', 74)
        return text


def read_limited(path):
    if path.is_symlink() or not path.is_file():
        raise Failure('evidence', 'Missing or unsafe evidence file: ' + path.name)
    with path.open('rb') as source:
        data = source.read(MAX_LOG_BYTES + 1)
    if len(data) > MAX_LOG_BYTES:
        raise Failure('evidence', 'Evidence file exceeds size limit: ' + path.name)
    return data.decode('utf-8', errors='replace')


def private_values(root, credential, extra=()):
    values = [credential, *extra]
    # Generated gateway credentials may appear in raw step output. Read only
    # known local configuration; these files themselves are never exported.
    iron = root / IRON_CONTROL_DIR
    for path in [root / '.env', Path.home() / '.config/onecli/config.json',
                 *(sorted(iron.glob('*.env')) if iron.is_dir() and not iron.is_symlink() else [])]:
        if not path.is_file() or path.is_symlink():
            continue
        text = read_limited(path)
        if path.suffix == '.json':
            def visit(value):
                if isinstance(value, dict):
                    for key, val in value.items():
                        if isinstance(val, str) and re.search(r'token|secret|password|api.?key', key, re.I):
                            values.append(val)
                        else:
                            visit(val)
                elif isinstance(value, list):
                    for val in value:
                        visit(val)
            try:
                visit(json.loads(text))
            except ValueError:
                raise Failure('redaction', 'Cannot parse gateway configuration', 74)
        else:
            # Iron Control's files also carry encryption keys (*_ENCRYPTION_*_KEY).
            pattern = r'TOKEN|SECRET|PASSWORD|KEY' if path.parent == iron else r'TOKEN|SECRET|PASSWORD|API_KEY'
            for line in text.splitlines():
                key, sep, value = line.partition('=')
                if sep and re.search(pattern, key):
                    values.append(value.strip().strip('\"\x27'))
    return values


def child_environment():
    # No NANOCLAW_SKIP, provider/channel auth, display-name presets, or other
    # inherited product settings may silently perform/bypass wizard choices.
    allowed = ('HOME', 'USER', 'LOGNAME', 'PATH', 'SHELL', 'TMPDIR',
               'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS',
               'GIT_CONFIG_NOSYSTEM', 'GIT_CONFIG_GLOBAL',
               'NANOCLAW_CHANNELS_REMOTE')
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env.update(TERM='xterm-256color', COLORTERM='truecolor', LANG='C.UTF-8', LC_ALL='C.UTF-8', TZ='UTC')
    if PREFER_LOCAL_BIN:
        local_bin = str(Path.home() / '.local/bin')
        paths = [p for p in env.get('PATH', os.defpath).split(os.pathsep) if p != local_bin]
        env['PATH'] = os.pathsep.join([local_bin, *paths])
    return env


class WizardTerminal:
    def __init__(self, scenario, values, timeout=1200, idle_timeout=180, columns=160, rows=48,
                 handoff_method=None, payload_verifier=None, run_id=None,
                 handoff_path=None, handoff_response_path=None, pre_auth_hook=None):
        import pyte
        self.scenario, self.values = scenario, values
        self.timeout, self.idle_timeout = timeout, idle_timeout
        self.columns, self.rows = columns, rows
        self.screen = pyte.HistoryScreen(columns, rows, history=30000)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.choices, self.seen = [], set()
        self.reply_verified = False
        self.output_bytes = 0
        self.last_prompt_index = -1
        self.private_values = []
        self.handoff_emitted = False
        self.handoff_method = handoff_method
        self.handoff_idle_timeout = 600
        self.payload_verifier = payload_verifier
        self.payload_receipt = None
        self.pre_auth_hook = pre_auth_hook
        self.pre_auth_receipt = None
        self.hook_finished_at = None
        self.handoff_response_submitted = False
        self.claude_command_submitted = False
        self.claude_setup_token_observed = False
        self.raw_handoff_input = ''
        self.raw_handoff_buffer = ''
        self.run_id = run_id
        self.handoff_nonce = secrets.token_hex(16)
        self.handoff_path = handoff_path
        self.handoff_response_path = handoff_response_path

    def text(self):
        history = [''.join(line[x].data for x in range(self.columns)).rstrip()
                   for line in self.screen.history.top]
        return '\n'.join(history + [line.rstrip() for line in self.screen.display])

    def live_screen_text(self):
        """Return the current rendered screen without its unused trailing rows."""
        return '\n'.join(line.rstrip() for line in self.screen.display).rstrip()

    def feed(self, data):
        self.output_bytes += len(data)
        if self.output_bytes > MAX_LOG_BYTES or len(self.screen.history.top) >= 30000:
            raise Failure('terminal', 'Terminal evidence limit exceeded')
        decoded = self.decoder.decode(data)
        self.stream.feed(decoded)
        # Retain raw input so split ANSI sequences complete on the next feed.
        self.raw_handoff_input = (self.raw_handoff_input + decoded)[-131072:]
        self.raw_handoff_buffer = ANSI.sub('', self.raw_handoff_input)
        if 'setup-token' in self.raw_handoff_buffer:
            self.claude_setup_token_observed = True
        for captured in CLAUDE_OAUTH_CAPTURE.findall(self.raw_handoff_buffer):
            token = re.sub(r'\s+', '', captured)
            if token not in self.private_values:
                self.private_values.append(token)
        if self.handoff_method == 'device' and not self.handoff_emitted:
            # Codex prints a short prompt at the top of a freshly cleared
            # 48-row screen. Trailing blank rows must not push it outside a
            # fixed tail slice; inspect the complete live screen, not history.
            block = '\n'.join(self.screen.display)
            # A partial code can itself match a shorter valid shape. Require
            # the code line's newline before emitting an immutable handoff.
            codes = DEVICE_CODE_PROMPT.findall(self.raw_handoff_buffer)
            if DEVICE_URL in block and codes and 'Enter this one-time code' in block:
                code = codes[-1]
                self.private_values.append(code)
                write_json(self.handoff_path, {
                    'run_id': self.run_id,
                    'nonce': self.handoff_nonce,
                    'user_code': code,
                    'verification_url': DEVICE_URL,
                    'created_at': now(),
                })
                self.handoff_path.chmod(0o600)
                self.handoff_emitted = True
        elif self.handoff_method == 'subscription' and not self.handoff_emitted:
            # Claude runs in a nested PTY and redraws its long URL. Parse the
            # emulator's complete live screen so physical wraps and cursor
            # movement have already been resolved. The final code prompt is
            # the terminator proving the rendered URL is complete.
            rendered = self.live_screen_text()
            urls = find_claude_auth_urls(rendered)
            active_code_prompt = re.search(
                r'(?:^|\n)\s*Paste code here if prompted[^\n]*\Z', rendered,
            )
            if urls and active_code_prompt and self.claude_setup_token_observed:
                url = urls[-1]
                self.private_values.append(url)
                write_json(self.handoff_path, {
                    'run_id': self.run_id,
                    'nonce': self.handoff_nonce,
                    'provider': 'claude',
                    'auth_method': 'subscription',
                    'authorization_url': url,
                    'response_required': True,
                    'created_at': now(),
                })
                self.handoff_path.chmod(0o600)
                self.handoff_emitted = True

    def handle_handoff(self, fd):
        text = self.raw_handoff_buffer
        if (self.handoff_method == 'subscription' and not self.claude_command_submitted
                and 'Press Enter to continue, or edit the command first.' in text
                and '$ claude setup-token' in text):
            os.write(fd, b'\r')
            self.claude_command_submitted = True
        # Only submit into the prompt that is currently visible at the bottom
        # of the rendered terminal. Raw nested-PTY bytes can contain obsolete
        # prompts and duplicated carriage returns from earlier redraws.
        rendered = self.live_screen_text()
        active_code_prompt = re.search(
            r'(?:^|\n)\s*Paste code here if prompted[^\n]*\Z', rendered,
        )
        if (self.handoff_method == 'subscription' and self.handoff_emitted
                and not self.handoff_response_submitted
                and active_code_prompt
                and self.handoff_response_path.exists()):
            try:
                flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                response_fd = os.open(self.handoff_response_path, flags)
                metadata = os.fstat(response_fd)
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077
                        or metadata.st_uid != os.getuid() or metadata.st_size > 8192):
                    raise Failure('auth', 'Private authorization response file must be an owned regular 0600 file', 66)
                with os.fdopen(response_fd, 'rb') as source:
                    response_fd = None
                    raw_response = source.read(8193)
                if len(raw_response) > 8192:
                    raise ValueError()
                response_doc = json.loads(raw_response.decode('utf-8'))
                response = response_doc['authorization_code']
            except Failure:
                raise
            except (OSError, ValueError, KeyError, TypeError):
                raise Failure('auth', 'Private authorization response is not valid JSON', 66)
            finally:
                if 'response_fd' in locals() and response_fd is not None:
                    os.close(response_fd)
            if (response_doc.get('run_id') != self.run_id
                    or response_doc.get('nonce') != self.handoff_nonce):
                raise Failure('auth', 'Private authorization response belongs to another run', 66)
            if (not isinstance(response, str) or not 8 <= len(response) <= 4096
                    or any(ord(char) < 0x20 or ord(char) == 0x7f for char in response)):
                raise Failure('auth', 'Private authorization response has an invalid shape', 66)
            self.private_values.append(response)
            os.write(fd, response.encode() + b'\r')
            self.handoff_response_path.unlink()
            self.handoff_response_submitted = True

    def active(self):
        lines = self.screen.display
        indices = [i for i, line in enumerate(lines) if re.match(r'^\s*[◆◇■●]\s+', line)]
        if not indices:
            return None
        start = indices[-1]
        # Completed spinners can leave a diamond in older rendered rows. Only
        # the most recent component can own input, and active clack prompts
        # have a closing corner below their input/options.
        if not re.match(r'^\s*◆\s+', lines[start]) or not any(
                re.match(r'^\s*└', line) for line in lines[start + 1:]):
            return None
        # A wrapped prompt can span lines before its options/input. The known
        # prompt must be a complete prefix, and matching uses only live screen.
        block = '\n'.join(lines[start:]).strip()
        header = re.sub(r'^\s*◆\s+', '', lines[start]).strip()
        for line in lines[start + 1:]:
            if re.match(r'^\s*[│└◇◆■]', line) or re.search('[●○]', line):
                break
            if line.strip():
                header += ' ' + line.strip()
        return header, block

    def answer(self, fd, active):
        header, block = active
        matches = []
        for index, prompt in enumerate(self.scenario['prompts']):
            message = prompt.get('prompt') or self.values[prompt['prompt_from']]
            if header == message:
                matches.append((index, prompt))
        if len(matches) != 1:
            raise Failure('prompt', 'Unknown active wizard prompt: ' + header)
        index, prompt = matches[0]
        prompt_id = prompt['id']
        if prompt_id == 'auth' and self.payload_verifier and self.payload_receipt is None:
            self.payload_receipt = self.payload_verifier()
        if prompt_id == 'auth' and self.pre_auth_hook and self.pre_auth_receipt is None:
            # Host preparation the provider's login needs; it can take a while,
            # so the run loop treats its completion like fresh output.
            self.pre_auth_receipt = self.pre_auth_hook()
            self.hook_finished_at = time.monotonic()
        if prompt_id in self.seen or index < self.last_prompt_index:
            raise Failure('prompt', 'Repeated or out-of-order prompt: ' + prompt_id)
        missing = [p['id'] for p in self.scenario['prompts'][:index]
                   if p.get('required') and p['id'] not in self.seen]
        if missing:
            raise Failure('proof', 'Wizard skipped required prompts: ' + ', '.join(missing))
        if prompt.get('require_reply'):
            transcript = self.text()
            question = self.values['challenge']
            # Only accept a standalone computed answer AFTER the submitted
            # question, never numbers in typed input, earlier ping, or headers.
            position = transcript.rfind(question)
            reply = transcript[position + len(question):] if position >= 0 else ''
            expected = self.values['answer']
            if not re.search(r'(?m)^\s*(?:│\s*)?(?:\*\*)?' + re.escape(expected) + r'(?:\*\*)?\s*$', reply):
                raise Failure('reply', 'No matching retained-agent reply', 2)
            self.reply_verified = True
        if 'select' in prompt or 'select_from' in prompt:
            desired = prompt.get('select') or self.values[prompt['select_from']]
            options = []
            for line in block.splitlines()[1:]:
                for marker, label in re.findall(r'([●○])\s*([^●○]+)', line):
                    options.append((marker, label.strip().rstrip('/').strip()))
            targets = [i for i, (_, label) in enumerate(options)
                       if label == desired or label.startswith(desired + ' (')]
            selected = [i for i, (marker, _) in enumerate(options) if marker == '●']
            if prompt_id == 'opencode-model-choice' and not targets and len(selected) == 1:
                # Catalogs scroll beyond the viewport. Traverse this known menu
                # to the final manual entry, bounded by the catalog and timeout.
                self.model_scrolls = getattr(self, 'model_scrolls', 0) + 1
                if self.model_scrolls > 1000:
                    raise Failure('prompt', 'OpenCode manual model choice was not found')
                os.write(fd, b'\x1b[B')
                return False
            if len(targets) != 1 or len(selected) != 1:
                raise Failure('prompt', 'Missing or ambiguous choice in ' + prompt_id)
            if targets[0] != selected[0]:
                os.write(fd, b'\x1b[B' if targets[0] > selected[0] else b'\x1b[A')
                return False
            value = desired
            data = b'\r'
        else:
            value = prompt.get('text', self.values.get(prompt.get('text_from'), ''))
            data = value.encode('utf-8') + b'\r'
        # Never export input bytes. Credential choices record only the prompt id.
        self.choices.append({'id': prompt_id, 'at': now(),
                             'value': '[REDACTED]' if prompt.get('text_from') == 'credential' else value})
        self.seen.add(prompt_id)
        self.last_prompt_index = index
        os.write(fd, data)
        return True

    def run(self, root, command=None):
        command = command or ['bash', 'nanoclaw.sh']
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.chdir(root)
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', self.rows, self.columns, 0, 0))
                os.execvpe(command[0], command, child_environment())
            except BaseException:
                os._exit(127)
        start = last_output = time.monotonic()
        stable_at, active_state, submitted = start, None, None
        status, last_action = None, start
        try:
            while True:
                clock = time.monotonic()
                if clock - start > self.timeout:
                    raise Failure('timeout', 'Wizard exceeded total timeout', 124)
                idle_limit = self.handoff_idle_timeout if self.handoff_emitted else self.idle_timeout
                if clock - last_output > idle_limit:
                    raise Failure('timeout', 'Wizard stopped producing output', 124)
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        chunk = b''
                    if chunk:
                        self.feed(chunk)
                        last_output = clock
                self.handle_handoff(fd)
                if status is None:
                    done, raw_status = os.waitpid(pid, os.WNOHANG)
                    if done:
                        status = os.waitstatus_to_exitcode(raw_status)
                if status is not None and clock - last_output > 0.15:
                    break
                active = self.active()
                if active != active_state:
                    active_state, stable_at = active, clock
                if submitted and (not active or active[0] != submitted):
                    submitted = None
                if active and not submitted and clock - stable_at >= 0.15 and clock - last_action >= 0.15:
                    if self.answer(fd, active):
                        submitted = active[0]
                    last_action = clock
                    if self.hook_finished_at and self.hook_finished_at > last_output:
                        last_output = self.hook_finished_at
                # A clack cancellation is a failure even if the process exits0.
                if re.search(r'(?im)(?:^\s*■|Setup cancell?ed\.)', '\n'.join(self.screen.display)):
                    raise Failure('cancelled', 'Wizard cancelled', 130)
            if status != 0:
                raise Failure('wizard', 'Public wizard exited unsuccessfully', status if status and status > 0 else 1)
            missing = [p['id'] for p in self.scenario['prompts'] if p.get('required') and p['id'] not in self.seen]
            if missing:
                raise Failure('proof', 'Missing required prompt proof: ' + ', '.join(missing))
            if not self.reply_verified or self.scenario['completed_text'] not in self.text():
                raise Failure('proof', 'Missing wizard completion or retained reply')
            return status
        finally:
            def signal_group(sig):
                nonlocal status
                try:
                    os.killpg(pid, sig)
                except ProcessLookupError:
                    return
                except PermissionError:
                    # Darwin can return EPERM for an exited, unreaped group
                    # leader. Reap only our child and retry; a live group's
                    # permission error still fails rather than being ignored.
                    if status is not None:
                        raise
                    done, raw = os.waitpid(pid, os.WNOHANG)
                    if not done:
                        raise
                    status = os.waitstatus_to_exitcode(raw)
                    try:
                        os.killpg(pid, sig)
                    except ProcessLookupError:
                        pass
            try:
                # Only our PTY process group; do not touch product services.
                signal_group(signal.SIGTERM)
                if status is None:
                    deadline = time.monotonic() + 1
                    while time.monotonic() < deadline:
                        done, raw = os.waitpid(pid, os.WNOHANG)
                        if done:
                            status = os.waitstatus_to_exitcode(raw)
                            break
                        time.sleep(0.02)
                    else:
                        signal_group(signal.SIGKILL)
                        if status is None:
                            _, raw = os.waitpid(pid, 0)
                            status = os.waitstatus_to_exitcode(raw)
                # A descendant can ignore TERM after the immediate child exits.
                signal_group(signal.SIGKILL)
            finally:
                os.close(fd)



def progression(text):
    steps, inputs = {}, {}
    records = re.split(r'(?m)^=== ', text)[1:]
    for record in records:
        header, _, body = record.partition('\n')
        fields = dict(re.findall(r'^  ([\w-]+): (.*)$', body, re.M))
        step = re.match(r'\[[^\]]+\] ([\w-]+) \[[^\]]+\] → ([\w-]+) ===$', header)
        choice = re.match(r'\[[^\]]+\] user-input → ([\w-]+) ===$', header)
        if step:
            name, status = step.groups()
            if status in ('failed', 'aborted'):
                raise Failure('product-step', 'Wizard step failed: ' + name)
            if name in steps:
                raise Failure('proof', 'Repeated setup step: ' + name)
            steps[name] = {'status': status, **fields}
        elif choice:
            inputs[choice[1]] = fields.get('value')
    return steps, inputs


def status_block(text, name):
    blocks = re.findall(r'^=== NANOCLAW SETUP: ' + re.escape(name) + r' ===\r?\n(.*?)^=== END ===', text, re.M | re.S)
    if len(blocks) != 1:
        raise Failure('proof', 'Missing or ambiguous ' + name + ' status block')
    return dict(re.findall(r'^([A-Z_]+):[ \t]*([^\r\n]*)', blocks[0], re.M))


def check_progress(root, scenario):
    text = read_limited(root / 'logs/setup.log')
    if 'invocation: nanoclaw.sh' not in text or not re.search(r'^## .+ · completed \(total .+\)$', text, re.M) or ' · aborted at ' in text:
        raise Failure('proof', 'Public wizard has no clean completion footer')
    steps, inputs = progression(text)
    for name in scenario['required_steps']:
        expected_status = scenario.get('required_step_statuses', {}).get(name, 'success')
        if steps.get(name, {}).get('status') != expected_status:
            raise Failure('proof', 'Required wizard step was not successful: ' + name)
    for name, value in scenario['required_inputs'].items():
        if inputs.get(name) != value:
            raise Failure('proof', 'Missing or incorrect wizard choice: ' + name)
    def raw(name, block):
        relative = steps[name].get('raw', '')
        path = root / relative
        if not relative.startswith('logs/setup-steps/') or not path.resolve().is_relative_to((root / 'logs/setup-steps').resolve()):
            raise Failure('proof', 'Invalid step log reference: ' + name)
        return status_block(read_limited(path), block)
    verify = raw('verify', 'VERIFY')
    for field, value in scenario['verify'].items():
        if verify.get(field) != value:
            raise Failure('verify', 'Final verification mismatch: ' + field)
    if not verify.get('REGISTERED_GROUPS', '').isdigit() or int(verify['REGISTERED_GROUPS']) < 1:
        raise Failure('verify', 'No retained registered agent')
    if verify.get('WIRING') or verify.get('SLACK_INSTALL'):
        raise Failure('verify', 'Setup left pending or external channel work')
    timezone = raw('timezone', 'TIMEZONE')
    if timezone.get('RESOLVED_TZ') != 'UTC' or not re.search(r'^TZ=[\"\']?UTC[\"\']?$', read_limited(root / '.env'), re.M):
        raise Failure('verify', 'UTC was not persisted by the wizard')
    service = raw('service', 'SETUP_SERVICE')
    if service.get('STATUS') != 'success' or service.get('SERVICE_LOADED') != 'true':
        raise Failure('service', 'Wizard did not load its service')
    if Path(service.get('PROJECT_PATH', '')).resolve() != root.resolve():
        raise Failure('service', 'Service belongs to another checkout')
    return verify, service


def process_arguments(pid):
    proc = Path('/proc') / str(pid) / 'cmdline'
    if proc.exists():
        return [os.fsdecode(part) for part in proc.read_bytes().split(b'\0') if part]
    query = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=15)
    if query.returncode:
        raise Failure('service', 'Current service process disappeared')
    return shlex.split(query.stdout.strip())


def verify_live_service(root, service):
    kind = service.get('SERVICE_TYPE')
    if kind in ('systemd-user', 'systemd-system'):
        unit = service.get('SERVICE_UNIT', '')
        if not re.fullmatch(r'[a-zA-Z0-9_.@-]+', unit) or unit.startswith('-'):
            raise Failure('service', 'Invalid service unit')
        prefix = ['systemctl'] + (['--user'] if kind == 'systemd-user' else [])
        query = subprocess.run(prefix + ['show', unit, '-p', 'MainPID', '--value'], capture_output=True, text=True, timeout=15)
        pid = query.stdout.strip()
        query_returncode = query.returncode
    elif kind == 'launchd':
        label = service.get('SERVICE_LABEL', '')
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+', label):
            raise Failure('service', 'Invalid LaunchAgent label')
        query = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'], capture_output=True, text=True, timeout=15)
        match = re.search(r'^\s*pid = (\d+)\s*$', query.stdout, re.M)
        pid = match[1] if match else ''
        query_returncode = query.returncode
    elif kind == 'nohup':
        wrapper = root / 'start-nanoclaw.sh'
        reported_wrapper = service.get('WRAPPER_PATH', '')
        if (not reported_wrapper or Path(reported_wrapper).resolve() != wrapper.resolve()
                or wrapper.is_symlink() or not wrapper.is_file()
                or wrapper.stat().st_uid != os.getuid() or not os.access(wrapper, os.X_OK)):
            raise Failure('service', 'Invalid nohup launcher for this checkout')
        pid_file = root / 'nanoclaw.pid'
        if (pid_file.is_symlink() or not pid_file.is_file()
                or pid_file.stat().st_uid != os.getuid()):
            raise Failure('service', 'Missing owned nohup PID file')
        pid = read_limited(pid_file).strip()
        query_returncode = 0
    else:
        raise Failure('service', 'Wizard service is not loaded by a supported manager')
    if query_returncode != 0 or not pid.isdigit() or int(pid) <= 1:
        raise Failure('service', 'Service has no current process')
    argv = process_arguments(pid)
    if (len(argv) < 2 or Path(argv[1]).resolve() != (root / 'dist/index.js').resolve()
            or not service.get('NODE_PATH') or Path(argv[0]).resolve() != Path(service['NODE_PATH']).resolve()):
        raise Failure('service', 'Current service process does not run this exact checkout entrypoint')
    sock = root / 'data/cli.sock'
    if sock.is_symlink() or not stat.S_ISSOCK(sock.stat().st_mode):
        raise Failure('service', 'Missing current checkout CLI socket')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(sock))
    return {'type': kind, 'pid': int(pid), 'checkout_verified': True, 'socket_connected': True}


def collect_container_status():
    """Return bounded container state without exporting container log bodies."""
    command = [
        'docker', 'ps', '-a', '--no-trunc', '--format',
        '{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}',
    ]
    header = 'CONTAINER ID\tNAME\tIMAGE\tSTATE\tSTATUS\n'
    try:
        run = subprocess.run(
            command, capture_output=True, text=True, timeout=5,
            env=child_environment(),
        )
    except subprocess.TimeoutExpired:
        return header + '[docker ps timed out]\n'
    except OSError:
        return header + '[docker ps unavailable]\n'
    if run.returncode != 0:
        return header + '[docker ps failed with exit code ' + str(run.returncode) + ']\n'
    return header + (run.stdout if run.stdout else '[no containers]\n')


def export_evidence(root, destination, terminal, result, credential, container_status=None):
    if destination.exists() or destination.is_symlink():
        raise Failure('export', 'Artifact directory already exists', 74)
    redactor = Redactor(private_values(root, credential, terminal.private_values if terminal else ()))
    files = {'terminal.txt': redactor.clean(terminal.text()) if terminal else '',
             'choices.json': redactor.clean(json.dumps(terminal.choices if terminal else [], indent=2)) + '\n'}
    if result.get('provider_payload_receipt'):
        files['provider-payload-receipt.json'] = redactor.clean(
            json.dumps(result['provider_payload_receipt'], indent=2) + '\n'
        )
    if result.get('codex_target_receipt'):
        files['codex-target-receipt.json'] = redactor.clean(
            json.dumps(result['codex_target_receipt'], indent=2) + '\n'
        )
    if result.get('opencode_target_receipt'):
        files['opencode-target-receipt.json'] = redactor.clean(
            json.dumps(result['opencode_target_receipt'], indent=2) + '\n'
        )
    if result.get('claude_target_receipt'):
        files['claude-target-receipt.json'] = redactor.clean(
            json.dumps(result['claude_target_receipt'], indent=2) + '\n'
        )
    logs = [(root / 'logs/setup.log', Path('setup-logs/setup.log'))]
    logs += [(path, Path('setup-logs/setup-steps') / path.name)
             for path in sorted((root / 'logs/setup-steps').glob('*.log'))]
    logs += [
        (root / 'logs/nanoclaw.log', Path('runtime-logs/nanoclaw.log')),
        (root / 'logs/nanoclaw.error.log', Path('runtime-logs/nanoclaw.error.log')),
    ]
    total = 0
    for path, exported in logs:
        if not path.exists() and not path.is_symlink():
            continue
        content = read_limited(path)
        total += len(content.encode('utf-8'))
        if total > MAX_LOG_BYTES:
            raise Failure('export', 'Log evidence exceeds size limit', 74)
        files[str(exported)] = redactor.clean(content)
    if container_status is not None:
        total += len(container_status.encode('utf-8'))
        if total > MAX_LOG_BYTES:
            raise Failure('export', 'Log evidence exceeds size limit', 74)
        files['runtime-logs/docker-containers.txt'] = redactor.clean(container_status)
    if (root / 'logs/setup.log').is_file():
        progress = read_limited(root / 'logs/setup.log')
        result['product_failures'] = re.findall(r'^=== \[.*?\] ([\w-]+) \[.*?\] → (?:failed|aborted) ===$', progress, re.M)
        result['product_aborts'] = re.findall(r'^## .*? · aborted at ([\w-]+) \(', progress, re.M)
    files['result.json'] = redactor.clean(json.dumps(result, indent=2)) + '\n'
    destination.mkdir(parents=True, mode=0o700)
    manifest = {'schema_version': 1, 'run_id': result['run_id'], 'sanitized': True, 'files': {}}
    for name, content in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode('utf-8')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
        manifest['files'][name] = hashlib.sha256(data).hexdigest()
    write_json(destination / 'manifest.json', manifest)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--provider', required=True, help='provider value discovered from this exact checkout')
    parser.add_argument('--auth-method', required=True, help='provider-owned authentication method value')
    parser.add_argument('--payload-ref', help='already-fetched provider payload ref, when the provider is installable')
    parser.add_argument('--opencode-model', help='full OpenCode backend/model ID (required for OpenCode)')
    parser.add_argument('--opencode-base-url', help='custom HTTP(S) API endpoint, including its API path')
    parser.add_argument('--opencode-provider', help='custom endpoint API scheme (default: openai)')
    parser.add_argument('--expected-auth-source-commit',
                        help='bind the run to the provider auth source inspected before provisioning')
    parser.add_argument('--expected-payload-commit',
                        help='bind a branch-owned provider payload to the commit selected before provisioning')
    parser.add_argument('--supervised-human-auth', action='store_true',
                        help='allow an explicitly supervised live provider sign-in')
    parser.add_argument('--require-codex-cli-fallback', action='store_true',
                        help='require the manifest-pinned Codex CLI fallback path')
    parser.add_argument('--gateway', choices=GATEWAYS, default='onecli',
                        help='credential gateway the wizard installs on a gateway-seam ref (default: onecli)')
    parser.add_argument('--credential-file', '--key-file', dest='credential_file', type=Path,
                        help='private credential for an automated paste method; --key-file is a compatibility alias')
    parser.add_argument('--result-file', type=Path)
    parser.add_argument('--artifacts-dir', type=Path)
    parser.add_argument('--run-id', default=secrets.token_hex(16))
    parser.add_argument('--timeout', type=float, default=1200)
    parser.add_argument('--idle-timeout', type=float, default=180)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    result_path = args.result_file or root / 'logs/e2e/result.json'
    artifacts = args.artifacts_dir or root / 'logs/e2e-wizard'
    result = {'schema_version': 1, 'mode': 'wizard', 'run_id': args.run_id, 'status': 'running',
              'phase': 'preflight', 'commit': None, 'exit_code': None, 'started_at': now(),
              'provider': args.provider, 'auth_method': args.auth_method,
              'require_codex_cli_fallback': args.require_codex_cli_fallback,
              'requested_gateway': args.gateway, 'gateway': None, 'gateway_seam': None}
    write_json(result_path, result)  # Invalidate stale success before any check.
    terminal, credential = None, ''
    handoff_dir = handoff_path = handoff_response_path = None
    previous_signals = {}
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    for signum in (signal.SIGTERM, signal.SIGHUP):
        previous_signals[signum] = signal.signal(signum, interrupted)
    try:
        if args.timeout <= 0 or args.idle_timeout <= 0 or not re.fullmatch(r'[a-zA-Z0-9-]{8,64}', args.run_id):
            raise Failure('preflight', 'Invalid timeout or run identity', 64)
        if artifacts.exists():
            raise Failure('preflight', 'Use a new artifact directory for each run', 64)
        if os.getuid() == 0:
            raise Failure('preflight', 'Run the fresh scenario as a regular user', 64)
        if not (root / 'nanoclaw.sh').is_file() or json.loads((root / 'package.json').read_text()).get('name') != 'nanoclaw':
            raise Failure('preflight', 'Run from the NanoClaw checkout under test', 65)
        for path in ['.env', 'data/v2.db', 'logs/setup.log', 'data/cli.sock']:
            if (root / path).exists():
                raise Failure('preflight', 'Fresh scenario refuses prior product state: ' + path, 65)
        if subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--'], cwd=root).returncode:
            raise Failure('preflight', 'Checkout has tracked edits', 65)
        seam = gateway_seam_present(root)
        result['gateway_seam'] = seam
        if args.gateway != 'onecli' and not seam:
            raise Failure('preflight', '--gateway ' + args.gateway
                          + ' needs a NanoClaw ref with the credential-gateway seam (setup/gateways/)', 64)
        if args.supervised_human_auth:
            handoff_dir, handoff_path, handoff_response_path = create_handoff_paths(args.run_id)
        if (args.provider, args.auth_method) == ('codex', 'device'):
            if (Path.home() / '.codex/auth.json').exists():
                raise Failure('preflight', 'Dedicated device pairing refuses a personal Codex login', 65)
        if ((args.provider, args.auth_method) == ('claude', 'subscription')
                and (Path.home() / '.claude/.credentials.json').exists()):
            raise Failure('preflight', 'Dedicated Claude subscription sign-in refuses personal Claude credentials', 65)
        if args.require_codex_cli_fallback:
            if not (args.supervised_human_auth
                    and (args.provider, args.auth_method) == ('codex', 'device')):
                raise Failure('preflight', '--require-codex-cli-fallback requires supervised Codex device pairing', 64)
            if shutil.which('codex') is not None:
                raise Failure('preflight', 'Codex CLI is globally available; fallback path would not run', 65)
        host_claude_absent_before = shutil.which('claude') is None
        host_codex_absent_before = shutil.which('codex') is None
        discovery = provider_discovery()
        try:
            model_provider = discovery.validate_model(args.provider, args.auth_method, args.opencode_model,
                                                      args.opencode_base_url, args.opencode_provider)
        except discovery.DiscoveryError as error:
            raise Failure('preflight', str(error), 64)
        result['commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        selected, method = selected_auth(
            root, args.provider, args.auth_method, args.payload_ref,
            args.expected_auth_source_commit, args.supervised_human_auth, args.expected_payload_commit,
        )
        if (method['automation'] == 'human-handoff'
                and (args.provider, args.auth_method) not in {
                    ('codex', 'device'), ('claude', 'subscription'),
                }):
            raise Failure('preflight', 'This supervised adapter does not implement that live handoff', 64)
        if (args.provider, args.auth_method) == ('claude', 'subscription') and args.gateway != 'onecli':
            raise Failure('preflight', 'Claude subscription proof reads the OneCLI vault; '
                          'Iron Proxy is supported for Codex device pairing and credential-file methods', 64)
        pre_auth_hook = None
        if (args.provider, args.auth_method) == ('codex', 'device') and not args.require_codex_cli_fallback:
            # The payload's host-side `codex login` needs the CLI on this
            # machine; install the skill's exact pin once the wizard has Node
            # and let the wizard find it before any host copy.
            codex_pin = pinned_codex_cli(root)
            pre_auth_hook = lambda: install_host_codex_cli(codex_pin)
            global PREFER_LOCAL_BIN
            PREFER_LOCAL_BIN = True
        credential = credential_for(args.credential_file, method['credential_kind'])
        result.update(provider_label=selected['label'], auth_method_label=method['label'],
                      auth_source=selected['auth_source'], auth_source_ref=selected['auth_source_ref'],
                      auth_source_commit=selected['auth_source_commit'],
                      payload_kind=selected.get('payload_kind'), payload_commit=branch_payload_commit(selected))
        if args.provider == 'opencode':
            result['opencode_model'] = args.opencode_model
            result['opencode_provider'] = model_provider
            result['opencode_base_url'] = args.opencode_base_url or 'native'
        left, right = 10000 + secrets.randbelow(80000), 11 + secrets.randbelow(88)
        values = {'credential': credential, 'display_name': 'Wizard Tester',
                  'provider_label': selected['label'], 'auth_option': method['label'],
                  'auth_prompt': selected['auth_prompt']}
        if method['automation'] == 'credential-file':
            values['credential_prompt'] = {
                      'anthropic-oauth': 'Paste your OAuth token',
                      'anthropic-api-key': 'Paste your API key',
                      'openai-api-key': 'Paste your OpenAI API key (sk-…)',
                      'opencode-api-key': 'API key',
                  }[method['credential_kind']]
        values.update(
            challenge=f'Reply with only the decimal value of {left} * {right}.',
            answer=str(left * right),
        )
        scenario = json.loads((Path(__file__).resolve().parent.parent / 'scenarios/fresh-cli.json').read_text())
        if args.provider == 'opencode':
            configure_opencode_scenario(scenario, values, args.auth_method, args.opencode_model,
                                        args.opencode_base_url, args.opencode_provider)
        if method['automation'] == 'human-handoff':
            scenario['prompts'] = [prompt for prompt in scenario['prompts'] if prompt['id'] != 'credential']
        if (args.provider, args.auth_method) == ('claude', 'subscription'):
            scenario['required_step_statuses'] = {'auth': 'interactive'}
        if seam:
            scenario = seam_scenario(scenario, args.provider)
        scenario['required_inputs'].update(
            agent_provider=args.provider,
            **{selected['auth_input_key']: args.auth_method},
        )
        result['scenario'] = scenario['name']
        result['scenario_source_commit'] = scenario['source_commit']
        result['phase'] = 'wizard'
        write_json(result_path, result)
        # The product writes the credential into its raw auth log. Protect the
        # parent directory while leaving the product log contents untouched.
        (root / 'logs').mkdir(mode=0o700, exist_ok=True)
        (root / 'logs').chmod(0o700)
        installable = selected.get('source', selected['auth_source']).endswith('/SKILL.md')
        terminal = WizardTerminal(
            scenario, values, args.timeout, args.idle_timeout,
            handoff_method=args.auth_method if method['automation'] == 'human-handoff' else None,
            payload_verifier=(lambda: verify_provider_payload(root, selected)) if installable else None,
            run_id=args.run_id,
            handoff_path=handoff_path,
            handoff_response_path=handoff_response_path,
            pre_auth_hook=pre_auth_hook,
        )
        # On a seam ref the public wizard takes the gateway from its own
        # --gateway-provider flag (the Advanced screen sets the same value).
        if seam:
            terminal.run(root, ['bash', 'nanoclaw.sh', '--gateway-provider', args.gateway])
        else:
            terminal.run(root)
        if installable:
            if terminal.payload_receipt is None:
                raise Failure('payload', 'Provider payload was not verified before authentication', 65)
            if verify_provider_payload(root, selected) != terminal.payload_receipt:
                raise Failure('payload', 'Provider payload changed during authentication', 65)
            result['provider_payload_receipt'] = terminal.payload_receipt
        if (args.provider, args.auth_method) == ('codex', 'device'):
            result['codex_target_receipt'] = verify_codex_target(
                root, terminal, method, args.require_codex_cli_fallback, args.gateway,
                host_codex_absent_before, terminal.pre_auth_receipt,
            )
        if (args.provider, args.auth_method) == ('claude', 'subscription'):
            result['claude_target_receipt'] = verify_claude_target(terminal, host_claude_absent_before)
        verify, service = check_progress(root, scenario)
        live = verify_live_service(root, service)
        # Bind the gateway the wizard actually installed, never the request alone.
        installed, problem = inspect_gateway(root, args.gateway, seam, live['pid'])
        result['gateway'] = installed
        if problem:
            raise Failure('verify', problem, 1)
        if args.provider == 'opencode':
            result['opencode_target_receipt'] = verify_opencode_target(root, args.auth_method, args.opencode_model,
                                                                      args.opencode_base_url, args.opencode_provider,
                                                                      args.gateway)
        if (args.provider, args.auth_method) == ('codex', 'device'):
            result['codex_target_receipt']['retained_agent'] = verify_retained_provider_group(root, 'codex')
        if (args.provider, args.auth_method) == ('claude', 'subscription'):
            result['claude_target_receipt']['retained_agent'] = verify_retained_provider_group(root, 'claude')
        result.update(status='pass', phase='complete', exit_code=0, ping='ok',
                      service_type=live['type'], service=live, verification=verify,
                      wizard_completed=True, retained_reply_verified=True)
    except KeyboardInterrupt:
        result.update(status='failed', phase='cancelled', exit_code=130, error='Driver interrupted')
    except Failure as error:
        result.update(status='failed', phase=error.phase, exit_code=error.code, error=str(error))
    except Exception as error:
        # Exception strings can contain subprocess arguments or configuration.
        result.update(status='failed', phase=result['phase'], exit_code=1, error='Driver error: ' + type(error).__name__)
    if terminal is not None and getattr(terminal, 'pre_auth_receipt', None) and 'codex_target_receipt' not in result:
        result['host_codex_cli'] = terminal.pre_auth_receipt  # a failed pairing still says which CLI ran
    if result['status'] != 'pass' and result.get('gateway') is None and result.get('gateway_seam'):
        # Like the headless installer, a failure names the kind the wizard
        # stamped so far (if any); only a pass requires it to match.
        try:
            result['gateway'] = env_gateway(read_limited(root / '.env')) if (root / '.env').is_file() else None
        except Failure:
            pass
    result['finished_at'] = now()
    result_redactor = Redactor([credential])
    try:
        result_redactor = Redactor(private_values(
            root, credential, terminal.private_values if terminal else (),
        ))
        export_evidence(root, artifacts, terminal, result, credential, collect_container_status())
        result['artifacts'] = str(artifacts)
    except Exception as error:
        result.update(status='failed', phase='export', exit_code=74, error='Sanitized evidence export failed: ' + type(error).__name__)
    safe_result = json.loads(result_redactor.clean(json.dumps(result)))
    write_json(result_path, safe_result)
    cleanup_handoff(
        handoff_dir, handoff_path, handoff_response_path, args.run_id,
        terminal.handoff_nonce if terminal else None,
    )
    print('[e2e-wizard] ' + safe_result['status'] + ' phase=' + safe_result['phase'] + ' result=' + str(result_path))
    for signum, handler in previous_signals.items():
        signal.signal(signum, handler)
    return safe_result['exit_code']


if __name__ == '__main__':
    sys.exit(main())
