# NanoClaw OSS Dev Tools: skill catalog

Verified: **2026-09-24**. This checkout contains **9 skills**, with plugin
manifest version **0.13.0** (development, unreleased). Three drive headless setup steps; `e2e-wizard` and
`e2e-windows` drive the public interactive wizard. The shared `e2e-triage` skill
researches unexpected failures and prepares reporting recommendations. The
workflows and their qualifications are listed separately below. `shared-terminal`
provides a local terminal for human and agent handoffs. `typesafe-docs-drift`
ranks where the docs portal disagrees with the code through the TypeSafe
decision API and writes nothing. `typesafe-triage` previews confidence-gated
issue and PR labels from the TypeSafe decision API without writing to GitHub.

Installed skill copies must be updated separately, and the manifest version
does not imply a tagged GitHub release.

The host-readiness fix merged in
[PR 6](https://github.com/nanocoai/nanoclaw-oss-dev-tools/pull/6) and includes the
successful SSH qualification described below.

OpenCode OpenRouter/DeepSeek and custom/self-hosted API-key wizard support
has offline coverage. On 2026-09-15, a fresh exe.dev run passed the full public
wizard through an operator-supplied OpenAI-compatible endpoint, including the
retained reply, provider, service, socket and sanitized evidence checks. An earlier
attempt exposed a harness tool-path bug and remains a retained failure. See the
[OpenCode workflow](../skills/e2e-wizard/SKILL.md#opencode) for flags and limits.

## Choose a skill

| Skill | Target and purpose | Live evidence | Main remaining gap |
|---|---|---|---|
| [e2e-exe-dev](../skills/e2e-exe-dev/SKILL.md) | Fresh or cached exe.dev Debian/Ubuntu VM; portable shared installer also usable on a prepared Linux host or CI runner. | Fresh and cached installs passed on 2026-09-10 with real replies and final verification. | Evidence used the nohup service fallback; do not infer every Linux service mode or arbitrary NanoClaw ref is qualified. |
| [e2e-proxmox](../skills/e2e-proxmox/SKILL.md) | Fresh unprivileged Debian 13 amd64 LXC, managed through SSH to the Proxmox node. | Fresh install passed on 2026-09-11 with a real reply and a running systemd user service. | Reboot recovery, cached clones and the transactional updater were not tested. |
| [e2e-macos](../skills/e2e-macos/SKILL.md) | Existing native Mac, local or SSH, using a new persistent checkout and its own LaunchAgent. | Local arm64 passed with 0.4.0; SSH on M4 Pro passed on 2026-09-11 with the readiness fix now merged in 0.4.1. Both proved a real reply, service and preservation. | Intel/older macOS, cold prerequisites and reboot/logout recovery remain unqualified. |
| [e2e-wizard](../skills/e2e-wizard/SKILL.md) | Public interactive setup in a fresh exe.dev VM or Proxmox LXC, driven through a real PTY and terminal emulator. | Fresh Proxmox wizard completed on 2026-09-11, with a retained agent's real reply and exact service verification. On 2026-09-15 a fresh exe.dev VM passed the distributed harness with OpenCode through a custom OpenAI-compatible endpoint. | Native macOS, OpenCode on Proxmox/WSL2 and the OpenRouter/DeepSeek backends, post-install channel/provider refresh and restart paths, reboot recovery, and real messaging channels remain unqualified. |
| [e2e-windows](../skills/e2e-windows/SKILL.md) | Fresh Windows WSL2 distribution using the local Docker Desktop Linux engine. | Public wizard passed again on 2026-09-14 at current NanoClaw `main`; terminal-close and post-Windows-reboot inference also passed with locally validated sanitized evidence. | WSL-only restart still left the service/socket unavailable, and Docker Desktop did not start automatically after Windows reboot. |
| [e2e-triage](../skills/e2e-triage/SKILL.md) | Agent workflow on the operator machine, using retained evidence and current upstream trackers. | Replayed retained Windows CA failure against live issues/PRs and the current NanoClaw bug form on 2026-09-12. | Instruction-driven; direct shell runs do not invoke it. No public submission was performed during validation. |
| [typesafe-docs-drift](../skills/typesafe-docs-drift/SKILL.md) | Ranks disagreements between a NanoClaw checkout and the nanoclaw-docs portal through the TypeSafe System One API: deterministic facts, lexical candidate sections, one fan-out request per pair, gated DRIFT / MISSING / UNSURE / OK verdicts. | Offline fixture replay and mocked-transport tests; one live run on 2026-09-16 (142 facts, 426 requests, 71.5 s: DRIFT 7, MISSING 1, UNSURE 11, OK 123; see [typesafe-docs-drift.md](typesafe-docs-drift.md)). | Detection only; candidate recall is lexical top-3; fixture answers are hand-written. |
| [typesafe-triage](../skills/typesafe-triage/SKILL.md) | Dry-run label triage of open nanocoai/nanoclaw issues and PRs through the TypeSafe System One API, with a confidence gate and comparison against existing labels. | Offline fixture replay and mocked-transport tests only; no live TypeSafe call has been recorded yet. | Live agreement rates are unmeasured; the fixture answers are hand-written. It proposes labels only and never applies them. |
| [shared-terminal](../skills/shared-terminal/SKILL.md) | One local PTY with browser and agent control, for supervised interactive work. | macOS browser typing, interactive prompts, agent/human handoff and shutdown passed; real HTTP/PTY tests passed. | Native Windows, WSL and browser forwarding are unqualified. |

The shared baseline for the three headless skills tested NanoClaw
[`74224f62a6c08418acccc727114ab02f92e403bf`](https://github.com/nanocoai/nanoclaw/commit/74224f62a6c08418acccc727114ab02f92e403bf).
The later Mac run against the upstream migration fix is recorded separately
below. These are exact-configuration results, not blanket compatibility claims.

## Shared workflow and acceptance

Run a skill from the **NanoClaw checkout under test**, not from this tools
repository. Remote lifecycle drivers resolve the requested ref locally and fetch
the exact commit on the target; local edits are not uploaded, and the commit must
be fetchable. The Windows runner executes inside the prepared WSL distribution
and requires its clean checkout to already match the requested exact commit.

The headless [e2e-install.sh](../skills/e2e-exe-dev/scripts/e2e-install.sh) drives
NanoClaw's existing setup steps for bootstrap, Docker/OneCLI readiness, auth,
agent image, service, CLI agent, model ping and final verification. On refs
with the credential-gateway seam it runs the `gateway`/`gateway-auth` steps
instead and can select OneCLI (default) or Iron Proxy through `--gateway` on
the exe.dev and Proxmox drivers; the result and sanitized evidence record which
gateway ran. The Mac path uses a dedicated LaunchAgent helper to preserve other
installations and remains OneCLI-only, as does the public-wizard driver. Wizard
mode uses the public `bash nanoclaw.sh` entry point and adds no product setup
actions or repairs behind its prompts.

Before provisioning, every full E2E workflow resolves the exact NanoClaw SHA
and uses `e2e-wizard/scripts/provider-options.py` to reproduce the offered
provider list from Git objects. After the operator chooses a provider, the
workflow reads that provider's auth prompt/options from the same NanoClaw SHA or
its exact fetched provider-payload SHA, shows them, and asks for the auth method.
Unattended drivers accept only methods they can fulfill with the matching private
credential file. The Proxmox and direct wizard entry points also support
supervised Codex device pairing and Claude subscription sign-in after the
operator chooses the method and is ready for the live handoff. Other entry
points remain unattended. `skip` never qualifies a pass.

A pass requires an actual CLI-agent response and successful NanoClaw service
verification for the requested commit. JSON reports record status, phase, exit
code and commit identity. A successful command launch, dry run or mocked test is
not live E2E proof. These tools exercise the CLI channel; real Slack, Telegram or
other messaging-channel integrations are outside their acceptance scope.

The credentials used by these workflows stay out of the repository. Drivers
read authorized operator files and transport values through stdin/private files;
OneCLI retains the imported credential in its vault where installation is used.
Keep target-specific reports and detailed logs private when they include host
or account details.

## e2e-exe-dev

**Entry points:** [exe-run.sh](../skills/e2e-exe-dev/scripts/exe-run.sh) on the
operator machine; the shared installer on the target.

- Needs Bash, Git, OpenSSH, Python 3, working exe.dev access and an authorized
  credential file matching the selected provider/auth method. The guest is Debian/Ubuntu with sudo.
- Supports an exact ref, a new VM, a cached base, an optional snapshot and a
  local result file. The driver confirms allocation/copy identity before use.
- `--gateway onecli|iron-proxy` on gateway-seam refs; validated before any VM
  work and bound into the result and evidence. Iron Proxy + Claude passed live
  on exe.dev on 2026-09-16 (OAuth token; stack tip and a #3840 core).
- Failed runs are retained. Removal after success is opt-in with the documented
  flag and requires a locally validated evidence bundle, matching run marker,
  inactive harness, verified JSON-inventory absence and teardown receipt.
- Live fresh/cached runs used the nohup fallback. A snapshot-response mismatch
  found during the fresh run was fixed in version 0.2.4 and the corrected live
  cached-copy run exited successfully. Preserve that distinction when citing
  the earlier run.

Use it for clean install reproduction and cached VM iteration. Follow the
[full workflow and evidence](../skills/e2e-exe-dev/SKILL.md#workflow) for flags,
credential handling and result semantics. The hardening and copy-response
changes merged in [PR 1](https://github.com/nanocoai/nanoclaw-oss-dev-tools/pull/1)
and [PR 2](https://github.com/nanocoai/nanoclaw-oss-dev-tools/pull/2).

## e2e-proxmox

**Entry points:** [proxmox-run.py](../skills/e2e-proxmox/scripts/proxmox-run.py)
and [bootstrap.sh](../skills/e2e-proxmox/scripts/bootstrap.sh), plus the shared
installer from the sibling exe.dev skill.

- Needs Python 3.10+, Git and OpenSSH on the operator, root SSH access to the
  selected node, an existing Debian 13 amd64 template, storage, bridge and
  working outbound networking.
- Creates a new unprivileged LXC with `nesting=1,keyctl=1`; defaults are 2 CPUs,
  8 GiB RAM and 40 GiB disk. It uses a regular developer account and a lingering
  systemd user session inside the guest.
- Reaches the guest through node-side `pct exec`; no guest SSH connection,
  browser session or Proxmox API token is required.
- Dry run resolves the ref and writes a plan without SSH or credential reads.
  A live run creates only a new guest, verifies its ownership marker and retains
  it on success or failure. Existing development guests are not test targets.
- `--gateway onecli|iron-proxy` is sent to the guest explicitly and must match
  the installer result before a pass is reported. Iron on Proxmox has offline
  coverage only; no live Proxmox Iron run is recorded yet.
- Cleanup is offered only after durable evidence. It requires a separately
  authorized exact CT, a fresh marker/controller recheck, absence verification
  and a local teardown receipt; it never includes the Proxmox host or other guests.
- Live evidence: Proxmox VE 9.2.18, kernel 7.0.14-16-pve, Debian 13.6 template,
  real model reply, final verification and active `systemd-user` service in
  about four minutes.

See the [workflow and compatibility evidence](../skills/e2e-proxmox/SKILL.md).
Merged in [PR 4](https://github.com/nanocoai/nanoclaw-oss-dev-tools/pull/4).
This skill creates **Linux containers**, not macOS VMs.

## e2e-macos

**Entry points:** [macos-run.py](../skills/e2e-macos/scripts/macos-run.py),
[macos-target.py](../skills/e2e-macos/scripts/macos-target.py), and
[macos-service.py](../skills/e2e-macos/scripts/macos-service.py), plus the shared
installer.

- Needs Python 3.9+, Git, Xcode Command Line Tools, Node 22+ or Homebrew, a
  functioning Docker daemon, and a regular user with an active GUI login.
- Supports the current Mac or an existing verified SSH destination. The GUI
  session must belong to the same user as the SSH session; SSH alone is
  insufficient. The driver does not enable Remote Login or enroll keys.
- Requires a new persistent installation path and an explicit gateway choice:
  reuse the existing target gateway or install a new one with an authorized
  credential. Reuse mode does not transfer an operator credential.
- Dry run performs read-only checks on the actual target. Live setup creates
  its own LaunchAgent and compares shared state before/after, including existing
  services, running Docker containers and shared configuration. It preserves
  peer services and the global `ncl` link.
- Cleanup is offered after durable evidence for the run-owned checkout and
  LaunchAgent only; shared host, Docker, OneCLI and unrelated services remain outside it.
- Local evidence: macOS 26.6.1 / arm64, Docker Desktop and Node 26.8.1; real reply,
  successful verification and passing preservation checks in about 65 seconds.
  The checkout and service were retained.
- SSH evidence: Apple M4 Pro / 24 GiB, macOS 26.6.2 / arm64, Node 26.8.2 and
  Docker Desktop with local Docker Engine 29.7.2. A fresh checkout with warm
  caches passed in about 44 seconds at the same NanoClaw commit, including a
  real reply, service verification and shared-state preservation.
- The first SSH attempt exposed concurrent SQLite migrations between the
  starting host and `init-cli-agent.ts`. It failed before any model probe.
  The shared installer now waits for the CLI socket before initializing the
  agent; NanoClaw opens that socket after completing host migrations. The
  original failed installation was retained and a separate fresh installation
  passed, without changing NanoClaw source or repairing its database manually.
  That installer is now merged in **0.4.1** through
  [PR 6](https://github.com/nanocoai/nanoclaw-oss-dev-tools/pull/6). Its SHA-256 is
  `90aa14c0811c06b95c04a45471cc59a2961f57563eab68723b7794fc4e0d51a8`.

A separate SSH run on the same M4 Pro passed in about **50 seconds** against
NanoClaw [PR 3766](https://github.com/nanocoai/nanoclaw/pull/3766), exact commit
[`1d5179b28ce7b76afef6a23b7c21f981e51cfdad`](https://github.com/nanocoai/nanoclaw/commit/1d5179b28ce7b76afef6a23b7c21f981e51cfdad).
It used the original installer ordering from tools commit
[`04500814`](https://github.com/nanocoai/nanoclaw-oss-dev-tools/commit/04500814d4a650b055fc5bd2b2ed0fba3745189c),
before the readiness fix. It returned a real reply, passed verification and
preserved the three existing services and three running containers. Both
deterministic cross-process migration regressions also passed on that Mac:
a fresh database and an existing database with a pending migration. The live
  install logs do not prove migration overlap; those regressions provide the
  direct race coverage. PR 3766 has since merged with that tested commit as its
  head; the qualification used warm prerequisites and caches.

See the [workflow and validation](../skills/e2e-macos/SKILL.md). Merged in
[PR 5](https://github.com/nanocoai/nanoclaw-oss-dev-tools/pull/5) on 2026-09-11.
The [environment comparison](macos-test-environments.md) records why physical
Macs are the preferred path and MacinCloud is parked.

## e2e-wizard

**Entry points:** [exe-run.sh](../skills/e2e-exe-dev/scripts/exe-run.sh) with
`--interactive`, or [proxmox-wizard.py](../skills/e2e-wizard/scripts/proxmox-wizard.py),
with the matching lifecycle skill installed alongside `e2e-wizard`.

- Needs Python 3.10+, the lifecycle driver's prerequisites and either an
  authorized credential file or a live operator for a supported supervised auth
  method. The target installs the hash-pinned terminal emulator in a private venv.
- Drives Standard setup, a fresh agent for the selected offered provider, a local sandbox image and a
  retained terminal chat. It declines optional Echo and Slack browser offers and
  skips phone channels; a chosen supervised authentication flow is handled separately.
- Requires known active prompts, recorded choices, successful required steps and
  actual public-wizard completion. A random arithmetic answer must come from the
  retained agent; echoed input and the temporary ping agent do not prove it.
- Requires final verification plus independent checks that the running service
  uses the exact checkout entrypoint and that its CLI socket accepts a connection.
- Redacts credentials on the target before exporting rendered terminal text,
  choices, setup/runtime logs and a bounded container-status snapshot. The
  collector reports safe diagnostic codes while checking paths, hashes, identity
  and proof before accepting a local success. Failure retains the machine.
- Proxmox and direct wizard runs can use `--supervised-human-auth` for the
  discovered Codex `device` or Claude `subscription` method. They require the
  matching vault entry and a retained session whose effective provider resolves
  to the selected provider through NanoClaw's exact installed resolver, in
  addition to all normal acceptance checks.
- On 2026-09-13, Codex device pairing passed in a fresh Debian 13.6 Proxmox LXC
  using tooling `7200211d9c86592a25e66694451b6e503202f014`, NanoClaw core
  `3f9ed607b7e7a4872747295f75286f1c377d7c33`, and [PR #3792](https://github.com/nanocoai/nanoclaw/pull/3792)
  payload `b6faffcfd83ee477ed8f477724985c78cce450eb`. All 22 provider files matched,
  the pinned `@openai/codex@0.146.0` fallback ran without a host CLI, and device
  sign-in, vault registration, a retained Codex-agent reply, systemd user service
  and socket checks passed. An earlier harness timeout remains a failed,
  retained attempt. Later split-chunk and owning-remote transport fixes have
  offline coverage; they were not part of this recorded live run.
- On 2026-09-13, Claude subscription sign-in passed through a shared visible
  real PTY on a fresh Debian 13.6 Proxmox LXC at NanoClaw core
  `3f9ed607b7e7a4872747295f75286f1c377d7c33`. A human completed authorization
  directly in the terminal; one Anthropic vault entry, the retained answer to
  `350 * 193`, saved session history, service identity and the CLI socket after
  logout were independently verified. Null group and session provider fields
  resolved to Claude through the installed resolver. This qualifies the visible
  manual public-wizard flow; the unattended handoff remains unqualified after
  two retained timeouts.
- On 2026-09-15, OpenCode passed twice on fresh exe.dev VMs at NanoClaw
  `1100f83f57e0b61b60efabea3ec4f8360535b7b4` with an `openai/<model>` id
  through an operator-supplied OpenAI-compatible endpoint and an API-key file.
  The endpoint and model are the operator's choice, not a harness default. The bundled
  payload (37 files) matched the source, the saved endpoint/model defaults and
  the retained agent's effective OpenCode provider were verified, and the agent
  answered the random arithmetic challenge. The first attempt's product setup
  succeeded but the harness verifier could not find `pnpm` installed by the
  child wizard; that run remains a retained failure. The corrected verifier
  passed on the second fresh VM. OpenRouter/DeepSeek backends and the Proxmox
  and Windows launchers have offline coverage only.
- Live qualification: a fresh unprivileged Debian 13 LXC under Proxmox VE 9.2.18
  passed against an earlier candidate of NanoClaw
  [PR 3767](https://github.com/nanocoai/nanoclaw/pull/3767), exact commit
  [`705c6b9e627ac36a8b4bbc280e5804c6debf9a25`](https://github.com/nanocoai/nanoclaw/commit/705c6b9e627ac36a8b4bbc280e5804c6debf9a25).
  The tested candidate completed in about five minutes, with a retained-agent
  reply and a verified systemd user service. No product step failed or was
  repaired by the driver; a nonfatal `pkttyagent` diagnostic was recorded. Later
  registry-ref and directory-collision follow-ups in the merged PR were not part
  of that run.
- Additional live evidence: a fresh Ubuntu run exercised NanoClaw
  [PR 3768](https://github.com/nanocoai/nanoclaw/pull/3768) at exact commit
  [`8d75571abfcf2486ceeef9b302ddc21ca69955b1`](https://github.com/nanocoai/nanoclaw/commit/8d75571abfcf2486ceeef9b302ddc21ca69955b1).
  A task-only adapter verified that the nohup process survived terminal exit;
  the distributed harness now carries equivalent owned-launcher, PID, entrypoint
  and socket checks with offline coverage. The merged PR's later channel-restart
  change was not exercised by that run.

See the [scenario, acceptance rules and compatibility evidence](../skills/e2e-wizard/SKILL.md).
A passing headless installation does not qualify the interactive wizard.

## e2e-windows

**Entry point:** [windows-run.py](../skills/e2e-windows/scripts/windows-run.py),
run inside the dedicated WSL2 distribution from the NanoClaw checkout.

- Needs a regular Linux user, ext4 checkout/home/results, systemd user session,
  Python/venv, noninteractive test sudo and Windows interoperability.
- Requires Docker Desktop integration for that distribution. Compares its local
  Linux engine with the Windows named-pipe engine and tests a read-only mount
  from the Linux home before starting the unchanged wizard.
- `--preflight-only` qualifies the environment without credentials or product
  setup. Only a validated wizard result can report an installation pass.
- Retains the distribution, service and private working evidence. Raw product
  logs are never the shareable artifact export.
- Cleanup is offered only for an explicitly identified disposable WSL/VM clone
  after evidence persistence and ownership/controller checks; never for the host.
- The [preparation reference](../skills/e2e-windows/references/windows-proxmox.md)
  records Windows template capture, clone identity, per-user WSL registration,
  Docker integration and credential handling. VM provisioning is not automated
  by this runner; the tested template still needs first-boot console assistance.
- Release requalification on **2026-09-14** used NanoClaw
  [`3f9ed607`](https://github.com/nanocoai/nanoclaw/commit/3f9ed607b7e7a4872747295f75286f1c377d7c33)
  and dev-tools candidate
  [`075f7428`](https://github.com/nanocoai/nanoclaw-oss-dev-tools/commit/075f74285d8e0cc3f54a84e7012d0d09c1e36646).
  Windows 11 Enterprise Evaluation 25H2, Ubuntu 24.04.4, WSL 2.7.14,
  Docker Desktop 4.90.0 and engine 29.7.2 passed the unchanged public wizard in
  about 10 minutes with Claude OAuth, a retained-agent reply, final verification,
  service/socket proof and an independently revalidated 20-file sanitized export.
  The same installed agent replied after the setup terminal closed. A WSL-only
  termination still left the service/socket unavailable after 60 seconds. After
  a full Windows reboot, Docker Desktop was absent at 132 seconds; launching it
  normally restored the same engine, the packaged environment qualification,
  the service/socket and a fresh retained-agent reply without a product repair.
  This qualifies manual full-reboot recovery for that exact configuration, while
  WSL-only and automatic Docker-start recovery remain unqualified.


## e2e-triage

**Entry point:** [SKILL.md](../skills/e2e-triage/SKILL.md), invoked by the testing
agent after an unexpected failure or directly against a retained run. Install it
alongside the E2E skill; the plugin includes all nine. This workflow adds no
runtime or GitHub dependency to the shell/Python test drivers.

- Captures distinct failures without rewriting installation/recovery outcomes.
- Searches issues and PRs in all states, follows relevant linked work and separates
  a matching report, a related symptom, an unmerged candidate and a verified merge.
  Checks candidate mergeability, target-base revision and relevant CI; conflicting
  PRs require a rebase or port and review before testing against the current base.
- Saves bounded search scope and uncertainties; unavailable or truncated searches
  are incomplete, never proof that a bug is new.
- Reads current target issue forms, contribution requirements and reporting routes
  before drafting. Required fields and individual attestations must be supported;
  it never silently files a generic body in place of a required form.
- Produces final recommendations and local issue/comment drafts; public submission
  requires authorization for the concrete content and destination.

Validation on **2026-09-12** used the retained Windows run at NanoClaw
`705c6b9e627ac36a8b4bbc280e5804c6debf9a25`, without rerunning or repairing it.
Live research found related [issue 2513](https://github.com/nanocoai/nanoclaw/issues/2513),
and [PR 3027](https://github.com/nanocoai/nanoclaw/pull/3027) describing the same WSL2
host-side `EISDIR` failure. That PR was open, unmerged and conflicting on follow-up
inspection; its candidate fix was not tested here. The recommendation is to
rebase or port the applicable change, review it, then test the resulting revision
against current `main`. [PR 2854](https://github.com/nanocoai/nanoclaw/pull/2854) was closed
without merge, so it was not treated as an available fix. The current NanoClaw
bug form and contribution rules were inspected at
`74224f62a6c08418acccc727114ab02f92e403bf`, and a local field mapping was prepared.
This is manual workflow validation with live reads, not a fresh E2E installation
or automated proof of future agent behavior. No issue or comment was posted.

## typesafe-docs-drift

**Entry point:** [typesafe-docs-drift.py](../skills/typesafe-docs-drift/scripts/typesafe-docs-drift.py),
run from a NanoClaw checkout (`--code`, default `.`) with a nanoclaw-docs
checkout (`--docs`).

- Needs Python 3.10+ and, for live mode, `TYPESAFE_API_KEY` exported in the
  shell. Standard library only; no SDK.
- Extracts facts with code (57 `ncl`, 18 env, 8 container-config, 53 skills,
  3 gateway, 3 timestamp facts on NanoClaw `6e5008fe`), finds the top 3 doc
  sections per fact with a BM25 index, and sends one fan-out request per pair
  asking `contradicts`, `covers` and `staleness`.
- Gates on the nouls (0.7) and the score confidence (0.5), all overridable,
  and prints one ranked row per fact with the doc page and heading, the
  numbers and the code evidence; a per-area summary reports tokens and wall
  time.
- `--facts-only` and `--plan` show the deterministic half without a key;
  `--fixture` replays recorded facts, sections and responses; `--record`
  saves a live run in that format. Raw answers go to a gitignored directory.
- Writes nothing anywhere. Details: [typesafe-docs-drift.md](typesafe-docs-drift.md).

Validation: fixture replay through the full pipeline and the mocked tests in
`tests/test_typesafe_docs_drift.py`, plus one live run on 2026-09-16 whose
sanitized summary is recorded on the detail page.

## typesafe-triage

**Entry point:** [typesafe-triage.py](../skills/typesafe-triage/scripts/typesafe-triage.py),
run from any directory; it needs no NanoClaw checkout.

- Needs Python 3.10+ and, for live mode, an authenticated `gh` plus
  `TYPESAFE_API_KEY` exported in the shell. Standard library only; no SDK.
- Fetches the 30 most recently updated open issues and 20 open PRs read-only
  (`gh api`), sends one fan-out request per item (area, kind, priority, and
  `needs_repro` for issues or `pr_ready` for PRs), and prints proposed labels
  with confidence, the existing labels and an AGREE / DISAGREE / NEW verdict.
- Gates on confidence (area and kind 0.6, priority 0.8, yes/no 0.7, all
  overridable) and proposes `triage/unresolved` below the gate instead of guessing.
- `--fixture` replays recorded items and responses offline; the shipped fixture
  holds five real items fetched on 2026-09-16 with hand-written answers.
  `--record` saves a live run in the same format. Raw answers go to a gitignored
  output directory.
- Writes nothing to GitHub. Details: [typesafe-triage.md](typesafe-triage.md).

Validation is offline only: fixture replay through the full pipeline and the
mocked-transport tests in `tests/test_typesafe_triage.py`. No live TypeSafe
request has been made from this repository yet.

## Installation and maintenance

Use the [README installation instructions](../README.md#1-install-the-skill).
The repository is portable Agent Skills format and also ships as the
`nanoclaw-e2e` Claude Code plugin. For a selected Proxmox or Mac skill, install
the companion `e2e-exe-dev` skill too, or use the plugin's full skill set. The
wizard requires its chosen `e2e-exe-dev` or `e2e-proxmox` lifecycle companion.
The driver expects the shared installer in the sibling skill directory unless
an explicit installer path is supplied.

The native-Mac delivery passed 86 offline tests, including 24 Mac-specific
tests. The readiness fix brought the suite to **88 tests**, with both new
regressions failing before the fix. Its
[hosted CI run](https://github.com/nanocoai/nanoclaw-oss-dev-tools/actions/runs/34616868865)
passed on Ubuntu and macOS. These checks simulate infrastructure and exercise
parsing, transport, ownership, timeouts and result validation; the live records
above provide the model/service proof. The current suite contains **205 tests**;
it passed in
[CI on Linux and macOS](https://github.com/nanocoai/nanoclaw-oss-dev-tools/actions/runs/34792207939).
Coverage includes
real PTY redraws, cancellation and process cleanup, strict proof rejection,
archive handling, runtime evidence and repository metadata contracts.

Recheck compatibility after NanoClaw setup changes. Record the tools revision,
full NanoClaw commit, target OS/architecture, access mode, result JSON and live
reply/service evidence for each additional environment. Keep reboot recovery,
upgrades and real-channel testing as explicit additional milestones.

## shared-terminal

**Entry points:** [server.py](../skills/shared-terminal/scripts/server.py) and
[control.py](../skills/shared-terminal/scripts/control.py).

- Needs macOS or Linux, Python 3.10+, Bash and a browser on the same host.
- Uses a loopback-only server, private per-session access files and one real PTY.
  The browser and agent share its shell; no raw transcript is written to disk.
- Real HTTP/PTY regression tests cover terminal resizing, cookie access, SSE
  reconnection, guarded input and shutdown. CI runs the full suite on Linux and
  macOS; loopback startup also has coverage with name resolution unavailable.
- A separate macOS browser run verified direct typing, an interactive Python
  prompt, an agent-started prompt answered in the browser, and owned-session
  shutdown. The existing operator terminal stayed open.
- Bundled xterm assets include exact versions, SHA-256 digests and MIT licenses;
  Python emulator dependencies are installed from a hash-pinned requirements file.
- Authentication remains a human handoff. The terminal grants no additional
  permission to log in, publish, provision or remove resources.

See the [workflow](../skills/shared-terminal/SKILL.md) for private access,
sensitive-screen handling and session lifetime. Added in plugin **0.10.0**.
