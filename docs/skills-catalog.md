# NanoClaw OSS Dev Tools: skill catalog

Verified: **2026-09-11**. The repository contains **three skills**. All are merged
into [nanocoai/nanoclaw-oss-dev-tools](https://github.com/nanocoai/nanoclaw-oss-dev-tools)
main; the merged plugin manifest is version **0.4.1**. The main revision checked
for this record was
[`6b500c042f6999e8fa0918a83c7145bf615d3f88`](https://github.com/nanocoai/nanoclaw-oss-dev-tools/commit/6b500c042f6999e8fa0918a83c7145bf615d3f88).
This records repository availability. Installed skill copies must be updated
separately, and the manifest version does not imply a tagged GitHub release.

The host-readiness fix merged in
[PR 6](https://github.com/nanocoai/nanoclaw-oss-dev-tools/pull/6) and includes the
successful SSH qualification described below.

## Choose a skill

| Skill | Target and purpose | Live evidence | Main remaining gap |
|---|---|---|---|
| [e2e-exe-dev](../skills/e2e-exe-dev/SKILL.md) | Fresh or cached exe.dev Debian/Ubuntu VM; portable shared installer also usable on a prepared Linux host or CI runner. | Fresh and cached installs passed on 2026-09-10 with real replies and final verification. | Evidence used the nohup service fallback; do not infer every Linux service mode or arbitrary NanoClaw ref is qualified. |
| [e2e-proxmox](../skills/e2e-proxmox/SKILL.md) | Fresh unprivileged Debian 13 amd64 LXC, managed through SSH to the Proxmox node. | Fresh install passed on 2026-09-11 with a real reply and a running systemd user service. | Reboot recovery, cached clones and the transactional updater were not tested. |
| [e2e-macos](../skills/e2e-macos/SKILL.md) | Existing native Mac, local or SSH, using a new persistent checkout and its own LaunchAgent. | Local arm64 passed with 0.4.0; SSH on M4 Pro passed on 2026-09-11 with the readiness fix now merged in 0.4.1. Both proved a real reply, service and preservation. | Intel/older macOS, cold prerequisites and reboot/logout recovery remain unqualified. |

The shared baseline for all three skills tested NanoClaw
[`74224f62a6c08418acccc727114ab02f92e403bf`](https://github.com/nanocoai/nanoclaw/commit/74224f62a6c08418acccc727114ab02f92e403bf).
The later Mac run against the upstream migration fix is recorded separately
below. These are exact-configuration results, not blanket compatibility claims.

## Shared workflow and acceptance

Run a skill from the **NanoClaw checkout under test**, not from this tools
repository. The drivers resolve the requested ref locally and fetch the exact
commit on the target. Local-only commits and uncommitted edits are not uploaded.
The chosen commit must be fetchable from the configured repository.

The shared [e2e-install.sh](../skills/e2e-exe-dev/scripts/e2e-install.sh) drives
NanoClaw's existing setup steps for bootstrap, Docker/OneCLI readiness, auth,
agent image, service, CLI agent, model ping and final verification. The Mac path
uses a dedicated LaunchAgent helper to preserve other installations.

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
  Anthropic credential file. The guest is Debian/Ubuntu with sudo.
- Supports an exact ref, a new VM, a cached base, an optional snapshot and a
  local result file. The driver confirms allocation/copy identity before use.
- Failed runs are retained. Removal after success is opt-in with the documented
  flag; it is not the default.
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
direct race coverage. This is evidence for that exact upstream PR commit,
which was still open when this record was written, with warm prerequisites
and caches.

See the [workflow and validation](../skills/e2e-macos/SKILL.md). Merged in
[PR 5](https://github.com/nanocoai/nanoclaw-oss-dev-tools/pull/5) on 2026-09-11.
The [environment comparison](macos-test-environments.md) records why physical
Macs are the preferred path and MacinCloud is parked.

## Installation and maintenance

Use the [README installation instructions](../README.md#1-install-the-skill).
The repository is portable Agent Skills format and also ships as the
`nanoclaw-e2e` Claude Code plugin. For a selected Proxmox or Mac skill, install
the companion `e2e-exe-dev` skill too, or use the plugin containing all three.
The driver expects the shared installer in the sibling skill directory unless
an explicit installer path is supplied.

The native-Mac delivery passed 86 offline tests, including 24 Mac-specific
tests. The merged readiness fix brings the suite to **88 tests**, with both new
regressions failing before the fix. Its
[hosted CI run](https://github.com/nanocoai/nanoclaw-oss-dev-tools/actions/runs/34616868865)
passed on Ubuntu and macOS. These checks simulate infrastructure and exercise
parsing, transport, ownership, timeouts and result validation; the live records
above provide the model/service proof.

Recheck compatibility after NanoClaw setup changes. Record the tools revision,
full NanoClaw commit, target OS/architecture, access mode, result JSON and live
reply/service evidence for each additional environment. Keep reboot recovery,
upgrades and real-channel testing as explicit additional milestones.
