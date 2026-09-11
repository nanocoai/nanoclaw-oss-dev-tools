#!/usr/bin/env python3
"""Install only this run's LaunchAgent; never reap peers or replace the ncl link."""

import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import sys
import time


def checked(args, *, capture=False):
    result = subprocess.run(args, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            timeout=600)
    return result.stdout.strip() if capture else None


def install():
    root = Path.cwd().resolve()
    private = root / ".git/nanoclaw-e2e"
    owner = json.loads((private / "run.json").read_text())
    if (owner.get("run_id") != os.environ.get("NANOCLAW_E2E_RUN_ID")
            or owner.get("root") != str(root)
            or checked(["git", "rev-parse", "HEAD"], capture=True) != owner.get("commit")):
        raise ValueError("checkout ownership or commit changed")
    if sys.platform != "darwin" or os.getuid() == 0:
        raise ValueError("a non-root macOS user is required")

    # Use NanoClaw's actual identifier API instead of inventing a second
    # service/image namespace. No source code is patched in the test checkout.
    identity = json.loads(checked([
        "pnpm", "exec", "tsx", "-e",
        "import {getLaunchdLabel} from './src/install-slug.ts';"
        "console.log(JSON.stringify({label:getLaunchdLabel(),node:process.execPath}))",
    ], capture=True))
    label = identity["label"]
    node = identity["node"]
    if not re.fullmatch(r"com\.nanoclaw-v2-[a-z0-9_-]+", label) or not Path(node).is_file():
        raise ValueError("invalid NanoClaw service identity or Node executable")
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{label}"
    checked(["launchctl", "print", domain], capture=True)
    agents = Path.home() / "Library/LaunchAgents"
    plist = agents / (label + ".plist")
    if plist.exists() or plist.is_symlink():
        raise ValueError("service plist already exists; it will not be replaced")
    if subprocess.run(["launchctl", "print", target], stdout=subprocess.DEVNULL,
                      stderr=subprocess.DEVNULL, timeout=15).returncode == 0:
        raise ValueError("service label is already loaded; it will not be replaced")

    checked(["pnpm", "run", "build"])
    version = str(json.loads((root / "package.json").read_text())["version"])
    checked(["pnpm", "exec", "tsx", "scripts/upgrade-state.ts", "set", version, "setup"])

    # SSH's bootstrap namespace can differ from the GUI namespace. Keep the
    # PID file understood by setup/verify.ts current across launchd restarts,
    # so its documented PID fallback can verify this exact host process too.
    wrapper = private / "launch.sh"
    wrapper.write_text("#!/bin/bash\nset -eu\n"
                       + "printf '%s\\n' \"$$\" > " + shlex.quote(str(root / "nanoclaw.pid")) + "\n"
                       + "exec " + shlex.join([node, str(root / "dist/index.js")]) + "\n")
    wrapper.chmod(0o700)
    environment = {"HOME": str(Path.home()), "PATH": os.environ["PATH"], "NANOCLAW_INSTALL_ID": ""}
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        if name in os.environ:
            environment[name] = os.environ[name]
    definition = {
        "Label": label, "ProgramArguments": ["/bin/bash", str(wrapper)],
        "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True,
        "EnvironmentVariables": environment,
        "StandardOutPath": str(root / "logs/nanoclaw.log"),
        "StandardErrorPath": str(root / "logs/nanoclaw.error.log"),
    }
    agents.mkdir(parents=True, exist_ok=True)
    # Exclusive creation closes the collision window after the earlier check.
    with plist.open("xb") as output:
        output.write(plistlib.dumps(definition))
    receipt = {"label": label, "target": target, "plist": str(plist), "run_id": owner["run_id"]}
    (private / "service.json").write_text(json.dumps(receipt, indent=2) + "\n")
    checked(["launchctl", "enable", target])
    checked(["launchctl", "bootstrap", domain, str(plist)])
    checked(["launchctl", "kickstart", target])
    for _ in range(30):
        state = checked(["launchctl", "print", target], capture=True)
        if re.search(r"^\s*state = running\s*$", state, re.M):
            print("[macos-service] running " + target)
            return
        time.sleep(1)
    raise ValueError("new LaunchAgent did not become running; inspect the retained checkout")


if __name__ == "__main__":
    try:
        install()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("[macos-service] failed: " + str(error), file=sys.stderr)
        sys.exit(1)
