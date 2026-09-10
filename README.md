# NanoClaw OSS Dev Tools

[![CI](https://github.com/nanocoai/nanoclaw-oss-dev-tools/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/nanocoai/nanoclaw-oss-dev-tools/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Portable development and testing skills for [NanoClaw](https://github.com/nanocoai/nanoclaw) contributors.

Reproduce a clean install, test a branch on a real machine, and check that a
message reaches the agent and gets a reply. The tools drive NanoClaw's existing
setup steps and record the exact commit tested.

[Skill reference](skills/e2e-exe-dev/SKILL.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

## What's included

| Skill | Use it for |
|---|---|
| [e2e-exe-dev](skills/e2e-exe-dev/SKILL.md) | A headless NanoClaw install and real model ping on a fresh machine, or a repeat run from a cached exe.dev VM. |

Each skill follows the [Agent Skills format](https://agentskills.io). Use it with
Codex, Claude Code, OpenCode, or another agent that supports the format. The
installer can also run directly on a Debian/Ubuntu machine or CI runner.

## Quick start

### 1. Install the skill

With Node.js and npm available, install for your user account and choose your
agent when prompted:

```bash
npx skills add nanocoai/nanoclaw-oss-dev-tools --global --skill e2e-exe-dev
```

`--global` makes the skill available across checkouts. Omit it to install into
one project. The [skills CLI](https://github.com/vercel-labs/skills) supports both
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
- An Anthropic API key or OAuth token in a local file, normally
  `~/.nanoclaw-e2e/anthropic_key`, with permissions `0600`.
- A commit that the VM can fetch from the checkout's repository.

The target machine must be Debian/Ubuntu with sudo access. The installer handles
Node.js, pnpm, Docker, OneCLI and the agent image. See the
[prerequisites](skills/e2e-exe-dev/SKILL.md#prerequisites) for details.

### 3. Run a test

In Codex, ask:

```text
$e2e-exe-dev Test this NanoClaw checkout on a fresh exe.dev VM and save the result locally.
```

In other agents, ask them to use the `e2e-exe-dev` skill for the same task.
For shell commands, cached VMs, snapshots and standalone installs, follow the
[workflow](skills/e2e-exe-dev/SKILL.md#workflow).

The driver resolves the requested ref to an exact commit. A passing installer
result includes a real agent reply and successful service verification. Results
are written to `logs/e2e/result.json` on the target; `--result-file` saves a local
report. Failed runs retain their VM for inspection. Snapshot and removal errors
are reported separately from the completed installer result.

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

## Update

For a global skills CLI installation:

```bash
npx skills update e2e-exe-dev --global
```

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
