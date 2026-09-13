#!/usr/bin/env python3
"""Qualify WSL2 + local Docker Desktop, then run the unchanged public wizard."""
import argparse
import base64
import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
WIZARD = HERE.parent.parent / 'e2e-wizard' / 'scripts'
ENDPOINT = 'unix:///var/run/docker.sock'
BIND_IMAGE = 'alpine:3.23'
LIMIT = 64 * 1024 * 1024


class Failure(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def call(argv, *, cwd=None, timeout=60):
    """Errors deliberately omit child output, which may contain private data."""
    try:
        process = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Failure('Required command unavailable or timed out: ' + Path(argv[0]).name) from error
    if process.returncode:
        raise Failure('Required command failed: ' + Path(argv[0]).name)
    return process.stdout.strip().lstrip('\ufeff')


def check_linux(system, release, uid, filesystem, environment):
    if system != 'Linux' or 'microsoft-standard-wsl2' not in release.lower():
        raise Failure('Run inside a WSL2 Linux distribution')
    if uid == 0:
        raise Failure('Run as the regular Linux test user')
    if filesystem != 'ext4':
        raise Failure('Keep the checkout and evidence on the WSL Linux ext4 filesystem')
    if any(environment.get(name) for name in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')):
        raise Failure('Remove Docker endpoint overrides before testing the local engine')


def check_engines(linux, windows):
    if (windows.get('status') != 'passed'
            or windows.get('dockerEndpoint') != 'npipe:////./pipe/dockerDesktopLinuxEngine'
            or windows.get('operatingSystem') != 'Docker Desktop'
            or windows.get('osType') != 'linux'):
        raise Failure('Windows local Docker Desktop proof is missing')
    if (not linux.get('ID') or linux['ID'] != windows.get('dockerEngineID')
            or linux.get('OSType') != 'linux' or linux.get('OperatingSystem') != 'Docker Desktop'):
        raise Failure('WSL must reach the same local Docker Desktop Linux engine as Windows')
    kernel = linux.get('KernelVersion', '')
    if 'microsoft-standard-wsl2' not in kernel.lower() or kernel != windows.get('kernelVersion'):
        raise Failure('Docker Desktop must run on the local WSL2 kernel')


def check_bind_result(expected, actual):
    if actual.strip() != expected:
        raise Failure('Container failed to read the Linux-home bind mount')


def qualification(root, *output_parents):
    filesystem = call(['findmnt', '--noheadings', '--output', 'FSTYPE', '--target', str(root)])
    check_linux(platform.system(), platform.release(), os.getuid(), filesystem, os.environ)
    for path in (Path.home(), *output_parents):
        if call(['findmnt', '--noheadings', '--output', 'FSTYPE', '--target', str(path)]) != 'ext4':
            raise Failure('Keep the Linux home and results on the WSL ext4 filesystem')
    if Path('/proc/1/comm').read_text().strip() != 'systemd':
        raise Failure('WSL systemd is required')
    if os.environ.get('XDG_RUNTIME_DIR') != '/run/user/' + str(os.getuid()):
        raise Failure('The regular user systemd runtime directory is missing')
    if call(['systemctl', '--user', 'is-active', 'default.target']) != 'active':
        raise Failure('The systemd user session is inactive')
    call(['sudo', '-n', 'true'])
    if shutil.which('dockerd'):
        raise Failure('Use Docker Desktop integration without a separate Linux Docker daemon')
    if not Path('/var/run/docker.sock').is_socket():
        raise Failure('Enable Docker Desktop WSL integration for this distribution')
    # The wizard uses plain docker; reject a selected remote context as well as
    # environment overrides even though our bind probe uses an explicit socket.
    context = json.loads(call(['docker', 'context', 'inspect']))
    if len(context) != 1 or context[0].get('Endpoints', {}).get('docker', {}).get('Host') != ENDPOINT:
        raise Failure('The current Linux Docker context must use /var/run/docker.sock')
    powershell = shutil.which('powershell.exe')
    if not powershell:
        raise Failure('Enable WSL Windows interoperability for the local-engine identity check')
    script = (HERE / 'windows-engine.ps1').read_text()
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    windows = json.loads(call([powershell, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]))
    docker = ['docker', '--host', ENDPOINT]
    info = json.loads(call(docker + ['info', '--format', '{{json .}}']))
    check_engines(info, windows)
    marker = 'nanoclaw-wsl-bind-' + secrets.token_hex(16)
    with tempfile.TemporaryDirectory(prefix='.nanoclaw-e2e-bind-', dir=Path.home()) as directory:
        (Path(directory) / 'marker.txt').write_text(marker + '\n')
        answer = call(docker + ['run', '--rm', '--network', 'none', '--mount',
                      'type=bind,src=' + directory + ',dst=/probe,readonly',
                      BIND_IMAGE, 'cat', '/probe/marker.txt'], timeout=180)
        check_bind_result(marker, answer)
    digests = json.loads(call(docker + ['image', 'inspect', BIND_IMAGE, '--format', '{{json .RepoDigests}}']))
    return {'status': 'passed', 'captured_at': now(), 'kernel': platform.release(),
            'uid': os.getuid(), 'filesystem': filesystem, 'windows': windows,
            'docker_endpoint': ENDPOINT, 'docker_engine_id': info['ID'],
            'docker_server_version': info['ServerVersion'], 'same_engine_as_windows': True,
            'docker_kernel_version': info['KernelVersion'],
            'linux_home_bind_mount_passed': True, 'bind_image_digests': digests,
            'systemd_user_session': 'active'}


def source_identity(root, ref):
    if not (root / 'nanoclaw.sh').is_file() or json.loads((root / 'package.json').read_text()).get('name') != 'nanoclaw':
        raise Failure('Run from the NanoClaw source checkout under test')
    commit = call(['git', 'rev-parse', '--verify', ref + '^{commit}'], cwd=root)
    if commit != call(['git', 'rev-parse', 'HEAD'], cwd=root):
        raise Failure('Check out the requested exact commit before running this test')
    if call(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root):
        raise Failure('The checkout contains tracked changes')
    return commit


def require_fresh(root):
    for name in ('.env', 'data/v2.db', 'logs/setup.log', 'data/cli.sock'):
        path = root / name
        if path.exists() or path.is_symlink():
            raise Failure('Fresh wizard refuses prior product state: ' + name)


def validate_export(source, artifacts, result_file, commit, run_id, code, key_file,
                    provider=None, auth_method=None, auth_source_commit=None):
    # Reuse the wizard's archive, identity, acceptance and credential checks.
    spec = importlib.util.spec_from_file_location('windows_wizard_collector', WIZARD / 'collect-wizard.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=data, mode='w') as archive:
        for path in sorted(source.rglob('*')):
            if path.is_dir() and not path.is_symlink():
                continue
            if not path.is_file() or path.is_symlink():
                raise Failure('Unsafe wizard export member')
            total += path.stat().st_size
            if total > LIMIT:
                raise Failure('Wizard export exceeds size limit')
            archive.add(path, arcname=str(path.relative_to(source)), recursive=False)
    if data.tell() > LIMIT:
        raise Failure('Wizard archive exceeds size limit')
    data.seek(0)
    module.collect(data, artifacts, result_file, commit, run_id, code, key_file,
                   provider, auth_method, auth_source_commit)


def run_wizard(command, root):
    """Forward cancellation to the existing driver's PTY cleanup handler."""
    previous = {}
    process = None

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    try:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, interrupted)
        process = subprocess.Popen(command, cwd=root, start_new_session=True)
        return process.wait()
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--ref', default='HEAD')
    parser.add_argument('--credential-file', '--key-file', dest='key_file', type=Path,
                        default=Path.home() / '.nanoclaw-e2e/anthropic_key')
    parser.add_argument('--provider', help='provider value discovered from this exact checkout')
    parser.add_argument('--auth-method', help='provider-owned authentication method value')
    parser.add_argument('--payload-ref', help='already-fetched provider payload ref')
    parser.add_argument('--result-file', required=True, type=Path)
    parser.add_argument('--artifacts-dir', type=Path)
    parser.add_argument('--wizard-timeout', type=int, default=1200)
    parser.add_argument('--preflight-only', action='store_true', help='Test WSL/Docker only; no credentials or NanoClaw setup')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    result_file = args.result_file.absolute()
    artifacts = (args.artifacts_dir or Path(str(result_file) + '.artifacts')).absolute()
    if not result_file.parent.is_dir() or result_file.is_symlink() or not artifacts.parent.is_dir():
        parser.error('Result and artifact parents must exist; the result cannot be a symlink')
    if result_file.resolve() == args.key_file.resolve():
        parser.error('The result file cannot overwrite the credential path')
    if not 1 <= args.wizard_timeout <= 2100:
        parser.error('--wizard-timeout must be between 1 and 2100 seconds')
    if not args.preflight_only and (not args.provider or not args.auth_method):
        parser.error('--provider and --auth-method are required for a wizard run')
    if result_file.exists():
        try:
            previous = json.loads(result_file.read_text())
            if previous.get('mode') != 'windows-wsl-wizard' or previous.get('schema_version') != 1:
                raise ValueError
        except (OSError, ValueError, AttributeError):
            parser.error('Refusing to overwrite a file that is not a Windows E2E report')
    run_id = secrets.token_hex(16)
    report = {'schema_version': 1, 'mode': 'windows-wsl-wizard', 'run_id': run_id,
              'status': 'running', 'phase': 'preflight', 'started_at': now(),
              'commit': None, 'exit_code': None, 'wizard_started': False,
              'provider': args.provider, 'auth_method': args.auth_method}
    write_json(result_file, report)  # Invalidate a previous pass before any checks.
    code = 1
    try:
        report['commit'] = source_identity(root, args.ref)
        if not args.preflight_only:
            require_fresh(root)
            if artifacts.exists():
                raise Failure('Choose a new artifact directory')
            if not all((WIZARD / name).is_file() for name in
                       ('wizard-install.sh', 'collect-wizard.py', 'provider-options.py')):
                raise Failure('Install e2e-wizard alongside e2e-windows')
            provider_module = importlib.util.spec_from_file_location(
                'windows_provider_options', WIZARD / 'provider-options.py')
            provider_options = importlib.util.module_from_spec(provider_module)
            provider_module.loader.exec_module(provider_options)
            try:
                selected = provider_options.discover(
                    root, args.provider, args.payload_ref, report['commit'],
                )['selected']
            except provider_options.DiscoveryError as error:
                raise Failure('Could not discover provider/auth choices: ' + str(error))
            methods = [item for item in selected['auth_methods']
                       if item['value'] == args.auth_method]
            if (len(methods) != 1 or not methods[0]['usable_for_e2e']
                    or methods[0]['automation'] != 'credential-file'):
                raise Failure('Selected provider auth is unavailable to the unattended wizard')
            report['auth_source_commit'] = selected['auth_source_commit']
            key = args.key_file.lstat()
            if not stat.S_ISREG(key.st_mode) or key.st_mode & 0o077 or key.st_uid != os.getuid():
                raise Failure('Credential must be a private regular file owned by the Linux user')
        report['environment'] = qualification(root, result_file.parent, artifacts.parent)
        if args.preflight_only:
            report.update(status='qualified', phase='environment', exit_code=0)
            code = 0
        else:
            # Raw wizard logs stay on this disposable distribution. The runner
            # protects product logs and generates a separate sanitized export.
            work = Path(tempfile.mkdtemp(prefix='.windows-wizard-', dir=result_file.parent))
            source = work / 'sanitized'
            wizard_result = work / 'wizard.json'
            report.update(phase='wizard', wizard_started=True, retained_work=str(work))
            write_json(result_file, report)
            returncode = run_wizard(['bash', str(WIZARD / 'wizard-install.sh'), '--root', str(root),
                       '--credential-file', str(args.key_file.absolute()), '--provider', args.provider,
                       '--auth-method', args.auth_method,
                       '--expected-auth-source-commit', report['auth_source_commit'],
                       '--artifacts-dir', str(source), '--run-id', run_id,
                       '--timeout', str(args.wizard_timeout)]
                       + (['--payload-ref', args.payload_ref] if args.payload_ref else []), root)
            code = returncode if returncode >= 0 else 128 - returncode
            report['phase'] = 'export'
            write_json(result_file, report)
            validated = work / 'validated.json'
            validate_export(source, artifacts, validated, report['commit'], run_id, code,
                            args.key_file, args.provider, args.auth_method,
                            report['auth_source_commit'])
            report['wizard'] = json.loads(validated.read_text())
            report.update(status='pass' if code == 0 else 'failed', phase='complete' if code == 0 else 'wizard', exit_code=code)
    except KeyboardInterrupt:
        code = 130
        report.update(status='failed', phase='cancelled', exit_code=code, error='Windows test interrupted; distribution retained')
    except Exception as error:
        code = 74 if report['phase'] == 'export' else 1
        message = str(error) if isinstance(error, Failure) else 'Windows test failed: ' + type(error).__name__
        report.update(status='failed', exit_code=code, error=message)
    report['finished_at'] = now()
    write_json(result_file, report)
    print('[e2e-windows] ' + report['status'] + ' phase=' + report['phase'] + ' result=' + str(result_file))
    return code


if __name__ == '__main__':
    sys.exit(main())
