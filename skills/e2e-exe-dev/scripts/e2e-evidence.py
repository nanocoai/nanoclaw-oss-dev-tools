#!/usr/bin/env python3
"""Create or validate a sanitized headless E2E evidence bundle."""

import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile


FILE_LIMIT = 32 * 1024 * 1024
BUNDLE_LIMIT = 64 * 1024 * 1024
TOKEN = re.compile(r"sk-(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{16,}")
DIRECTORIES = (".", "setup-logs", "runtime-logs")
EXACT_PATHS = {
    "manifest.json", "result.json", "runtime-state.json", "triage.json",
    "runtime-logs/nanoclaw.log", "runtime-logs/nanoclaw.error.log",
}


class EvidenceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def reject(code):
    raise EvidenceError(code)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name("." + path.name + "." + os.urandom(6).hex())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_limited(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        reject("unsafe-source-file")
    with path.open("rb") as source:
        data = source.read(FILE_LIMIT + 1)
    if len(data) > FILE_LIMIT:
        reject("source-file-too-large")
    return data.decode("utf-8", errors="replace")


def private_values(root, credential):
    values = [credential]
    env_file = root / ".env"
    if env_file.is_file() and not env_file.is_symlink():
        for line in read_limited(env_file).splitlines():
            key, separator, value = line.partition("=")
            if separator and re.search(r"TOKEN|SECRET|PASSWORD|API_KEY", key, re.I):
                values.append(value.strip().strip("\"'"))
    return values


class Redactor:
    def __init__(self, values):
        self.values = sorted({value for value in values if len(value) >= 8}, key=len, reverse=True)

    def clean(self, text):
        for value in self.values:
            text = re.sub(r"\s*".join(map(re.escape, value)), "[REDACTED]", text)
        text = TOKEN.sub("[REDACTED]", text)
        text = re.sub(
            r"(?im)((?:[\w-]*(?:token|password|secret|api.?key)[\w-]*)[\"']?\s*[:=]\s*)[^\s,}\n]+",
            r"\1[REDACTED]", text,
        )
        text = re.sub(
            r"(?i)(--(?:value|token|password|api-key)\s+)(?:\"[^\"]*\"|'[^']*'|\S+)",
            r"\1[REDACTED]", text,
        )
        compact = re.sub(r"\s+", "", text)
        if any(re.sub(r"\s+", "", value) in compact for value in self.values):
            reject("redaction-failed")
        return text


def safe_command(args, timeout=5):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "unavailable"}
    return {
        "status": "ok" if result.returncode == 0 else "failed",
        "exit_code": result.returncode,
        "stdout": result.stdout[:FILE_LIMIT],
    }


def runtime_state(root, result, dev_tools_commit, harness_sha256):
    sock = root / "data/cli.sock"
    socket_ready = False
    try:
        socket_ready = not sock.is_symlink() and stat.S_ISSOCK(sock.stat().st_mode)
    except OSError:
        pass
    pid_state = {"present": False, "running": False}
    pid_file = root / "nanoclaw.pid"
    if pid_file.is_file() and not pid_file.is_symlink():
        raw = read_limited(pid_file).strip()
        if raw.isdigit() and int(raw) > 1:
            pid_state = {"present": True, "pid": int(raw), "running": True}
            try:
                os.kill(int(raw), 0)
            except OSError:
                pid_state["running"] = False
    docker = safe_command([
        "docker", "ps", "-a", "--no-trunc", "--format",
        "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.State}}\t{{.Status}}",
    ])
    services = (safe_command(["systemctl", "--user", "--no-pager", "--plain", "--no-legend",
                              "list-units", "nanoclaw*", "--all"])
                if sys.platform != "darwin" else
                safe_command(["launchctl", "list"]))
    return {
        "schema_version": 1,
        "captured_at": now(),
        "nanoclaw_commit": result.get("commit"),
        "auth_source_commit": result.get("auth_source_commit"),
        "dev_tools_commit": dev_tools_commit or None,
        "harness_sha256": harness_sha256,
        "service_type": result.get("service_type"),
        "service_state": services,
        "pid_state": pid_state,
        "cli_socket": {"path": "data/cli.sock", "is_socket": socket_ready},
        "container_state": docker,
    }


def export_bundle(root, destination, credential_file, run_id, provider, auth_method,
                  auth_source_commit, dev_tools_commit, harness_sha256):
    root, destination = Path(root).resolve(), Path(destination)
    if destination.exists() or destination.is_symlink():
        reject("destination-exists")
    try:
        credential = "".join(read_limited(credential_file).split())
    except EvidenceError:
        reject("credential-unreadable")
    if not credential:
        reject("credential-empty")
    result_path = root / "logs/e2e/result.json"
    try:
        result = json.loads(read_limited(result_path))
    except (EvidenceError, ValueError):
        reject("invalid-result")
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=15).strip()
    except (OSError, subprocess.SubprocessError):
        reject("source-identity-unavailable")
    if (not isinstance(result, dict) or result.get("schema_version") != 1
            or result.get("commit") != head or result.get("provider") != provider
            or result.get("auth_method") != auth_method
            or result.get("auth_source_commit") != auth_source_commit):
        reject("result-identity-mismatch")
    if not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", run_id):
        reject("invalid-run-id")
    if dev_tools_commit and not re.fullmatch(r"[a-f0-9]{40}", dev_tools_commit):
        reject("invalid-dev-tools-commit")
    if not re.fullmatch(r"[a-f0-9]{64}", harness_sha256):
        reject("invalid-harness-digest")
    redactor = Redactor(private_values(root, credential))
    result = dict(result, run_id=run_id, dev_tools_commit=dev_tools_commit or None,
                  harness_sha256=harness_sha256)
    files = {"result.json": json.dumps(result, indent=2) + "\n"}
    log_root = root / "logs/e2e"
    if log_root.is_dir() and not log_root.is_symlink():
        for path in sorted(log_root.glob("*.log")):
            files["setup-logs/" + path.name] = read_limited(path)
    for source, name in (
        (root / "logs/nanoclaw.log", "runtime-logs/nanoclaw.log"),
        (root / "logs/nanoclaw.error.log", "runtime-logs/nanoclaw.error.log"),
    ):
        if source.exists() or source.is_symlink():
            files[name] = read_limited(source)
    state = runtime_state(root, result, dev_tools_commit, harness_sha256)
    files["runtime-state.json"] = json.dumps(state, indent=2) + "\n"
    files["triage.json"] = json.dumps({
        "schema_version": 1,
        "status": "not-required" if result.get("status") == "pass" else "pending",
        "unexpected_failure": result.get("status") != "pass",
    }, indent=2) + "\n"
    cleaned = {}
    total = 0
    for name, content in files.items():
        value = redactor.clean(content)
        total += len(value.encode())
        if total > BUNDLE_LIMIT:
            reject("expanded-artifacts-too-large")
        cleaned[name] = value
    destination.mkdir(parents=True, mode=0o700)
    manifest = {"schema_version": 1, "run_id": run_id, "sanitized": True, "files": {}}
    try:
        for name, content in cleaned.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(data)
            manifest["files"][name] = hashlib.sha256(data).hexdigest()
        write_json(destination / "manifest.json", manifest)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def allowed_path(name):
    return name in EXACT_PATHS or (
        name.startswith("setup-logs/") and name.endswith(".log")
        and len(PurePosixPath(name).parts) == 2
    )


def json_file(files, name, code):
    try:
        value = json.loads(files[name].decode())
    except (KeyError, UnicodeDecodeError, ValueError):
        reject(code)
    if not isinstance(value, dict):
        reject(code)
    return value


def collect(source, destination, result_path, credential_file, commit, run_id,
            exit_code, provider, auth_method, auth_source_commit,
            dev_tools_commit, harness_sha256):
    destination, result_path = Path(destination), Path(result_path)
    if destination.exists() or destination.is_symlink():
        reject("destination-exists")
    try:
        credential = "".join(Path(credential_file).read_text().split())
    except (OSError, UnicodeError):
        reject("credential-unreadable")
    if not credential:
        reject("credential-empty")
    data = source.read(BUNDLE_LIMIT + 1)
    if len(data) > BUNDLE_LIMIT:
        reject("archive-too-large")
    files, total = {}, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for item in archive:
                name = str(PurePosixPath(item.name))
                if item.isdir() and name in DIRECTORIES:
                    continue
                if (not item.isfile() or item.size < 0 or name.startswith("/")
                        or ".." in PurePosixPath(name).parts or name in files):
                    reject("unsafe-archive-member")
                if not allowed_path(name):
                    reject("unexpected-artifact-path")
                total += item.size
                if total > BUNDLE_LIMIT:
                    reject("expanded-artifacts-too-large")
                extracted = archive.extractfile(item)
                if extracted is None:
                    reject("unsafe-archive-member")
                files[name] = extracted.read()
    except EvidenceError:
        raise
    except (tarfile.TarError, EOFError, OSError):
        reject("invalid-tar-archive")
    manifest = json_file(files, "manifest.json", "invalid-manifest")
    if (manifest.get("schema_version") != 1 or manifest.get("run_id") != run_id
            or manifest.get("sanitized") is not True):
        reject("artifact-identity-mismatch")
    manifest_files = manifest.get("files")
    if not isinstance(manifest_files, dict) or set(manifest_files) != set(files) - {"manifest.json"}:
        reject("incomplete-manifest")
    for name, digest in manifest_files.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            reject("invalid-manifest")
        if hashlib.sha256(files[name]).hexdigest() != digest:
            reject("checksum-mismatch")
    for content in files.values():
        try:
            text = content.decode()
        except UnicodeDecodeError:
            reject("invalid-artifact-text")
        if credential in "".join(text.split()) or TOKEN.search(text):
            reject("credential-found")
    result = json_file(files, "result.json", "invalid-result")
    expected_status = "pass" if exit_code == 0 else "failed"
    if (result.get("schema_version") != 1 or result.get("run_id") != run_id
            or result.get("commit") != commit or result.get("status") != expected_status
            or result.get("exit_code") != exit_code or result.get("provider") != provider
            or result.get("auth_method") != auth_method
            or result.get("auth_source_commit") != auth_source_commit
            or result.get("dev_tools_commit") != (dev_tools_commit or None)
            or result.get("harness_sha256") != harness_sha256):
        reject("invocation-mismatch")
    state = json_file(files, "runtime-state.json", "invalid-runtime-state")
    if (state.get("nanoclaw_commit") != commit
            or state.get("auth_source_commit") != auth_source_commit
            or state.get("dev_tools_commit") != (dev_tools_commit or None)
            or state.get("harness_sha256") != harness_sha256):
        reject("runtime-identity-mismatch")
    triage = json_file(files, "triage.json", "invalid-triage")
    expected_triage = "not-required" if expected_status == "pass" else "pending"
    if (triage.get("status") != expected_triage
            or triage.get("unexpected_failure") is not (expected_status == "failed")):
        reject("triage-mismatch")
    required = {"manifest.json", "result.json", "runtime-state.json", "triage.json"}
    if (not required.issubset(files)
            or not any(name.startswith("setup-logs/") and name.endswith(".log")
                       for name in files)):
        reject("acceptance-evidence-missing")
    temporary = Path(tempfile.mkdtemp(prefix=".e2e-export-", dir=destination.parent))
    try:
        for name, content in files.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(content)
        os.rename(temporary, destination)
        result["artifacts"] = str(destination.resolve())
        write_json(result_path, result)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export")
    export.add_argument("--root", type=Path, required=True)
    export.add_argument("--destination", type=Path, required=True)
    export.add_argument("--credential-file", type=Path, required=True)
    export.add_argument("--run-id", required=True)
    export.add_argument("--provider", required=True)
    export.add_argument("--auth-method", required=True)
    export.add_argument("--auth-source-commit", required=True)
    export.add_argument("--dev-tools-commit", default="")
    export.add_argument("--harness-sha256", required=True)
    collect_parser = sub.add_parser("collect")
    collect_parser.add_argument("--artifacts-dir", type=Path, required=True)
    collect_parser.add_argument("--result-file", type=Path, required=True)
    collect_parser.add_argument("--credential-file", type=Path, required=True)
    collect_parser.add_argument("--commit", required=True)
    collect_parser.add_argument("--run-id", required=True)
    collect_parser.add_argument("--exit-code", type=int, required=True)
    collect_parser.add_argument("--provider", required=True)
    collect_parser.add_argument("--auth-method", required=True)
    collect_parser.add_argument("--auth-source-commit", required=True)
    collect_parser.add_argument("--dev-tools-commit", default="")
    collect_parser.add_argument("--harness-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            export_bundle(args.root, args.destination, args.credential_file, args.run_id,
                          args.provider, args.auth_method, args.auth_source_commit,
                          args.dev_tools_commit,
                          args.harness_sha256)
            print("[e2e-evidence] sanitized headless evidence prepared")
        else:
            collect(sys.stdin.buffer, args.artifacts_dir, args.result_file,
                    args.credential_file, args.commit, args.run_id, args.exit_code,
                    args.provider, args.auth_method, args.auth_source_commit,
                    args.dev_tools_commit,
                    args.harness_sha256)
            print("[e2e-evidence] sanitized headless evidence saved")
        return 0
    except EvidenceError as error:
        print("[e2e-evidence] validation failed: " + error.code, file=sys.stderr)
        return 74
    except Exception as error:
        print("[e2e-evidence] validation failed: " + type(error).__name__, file=sys.stderr)
        return 74


if __name__ == "__main__":
    sys.exit(main())
