"""Exercise the Proxmox driver with real Git and a simulated SSH/PVE boundary."""

import json
from pathlib import Path
import subprocess
import sys

from test_e2e import PYTHON, Sandbox


DRIVER = Path(__file__).resolve().parents[1] / "skills/e2e-proxmox/scripts/proxmox-run.py"


class ProxmoxTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.key = self.root / "credential"
        self.key.write_text("test-key-DO-NOT-PRINT")
        self.report = self.root / "report.json"
        self.calls = self.root / "calls.jsonl"
        self.state = self.root / "guest.json"
        self.git("remote", "add", "origin", "git@github.com:example/nanoclaw.git")
        self.env.update(MOCK_CALLS=str(self.calls), MOCK_STATE=str(self.state), MOCK_COMMIT=self.commit)
        self.executable("ssh", PYTHON + r'''
import json, os, shlex, sys
from pathlib import Path
args = shlex.split(sys.argv[-1])
data = sys.stdin.buffer.read().decode()
with open(os.environ["MOCK_CALLS"], "a") as out:
    out.write(json.dumps({"ssh": sys.argv[1:], "args": args, "input": data}) + "\n")
state = Path(os.environ["MOCK_STATE"])
mode = os.environ.get("MOCK_MODE", "pass")
if args == ["id", "-u"]:
    print("1000" if mode == "not-root" else "0")
elif args == ["pveversion"]:
    print("pve-manager/9.2.18/test")
elif args[:3] == ["pvesh", "get", "/cluster/resources"]:
    print(json.dumps([{"vmid": 101}, {"vmid": 102}]))
elif args[:3] == ["pvesh", "get", "/cluster/nextid"]:
    print(json.dumps("103"))
elif args[:2] == ["pvesm", "path"]:
    print("/var/lib/vz/template/cache/debian.tar.zst")
elif args[:2] == ["pct", "create"]:
    fields = dict(zip(args[4::2], args[5::2]))
    state.write_text(json.dumps(fields))
    if mode == "create-lost-response":
        sys.exit(255)
elif args[:2] == ["pct", "config"]:
    fields = json.loads(state.read_text())
    if mode == "wrong-identity":
        fields["--description"] = "someone-elses-container"
    if mode == "extra-description":
        fields["--description"] += "%0Aanother-owner"
    fields["--description"] += "%0A"
    if mode == "privileged":
        fields["--unprivileged"] = "0"
    for key in ("hostname", "description", "unprivileged", "features"):
        print(key + ": " + fields["--" + key])
elif args[:2] == ["pct", "exec"]:
    script = args[-1]
    if "apt-get" in script and mode == "bootstrap-failure":
        sys.exit(100)
    if "exec runuser" in script and mode == "auth-failure":
        sys.exit(2)
    if script == "cat /opt/nanoclaw/logs/e2e/result.json":
        if mode == "missing-result":
            sys.exit(1)
        failed = mode == "auth-failure"
        print(json.dumps({
            "schema_version": 1, "status": "failed" if failed else "pass",
            "exit_code": 2 if failed else 0,
            "commit": "0" * 40 if mode == "stale-result" else os.environ["MOCK_COMMIT"],
            "ping": "auth_error" if failed else "ok", "phase": "ping" if failed else "complete",
        }))
elif args[:2] not in (["test", "-r"], ["ip", "link"], ["pct", "start"]):
    sys.exit("unexpected command")
''')

    def run_driver(self, *args, mode="pass"):
        self.env["MOCK_MODE"] = mode
        return subprocess.run([
            sys.executable, str(DRIVER), "--host", "root@pve.example.test",
            "--template", "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst",
            "--storage", "local-lvm", "--bridge", "vmbr0", "--key-file", str(self.key),
            "--result-file", str(self.report), *args,
        ], cwd=self.checkout, env=self.env, capture_output=True, text=True, timeout=20)

    def commands(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def result(self):
        return json.loads(self.report.read_text())

    def test_full_run_uses_new_unprivileged_guest_and_exact_commit(self):
        run = self.run_driver()
        self.assertEqual(run.returncode, 0, run.stderr)
        result = self.result()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["commit"], self.commit)
        self.assertEqual(result["guest"]["ctid"], 103)
        self.assertTrue(result["guest"]["creation_confirmed"])
        calls = self.commands()
        created = next(c["args"] for c in calls if c["args"][:2] == ["pct", "create"])
        self.assertIn("nesting=1,keyctl=1", created)
        self.assertEqual(created[created.index("--unprivileged") + 1], "1")
        self.assertEqual(created[created.index("--rootfs") + 1], "local-lvm:40")
        uploaded = [c["input"] for c in calls if "git clone --no-checkout" in c["input"]]
        self.assertEqual(len(uploaded), 1)
        self.assertIn("https://github.com/example/nanoclaw.git", uploaded[0])
        self.assertIn("checkout --detach " + self.commit, uploaded[0])
        self.assertFalse(any(c["args"][:2] in (["pct", "destroy"], ["pct", "stop"], ["pct", "clone"]) for c in calls))
        self.assertTrue(all(c["args"][2] == "103" for c in calls if c["args"][0] == "pct"))

    def test_credentials_and_forwarded_settings_use_stdin_only(self):
        token = "gateway-'quoted-$(must-not-execute)"
        self.env["NANOCLAW_ONECLI_API_TOKEN"] = token
        self.env["NANOCLAW_DISPLAY_NAME"] = "Test ' operator"
        run = self.run_driver()
        self.assertEqual(run.returncode, 0, run.stderr)
        calls = self.commands()
        public = run.stdout + run.stderr + self.report.read_text() + json.dumps([c["ssh"] for c in calls])
        self.assertNotIn(self.key.read_text(), public)
        self.assertNotIn(token, public)
        self.assertTrue(any(c["input"] == self.key.read_text() for c in calls))
        settings = next(c["input"] for c in calls if "export NANOCLAW_ONECLI_API_TOKEN=" in c["input"])
        # Execute only our generated quoting against a harmless fixture, then
        # assert exact values survived without interpreting shell substitutions.
        checked = subprocess.run(["bash", "-c", settings + '\npython3 -c \'import os,json;print(json.dumps([os.environ["NANOCLAW_ONECLI_API_TOKEN"],os.environ["NANOCLAW_DISPLAY_NAME"]]))\''],
                                 env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout), [token, "Test ' operator"])

    def test_dry_run_never_connects_or_reads_missing_credentials(self):
        self.key.unlink()
        run = self.run_driver("--dry-run")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.result()["status"], "planned")
        self.assertIsNone(self.result()["commit"])

    def test_existing_guest_id_is_refused_without_guest_mutations(self):
        run = self.run_driver("--ctid", "101")
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(any(c["args"][0] == "pct" for c in self.commands()))
        self.assertFalse(self.result()["guest"]["creation_confirmed"])

    def test_lost_create_response_never_adopts_guest_or_uploads_key(self):
        run = self.run_driver(mode="create-lost-response")
        self.assertEqual(run.returncode, 255)
        self.assertTrue(self.state.exists())
        calls = self.commands()
        self.assertEqual(calls[-1]["args"][:2], ["pct", "create"])
        self.assertFalse(any(c["input"] for c in calls))
        self.assertFalse(self.result()["guest"]["creation_confirmed"])

    def test_identity_and_unprivileged_mode_are_required_before_start(self):
        for mode in ("wrong-identity", "extra-description", "privileged"):
            with self.subTest(mode=mode):
                if self.calls.exists():
                    self.calls.unlink()
                run = self.run_driver(mode=mode)
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse(any(c["args"][:2] in (["pct", "start"], ["pct", "exec"]) for c in self.commands()))

    def test_stale_installer_result_cannot_report_pass(self):
        run = self.run_driver(mode="stale-result")
        self.assertEqual(run.returncode, 74)
        self.assertEqual(self.result()["status"], "failed")
        self.assertIsNone(self.result()["commit"])

    def test_installer_auth_failure_is_exported_as_failure(self):
        run = self.run_driver(mode="auth-failure")
        self.assertEqual(run.returncode, 2)
        self.assertEqual(self.result()["installer"]["ping"], "auth_error")
        self.assertEqual(self.result()["status"], "failed")
        self.assertEqual(self.result()["commit"], self.commit)

    def test_missing_installer_result_is_not_success(self):
        run = self.run_driver(mode="missing-result")
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(self.result()["status"], "failed")

    def test_bootstrap_failure_does_not_upload_credentials(self):
        run = self.run_driver(mode="bootstrap-failure")
        self.assertEqual(run.returncode, 100)
        self.assertFalse(any(c["input"] for c in self.commands()))
        self.assertEqual(self.result()["phase"], "bootstrap")

    def test_preflight_failures_replace_old_pass_without_host_access(self):
        for args in (("--ref", "missing-ref"), ("--bridge", "vmbr0; touch surprise"),
                     ("--repo", "https://user:password@example.test/repo.git"),
                     ("--installer", str(self.root / "missing-installer"))):
            with self.subTest(args=args):
                self.report.write_text('{"status":"pass"}')
                run = self.run_driver(*args)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(self.result()["status"], "failed")
                self.assertEqual(self.commands(), [])

    def test_key_cannot_be_overwritten_by_result_file(self):
        original = self.key.read_bytes()
        run = self.run_driver("--result-file", str(self.key))
        self.assertEqual(run.returncode, 64)
        self.assertEqual(self.key.read_bytes(), original)
        self.assertEqual(self.commands(), [])

    def test_identity_file_uses_noninteractive_host_key_verification(self):
        identity = self.root / "identity with spaces"
        run = self.run_driver("--identity-file", str(identity))
        self.assertEqual(run.returncode, 0, run.stderr)
        for call in self.commands():
            self.assertIn(str(identity), call["ssh"])
            self.assertIn("StrictHostKeyChecking=yes", call["ssh"])
            self.assertIn("IdentityAgent=none", call["ssh"])

    def test_copied_skill_accepts_explicit_shared_installer(self):
        copied = self.root / "copied-skill/scripts/proxmox-run.py"
        copied.parent.mkdir(parents=True)
        copied.write_bytes(DRIVER.read_bytes())
        copied.with_name("bootstrap.sh").write_bytes(DRIVER.with_name("bootstrap.sh").read_bytes())
        installer = DRIVER.parents[2] / "e2e-exe-dev/scripts/e2e-install.sh"
        run = subprocess.run([
            sys.executable, str(copied), "--host", "root@pve.example.test",
            "--template", "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst",
            "--storage", "local-lvm", "--bridge", "vmbr0", "--installer", str(installer),
            "--result-file", str(self.report), "--dry-run",
        ], cwd=self.checkout, env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.commands(), [])


class ProxmoxBootstrapTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.calls = self.root / "bootstrap-calls"
        self.env["MOCK_BOOTSTRAP_CALLS"] = str(self.calls)
        self.executable("getent", "#!/bin/sh\nexit 0\n")
        self.executable("id", "#!/bin/sh\nexit 1\n")
        self.executable("timeout", '#!/bin/sh\nshift\nexec "$@"\n')
        self.executable("apt-get", PYTHON + '''
import os,sys
with open(os.environ["MOCK_BOOTSTRAP_CALLS"], "a") as out:
    out.write(" ".join(sys.argv[1:]) + "\\n")
# Model an unreachable repository: normal apt update returns 0, strict update
# returns 100. If the strict option is lost, this test reaches install.
sys.exit(100 if "--error-on=any" in sys.argv else 99 if "install" in sys.argv else 0)
''')

    def test_incomplete_index_update_stops_before_package_install(self):
        run = self.run_script(DRIVER.with_name("bootstrap.sh"))
        self.assertEqual(run.returncode, 100, run.stderr)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1)
        self.assertNotIn("install", self.calls.read_text())

    def test_network_wait_timeout_stops_before_apt(self):
        self.executable("timeout", '#!/bin/sh\nexit 124\n')
        run = self.run_script(DRIVER.with_name("bootstrap.sh"))
        self.assertEqual(run.returncode, 124)
        self.assertFalse(self.calls.exists())

    def test_existing_account_stops_before_any_package_changes(self):
        self.executable("id", '#!/bin/sh\nexit 0\n')
        run = self.run_script(DRIVER.with_name("bootstrap.sh"))
        self.assertEqual(run.returncode, 65)
        self.assertFalse(self.calls.exists())

    def test_user_manager_starts_with_docker_membership(self):
        state = self.root / "account.json"
        self.env["MOCK_ACCOUNT_STATE"] = str(state)
        self.executable("apt-get", '#!/bin/sh\nexit 0\n')
        account_tool = PYTHON + '''
import json,os,sys
from pathlib import Path
path = Path(os.environ["MOCK_ACCOUNT_STATE"])
state = json.loads(path.read_text()) if path.exists() else {"created": False, "groups": []}
tool = Path(sys.argv[0]).name
if tool == "id":
    if not state["created"]: sys.exit(1)
    print("1000")
elif tool == "useradd": state["created"] = True
elif tool == "usermod": state["groups"].append(sys.argv[2])
elif tool == "systemctl": state["manager_groups"] = list(state["groups"])
path.write_text(json.dumps(state))
'''
        for name in ("id", "useradd", "groupadd", "usermod", "loginctl", "systemctl", "visudo", "install"):
            self.executable(name, account_tool)
        sudoers = self.root / "sudoers"
        sudoers.mkdir()
        # Redirect the three fixed guest filesystem paths into this fixture;
        # run the actual Bash control flow against simulated account/session tools.
        script = DRIVER.with_name("bootstrap.sh").read_text()
        script = script.replace("/etc/sudoers.d", str(sudoers))
        script = script.replace("/opt/nanoclaw", str(self.root / "app"))
        script = script.replace("/home/nanoclaw", str(self.root / "developer"))
        fixture = self.root / "bootstrap.sh"
        fixture.write_text(script)
        run = self.run_script(fixture)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("docker", json.loads(state.read_text())["manager_groups"])
