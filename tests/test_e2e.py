"""Offline behavioral tests: real Git/shell, simulated SSH and wizard steps.

Run: python3 -m unittest discover -s tests -v
No real VMs, external network, Docker daemon or credentials are used.
"""

import json
import os
import signal
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/e2e-exe-dev/scripts"
PYTHON = "#!" + sys.executable + "\n"


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nc-e2e-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.env = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
        self.git("init", "--initial-branch=main")
        self.git("config", "user.name", "Offline test")
        self.git("config", "user.email", "test@example.invalid")
        (self.checkout / "package.json").write_text('{"name":"nanoclaw"}\n')
        (self.checkout / "setup.sh").write_text('exit "${MOCK_BOOTSTRAP_RC:-0}"\n')
        self.commit = self.commit_change("first")

    def executable(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)
        return path

    def git(self, *args, cwd=None):
        run = subprocess.run(
            ["git", *args], cwd=cwd or self.checkout, env=self.env,
            text=True, capture_output=True, timeout=15,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip()

    def commit_change(self, value):
        (self.checkout / "version").write_text(value)
        self.git("add", ".")
        self.git("commit", "-qm", value)
        return self.git("rev-parse", "HEAD")

    def run_script(self, script, *args, cwd=None):
        return subprocess.run(
            ["bash", str(script), *args], cwd=cwd or self.checkout,
            env=self.env, text=True, capture_output=True, timeout=20,
        )


class DriverTests(Sandbox):
    def setUp(self):
        super().setUp()
        # Exercise remote-shell quoting using an actual repository path.
        self.origin = self.root / "origin with ' quote.git"
        self.git("clone", "--bare", str(self.checkout), str(self.origin))
        self.git("remote", "add", "origin", str(self.origin))
        self.vm = self.root / "vm"
        self.vm.mkdir()
        self.key = self.root / "fake-key"
        self.key.write_text("FAKE_ANTHROPIC_TOKEN")
        self.skill = self.root / "installed skill"
        self.skill.mkdir()
        self.driver = self.skill / "exe-run.sh"
        shutil.copy2(SCRIPTS / "exe-run.sh", self.driver)
        # The driver transfers this fake installer, which records actual state
        # after all remote Git commands and environment loading have run.
        (self.skill / "e2e-install.sh").write_text(
            "python3 - <<'PY'\n"
            "import json, os, subprocess\n"
            "from pathlib import Path\n"
            "observed = {'commit': subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),\n"
            " 'env': {k: v for k, v in os.environ.items() if k.startswith('NANOCLAW_')}}\n"
            "(Path.home()/'observed.json').write_text(json.dumps(observed))\n"
            "logs = Path('logs/e2e'); logs.mkdir(parents=True, exist_ok=True)\n"
            "rc = int(os.environ.get('MOCK_INSTALL_RC', '0'))\n"
            "result = {'schema_version': 1, 'commit': observed['commit'], 'exit_code': rc,\n"
            " 'status': 'failed' if rc else 'pass'}\n"
            "if os.environ.get('MOCK_WRONG_COMMIT'): result['commit'] = 'wrong'\n"
            "(logs/'result.json').write_text(json.dumps(result))\n"
            "PY\n"
            'exit "${MOCK_INSTALL_RC:-0}"\n'
        )
        self.calls_file = self.root / "ssh-calls.jsonl"
        self.env.update({
            "MOCK_VM_HOME": str(self.vm),
            "MOCK_CALLS": str(self.calls_file),
            "MOCK_CREATED": json.dumps({"vm_name": "test-vm", "ssh_dest": "test-vm.exe.xyz"}),
            "MOCK_SNAPSHOT": json.dumps({"vm_name": "saved-vm", "ssh_dest": "saved-vm.exe.xyz"}),
        })
        self.executable("ssh", PYTHON + r'''
import json, os, subprocess, sys
args = sys.argv[1:]
with open(os.environ["MOCK_CALLS"], "a") as out:
    out.write(json.dumps(args) + "\n")
while args and args[0] == "-o":
    args = args[2:]
dest, args = args[0], args[1:]
if dest == "exe.dev":
    if args[0] == "new" or (args[0] == "cp" and args[2] == "test-vm"):
        print(os.environ["MOCK_CREATED"])
        sys.exit(int(os.environ.get("MOCK_CREATE_RC", "0")))
    if args[0] == "cp":
        print(os.environ["MOCK_SNAPSHOT"])
        sys.exit(int(os.environ.get("MOCK_SNAPSHOT_RC", "0")))
    if args[0] == "rm":
        sys.exit(0)
    # Deliberately expose the old name to catch unsafe ls fallbacks.
    if args[0] == "ls":
        print(json.dumps({"vms": [{"vm_name": "test-vm", "ssh_dest": "test-vm.exe.xyz"}]}))
        sys.exit(0)
    sys.exit("unexpected lobby command")
if dest not in ("test-vm.exe.xyz", "vm+test-vm@vm.exe.xyz"):
    sys.exit("unexpected VM destination")
remote_env = {k: v for k, v in os.environ.items() if not k.startswith("NANOCLAW_")}
remote_env["HOME"] = os.environ["MOCK_VM_HOME"]
sys.exit(subprocess.run(["bash", "-c", " ".join(args)], env=remote_env).returncode)
''')

    def run_driver(self, *args):
        return self.run_script(
            self.driver, "--name", "test-vm", "--key-file", str(self.key), *args,
        )

    def calls(self):
        if not self.calls_file.exists():
            return []
        calls = []
        for line in self.calls_file.read_text().splitlines():
            args = json.loads(line)
            while args and args[0] == "-o":
                args = args[2:]
            calls.append(args)
        return calls

    def assert_no_vm_contact(self):
        self.assertTrue(all(call[0] == "exe.dev" for call in self.calls()))
        self.assertFalse(any(call[1] in ("rm", "ls") for call in self.calls()))

    def assert_retained(self):
        self.assertFalse(any(call[:2] == ["exe.dev", "rm"] for call in self.calls()))

    def test_help_works_outside_checkout(self):
        run = self.run_script(self.driver, "--help", cwd=self.root)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("--ref", run.stdout)
        self.assertEqual(self.calls(), [])

    def test_invalid_input_allocates_nothing(self):
        for args in (("--ref",), ("--ref", "does-not-exist"), ("--cpu", "1; echo nope")):
            with self.subTest(args=args):
                run = self.run_driver(*args)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_taken_name_never_reuses_or_deletes_existing_vm(self):
        self.env["MOCK_CREATED"] = "name test-vm is not available"
        for base in ((), ("--base", "base-vm")):
            with self.subTest(base=base):
                run = self.run_driver(*base, "--rm")
                self.assertEqual(run.returncode, 69, run.stderr)
                self.assert_no_vm_contact()

    def test_invalid_creation_records_stop_before_contact(self):
        records = (
            "not JSON", "null", "[]",
            json.dumps({"vm_name": "other-vm", "ssh_dest": "test-vm.exe.xyz"}),
            json.dumps({"vm_name": "test-vm"}),
            json.dumps({"vm_name": "test-vm", "ssh_dest": "-oProxyCommand=bad"}),
        )
        for record in records:
            with self.subTest(record=record):
                self.env["MOCK_CREATED"] = record
                run = self.run_driver("--rm")
                self.assertEqual(run.returncode, 69, run.stderr)
                self.assert_no_vm_contact()

    def test_failed_create_exit_is_not_treated_as_ownership(self):
        self.env["MOCK_CREATE_RC"] = "1"
        run = self.run_driver("--rm")
        self.assertEqual(run.returncode, 69)
        self.assert_no_vm_contact()

    def test_current_copy_response_confirms_base_and_snapshot(self):
        self.env["MOCK_CREATED"] = json.dumps({
            "name": "test-vm", "source": "base-vm",
            "ssh_host": "test-vm.exe.xyz", "state": "starting",
        })
        self.env["MOCK_SNAPSHOT"] = json.dumps({
            "name": "saved-vm", "source": "test-vm",
            "ssh_host": "saved-vm.exe.xyz", "state": "starting",
        })
        run = self.run_driver("--base", "base-vm", "--snapshot", "saved-vm", "--rm")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("snapshot confirmed: saved-vm", run.stdout)
        self.assertIn(["exe.dev", "rm", "test-vm"], self.calls())

    def test_copy_response_requires_matching_source_and_name(self):
        for metadata in (
            {"name": "test-vm", "source": "other-base"},
            {"name": "test-vm"},
            {"name": "other-vm", "source": "base-vm"},
            {"vm_name": "test-vm", "source": "other-base"},
        ):
            with self.subTest(metadata=metadata):
                self.env["MOCK_CREATED"] = json.dumps({**metadata, "ssh_host": "test-vm.exe.xyz"})
                run = self.run_driver("--base", "base-vm", "--rm")
                self.assertEqual(run.returncode, 69, run.stderr)
                self.assert_no_vm_contact()

    def test_copy_response_is_not_accepted_for_new(self):
        self.env["MOCK_CREATED"] = json.dumps({
            "name": "test-vm", "source": "base-vm", "ssh_host": "test-vm.exe.xyz",
        })
        run = self.run_driver("--rm")
        self.assertEqual(run.returncode, 69, run.stderr)
        self.assert_no_vm_contact()

    def test_snapshot_response_requires_matching_source(self):
        self.env["MOCK_SNAPSHOT"] = json.dumps({
            "name": "saved-vm", "source": "other-vm", "ssh_host": "saved-vm.exe.xyz",
        })
        run = self.run_driver("--snapshot", "saved-vm", "--rm")
        self.assertEqual(run.returncode, 70, run.stderr)
        self.assertIn('"source": "other-vm"', run.stderr)
        self.assert_retained()

    def test_creation_validation_survives_python_optimization(self):
        self.env["PYTHONOPTIMIZE"] = "1"
        self.env["MOCK_CREATED"] = json.dumps({
            "vm_name": "other-vm", "ssh_dest": "test-vm.exe.xyz",
        })
        run = self.run_driver("--rm")
        self.assertEqual(run.returncode, 69, run.stderr)
        self.assert_no_vm_contact()

    def test_success_checks_out_requested_commit_and_deletes_owned_vm(self):
        run = self.run_driver("--rm")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads((self.vm / "observed.json").read_text())["commit"], self.commit)
        self.assertIn(["exe.dev", "rm", "test-vm"], self.calls())
        self.assertIn("commit=" + self.commit, run.stdout)
        self.assertEqual((self.vm / ".nanoclaw-e2e/anthropic_key").stat().st_mode & 0o777, 0o600)

    def test_legacy_ssh_destination_is_preserved(self):
        self.env["MOCK_CREATED"] = json.dumps({
            "vm_name": "test-vm", "ssh_dest": "vm+test-vm@vm.exe.xyz",
        })
        run = self.run_driver()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(any(call[0] == "vm+test-vm@vm.exe.xyz" for call in self.calls()))

    def test_cached_branch_moves_to_local_requested_commit(self):
        self.git("clone", str(self.origin), str(self.vm / "nanoclaw"))
        latest = self.commit_change("second")
        self.git("push", "origin", "main")
        run = self.run_driver("--base", "base-vm", "--ref", "main")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.vm / "nanoclaw"), latest)
        self.assertEqual(json.loads((self.vm / "observed.json").read_text())["commit"], latest)

    def test_base_uses_requested_repository(self):
        self.git("clone", str(self.origin), str(self.vm / "nanoclaw"))
        latest = self.commit_change("fork-only")
        fork = self.root / "fork.git"
        self.git("clone", "--bare", str(self.checkout), str(fork))
        run = self.run_driver("--base", "base-vm", "--repo", str(fork))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.git("remote", "get-url", "origin", cwd=self.vm / "nanoclaw"), str(fork))
        self.assertEqual(json.loads((self.vm / "observed.json").read_text())["commit"], latest)

    def test_unfetchable_commit_does_not_run_installer(self):
        self.commit_change("not-pushed")
        run = self.run_driver("--rm")
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse((self.vm / "observed.json").exists())
        self.assert_retained()

    def test_dirty_base_is_preserved_and_rejected(self):
        base = self.vm / "nanoclaw"
        self.git("clone", str(self.origin), str(base))
        (base / "version").write_text("preserve this edit")
        run = self.run_driver("--base", "base-vm", "--rm")
        self.assertEqual(run.returncode, 65, run.stderr)
        self.assertEqual((base / "version").read_text(), "preserve this edit")
        self.assertFalse((self.vm / "observed.json").exists())
        self.assert_retained()

    def test_installer_failure_keeps_vm_and_skips_snapshot(self):
        self.env["MOCK_INSTALL_RC"] = "2"
        run = self.run_driver("--snapshot", "saved-vm", "--rm")
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertFalse(any(call[:2] == ["exe.dev", "cp"] for call in self.calls()))
        self.assert_retained()

    def test_failed_snapshot_is_not_confirmed_by_existing_name(self):
        self.env["MOCK_SNAPSHOT"] = "name saved-vm is not available"
        run = self.run_driver("--snapshot", "saved-vm", "--rm")
        self.assertEqual(run.returncode, 70, run.stderr)
        self.assert_retained()

    def test_confirmed_snapshot_allows_requested_removal(self):
        run = self.run_driver("--snapshot", "saved-vm", "--rm")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn(["exe.dev", "cp", "test-vm", "saved-vm", "--json"], self.calls())
        self.assertIn(["exe.dev", "rm", "test-vm"], self.calls())

    def test_result_export_survives_requested_removal(self):
        result_file = self.root / "saved result.json"
        run = self.run_driver("--rm", "--result-file", str(result_file))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(result_file.read_text())["commit"], self.commit)
        self.assertIn(["exe.dev", "rm", "test-vm"], self.calls())

    def test_mismatched_result_prevents_removal(self):
        self.env["MOCK_WRONG_COMMIT"] = "1"
        result_file = self.root / "result.json"
        run = self.run_driver("--rm", "--result-file", str(result_file))
        self.assertEqual(run.returncode, 74, run.stderr)
        result = json.loads(result_file.read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 74)
        self.assertEqual(result["phase"], "export")
        self.assertIsNone(result["commit"])
        self.assertEqual(result["requested_commit"], self.commit)
        self.assert_retained()

    def test_reused_local_result_cannot_keep_pass_after_failed_checkout(self):
        result_file = self.root / "result.json"
        first = self.run_driver("--result-file", str(result_file))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(result_file.read_text())["status"], "pass")
        latest = self.commit_change("not-pushed")
        fresh_vm = self.root / "fresh-vm"
        fresh_vm.mkdir()
        self.env["MOCK_VM_HOME"] = str(fresh_vm)
        second = self.run_driver("--rm", "--result-file", str(result_file))
        self.assertEqual(second.returncode, 128, second.stderr)
        result = json.loads(result_file.read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "checkout")
        self.assertEqual(result["exit_code"], second.returncode)
        self.assertEqual(result["requested_commit"], latest)
        self.assertIsNone(result["commit"])
        self.assertFalse((fresh_vm / "observed.json").exists())
        self.assert_retained()

    def test_early_failures_replace_old_local_pass(self):
        result_file = self.root / "result.json"
        self.env["MOCK_CREATED"] = "name test-vm is not available"
        for args, rc, phase, requested in (
            (("--ref", "missing-ref"), 65, "preflight", None),
            (("--key-file", str(self.root / "missing-key")), 66, "preflight", self.commit),
            ((), 69, "create", self.commit),
        ):
            with self.subTest(args=args):
                result_file.write_text('{"schema_version":1,"status":"pass","commit":"old"}')
                run = self.run_driver(*args, "--rm", "--result-file", str(result_file))
                self.assertEqual(run.returncode, rc, run.stderr)
                result = json.loads(result_file.read_text())
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["phase"], phase)
                self.assertEqual(result["exit_code"], rc)
                self.assertEqual(result["requested_commit"], requested)
                self.assertIsNone(result["commit"])
                self.assert_no_vm_contact()

    def test_local_result_is_invalidated_before_vm_creation(self):
        result_file = self.root / "result.json"
        result_file.write_text('{"status":"pass","commit":"old"}')
        ssh = self.bin / "ssh"
        ssh.write_text(PYTHON + "import json, sys\n"
                       + "result = json.load(open(" + repr(str(result_file)) + "))\n"
                       + "sys.exit(0 if result['status'] == 'running' else 98)\n")
        run = self.run_driver("--result-file", str(result_file))
        self.assertEqual(run.returncode, 69, run.stderr)
        self.assertIn("creation was not confirmed", run.stderr)

    def test_failed_installer_result_preserves_probe_details(self):
        self.env["MOCK_INSTALL_RC"] = "2"
        result_file = self.root / "result.json"
        run = self.run_driver("--rm", "--result-file", str(result_file))
        self.assertEqual(run.returncode, 2, run.stderr)
        result = json.loads(result_file.read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["commit"], self.commit)
        self.assert_retained()

    def test_snapshot_failure_preserves_completed_test_result(self):
        self.env["MOCK_SNAPSHOT_RC"] = "1"
        result_file = self.root / "result.json"
        run = self.run_driver("--rm", "--snapshot", "saved-vm", "--result-file", str(result_file))
        self.assertEqual(run.returncode, 70, run.stderr)
        result = json.loads(result_file.read_text())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["commit"], self.commit)
        self.assert_retained()

    def test_checkout_failure_invalidates_base_result(self):
        base = self.vm / "nanoclaw"
        self.git("clone", str(self.origin), str(base))
        logs = base / "logs/e2e"
        logs.mkdir(parents=True)
        (logs / "result.json").write_text('{"schema_version":1,"status":"pass"}')
        (base / "version").write_text("preserve this edit")
        result_file = self.root / "result.json"
        run = self.run_driver("--base", "base-vm", "--rm", "--result-file", str(result_file))
        self.assertEqual(run.returncode, 65, run.stderr)
        result = json.loads(result_file.read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "checkout")
        self.assertEqual(result["exit_code"], 65)
        self.assertEqual(result["requested_commit"], self.commit)
        self.assert_retained()

    def test_settings_arrive_without_secrets_in_ssh_arguments(self):
        settings = {
            "NANOCLAW_E2E_FORCE_AUTH": "1",
            "NANOCLAW_E2E_TZ": "Europe/Madrid",
            "NANOCLAW_DISPLAY_NAME": "Test's name with spaces",
            "NANOCLAW_ONECLI_API_TOKEN": "FAKE_REMOTE_TOKEN_'\"$()\nsecond line",
        }
        self.env.update(settings)
        run = self.run_driver()
        self.assertEqual(run.returncode, 0, run.stderr)
        observed = json.loads((self.vm / "observed.json").read_text())["env"]
        for name, value in settings.items():
            self.assertEqual(observed[name], value)
        self.assertFalse((self.vm / ".nanoclaw-e2e/run-env.sh").exists())
        exposed = run.stdout + run.stderr + self.calls_file.read_text()
        self.assertNotIn("FAKE_REMOTE_TOKEN", exposed)
        self.assertNotIn("FAKE_ANTHROPIC_TOKEN", exposed)


class InstallerTests(Sandbox):
    def setUp(self):
        super().setUp()
        (self.checkout / "data").mkdir()
        self.sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.sock.close)
        self.sock.bind(str(self.checkout / "data/cli.sock"))
        self.executable("docker", "#!/bin/sh\nexit 0\n")
        self.executable("uname", "#!/bin/sh\necho Linux\n")
        self.executable("timeout", '#!/bin/sh\nshift\nexec "$@"\n')
        self.executable("pnpm", PYTHON + r'''
import os, sys
args = sys.argv[1:]
if os.environ.get("MOCK_STEP_CALLS"):
    with open(os.environ["MOCK_STEP_CALLS"], "a") as calls:
        calls.write(repr(args) + "\n")
if os.environ.get("MOCK_PATH_FILE"):
    with open(os.environ["MOCK_PATH_FILE"], "w") as seen:
        seen.write(os.environ.get("PATH", ""))
if "--step" in args:
    name = args[args.index("--step") + 1]
    if os.environ.get("MOCK_NO_BLOCK") == name:
        print("step returned without a status")
        sys.exit(0)
    status = "success"
    if name == "auth" and "--check" in args:
        status = os.environ.get("MOCK_AUTH_STATUS", "success")
    if os.environ.get("MOCK_FAIL_STEP") == name:
        status = "failed"
    if name == "mounts":
        status = "skipped"
    print("=== NANOCLAW SETUP: " + name.upper() + " ===")
    print("STATUS: " + status)
    if name == "service":
        print("SERVICE_TYPE: systemd-user")
    print("=== END ===")
    sys.exit(int(os.environ.get("MOCK_STEP_RC", "0")))
if "chat" in args:
    print(os.environ.get("MOCK_PING", "pong"))
    print(os.environ.get("MOCK_PING_ERR", ""), file=sys.stderr)
    sys.exit(int(os.environ.get("MOCK_PING_RC", "0")))
if "scripts/init-cli-agent.ts" in args and os.environ.get("MOCK_MIGRATIONS_READY"):
    from pathlib import Path
    if not Path(os.environ["MOCK_MIGRATIONS_READY"]).exists():
        print("host migrations are still running", file=sys.stderr)
        sys.exit(73)
sys.exit(0)
''')

    def run_installer(self):
        return self.run_script(SCRIPTS / "e2e-install.sh")

    def result(self):
        return json.loads((self.checkout / "logs/e2e/result.json").read_text())

    def test_pass_records_tested_commit_and_probe(self):
        run = self.run_installer()
        self.assertEqual(run.returncode, 0, run.stderr)
        result = self.result()
        self.assertEqual(result["commit"], self.commit)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(result["ping"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["tracked_changes_at_start"])
        self.assertIn("COMMIT: " + self.commit, run.stdout)

    def test_system_sh_on_path_runs_without_a_note(self):
        run = self.run_installer()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("not the system shell", run.stdout + run.stderr)

    def test_foreign_sh_is_dropped_from_path_before_any_step(self):
        # exe.dev images put /exe.dev/bin first on PATH with their own `sh`
        # (builtin lsof always exits 0, so OneCLI's port probe fails). The
        # installer must run every step with the system shell as `sh`.
        foreign = self.root / "exe.dev" / "bin"
        foreign.mkdir(parents=True)
        similarly_named = self.root / "exeXdev" / "bin"
        similarly_named.mkdir(parents=True)
        shim = foreign / "sh"
        shim.write_text("#!/bin/bash\necho foreign-sh >&2\nexit 0\n")
        shim.chmod(0o755)
        self.env["PATH"] = str(foreign) + os.pathsep + str(similarly_named) + os.pathsep + self.env["PATH"]
        seen = self.root / "seen-path"
        self.env["MOCK_PATH_FILE"] = str(seen)
        run = self.run_installer()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("not the system shell", run.stdout)
        self.assertIn(str(foreign), run.stdout)
        self.assertEqual(self.result()["status"], "pass")
        step_path = seen.read_text().split(os.pathsep)
        self.assertNotIn(str(foreign), step_path)
        self.assertIn(str(similarly_named), step_path)
        self.assertIn(str(self.bin), step_path)
        self.assertNotIn("foreign-sh", run.stdout + run.stderr)

    def test_bootstrap_failure_replaces_old_pass(self):
        logs = self.checkout / "logs/e2e"
        logs.mkdir(parents=True)
        (logs / "result.json").write_text('{"status":"pass"}')
        self.env["MOCK_BOOTSTRAP_RC"] = "1"
        run = self.run_installer()
        self.assertEqual(run.returncode, 1)
        self.assertEqual(self.result()["status"], "failed")
        self.assertEqual(self.result()["phase"], "bootstrap")
        self.assertEqual(self.result()["ping"], "not_run")

    def test_zero_exit_failed_status_cannot_pass(self):
        self.env["MOCK_FAIL_STEP"] = "verify"
        run = self.run_installer()
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(self.result()["phase"], "verify")
        self.assertEqual(self.result()["status"], "failed")
        self.assertEqual(self.result()["ping"], "ok")
        self.assertNotIn("STATUS: pass", run.stdout)

    def test_missing_status_block_fails(self):
        self.env["MOCK_NO_BLOCK"] = "container"
        run = self.run_installer()
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(self.result()["phase"], "container")
        self.assertEqual(self.result()["status"], "failed")

    def test_ping_failures_never_pass(self):
        cases = [
            ({"MOCK_PING": " \t "}, 2, "no_reply"),
            ({"MOCK_PING": "Invalid API key"}, 2, "auth_error"),
            ({"MOCK_PING_ERR": "authentication_error"}, 2, "auth_error"),
            ({"MOCK_PING_RC": "2"}, 3, "socket_error"),
            ({"MOCK_PING_RC": "3"}, 2, "no_reply"),
        ]
        for settings, rc, ping in cases:
            with self.subTest(settings=settings):
                for name in ("MOCK_PING", "MOCK_PING_ERR", "MOCK_PING_RC"):
                    self.env.pop(name, None)
                self.env.update(settings)
                run = self.run_installer()
                self.assertEqual(run.returncode, rc, run.stderr)
                self.assertEqual(self.result()["ping"], ping)
                self.assertEqual(self.result()["status"], "failed")

    def test_large_auth_error_output_never_passes(self):
        for stream in ("MOCK_PING", "MOCK_PING_ERR"):
            with self.subTest(stream=stream):
                self.env.pop("MOCK_PING", None)
                self.env.pop("MOCK_PING_ERR", None)
                self.env[stream] = "Invalid API key\n" + "x" * 100000
                run = self.run_installer()
                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertEqual(self.result()["ping"], "auth_error")
                self.assertEqual(self.result()["status"], "failed")
                self.assertNotIn("STATUS: pass", run.stdout)

    def test_large_successful_reply_still_passes(self):
        self.env["MOCK_PING"] = "pong\n" + "x" * 100000
        run = self.run_installer()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.result()["status"], "pass")
        self.assertEqual(self.result()["ping"], "ok")

    def test_auth_create_redacts_key_and_accepts_missing_check(self):
        key = self.root / "fake-key"
        key.write_text("FAKE_SECRET_DO_NOT_LOG")
        self.env.update({"MOCK_AUTH_STATUS": "missing", "NANOCLAW_E2E_KEY_FILE": str(key)})
        run = self.run_installer()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("<redacted>", run.stdout)
        self.assertNotIn("FAKE_SECRET_DO_NOT_LOG", run.stdout + run.stderr)
        self.assertEqual(self.result()["status"], "pass")

    def test_records_tracked_modifications_for_standalone_run(self):
        (self.checkout / "version").write_text("local edit")
        run = self.run_installer()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(self.result()["tracked_changes_at_start"])

    def macos(self):
        self.executable("uname", "#!/bin/sh\necho Darwin\n")
        helper = self.root / "macos-service.py"
        helper.write_text('print("native service started")\n')
        self.env["NANOCLAW_E2E_MACOS_SERVICE_HELPER"] = str(helper)
        calls = self.root / "step-calls"
        self.env["MOCK_STEP_CALLS"] = str(calls)
        return helper, calls

    def test_mac_uses_owned_service_helper_and_no_gnu_timeout(self):
        _, calls = self.macos()
        self.executable("timeout", "#!/bin/sh\nexit 99\n")
        run = self.run_installer()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.result()["service_type"], "launchd")
        self.assertNotIn("'--step', 'service'", calls.read_text())
        self.assertIn("native service started", run.stdout)

    def test_mac_refuses_unready_docker_without_linux_repair_commands(self):
        self.macos()
        self.executable("docker", "#!/bin/sh\nexit 1\n")
        sentinel = self.root / "linux-repair"
        self.executable("sudo", '#!/bin/sh\ntouch "' + str(sentinel) + '"\nexit 1\n')
        run = self.run_installer()
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(self.result()["phase"], "docker")
        self.assertFalse(sentinel.exists())

    def test_mac_service_failure_cannot_reach_ping_or_pass(self):
        helper, calls = self.macos()
        helper.write_text('import sys\nsys.exit(8)\n')
        run = self.run_installer()
        self.assertEqual(run.returncode, 8)
        self.assertEqual(self.result()["phase"], "service")
        self.assertEqual(self.result()["ping"], "not_run")
        self.assertNotIn("'chat'", calls.read_text())

    def test_agent_initialization_waits_for_host_migrations(self):
        self.macos()
        self.sock.close()
        (self.checkout / "data/cli.sock").unlink()
        ready = self.root / "migrations-ready"
        self.env["MOCK_MIGRATIONS_READY"] = str(ready)
        # NanoClaw opens its CLI socket only after host migrations complete.
        # Advance that simulated startup during the installer's first wait.
        self.executable("sleep", PYTHON + '''
import os, socket
from pathlib import Path
Path(os.environ["MOCK_MIGRATIONS_READY"]).touch()
with socket.socket(socket.AF_UNIX) as sock:
    sock.bind("data/cli.sock")
''')
        run = self.run_installer()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(ready.exists())
        self.assertEqual(self.result()["ping"], "ok")

    def test_unready_host_cannot_initialize_agent_or_send_ping(self):
        _, calls = self.macos()
        self.sock.close()
        (self.checkout / "data/cli.sock").unlink()
        self.executable("sleep", "#!/bin/sh\nexit 0\n")
        run = self.run_installer()
        self.assertEqual(run.returncode, 3, run.stderr)
        self.assertEqual(self.result()["phase"], "socket")
        self.assertEqual(self.result()["ping"], "not_run")
        self.assertNotIn("scripts/init-cli-agent.ts", calls.read_text())
        self.assertNotIn("'chat'", calls.read_text())

    def test_explicit_gateway_reuse_never_falls_back_to_install(self):
        _, calls = self.macos()
        self.env["NANOCLAW_E2E_ONECLI_MODE"] = "reuse"
        run = self.run_installer()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("'--step', 'onecli', '--reuse'", calls.read_text())

    def test_shared_gateway_missing_credential_is_never_seeded(self):
        _, calls = self.macos()
        key = self.root / "credential"
        key.write_text("MUST_NOT_IMPORT")
        self.env.update(NANOCLAW_E2E_REQUIRE_EXISTING_AUTH="1", MOCK_AUTH_STATUS="missing",
                        NANOCLAW_E2E_KEY_FILE=str(key))
        run = self.run_installer()
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn("--create", calls.read_text())
        self.assertNotIn("MUST_NOT_IMPORT", run.stdout + run.stderr)

    def test_shared_gateway_force_auth_is_refused(self):
        _, calls = self.macos()
        self.env.update(NANOCLAW_E2E_REQUIRE_EXISTING_AUTH="1", NANOCLAW_E2E_FORCE_AUTH="1")
        run = self.run_installer()
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn("--create", calls.read_text())

    def test_probe_timeout_kills_children_that_keep_output_open(self):
        source = (SCRIPTS / "e2e-install.sh").read_text()
        start = source.index("run_bounded() {")
        end = source.index("\n}\n", start) + 3
        function = self.root / "bounded.sh"
        function.write_text(source[start:end] + '\nrun_bounded "$@"\n')
        child_pid = self.root / "child-pid"
        child = self.root / "child.py"
        child.write_text('import os,signal,time\nfrom pathlib import Path\n'
                         'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
                         + 'Path(' + repr(str(child_pid)) + ').write_text(str(os.getpid()))\ntime.sleep(30)\n')
        parent = self.root / "parent.py"
        parent.write_text('import subprocess,sys,time\n'
                          + 'subprocess.Popen([sys.executable,' + repr(str(child)) + '])\ntime.sleep(30)\n')
        try:
            run = subprocess.run(["bash", str(function), "1", sys.executable, str(parent)],
                                 env=self.env, capture_output=True, text=True, timeout=7)
            self.assertEqual(run.returncode, 124, run.stderr)
        finally:
            if child_pid.exists():
                try:
                    os.kill(int(child_pid.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
