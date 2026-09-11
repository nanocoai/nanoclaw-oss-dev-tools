#!/usr/bin/env python3
"""Native-Mac half of macos-run.py. Receives its request only through stdin."""

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


class Failure(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def environment():
    # Do not inherit channel credentials or a shared NANOCLAW_INSTALL_ID into
    # this independent test install. Retain the operator's Docker selection.
    names = ("HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL",
             "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
    env = {key: os.environ[key] for key in names if key in os.environ}
    env["PATH"] = os.pathsep.join([str(Path.home() / ".local/bin"), "/opt/homebrew/bin",
                                  "/opt/homebrew/sbin", "/usr/local/bin", os.environ.get("PATH", "/usr/bin:/bin")])
    env.update(GIT_TERMINAL_PROMPT="0", NANOCLAW_NO_DIAGNOSTICS="1", NANOCLAW_SKIP_CLAUDE_ASSIST="1")
    return env


def command(args, *, cwd=None, capture=True, timeout=30, env=None):
    try:
        return subprocess.run(args, cwd=cwd, env=env or environment(), check=False,
                              text=True, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE if capture else sys.stderr,
                              stderr=subprocess.PIPE if capture else sys.stderr, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Failure(f"{args[0]} could not complete ({type(error).__name__})")


def checked(args, **kwargs):
    result = command(args, **kwargs)
    if result.returncode:
        raise Failure(f"{args[0]} failed with exit {result.returncode}")
    return result.stdout.strip() if result.stdout is not None else ""


def succeeds(args):
    try:
        return command(args).returncode == 0
    except Failure:
        return False


def file_state(path):
    if path.is_symlink():
        return {"kind": "symlink", "target": str(path.readlink())}
    if path.is_file():
        return {"kind": "file", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    return {"kind": "absent"}


def gateway_url():
    raw = checked(["onecli", "config", "get", "api-host"])
    try:
        parsed = json.loads(raw)
        raw = parsed.get("data", parsed.get("value", "")) if isinstance(parsed, dict) else parsed
    except ValueError:
        pass
    if not isinstance(raw, str):
        raise Failure("existing OneCLI api-host is not a URL")
    match = re.search(r"https?://[^\s\"']+", raw)
    url = match.group(0) if match else ""
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Failure("existing OneCLI api-host is missing or contains unsupported credentials")
    return url.rstrip("/")


def vault_inventory():
    parsed = json.loads(checked(["onecli", "secrets", "list"]))
    secrets = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(secrets, list) or not all(isinstance(item, dict) for item in secrets):
        raise Failure("OneCLI did not return a secret inventory")
    # Compare identities/types, never persist or print credential values.
    identities = sorted((str(item.get("id", "")), str(item.get("type", ""))) for item in secrets)
    return {"count": len(identities), "anthropic": any(kind == "anthropic" for _, kind in identities),
            "sha256": hashlib.sha256(json.dumps(identities).encode()).hexdigest()}


def snapshot(gateway):
    home = Path.home()
    paths = list((home / "Library/LaunchAgents").glob("com.nanoclaw*.plist"))
    paths += [home / ".local/bin/ncl", home / ".config/nanoclaw/mount-allowlist.json"]
    if gateway == "reuse":
        paths += [home / ".onecli/config.json"]
    assets = {str(path): file_state(path) for path in paths}
    pids = {}
    for path in paths:
        if path.suffix != ".plist":
            continue
        result = command(["launchctl", "print", f"gui/{os.getuid()}/{path.stem}"])
        match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.M)
        if result.returncode == 0 and match:
            pids[path.stem] = int(match.group(1))
    containers = checked(["docker", "ps", "--quiet"]).splitlines() if succeeds(["docker", "info"]) else []
    return {"assets": assets, "service_pids": pids, "running_containers": containers,
            "gateway_url": gateway_url() if gateway == "reuse" else None,
            "vault": vault_inventory() if gateway == "reuse" else None}


def preserved(before, after):
    changes = []
    for path, state in before["assets"].items():
        # Creating the first mount allowlist is normal setup. Existing files
        # and the global ncl entry must retain their original identities.
        if state["kind"] == "absent" and Path(path).name != "ncl":
            continue
        if after["assets"].get(path) != state:
            changes.append(path)
    for label, pid in before["service_pids"].items():
        if after["service_pids"].get(label) != pid:
            changes.append("service:" + label)
    for identity in before["running_containers"]:
        if identity not in after["running_containers"]:
            changes.append("container:" + identity)
    if before["gateway_url"] is not None and after["gateway_url"] != before["gateway_url"]:
        changes.append("OneCLI api-host")
    if before["vault"] is not None and after["vault"] != before["vault"]:
        changes.append("OneCLI credential inventory")
    return {"unchanged": not changes, "changes": changes}


def probe(request):
    if platform.system() != "Darwin" or os.getuid() == 0:
        raise Failure("target must be macOS under a regular user, not root")
    root = Path(request["install_dir"]).expanduser()
    if not root.is_absolute() or any(c in str(root) for c in '\n\r\x00`$"'):
        raise Failure("install directory must be an absolute path without shell control characters")
    if root.exists() or root.is_symlink():
        raise Failure("install directory already exists; existing checkouts are not adopted or overwritten")
    root = root.resolve()
    if not root.parent.is_dir() or not os.access(root.parent, os.W_OK):
        raise Failure("install directory's parent must exist and be writable")
    mode = request["gateway"]
    if mode not in ("reuse", "install"):
        raise Failure("choose gateway reuse or install")
    blockers = []
    if not succeeds(["launchctl", "print", f"gui/{os.getuid()}"]):
        blockers.append("target user needs a logged-in GUI session for Docker Desktop and the LaunchAgent")
    if not succeeds(["xcode-select", "-p"]):
        blockers.append("install Xcode Command Line Tools on the target Mac first")
    path = environment()["PATH"]
    node = shutil.which("node", path=path)
    node_major = 0
    if node:
        try:
            node_major = int(checked([node, "-p", "process.versions.node.split('.')[0]"]))
        except (Failure, ValueError):
            pass
    if node_major < 22 and not shutil.which("brew", path=path):
        blockers.append("Node 22+ or Homebrew is required before NanoClaw bootstrap")
    docker_ready = succeeds(["docker", "info"])
    if not docker_ready:
        blockers.append("Docker is not ready; prepare/start it on the target Mac, then rerun preflight")
    if mode == "install":
        # Never turn a failed detection of an existing gateway into permission
        # to reinstall it. Config, Docker labels and its default port all count.
        existing = (Path.home() / ".onecli/config.json").exists()
        if shutil.which("onecli", path=path):
            try:
                existing |= bool(gateway_url())
            except Failure:
                pass
        if docker_ready:
            existing |= bool(checked(["docker", "ps", "-a", "--filter",
                                      "label=com.docker.compose.project=onecli", "--quiet"]))
        try:
            with socket.create_connection(("127.0.0.1", 10254), timeout=0.5):
                existing = True
        except OSError:
            pass
        if existing:
            blockers.append("an existing gateway/configuration was detected; select reuse or inspect it first")
    else:
        if not shutil.which("onecli", path=path):
            blockers.append("reuse requires the configured OneCLI CLI on the target")
        else:
            try:
                gateway_url()
                if not vault_inventory()["anthropic"]:
                    blockers.append("reuse requires an existing Anthropic credential; this mode never imports one")
            except (Failure, ValueError) as error:
                blockers.append(str(error))
    result = {"ready": not blockers, "blockers": blockers, "install_dir": str(root),
              "hostname": socket.gethostname(), "macos_version": platform.mac_ver()[0],
              "architecture": platform.machine(), "uid": os.getuid(), "gateway": mode,
              "docker_ready": docker_ready, "node_major": node_major}
    if not blockers:
        result["before"] = snapshot(mode)
    return result


def save(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".macos-result-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run(request):
    report = {"status": "failed", "phase": "preflight", "exit_code": 1,
              "commit": None, "run_id": request["run_id"], "created": False,
              "started_at": now(), "finished_at": None}
    root = private = None
    baseline = None
    try:
        plan = probe(request)
        report["target"] = plan
        if not plan["ready"]:
            raise Failure("; ".join(plan["blockers"]))
        baseline = plan["before"]
        if baseline != request["before"]:
            raise Failure("target state changed after preflight; inspect it before retrying")
        root = Path(plan["install_dir"])
        root.mkdir(mode=0o700)  # Atomic allocation. Never adopt a competing directory.
        report.update(created=True, phase="checkout")
        checked(["git", "clone", "--no-checkout", "--", request["repo"], str(root)], capture=False, timeout=600)
        checked(["git", "fetch", "origin", request["commit"]], cwd=root, capture=False, timeout=600)
        checked(["git", "checkout", "--detach", request["commit"]], cwd=root, capture=False)
        if checked(["git", "rev-parse", "HEAD"], cwd=root) != request["commit"]:
            raise Failure("checkout does not match requested commit")
        private = root / ".git/nanoclaw-e2e"
        private.mkdir(mode=0o700)
        owner = {"root": str(root), "commit": request["commit"], "run_id": request["run_id"]}
        (private / "run.json").write_text(json.dumps(owner))
        for name in ("e2e-install.sh", "macos-service.py"):
            (private / name).write_bytes(base64.b64decode(request["scripts"][name], validate=True))
        env = environment()
        env.update(NANOCLAW_E2E_ROOT=str(root), NANOCLAW_E2E_ONECLI_MODE=request["gateway"],
                   NANOCLAW_E2E_MACOS_SERVICE_HELPER=str(private / "macos-service.py"),
                   NANOCLAW_E2E_RUN_ID=request["run_id"],
                   NANOCLAW_DISPLAY_NAME=request.get("display_name", "E2E"),
                   NANOCLAW_E2E_TZ=request.get("timezone", "UTC"))
        if request["gateway"] == "reuse":
            env["NANOCLAW_E2E_REQUIRE_EXISTING_AUTH"] = "1"
        else:
            key = base64.b64decode(request["key"], validate=True)
            if not key.strip():
                raise Failure("credential file is empty")
            key_path = private / "credential"
            with key_path.open("xb") as output:
                output.write(key)
            key_path.chmod(0o600)
            env["NANOCLAW_E2E_KEY_FILE"] = str(key_path)
        report["phase"] = "install"
        result = command(["bash", str(private / "e2e-install.sh")], cwd=root,
                         capture=False, timeout=2400, env=env)
        report["phase"] = "export"
        installer = json.loads((root / "logs/e2e/result.json").read_text())
        expected = "pass" if result.returncode == 0 else "failed"
        if (not isinstance(installer, dict) or installer.get("schema_version") != 1 or installer.get("commit") != request["commit"]
                or installer.get("status") != expected or installer.get("exit_code") != result.returncode
                or (result.returncode == 0 and (installer.get("ping") != "ok"
                                               or installer.get("phase") != "complete"
                                               or installer.get("service_type") != "launchd"))):
            raise Failure("installer result does not match this run")
        report.update(installer=installer, commit=installer["commit"], exit_code=result.returncode)
        if result.returncode:
            raise Failure("NanoClaw installer failed; inspect the retained checkout")
        receipt = json.loads((private / "service.json").read_text())
        state = checked(["launchctl", "print", receipt["target"]])
        if receipt.get("run_id") != request["run_id"] or not re.search(r"^\s*state = running\s*$", state, re.M):
            raise Failure("this run's LaunchAgent is not running")
        report["service"] = receipt
        report.update(status="pass", phase="complete", exit_code=0)
    except (Failure, OSError, ValueError, KeyError) as error:
        report["error"] = str(error)
        if report["exit_code"] == 0:
            report["exit_code"] = 1
    finally:
        if private and (private / "credential").exists():
            (private / "credential").unlink()
        if baseline is not None and report["created"]:
            try:
                report["preservation"] = preserved(baseline, snapshot(request["gateway"]))
                if not report["preservation"]["unchanged"]:
                    report.update(status="failed", phase="preservation", exit_code=1,
                                  error="shared state changed during this run; no rollback was attempted")
            except (Failure, OSError, ValueError) as error:
                report.update(status="failed", phase="preservation", exit_code=1, error=str(error))
        report["finished_at"] = now()
        if private:
            save(private / "result.json", report)
    return report


def main():
    os.umask(0o077)
    try:
        request = json.load(sys.stdin)
        if request["action"] == "probe":
            result = probe(request)
        elif request["action"] == "run":
            result = run(request)
        else:
            raise Failure("unknown target action")
        print(json.dumps(result))
        return 0
    except (Failure, OSError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
