---
name: e2e-wizard
description: Test NanoClaw's real interactive setup wizard through a PTY and terminal emulator on a fresh exe.dev VM or Proxmox LXC. Require public-wizard completion, a retained terminal agent's real reply and service verification, with sanitized evidence and retained failures. Use for interactive installer validation; use e2e-exe-dev for headless setup-step testing.
license: MIT
---

# Test the interactive NanoClaw wizard

For each unexpected failure, preserve the test evidence, then follow the companion
[e2e-triage](../e2e-triage/SKILL.md) at a safe checkpoint and include its findings
and next actions in the final E2E report. Research each distinct failure once,
including when another E2E skill delegates here; do not delay active prompts,
change acceptance results or submit public issues/comments without authorization.
Install `e2e-triage` alongside this skill. If its relative link is unavailable,
resolve it by skill name in the agent's installed catalog; if absent, report that
triage was unavailable and continue the authorized test/report without installing
anything implicitly.

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

First resolve the exact source and inspect the picker without checking out or
executing files from another revision:

```bash
COMMIT="$(git rev-parse --verify 'HEAD^{commit}')"
E2E_WIZARD_DIR=/absolute/path/to/installed/e2e-wizard
python3 "$E2E_WIZARD_DIR/scripts/provider-options.py" \
  --root "$PWD" --revision "$COMMIT"
python3 "$E2E_WIZARD_DIR/scripts/provider-options.py" \
  --root "$PWD" --revision "$COMMIT" --provider claude
```

Show the offered provider list and ask the operator which provider to test.
Then show that provider's exact `auth_prompt` and `auth_methods` and ask for the
auth method. Reuse provider/backend choices already supplied by the operator.
For an installable provider, inspect `payload_kind`: `bundled` uses the exact
NanoClaw commit and needs no `--payload-ref`; `branch` needs its single
`nc:copy from-branch:` payload fetched from the owning remote, with that fetched
ref passed as `--payload-ref` to discovery and the lifecycle driver. Record
both `nanoclaw_commit` and `auth_source_commit`; never guess the remote or use
the working tree as provider evidence. Only methods marked `credential-file`
are unattended. The Proxmox and direct wizard entry points support the discovered
Codex `device` and Claude `subscription` methods with `--supervised-human-auth`;
read [Supervised authentication](references/supervised-auth.md) for those runs.
Start a live flow after the operator chooses the method and is ready to complete
it; reuse that authorization rather than asking again for the same handoff.
Other human-login methods or entry points fail preflight. `skip` is invalid for
E2E. Read a credential file only after its matching paste method is chosen.

```bash
E2E_SKILL_DIR=/absolute/path/to/installed/e2e-exe-dev
cd /absolute/path/to/nanoclaw
bash "$E2E_SKILL_DIR/scripts/exe-run.sh" --interactive \
  --ref HEAD --repo https://github.com/nanocoai/nanoclaw.git \
  --provider claude --auth-method api \
  --credential-file /absolute/path/to/private/anthropic-key \
  --result-file /absolute/path/to/results/wizard.json
```

The result's parent directory must already exist. The driver resolves the ref
locally and checks out that exact fetchable commit on a newly owned VM. Local
edits are not uploaded. It reuses existing creation-response validation, SSH
transport, snapshot confirmation and failure retention.

The live run provisions a cloud VM and sends the selected provider credential
to its private file over SSH stdin. Use an existing credential file, normally
`~/.nanoclaw-e2e/anthropic_key` for Claude, with mode `0600`;
`--credential-file` selects another (`--key-file` remains an alias). Key
files follow `~/.nanoclaw-e2e/<provider>_key`: `openrouter_key`,
`deepseek_key`, `openai_key` (Codex `api`), and `anthropic_key` for Claude or
an OpenCode `custom` endpoint with `--opencode-provider anthropic`. The drivers
carry the path only and never print a key. Keep the test VM and credential
transfer within the operator's authorized scope. The unattended exe.dev
scenario never opens browser sign-in or creates an account; the one supervised
exception is Codex device pairing (`--supervised-human-auth`, see
[Supervised authentication](references/supervised-auth.md)).

On a NanoClaw ref with the credential-gateway seam, `--gateway onecli|iron-proxy`
selects the gateway the wizard installs (default `onecli`); see
[Gateway seam](#gateway-seam).

`--interactive` requires `--result-file`. Sanitized evidence goes to
`<result-file>.artifacts`; `--artifacts-dir` chooses another new directory.
`--wizard-timeout` sets the total PTY deadline (default 1200 seconds); unattended
runs also stop after 180 seconds without output. Unknown prompts fail rather than
choosing a default. Failures and failed exports retain the VM. Successful
removal requires the existing opt-in `--rm` flag and happens after validated
artifact export. It also requires a matching run-ownership marker, an inactive
harness, verified absence from the exe.dev JSON inventory, and a local teardown
receipt. The initial fresh scenario rejects `--base` because a cached
installation can skip the setup and authentication being tested.

## OpenCode

OpenCode's backend picker and auth helper are bundled in NanoClaw main. Discover
with `--provider opencode`; `auth_source_commit` must equal `nanoclaw_commit`
for this payload. Automated API-key backends are `openrouter`, `deepseek`,
`custom` and `local`, using an existing private key file and an explicitly
chosen full provider/model ID.

Show the discovered backends and ask the operator to choose one: a hosted
provider from OpenCode's catalog (`openrouter`, `deepseek`) or their own
OpenAI-compatible endpoint (`custom`, `local`). Then ask for the model they
have access to; do not guess account access from catalog membership, and do
not substitute a default endpoint or model. `--auth-method` names the backend
in this flow.

For example, from the NanoClaw checkout under test, with `$COMMIT` resolved and
`$E2E_SKILL_DIR` pointing to the companion e2e-exe-dev skill:

```bash
bash "$E2E_SKILL_DIR/scripts/exe-run.sh" --interactive --ref "$COMMIT" \
  --provider opencode --auth-method openrouter \
  --opencode-model "openrouter/<vendor>/<model>" \
  --credential-file /absolute/path/to/private/openrouter-key \
  --result-file /absolute/path/to/results/opencode.json
```

Replace the model placeholder with an account-accessible model that supports
tools, and quote the full value. The same OpenCode connection flags work
with `proxmox-wizard.py`, `wizard-run.py` and `windows-run.py`. No payload branch
is fetched for bundled OpenCode. The runner uses the public backend, model and
password prompts; it records the selected model, verifies all installed payload
files before auth and at completion, and requires the saved defaults and the
retained session's effective provider to match. The existing computed reply and
service checks still apply. Raw credentials stay out of exported evidence.

### Custom endpoint and API key

For an OpenAI-compatible endpoint, choose `custom`, supply its API base URL
(including `/v1` if required), and use `openai/<model-id>`:

```bash
bash "$E2E_SKILL_DIR/scripts/exe-run.sh" --interactive --ref "$COMMIT" \
  --provider opencode --auth-method custom \
  --opencode-base-url "https://models.example.test/v1" \
  --opencode-model "openai/my-model" \
  --credential-file /absolute/path/to/private/endpoint-key \
  --result-file /absolute/path/to/results/opencode-custom.json
```

`--opencode-provider` defaults to `openai` for `custom`. Set it to `anthropic`,
`google`, `openrouter` or `deepseek` when the endpoint speaks that provider's API;
the model prefix must match. NanoClaw owns the matching key injection format.
`--auth-method local` selects the public Local or self-hosted flow and always
uses the `openai` provider. Both flows require `--opencode-base-url` and explicit `--credential-file`
(`--key-file` is an alias); the legacy default key file is not selected.

The endpoint must be reachable from the test guest and its agent container.
For example, `localhost` addresses the machine/container making the request,
not the operator's workstation. HTTP and HTTPS, private addresses, IPv6 and
custom ports are accepted. Keep credentials out of the URL: userinfo, query
strings and fragments are rejected. The key file must be private (`0600`) and
contain one token of 8–1024 printable characters without internal whitespace.
Symbols such as `+`, `/` and `=` are preserved.

For OpenAI-compatible endpoints, the wizard answers **No** to keyless access
and enters the API key before model discovery. Other supported API formats ask
for the model first. Manual model entry remains available when a catalog cannot
be listed. Acceptance binds the requested URL and API provider to both saved
settings and sanitized evidence, alongside the real reply and service checks.

ChatGPT subscription and keyless endpoint authentication are not automated. OpenCode uses public wizard mode; the headless setup-step driver
remains Claude-only. Offline regressions cover the automated backends; the
custom OpenAI-compatible flow has a fresh exe.dev pass described below.

On **2026-09-15**, a fresh exe.dev VM at NanoClaw
[`1100f83f57e0b61b60efabea3ec4f8360535b7b4`](https://github.com/nanocoai/nanoclaw/commit/1100f83f57e0b61b60efabea3ec4f8360535b7b4)
completed the public wizard with an `openai/<model>` id served by a private,
operator-supplied OpenAI-compatible endpoint. All 37 installed provider files
matched the source;
the retained agent answered `11438 * 71` with `812098`, and final product
verification succeeded. The original E2E result remains **failed** because
its provider verifier could not find `pnpm`: bootstrap installed Node and pnpm
under `~/.local/bin` inside the wizard's child shell.

The verifier now restores that tool search path, including npm's global-prefix
fallback, for its own CLI checks. A separate read-only recheck passed against
the retained installation: unchanged payload, selected endpoint/model, effective
OpenCode provider, live nohup process and connected CLI socket. No NanoClaw
repair or original-result rewrite was performed.

A **second fresh exe.dev VM passed the complete corrected harness** at that
same NanoClaw commit on 2026-09-15. Public wizard completion, OneCLI API-key
authentication, all 37 payload files, saved endpoint/model, the retained agent's
effective OpenCode provider, final product verification, the exact nohup process
and CLI socket all passed. The retained agent answered `15832 * 47` with
`744104`. The wizard took about 5 minutes 57 seconds; 20 sanitized artifacts
passed checksum, run-identity and credential checks. Both attempts were retained.
This qualifies custom OpenAI-compatible OpenCode on exe.dev; other OpenCode
backends and the Proxmox/Windows launchers still have offline-only coverage.

Source contract checked at NanoClaw
[`1100f83f57e0b61b60efabea3ec4f8360535b7b4`](https://github.com/nanocoai/nanoclaw/commit/1100f83f57e0b61b60efabea3ec4f8360535b7b4):
`.claude/skills/add-opencode/SKILL.md` and its payload's
`setup/providers/opencode.ts`, `scripts/opencode-auth.ts` and
`scripts/opencode-model-config.ts`.

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
  --provider claude --auth-method api \
  --credential-file /absolute/path/to/private/anthropic-key \
  --result-file /absolute/path/to/results/wizard-proxmox.json
```

Use an existing Debian 13 amd64 template, storage and DHCP bridge. This accepts
[e2e-proxmox's options](../e2e-proxmox/SKILL.md), including `--dry-run`,
`--identity-file` and an unused `--ctid`, plus `--artifacts-dir` and
`--wizard-timeout` (1–2100 seconds). `--installer` is unavailable because the
adapter supplies the public-wizard harness. Both lifecycle modes use the same
scenario, terminal driver and artifact validation.

For Codex device pairing or Claude subscription sign-in, add
`--supervised-human-auth` and omit `--credential-file`. The private handoff,
authorization-code return path and extra provider proof are described in
[Supervised authentication](references/supervised-auth.md). The adapter transports
an explicitly selected installable-provider payload into the guest and makes
the wizard install it through the normal registry remote. For Proxmox, supply
an explicit remote-tracking ref, such as `refs/remotes/fork/providers` or
`fork/providers`, whose owning remote uses public HTTPS without credentials.
Raw SHAs, local branches, missing refs and ambiguous remote ownership fail
preflight. The guest fetches that owning remote's branch and rejects it if its
SHA changed after selection. It compares copied files with the selected commit
before authentication and again before acceptance.

The adapter reuses unprivileged LXC creation, random ownership markers, a regular
developer account and its systemd user session. It installs only the shared OS
bootstrap and test-harness prerequisites; NanoClaw's public wizard performs all
product setup. Provider and channel presets are not forwarded. Every container
is retained, including successful runs, and existing guests are never adopted.
The top-level report includes `installer` and locally validated `wizard` results.
After those artifacts are durable, offer removal of a disposable run-owned guest
after a pass; recommend retention after an unexpected failure until triage and
inspection complete. Recheck the random CT marker and inactive controller
immediately before separately authorized cleanup, verify absence afterward, and
write a teardown receipt. Never remove when evidence, redaction, checksum, local
persistence, triage, ownership, or controller validation is incomplete.

## Scenario and acceptance

The bundled [fresh-cli scenario](scenarios/fresh-cli.json) selects Standard
setup, a fresh agent and a locally built image. It declines the Echo and Slack
browser offers whenever they appear, completes the selected credential-file or
supported supervised sign-in method, chooses terminal chat to retain an agent, submits a
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

Supervised runs also verify the provider's vault entry and the retained agent's
effective provider through read-only `ncl` queries and the exact installed
`resolveProviderName` implementation. This preserves NanoClaw's null/default
Claude configuration and honors session-level overrides while rejecting an
effective mismatch. Claude subscription auth records
`auth: interactive` in the source contract; that one status is accepted only for
the corresponding supervised method and with all its additional proof.

Cancellation, unknown or skipped prompts, timeout, missing proof, failed steps
and ambiguous evidence cannot pass, even if the public process exits zero.
Product failures remain product failures: preserve the evidence and report
them separately. Do not start a missing nohup wrapper, rewire an agent, retry
through an assistant repair offer, or add a setup action behind the wizard.

This fresh-install scenario does not exercise later registry-backed channel or
provider installs, their restart directives, invalid configuration recovery,
host reboot, or gateway-file recovery after temporary storage disappears. A
passing wizard run cannot qualify those paths; pair their focused regressions
with a live scenario that triggers the actual lifecycle being changed.

## Gateway seam

On refs with NanoClaw's credential-gateway seam (`setup/gateways/`,
[nanocoai/nanoclaw#3815](https://github.com/nanocoai/nanoclaw/pull/3815)
onward) the public wizard installs the gateway through `/add-<gateway>` and
takes the kind from its own `--gateway-provider` flag, the Advanced screen or
`NANOCLAW_GATEWAY_PROVIDER`. `wizard-run.py --gateway onecli|iron-proxy`
(forwarded by `exe-run.sh --interactive --gateway`) detects the seam by
`setup/gateways/step.ts`, runs `bash nanoclaw.sh --gateway-provider <kind>`,
and adapts the bundled scenario: the `onecli` runner step no longer exists, and
`auth` is required only for providers whose own hook logs it (OpenCode and
Codex do; Claude's gateway auth script does not). Requesting `iron-proxy` on a
ref without the seam fails preflight; older refs only ever install OneCLI.
Claude itself is not yet discoverable on a seam ref (its picker moved into
the gateway skills' own `scripts/auth.ts`, which `provider-options.py` does
not read), so `--provider claude` with `--gateway` stays on the headless
[e2e-exe-dev](../e2e-exe-dev/SKILL.md#refs-on-the-gateway-seam) driver; the
wizard route covers the providers that expose their own picker (OpenCode,
Codex).

After the wizard, the runner binds the gateway the way the headless installer
does: `gateway` is the `NANOCLAW_GATEWAY_PROVIDER` stamp `installGateway` wrote
to `.env` (null until proven), `requested_gateway` the request, `gateway_seam`
whether the seam was present, and the running service's environment must not
select another kind. A mismatch fails the run with the found kind recorded, and
the collector rejects a pass whose `gateway` differs from the driver's
`--gateway` (or Iron without the seam) with `gateway-mismatch`. Iron Control's
environment files join the redaction set.

Under Iron, Codex device pairing is proven through the Iron adapter instead of
a OneCLI listing: the runner reads the adapter's non-secret metadata (one
token broker plus two static secrets) and runs the installed
`add-iron-proxy` credential store's `has('codex')`, which re-checks isolation
and broker health through Iron Control; values are never read. OpenCode runs
under Iron also record whether an agent container carried the read-only
`gateway-trust` CA mount. Claude subscription proof still reads the OneCLI
vault, so that method is refused with `--gateway iron-proxy`. The Proxmox
wizard adapter does not forward a gateway yet and refuses one rather than
ignoring it; the headless [e2e-proxmox](../e2e-proxmox/SKILL.md#choose-the-credential-gateway)
driver covers Iron there.

Cells C and D of the 2026-09-22 gateway-stack e2e (nanoclaw `290aa683`,
#3825) ran through this route; their evidence is recorded in the compatibility
section below: C passed on its second attempt (an OpenAI-compatible HTTPS
endpoint with an API key; the first attempt used the shared `anthropic_key`,
a Claude Code OAuth token, which Iron swapped into `X-Api-Key` correctly and
Anthropic rejected), and D needs a person at the browser within the pairing
window.

## Evidence and privacy

[The runner](scripts/wizard-run.py) retains rendered terminal text in memory
and exports sanitized `terminal.txt`, recorded `choices.json`, `result.json`,
setup logs, `logs/nanoclaw.log`, `logs/nanoclaw.error.log`, a bounded
`docker ps -a` status snapshot, and a checksum manifest. It never exports raw
PTY input bytes. Passwords and long tokens are redacted after complete texts
are assembled, including values split across chunks or wrapped terminal lines.
Supervised handoff files stay outside that export; authorization URLs/codes and
generated subscription tokens are redacted from terminal and log evidence.

NanoClaw at the source revision below writes the pasted credential into the
raw auth step's command header. The runner protects the target `logs/` directory
with mode `0700`, reads those logs on the target, and writes separate sanitized
copies before transfer. It also redacts generated gateway credentials from
known local configuration. Original product logs and the imported vault secret
remain on the retained VM; never copy `logs/setup-steps/` directly.
Container log bodies also remain on the retained private target because they
can include arbitrary application or model content. The exported status snapshot
records container identity, image, state and status without those log bodies.

[The collector](scripts/collect-wizard.py) validates archive members, checksums,
run identity, NanoClaw commit, provider, auth method, provider auth-source commit,
exit status and credential redaction before saving the
local report. A missing or invalid export prevents a local pass and VM removal.
Reports can still contain host paths and application messages; review them
before public sharing.

Collector failures print a safe diagnostic code without echoing remote archive
content. Use the code to choose the next inspection:

| Code | Inspect or correct |
|---|---|
| `destination-exists` | Choose a new local artifact directory; the collector does not adopt or replace one. |
| `credential-unreadable`, `credential-empty` | Correct the local authorized key path and its contents. |
| `archive-too-large`, `expanded-artifacts-too-large` | Inspect the retained target for an unexpectedly large log or archive. |
| `invalid-tar-archive`, `unsafe-archive-member`, `unexpected-artifact-path` | Treat the export as invalid and inspect the target-side artifact directory and transfer boundary. |
| `invalid-manifest`, `incomplete-manifest`, `checksum-mismatch` | Re-export from the retained target; do not accept a partial or changed archive. |
| `artifact-identity-mismatch`, `invocation-mismatch` | Compare the expected run ID, commit and exit code with the retained result. |
| `invalid-artifact-text`, `invalid-result`, `acceptance-evidence-missing` | Inspect the target's sanitized result and required proof files. |
| `credential-found` | Keep the remote export private and inspect the redaction inputs; no local pass is written. |

## Runtime and validation

The harness uses Python 3.10+, `pyte==0.8.2` and `wcwidth==0.2.13` from the
hash-pinned [requirements](requirements.txt). [wizard-install.sh](scripts/wizard-install.sh)
installs only this test dependency in a target venv, adding `python3-venv` if
needed. It does not install or repair NanoClaw prerequisites itself.

For a standalone run on a fresh, disposable Linux checkout with the emulator
already installed:

```bash
python3 /absolute/path/to/e2e-wizard/scripts/wizard-run.py \
  --provider claude --auth-method api \
  --credential-file /absolute/path/to/private/anthropic_key
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
LXC under Proxmox VE 9.2.18, testing an earlier candidate of PR 3767 at the exact
commit below.
The wizard built the local image, configured Claude through its OAuth prompt,
completed setup and left one retained terminal agent. That agent answered a
random arithmetic question correctly. Final verification reported success;
the driver independently matched the systemd user service process to the tested
checkout and connected to its CLI socket. The lifecycle and sanitized export
took about 4 minutes 22 seconds, including OS bootstrap.

Merged [PR 3767](https://github.com/nanocoai/nanoclaw/pull/3767) later added
explicit registry tracking refs and directory-collision handling. Those follow-up
paths were outside this run and require their own focused and live evidence.

No product step failed in that run. The service startup printed a nonfatal
missing-`pkttyagent` diagnostic; service identity, inference and final verification
still passed. The test did not supply a product repair. An earlier run stopped
on a driver bug involving a stale spinner diamond; the corrected parser has a
regression and that attempt remained a failure with retained evidence.

On **2026-09-12**, another fresh Ubuntu wizard run exercised NanoClaw
[PR 3768](https://github.com/nanocoai/nanoclaw/pull/3768) at commit
[`8d75571abfcf2486ceeef9b302ddc21ca69955b1`](https://github.com/nanocoai/nanoclaw/commit/8d75571abfcf2486ceeef9b302ddc21ca69955b1).
The retained agent returned the expected computed answer, and the nohup host
kept the same PID and served an admin request after the wizard terminal exited.
That run used a task-only adapter for nohup proof. This harness now performs the
same owned launcher, PID, exact-entrypoint and socket checks directly, with
offline regression coverage. The merged PR later added post-install restart
handling for channel skills; that separate path was not part of the live run.

On **2026-09-22** the gateway-forwarding route ran cells C and D of the
gateway-stack e2e on fresh exe.dev VMs against nanoclaw
`290aa68358f7813e2505d48bcc132f50720f2cca` (#3825) with `--gateway iron-proxy`:

- **C, OpenCode `custom` (`--opencode-provider anthropic`,
  `https://api.anthropic.com/v1`, `anthropic/claude-haiku-4-5-20251001`)**:
  the seam scenario held (`bootstrap → environment → container → auth → mounts
  → service → cli-agent → cleanup-cli-agent → create-terminal-agent` all
  `success`, `.env` stamped `NANOCLAW_GATEWAY_PROVIDER=iron-proxy`, the agent
  container carried `data/gateway-trust/iron-proxy/ca.crt` read-only), Iron's
  audit shows the secrets transform swapping `header:X-Api-Key` on
  `POST /v1/messages`, and Anthropic answered `401 API key is invalid`: the
  shared credential file is a Claude Code OAuth token, not an API key. The
  wizard's own ping accepted the "agent run failed" reply; the retained-agent
  arithmetic check caught it (`phase: reply`, exit 2). Failed, VM
  `nc-stack-c-iron-opencode` retained, sanitized evidence collected.
- **C, second attempt: OpenCode `custom` on an OpenAI-compatible endpoint
  (`https://api.nan.builders/v1`, `openai/glm5.3`, API key file)**: all
  eleven steps `success` in 8 min 15 s on a fresh VM, `gateway: iron-proxy`
  bound from the `.env` stamp, `gateway_trust_mount_observed: true`, the
  retained OpenCode agent answered the arithmetic challenge, the collector
  accepted the archive and `--rm` deleted the VM with a teardown receipt.
  **Pass**: OpenCode through Iron's front proxy to an HTTPS-on-443 model host
  ([nanocoai/nanoclaw#3825](https://github.com/nanocoai/nanoclaw/pull/3825)).
- **D, Codex `device` with `--supervised-human-auth`**: Iron installed, the
  Codex payload (19 files from `providers` @ `a1148a8f`, 3 bundled) installed
  and the image rebuilt, the login reached the device prompt and the driver
  relayed the request privately; nobody was at the browser and the runner
  stopped after its 10-minute handoff window (`phase: timeout`, exit 124).
  exe.dev images already ship a Codex CLI (`/usr/local/bin/codex` 0.155.1),
  so the pinned-CLI install did not run. Failed, VM retained.
- **D, second and third attempts, paired**: the code was entered in time
  (host Codex 0.155.1, then the driver-installed 0.146.0 pin), Codex printed
  `Successfully logged in`, and the payload's `store.save('codex')` failed
  before any Iron Control request ("Couldn't save your Codex credentials to
  the vault"). Root cause, reproduced in one process on the VM: the wizard
  loads `src/provider-contracts` before the Codex payload appends
  `import './codex.js'` to that barrel, so the ESM-cached registry never
  gains Codex's `modelEndpoints` and Iron's adapter throws `Provider codex
  does not declare its subscription endpoint` (the payload swallows the
  message). The same adapter saved both a synthetic and the real 0.146.0
  login file from a fresh process. This is a nanoclaw defect at `290aa683`
  for "install Codex and pair under Iron in one public-wizard run"; OneCLI
  stores the login verbatim and never asks for the endpoint, which is why
  the 09-13 Proxmox pass did not see it. **Failed, product**, VM
  `nc-stack-d-iron-codex` retained. The runner now always pairs with the
  skill's pinned CLI so the login file matches the payload.

Both drivers' SSH sessions stayed open after the guest wizard had exited,
with nothing on the VM holding them; the driver now stops waiting once the
guest's terminal result exists (see `wait_for_wizard` in `exe-run.sh`).

The offline suite runs on Linux and macOS. Those checks qualify
terminal behavior, acceptance rules and simulated lifecycle boundaries. The
distributed interactive exe.dev path has the OpenCode custom-endpoint pass
above. A native macOS wizard install remains unqualified. Headless installation
evidence remains with its own skills.

## Source contracts

The initial scenario was checked against an earlier candidate of NanoClaw
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
