# Contributing

This repository holds portable development and testing tools for NanoClaw OSS
contributors. Small, focused fixes and new skills that solve a concrete contributor
problem are welcome. Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Where to start

- Report tooling bugs or propose a skill through the [issue forms](https://github.com/nanocoai/nanoclaw-oss-dev-tools/issues/new/choose).
- For a substantial new tool, describe the contributor task and proposed scope in
  an issue before building it.
- NanoClaw runtime and setup changes belong in [NanoClaw](https://github.com/nanocoai/nanoclaw).
  Link related upstream issues or PRs when a tooling change depends on them.
- Report vulnerabilities privately using [SECURITY.md](SECURITY.md).

## Repository layout

```text
skills/<name>/SKILL.md       Skill instructions and source references
skills/<name>/scripts/      Portable scripts used by that skill
tests/                      Offline behavioral regression tests
.claude-plugin/             Claude Code plugin and marketplace metadata
.github/                    CI, dependency updates and contribution templates
```

Skills run from a NanoClaw checkout; tests run from this repository. Resolve skill
resources relative to their `SKILL.md`, rather than assuming the two checkouts
share a directory or relying on one agent's environment variables.

## Local checks

The regression suite uses Python, Bash and Git. Wizard tests also use the
hash-pinned terminal emulator dependency. CI uses
Python 3.12 on Linux and macOS. A local Unix socket must be permitted by your
execution environment.

From this repository's root:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r skills/e2e-wizard/requirements.txt
for script in skills/*/scripts/*.sh; do bash -n "$script" || exit; done
python3 -m unittest discover -s tests -v
git diff --check
```

CI also parses the plugin manifests as JSON. GitHub Actions dependencies are
pinned to commits; Dependabot groups their weekly update proposals into one PR.

## Changing or adding a skill

1. Put a new skill under `skills/<name>/` using the [Agent Skills format](https://agentskills.io/specification).
   Keep its `SKILL.md` focused and under 500 lines.
2. Cite the NanoClaw source files and commit whose behavior the skill drives.
   Re-check those contracts when `setup/` or the invoked scripts change.
3. Keep shell commands portable and non-interactive. Read credentials from local
   files or documented environment variables, and never print or commit them.
4. For behavior changes, add a regression that reproduces the failure or verifies
   the changed behavior. Use fixtures for offline tests; keep live testing explicit.
5. Record relevant changes under `Unreleased` in [CHANGELOG.md](CHANGELOG.md).
   Update the plugin version when changing a distributed skill or plugin manifest.

Changes to VM lifecycle, setup sequencing or model-reply classification should
include live evidence when possible: the tested NanoClaw commit, phase, exit code
and sanitized result. Clearly distinguish offline tests from live installation
and inference. Documentation-only changes need relevant link and example checks.

## Pull requests

Explain the problem, the resulting behavior and how you verified it. Keep the PR
focused enough to review independently. Use the provided template, link related
issues and call out any compatibility limitations. Remove credentials, private
hostnames and unrelated logs from shared evidence.

## Versioning and changelog

The plugin version lives in `.claude-plugin/plugin.json`. Keep it synchronized with
any versioned changelog entry. Repository-only documentation, community files and
CI maintenance can stay under `Unreleased` without a plugin version bump.

For a release, prepare the version and dated changelog entry in a PR. Once it is
merged and CI passes, a maintainer can tag that exact commit and publish GitHub
release notes from the changelog. Do not describe an untagged commit as a published
release. The initial historical entries link to the commits that recorded each
plugin version.
