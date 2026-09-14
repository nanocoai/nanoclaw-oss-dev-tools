# NanoClaw OSS Dev Tools

[![CI](https://github.com/nanocoai/nanoclaw-oss-dev-tools/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/nanocoai/nanoclaw-oss-dev-tools/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Portable development and testing skills for [NanoClaw](https://github.com/nanocoai/nanoclaw) contributors.

Reproduce a clean install, test a branch on a real machine, and check that a
message reaches the agent and gets a reply. The tools drive NanoClaw's existing
setup steps and record the exact commit tested.
Before provisioning, the E2E workflows discover the provider picker and the
selected provider's authentication choices from that exact Git revision.

[Skills](#whats-included) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

## What's included

| Skill | Use it for |
|---|---|
| [e2e-exe-dev](skills/e2e-exe-dev/SKILL.md) | A headless NanoClaw install and real model ping on a fresh machine, or a repeat run from a cached exe.dev VM. |
| [e2e-proxmox](skills/e2e-proxmox/SKILL.md) | The same install and model ping in a new unprivileged Debian 13 LXC on your Proxmox host, with a retained guest and local report. |
| [e2e-macos](skills/e2e-macos/SKILL.md) | A native install and model ping on an existing Mac, locally or over SSH, with a separate checkout and preservation checks for existing services and OneCLI. |
| [e2e-wizard](skills/e2e-wizard/SKILL.md) | Drive the real public setup wizard through a PTY on a fresh exe.dev VM or Proxmox LXC, with a retained agent reply, service proof and sanitized evidence. |
| [e2e-windows](skills/e2e-windows/SKILL.md) | Qualify Windows WSL2 with the local Docker Desktop engine, then run the public wizard with validated reply, service and sanitized evidence. |
| [e2e-triage](skills/e2e-triage/SKILL.md) | Research unexpected E2E failures against upstream issues and PRs, recommend next actions, and prepare drafts using the target repository’s current issue forms. |
| [shared-terminal](skills/shared-terminal/SKILL.md) | One local browser terminal shared by the human and agent for supervised installers and interactive handoffs on macOS or Linux. |

Each skill follows the [Agent Skills format](https://agentskills.io). Use it with
Codex, Claude Code, OpenCode, or another agent that supports the format. The
installer can also run directly on a Debian/Ubuntu machine or CI runner.
See the [skill catalog](docs/skills-catalog.md) for entry points, prerequisites,
live validation and remaining coverage gaps.

## Quick start

The commands below start with exe.dev. For other targets, follow the
[Proxmox LXC workflow](skills/e2e-proxmox/SKILL.md), the
[local/SSH Mac workflow](skills/e2e-macos/SKILL.md), or the
[Windows WSL2 workflow](skills/e2e-windows/SKILL.md).
The [macOS test environment notes](docs/macos-test-environments.md) record the
physical-hardware plan and the deferred MacinCloud evaluation.
To test the public interactive installer, follow the
[wizard workflow](skills/e2e-wizard/SKILL.md); the commands below test headless setup.
The Windows workflow runs from a prepared WSL2 distribution and reuses the wizard
harness. For supervised Codex device or Claude subscription authentication on
macOS or Linux, [shared-terminal](skills/shared-terminal/SKILL.md) gives the
operator and agent one private local PTY.

### 1. Install the skill

With Node.js and npm available, install for your user account and choose your
agent when prompted:

```bash
npx skills add nanocoai/nanoclaw-oss-dev-tools --list
npx skills add nanocoai/nanoclaw-oss-dev-tools --global \
  --skill e2e-exe-dev \
  --skill e2e-wizard \
  --skill e2e-windows \
  --skill e2e-triage \
  --skill shared-terminal
```

`--global` makes the skill available across checkouts. Omit it to install into
one project. Multiple `--skill` options install a selected set in one run; add
`e2e-proxmox` or `e2e-macos` for those targets, or use `--all` to install every
skill. The [skills CLI](https://github.com/vercel-labs/skills) supports both
symlink and copy installation.

<details>
<summary>Install as a Claude Code plugin</summary>

Run these commands inside Claude Code:

```text
/plugin marketplace add nanocoai/nanoclaw-oss-dev-tools
/plugin install nanoclaw-e2e@nanoclaw-oss-dev-tools
```

Invoke it with `/nanoclaw-e2e:e2e-exe-dev`.

</details>

### 2. Prepare a NanoClaw checkout

Open the **NanoClaw checkout you want tested** in your agent. The skill's
installation directory and the checkout under test are separate locations.

For the exe.dev workflow, you need:

- Bash, Git, SSH and Python 3 on the machine launching the test.
- Working `ssh exe.dev` access.
- A credential matching the provider/auth method chosen from the exact revision;
  for Claude this is normally an Anthropic API key or OAuth token in
  `~/.nanoclaw-e2e/anthropic_key`, with permissions `0600`.
- A commit that the VM can fetch from the checkout's repository.

The target machine must be Debian/Ubuntu with sudo access. The installer handles
Node.js, pnpm, Docker, OneCLI and the agent image. See the
[prerequisites](skills/e2e-exe-dev/SKILL.md#prerequisites) for details.
The other drivers state their stricter launcher requirements separately:
Python 3.9+ for macOS and Python 3.10+ for Proxmox and the interactive wizard.
The [skill catalog](docs/skills-catalog.md#choose-a-skill) links each workflow.

Before a live run, the agent resolves the requested NanoClaw commit, runs
`e2e-wizard/scripts/provider-options.py --revision <sha>`, shows the offered
providers, and asks which one to test. It then shows that provider's own auth
prompt/options and asks which method to use. Installable-provider auth is read
from the exact fetched provider payload and records its separate SHA. Unattended
drivers accept only credential-file flows they can prove. The Proxmox and direct
wizard entry points also support supervised Codex device pairing and Claude
subscription sign-in with a live human handoff; `skip` cannot pass. See the
[provider-selection workflow](skills/e2e-wizard/SKILL.md#install-and-run).

### 3. Run a test

In Codex, ask:

```text
$e2e-exe-dev Test this NanoClaw checkout on a fresh exe.dev VM and save the result locally.
```

In other agents, ask them to use the `e2e-exe-dev` skill for the same task.
For shell commands, cached VMs, snapshots and standalone installs, follow the
[workflow](skills/e2e-exe-dev/SKILL.md#workflow).

From a prepared NanoClaw checkout inside Windows WSL2, ask:

```text
$e2e-windows Qualify this Windows/WSL2 environment, run the public wizard, and preserve the result locally.
```

The Windows skill checks that WSL and Windows use the same local Docker Desktop
engine before it reads a credential or starts the wizard.

The driver resolves the requested ref to an exact commit. A passing installer
result includes a real agent reply and successful service verification. Results
are written to `logs/e2e/result.json` on the target; `--result-file` saves a local
report and sanitized evidence. Failed runs retain their VM for inspection.
After evidence is durable, the skill offers to remove a disposable run-owned
target after a pass and recommends retaining unexpected failures for triage.
The exe.dev `--rm` path requires validated local evidence, rechecks ownership
and inactive control, verifies inventory absence, and writes a teardown receipt.
Other targets remain retained until separately requested cleanup completes the
same evidence-first checks. Snapshot and removal errors are reported separately
from the completed installer result.

## Failure triage

Install `e2e-triage` alongside whichever E2E skills you use, or install the plugin,
which includes all seven skills. Each E2E skill instructs the agent to invoke triage after
unexpected failures and include its findings at completion. The shell/Python
drivers themselves do not search trackers; direct driver runs can be triaged
later by asking the agent to use `e2e-triage` with the retained result.

Triage checks open and closed issues and PRs, explains whether a candidate
matches or is merely related, and recommends a specific next action. It preserves
the original test result. New issue drafts follow the destination's current
forms and contribution rules, including required fields and acknowledgments.
Public issues and comments require authorization for the reviewed action.

## Validation

[CI](.github/workflows/ci.yml) runs shell syntax checks, plugin JSON validation and
the offline regression suite on Linux and macOS. Those checks use simulated SSH
and setup commands, with no credentials or VMs.

Fresh and cached live installations were verified on **2026-09-10** at NanoClaw
commit [`74224f62`](https://github.com/nanocoai/nanoclaw/commit/74224f62a6c08418acccc727114ab02f92e403bf).
Both produced real model replies and passed final service verification. See the
[compatibility evidence](skills/e2e-exe-dev/SKILL.md#compatibility-evidence), including
the copy-response fix found during that run. Re-check compatibility when NanoClaw's
setup code changes.

A fresh unprivileged Proxmox LXC passed the same test on **2026-09-11**, using
Proxmox VE 9.2.18, Debian 13 and the same NanoClaw commit. See the
[Proxmox compatibility evidence](skills/e2e-proxmox/SKILL.md#compatibility-evidence)
for the exact host, guest and service configuration.

Native macOS installations passed locally and over SSH on **2026-09-11**, with
real model replies, running LaunchAgents and preserved existing services and
gateway state. The SSH run used the host-readiness fix now merged in plugin
version 0.4.1. See the [catalog's Mac evidence](docs/skills-catalog.md#e2e-macos)
for exact configurations, the separate upstream migration-fix validation and
remaining gaps.

The public wizard passed in a fresh Proxmox LXC. Later supervised runs verified
Codex device pairing and a visible Claude subscription login with retained-agent
replies, provider identity, service and socket checks. The unattended Claude
handoff remains unqualified after two bounded timeouts; see the
[wizard evidence](docs/skills-catalog.md#e2e-wizard).

Windows WSL2 and Docker Desktop passed a fresh public-wizard installation with a
real reply and validated evidence. A 2026-09-14 requalification against current
NanoClaw `main` also passed, including inference after the setup terminal closed.
Terminating only the WSL distribution still left the service/socket unavailable.
After a full Windows reboot, Docker Desktop did not start within 132 seconds;
launching it normally restored the same engine, the packaged environment
qualification, the service/socket and a fresh retained-agent reply without a
product repair. Automatic restart recovery remains unqualified; see the
[Windows evidence](docs/skills-catalog.md#e2e-windows).

The shared terminal passed real HTTP/PTY tests and a macOS browser handoff with
direct human and agent control of the same shell. Native Windows, WSL and remote
browser forwarding remain outside that qualification.

## Update

For a global skills CLI installation:

```bash
npx skills update --global
```

This updates all globally installed skills. Pass one or more skill names before
`--global` to update only that selected set.

For a Claude Code plugin installation:

```text
/plugin marketplace update nanoclaw-oss-dev-tools
```

## Contribute

Bug fixes, clearer instructions and new contributor tools are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) for the repository layout, local checks, skill
conventions and versioning. Use the [issue forms](https://github.com/nanocoai/nanoclaw-oss-dev-tools/issues/new/choose)
for tooling bugs and proposals.

For vulnerabilities, follow [SECURITY.md](SECURITY.md). This project follows the
NanoClaw community's [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE), maintained by [Nano Co](https://github.com/nanocoai).
