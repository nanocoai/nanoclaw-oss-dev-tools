# nanoclaw-oss-dev-tools

Portable development and testing tools for contributors to [NanoClaw OSS](https://github.com/nanocoai/nanoclaw). Shipped as [Agent Skills](https://agentskills.io) that install into any coding agent (Codex, OpenCode, Pi, Cursor, Claude Code, …).

## Install

Skills here follow the [Agent Skills](https://agentskills.io) format, so any agent that reads `SKILL.md` can use them.

**Any agent** (Codex, OpenCode, Pi, Cursor, Claude Code, …) — the [`skills`](https://github.com/vercel-labs/skills) CLI symlinks them into each agent's skill directory:

```
npx skills add nanocoai/nanoclaw-oss-dev-tools        # pick agents and skills interactively
npx skills add nanocoai/nanoclaw-oss-dev-tools --all  # every skill, every agent
```

**Claude Code** can also take it as a plugin marketplace:

```
/plugin marketplace add nanocoai/nanoclaw-oss-dev-tools
/plugin install nanoclaw-e2e@nanoclaw-oss-dev-tools
```

Either way the skills then apply in every NanoClaw checkout, worktree or fork on that machine. Update with `npx skills update` or `/plugin marketplace update nanoclaw-oss-dev-tools`.

In Codex, use `$e2e-exe-dev` in a task opened on the NanoClaw checkout you want
tested, or ask to test that checkout end to end. The skill's installed
directory is separate from that checkout; its instructions explain how to
invoke the scripts by their full path.

## Skills

| Plugin | Skill | What it does |
|---|---|---|
| `nanoclaw-e2e` | `e2e-exe-dev` (Claude Code: `/nanoclaw-e2e:e2e-exe-dev`) | Headless NanoClaw install on a fresh machine, composed from the setup wizard's own steps, ending in the wizard's ping round-trip. Runs on any Debian/Ubuntu host or CI runner; the bundled driver provisions an [exe.dev](https://exe.dev) VM for you if you have an account. |

## Conventions

- **No secrets in this repo, ever.** Tools read credentials from files on the operator's machine (the e2e driver reads `~/.nanoclaw-e2e/anthropic_key`) and never echo them.
- **Works anywhere first, exe.dev second.** A tool that needs a machine must run on a plain Debian/Ubuntu box; exe.dev is the convenient path, not a requirement.
- **Verified against the NanoClaw source it drives.** Each skill's `SKILL.md` cites the file that defines every command it calls; re-verify after `setup/` changes — it moves fast.
- **Portable first.** Reference files relative to the skill directory, never an agent-specific variable; keep `SKILL.md` under 500 lines. New tool = new directory under `skills/`; bump `version` in `.claude-plugin/plugin.json` on every change (Claude Code users only receive updates when it changes).

## Layout

```
skills/<skill>/SKILL.md            the skill, Agent Skills format
skills/<skill>/scripts/            its code
.claude-plugin/plugin.json         Claude Code plugin manifest (points at ./skills/)
.claude-plugin/marketplace.json    Claude Code marketplace catalog
AGENTS.md                          orientation for agents opening this repo
tests/test_e2e.py                  offline driver and installer regression tests
```

## Development checks

```bash
for script in skills/*/scripts/*.sh; do bash -n "$script"; done
python3 -m unittest discover -s tests -v
```

Tests use temporary local Git repositories and simulated SSH, Docker and wizard
commands, with no real credentials or VM provisioning. A temporary Unix socket
requires an environment that permits local socket creation. Live setup
compatibility and the last reviewed NanoClaw SHA are recorded in the skill.

The [CI workflow](.github/workflows/ci.yml) runs shell syntax checks, plugin
manifest validation and the offline regression suite on Linux and macOS for
every pull request and push to `main`. It uses Python 3.12 and needs no secrets
or exe.dev account. Live VM installation and inference remain a separate check.

## License

MIT — see [LICENSE](LICENSE).
