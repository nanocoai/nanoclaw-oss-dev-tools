---
name: e2e-exe-dev
description: "Provision an exe.dev VM and run a headless NanoClaw install on it end to end — Node/pnpm, Docker, OneCLI, vault secret, agent image, service, a cli-channel agent — then assert the same ping round-trip the setup wizard uses. Use to test a branch or PR on a real machine, to reproduce an install failure, or to bake a reusable base VM. Triggers on \"e2e test on exe.dev\", \"install nanoclaw on a vm\", \"headless install\", \"test this branch end to end\", \"exe.dev\"."
---

# e2e on exe.dev

Installs NanoClaw on a disposable exe.dev VM without a human at the keyboard
and proves the whole loop: message in over the cli channel → host → container
→ Claude → reply out. It reuses the setup wizard's own step processes
(`pnpm exec tsx setup/index.ts --step …`) in the wizard's order — no parallel
install logic to drift — and replaces each prompt with an env var or a
pre-seeded state. The assertion is the wizard's first-chat probe
(`setup/lib/agent-ping.ts`): `pnpm run chat ping` must return a reply.

Two scripts, both under `scripts/`:

| Script | Runs on | Does |
|---|---|---|
| `exe-run.sh` | your machine | `ssh exe.dev new` (or `cp` from a base VM), pushes the key and the installer over SSH, clones the ref, runs the installer, optionally snapshots |
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

- An exe.dev account with `ssh exe.dev` working from your machine (the VM
  gets a `<name>.exe.xyz` hostname; SSH and the HTTPS proxy come with it).
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

1. **Run it** from the checkout root, on the branch you want tested:

   ```bash
   .claude/skills/e2e-exe-dev/scripts/exe-run.sh                 # HEAD, new VM
   .claude/skills/e2e-exe-dev/scripts/exe-run.sh --ref origin/main --name nc-main
   ```

   `--ref` can be any commit — the installer is pushed from *this* checkout,
   so refs that predate the skill are testable too.

2. **Read the result.** The last thing printed on success is

   ```
   === NANOCLAW E2E: RESULT ===
   STATUS: pass
   SERVICE_TYPE: systemd-user | systemd-system | nohup
   PING: ok
   REPLY: <first 200 chars of the agent's reply>
   === END ===
   ```

   Exit codes: `0` pass · `1` a step failed (its `=== NANOCLAW SETUP: … ===`
   block is printed right above the failure; raw output in
   `~/nanoclaw/logs/e2e/<step>.log` on the VM) · `2` no reply / auth error ·
   `3` the host never opened `data/cli.sock`.

3. **Bake a base VM** once the run is green, then clone it for every later run:

   ```bash
   .claude/skills/e2e-exe-dev/scripts/exe-run.sh --snapshot nanoclaw-base
   .claude/skills/e2e-exe-dev/scripts/exe-run.sh --base nanoclaw-base --ref my-branch
   ```

   Every step is idempotent against the cloned state: `onecli --reuse` when
   `.env` already has `ONECLI_URL`, `auth --check` short-circuits on an
   existing vault secret, `container` rebuilds only what changed,
   `init-cli-agent` reuses the group and wiring.

4. **Poke at it.** `ssh <name>.exe.xyz`, then in `~/nanoclaw`:
   `pnpm run chat hi`, `bin/ncl groups list`, `tail -f logs/nanoclaw.log`.
   The VM persists until you delete it from exe.dev.

### Running the installer somewhere else

`e2e-install.sh` has no exe.dev dependency. On a CI runner or any fresh box:

```bash
git clone <repo> nanoclaw && cd nanoclaw
NANOCLAW_E2E_KEY_FILE=/path/to/key bash .claude/skills/e2e-exe-dev/scripts/e2e-install.sh
```

Env it honors: `NANOCLAW_E2E_ROOT`, `NANOCLAW_E2E_KEY_FILE`,
`NANOCLAW_ONECLI_API_HOST` + `NANOCLAW_ONECLI_API_TOKEN` (remote gateway
instead of a local install — the same vars `setup/auto.ts` reads),
`NANOCLAW_DISPLAY_NAME`, `NANOCLAW_E2E_TZ` (default `UTC`),
`NANOCLAW_E2E_KEEP_AGENT` (default `1`).

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
| 10 | `scripts/init-cli-agent.ts --display-name … --agent-name "E2E Agent" --folder e2e-agent` | Creates the `cli:local` owner, an agent group and the wiring to the cli messaging group; runs migrations itself, safe alongside the running host |
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
  it. The installer starts it. `verify.ts` recognises the resulting
  `nanoclaw.pid`.
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
| `verify reported failed` after a green ping | the `SERVICE:` / `CREDENTIALS:` / `REGISTERED_GROUPS:` fields | agent deleted (`KEEP_AGENT=0`), or the service isn't detectable the way it was started |

## Teardown

Delete the VM from exe.dev when done (its persistent disk holds the vault
secret). Nothing is left in this repo: the skill is instruction plus its own
`scripts/`, so there is no `REMOVE.md`.
