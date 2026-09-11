---
name: e2e-wizard
description: Test NanoClaw's real interactive setup wizard through a PTY and terminal emulator on a fresh exe.dev VM or Proxmox LXC. Require public-wizard completion, a retained terminal agent's real reply and service verification, with sanitized evidence and retained failures. Use for interactive installer validation; use e2e-exe-dev for headless setup-step testing.
license: MIT
---

# Test the interactive NanoClaw wizard

This skill drives **`bash nanoclaw.sh`**, the public setup entry point. It sends
keys only after the expected active prompt renders in a real PTY, interpreted
by a VT terminal emulator. It does not run setup steps, create agents, start
services, repair databases or seed credentials outside the wizard.

Use it to find failures that the headless installer can hide. The existing
[e2e-exe-dev](../e2e-exe-dev/SKILL.md) remains the headless setup-step test and
owns the shared exe.dev lifecycle. The [e2e-proxmox](../e2e-proxmox/SKILL.md) companion owns LXC creation and retention. Passing one mode does not qualify the other.

## Install and run

The operator needs Bash, Git, OpenSSH, Python 3.10+ and tar, plus an existing
exe.dev connection or root SSH access to Proxmox. The Linux target needs sudo and internet access.

Install this skill with its companion lifecycle driver:

```bash
npx skills add nanocoai/nanoclaw-oss-dev-tools --global --skill e2e-exe-dev
npx skills add nanocoai/nanoclaw-oss-dev-tools --global --skill e2e-wizard
```

All skills also ship in the Claude Code plugin. Keep the installed skill folders
as siblings. Run the lifecycle driver from the **NanoClaw source checkout under
test**, using its absolute installed path:

```bash
E2E_SKILL_DIR=/absolute/path/to/installed/e2e-exe-dev
cd /absolute/path/to/nanoclaw
bash "$E2E_SKILL_DIR/scripts/exe-run.sh" --interactive \
  --ref HEAD --repo https://github.com/nanocoai/nanoclaw.git \
  --result-file /absolute/path/to/results/wizard.json
```

The result's parent directory must already exist. The driver resolves the ref
locally and checks out that exact fetchable commit on a newly owned VM. Local
edits are not uploaded. It reuses existing creation-response validation, SSH
transport, snapshot confirmation and failure retention.

The live run provisions a cloud VM and sends an authorized Anthropic API key or
existing OAuth token to its private file over SSH stdin. Use an existing
credential file, normally `~/.nanoclaw-e2e/anthropic_key`, with mode `0600`;
`--key-file` selects another. Keep the test VM and credential transfer within
the operator's authorized scope. This scenario never opens browser sign-in or
creates an account.

`--interactive` requires `--result-file`. Sanitized evidence goes to
`<result-file>.artifacts`; `--artifacts-dir` chooses another new directory.
`--wizard-timeout` sets the total PTY deadline (default 1200 seconds); the runner
also stops after 180 seconds without output. Unknown prompts fail rather than
choosing a default. Failures and failed exports retain the VM. Successful
removal requires the existing opt-in `--rm` flag and happens after validated
artifact export. The initial fresh scenario rejects `--base` because a cached
installation can skip the setup and authentication being tested.

## Proxmox LXC

Install `e2e-proxmox` alongside `e2e-wizard` and use the
[Proxmox adapter](scripts/proxmox-wizard.py) from the NanoClaw checkout:

```bash
npx skills add nanocoai/nanoclaw-oss-dev-tools --global --skill e2e-proxmox
E2E_WIZARD_DIR=/absolute/path/to/installed/e2e-wizard
python3 "$E2E_WIZARD_DIR/scripts/proxmox-wizard.py" \
  --host root@pve.example.test \
  --template local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst \
  --storage local-lvm --bridge vmbr0 --ref HEAD \
  --result-file /absolute/path/to/results/wizard-proxmox.json
```

Use an existing Debian 13 amd64 template, storage and DHCP bridge. This accepts
[e2e-proxmox's options](../e2e-proxmox/SKILL.md), including `--dry-run`,
`--identity-file` and an unused `--ctid`, plus `--artifacts-dir` and
`--wizard-timeout` (1–2100 seconds). `--installer` is unavailable because the
adapter supplies the public-wizard harness. Both lifecycle modes use the same
scenario, terminal driver and artifact validation.

The adapter reuses unprivileged LXC creation, random ownership markers, a regular
developer account and its systemd user session. It installs only the shared OS
bootstrap and test-harness prerequisites; NanoClaw's public wizard performs all
product setup. Provider and channel presets are not forwarded. Every container
is retained, including successful runs, and existing guests are never adopted.
The top-level report includes `installer` and locally validated `wizard` results.

## Scenario and acceptance

The bundled [fresh-cli scenario](scenarios/fresh-cli.json) selects Standard
setup, a fresh agent and a locally built image. It declines the Echo and Slack
browser offers whenever they appear, pastes the credential using the matching
API-key or OAuth choice, chooses terminal chat to retain an agent, submits a
random arithmetic question, keeps UTC and skips the phone channel. Portal
availability checks may still run; declining an offer does not disable them.

A pass requires all of the following from this invocation:

- Every required prompt and recorded choice, successful required setup steps,
  and the public wizard's completed progression-log footer.
- A computed answer absent from the typed question, received during chat with
  the retained terminal agent. Echoed input, the temporary ping agent, a chat
  subprocess exiting, or a friendly outro is insufficient.
- Final `VERIFY` success, running service, configured credentials, Docker,
  at least one registered group, configured mount allowlist and a local image.
- UTC persisted by the wizard, the intended checkout's current service process
  and a reachable CLI socket. This final socket connection sends no message.

Cancellation, unknown or skipped prompts, timeout, missing proof, failed steps
and ambiguous evidence cannot pass, even if the public process exits zero.
Product failures remain product failures: preserve the evidence and report
them separately. Do not start a missing nohup wrapper, rewire an agent, retry
through an assistant repair offer, or add a setup action behind the wizard.

## Evidence and privacy

[The runner](scripts/wizard-run.py) retains rendered terminal text in memory
and exports sanitized `terminal.txt`, recorded `choices.json`, `result.json`,
setup logs and a checksum manifest. It never exports raw PTY input bytes.
Passwords and long tokens are redacted after complete texts are assembled,
including values split across chunks or wrapped terminal lines.

NanoClaw at the source revision below writes the pasted credential into the
raw auth step's command header. The runner protects the target `logs/` directory
with mode `0700`, reads those logs on the target, and writes separate sanitized
copies before transfer. It also redacts generated gateway credentials from
known local configuration. Original product logs and the imported vault secret
remain on the retained VM; never copy `logs/setup-steps/` directly.

[The collector](scripts/collect-wizard.py) validates archive members, checksums,
run identity, commit, exit status and credential redaction before saving the
local report. A missing or invalid export prevents a local pass and VM removal.
Reports can still contain host paths and application messages; review them
before public sharing.

## Runtime and validation

The harness uses Python 3.10+, `pyte==0.8.2` and `wcwidth==0.2.13` from the
hash-pinned [requirements](requirements.txt). [wizard-install.sh](scripts/wizard-install.sh)
installs only this test dependency in a target venv, adding `python3-venv` if
needed. It does not install or repair NanoClaw prerequisites itself.

For a standalone run on a fresh, disposable Linux checkout with the emulator
already installed:

```bash
python3 /absolute/path/to/e2e-wizard/scripts/wizard-run.py \
  --key-file /absolute/path/to/private/anthropic_key
```

This creates persistent product state and makes real model requests. Use a fresh
exe.dev VM or Proxmox LXC for isolation; the first scenario does not qualify existing Macs
or replace the preservation checks in `e2e-macos`.

Offline regressions cover actual PTYs, terminal redraws, split Unicode/escape
sequences, active selection, cancellation, unknown prompts, missing replies,
timeouts and descendant cleanup, false success records, credential redaction,
unsafe archives, stale results and VM retention. These use fixtures and are not
live wizard qualification.

## Compatibility evidence

On **2026-09-11**, the fresh public wizard passed on a new unprivileged Debian 13
LXC under Proxmox VE 9.2.18, testing draft PR 3767 at the exact commit below.
The wizard built the local image, configured Claude through its OAuth prompt,
completed setup and left one retained terminal agent. That agent answered a
random arithmetic question correctly. Final verification reported success;
the driver independently matched the systemd user service process to the tested
checkout and connected to its CLI socket. The lifecycle and sanitized export
took about 4 minutes 22 seconds, including OS bootstrap.

No product step failed in that run. The service startup printed a nonfatal
missing-`pkttyagent` diagnostic; service identity, inference and final verification
still passed. The test did not supply a product repair. An earlier run stopped
on a driver bug involving a stale spinner diamond; the corrected parser has a
regression and that attempt remained a failure with retained evidence.

The 122 offline regressions passed on Linux and macOS. Those checks qualify
terminal behavior, acceptance rules and simulated lifecycle boundaries. The
interactive exe.dev path and a native macOS wizard install have not been live
qualified by this run. Headless installation evidence remains with its own skills.

## Source contracts

The initial scenario was checked against NanoClaw draft
[PR 3767](https://github.com/nanocoai/nanoclaw/pull/3767), exact commit
[`705c6b9e627ac36a8b4bbc280e5804c6debf9a25`](https://github.com/nanocoai/nanoclaw/commit/705c6b9e627ac36a8b4bbc280e5804c6debf9a25):

- `nanoclaw.sh` is the public bootstrap and writes the initial progression log.
- `setup/auto.ts` defines prompts, deletes the temporary ping agent, creates the
  retained terminal agent and writes completion. Chat failures and failed final
  verification can return without a failing public process exit.
- `setup/lib/bright-select.ts` drains pending input before active prompts;
  `setup/logs.ts` defines step, user-input and completion records.
- `setup/lib/runner.ts` writes raw child command headers, including auth values,
  and treats user cancellation as exit zero.
- `setup/verify.ts`, `setup/service.ts` and `scripts/chat.ts` define final
  verification, service identity and terminal-reply behavior.

The branch includes dev-tools [PR 6](https://github.com/nanocoai/nanoclaw-oss-dev-tools/pull/6).
That readiness correction applies to the headless installer; wizard mode does
not inject its socket-wait ordering into NanoClaw. Recheck prompts and evidence
contracts after product setup changes. A run against another ref records the
exact tested commit; an unknown prompt stops for scenario review.
