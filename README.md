# nanoclaw-dev-tools

Tooling for people who work *on* [NanoClaw](https://github.com/nanocoai/nanoclaw) — contributors, maintainers, the core team — shipped as portable [Agent Skills](https://agentskills.io) that install into any coding agent (Codex, OpenCode, Pi, Cursor, Claude Code, …). Nothing here ships to NanoClaw users; it stays out of the product tree so it never conflicts with your PR.

## Install

Skills here follow the [Agent Skills](https://agentskills.io) format, so any agent that reads `SKILL.md` can use them.

**Any agent** (Codex, OpenCode, Pi, Cursor, Claude Code, …) — the [`skills`](https://github.com/vercel-labs/skills) CLI symlinks them into each agent's skill directory:

```
npx skills add nanocoai/nanoclaw-dev-tools        # pick agents and skills interactively
npx skills add nanocoai/nanoclaw-dev-tools --all  # every skill, every agent
```

**Claude Code** can also take it as a plugin marketplace:

```
/plugin marketplace add nanocoai/nanoclaw-dev-tools
/plugin install nanoclaw-e2e@nanoclaw-dev-tools
```

Either way the skills then apply in every NanoClaw checkout, worktree or fork on that machine. Update with `npx skills update` or `/plugin marketplace update nanoclaw-dev-tools`.

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
```

## License

MIT — see [LICENSE](LICENSE).
