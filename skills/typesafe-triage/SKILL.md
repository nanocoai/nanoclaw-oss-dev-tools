---
name: typesafe-triage
description: Dry-run label triage for nanocoai/nanoclaw issues and pull requests using the TypeSafe System One decision API. Proposes area, kind, priority and triage labels with confidence, compares them with the existing labels, and writes nothing to GitHub. Use to preview or evaluate automated triage, or to replay the offline fixture without a key.
license: MIT
---

# TypeSafe triage dry run

Fetch the most recently updated open issues and pull requests, ask TypeSafe one
fan-out request per item, gate every answer on confidence, and print proposed
labels next to the labels the item already carries. The script is read-only
towards GitHub. Applying labels is a separate, human decision.

Entry point: [typesafe-triage.py](scripts/typesafe-triage.py) (Python 3.10+,
standard library only). Offline fixture:
[nanoclaw-sample.json](fixtures/nanoclaw-sample.json). Details of the questions,
gate and output: [docs/typesafe-triage.md](../../docs/typesafe-triage.md).

## Sources this skill drives

- NanoClaw label taxonomy: `gh label list -R nanocoai/nanoclaw` (the `area/*`,
  `kind/*`, `priority/*` and `triage/*` families and their descriptions).
- NanoClaw `CONTRIBUTING.md` (issue forms, "exactly one `area/*`", maintainer-only
  priority, `triage/needs-repro` meaning) and `.github/labeler.yml` (path to area
  mapping used for the area rubrics).
- NanoClaw `.github/PULL_REQUEST_TEMPLATE.md`, marker `nanoclaw-pr-template:v2`,
  whose section headings the script checks for content.
- TypeSafe HTTP API: `POST https://api.typesafe.ai/v1/systemone`, model
  `jev-latest`, question types `noul`, `choice`, `score`
  ([API reference](https://docs.typesafe.ai/api.md),
  [confidence](https://docs.typesafe.ai/confidence.md),
  [speculative fan-out](https://docs.typesafe.ai/patterns/fan-out.md)).

Re-check the rubrics when the label set or the labeler mapping changes.

## Prerequisites

- `gh` authenticated with read access to the target repository (live mode only).
- `TYPESAFE_API_KEY` exported in the shell (live mode only). The script reads it
  from the environment at request time, never from a file, and never prints it.
  Do not put the key in `.env`, a fixture, or any file inside a repository.

## Run

Offline demonstration, no key and no network:

```bash
python3 skills/typesafe-triage/scripts/typesafe-triage.py \
  --fixture skills/typesafe-triage/fixtures/nanoclaw-sample.json
```

Live dry run against nanocoai/nanoclaw (30 issues, 20 PRs; nothing is written):

```bash
export TYPESAFE_API_KEY=...   # from your password manager, this shell only
python3 skills/typesafe-triage/scripts/typesafe-triage.py --record /tmp/triage-record.json
```

`--record` saves the fetched items and raw responses so the same run can be
replayed later with `--fixture`. Raw answers for every run land in
`skills/typesafe-triage/output/` (gitignored) or `--output-dir`. Item bodies are
included there; keep the directory private.

Flags: `--repo`, `--issues`, `--prs`, `--area-threshold` (0.6),
`--kind-threshold` (0.6), `--priority-threshold` (0.8), `--noul-threshold` (0.7),
`--model`, `--timeout`, `--json`.

## Read the output

One row per proposal. `Verdict` compares the proposal with the existing labels:
`AGREE` (already present), `DISAGREE` (a different label from the same
`area/`, `kind/` or `priority/` family is present), `NEW` (that family is
absent), `SKIP` (not applicable, for example `needs_repro` on a feature
request). Below the confidence gate the proposal is `triage/unresolved` with the
model's top answer in parentheses, so a maintainer sees what it leaned towards
without the script guessing.

The summary reports agreement per question over compared items only
(agree / (agree + disagree)), the number of items with at least one gated
question, token usage and wall time.

## Limits

- Dry run only. There is no flag that writes labels, comments or anything else.
- The fixture's answers are hand-written in the documented response shapes to
  exercise the pipeline; they are not evidence of model accuracy. Evaluate
  accuracy with a live `--record` run and the agreement summary.
- `priority/*` is maintainer-set; the default 0.8 gate keeps most priority
  proposals as `triage/unresolved` on purpose.
- Existing labels are never sent to the model, so agreement is a fair comparison.
- Requests are sequential; 50 items take roughly a minute. A request that fails
  after retries stops the run, but completed items are still saved and printed
  (exit status 1, `partial` names the failing item).
