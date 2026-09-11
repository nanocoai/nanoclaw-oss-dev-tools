#!/usr/bin/env python3
"""Run NanoClaw's shared E2E installer in a new unprivileged Proxmox LXC.

Only the Proxmox node needs SSH access. All guest operations use pct exec.
Containers are retained on both success and failure; existing guests are never
adopted, cloned, stopped or deleted. Requires Python 3.10+ on the operator.
"""

import argparse
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
from urllib.parse import unquote, urlsplit


FORWARDED = (
    "NANOCLAW_ONECLI_API_HOST", "NANOCLAW_ONECLI_API_TOKEN",
    "NANOCLAW_DISPLAY_NAME", "NANOCLAW_E2E_TZ", "NANOCLAW_E2E_FORCE_AUTH",
)
CHECKOUT = "/opt/nanoclaw"
PRIVATE = "/home/nanoclaw/.nanoclaw-e2e"
DEFAULT_INSTALLER = Path(__file__).resolve().parents[2] / "e2e-exe-dev/scripts/e2e-install.sh"
BOOTSTRAP = Path(__file__).with_name("bootstrap.sh")


class Failure(Exception):
    def __init__(self, message, code=69):
        super().__init__(message)
        self.code = code


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    fd, temporary = tempfile.mkstemp(prefix=".proxmox-result-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, indent=2)
            out.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def command(args, *, data=None, capture=True, timeout=120):
    try:
        return subprocess.run(
            args, input=data, stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None, timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Failure("command timed out; a remote operation may still be running", 124)
    except OSError as error:
        raise Failure(str(error))


def checked(args, **kwargs):
    result = command(args, **kwargs)
    if result.returncode:
        # Do not echo arbitrary remote diagnostics or command arguments: these
        # can contain provider output, private URLs or expanded credentials.
        raise Failure(f"command failed with exit {result.returncode}", result.returncode)
    return result.stdout.decode().strip()


def positive(value):
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(value)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Proxmox root SSH destination or configured alias")
    parser.add_argument("--identity-file", type=Path, help="existing SSH private key; disables ssh-agent")
    parser.add_argument("--template", required=True, help="existing Debian 13 OS template volume, e.g. local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst")
    parser.add_argument("--storage", required=True, help="storage for this new container's root disk")
    parser.add_argument("--bridge", required=True, help="existing bridge with DHCP and outbound access")
    parser.add_argument("--ctid", type=positive, help="unused CT ID; otherwise ask Proxmox for the next ID")
    parser.add_argument("--cores", type=positive, default=2)
    parser.add_argument("--memory", type=positive, default=8192, help="RAM in MiB (default: 8192)")
    parser.add_argument("--disk", type=positive, default=40, help="root disk in GiB (default: 40)")
    parser.add_argument("--ref", default="HEAD", help="local Git ref to test exactly (default: HEAD)")
    parser.add_argument("--repo", help="public HTTPS clone URL; defaults to the checkout's origin")
    parser.add_argument("--key-file", type=Path, default=Path(os.environ.get("NANOCLAW_E2E_KEY_FILE", "~/.nanoclaw-e2e/anthropic_key")).expanduser())
    parser.add_argument("--installer", type=Path, help="e2e-exe-dev/scripts/e2e-install.sh; default: sibling installed skill")
    parser.add_argument("--result-file", type=Path, required=True, help="local JSON report; its parent directory must exist")
    parser.add_argument("--dry-run", action="store_true", help="print the resolved plan without SSH or reading credentials")
    return parser.parse_args(argv)


class Run:
    def __init__(self, args):
        self.args = args
        self.ctid = args.ctid
        self.owned = False
        self.commit = None
        self.run_id = uuid.uuid4().hex
        self.marker = "nanoclaw-e2e-" + self.run_id
        self.report = {
            "schema_version": 1, "provider": "proxmox", "status": "running",
            "exit_code": None, "commit": None, "requested_ref": args.ref,
            "requested_commit": None, "phase": "preflight", "run_id": self.run_id,
            "started_at": now(), "finished_at": None,
            "guest": {"host": args.host, "ctid": self.ctid, "creation_confirmed": False},
        }
        self.ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                    "-o", "ServerAliveCountMax=4"]
        if args.identity_file:
            self.ssh += ["-i", str(args.identity_file.expanduser()), "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes"]
        self.ssh += ["--", args.host]

    def save(self):
        try:
            atomic_json(self.args.result_file, self.report)
        except OSError as error:
            raise Failure(f"could not write local result: {error}", 74)

    def phase(self, name):
        self.report["phase"] = name
        self.save()
        print(f"[proxmox-run] {name}", flush=True)

    def host(self, *args, **kwargs):
        return checked(self.ssh + [shlex.join(str(x) for x in args)], **kwargs)

    def config(self):
        return dict(line.split(": ", 1) for line in self.host("pct", "config", self.ctid).splitlines() if ": " in line)

    def confirm_guest(self):
        config = self.config()
        features = dict(x.split("=", 1) for x in config.get("features", "").split(",") if "=" in x)
        # pct config URI-encodes descriptions and includes the config file's
        # terminal newline (%0A). Decode it before comparing the whole marker.
        description = unquote(config.get("description", "")).rstrip("\n")
        if (description != self.marker or config.get("hostname") != self.name
                or config.get("unprivileged") != "1" or features.get("nesting") != "1"
                or features.get("keyctl") != "1"):
            raise Failure("container identity or required isolation settings do not match this run")

    def guest(self, script, *, data=None, capture=True, timeout=120):
        if not self.owned:
            raise Failure("container creation was not confirmed")
        self.confirm_guest()
        return command(self.ssh + [shlex.join([
            "pct", "exec", str(self.ctid), "--keep-env", "0", "--", "bash", "-c", script,
        ])], data=data, capture=capture, timeout=timeout)

    def guest_checked(self, script, **kwargs):
        result = self.guest(script, **kwargs)
        if result.returncode:
            raise Failure(f"container command failed with exit {result.returncode}", result.returncode)
        return result.stdout.decode().strip() if result.stdout is not None else ""

    def preflight(self):
        args = self.args
        if (not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*", args.host)):
            raise Failure("--host must be an SSH alias or user@hostname, without a port or URL", 64)
        for name in ("storage", "bridge"):
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", getattr(args, name)):
                raise Failure(f"invalid --{name}", 64)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*:vztmpl/debian-13-[A-Za-z0-9_.+-]+_amd64\.tar\.(?:zst|xz|gz)", args.template):
            raise Failure("--template must name an existing Debian 13 amd64 OS template volume", 64)
        if self.ctid is not None and not 100 <= self.ctid <= 999999999:
            raise Failure("--ctid must be between 100 and 999999999", 64)
        try:
            if json.loads(Path("package.json").read_text()).get("name") != "nanoclaw":
                raise ValueError()
        except (OSError, ValueError, AttributeError):
            raise Failure("run from the root of the NanoClaw checkout to test", 65)
        self.commit = checked(["git", "rev-parse", "--verify", "--end-of-options", args.ref + "^{commit}"])
        if not re.fullmatch(r"[a-f0-9]{40}", self.commit):
            raise Failure("could not resolve the requested commit", 65)
        self.report["requested_commit"] = self.commit
        self.repo = args.repo or checked(["git", "remote", "get-url", "origin"])
        # Public GitHub SSH origins work without installing Git credentials in
        # the guest. Other origins must explicitly provide a public HTTPS URL.
        if self.repo.startswith("git@github.com:"):
            self.repo = "https://github.com/" + self.repo.split(":", 1)[1]
        url = urlsplit(self.repo)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(x.isspace() for x in self.repo)):
            raise Failure("--repo must be a public HTTPS URL without embedded credentials", 64)
        self.installer = args.installer or DEFAULT_INSTALLER
        if not self.installer.is_file():
            raise Failure("install e2e-exe-dev alongside e2e-proxmox, or pass --installer", 66)
        if not BOOTSTRAP.is_file():
            raise Failure("bootstrap.sh is missing from the installed e2e-proxmox skill", 66)
        self.name = f"nanoclaw-e2e-{self.commit[:7]}-{self.run_id[:6]}"
        if checked(["git", "status", "--porcelain"]):
            print("[proxmox-run] local changes are not uploaded; testing the resolved commit", file=sys.stderr)

    def create(self):
        self.phase("host-preflight")
        if self.host("id", "-u") != "0":
            raise Failure("the SSH destination must run commands as root on the Proxmox node")
        self.report["proxmox_version"] = self.host("pveversion")
        if not self.report["proxmox_version"].startswith("pve-manager/"):
            raise Failure("SSH destination did not identify itself as a Proxmox node")
        guests = json.loads(self.host("pvesh", "get", "/cluster/resources", "--type", "vm", "--output-format", "json"))
        if not isinstance(guests, list):
            raise Failure("invalid Proxmox guest inventory")
        if self.ctid is None:
            self.ctid = json.loads(self.host("pvesh", "get", "/cluster/nextid", "--output-format", "json"))
        # PVE's nextid endpoint returns a JSON string, while --ctid is an int.
        if isinstance(self.ctid, str) and re.fullmatch(r"[1-9][0-9]*", self.ctid):
            self.ctid = int(self.ctid)
        if type(self.ctid) is not int or not 100 <= self.ctid <= 999999999:
            raise Failure("Proxmox returned an invalid next guest ID")
        if any(str(guest.get("vmid")) == str(self.ctid) for guest in guests):
            raise Failure(f"guest {self.ctid} already exists; use an unused ID")
        self.report["guest"]["ctid"] = self.ctid
        path = self.host("pvesm", "path", self.args.template)
        self.host("test", "-r", path)
        self.host("ip", "link", "show", "dev", self.args.bridge)
        self.phase("create")
        # pct create's success AND our random description establish ownership.
        # If the call fails or disconnects, never adopt a guest by looking it up.
        self.host(
            "pct", "create", self.ctid, self.args.template,
            "--hostname", self.name, "--description", self.marker,
            "--unprivileged", "1", "--features", "nesting=1,keyctl=1",
            "--ostype", "debian", "--arch", "amd64", "--cores", self.args.cores,
            "--memory", self.args.memory, "--swap", "0",
            "--rootfs", f"{self.args.storage}:{self.args.disk}",
            "--net0", f"name=eth0,bridge={self.args.bridge},ip=dhcp,firewall=1",
            "--tags", "nanoclaw-e2e", "--onboot", "0", "--start", "0", timeout=600,
        )
        self.confirm_guest()
        self.owned = True
        self.report["guest"]["creation_confirmed"] = True
        self.phase("start")
        self.host("pct", "start", self.ctid, timeout=120)

    def upload(self, path, data):
        self.guest_checked(
            "set -e; umask 077; cat > " + shlex.quote(path)
            + "; chmod 600 " + shlex.quote(path)
            + "; chown nanoclaw:nanoclaw " + shlex.quote(path), data=data,
        )

    def install(self, key):
        self.phase("bootstrap")
        self.guest_checked(BOOTSTRAP.read_text(), capture=False, timeout=600)
        self.phase("upload")
        self.upload(PRIVATE + "/anthropic_key", key)
        self.upload(PRIVATE + "/e2e-install.sh", self.installer.read_bytes())
        settings = "\n".join("export " + name + "=" + shlex.quote(os.environ[name]) for name in FORWARDED if name in os.environ)
        self.upload(PRIVATE + "/run-env.sh", settings.encode())
        # All application setup runs as the same developer account/session as
        # the proven helper. The test key and forwarded settings use SSH stdin.
        runner = f"""set -eu
export PATH="$HOME/.local/bin:$PATH" GIT_TERMINAL_PROMPT=0
export NANOCLAW_E2E_ROOT={CHECKOUT} NANOCLAW_E2E_KEY_FILE={PRIVATE}/anthropic_key
trap 'rm -f {PRIVATE}/anthropic_key {PRIVATE}/run-env.sh' EXIT
. {PRIVATE}/run-env.sh
rm -f {PRIVATE}/run-env.sh
git clone --no-checkout -- {shlex.quote(self.repo)} {CHECKOUT}
git -C {CHECKOUT} fetch origin {self.commit}
git -C {CHECKOUT} checkout --detach {self.commit}
test "$(git -C {CHECKOUT} rev-parse HEAD)" = {self.commit}
cd {CHECKOUT}
bash {PRIVATE}/e2e-install.sh
"""
        self.upload(PRIVATE + "/run.sh", runner.encode())
        self.phase("install")
        result = self.guest(f"""set -eu
uid=$(id -u nanoclaw)
exec runuser -u nanoclaw -- env HOME=/home/nanoclaw USER=nanoclaw LOGNAME=nanoclaw XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus bash {PRIVATE}/run.sh
""", capture=False, timeout=2400)
        self.phase("export")
        raw = self.guest_checked(f"cat {CHECKOUT}/logs/e2e/result.json")
        report = json.loads(raw)
        expected = "pass" if result.returncode == 0 else "failed"
        if (not isinstance(report, dict) or report.get("schema_version") != 1
                or report.get("commit") != self.commit or report.get("status") != expected
                or report.get("exit_code") != result.returncode
                or (result.returncode == 0 and (report.get("ping") != "ok" or report.get("phase") != "complete"))):
            raise Failure("installer result does not match this run; refusing to report a pass", 74)
        self.report["installer"] = report
        self.report["commit"] = report["commit"]
        if result.returncode:
            raise Failure("NanoClaw E2E installer failed; inspect the retained container", result.returncode)
        self.report["status"] = "pass"
        self.report["phase"] = "complete"

    def execute(self):
        # Do not replace credentials/source if the caller accidentally chooses
        # one of those paths for the report (including through a symlink).
        protected = [self.args.key_file, self.args.identity_file, self.args.installer or DEFAULT_INSTALLER, BOOTSTRAP,
                     Path(__file__), Path("package.json")]
        if any(p and self.args.result_file.resolve() == p.expanduser().resolve() for p in protected):
            raise Failure("--result-file cannot overwrite an input file", 64)
        if self.args.result_file.is_symlink():
            raise Failure("--result-file must not be a symlink", 64)
        self.save()
        code = 0
        try:
            self.preflight()
            if self.args.dry_run:
                self.report.update(status="planned", phase="dry-run")
                self.report["plan"] = {
                    "repo": self.repo, "template": self.args.template, "storage": self.args.storage,
                    "bridge": self.args.bridge, "cores": self.args.cores, "memory_mib": self.args.memory,
                    "disk_gib": self.args.disk, "unprivileged": True,
                    "features": "nesting=1,keyctl=1", "retained": True,
                }
                print(json.dumps(self.report["plan"], indent=2))
            else:
                try:
                    key = self.args.key_file.expanduser().read_bytes()
                    if not key.strip():
                        raise ValueError("empty key")
                except (OSError, ValueError):
                    raise Failure("Anthropic key file is unreadable or empty", 66)
                self.create()
                self.install(key)
        except KeyboardInterrupt:
            code = 130
            self.report["status"] = "failed"
            print("[proxmox-run] interrupted; a remote operation may still be running", file=sys.stderr)
        except (Failure, OSError, ValueError) as error:
            code = error.code if isinstance(error, Failure) else 74
            self.report["status"] = "failed"
            print(f"[proxmox-run] {error}", file=sys.stderr)
        self.report.update(exit_code=code, finished_at=now())
        self.save()
        if self.owned:
            print(f"[proxmox-run] retained CT {self.ctid}; checkout: {CHECKOUT}; report: {self.args.result_file}")
        return code


def main():
    try:
        return Run(parse_args()).execute()
    except Failure as error:
        print(f"[proxmox-run] {error}", file=sys.stderr)
        return error.code


if __name__ == "__main__":
    sys.exit(main())
