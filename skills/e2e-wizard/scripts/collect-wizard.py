#!/usr/bin/env python3
"""Validate a sanitized wizard archive before publishing its local result."""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile
import tempfile

spec = importlib.util.spec_from_file_location('wizard_runner', Path(__file__).with_name('wizard-run.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
LIMIT = 64 * 1024 * 1024
EXACT_PATHS = {
    'manifest.json', 'result.json', 'choices.json', 'terminal.txt',
    'provider-payload-receipt.json', 'codex-target-receipt.json',
    'setup-logs/setup.log', 'runtime-logs/nanoclaw.log',
    'runtime-logs/nanoclaw.error.log', 'runtime-logs/docker-containers.txt',
}
DIRECTORIES = ('.', 'setup-logs', 'setup-logs/setup-steps', 'runtime-logs')


class ValidationError(ValueError):
    """A safe, documented validation code with no remote artifact content."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def reject(code):
    raise ValidationError(code)


def allowed_path(name):
    return (name in EXACT_PATHS
            or (name.startswith('setup-logs/setup-steps/')
                and name.endswith('.log') and len(PurePosixPath(name).parts) == 3))


def load_json(files, name, code):
    if name not in files:
        reject(code)
    try:
        value = json.loads(files[name].decode('utf-8'))
    except (UnicodeDecodeError, ValueError, TypeError):
        reject(code)
    if not isinstance(value, dict):
        reject(code)
    return value


def collect(source, destination, result_path, expected, run_id, exit_code, key_file,
            provider=None, auth_method=None, auth_source_commit=None):
    destination, result_path = Path(destination), Path(result_path)
    if destination.exists() or destination.is_symlink():
        reject('destination-exists')
    key = ''
    if key_file is not None:
        try:
            key = ''.join(Path(key_file).read_text().split())
        except (OSError, UnicodeError):
            reject('credential-unreadable')
        if not key:
            reject('credential-empty')
    elif provider != 'codex' or auth_method != 'device':
        reject('credential-unreadable')
    data = source.read(LIMIT + 1)
    if len(data) > LIMIT:
        reject('archive-too-large')
    files, total = {}, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
            for item in archive:
                name = str(PurePosixPath(item.name))
                if item.isdir() and name in DIRECTORIES:
                    continue
                if (not item.isfile() or item.size < 0 or name.startswith('/')
                        or '..' in PurePosixPath(name).parts or name in files):
                    reject('unsafe-archive-member')
                if not allowed_path(name):
                    reject('unexpected-artifact-path')
                total += item.size
                if total > LIMIT:
                    reject('expanded-artifacts-too-large')
                extracted = archive.extractfile(item)
                if extracted is None:
                    reject('unsafe-archive-member')
                files[name] = extracted.read()
    except ValidationError:
        raise
    except (tarfile.TarError, EOFError, OSError):
        reject('invalid-tar-archive')
    manifest = load_json(files, 'manifest.json', 'invalid-manifest')
    if manifest.get('schema_version') != 1 or manifest.get('run_id') != run_id or manifest.get('sanitized') is not True:
        reject('artifact-identity-mismatch')
    manifest_files = manifest.get('files')
    if (not isinstance(manifest_files, dict)
            or not all(isinstance(name, str) and isinstance(digest, str)
                       for name, digest in manifest_files.items())):
        reject('invalid-manifest')
    if set(manifest_files) != set(files) - {'manifest.json'}:
        reject('incomplete-manifest')
    for name, digest in manifest_files.items():
        if hashlib.sha256(files[name]).hexdigest() != digest:
            reject('checksum-mismatch')
    for content in files.values():
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError:
            reject('invalid-artifact-text')
        if ((key and key in ''.join(text.split())) or runner.TOKEN.search(text)
                or runner.DEVICE_CODE.search(text)):
            reject('credential-found')
    result = load_json(files, 'result.json', 'invalid-result')
    expected_status = 'pass' if exit_code == 0 else 'failed'
    if result.get('schema_version') != 1 or result.get('mode') != 'wizard' or result.get('run_id') != run_id or result.get('commit') != expected or result.get('exit_code') != exit_code or result.get('status') != expected_status:
        reject('invocation-mismatch')
    if ((provider is not None and result.get('provider') != provider)
            or (auth_method is not None and result.get('auth_method') != auth_method)
            or (auth_source_commit is not None
                and result.get('auth_source_commit') != auth_source_commit)):
        reject('provider-selection-mismatch')
    required = {'terminal.txt', 'choices.json', 'result.json', 'setup-logs/setup.log'}
    if exit_code == 0:
        service = result.get('service', {})
        if (not isinstance(service, dict) or not required.issubset(files)
                or not result.get('wizard_completed') or not result.get('retained_reply_verified')
                or service.get('checkout_verified') is not True
                or service.get('socket_connected') is not True):
            reject('acceptance-evidence-missing')
        if provider == 'codex' and auth_method == 'device':
            payload = load_json(files, 'provider-payload-receipt.json', 'acceptance-evidence-missing')
            target = load_json(files, 'codex-target-receipt.json', 'acceptance-evidence-missing')
            if (payload != result.get('provider_payload_receipt')
                    or payload.get('commit') != auth_source_commit
                    or not isinstance(payload.get('paths'), dict)
                    or not payload['paths']
                    or payload.get('file_count') != len(payload['paths'])
                    or target != result.get('codex_target_receipt')
                    or target.get('fallback_proof') is not True
                    or target.get('auth_method') != 'device'
                    or target.get('host_codex_absent_before_wizard') is not True
                    or target.get('personal_auth_absent_before_wizard') is not True
                    or target.get('personal_auth_absent_after_wizard') is not True
                    or target.get('device_handoff_observed') is not True
                    or target.get('retained_agent') != {
                        'group_count': 1, 'provider': 'codex', 'verified_via': 'ncl',
                    }):
                reject('acceptance-evidence-missing')
    temporary = Path(tempfile.mkdtemp(prefix='.wizard-export-', dir=destination.parent))
    try:
        for name, content in files.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as out:
                out.write(content)
        os.rename(temporary, destination)
        result['artifacts'] = str(destination.resolve())
        runner.write_json(result_path, result)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts-dir', required=True, type=Path)
    parser.add_argument('--result-file', required=True, type=Path)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--exit-code', required=True, type=int)
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--provider')
    parser.add_argument('--auth-method')
    parser.add_argument('--auth-source-commit')
    args = parser.parse_args()
    try:
        collect(sys.stdin.buffer, args.artifacts_dir, args.result_file, args.commit, args.run_id,
                args.exit_code, args.key_file, args.provider, args.auth_method,
                args.auth_source_commit)
    except ValidationError as error:
        print('[e2e-wizard] artifact validation failed: ' + error.code, file=sys.stderr)
        return 74
    except Exception as error:
        # Tar and parsing exceptions may contain unsanitized remote content.
        print('[e2e-wizard] artifact validation failed: ' + type(error).__name__, file=sys.stderr)
        return 74
    print('[e2e-wizard] sanitized transcript, setup/runtime diagnostics and result saved')
    return 0


if __name__ == '__main__':
    sys.exit(main())
