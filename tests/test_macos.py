"""Native Mac ownership, transport and preservation without touching host services."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
from unittest.mock import patch

from test_e2e import Sandbox, PYTHON


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/e2e-macos/scripts"


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class MacDriverTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.driver = module("macos-run")
        self.report = self.root / "mac-result.json"
        self.git("remote", "add", "origin", "git@github.com:example/nanoclaw.git")
        self.calls = []
        self.plan = {"ready": True, "install_dir": "/Users/operator/nanoclaw-test", "before": {"fixture": True}}

    def response(self, request, timeout=120):
        self.calls.append(request.copy())
        if request["action"] == "probe":
            return self.plan
        installer = {"schema_version": 1, "commit": self.commit, "status": "pass", "exit_code": 0,
                     "ping": "ok", "phase": "complete", "service_type": "launchd"}
        return {"run_id": request["run_id"], "created": True, "status": "pass", "exit_code": 0,
                "phase": "complete", "commit": self.commit, "preservation": {"unchanged": True}, "installer": installer,
                "service": {"run_id": request["run_id"], "label": "com.nanoclaw-v2-test"}}

    def run_driver(self, *extra, response=None):
        args = self.driver.parse_args(["--install-dir", "/Users/operator/nanoclaw-test", "--gateway", "reuse",
                                       "--result-file", str(self.report), *extra])
        with patch.dict(os.environ, self.env, clear=True), patch.object(self.driver.Run, "target", side_effect=response or self.response):
            previous = Path.cwd()
            try:
                os.chdir(self.checkout)
                result = self.driver.Run(args).execute()
            finally:
                os.chdir(previous)
        return result

    def test_local_and_ssh_modes_require_matching_native_proof(self):
        for options in ((), ("--host", "operator@mac.example.test")):
            with self.subTest(options=options):
                self.assertEqual(self.run_driver(*options), 0)
                report = json.loads(self.report.read_text())
                self.assertEqual(report["status"], "pass")
                self.assertEqual(report["commit"], self.commit)
                self.assertEqual(report["mode"], "ssh" if options else "local")

    def test_dry_run_never_sends_scripts_or_reads_a_missing_key(self):
        self.assertEqual(self.run_driver("--gateway", "install", "--key-file", str(self.root / "missing"), "--dry-run"), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("key", self.calls[0])
        self.assertNotIn("scripts", self.calls[0])
        self.assertEqual(json.loads(self.report.read_text())["status"], "planned")

    def test_reuse_never_imports_operator_credentials(self):
        self.env["NANOCLAW_E2E_KEY_FILE"] = str(self.root / "missing")
        self.assertEqual(self.run_driver(), 0)
        self.assertTrue(all("key" not in request for request in self.calls))

    def test_failed_preflight_invalidates_old_pass_without_install(self):
        self.report.write_text('{"status":"pass"}')
        self.plan = {"ready": False, "blockers": ["no GUI session"]}
        self.assertEqual(self.run_driver(), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads(self.report.read_text())["status"], "failed")

    def test_mismatched_or_incomplete_results_never_pass(self):
        for replacement in ({"run_id": "other"}, {"commit": "0" * 40}, {"created": False},
                            {"preservation": {"unchanged": False}}, {"installer": {"ping": "ok"}},
                            {"service": {"run_id": "other"}}):
            with self.subTest(replacement=replacement):
                def response(request, timeout=120):
                    result = self.response(request)
                    return {**result, **replacement} if request["action"] == "run" else result
                self.assertEqual(self.run_driver(response=response), 1)
                self.assertEqual(json.loads(self.report.read_text())["status"], "failed")

    def test_ssh_credential_payload_uses_stdin_not_command_arguments(self):
        calls = self.root / "ssh-calls"
        self.env["MAC_SSH_CALLS"] = str(calls)
        self.executable("ssh", PYTHON + r'''
import json,os,sys
payload = json.load(sys.stdin)
with open(os.environ['MAC_SSH_CALLS'], 'w') as out:
    json.dump({'argv':sys.argv[1:], 'payload':payload}, out)
print('{}')
''')
        args = self.driver.parse_args(["--host", "operator@mac.example.test", "--identity-file", str(self.root / "ssh key"),
                                       "--install-dir", "/Users/operator/new", "--gateway", "install", "--result-file", str(self.report)])
        payload = {"action": "run", "key": "SECRET-DO-NOT-PRINT"}
        with patch.dict(os.environ, self.env, clear=True):
            self.driver.Run(args).target(payload)
        actual = json.loads(calls.read_text())
        self.assertNotIn(payload["key"], json.dumps(actual["argv"]))
        self.assertEqual(actual["payload"], payload)
        self.assertIn("StrictHostKeyChecking=yes", actual["argv"])
        self.assertIn("IdentityAgent=none", actual["argv"])

    def test_copied_install_uses_sibling_shared_installer(self):
        copied = self.root / "installed/e2e-macos/scripts"
        copied.mkdir(parents=True)
        for source in SCRIPTS.glob("*.py"):
            (copied / source.name).write_bytes(source.read_bytes())
        spec = importlib.util.spec_from_file_location("copied_mac", copied / "macos-run.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        self.assertEqual(loaded.DEFAULT_INSTALLER, (self.root / "installed/e2e-exe-dev/scripts/e2e-install.sh").resolve())

    def test_result_cannot_overwrite_a_credential(self):
        key = self.root / "key"
        key.write_text("DO-NOT-OVERWRITE")
        with self.assertRaises(self.driver.Failure):
            self.run_driver("--gateway", "install", "--key-file", str(key), "--result-file", str(key))
        self.assertEqual(key.read_text(), "DO-NOT-OVERWRITE")


class MacTargetTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.target = module("macos-target")
        self.request = {"action": "probe", "install_dir": str(self.root / "new-checkout"), "gateway": "reuse"}
        self.patchers = [patch.object(self.target.platform, "system", return_value="Darwin"),
                         patch.object(self.target.os, "getuid", return_value=501),
                         patch.object(self.target.Path, "home", return_value=self.home)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_existing_directory_is_refused_before_any_commands(self):
        dangling = self.root / "dangling-checkout"
        dangling.symlink_to(self.root / "unrelated-missing-directory")
        for existing in (self.checkout, dangling):
            with self.subTest(existing=existing), patch.object(self.target, "command") as command:
                self.request["install_dir"] = str(existing)
                with self.assertRaises(self.target.Failure):
                    self.target.probe(self.request)
                command.assert_not_called()
        self.assertTrue(dangling.is_symlink())
        self.assertFalse(dangling.exists())

    def test_root_or_non_mac_target_is_refused_before_any_commands(self):
        for attribute, value in (("system", "Linux"), ("getuid", 0)):
            owner = self.target.platform if attribute == "system" else self.target.os
            with patch.object(owner, attribute, return_value=value), patch.object(self.target, "command") as command:
                with self.assertRaises(self.target.Failure):
                    self.target.probe(self.request)
                command.assert_not_called()

    def test_missing_gui_and_docker_block_before_install(self):
        with patch.object(self.target, "succeeds", return_value=False), patch.object(self.target.shutil, "which", return_value=None):
            plan = self.target.probe(self.request)
        self.assertFalse(plan["ready"])
        self.assertTrue(any("GUI session" in reason for reason in plan["blockers"]))
        self.assertTrue(any("Docker" in reason for reason in plan["blockers"]))
        self.assertFalse(Path(self.request["install_dir"]).exists())

    def test_shared_state_checks_changes_without_rollback(self):
        before = {"assets": {"/home/ncl": {"kind": "symlink", "target": "original"}},
                  "service_pids": {"com.nanoclaw-v2-existing": 20}, "running_containers": ["onecli-id"],
                  "gateway_url": "http://gateway", "vault": {"sha256": "original"}}
        after = json.loads(json.dumps(before))
        self.assertTrue(self.target.preserved(before, after)["unchanged"])
        after["assets"]["/home/ncl"]["target"] = "other"
        after["service_pids"] = {}
        after["running_containers"] = []
        after["vault"]["sha256"] = "changed"
        result = self.target.preserved(before, after)
        self.assertFalse(result["unchanged"])
        self.assertEqual(len(result["changes"]), 4)

    def test_changed_preflight_state_stops_before_creating_checkout(self):
        plan = {"ready": True, "install_dir": self.request["install_dir"], "before": {"changed": True}}
        request = {**self.request, "run_id": "test", "before": {"old": True}}
        with patch.object(self.target, "probe", return_value=plan):
            result = self.target.run(request)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["created"])
        self.assertFalse(Path(self.request["install_dir"]).exists())

    def test_environment_drops_channel_secrets_and_shared_install_identity(self):
        with patch.dict(os.environ, {"NANOCLAW_INSTALL_ID": "personal", "GITHUB_TOKEN": "private",
                                    "TELEGRAM_BOT_TOKEN": "private", "NANOCLAW_E2E_FORCE_AUTH": "1", "DOCKER_CONTEXT": "desktop-linux"}):
            env = self.target.environment()
        for key in ("NANOCLAW_INSTALL_ID", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN", "NANOCLAW_E2E_FORCE_AUTH"):
            self.assertNotIn(key, env)
        self.assertEqual(env["DOCKER_CONTEXT"], "desktop-linux")

    def test_vault_receipt_never_contains_values_or_names(self):
        secret = {"id": "one", "type": "anthropic", "name": "PRIVATE-NAME", "value": "PRIVATE-VALUE"}
        with patch.object(self.target, "checked", return_value=json.dumps({"data": [secret]})):
            receipt = self.target.vault_inventory()
        self.assertTrue(receipt["anthropic"])
        self.assertEqual(receipt["count"], 1)
        self.assertNotIn("PRIVATE", json.dumps(receipt))


class MacTargetRunTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.target = module("macos-target")
        self.install_dir = self.root / "installed"
        self.baseline = {"assets": {}, "service_pids": {}, "running_containers": [], "gateway_url": None, "vault": None}
        self.plan = {"ready": True, "install_dir": str(self.install_dir), "before": self.baseline}
        self.executable("launchctl", '#!/bin/sh\nprintf "state = running\\npid = 123\\n"\n')
        self.installer = r'''#!/bin/bash
python3 - <<'PY'
import json,os,subprocess
from pathlib import Path
root=Path.cwd()
logs=root/'logs/e2e'
logs.mkdir(parents=True)
commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
mode=os.environ.get('MOCK_MAC_MODE','pass')
if mode=='stale': commit='0'*40
code=2 if mode=='auth-failed' else 0
if mode!='missing':
    (logs/'result.json').write_text(json.dumps({'schema_version':1,'commit':commit,'status':'failed' if code else 'pass',
        'exit_code':code,'ping':'auth_error' if code else 'ok','phase':'ping' if code else 'complete','service_type':'launchd'}))
(root/'.git/nanoclaw-e2e/service.json').write_text(json.dumps({'run_id':os.environ['NANOCLAW_E2E_RUN_ID'],'target':'gui/501/com.nanoclaw-v2-test'}))
raise SystemExit(code)
PY
'''

    def run_target(self, mode="pass", changed_after=False):
        request = {"action": "run", "install_dir": str(self.install_dir), "gateway": "install",
                   "before": self.baseline, "run_id": "owned-run", "repo": str(self.checkout), "commit": self.commit,
                   "scripts": {"e2e-install.sh": base64.b64encode(self.installer.encode()).decode(),
                               "macos-service.py": base64.b64encode(b"# fixture\n").decode()},
                   "key": base64.b64encode(b"PRIVATE-NEW-VAULT-CREDENTIAL").decode()}
        after = self.baseline
        if changed_after:
            self.baseline["running_containers"] = ["original"]
            after = {**self.baseline, "running_containers": []}
        env = {**self.env, "MOCK_MAC_MODE": mode}
        with patch.object(self.target, "probe", return_value=self.plan), \
                patch.object(self.target, "snapshot", return_value=after), \
                patch.object(self.target, "environment", return_value=env):
            return self.target.run(request)

    def test_real_checkout_and_installer_export_with_temporary_key_cleanup(self):
        result = self.run_target()
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.install_dir), self.commit)
        private = self.install_dir / ".git/nanoclaw-e2e"
        self.assertFalse((private / "credential").exists())
        self.assertEqual(json.loads((private / "result.json").read_text())["status"], "pass")
        self.assertNotIn("PRIVATE-NEW-VAULT-CREDENTIAL", json.dumps(result))
        self.assertTrue(result["preservation"]["unchanged"])

    def test_stale_installer_result_is_rejected_and_checkout_retained(self):
        result = self.run_target("stale")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["commit"])
        self.assertTrue(self.install_dir.is_dir())
        self.assertFalse((self.install_dir / ".git/nanoclaw-e2e/credential").exists())

    def test_installer_auth_failure_retains_its_result(self):
        result = self.run_target("auth-failed")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["installer"]["ping"], "auth_error")

    def test_missing_installer_result_cannot_pass(self):
        result = self.run_target("missing")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["commit"])

    def test_shared_state_change_overrides_a_successful_ping(self):
        result = self.run_target(changed_after=True)
        self.assertEqual(result["installer"]["status"], "pass")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "preservation")

    def test_racing_existing_directory_is_not_adopted(self):
        self.install_dir.mkdir()
        sentinel = self.install_dir / "personal-file"
        sentinel.write_text("keep")
        result = self.run_target()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["created"])
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertFalse((self.install_dir / ".git").exists())


class MacServiceTests(Sandbox):
    def setUp(self):
        super().setUp()
        self.service = module("macos-service")
        (self.checkout / "package.json").write_text('{"name":"nanoclaw","version":"1.0.0"}\n')
        self.private = self.checkout / ".git/nanoclaw-e2e"
        self.private.mkdir()
        self.owner = {"run_id": "owned-run", "root": str(self.checkout.resolve()), "commit": self.commit}
        (self.private / "run.json").write_text(json.dumps(self.owner))
        self.agents = self.home / "Library/LaunchAgents"
        self.agents.mkdir(parents=True)
        self.label = "com.nanoclaw-v2-owned"
        self.calls = []

    def checked(self, args, capture=False):
        self.calls.append(args)
        if args[:2] == ["git", "rev-parse"]:
            return self.commit
        if "-e" in args:
            return json.dumps({"label": self.label, "node": sys.executable})
        if args[:2] == ["launchctl", "print"]:
            return "state = running\npid = 123"
        return ""

    def install(self):
        previous = Path.cwd()
        try:
            os.chdir(self.checkout)
            with patch.dict(os.environ, {**self.env, "NANOCLAW_E2E_RUN_ID": "owned-run"}), \
                    patch.object(self.service.sys, "platform", "darwin"), \
                    patch.object(self.service.os, "getuid", return_value=501), \
                    patch.object(self.service.Path, "home", return_value=self.home), \
                    patch.object(self.service, "checked", side_effect=self.checked), \
                    patch.object(self.service.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
                self.service.install()
        finally:
            os.chdir(previous)

    def test_only_new_launchagent_is_managed_and_global_link_is_preserved(self):
        existing = self.agents / "com.nanoclaw-v2-personal.plist"
        existing.write_text("personal service")
        ncl = self.home / ".local/bin/ncl"
        ncl.parent.mkdir(parents=True, exist_ok=True)
        ncl.symlink_to("/personal/bin/ncl")
        self.install()
        self.assertEqual(existing.read_text(), "personal service")
        self.assertEqual(str(ncl.readlink()), "/personal/bin/ncl")
        definition = plistlib.loads((self.agents / (self.label + ".plist")).read_bytes())
        self.assertEqual(definition["WorkingDirectory"], str(self.checkout.resolve()))
        self.assertEqual(definition["ProgramArguments"], ["/bin/bash", str((self.private / "launch.sh").resolve())])
        launch_calls = [args for args in self.calls if args[0] == "launchctl" and args[1] != "print"]
        self.assertEqual([args[1] for args in launch_calls], ["enable", "bootstrap", "kickstart"])
        self.assertTrue(all(self.label in " ".join(args) for args in launch_calls))
        wrapper = (self.private / "launch.sh").read_text()
        self.assertIn("nanoclaw.pid", wrapper)
        self.assertIn("exec ", wrapper)

    def test_existing_service_plist_is_never_replaced(self):
        plist = self.agents / (self.label + ".plist")
        plist.write_text("belongs to somebody else")
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(plist.read_text(), "belongs to somebody else")
        self.assertFalse(any(args[:3] == ["pnpm", "run", "build"] for args in self.calls))

    def test_changed_checkout_marker_is_refused_before_service_operations(self):
        self.owner["run_id"] = "other-run"
        (self.private / "run.json").write_text(json.dumps(self.owner))
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(self.calls, [])
