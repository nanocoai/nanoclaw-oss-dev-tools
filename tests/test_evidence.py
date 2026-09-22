"""Sanitized headless evidence is complete and bound to one invocation."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "skills/e2e-exe-dev/scripts/e2e-evidence.py"


def load():
    spec = importlib.util.spec_from_file_location("headless_evidence_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


evidence = load()


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="headless-evidence-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "checkout"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Evidence test"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "evidence@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=self.root, check=True)
        hooks = self.root / "empty-hooks"
        hooks.mkdir()
        subprocess.run(["git", "config", "core.hooksPath", str(hooks)], cwd=self.root, check=True)
        (self.root / "package.json").write_text('{"name":"nanoclaw"}\n')
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.root, check=True)
        self.commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        self.logs = self.root / "logs/e2e"
        self.logs.mkdir(parents=True)
        self.secret = "sk-ant-api03-FAKE_HEADLESS_CREDENTIAL_0123456789"
        self.credential = Path(self.temporary.name) / "credential"
        self.credential.write_text(self.secret)
        self.credential.chmod(0o600)
        (self.logs / "auth.log").write_text("onecli --value " + self.secret + "\n")
        (self.root / "logs/nanoclaw.log").write_text("host ready\n")
        (self.root / "logs/nanoclaw.error.log").write_text("apiToken=" + self.secret + "\n")
        (self.root / ".env").write_text("ONECLI_API_KEY=generated-private-gateway-token\n")
        self.run_id = "headlessrun1234"
        self.harness = "b" * 64
        self.devtools = "c" * 40
        self.gateway = "onecli"
        self.write_result("pass", 0)

    def write_result(self, status, code):
        (self.logs / "result.json").write_text(json.dumps({
            "schema_version": 1, "status": status, "exit_code": code,
            "commit": self.commit, "provider": "claude", "auth_method": "api",
            "auth_source_commit": self.commit, "phase": "complete" if not code else "ping",
            "ping": "ok" if not code else "auth_error", "service_type": "nohup",
            "gateway": self.gateway, "requested_gateway": self.gateway,
            "gateway_seam": self.gateway == "iron-proxy",
        }))

    def export(self):
        destination = Path(self.temporary.name) / "remote-sanitized"
        with patch.object(evidence, "safe_command", return_value={"status": "ok", "exit_code": 0, "stdout": "fixture\n"}):
            evidence.export_bundle(self.root, destination, self.credential, self.run_id,
                                   "claude", "api", self.commit, self.devtools, self.harness,
                                   self.gateway)
        return destination

    @staticmethod
    def archive(destination):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for path in destination.rglob("*"):
                if path.is_file():
                    archive.add(path, arcname=str(path.relative_to(destination)))
        output.seek(0)
        return output

    def collect(self, destination, code=0):
        local = Path(self.temporary.name) / "local-evidence"
        result = Path(self.temporary.name) / "local-result.json"
        evidence.collect(self.archive(destination), local, result, self.credential,
                         self.commit, self.run_id, code, "claude", "api",
                         self.commit, self.devtools, self.harness, self.gateway)
        return local, json.loads(result.read_text())

    def test_export_redacts_logs_and_collects_checksum_bound_evidence(self):
        remote = self.export()
        for path in remote.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.secret, path.read_text())
                self.assertNotIn("generated-private-gateway-token", path.read_text())
                self.assertEqual(path.stat().st_mode & 0o077, 0)
        local, result = self.collect(remote)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["dev_tools_commit"], self.devtools)
        self.assertEqual(result["harness_sha256"], self.harness)
        self.assertTrue((local / "runtime-state.json").is_file())
        self.assertEqual(json.loads((local / "triage.json").read_text())["status"], "not-required")
        self.assertEqual(result["gateway"], "onecli")
        self.assertEqual(json.loads((local / "runtime-state.json").read_text())["gateway"], "onecli")

    def test_iron_proxy_evidence_binds_the_selected_gateway(self):
        self.gateway = "iron-proxy"
        self.write_result("pass", 0)
        remote = self.export()
        local, result = self.collect(remote)
        self.assertEqual(result["gateway"], "iron-proxy")
        self.assertIs(result["gateway_seam"], True)
        state = json.loads((local / "runtime-state.json").read_text())
        self.assertEqual((state["gateway"], state["gateway_seam"]), ("iron-proxy", True))

    def test_gateway_mismatch_is_rejected_at_export_and_collect(self):
        self.gateway = "iron-proxy"
        with self.assertRaises(evidence.EvidenceError) as caught:
            self.export()  # result.json still says onecli
        self.assertEqual(caught.exception.code, "result-identity-mismatch")
        self.gateway = "onecli"
        remote = self.export()
        self.gateway = "iron-proxy"
        with self.assertRaises(evidence.EvidenceError) as caught:
            self.collect(remote)
        self.assertEqual(caught.exception.code, "invocation-mismatch")
        self.gateway = "onecli"
        result_json = json.loads((remote / "result.json").read_text())
        del result_json["gateway_seam"]
        content = json.dumps(result_json, indent=2) + "\n"
        (remote / "result.json").write_text(content)
        manifest = json.loads((remote / "manifest.json").read_text())
        manifest["files"]["result.json"] = hashlib.sha256(content.encode()).hexdigest()
        (remote / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(evidence.EvidenceError) as caught:
            self.collect(remote)
        self.assertEqual(caught.exception.code, "invocation-mismatch")

    def test_passing_iron_result_without_the_seam_is_rejected(self):
        self.gateway = "iron-proxy"
        (self.logs / "result.json").write_text(json.dumps({
            "schema_version": 1, "status": "pass", "exit_code": 0, "commit": self.commit,
            "provider": "claude", "auth_method": "api", "auth_source_commit": self.commit,
            "phase": "complete", "ping": "ok", "service_type": "nohup",
            "gateway": "iron-proxy", "requested_gateway": "iron-proxy", "gateway_seam": False,
        }))
        with self.assertRaises(evidence.EvidenceError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, "result-identity-mismatch")
        # A failed Iron request may report whatever the installer actually found.
        self.write_result("failed", 1)
        mismatch = json.loads((self.logs / "result.json").read_text())
        mismatch["gateway"] = "typo-from-a-service-override"
        (self.logs / "result.json").write_text(json.dumps(mismatch))
        local, result = self.collect(self.export(), code=1)
        self.assertEqual((result["gateway"], result["requested_gateway"]),
                         ("typo-from-a-service-override", "iron-proxy"))
        shutil.rmtree(Path(self.temporary.name) / "remote-sanitized")
        shutil.rmtree(local)
        mismatch.update(status="pass", exit_code=0, gateway_seam=True)
        (self.logs / "result.json").write_text(json.dumps(mismatch))
        with self.assertRaises(evidence.EvidenceError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, "result-identity-mismatch")
        # A failed Iron run may legitimately have stopped before the seam step.
        self.write_result("failed", 1)
        (self.logs / "result.json").write_text((self.logs / "result.json").read_text().replace('"gateway_seam": true', '"gateway_seam": false'))
        remote = self.export()
        local, result = self.collect(remote, code=1)
        self.assertEqual((result["gateway"], result["gateway_seam"]), ("iron-proxy", False))

    def test_failed_run_is_preserved_with_pending_triage(self):
        self.write_result("failed", 2)
        remote = self.export()
        local, result = self.collect(remote, code=2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(json.loads((local / "triage.json").read_text())["status"], "pending")

    def test_checksum_secret_and_invocation_mismatch_are_rejected(self):
        for mode in ("checksum", "secret", "provider"):
            with self.subTest(mode=mode):
                shutil.rmtree(Path(self.temporary.name) / "remote-sanitized", ignore_errors=True)
                remote = self.export()
                manifest = json.loads((remote / "manifest.json").read_text())
                if mode == "checksum":
                    (remote / "setup-logs/auth.log").write_text("changed")
                elif mode == "secret":
                    (remote / "setup-logs/auth.log").write_text(self.secret)
                    manifest["files"]["setup-logs/auth.log"] = hashlib.sha256(self.secret.encode()).hexdigest()
                    (remote / "manifest.json").write_text(json.dumps(manifest))
                else:
                    with self.assertRaises(evidence.EvidenceError) as caught:
                        evidence.collect(self.archive(remote), Path(self.temporary.name) / "bad-provider",
                                         Path(self.temporary.name) / "bad-result.json", self.credential,
                                         self.commit, self.run_id, 0, "codex", "api",
                                         self.commit, self.devtools, self.harness)
                    self.assertEqual(caught.exception.code, "invocation-mismatch")
                    continue
                with self.assertRaises(evidence.EvidenceError):
                    self.collect(remote)

    def test_symlinked_source_log_never_enters_bundle(self):
        (self.logs / "auth.log").unlink()
        (self.logs / "auth.log").symlink_to(self.credential)
        with self.assertRaises(evidence.EvidenceError):
            self.export()
        self.assertFalse((Path(self.temporary.name) / "remote-sanitized").exists())

    def test_missing_setup_log_cannot_be_collected(self):
        remote = self.export()
        (remote / "setup-logs/auth.log").unlink()
        manifest = json.loads((remote / "manifest.json").read_text())
        del manifest["files"]["setup-logs/auth.log"]
        (remote / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(evidence.EvidenceError) as caught:
            self.collect(remote)
        self.assertEqual(caught.exception.code, "acceptance-evidence-missing")
        self.assertFalse((Path(self.temporary.name) / "local-evidence").exists())


if __name__ == "__main__":
    unittest.main()
