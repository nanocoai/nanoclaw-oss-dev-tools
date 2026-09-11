---
name: e2e-macos
description: Install and test an exact NanoClaw commit on an existing Mac, locally or over SSH, using a separate retained checkout and a real CLI-agent reply. Use for native macOS install and integration testing while preserving existing NanoClaw services and shared OneCLI credentials.
license: MIT
---

# NanoClaw E2E on macOS

Use this skill for a Mac you are working on or a Mac accessible through an
existing SSH connection. [scripts/macos-run.py](scripts/macos-run.py) creates a
separate persistent checkout, runs NanoClaw's setup steps, starts its own
LaunchAgent, and requires a real model reply and successful verification.

Run the driver from the **NanoClaw source checkout under test**. It resolves
`--ref` locally and fetches that exact commit into the new installation directory.
Local edits are not uploaded. The target can be this Mac or a different Mac;
the operator can also launch the SSH mode from Linux.

## Install and target prerequisites

Install this skill and the companion shared installer:

```bash
npx skills add nanocoai/nanoclaw-oss-dev-tools --global --skill e2e-exe-dev
npx skills add nanocoai/nanoclaw-oss-dev-tools --global --skill e2e-macos
```

Both also ship in the repository's Claude Code plugin. The driver locates
`e2e-exe-dev/scripts/e2e-install.sh` beside its own installed skill; pass
`--installer` if they are in different locations. Copies and symlinks work.

Check the intended target before installing:

- Python 3.9+, Git, and a regular macOS user account. Do not run as root or use
  `sudo` for the driver. `--target-python` selects another installed Python.
- Xcode Command Line Tools, plus Node 22+ or Homebrew so NanoClaw can install
  Node. The shared bootstrap installs pinned pnpm and checkout dependencies.
- A working Docker CLI and daemon. On Docker Desktop, complete its first-launch
  GUI setup first. Inspect an existing runtime before deciding to install or
  start another one. The driver stops when Docker is unavailable; it never
  applies Linux service/group repairs on a Mac.
- An active GUI login session **for that same user**. The LaunchAgent is loaded
  explicitly into `gui/<uid>`, including when launched over SSH. An SSH login
  alone does not create a GUI session. This first version does not configure an
  unattended LaunchDaemon or automatic login.
- A new, unused installation path with an existing writable parent. Prefer a
  persistent development directory. Existing directories, even empty ones or
  earlier test installations, are not adopted or overwritten.
- For SSH: an existing destination and verified host key. `--identity-file`
  selects a key and disables ssh-agent. Ports and other connection settings
  belong in the operator's SSH configuration. The skill does not enable Remote
  Login, enroll keys, or modify SSH configuration.

If prerequisites are missing, prepare them within the user's chosen scope on
the target Mac, then rerun preflight. NanoClaw's `setup/install-node.sh` and
`setup/install-docker.sh` contain its macOS installation paths; Homebrew, Xcode
tools, and Docker Desktop can require interaction. Keep that prerequisite work
distinct from a completed E2E result.

## Choose the gateway explicitly

`--gateway reuse` uses the target user's already-configured OneCLI gateway and
existing Anthropic credential. It never reads or transfers an operator key,
reinstalls the gateway, selects a different endpoint, or replaces a vault secret.
An unavailable gateway or missing credential stops the run. A detected gateway
is a choice to present, not permission to reinstall it.

`--gateway install` permits a new OneCLI installation. It refuses a detected
existing configuration, OneCLI container, configured endpoint, or occupied
default gateway port. Supply an authorized Anthropic credential using
`--key-file`, `NANOCLAW_E2E_KEY_FILE`, or the default
`~/.nanoclaw-e2e/anthropic_key` **on the operator machine**. Over SSH, the key
travels through stdin and is temporarily stored in a private file in the new
checkout. That file is removed after the installer exits; the OneCLI vault
retains the credential. No force-replacement mode is supported here.

Creating a persistent checkout/service and making a provider request must be
within the user's authorization for that Mac. A request to develop or review
the skill alone does not authorize a live install on the operator's personal
machine. Prepare the code and a concrete preflight plan before asking for any
additional authorization that is actually needed. Reuse prior scoped approval.

## Local and SSH runs

Set `MACOS_SKILL_DIR` to the directory containing this `SKILL.md`. From the
NanoClaw checkout, plan a local install:

```bash
python3 "$MACOS_SKILL_DIR/scripts/macos-run.py" \
  --install-dir /Users/operator/work/nanoclaw-e2e \
  --gateway reuse --ref origin/main \
  --result-file /path/to/macos-result.json --dry-run
```

For a Mac reached over SSH, add `--host` and use a path on that Mac:

```bash
python3 "$MACOS_SKILL_DIR/scripts/macos-run.py" \
  --host operator@mac.example.test \
  --install-dir /Users/operator/work/nanoclaw-e2e \
  --gateway reuse --ref origin/main \
  --result-file /path/to/macos-result.json --dry-run
```

Use actual usernames, paths and connection settings. Public HTTPS repository
origins are supported; GitHub SSH origins are converted to HTTPS. `--repo`
selects another public source repository. Private Git authentication is outside
this first version.

Dry run performs **read-only target checks**, including over SSH, and writes a
local `planned` report. It does not read credentials or create an installation.
Inspect readiness, the target identity, the checkout path and the gateway
choice. Remove `--dry-run` to execute the authorized plan. The driver repeats
preflight and checks for changed shared state before creating the directory.

Optional `--display-name` and `--timezone` configure the test agent. Each run
uses a new checkout, so its CLI agent and database are separate from existing
installations. It does not connect real messaging channels.

## Service and shared-host preservation

The shared installer drives bootstrap, environment, OneCLI/auth, image build,
mount configuration, timezone, CLI-agent creation, the model ping, and final
NanoClaw verification. The Mac-specific service helper builds the host, stamps
the upgrade marker through NanoClaw's CLI, asks NanoClaw for its actual service
label, and creates only that checkout's LaunchAgent.

This deliberately replaces the **service setup step** for this test workflow:
NanoClaw's normal `setup/service.ts` may clean up unhealthy/dead peer services
and replace `~/.local/bin/ncl`. The E2E helper leaves those peers and that link
alone. It refuses an existing plist or loaded label rather than unloading it.
The launcher maintains NanoClaw's PID file so `setup/verify.ts` can also verify
the process from an SSH bootstrap namespace. Its PATH includes the discovered
Homebrew/user tools; the service is still a native LaunchAgent.

Before and after the run, compare existing NanoClaw plist contents and service
PIDs, the global `ncl` link, an existing mount allowlist, and originally running
Docker containers. Reuse mode also compares the OneCLI endpoint, config and
credential inventory. An unexpected change prevents a pass and is reported;
there is no automatic rollback of shared state. Changes made concurrently by
another operator can also trigger this check. NanoClaw may add its normal
`~/.local/bin` PATH entry to shell profiles when configuring OneCLI.

## Results and retained installs

Read the local JSON report's `status`, `requested_commit`, `commit`, `phase`,
`plan`, and `target_run`. A pass requires matching run/commit identities, a
successful installer result with `ping: ok` and `service_type: launchd`, the
new service actually running, and the shared-state comparison passing.

The new checkout contains detailed logs at `logs/e2e/`, the installer's
`logs/e2e/result.json`, and the native driver's `.git/nanoclaw-e2e/result.json`.
Its `.git/nanoclaw-e2e/service.json` records the exact LaunchAgent target and
plist. Keep local reports outside source files and credential paths.

Success and failure retain the checkout, agent, image and installed service.
A lost SSH connection or timeout may leave work running; inspect the target
before retrying, and use a new path rather than adopting an uncertain install.
Only stop/remove a retained install when that cleanup is requested. Shared
Docker and OneCLI components are not owned by the test installation.

LaunchAgents depend on the user's login session. Initial readiness does not
prove recovery after logout, reboot or Docker Desktop restart. Those are
separate tests and are not claimed by this first version.

## Source contracts and validation

The initial implementation follows NanoClaw
[`74224f62a6c08418acccc727114ab02f92e403bf`](https://github.com/nanocoai/nanoclaw/tree/74224f62a6c08418acccc727114ab02f92e403bf):
`setup.sh`, `setup/install-node.sh`, `setup/install-docker.sh`, `setup/onecli.ts`,
`setup/auth.ts`, `setup/mounts.ts`, `setup/service.ts`, `setup/peer-cleanup.ts`,
`setup/verify.ts`, `src/install-slug.ts`, `scripts/upgrade-state.ts`,
`scripts/init-cli-agent.ts`, and `scripts/chat.ts`.

Re-check these contracts when NanoClaw's setup changes. Apple's
[LaunchAgent guide](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
and the target Mac's `launchctl(1)` / `launchd.plist(5)` manuals describe the
service and GUI-session behavior.

Offline regressions cover transport, exact checkouts, existing-path/service
protection, gateway credential preservation, stale results, process timeouts,
Mac service dispatch, and shared-state comparison. Record the exact macOS/CPU,
NanoClaw SHA, local/SSH mode and successful model/service evidence when claiming
live compatibility; a dry run or simulated SSH test is not that evidence.

On 2026-09-11, local read-only preflight passed on macOS 26.6.1 / arm64 with
Docker Desktop running, Node 26 and an existing OneCLI vault, targeting the
NanoClaw commit above. The 86-test offline suite passed, including 24 Mac-specific
tests. Native installation/model inference and real SSH execution have not yet
been qualified with this skill; SSH authentication was unavailable on the
candidate target. No reboot or cold-prerequisite behavior is claimed.
