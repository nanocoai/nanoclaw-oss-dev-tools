---
name: e2e-exe-dev
description: "Provision an exe.dev VM and run a headless NanoClaw install on it end to end — Node/pnpm, Docker, OneCLI, vault secret, agent image, service, a cli-channel agent — then assert the same ping round-trip the setup wizard uses. Use to test a branch or PR on a real machine, to reproduce an install failure, or to bake a reusable base VM. Triggers on \"e2e test on exe.dev\", \"install nanoclaw on a vm\", \"headless install\", \"test this branch end to end\", \"exe.dev\"."
license: MIT
compatibility: Requires bash, git, ssh and python3 on the operator machine; the target is a Debian/Ubuntu host with sudo, git and python3. Docker is installed by setup if missing. An exe.dev account is needed only for the bundled driver.
---

# e2e on exe.dev

For an installation on an existing Mac, locally or over SSH, use
[`e2e-macos`](../e2e-macos/SKILL.md). This skill's target workflow is Linux;
its installer is shared with the Mac driver and its LaunchAgent helper.

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
checkout being tested are separate directories.

Two scripts, both under `scripts/`:

| Script | Runs on | Does |
|---|---|---|
| `exe-run.sh` | your machine | `ssh exe.dev new --json` (or `cp` from a base VM), pushes the key and the installer over SSH, clones the ref, runs the installer, optionally snapshots (`--snapshot`) or deletes on pass (`--rm`) |
| `e2e-install.sh` | the VM (or any Debian/Ubuntu box, or a CI runner) | the headless install + ping; exits 0 on pass |

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
- An Anthropic API key or OAuth token in a local file, default
  `~/.nanoclaw-e2e/anthropic_key`. It goes to the VM over stdin into a
  `0600` file; the installer seeds it into the OneCLI vault exactly as
  `setup/auth.ts` does (`onecli secrets create --type anthropic
  --host-pattern api.anthropic.com`).
- A Debian/Ubuntu image on the VM (exe.dev's default). Both bootstrap
  helpers are apt-based: `setup/install-node.sh` (NodeSource) and
  `setup/install-docker.sh` (`get.docker.com` + `usermod -aG docker`).
  Docker runs inside exe.dev VMs.

## Workflow

1. **Run it** from the checkout root, on the branch you want tested. Set
   `E2E_SKILL_DIR` to the actual directory containing this `SKILL.md`:

   ```bash
   E2E_SKILL_DIR=/absolute/path/to/installed/e2e-exe-dev
   cd /absolute/path/to/nanoclaw
   bash "$E2E_SKILL_DIR/scripts/exe-run.sh"  # HEAD, new VM
   bash "$E2E_SKILL_DIR/scripts/exe-run.sh" --ref origin/main --name nc-main
   ```

   For a disposable run with a retained local report, add
   `--result-file /path/to/result.json --rm` (the parent directory must exist).

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
   type, ping classification and timestamps. An in-progress run has status
   `running`. It replaces a previous run's result and contains no credentials
   or reply text. With `--result-file`, after argument parsing and destination
   validation the driver replaces any old local report with `running` before
   checking the checkout, credentials or VM. It exports a matching completed
   installer result before snapshot/removal. Otherwise it records the driver
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
   bash "$E2E_SKILL_DIR/scripts/exe-run.sh" --snapshot my-nanoclaw-e2e-base
   bash "$E2E_SKILL_DIR/scripts/exe-run.sh" --base my-nanoclaw-e2e-base --ref my-branch
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

5. **Delete it.** `ssh exe.dev rm <name>` — or pass `--rm` to the driver to
   delete on a pass (a failed VM is always kept so you can look at it).

### Running the installer somewhere else

`e2e-install.sh` has no exe.dev dependency. On a CI runner or any fresh box:

```bash
git clone https://github.com/nanocoai/nanoclaw-oss-dev-tools.git
E2E_SKILL_DIR="$(pwd)/nanoclaw-oss-dev-tools/skills/e2e-exe-dev"
git clone https://github.com/nanocoai/nanoclaw.git
cd nanoclaw
NANOCLAW_E2E_KEY_FILE=/path/to/key bash "$E2E_SKILL_DIR/scripts/e2e-install.sh"
```

The target needs `git` and `python3` before starting the installer. The exe.dev
driver installs these if missing; provision them yourself on other hosts.

Env it honors: `NANOCLAW_E2E_ROOT`, `NANOCLAW_E2E_KEY_FILE`,
`NANOCLAW_ONECLI_API_HOST` + `NANOCLAW_ONECLI_API_TOKEN` (remote gateway
instead of a local install — the same vars `setup/auto.ts` reads),
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
| 4 | `--step onecli` \| `--reuse` \| `--remote-url <host>` | `setup/onecli.ts`; mode chosen the way `setup/auto.ts` chooses it (`NANOCLAW_ONECLI_API_HOST` → remote; existing install → reuse; else fresh `curl onecli.sh/install \| sh` + CLI from GitHub releases, pinned by `versions.json`) |
| 5 | `--step auth --check`, then `--create --value <key>` only when `STATUS: missing` | `setup/auth.ts`; mirrors `runAuthStep`'s `anthropicSecretExists()` short-circuit |
| 6 | `--step container` | `setup/container.ts`: local `docker build` (or pull when `.env` has `NANOCLAW_HARDENED_IMAGE=true`), then the in-container smoke test |
| 7 | `--step mounts --empty` | The wizard's own args (`setup/auto.ts`); `skipped` on re-runs is fine |
| 8 | `--step timezone --tz <zone>` | `setup/timezone.ts` validates with `isValidTimezone` and writes `TZ` to `.env` |
| 9 | `--step service`, then `./start-nanoclaw.sh` iff `SERVICE_TYPE: nohup` | `setup/service.ts`: builds, **stamps the upgrade marker** (so the host's tripwire passes), installs a system unit as root / user unit otherwise / nohup wrapper without user systemd |
| 10 | `scripts/init-cli-agent.ts --display-name … --agent-name "E2E Agent" --folder e2e-agent` | Creates the `cli:local` scratch user, an agent group and the wiring to the cli messaging group; runs migrations itself, safe alongside the running host |
| 11 | `pnpm --silent run chat ping` | `scripts/chat.ts`: exit 0 + reply = ok, 2 = socket unreachable, 3 = no reply within its 120 s stop; the auth-error patterns are the ones `agent-ping.ts` classifies |
| 12 | `--step verify` | `setup/verify.ts`: `success` iff service running ∧ credentials present ∧ (groups > 0 ∨ wiring pending); exits 1 otherwise |

Step output is parsed from the `=== NANOCLAW SETUP: <STEP> === … STATUS: … === END ===`
block every step prints (`setup/status.ts`).

## Gotchas (each one cost a wrong assumption)

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
| exit `2` with an auth-error reply | the reply text in `logs/e2e/ping.out` | wrong or expired key in the vault; `onecli secrets` to fix, then re-run (auth step short-circuits, so delete the bad secret first) |
| exit `2`, no reply at all | `logs/nanoclaw.log` around the ping timestamp, `bin/ncl sessions list` | container failed to spawn (OneCLI "not applied"), or the agent errored — container logs are gone after exit, so check the outbound DB: `pnpm exec tsx scripts/q.ts data/v2-sessions/<group>/<session>/outbound.db "select * from messages_out"` |
| `verify reported failed` after a green ping | the `SERVICE:` / `CREDENTIALS:` / `REGISTERED_GROUPS:` fields | agent deleted (`KEEP_AGENT=0`); or `SERVICE: not_found` on a nohup-started host — see the source-review limitation above |
| `snapshot … creation was not confirmed` / `NAME creation was not confirmed` | the lobby response right above it | name taken, malformed response, or copy source/destination mismatch — check before retrying |

## Teardown

`ssh exe.dev rm <name>` when done — the VM's persistent disk holds the vault
secret. A standalone installer writes setup state and logs into its target
checkout; the driver runs setup on the VM. Uninstall the plugin
with `/plugin uninstall nanoclaw-e2e@nanoclaw-oss-dev-tools`.
