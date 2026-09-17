---
name: typesafe-docs-drift
description: Detect and rank drift between the NanoClaw code and the nanoclaw-docs Mintlify portal with the TypeSafe System One decision API. Deterministic code extracts facts (ncl verbs and flags, environment variables, container_configs and cli_scope, workspace skills, gateway selection, timestamp rules), lexical search picks candidate doc sections, and one fan-out request per fact x section asks contradicts / covers / staleness. Prints DRIFT, MISSING, UNSURE and OK rows with the model's numbers; writes nothing to either repository. Use to find doc pages that need a fix, or replay the offline fixture without a key.
license: MIT
---

# TypeSafe docs drift (phase 1)

Extract facts from a NanoClaw checkout with deterministic code, find the top
doc sections for each fact by lexical search over the docs portal, ask TypeSafe
one fan-out request per fact x section pair, gate on the answers, and print a
ranked drift report. The model only judges; it never writes text. Phase 2 (an
agent writing doc PRs from confirmed `DRIFT` rows) consumes the JSON this
script saves.

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
`--model`, `--timeout`, `--json`.

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
- Candidate sections come from a small BM25 index over headings and
  paragraphs; a fact whose true home is not in its top-k sections shows as
  `MISSING` or `UNSURE`. Raise `--top-k` or read the `pairs` in the JSON.
- The fixture's answers are hand-written in the documented response shapes;
  they exercise the pipeline and are not evidence of model accuracy.
- `docs/gateway-seam.md` and a non-empty `.env.example` are used when present;
  on NanoClaw `main` at `6e5008fe` both are absent, so gateway facts come from
  `src/gateway-providers/index.ts` and the run notes say so.
