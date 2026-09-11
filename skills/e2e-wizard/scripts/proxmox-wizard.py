#!/usr/bin/env python3
"""Drive the public wizard in a new LXC using e2e-proxmox's retained lifecycle.

Accepts the e2e-proxmox options, plus --artifacts-dir and --wizard-timeout.
The companion e2e-proxmox skill must be installed alongside e2e-wizard.
"""
import argparse
import base64
import importlib.util
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile

HERE = Path(__file__).resolve().parent
LIFECYCLE = HERE.parents[1] / 'e2e-proxmox/scripts/proxmox-run.py'
BUNDLE = ('scripts/wizard-run.py', 'scripts/wizard-install.sh',
          'requirements.txt', 'scenarios/fresh-cli.json')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def wrapper(run_id, timeout):
    # Only public harness code travels in this bundle. The lifecycle separately
    # uploads the credential over SSH stdin with its existing private-file rules.
    files = {name: base64.b64encode((HERE.parent / name).read_bytes()).decode()
             for name in BUNDLE}
    return """#!/usr/bin/env bash
set -euo pipefail
umask 077
python3 - <<'WIZARD_BUNDLE'
import base64, json
from pathlib import Path
root = Path.home() / '.nanoclaw-e2e/wizard'
for name, data in json.loads(%r).items():
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(data))
WIZARD_BUNDLE
exec bash "$HOME/.nanoclaw-e2e/wizard/scripts/wizard-install.sh" --run-id %s --timeout %s
""" % (json.dumps(files), shlex.quote(run_id), timeout)


def main(argv=None):
    own = argparse.ArgumentParser(description=__doc__, add_help=False)
    own.add_argument('--artifacts-dir', type=Path, help='new local sanitized evidence directory')
    own.add_argument('--wizard-timeout', type=int, default=1200, help='total PTY deadline, 1–2100 seconds (default: 1200)')
    options, remaining = own.parse_known_args(argv)
    if not LIFECYCLE.is_file():
        print('[e2e-wizard] install e2e-proxmox alongside this skill', file=sys.stderr)
        return 66
    if any(flag in remaining for flag in ('-h', '--help')):
        print(own.format_help())
    base = module('wizard_proxmox_lifecycle', LIFECYCLE)
    # Product choices and credential configuration must come from the prompts.
    base.FORWARDED = ()
    args = base.parse_args(remaining)
    custom_installer = args.installer
    artifacts = options.artifacts_dir or Path(str(args.result_file) + '.artifacts')
    protected = [HERE / 'proxmox-wizard.py', HERE / 'collect-wizard.py', LIFECYCLE,
                 *(HERE.parent / name for name in BUNDLE)]
    if custom_installer:
        protected.append(custom_installer)
    if any(args.result_file.resolve() == path.resolve() for path in protected):
        print('[e2e-wizard] --result-file cannot overwrite harness source', file=sys.stderr)
        return 64

    class WizardRun(base.Run):
        def preflight(self):
            self.report['mode'] = 'wizard'
            if custom_installer:
                raise base.Failure('--installer is unavailable in wizard mode', 64)
            # Fallible bundle preparation happens only after execute() has
            # protected input paths and invalidated any old success report.
            self.args.installer.write_text(wrapper(self.run_id, options.wizard_timeout))
            super().preflight()
            if not 1 <= options.wizard_timeout <= 2100:
                raise base.Failure('--wizard-timeout must be between 1 and 2100 seconds', 64)
            if artifacts.exists() or artifacts.is_symlink() or not artifacts.parent.is_dir():
                raise base.Failure('--artifacts-dir must be new and its parent must exist', 64)
            if self.args.result_file.resolve().is_relative_to(artifacts.resolve()):
                raise base.Failure('--result-file must be outside --artifacts-dir', 64)

        def install(self, key):
            failure = None
            try:
                super().install(key)
            except base.Failure as error:
                failure = error
            # Bootstrap/transport failures may have no result. Preserve that
            # failure; never reinterpret absent wizard evidence as a pass.
            report = self.report.get('installer')
            if report is None:
                raise failure or base.Failure('Missing wizard result', 74)
            self.report['status'] = 'running'
            self.phase('wizard-export')
            archive = self.guest('tar -C /opt/nanoclaw/logs/e2e-wizard -cf - .')
            if archive.returncode:
                raise base.Failure('Could not retrieve sanitized wizard artifacts', 74)
            try:
                collector = module('wizard_collector', HERE / 'collect-wizard.py')
                with tempfile.TemporaryDirectory(prefix='wizard-result-') as temporary:
                    nested = Path(temporary) / 'result.json'
                    collector.collect(io.BytesIO(archive.stdout), artifacts, nested,
                                      self.commit, self.run_id, report['exit_code'],
                                      self.args.key_file.expanduser())
                    self.report['wizard'] = json.loads(nested.read_text())
            except Exception as error:
                # Never print parser errors containing remote artifact text.
                raise base.Failure('Sanitized wizard artifact validation failed: ' + type(error).__name__, 74)
            if failure:
                raise failure
            self.report.update(status='pass', phase='complete')

    try:
        with tempfile.TemporaryDirectory(prefix='wizard-installer-') as temporary:
            args.installer = Path(temporary) / 'wizard.sh'
            run = WizardRun(args)
            return run.execute()
    except (base.Failure, OSError) as error:
        print('[e2e-wizard] ' + str(error), file=sys.stderr)
        return error.code if isinstance(error, base.Failure) else 74


if __name__ == '__main__':
    sys.exit(main())
