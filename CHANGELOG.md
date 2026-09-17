# Changelog

Notable changes to NanoClaw OSS Dev Tools are recorded here, grouped by plugin
version and following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

The initial history through 0.4.0 was reconstructed from manifest versions and
commits. Those entries link to source history; they do not imply that GitHub
releases or tags were published for those versions. Versions 0.5.0 through 0.9.0
were development manifest milestones; their changes are consolidated in 0.10.0
instead of being backfilled as releases.

## Unreleased

Development milestone (plugin 0.12.1); supersedes the unreleased 0.11.0
milestone and is not yet released.

### Added

- `typesafe-triage` `--apply` flag: adds only the two label questions that
  measured 100% agreement on a live run. The ungated `kind/*` proposal is added
  when the item has no existing `kind/*` label; on issues, `triage/needs-repro`
  is added when `needs_repro` resolved yes (p >= the noul threshold) and the
  label is not already present. Never removes a label, never touches
  `area/*`/`priority/*`/`pr_ready`, never comments. Labels are added through
  `gh issue edit --add-label` / `gh pr edit --add-label` via the same mockable
  subprocess helper pattern as the read-only `gh api` calls, with a live label
  recheck immediately before each write so a label a human added between fetch
  and write is respected instead of duplicated; a failed write stops the run
  as `PartialFailure`, keeping earlier items' results and applied labels.
  Refused together with `--fixture` (a frozen snapshot, never live state). The
  table gains an `Applied` column and the summary an applied count; `--json`
  includes an `applied` list per item.
- `typesafe-triage` `--since <ISO timestamp>` (only consider items created
  strictly after it) and `--only-unlabeled` (skip items that already carry a
  `kind/*` label), so a scheduled run can triage only what is new and
  unlabeled.
- `.github/workflows/typesafe-triage.yml`: runs the skill against
  `nanocoai/nanoclaw` (overridable) on `workflow_dispatch` (with an `apply`
  input) and every 6 hours on a schedule. The scheduled run always passes
  `--only-unlabeled` and only adds `--apply` when the `TYPESAFE_TRIAGE_APPLY`
  repository variable is `"true"`; otherwise every run's table is uploaded as a
  workflow artifact. Requires the `TYPESAFE_API_KEY` and `TRIAGE_GH_TOKEN`
  secrets (the latter a token scoped to the target repo with `issues:write` and
  `pull-requests:write`, not the default `GITHUB_TOKEN`, since triage usually
  targets a different repo than the one the workflow runs in). Rollback: unset
  `TYPESAFE_TRIAGE_APPLY`; already-applied labels are additive and are not
  undone by turning it off.
- `typesafe-triage` skill: a dry-run label triage of open nanocoai/nanoclaw
  issues and PRs through the TypeSafe System One API. One fan-out request per
  item (area, kind, priority, needs-repro or PR readiness), a confidence gate
  that falls back to `triage/unresolved`, AGREE/DISAGREE/NEW against existing
  labels, a summary with agreement rates, token usage and wall time, raw answers
  saved under a gitignored directory, and `--fixture` replay for offline runs.
  Reads `TYPESAFE_API_KEY` from the environment only and never writes to GitHub.
- OpenCode public-wizard E2E for OpenRouter, DeepSeek, and custom/self-hosted
  API-key endpoints across exe.dev, Proxmox and Windows WSL2. Select the model
  with `--opencode-model`, the endpoint with `--opencode-base-url`, and its API
  format with optional `--opencode-provider` (custom defaults to `openai`).
- Drive custom endpoint credential/catalog prompts in the provider-owned order;
  preserve opaque API-key symbols and bind endpoint/provider settings to evidence.
- Discover bundled provider payloads and delegated OpenCode backend choices from
  the exact NanoClaw commit. Verify installed payload files, saved backend/model
  defaults and the retained agent's effective provider in sanitized evidence.

### Changed

- Headless installer follows NanoClaw's credential-gateway seam
  (nanocoai/nanoclaw#3815 onward): when the checkout has `setup/gateways/`,
  it runs `--step gateway <kind>` and `--step gateway-auth claude` instead of
  the removed `onecli` and `auth` steps, seeds the OneCLI vault the way the
  OneCLI skill does, or hands an API key / OAuth token to Iron Proxy through
  that step's environment. New `--gateway onecli|iron-proxy` driver flag and
  `NANOCLAW_E2E_GATEWAY`. Older refs keep the previous behaviour.

### Fixed

- Find Node, pnpm and OneCLI installed by the wizard in `~/.local/bin` (or pnpm
  in npm's global prefix) during post-wizard provider verification. The child
  wizard's PATH changes do not propagate to the parent harness.

OpenCode support has offline source, PTY and acceptance coverage. A live custom
endpoint run completed setup and returned a real model reply, but its
final harness check failed on the tool path above. The corrected verifier passed
a separate read-only recheck and a second fresh exe.dev run passed the full
public wizard, retained-agent reply, provider, service and evidence checks at
NanoClaw `1100f83f57e0b61b60efabea3ec4f8360535b7b4`. ChatGPT
subscription and keyless endpoints are not automated. Headless setup remains
Claude-only.

## 0.10.0 - 2026-09-14

### Fixed

- Report safe, actionable validation codes when wizard artifacts are malformed,
  stale, incomplete, corrupt, oversized or contain an unredacted credential.
- Keep the plugin manifest version, marketplace description and distributed-skill
  listings checked against the contributor README and skill catalog.
- Run the headless installer with the system shell as `sh`: a host that puts a
  foreign `sh` first on PATH (exe.dev images since 2026-09-09 ship
  `/exe.dev/bin/sh`, whose builtin `lsof` always exits 0) made NanoClaw's
  OneCLI step fail with a misleading "port already in use". The installer now
  drops that directory from PATH for the run and stops when no system shell
  exists; a note in the log says when it did so.
- Wait for the host's CLI socket before initializing the E2E agent, avoiding
  concurrent fresh-database migrations on older NanoClaw revisions. Stop before
  agent initialization or a model request if the host never becomes ready.

### Added

- `shared-terminal`: one real local PTY shared by browser and agent controls,
  with private session access, prompt guards, reconnect recovery and bounded
  shutdown. Bundled terminal assets and Python dependencies are pinned; actual
  HTTP/PTY tests cover the control boundary (plugin 0.10.0).

- Supervised public-wizard authentication for Codex device pairing and Claude
  subscription sign-in through the Proxmox and direct wizard entry points.
  Private handoffs carry browser links and authorization codes; the runner still
  requires vault, retained-provider, model-reply, service and sanitized-export
  evidence. Existing credential-file methods remain available (plugin 0.9.0).
  Codex device sign-in and the visible, manually driven Claude subscription
  wizard have retained live acceptance runs; the unattended Claude handoff still
  awaits a successful live run. Retained-provider checks use the exact installed
  NanoClaw resolver, including Claude defaults and session overrides.
- Exact provider-payload transport for Proxmox wizard runs: fetch the selected
  payload commit, expose it through the normal registry remote, and verify the
  installed files before authentication and again before accepting a result.

- Provider-aware preflight for every full E2E workflow. A read-only helper
  discovers the exact revision's offered provider picker, then the selected
  provider's auth prompt/options from either that NanoClaw SHA or its fetched
  provider-payload SHA. Reports bind both source identities; unattended runners
  reject human-login and skip flows before provisioning (plugin 0.8.0).
- Sanitized, checksummed headless exe.dev evidence with setup/runtime logs,
  runtime state, run/provider/auth identity, dev-tools SHA and exact harness
  digest. Opt-in VM removal now requires validated local evidence, rechecks the
  run marker and inactive controller, verifies JSON-inventory absence, and
  writes a teardown receipt. Every platform documents evidence-first post-run
  retention and cleanup boundaries (plugin 0.8.0).

- `e2e-triage` and invocation instructions in all five E2E skills: bounded
  upstream issue/PR research, evidence-based matches, candidate conflict/CI
  checks, final recommendations and local drafts that follow the target
  repository's current issue forms. Test outcomes remain unchanged; public
  submission requires authorization (plugin 0.7.0).

- `e2e-windows` for a prepared Windows WSL2 distribution, with same-engine
  Docker Desktop proof, a Linux-home bind test, unchanged public-wizard reuse,
  validated artifacts and a Proxmox Windows preparation reference (plugin 0.6.0).
  The 0.10.0 candidate was requalified against current NanoClaw `main`: fresh
  setup, terminal-close inference and manual full-Windows-reboot recovery passed;
  WSL-only recovery and Docker Desktop autostart remain unqualified.

- Export sanitized NanoClaw runtime logs and a bounded container-status snapshot
  with wizard evidence, while retaining container log bodies on the private target.
- Verify the nohup fallback through its owned launcher and PID file, exact
  checkout entrypoint and CLI socket when Linux has no usable systemd service.
- Add failure-oriented troubleshooting tables for Proxmox and macOS, including
  lifecycle identity, Docker/gateway readiness, service logs and retry boundaries.
- A catalog of all distributed E2E skills, with prerequisites, entry points, exact
  live-test evidence and remaining coverage gaps.
- macOS test-environment notes covering physical hardware, VM and cloud options,
  the deferred MacinCloud evaluation and the merged readiness fix.
- `e2e-wizard`, an opt-in `--interactive` exe.dev mode and a Proxmox adapter
  for the public setup wizard, with a real PTY/emulator, strict retained-agent and service proof,
  sanitized evidence export and fail-closed VM retention.
- Offline wizard regressions for terminal redraws, reply proof, cancellation,
  timeouts, process cleanup, redaction, archive validation and lifecycle reuse.

- Two readiness-ordering regressions and live macOS SSH validation with a real
  agent reply and preserved shared services and gateway state.

## 0.4.0 - 2026-09-11

### Added

- `e2e-macos` for native NanoClaw installation tests on an existing Mac, locally
  or over SSH, with a fresh persistent checkout and explicit OneCLI gateway choice.
- A per-checkout LaunchAgent helper that preserves other services and the global
  `ncl` link, plus before/after checks for shared services, containers and credentials.
- Offline regressions for Mac transport, ownership, existing gateway protection,
  service isolation, exact source/result identity and retained failure evidence.
- Live local macOS 26.6.1 / arm64 evidence with a real CLI-agent reply,
  successful NanoClaw verification and shared-state preservation checks.

### Changed

- The shared E2E installer handles Mac Docker readiness separately from Linux,
  supports explicit gateway reuse without credential imports, and bounds the model
  probe using Python instead of requiring GNU `timeout`.

## 0.3.0 - 2026-09-11

### Added

- `e2e-proxmox`: a fresh unprivileged Debian 13 LXC driver using the shared
  NanoClaw installer, exact commit selection, guarded container ownership and
  a local result report. All test guests are retained for inspection.
- Offline Proxmox driver regressions, including uncertain creation, existing
  guest protection, credential transport and stale result rejection.
- Live Proxmox VE 9.2.18 / Debian 13 LXC evidence with a real model reply and
  successful verification, including regressions for DNS/APT readiness and
  Docker-group membership before systemd user-session startup.
- Contribution, security and community guidance, plus bug-report, feature-request
  and pull-request templates.
- Grouped weekly update proposals for pinned GitHub Actions dependencies.
- Shared editor and line-ending conventions.

### Changed

- Reorganized the README around installing, running and updating the skill, with
  explicit global versus project installation and links to live-test evidence.

## [0.2.4] - 2026-09-10

### Added

- CI runs shell syntax checks, plugin JSON validation and the offline regression
  suite on Linux and macOS.
- Four copy-response regressions, bringing the suite to 37 tests.
- Live compatibility evidence for fresh and cached installations at NanoClaw
  commit `74224f62a6c08418acccc727114ab02f92e403bf`.

### Fixed

- Accept current exe.dev copy responses using `name`, `source` and `ssh_host`,
  requiring the requested source and destination to match.
- Print snapshot responses so failed confirmation can be diagnosed.

## [0.2.3] - 2026-09-10

### Fixed

- Replace a previous local pass before preflight or VM work begins. If no matching
  installer report is available, record the driver failure and requested commit.
- Detect authentication errors in large ping replies and stderr without losing
  the match to a broken pipe.

## [0.2.2] - 2026-09-10

### Added

- Machine-readable installer results with the tested commit, phase, outcome and
  ping classification, plus local export through `--result-file`.
- Offline behavioral regression tests using real Git and shell commands with
  simulated VM and setup operations.

### Fixed

- Require a matching create/copy response before contacting a VM; remove the
  fallback that could adopt an existing VM by name.
- Test the exact requested commit, honor the requested repository on cached VMs,
  and reject tracked changes in a base checkout.
- Forward supported installer settings over SSH stdin and reject incomplete setup
  statuses or empty ping replies.

## [0.2.1] - 2026-09-10

### Changed

- Rename the repository and marketplace to `nanoclaw-oss-dev-tools` and update
  contributor-facing links.

## [0.2.0] - 2026-09-10

### Changed

- Move skills to the portable `skills/<name>/` Agent Skills layout.
- Make installation and resource paths usable across coding agents while retaining
  Claude Code plugin delivery.

## [0.1.0] - 2026-09-10

### Added

- Initial Claude Code plugin and marketplace packaging for the headless NanoClaw
  installer and exe.dev driver.
- Setup, CLI-agent ping and verification composed from NanoClaw's existing steps,
  with instructions from the first live exe.dev run.

[0.2.4]: https://github.com/nanocoai/nanoclaw-oss-dev-tools/compare/f56a09a34390d4d64a7ac99212c060f7ed6c9b18...efef015f0ddc54c2c2498cf44ababb7f3e1a958b
[0.2.3]: https://github.com/nanocoai/nanoclaw-oss-dev-tools/compare/63a6073588f06d11e873f82cd7f3029afccbc8ed...f56a09a34390d4d64a7ac99212c060f7ed6c9b18
[0.2.2]: https://github.com/nanocoai/nanoclaw-oss-dev-tools/compare/a10bbe32b828b324ab70d26e56f421fd479fc942...63a6073588f06d11e873f82cd7f3029afccbc8ed
[0.2.1]: https://github.com/nanocoai/nanoclaw-oss-dev-tools/compare/d0f998468fe5051b241bd1c72b4861f78026f9e0...a10bbe32b828b324ab70d26e56f421fd479fc942
[0.2.0]: https://github.com/nanocoai/nanoclaw-oss-dev-tools/compare/e0e90a71d4786598f528cfdacf05d6fe4292305a...d0f998468fe5051b241bd1c72b4861f78026f9e0
[0.1.0]: https://github.com/nanocoai/nanoclaw-oss-dev-tools/tree/e0e90a71d4786598f528cfdacf05d6fe4292305a
