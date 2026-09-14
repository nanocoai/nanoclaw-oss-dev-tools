---
name: e2e-windows
description: Test NanoClaw's public setup wizard in a fresh Windows WSL2 distribution using the local Docker Desktop engine. Verify Linux bind mounts, a retained agent's real reply and the service, while preserving sanitized evidence. Includes a Proxmox Windows preparation reference.
license: MIT
---

# Test NanoClaw on Windows through WSL2

For each unexpected failure, preserve the test evidence, then follow the companion
[e2e-triage](../e2e-triage/SKILL.md) at a safe checkpoint and include its findings
and next actions in the final E2E report. Research each distinct failure once,
including when another E2E skill delegates here; do not delay active prompts,
change acceptance results or submit public issues/comments without authorization.
Install `e2e-triage` alongside this skill. If its relative link is unavailable,
resolve it by skill name in the agent's installed catalog; if absent, report that
triage was unavailable and continue the authorized test/report without installing
anything implicitly.

Run this skill inside a **fresh, disposable WSL2 Linux distribution**, as its
regular Linux user, from the NanoClaw checkout under test. Windows owns Docker
Desktop; Ubuntu reaches its Linux engine through Docker Desktop's WSL integration.
The checkout, home and test evidence belong on the Linux filesystem.

Use the [Windows preparation reference](references/windows-proxmox.md) when a
Windows VM, WSL distribution or Docker integration still needs preparation.
The runner does not create VMs, change Windows settings, import credentials,
or install another Docker daemon. A Windows template is separate from a WSL
distribution containing an installed NanoClaw.

## Prepare and run

Install this skill and [e2e-wizard](../e2e-wizard/SKILL.md) as sibling folders.
The latter supplies the unchanged PTY driver, scenario and artifact validator.
Keep the skill installation outside the NanoClaw checkout under test.

The Linux distribution needs Git, Python 3.10+, venv, Bash, sudo, build tools,
an active systemd user session and Windows interoperability (`powershell.exe`).
Docker Desktop must already be running in the Windows account that owns this
distribution, with integration enabled for that distribution. Use an authorized
provider credential in a private Linux file, normally
`~/.nanoclaw-e2e/anthropic_key`, owned by the test user with mode `0600`.

Before a full wizard run reads that file or starts setup, resolve the exact
commit and inspect the shared public picker's provider choices:

```bash
COMMIT="$(git rev-parse --verify 'HEAD^{commit}')"
PROVIDER_HELPER=/absolute/path/to/installed/e2e-wizard/scripts/provider-options.py
python3 "$PROVIDER_HELPER" --root "$PWD" --revision "$COMMIT"
python3 "$PROVIDER_HELPER" --root "$PWD" --revision "$COMMIT" --provider claude
```

Show the first result to the operator and ask for a provider, then show the
selected provider's exact auth prompt/options and ask for an auth method. For
an installable provider, fetch its one `nc:copy from-branch:` payload from its
owning remote and pass that fetched ref as `--payload-ref` to discovery and the
runner. Record the NanoClaw and auth-source SHAs. The unattended WSL wizard can
use only a discovered `credential-file` method, such as Claude `api`/`oauth` or
Codex `api`. Browser, subscription, and device methods require a separately
authorized live handoff and stop before setup here. `skip` cannot pass. Read the
credential only after its matching method is selected.

From a clean NanoClaw checkout on Linux ext4:

```bash
E2E_WINDOWS_DIR=/absolute/path/to/installed/e2e-windows
mkdir -p "$HOME/e2e-results"
python3 "$E2E_WINDOWS_DIR/scripts/windows-run.py" \
  --ref HEAD --provider claude --auth-method api \
  --credential-file "$HOME/.nanoclaw-e2e/anthropic_key" \
  --result-file "$HOME/e2e-results/windows.json"
```

`--ref` resolves to an exact local commit and must match the checkout's HEAD.
Tracked changes and existing product state are rejected. The runner does not
fetch, switch branches or copy local edits. `--root` selects another checkout;
`--key-file` selects another private credential file. The result and artifact
parent directories must already exist.

To qualify just the environment, without reading a credential or starting
NanoClaw:

```bash
python3 "$E2E_WINDOWS_DIR/scripts/windows-run.py" \
  --preflight-only --result-file "$HOME/e2e-results/windows-environment.json"
```

This runs a small disposable Alpine container with a read-only Linux-home mount
and may pull its image. Its result is **`qualified`**, never a NanoClaw `pass`.
It can inspect an existing checkout without running the fresh-install scenario.

## Required proof

Before invoking the public wizard, the runner verifies:

- Actual WSL2 Linux execution as a regular user, ext4 paths, systemd and the
  user's active service session.
- The Linux Docker context uses `/var/run/docker.sock`, with no Docker endpoint
  environment overrides or separate Linux `dockerd` executable.
- The Windows CLI reaches Docker Desktop through its local named pipe, and
  the Linux client reports the same engine identity and local WSL2 kernel.
- A container reads a unique marker bind-mounted from the Linux home directory.

It then drives **`bash nanoclaw.sh`** through the shared wizard harness. NanoClaw
performs its own Node/pnpm bootstrap, local agent image build, authentication,
agent creation and service setup. Do not preinstall Node or patch PATH to conceal
a public bootstrap failure in a fresh test.

A `pass` requires the shared scenario's completed public wizard, recorded
choices, a computed answer from the retained terminal agent, final `VERIFY`
success, the tested checkout's service process and a reachable CLI socket.
The shared collector also validates the sanitized manifest, exact commit,
run identity, exit code and credential redaction before the Windows report can pass.

`--wizard-timeout` sets the total PTY deadline (default 1200 seconds, range
1–2100); the shared driver also enforces its idle timeout. Unknown prompts,
product failures, cancellation and invalid exports remain failures. Preserve
the distribution and evidence; do not inject setup repairs behind the wizard.

## Evidence and retention

The top-level result combines Windows/WSL/Docker evidence with the validated
wizard result. Sanitized evidence is written to `<result-file>.artifacts`;
`--artifacts-dir` selects another new directory. Raw product logs and credentials
stay in the retained Linux distribution. The runner's private working directory
is also retained and named in the report. Copy only the collector-validated
artifact directory and final report when sharing results.

No VM, distribution, product container or service is removed. The temporary
bind-test container uses `--rm`; its image can remain cached. Environment
qualification does not establish Docker Desktop support for the underlying
hypervisor, and a generalized Windows Evaluation template retains its licensing
limits.

After validated artifacts and the report are stored outside the distribution,
offer cleanup for a pass only when the operator identifies the WSL distribution
or enclosing VM as a disposable clone owned by this run. Recommend retaining an
unexpected failure until triage and requested inspection finish. Before cleanup,
recheck clone ownership and that no wizard/controller is active; never infer
ownership from a distribution name. If evidence export, redaction, checksums,
local persistence, triage, ownership, or controller checks fail, do not remove
anything. Verify the selected distribution or VM is absent afterward and write
a teardown receipt beside the preserved report. Cleanup never includes the
Windows host, Docker Desktop, templates, or unrelated WSL distributions.

## Source and compatibility

The initial Windows qualification uses NanoClaw
[`705c6b9e627ac36a8b4bbc280e5804c6debf9a25`](https://github.com/nanocoai/nanoclaw/commit/705c6b9e627ac36a8b4bbc280e5804c6debf9a25),
the same public-wizard contract as `e2e-wizard`: `nanoclaw.sh`, `setup/auto.ts`,
`setup/logs.ts`, `setup/verify.ts`, `setup/service.ts` and `scripts/chat.ts`.
Recheck the shared scenario when those source contracts change.

On 2026-09-11, Windows 11 Enterprise Evaluation 25H2 on Proxmox VE 9.2.18
executed Ubuntu 24.04.4 under WSL 2.7.13. Docker Desktop 4.90.0 / Docker 29.7.2
passed both a Windows `hello-world` run and a regular-user WSL Linux-home
bind-mount test against the same local engine. A clean Windows template was
generalized and cloned; first boot still required one Windows recovery-screen
confirmation. The unchanged public wizard then passed in about 10 minutes: bootstrap and
local image build completed, the retained agent answered `42263 * 38` with
`1605994`, final verification succeeded, and the systemd user service and CLI
socket were independently verified. No product step failed or was repaired.
The complete sanitized export passed local identity, checksum, credential and
acceptance validation. The packaged adapter's `--preflight-only` mode also passed against this real
Windows/WSL engine. Its orchestration and export failure cases have offline
coverage; these checks do not imply a second fresh Windows installation.

The installed service survived the setup terminal closing. A separate
`wsl --terminate Ubuntu-24.04` / normal-session restart test failed: the Docker
socket was absent and the enabled NanoClaw service repeatedly exited. Preserve
that result separately from the fresh installation pass. After a separate Windows reboot, Docker Desktop did not start automatically
within 109 seconds. Launching it normally restored the Docker socket, the
existing NanoClaw service and CLI socket without a product repair. However,
a new request to the retained agent timed out (CLI exit 3): model inference
after reboot is **not qualified**. The packaged environment preflight passed
in that state, demonstrating why its `qualified` status is separate from an
installation or inference pass. Keep the failed recovery evidence. Redacted logs and path inspection identified
an empty root-owned directory at `/tmp/onecli-proxy-ca.pem`; SDK 2.2.1 raises
`EISDIR` while writing the certificate. OneCLI and PostgreSQL were healthy.
This is related to [issue 2513](https://github.com/nanocoai/nanoclaw/issues/2513),
which reports a CA bind-mount directory problem on Colima. The directory-creation
mechanism in this Windows run remains unproven.

A release requalification on 2026-09-14 used current NanoClaw
[`3f9ed607b7e7a4872747295f75286f1c377d7c33`](https://github.com/nanocoai/nanoclaw/commit/3f9ed607b7e7a4872747295f75286f1c377d7c33)
and dev-tools candidate
[`075f74285d8e0cc3f54a84e7012d0d09c1e36646`](https://github.com/nanocoai/nanoclaw-oss-dev-tools/commit/075f74285d8e0cc3f54a84e7012d0d09c1e36646).
On a fresh clone of the same Windows template, Ubuntu 24.04.4 ran under WSL
2.7.14 with the same kernel and Docker versions. The unchanged public wizard
passed in about 10 minutes using Claude OAuth; its retained agent replied, final
verification succeeded, and the systemd user service and CLI socket were
independently verified. No product step failed or was repaired. The fresh
environment and a separately repeated post-reboot environment both passed the
packaged `--preflight-only` qualification. The complete sanitized export was
checksum-matched and revalidated locally into 20 artifacts by the exact candidate
collector.

The retained agent also replied after the setup terminal closed. Terminating
only `Ubuntu-24.04` still left its service/socket unavailable after 60 seconds,
with no repair applied. A later full Windows reboot logged the test account in,
but Docker Desktop had not started 132 seconds after boot. Launching Docker
Desktop normally restored the same engine ID, passed a Windows `hello-world`,
restored WSL integration and the existing service/socket, and the retained agent
returned a fresh exact reply. This qualifies manual full-reboot recovery for the
tested configuration. WSL-only recovery and automatic Docker Desktop startup
remain unqualified.
