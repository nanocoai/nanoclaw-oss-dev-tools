---
name: e2e-exe-dev
description: "Provision an exe.dev VM and run a headless NanoClaw install on it end to end — Node/pnpm, Docker, OneCLI, vault secret, agent image, service, a cli-channel agent — then assert the same ping round-trip the setup wizard uses. Use to test a branch or PR on a real machine, to reproduce an install failure, or to bake a reusable base VM. Triggers on \"e2e test on exe.dev\", \"install nanoclaw on a vm\", \"headless install\", \"test this branch end to end\", \"exe.dev\"."
license: MIT
compatibility: Requires bash, git, ssh and python3 on the operator machine; the target is a Debian/Ubuntu host with sudo, git and python3. Docker is installed by setup if missing. An exe.dev account is needed only for the bundled driver.
---

# e2e on exe.dev

For each unexpected failure, preserve the test evidence, then follow the companion
[e2e-triage](../e2e-triage/SKILL.md) at a safe checkpoint and include its findings
and next actions in the final E2E report. Research each distinct failure once,
including when another E2E skill delegates here; do not delay active prompts,
change acceptance results or submit public issues/comments without authorization.
Install `e2e-triage` alongside this skill. If its relative link is unavailable,
resolve it by skill name in the agent's installed catalog; if absent, report that
triage was unavailable and continue the authorized test/report without installing
anything implicitly.

For an installation on an existing Mac, locally or over SSH, use
[`e2e-macos`](../e2e-macos/SKILL.md). This skill's target workflow is Linux;
its installer is shared with the Mac driver and its LaunchAgent helper.

For the real public interactive installer, use the companion
[e2e-wizard](../e2e-wizard/SKILL.md) and this driver's `--interactive` mode.
The workflow below remains the headless test; its setup repairs and acceptance
results do not qualify the wizard.

Installs NanoClaw on a disposable exe.dev VM without a human at the keyboard
and proves the whole loop: message in over the cli channel → host → container
→ Claude → reply out. It reuses the setup wizard's own step processes
(`pnpm exec tsx setup/index.ts --step …`) in the wizard's order — no parallel
install logic to drift — and replaces each prompt with an env var or a
pre-seeded state. The assertion is the wizard's first-chat probe
(`setup/lib/agent-ping.ts`): `pnpm run chat ping` must return a reply.

Ships in [nanocoai/nanoclaw-oss-dev-tools](https://github.com/nanocoai/nanoclaw-oss-dev-tools)
as `skills/e2e-exe-dev`, in the portable [Agent Skills](https://agentskills.io)
format. Install it into any agent with `npx skills add nanocoai/nanoclaw-oss-dev-tools`,
or in Claude Code with `/plugin install nanoclaw-e2e@nanoclaw-oss-dev-tools` (there it
is invoked as `/nanoclaw-e2e:e2e-exe-dev`). In Codex, invoke `$e2e-exe-dev`.
Resolve the scripts relative to this `SKILL.md`, then run them **from the root
of the NanoClaw checkout you want tested**. The installed skill and the
checkout being tested are separate directories. Install `e2e-wizard` alongside
this skill; its shared read-only helper discovers the exact provider picker.

Three scripts under `scripts/`:

| Script | Runs on | Does |
|---|---|---|
| `exe-run.sh` | your machine | `ssh exe.dev new --json` (or `cp` from a base VM), pushes the selected credential and installer over SSH, clones the ref, runs the installer, exports evidence, optionally snapshots (`--snapshot`) or performs guarded deletion on pass (`--rm`) |
| `e2e-install.sh` | the VM (or any Debian/Ubuntu box, or a CI runner) | the headless install + ping; exits 0 on pass |
| `e2e-evidence.py` | VM and operator machine | creates, redacts and validates the headless evidence bundle before any requested teardown |

## When to use

- A branch or PR touches setup, the container image, OneCLI wiring, the
  service, or the router/delivery path and unit tests can't prove it.
- Someone reports an install failure you want to reproduce on a clean machine.
- You want a base VM with Docker, OneCLI and the built image already in place
  so later runs skip the 3–10 minute build.

Not for: wiring real channels (Slack, Telegram, …). The e2e agent talks over
the always-on `cli` channel (`src/channels/cli.ts`) so no platform credentials
are involved. Use `/manage-channels` on the VM afterwards if you want more.

## Prerequisites

- An exe.dev account with `ssh exe.dev` working from your machine. exe.dev
  has two SSH destinations: `ssh exe.dev <cmd>` is the **lobby** (VM
  lifecycle only — no shell, no scp) and `ssh <ssh_dest>` is the **VM**
  (full shell). Pin the key for both so non-interactive runs never stall on
  key selection:

  ```
  Host exe.dev *.exe.xyz
    IdentitiesOnly yes
    IdentityFile ~/.ssh/id_ed25519
  ```

  exe.dev publishes its own agent skill (`using-exe-dev`, in
  `skill/SKILL.md` of github.com/boldsoftware/exe.dev) and docs at
  https://exe.dev/docs.md (index) / https://exe.dev/docs/all.md (one page).
  `ssh exe.dev help <command>` is the authoritative flag reference; this
  skill only adds the NanoClaw side.
- For the headless Claude path, an Anthropic API key or OAuth token in a local file, default
  `~/.nanoclaw-e2e/anthropic_key`. It goes to the VM over stdin into a
  `0600` file; the installer seeds it into the OneCLI vault exactly as
  `setup/auth.ts` does (`onecli secrets create --type anthropic
  --host-pattern api.anthropic.com`).
- A Debian/Ubuntu image on the VM (exe.dev's default). Both bootstrap
  helpers are apt-based: `setup/install-node.sh` (NodeSource) and
  `setup/install-docker.sh` (`get.docker.com` + `usermod -aG docker`).
  Docker runs inside exe.dev VMs.

## Choose the provider and authentication first

Resolve the exact NanoClaw commit and inspect its provider picker before reading
a credential or provisioning anything. The discovery helper belongs to the
sibling `e2e-wizard` skill and reads Git objects, so uncommitted files cannot
change the choices:

```bash
COMMIT="$(git rev-parse --verify HEAD^{commit})"
PROVIDER_HELPER=/absolute/path/to/installed/e2e-wizard/scripts/provider-options.py
python3 "$PROVIDER_HELPER" --root "$PWD" --revision "$COMMIT"
```

Show the reported `providers` to the operator and ask which provider to test.
Then inspect that provider and show its exact `auth_prompt` and `auth_methods`:

```bash
python3 "$PROVIDER_HELPER" --root "$PWD" --revision "$COMMIT" --provider claude
```

For a bundled provider (`payload_kind: bundled`), use the NanoClaw SHA directly
without `--payload-ref`. For a branch-owned provider, first fetch the one `nc:copy from-branch:` payload
named by its offered skill from the owning remote, then pass that fetched ref as
`--payload-ref`. Record both `nanoclaw_commit` and `auth_source_commit`. Never
guess a payload remote or silently choose a provider or auth method.

The unattended headless installer currently supports Claude `api` and `oauth`.
Its `existing` mode is only for an explicitly selected reused gateway whose
vault already has a usable Anthropic credential; it is not a public-picker
choice. The public wizard can also automate credential-file methods exposed by
other offered providers. OpenCode runs require `--interactive` and
`--opencode-model`; custom/self-hosted endpoints also use `--opencode-base-url`
and an API-key file. Follow [OpenCode wizard coverage](../e2e-wizard/SKILL.md#opencode).
Browser, subscription, or device methods require a
separately authorized live human handoff; these drivers stop before allocation
because they cannot complete that handoff unattended. `skip` cannot produce an
E2E pass. Read a credential only after the operator chooses its matching method.

## Workflow

1. **Run it** from the checkout root, on the branch you want tested. Set
   `E2E_SKILL_DIR` to the actual directory containing this `SKILL.md`:

   ```bash
   E2E_SKILL_DIR=/absolute/path/to/installed/e2e-exe-dev
   cd /absolute/path/to/nanoclaw
   bash "$E2E_SKILL_DIR/scripts/exe-run.sh" \
     --provider claude --auth-method api --credential-file /path/to/anthropic-key \
     --result-file /path/to/results/exe-headless.json
   bash "$E2E_SKILL_DIR/scripts/exe-run.sh" --ref origin/main --name nc-main \
     --provider claude --auth-method oauth --credential-file /path/to/anthropic-oauth-token \
     --result-file /path/to/results/exe-main.json
   bash "$E2E_SKILL_DIR/scripts/exe-run.sh" --ref feat/iron --gateway iron-proxy \
     --provider claude --auth-method oauth --credential-file /path/to/anthropic-oauth-token \
     --result-file /path/to/results/exe-iron.json
   ```

   `--gateway onecli|iron-proxy` picks the gateway on a seam ref ([below](#refs-on-the-gateway-seam));
   an unknown value stops before any VM work; `--interactive` rejects it.

   Keep `--result-file` on every live run so the report and sanitized evidence
   exist before the post-run retention choice. For an already authorized
   disposable run, add `--rm` (the result parent directory must exist).
   `--rm` is rejected without `--result-file`; `<result-file>.artifacts` must be
   a new local path so teardown cannot precede validated evidence persistence.

   `--ref` resolves in the local checkout to an exact commit before creating
   a VM. Fetch locally first if you want an updated `origin/main`. The VM
   fetches that SHA from `--repo` (default: local `origin`), checks it out
   detached, and verifies `HEAD` matches. The commit must be fetchable from
   that repository; local-only commits and uncommitted edits are not uploaded.
   The installer comes from the installed skill, so compatible older NanoClaw
   refs can be tested too.

2. **Read the result.** The last thing printed on success is

   ```
   === NANOCLAW E2E: RESULT ===
   STATUS: pass
   COMMIT: <full tested SHA>
   SERVICE_TYPE: systemd-user | systemd-system | nohup
   PING: ok
   REPLY: <first 200 chars of the agent's reply>
   RESULT: logs/e2e/result.json
   === END ===
   ```

   Exit codes: `0` pass · `1` a step failed (its `=== NANOCLAW SETUP: … ===`
   block is printed right above the failure; raw output in
   `~/nanoclaw/logs/e2e/<step>.log` on the VM) · `2` no reply / auth error ·
   `3` the host never opened `data/cli.sock` or the socket was unreachable.
   The driver uses `64` for invalid arguments, `65` for checkout/ref errors,
   `66` for an unreadable key, `69` for unconfirmed VM creation/reachability,
   `70` for unconfirmed snapshot creation, and `74` for local result writing/export failure;
   other command failures can return their own nonzero exit code.

   After installer preflight, `logs/e2e/result.json` records pass/failure,
   exit code, tested commit, tracked changes present at start, phase, service
   type, ping classification, timestamps, the installed gateway (`gateway`),
   the requested one (`requested_gateway`) and whether the seam's gateway
   step ran (`gateway_seam`); the summary block prints a `GATEWAY:` line. An in-progress run has status
   `running`. It replaces a previous run's result and contains no credentials
   or reply text. With `--result-file`, after argument parsing and destination
   validation the driver replaces any old local report with `running` before
   checking the checkout, credentials or VM. It exports a matching completed
   installer result and a sanitized evidence directory before snapshot/removal.
   The evidence binds the run ID, NanoClaw SHA, provider, auth method, provider
   auth-source SHA, gateway kind, dev-tools SHA when available, and exact
   harness digest; an installer result naming a different gateway fails the
   export and keeps the VM. A driver-only failure report has a null `gateway`. It
   includes checksummed setup/runtime logs and a service/container/socket state
   snapshot. Otherwise the driver records the driver
   failure, exit code, phase and requested ref/commit; `commit` is null because
   no tested revision was confirmed. An interrupted run may remain `running`.
   A failed export keeps the VM. Once exported, the installer result describes
   the test; snapshot/removal errors are reported by the driver's exit code.

   On the VM, a checkout failure on a base leaves a `running` record with phase
   `checkout` and the requested commit, replacing any old pass. Failures before
   installer preflight on a fresh VM may have no remote result file; the local
   driver report still records the failure.

3. **Bake a base VM** once the run is green, then clone it for every later run:

   ```bash
   bash "$E2E_SKILL_DIR/scripts/exe-run.sh" --snapshot my-nanoclaw-e2e-base \
     --provider claude --auth-method api --credential-file /path/to/anthropic-key \
     --result-file /path/to/results/base.json
   bash "$E2E_SKILL_DIR/scripts/exe-run.sh" --base my-nanoclaw-e2e-base --ref my-branch \
     --provider claude --auth-method api --credential-file /path/to/anthropic-key \
     --result-file /path/to/results/cached.json
   ```

   VM names are global across exe.dev (they become `<name>.exe.xyz`), so
   pick a distinctive snapshot name. Default run names include a random
   suffix. A taken name or invalid create/copy response stops the run; an
   existing VM is never adopted as a replacement. A failed snapshot keeps
   the tested VM, even with `--rm`.

   Every step is idempotent against the cloned state: `onecli --reuse` when
   `.env` already has `ONECLI_URL`, `auth --check` short-circuits on an
   existing vault secret, `container` rebuilds only what changed,
   `init-cli-agent` reuses the group and wiring. Cached checkouts are pointed
   at the requested repository and exact SHA; tracked edits in a base
   checkout cause a failure instead of being overwritten.

4. **Poke at it.** `ssh <ssh_dest>` (printed at the end; `ssh exe.dev ls
   --json` lists it), then in `~/nanoclaw`: `pnpm run chat hi`,
   `bin/ncl groups list`, `tail -f logs/nanoclaw.log`.

5. **Choose retention after evidence is safe.** Recommend deleting a disposable,
   run-owned VM after a pass and retaining an unexpected failure until triage is
   complete. Offer the operator that choice after reporting the result. With
   `--rm`, the driver revalidates its run marker, confirms no harness controller
   is active, calls `ssh exe.dev rm <name> --json`, verifies the name is absent
   from `ssh exe.dev ls --json`, and writes
   `teardown-receipt.json` into the validated artifact directory. Any failed
   export, checksum, redaction, local persistence, ownership, controller, or
   absence check blocks or leaves teardown unconfirmed. For manual cleanup,
   repeat the same checks and record the receipt; inventory lookup alone never
   proves ownership.

### Running the installer somewhere else

`e2e-install.sh` has no exe.dev dependency. On a CI runner or any fresh box:

```bash
git clone https://github.com/nanocoai/nanoclaw-oss-dev-tools.git
E2E_SKILL_DIR="$(pwd)/nanoclaw-oss-dev-tools/skills/e2e-exe-dev"
git clone https://github.com/nanocoai/nanoclaw.git
cd nanoclaw
NANOCLAW_E2E_PROVIDER=claude NANOCLAW_E2E_AUTH_METHOD=api \
NANOCLAW_E2E_AUTH_SOURCE_COMMIT="$(git rev-parse HEAD)" \
NANOCLAW_E2E_KEY_FILE=/path/to/key bash "$E2E_SKILL_DIR/scripts/e2e-install.sh"
```

The target needs `git` and `python3` before starting the installer. The exe.dev
driver installs these if missing; provision them yourself on other hosts.

Env it honors: `NANOCLAW_E2E_ROOT`, `NANOCLAW_E2E_KEY_FILE`,
`NANOCLAW_E2E_GATEWAY` (`onecli`, the default, or `iron-proxy`; only refs on
the gateway seam can select Iron — see below), `NANOCLAW_ONECLI_API_HOST` +
`NANOCLAW_ONECLI_API_TOKEN` (remote gateway instead of a local install — the
same vars `setup/auto.ts` reads),
`NANOCLAW_DISPLAY_NAME`, `NANOCLAW_E2E_TZ` (default `UTC`),
`NANOCLAW_E2E_KEEP_AGENT` (default `1`), `NANOCLAW_E2E_FORCE_AUTH` (`1`
replaces an existing vault secret with the key file — token rotation on a
`--base` VM whose snapshot still holds the old one).

The driver forwards the remote gateway settings, display name, timezone,
keep-agent and force-auth settings over stdin into a private environment
file, which is removed after loading. `NANOCLAW_E2E_KEY_FILE` (or `--key-file`)
selects the local key to upload; `NANOCLAW_E2E_ROOT` applies to standalone
installer runs. The driver always uses `~/nanoclaw` on its VM.

## Compatibility evidence

On **2026-09-12** a fresh exe.dev VM failed at the `onecli` step against
NanoClaw `fd767d381fe5a1fea51373e4e8cb411da79880ec` with `Port 5432 is already
in use` and nothing listening: the image had started shipping `/exe.dev/bin/sh`
first on PATH (see the gotcha below). With the installer's system-shell
preflight, a new fresh VM passed end to end against
[`d96dde93db7a766531ca288190ca202ad473547e`](https://github.com/nanocoai/nanoclaw/tree/d96dde93db7a766531ca288190ca202ad473547e):
the note line reported `/exe.dev/bin` removed from PATH, every step from
`environment` to `verify` succeeded, `SERVICE_TYPE: nohup`, `PING: ok`, exit 0,
about 3 minutes 20 seconds including OS bootstrap, and the VM was removed with
`--rm`. NanoClaw itself pins `/bin/sh` in those steps from
[nanocoai/nanoclaw#3776](https://github.com/nanocoai/nanoclaw/pull/3776), so
later refs no longer depend on this preflight.

Fresh and cached VM installations passed on **2026-09-10** against NanoClaw
[`74224f62a6c08418acccc727114ab02f92e403bf`](https://github.com/nanocoai/nanoclaw/tree/74224f62a6c08418acccc727114ab02f92e403bf).
Both produced real model replies, `SERVICE: running`, and successful final
verification using the nohup fallback. The cached run reused OneCLI, the
vault credential, Docker build layers and the existing agent wiring.

The fresh install passed with skill version `0.2.3`, but its subsequent
snapshot returned driver exit `70`: exe.dev created the copy using a response
shape that the driver did not recognize. Version `0.2.4` accepts that shape
only when its source and destination names match the request. The corrected
driver's live cached-copy run completed with exit `0`; offline regressions
also cover snapshot confirmation and mismatched copy responses.

Older NanoClaw refs, including `2c754a2234390fcc597273cef6344d99e8ac03d0`,
could fail verification after a successful ping when systemd's user bus was
unavailable. The live-tested ref fixes this by consulting the nohup PID file
when no service is found. Check the selected ref before assuming compatibility.

## What the installer calls, and why each call is shaped that way

Everything below is a wizard step or a script that already ships; the
installer adds sequencing and assertions only.

| # | Call | Source of truth |
|---|---|---|
| 1 | `bash setup.sh` | The launcher's prompt-free bootstrap (`nanoclaw.sh` runs it under a spinner): Node 22 via `setup/install-node.sh`, pnpm via corepack/npm, `pnpm install --frozen-lockfile`, native-module check |
| 2 | `bash setup/install-docker.sh`, then `sg docker` re-exec if the socket is group-gated | `setup/container.ts` does the same for its own step; done once up front because the `onecli` step (a docker-compose install) needs the daemon first |
| 3 | `--step environment` | Informational (`setup/environment.ts` never fails on a missing Docker) — kept for the log |
| 4 | `--step onecli` \| `--reuse` \| `--remote-url <host>` — or, on the gateway seam, `--step gateway <kind>` (see below) | `setup/onecli.ts`; mode chosen the way `setup/auto.ts` chooses it (`NANOCLAW_ONECLI_API_HOST` → remote; existing install → reuse; else fresh `curl onecli.sh/install \| sh` + CLI from GitHub releases, pinned by `versions.json`) |
| 5 | `--step auth --check`, then `--create --value <key>` only when `STATUS: missing` — or, on the gateway seam, a vault check + `--step gateway-auth claude` (see below) | `setup/auth.ts`; mirrors `runAuthStep`'s `anthropicSecretExists()` short-circuit |
| 6 | `--step container` | `setup/container.ts`: local `docker build` (or pull when `.env` has `NANOCLAW_HARDENED_IMAGE=true`), then the in-container smoke test |
| 7 | `--step mounts --empty` | The wizard's own args (`setup/auto.ts`); `skipped` on re-runs is fine |
| 8 | `--step timezone --tz <zone>` | `setup/timezone.ts` validates with `isValidTimezone` and writes `TZ` to `.env` |
| 9 | `--step service`, then `./start-nanoclaw.sh` iff `SERVICE_TYPE: nohup` | `setup/service.ts`: builds, **stamps the upgrade marker** (so the host's tripwire passes), installs a system unit as root / user unit otherwise / nohup wrapper without user systemd |
| 10 | Wait for `data/cli.sock`, then `scripts/init-cli-agent.ts --display-name … --agent-name "E2E Agent" --folder e2e-agent` | `src/index.ts` finishes host migrations before opening the CLI socket. Only then run the initializer, which also migrates the DB, to create the `cli:local` scratch user, agent group and wiring without racing fresh host migrations. |
| 11 | `pnpm --silent run chat ping` | `scripts/chat.ts`: exit 0 + reply = ok, 2 = socket unreachable, 3 = no reply within its 120 s stop; the auth-error patterns are the ones `agent-ping.ts` classifies |
| 12 | `--step verify` | `setup/verify.ts`: `success` iff service running ∧ credentials present ∧ (groups > 0 ∨ wiring pending); exits 1 otherwise |

Step output is parsed from the `=== NANOCLAW SETUP: <STEP> === … STATUS: … === END ===`
block every step prints (`setup/status.ts`).

### Refs on the gateway seam

From [nanocoai/nanoclaw#3815](https://github.com/nanocoai/nanoclaw/pull/3815)
onward the credential gateway is a seam (`setup/gateways/`): the `onecli` and
`auth` steps are gone, `--step gateway <kind>` installs or reuses the selected
gateway through its own `/add-<gateway>` skill, and `--step gateway-auth
<provider>` connects the model credential. The installer detects
`setup/gateways/step.ts` and switches to those steps; `NANOCLAW_E2E_GATEWAY`
(driver flag `--gateway`) picks `onecli` (default) or `iron-proxy`.

- **OneCLI**: the skill's auth flow has no prompt-free key path, so the
  installer seeds the vault the way that flow's `saveSecret()` does
  (`onecli secrets create --type anthropic --host-pattern api.anthropic.com`),
  after the same presence check the old `auth --check` made.
  `NANOCLAW_E2E_REQUIRE_EXISTING_AUTH` and `NANOCLAW_E2E_FORCE_AUTH` keep their
  meaning. `NANOCLAW_E2E_ONECLI_MODE` is not consulted: the skill reuses a
  healthy install, installs when absent, or uses the remote-gateway vars.
- **Iron Proxy**: the credential reaches `gateway-auth` through the
  environment of that one step. `--auth-method oauth` sets
  `NANOCLAW_CLAUDE_CODE_OAUTH_TOKEN` (and, for cores without
  [#3840](https://github.com/nanocoai/nanoclaw/pull/3840), also
  `NANOCLAW_ANTHROPIC_API_KEY`, which the fixed flow recognises by prefix);
  `api` sets only `NANOCLAW_ANTHROPIC_API_KEY`. Iron builds its proxy image
  from source on first install (about 3 minutes on a 4-CPU VM) and starts Iron
  Control on `127.0.0.1:10257`.
- Cores before #3840 return from the gateway steps without a status block;
  a zero exit is accepted there with a `note:` line, other steps need one.
- `gateway` is the kind this run proved installed (null until proven) and
  `requested_gateway` the request: after the `gateway` step the installer
  checks the block's `GATEWAY:` field when present and, on every seam core,
  the `NANOCLAW_GATEWAY_PROVIDER` stamp `installGateway` writes to `.env`;
  after the `service` step it reads the service process's environment (or
  the unit's `Environment=`, `EnvironmentFile=` and manager environment)
  for another gateway, case-insensitively; a failed inspection or any
  mismatch stops the run, recording the found kind.
  An inherited `NANOCLAW_GATEWAY_PROVIDER` is unset first, because
  `gateway-auth`, the credential store and the runtime prefer it over the
  stamp. `gateway_seam` is true only once the seam's `gateway` step starts.

Verified on **2026-09-16** against the stack tip
`7b5eb18544aabb7dcfa3c6a09a7c1c2dfe232af3`: OneCLI + Claude and Iron Proxy +
Claude both passed on one fresh exe.dev VM with real replies and
`verify: success`. The Iron pass used an OAuth token; on that pre-#3840 core it
had to be stored as `CLAUDE_CODE_OAUTH_TOKEN` by hand, which is what the
`--auth-method oauth` handling above now does. A second fresh VM
(`nc-gw3840-iron-oauth`) then passed this driver end to end with
`--gateway iron-proxy --auth-method oauth` against a core with #3840
(`44bfc117884232a6d1092024bc0a9f6e59bcb728`): status blocks from both gateway
steps, `PING: ok`, `verify: success`, exit 0, sanitized evidence retained.
Those runs predate the `gateway` result field. Not covered by Iron yet, both
OneCLI-only: [e2e-wizard](../e2e-wizard/SKILL.md#gateway-seam-limitation) and
[e2e-macos](../e2e-macos/SKILL.md#choose-the-gateway-explicitly). OpenCode through Iron
([nanocoai/nanoclaw#3825](https://github.com/nanocoai/nanoclaw/pull/3825))
also needs an HTTPS-on-443 model endpoint and the read-only `gateway-trust` CA
mount in the agent container; neither is automated here.

## Gotchas (each one cost a wrong assumption)

- **`sh` on PATH may not be the system shell.** exe.dev images since
  2026-09-09 put `/exe.dev/bin` first on PATH with their own `sh`, whose
  builtin `lsof` always exits 0. NanoClaw's setup pipes downloaded installers
  into `sh` (`setup/onecli.ts`, `setup/install-docker.sh`), so the OneCLI
  installer's port probe reported every port as busy — `Port 5432 is already
  in use (probably a local PostgreSQL)` with nothing listening, and the same
  for `POSTGRES_PORT=5433`. The installer now drops the directory holding a
  foreign `sh` from PATH before the first step (it prints a `note:` line) and
  stops when no system shell exists at `/bin/sh` or `/usr/bin/sh`. The
  product-side fix pins `/bin/sh` in those steps.
- **The wizard is not headless.** `pnpm run setup:auto` prompts for start
  mode, existing-install action and OneCLI reuse with no env bypass, and
  `nanoclaw.sh` reads its root warning from `/dev/tty`. Only the step runner
  (`setup/index.ts --step`) is prompt-free — that's why the installer is
  built on it and never touches `nanoclaw.sh`/`setup:auto`.
- **No user systemd ⇒ nothing starts.** `setupNohupFallback` writes
  `start-nanoclaw.sh` and reports `SERVICE_LOADED: false`; it does not run
  it. The installer starts it. exe.dev VMs are exactly this shape: systemd
  is PID 1 but `systemctl --user` fails ("Failed to connect to bus"), so
  `service` falls back to nohup. `verify.ts` used to check `nanoclaw.pid`
  only when there was no systemd at all and reported `SERVICE: not_found`
  here. The live-tested SHA above fixes this by checking the PID file whenever
  no service is found; older NanoClaw refs may still have the limitation.
- **A non-interactive SSH shell has no `~/.local/bin` on PATH** — `pnpm`,
  `node`, `onecli` all live there. Prefix ad-hoc commands on the VM with
  `export PATH="$HOME/.local/bin:$PATH"`; the installer does this itself.
- **Treat the token as exposed once it has been in any log.** The installer
  redacts `--value` in its own output, but the OAuth token is long-lived:
  rotate it (revoke, `claude setup-token`) after a run whose logs left the
  machine, and re-seed base VMs with `NANOCLAW_E2E_FORCE_AUTH=1`.
- **Keep the agent, or `verify` fails.** The wizard deletes its `ping_test`
  group after the ping (`scripts/delete-cli-agent.ts`) *before* running
  `verify`, which then relies on a channel deferring its wiring. With no
  channel configured that's `registeredGroups: 0` → `failed`. The installer
  keeps `e2e-agent` (set `NANOCLAW_E2E_KEEP_AGENT=0` to delete it after the
  ping; expect `verify` to fail then).
- **The vault is not optional.** Spawn refuses without a gateway
  contribution (`src/gateway-providers/onecli.ts`: "refusing to spawn
  container without credentials"), and `onecli` is the only provider in
  trunk. There is no API-key-in-env shortcut.
- **Docker is the only driver.** `src/drivers/index.ts` ships `docker` alone;
  `NANOCLAW_RUNTIME_DRIVER` set to anything else throws at startup.
- **First container boot is 30–60 s** after the ping is sent; `chat.ts`
  waits up to 120 s and the installer wraps it in `timeout 150`.
- **`GITHUB_TOKEN` in the environment counts as a configured channel** in
  `verify.ts` (`has()` reads `process.env`). The installer unsets it so the
  report describes the install, not the shell.
- **Egress the VM must have:** `deb.nodesource.com`, `get.docker.com`,
  `onecli.sh`, `github.com` (OneCLI CLI release tarball), Docker Hub (base
  image), `api.anthropic.com`. exe.dev VMs have a full network stack; locked-
  down runners do not.
- **The key is on argv for one process.** `auth --create --value` is how
  `setup/auth.ts` accepts it (it then `execFileSync`s `onecli`, no shell), so
  it is visible to `ps` on the VM for that step. Fine for a disposable VM;
  use `NANOCLAW_ONECLI_API_HOST` with a pre-seeded remote vault if not.
- **Don't synthesize the VM hostname.** `new --json` returns `vm_name` and
  `ssh_dest`; the current `cp --json` response uses `name`, `source` and
  `ssh_host`. The driver checks the requested destination name and, for that
  copy format, the source name too. It also accepts the older `vm_name`
  response shape. Modern destinations look like `<name>.exe.xyz`, legacy
  ones like `vm+<name>@vm.exe.xyz`; the driver passes the returned destination
  to SSH. It never falls back to `ls --json` to infer creation success.
- **First contact blocks on the host-key prompt** in a non-interactive
  shell with nothing visible. Every VM connection in the driver carries
  `-o StrictHostKeyChecking=accept-new`.
- **The lobby is not a shell.** `scp`/`sftp`/commands against `exe.dev`
  fail; files go to the VM destination, and the driver uses
  `ssh <vm> 'cat > file' < local` (exe.dev's documented scp-less path).
- **exe.dev `--setup-script` is capped at 10 KiB** and runs at first boot,
  before you can hand it a secret safely. The driver therefore SSHes in after
  boot instead — no size limit, secrets over stdin.

## Troubleshooting

| Symptom | Look at | Likely cause |
|---|---|---|
| `setup.sh failed` | `logs/e2e/bootstrap.log` | apt/NodeSource unreachable, or `corepack enable` needed sudo and none was passwordless |
| `docker socket still not accessible under sg docker` | `id -nG`, `ls -l /var/run/docker.sock` | daemon not started; run `sudo systemctl start docker` and re-run |
| `onecli install failed` / `could_not_resolve_api_host` | `logs/e2e/onecli.log`, `logs/setup.log` | `onecli.sh` or GitHub releases blocked; `docker compose` missing from the Docker install |
| `auth --check reported failed` | `onecli secrets list` on the VM | gateway not healthy on port 10254 |
| `container step failed` | `logs/e2e/container.log` | Docker Hub pull blocked, or the smoke test failed inside the image |
| exit `3` (no `data/cli.sock`) | `logs/nanoclaw.error.log` | host crashed at start — read the first ERROR; the upgrade tripwire is not it (`service` stamped the marker) |
| exit `2` with an auth-error reply | the reply text in `logs/e2e/ping.out` | wrong or expired key in the vault; `onecli secrets` to fix, then re-run (auth step short-circuits, so delete the bad secret first). With Iron, an OAuth token stored as an API key shows the same symptom; use `--auth-method oauth` |
| `invalid gateway` / `does not select a gateway` (exit `64`) | the `--gateway` value or `NANOCLAW_E2E_GATEWAY` in your shell | only `onecli` and `iron-proxy` exist, and only headless runs take the choice; nothing was created |
| `result-identity-mismatch` on export after a green install | `logs/e2e/result.json` `gateway` on the VM vs. the driver's `--gateway` | the installer ran another gateway than requested (an inherited setting on a base VM, or a stale installer copy); the VM is kept |
| exit `2`, no reply at all | `logs/nanoclaw.log` around the ping timestamp, `bin/ncl sessions list` | container failed to spawn (OneCLI "not applied"), or the agent errored — container logs are gone after exit, so check the outbound DB: `pnpm exec tsx scripts/q.ts data/v2-sessions/<group>/<session>/outbound.db "select * from messages_out"` |
| `verify reported failed` after a green ping | the `SERVICE:` / `CREDENTIALS:` / `REGISTERED_GROUPS:` fields | agent deleted (`KEEP_AGENT=0`); or `SERVICE: not_found` on a nohup-started host — see the source-review limitation above |
| `snapshot … creation was not confirmed` / `NAME creation was not confirmed` | the lobby response right above it | name taken, malformed response, or copy source/destination mismatch — check before retrying |

## Teardown

`ssh exe.dev rm <name>` when done — the VM's persistent disk holds the vault
secret. A standalone installer writes setup state and logs into its target
checkout; the driver runs setup on the VM. Uninstall the plugin
with `/plugin uninstall nanoclaw-e2e@nanoclaw-oss-dev-tools`.
