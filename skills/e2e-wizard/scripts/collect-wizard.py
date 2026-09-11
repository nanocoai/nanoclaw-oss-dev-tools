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


def collect(source, destination, result_path, expected, run_id, exit_code, key_file):
    destination, result_path = Path(destination), Path(result_path)
    if destination.exists():
        raise ValueError('Local artifact directory already exists')
    key = ''.join(Path(key_file).read_text().split())
    data = source.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('Artifact archive exceeds size limit')
    files, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
        for item in archive:
            name = str(PurePosixPath(item.name))
            if item.isdir() and name in ('.', 'setup-logs', 'setup-logs/setup-steps'):
                continue
            if not item.isfile() or name.startswith('/') or '..' in PurePosixPath(name).parts or name in files:
                raise ValueError('Unsafe archive member')
            if name not in ('manifest.json', 'result.json', 'choices.json', 'terminal.txt', 'setup-logs/setup.log') and not (name.startswith('setup-logs/setup-steps/') and name.endswith('.log') and len(PurePosixPath(name).parts) == 3):
                raise ValueError('Unexpected artifact path')
            total += item.size
            if total > LIMIT:
                raise ValueError('Expanded artifacts exceed size limit')
            files[name] = archive.extractfile(item).read()
    manifest = json.loads(files['manifest.json'])
    if manifest.get('schema_version') != 1 or manifest.get('run_id') != run_id or manifest.get('sanitized') is not True:
        raise ValueError('Unconfirmed sanitized artifact identity')
    if set(manifest['files']) != set(files) - {'manifest.json'}:
        raise ValueError('Incomplete artifact manifest')
    for name, digest in manifest['files'].items():
        if hashlib.sha256(files[name]).hexdigest() != digest:
            raise ValueError('Artifact checksum mismatch')
    for content in files.values():
        text = content.decode('utf-8')
        if key in ''.join(text.split()) or runner.TOKEN.search(text):
            raise ValueError('Unredacted credential in remote export')
    result = json.loads(files['result.json'])
    expected_status = 'pass' if exit_code == 0 else 'failed'
    if result.get('schema_version') != 1 or result.get('mode') != 'wizard' or result.get('run_id') != run_id or result.get('commit') != expected or result.get('exit_code') != exit_code or result.get('status') != expected_status:
        raise ValueError('Wizard result does not match this invocation')
    required = {'terminal.txt', 'choices.json', 'result.json', 'setup-logs/setup.log'}
    if exit_code == 0:
        service = result.get('service', {})
        if not required.issubset(files) or not result.get('wizard_completed') or not result.get('retained_reply_verified') or service.get('checkout_verified') is not True or service.get('socket_connected') is not True:
            raise ValueError('Missing wizard acceptance evidence')
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
    parser.add_argument('--key-file', required=True, type=Path)
    args = parser.parse_args()
    try:
        collect(sys.stdin.buffer, args.artifacts_dir, args.result_file, args.commit, args.run_id, args.exit_code, args.key_file)
    except Exception as error:
        # Tar and parsing exceptions may contain unsanitized remote content.
        print('[e2e-wizard] artifact validation failed: ' + type(error).__name__, file=sys.stderr)
        return 74
    print('[e2e-wizard] sanitized transcript, setup logs and result saved')
    return 0


if __name__ == '__main__':
    sys.exit(main())
