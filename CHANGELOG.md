# Changelog

Notable changes to NanoClaw OSS Dev Tools are recorded here, grouped by plugin
version and following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

The initial history below was reconstructed from manifest versions and commits.
Those entries link to source history; they do not imply that GitHub releases or
tags were published for those versions.

## Unreleased

### Fixed

- Wait for the host's CLI socket before initializing the E2E agent, avoiding
  concurrent fresh-database migrations on older NanoClaw revisions. Stop before
  agent initialization or a model request if the host never becomes ready.

### Added

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
