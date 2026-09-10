# nanoclaw-dev-tools

Tooling for people who work on NanoClaw, shipped as portable skills.

- Skills live in `skills/<name>/` in the Agent Skills format (`SKILL.md` + `scripts/`).
- Each skill is meant to run from the root of a NanoClaw checkout and cites the NanoClaw source it drives; re-verify after `setup/` changes.
- No secrets in this repo. Tools read credentials from files on the operator's machine and never echo them.
- To add a tool: new directory under `skills/`, relative paths only, then bump `version` in `.claude-plugin/plugin.json`.
