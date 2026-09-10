# nanoclaw-dev-tools

Tooling for people who work *on* [NanoClaw](https://github.com/nanocoai/nanoclaw) — contributors, maintainers, the core team — packaged as a [Claude Code plugin marketplace](https://code.claude.com/docs/en/plugin-marketplaces). Nothing here ships to NanoClaw users; it stays out of the product tree so it never conflicts with your PR.

## Install

Once, on your machine:

```
/plugin marketplace add nanocoai/nanoclaw-dev-tools
/plugin install nanoclaw-e2e@nanoclaw-dev-tools
```

The skills then appear in every NanoClaw checkout, worktree or fork on that machine. Update later with `/plugin marketplace update nanoclaw-dev-tools`.

## Plugins

| Plugin | Skill | What it does |
|---|---|---|
| `nanoclaw-e2e` | `/nanoclaw-e2e:e2e-exe-dev` | Headless NanoClaw install on a fresh machine, composed from the setup wizard's own steps, ending in the wizard's ping round-trip. Runs on any Debian/Ubuntu host or CI runner; the bundled driver provisions an [exe.dev](https://exe.dev) VM for you if you have an account. |

## Conventions

- **No secrets in this repo, ever.** Tools read credentials from files on the operator's machine (the e2e driver reads `~/.nanoclaw-e2e/anthropic_key`) and never echo them.
- **Works anywhere first, exe.dev second.** A tool that needs a machine must run on a plain Debian/Ubuntu box; exe.dev is the convenient path, not a requirement.
- **Verified against the NanoClaw source it drives.** Each skill's `SKILL.md` cites the file that defines every command it calls; re-verify after `setup/` changes — it moves fast.
- **One tool per plugin.** Add a directory under `plugins/`, a `.claude-plugin/plugin.json`, register it in `.claude-plugin/marketplace.json`, and bump the plugin's `version` on every change (users only receive updates when it changes).

## Layout

```
.claude-plugin/marketplace.json        catalog
plugins/<plugin>/.claude-plugin/plugin.json
plugins/<plugin>/skills/<skill>/SKILL.md
plugins/<plugin>/skills/<skill>/scripts/
```

## License

MIT — see [LICENSE](LICENSE).
