---
name: typesafe-docs-drift
description: Detect and rank drift between the NanoClaw code and the nanoclaw-docs Mintlify portal with the TypeSafe System One decision API. Deterministic code extracts facts (ncl verbs and flags, environment variables, container_configs and cli_scope, workspace skills, gateway selection, timestamp rules), lexical search picks candidate doc sections, and one fan-out request per fact x section asks contradicts / covers / staleness. Prints DRIFT, MISSING, UNSURE and OK rows with the model's numbers; writes nothing to either repository. The release gate (docs-drift-gate.py) adds the pull requests merged since the last stable tag that touch files the docs cite and never name them, the verified-against stamp check, a baseline for decided rows, and per-page briefs. Use to find doc pages that need a fix at a release, or replay the offline fixture without a key.
license: MIT
---

# TypeSafe docs drift (phase 1) and the release gate

Extract facts from a NanoClaw checkout with deterministic code, find the top
doc sections for each fact by lexical search over the docs portal, ask TypeSafe
one fan-out request per fact x section pair, gate on the answers, and print a
ranked drift report. The model only judges; it never writes text. Phase 2 (an
agent writing doc PRs) consumes the JSON this script saves and the briefs the
[release gate](#release-gate) writes from it.

Entry point: [typesafe-docs-drift.py](scripts/typesafe-docs-drift.py) (Python
3.10+, standard library only). Offline fixture:
[nanoclaw-sample.json](fixtures/nanoclaw-sample.json). Details of the facts,
questions, gate and JSON: [docs/typesafe-docs-drift.md](../../docs/typesafe-docs-drift.md).

## Sources this skill drives

- NanoClaw `src/cli/resources/*.ts` (`registerResource` definitions: plural,
  `operations`, `customOperations` with `access`, declared `args` or the
  `--flags` named in descriptions, column `enum`s), `src/config.ts` (`readEnvFile([...])` keys and defaults),
  `src/gateway-providers/index.ts` and `src/drivers/index.ts` (default kinds),
  `src/**/*.ts` `process.env.X || 'default'` reads, `.env.example`,
  `src/db/migrations/*.ts` (`container_configs` columns), the `--cli-scope`
  validation in `src/cli/resources/groups.ts`, `.claude/skills/*/SKILL.md`
  frontmatter, `docs/gateway-seam.md` (`## Selection` and
  `## How a gateway gets installed`, when present) and the `## Timestamps`
  section of `CLAUDE.md`. Verified against nanocoai/nanoclaw `6e5008fe`.
- nanoclaw-docs: `docs.json` navigation and the `.mdx` pages it lists, split
  on `##`/`###`/`####` headings (changelog pages excluded by default).
- TypeSafe HTTP API: `POST https://api.typesafe.ai/v1/systemone`, model
  `jev-latest`, question types `noul` and `score`
  ([API reference](https://docs.typesafe.ai/api.md),
  [confidence](https://docs.typesafe.ai/confidence.md),
  [speculative fan-out](https://docs.typesafe.ai/patterns/fan-out.md),
  [citation check cookbook](https://docs.typesafe.ai/cookbooks/citation_check.md)).

Re-check the extractors when `src/cli/crud.ts`, `src/config.ts` or the
migration layout changes.

## Prerequisites

- A NanoClaw checkout (`--code`, default: the current directory) and a
  nanoclaw-docs checkout (`--docs`). Both are read only.
- `TYPESAFE_API_KEY` exported in the shell (live mode only). The script reads
  it from the environment at request time, never from a file, and never prints
  it. Do not put the key in `.env`, a fixture, or any file inside a repository.

## Run

Offline demonstration, no key and no network:

```bash
python3 skills/typesafe-docs-drift/scripts/typesafe-docs-drift.py \
  --fixture skills/typesafe-docs-drift/fixtures/nanoclaw-sample.json
```

See the facts, or the fact x section pairs, without calling the API:

```bash
python3 skills/typesafe-docs-drift/scripts/typesafe-docs-drift.py --code . --facts-only
python3 skills/typesafe-docs-drift/scripts/typesafe-docs-drift.py --code . --docs ../nanoclaw-docs --plan
```

Live run from a NanoClaw checkout (about 140 facts, 430 requests, a few
minutes at the default concurrency of 4):

```bash
export TYPESAFE_API_KEY=...   # from your password manager, this shell only
python3 skills/typesafe-docs-drift/scripts/typesafe-docs-drift.py \
  --code . --docs ../nanoclaw-docs --record /tmp/drift-record.json
```

`--record` saves facts, sections and raw responses so the run can be replayed
with `--fixture`. Raw answers for every run land in
`skills/typesafe-docs-drift/output/` (gitignored) or `--output-dir`.

Flags: `--areas` (comma-separated subset of `ncl,env,container-config,skills,
gateway,timestamps`), `--limit-facts`, `--top-k` (3), `--include-changelog`,
`--contradicts-threshold` (0.7), `--covers-threshold` (0.7),
`--staleness-threshold` (0.5, score confidence), `--concurrency` (4),
`--model`, `--timeout`, `--json`, `--strict` (exit 1 when any pair failed after
retries; by default failures are listed under `partial` and the exit status is 0).

## Release gate

[docs-drift-gate.py](scripts/docs-drift-gate.py) is what to run when a NanoClaw
release is cut. It clones the release and the docs into a scratch directory
(URLs or local paths; neither source is changed) and writes a run directory
with `digest.md`, `gate.json` and one brief per page that needs work.

```bash
export TYPESAFE_API_KEY=...   # this shell only; or use --no-model / --fixture
python3 skills/typesafe-docs-drift/scripts/docs-drift-gate.py \
  --release v2026.10.1-rc.1 --baseline ~/path/to/docs-drift-baseline.json
```

It runs three checks:

1. **Pull request queue** (free). Pull requests merged since the previous
   stable tag (`--since` to override) that change a file some docs page cites,
   minus those whose number (four digits or more) the docs history or pages
   already name, and minus CI, test and release chores (a `!` breaking marker
   keeps a change in). A page is hit only by a change merged after the stamp
   that cites the file: a stamped commit that already contains the pull request
   was verified with it in place. This is the list to work from; pages are
   ranked by how many of a pull request's files they cite.
2. **Stamp staleness** (free). Each `{/* verified-against: ... @ <sha> */}`
   anchor is diffed against the release. On a busy tree most pages come out
   stale, so `gate.json` keeps the full map and the digest only counts it.
   Anchors whose SHA is not in the NanoClaw repository (the templates repo, a
   rewritten branch) are listed for a manual look.
3. **Detector** (the phase-1 script, needs the key unless `--fixture`). A
   DRIFT row whose best section never mentions the fact's terms is demoted to
   UNSURE. `--baseline` holds rows a person already decided:
   `{"entries": [{"fact": "env:X", "status": "held|false-positive|accepted", "reason": "...", "section": "optional#id"}]}`.
   An entry with a `section` holds only that section's DRIFT pair, so a second
   drifting section is still raised. A partial detector run is reported, and
   fails a `--strict` gate (exit 2).

Read `digest.md` first, then work the briefs in order as stacked **draft** docs
PRs: each brief lists the pull requests, the cited files they changed, the
detector rows and the docs repo's rules. Nothing in a brief is generated prose.
Bump a page's stamp only for claims re-verified at that SHA, and report product
bugs found on the way instead of documenting around them.

Flags: `--release` (required), `--since`, `--code-repo`, `--docs-repo`,
`--docs-ref` (main), `--output-dir`, `--baseline`, `--fixture`, `--no-model`,
`--max-briefs` (15), `--workdir`, `--keep`, `--json`, `--concurrency`,
`--strict` with `--max-undocumented` (exit 1 on new DRIFT rows or a longer
queue; the default exit status is 0). Exit 2 is an operational failure.

## Benchmark

[drift-benchmark.py](scripts/drift-benchmark.py) compares TypeSafe with a
prompt-and-parse LLM (`claude -p`, tools disabled) on the same recorded pairs:
speed, tokens, cost, parse failures, stability, agreement, and precision/recall
against [benchmark-gold.json](fixtures/benchmark-gold.json), whose labels were
written by an LLM-assisted reviewer. With no flags it is offline and scores the
recorded answers only. Method and results:
[docs/typesafe-docs-drift.md](../../docs/typesafe-docs-drift.md#benchmark-typesafe-versus-a-prompt-and-parse-llm).

## Read the output

One row per fact, ranked `DRIFT` (a section contradicts the fact, highest
contradiction first), `MISSING` (no candidate section covers the fact),
`UNSURE` (an answer sits between the gates), `OK` (a section covers the fact
and does not contradict it). Each row names the doc page and heading the
verdict rests on, the `contradicts` and `covers` probabilities, the staleness
score with its level (`?` when the score's confidence is below the gate) and
the code evidence as `file:line`. The summary counts verdicts per area and
reports tokens and wall time.

## Limits

- Detection only. Nothing is written to NanoClaw, nanoclaw-docs or GitHub.
- Detector rows are model judgments. On the first live release run (rc.1, 2026-10-21)
  none of four `DRIFT` rows was a docs error: three came from extractor bugs
  (enums built from `[...CONSTANT]` read as empty, a raw empty default), now
  fixed, and one from a SKILL.md description that the docs rightly contradict.
  Check every row against the code before editing a page.
- The detector covers six fact areas. Channel flows, setup behaviour and
  lifecycle prose are only reachable through the pull request queue and the
  stamps.
- A pull request counts as documented when its number appears in the docs
  history or pages; an open docs PR, or a page that describes the change
  without the number, leaves it in the queue.
- Candidate sections come from a small BM25 index over headings and
  paragraphs; a fact whose true home is not in its top-k sections shows as
  `MISSING` or `UNSURE`. Raise `--top-k` or read the `pairs` in the JSON.
- The fixture's answers are hand-written in the documented response shapes;
  they exercise the pipeline and are not evidence of model accuracy.
- `docs/gateway-seam.md` and a non-empty `.env.example` are used when present;
  on NanoClaw `main` at `6e5008fe` both are absent, so gateway facts come from
  `src/gateway-providers/index.ts` and the run notes say so.
