#!/usr/bin/env python3
"""Install and test an exact NanoClaw commit on this Mac or a Mac reached by SSH.

Creates a separate persistent checkout. Existing installs are never adopted;
the new checkout, agent and LaunchAgent are retained on success or failure.
"""

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid
from urllib.parse import urlsplit


SCRIPTS = Path(__file__).resolve().parent
DEFAULT_INSTALLER = SCRIPTS.parents[1] / "e2e-exe-dev/scripts/e2e-install.sh"


class Failure(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def local(args):
    result = subprocess.run(args, check=False, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise Failure(f"{args[0]} failed with exit {result.returncode}")
    return result.stdout.strip()


def save(path, report):
    fd, temporary = tempfile.mkstemp(prefix=".macos-result-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(report, output, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", help="SSH destination for a Mac; omit to install on this Mac")
    parser.add_argument("--identity-file", type=Path, help="existing SSH key; disables ssh-agent")
    parser.add_argument("--target-python", help="target Python executable; default: python3 over SSH, current Python locally")
    parser.add_argument("--install-dir", required=True, help="unused persistent checkout path on the target Mac; parent must exist")
    parser.add_argument("--gateway", required=True, choices=("reuse", "install"), help="reuse the target's configured OneCLI vault, or explicitly install a new one")
    parser.add_argument("--key-file", type=Path, help="local Anthropic credential for a NEW gateway only; sent via stdin over SSH")
    parser.add_argument("--ref", default="HEAD", help="local Git ref to test exactly (default: HEAD)")
    parser.add_argument("--repo", help="public HTTPS clone URL; defaults to the checkout's origin")
    parser.add_argument("--installer", type=Path, help="shared e2e-install.sh; defaults to the sibling installed skill")
    parser.add_argument("--display-name", default="E2E")
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--result-file", required=True, type=Path, help="local JSON report; parent must exist")
    parser.add_argument("--dry-run", action="store_true", help="read-only target checks and a local plan; never reads credentials or creates an install")
    return parser.parse_args(argv)


class Run:
    def __init__(self, args):
        self.args = args
        self.run_id = uuid.uuid4().hex
        self.installer = (args.installer or DEFAULT_INSTALLER).expanduser()
        self.key_file = (args.key_file or Path(os.environ.get("NANOCLAW_E2E_KEY_FILE", "~/.nanoclaw-e2e/anthropic_key"))).expanduser()
        self.report = {"schema_version": 1, "provider": "macos", "status": "running",
                       "exit_code": None, "phase": "preflight", "run_id": self.run_id,
                       "commit": None, "requested_ref": args.ref, "requested_commit": None,
                       "host": args.host, "mode": "ssh" if args.host else "local",
                       "install_dir": args.install_dir, "started_at": now(), "finished_at": None}

    def phase(self, phase):
        self.report["phase"] = phase
        save(self.args.result_file, self.report)
        print("[macos-run] " + phase, flush=True)

    def preflight(self):
        args = self.args
        if args.host and not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*", args.host):
            raise Failure("--host must be an SSH alias or user@hostname, without a URL or port")
        if args.identity_file and not args.host:
            raise Failure("--identity-file requires --host")
        if args.gateway == "reuse" and args.key_file:
            raise Failure("--key-file is for a new gateway; reuse never reads or transfers credentials")
        if json.loads(Path("package.json").read_text()).get("name") != "nanoclaw":
            raise Failure("run from the root of the NanoClaw checkout to test")
        commit = local(["git", "rev-parse", "--verify", "--end-of-options", args.ref + "^{commit}"])
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise Failure("could not resolve the requested commit")
        repo = args.repo or local(["git", "remote", "get-url", "origin"])
        if repo.startswith("git@github.com:"):
            repo = "https://github.com/" + repo.split(":", 1)[1]
        url = urlsplit(repo)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(c.isspace() for c in repo)):
            raise Failure("repository must be public HTTPS without embedded credentials")
        for path in (self.installer, SCRIPTS / "macos-target.py", SCRIPTS / "macos-service.py"):
            if not path.is_file():
                raise Failure("install e2e-macos and its sibling e2e-exe-dev skill, or provide --installer")
        self.report["requested_commit"] = commit
        self.request = {"action": "probe", "run_id": self.run_id, "install_dir": args.install_dir,
                        "gateway": args.gateway, "repo": repo, "commit": commit,
                        "display_name": args.display_name, "timezone": args.timezone}
        if local(["git", "status", "--porcelain"]):
            print("[macos-run] local changes are not uploaded; testing the resolved commit", file=sys.stderr)

    def target(self, request, timeout=120):
        # The target helper is code, never credential data. The entire request,
        # including an optional new-vault key, travels through stdin.
        code = (SCRIPTS / "macos-target.py").read_text()
        if self.args.host:
            command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                       "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
            if self.args.identity_file:
                command += ["-i", str(self.args.identity_file.expanduser()), "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes"]
            command += ["--", self.args.host, shlex.join([self.args.target_python or "python3", "-c", code])]
        else:
            command = [self.args.target_python or sys.executable, "-c", code]
        try:
            result = subprocess.run(command, input=json.dumps(request), text=True,
                                    stdout=subprocess.PIPE, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise Failure("target timed out; installation may still be running; inspect the retained checkout")
        try:
            response = json.loads(result.stdout)
        except ValueError:
            raise Failure("target did not return a valid result; no success is assumed")
        if not isinstance(response, dict):
            raise Failure("target returned an invalid result")
        if result.returncode:
            raise Failure(response.get("error", f"target exited {result.returncode}"))
        return response

    def execute(self):
        args = self.args
        protected = [self.key_file, self.installer, args.identity_file, Path("package.json"), *SCRIPTS.glob("*.py")]
        if args.result_file.is_symlink() or any(p and args.result_file.resolve() == p.expanduser().resolve() for p in protected):
            raise Failure("result file cannot overwrite an input file or be a symlink")
        save(args.result_file, self.report)  # Invalidate an old pass before any preflight.
        code = 1
        try:
            self.preflight()
            self.phase("target-preflight")
            plan = self.target(self.request)
            self.report["plan"] = plan
            if not plan.get("ready"):
                raise Failure("; ".join(plan.get("blockers", [plan.get("error", "target is not ready")])))
            self.report["install_dir"] = plan["install_dir"]
            if args.dry_run:
                self.report.update(status="planned", phase="dry-run")
                print(json.dumps({key: value for key, value in plan.items() if key != "before"}, indent=2))
                code = 0
            else:
                request = {**self.request, "action": "run", "before": plan["before"]}
                request["scripts"] = {
                    "e2e-install.sh": base64.b64encode(self.installer.read_bytes()).decode(),
                    "macos-service.py": base64.b64encode((SCRIPTS / "macos-service.py").read_bytes()).decode(),
                }
                if args.gateway == "install":
                    key = self.key_file.read_bytes()
                    if not key.strip():
                        raise Failure("credential file is empty")
                    request["key"] = base64.b64encode(key).decode()
                self.phase("install")
                result = self.target(request, timeout=3900)
                self.report["target_run"] = result
                if result.get("run_id") != self.run_id or result.get("status") not in ("pass", "failed"):
                    raise Failure("target result belongs to a different or incomplete run")
                self.report["phase"] = result.get("phase", "install")
                if result.get("commit") == self.request["commit"]:
                    self.report["commit"] = result["commit"]
                if result["status"] == "failed":
                    raise Failure(result.get("error", "target install failed"))
                installer = result.get("installer", {})
                service = result.get("service", {})
                preservation = result.get("preservation", {})
                if (result.get("exit_code") != 0 or result.get("phase") != "complete"
                        or result.get("commit") != self.request["commit"] or result.get("created") is not True
                        or not isinstance(preservation, dict) or preservation.get("unchanged") is not True
                        or not isinstance(installer, dict) or not isinstance(service, dict)
                        or service.get("run_id") != self.run_id
                        or installer.get("schema_version") != 1 or installer.get("phase") != "complete"
                        or installer.get("commit") != self.request["commit"]
                        or installer.get("status") != "pass" or installer.get("exit_code") != 0
                        or installer.get("ping") != "ok" or installer.get("service_type") != "launchd"):
                    raise Failure("target did not prove a matching install, real reply, LaunchAgent and preserved shared state")
                self.report.update(status="pass", phase="complete")
                code = 0
                print("[macos-run] passed; retained checkout: " + plan["install_dir"])
        except KeyboardInterrupt:
            code = 130
            self.report.update(status="failed", error="interrupted; installation may still be running")
        except (Failure, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            self.report.update(status="failed", error=str(error))
            print("[macos-run] " + str(error), file=sys.stderr)
        self.report.update(exit_code=code, finished_at=now())
        save(args.result_file, self.report)
        return code


def main():
    try:
        return Run(parse_args()).execute()
    except (Failure, OSError, ValueError) as error:
        print("[macos-run] " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
