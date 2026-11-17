---
name: typesafe-triage
description: Label triage for nanocoai/nanoclaw issues and pull requests using the TypeSafe System One decision API. Proposes area, kind, priority and triage labels with confidence, compares them with the existing labels, and by default writes nothing to GitHub; an --apply flag adds only the two label questions proven out at 100% agreement (kind, needs_repro). Use to preview or evaluate automated triage, apply the proven labels, or replay the offline fixture without a key.
license: MIT
---

# TypeSafe triage

Fetch the most recently updated open issues and pull requests, ask TypeSafe one
fan-out request per item, gate every answer on confidence, and print proposed
labels next to the labels the item already carries. Dry run by default: the
script is read-only towards GitHub. Pass `--apply` to add the `kind` and
`needs_repro` proposals that measured 100% agreement on a live run — see
[Applying labels](#applying-labels---apply) below. Every other question
(`area`, `priority`, `pr_ready`) stays proposal-only; applying it is a separate,
human decision.

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

- `gh` authenticated with read access to the target repository (live mode only);
  `issues:write` and `pull-requests:write` as well if you pass `--apply`.
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
`--kind-threshold` (0.6), `--priority-threshold` (0.6), `--noul-threshold` (0.7),
`--model`, `--timeout`, `--json`, `--apply`, `--since <ISO timestamp>`,
`--only-unlabeled`.

## Applying labels (`--apply`)

`--apply` adds exactly two things, and only when the confidence gate already
cleared them:

- The ungated `kind/*` proposal, when the item has no existing `kind/*` label.
- On issues, `triage/needs-repro`, when `needs_repro` resolved yes (p >= the
  noul threshold) and the label is not already present.

Nothing is ever removed. `area/*`, `priority/*` and `pr_ready` are never
applied by this flag, and the script never comments. Labels go through
`gh issue edit --add-label` / `gh pr edit --add-label`, the same `gh` CLI the
read-only fetch uses, via a subprocess helper tests mock instead of calling.
Immediately before writing, each eligible label is re-checked against the
item's live labels (one extra read-only `gh api` call) so a label added by a
human between fetch and write is respected, not overwritten with a second one.
Rerunning with `--apply` is safe: the same eligibility checks (no existing
`kind/*` label, no existing `triage/needs-repro`) make it a no-op on anything
already labeled. `--apply` cannot be combined with `--fixture`, which replays
a frozen hand-written snapshot rather than an item's real state.

`--since <ISO timestamp>` (items created strictly after it) and
`--only-unlabeled` (skip items with any existing `kind/*` label) narrow the
item set independently of `--apply` — a scheduled unattended run typically
passes `--only-unlabeled` and conditionally `--apply`; see
[.github/workflows/typesafe-triage.yml](../../.github/workflows/typesafe-triage.yml)
and [docs/typesafe-triage.md](../../docs/typesafe-triage.md#running-it-unattended-github-actions)
for the scheduled setup, its two required secrets (`TYPESAFE_API_KEY`,
`TRIAGE_GH_TOKEN`), the `TYPESAFE_TRIAGE_APPLY` repository variable that gates
the schedule's `--apply`, and how to roll it back.

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
question, the number of labels applied (0 unless `--apply` was passed), token
usage and wall time. With `--apply`, the table also gets an `Applied` column
and `--json` an `applied` list per item.

## Limits

- Dry run unless `--apply` is passed, and even then only `kind` and
  `needs_repro` are ever written — never `area/*`, `priority/*`, `pr_ready`,
  and never a comment or a label removal.
- The fixture's answers are hand-written in the documented response shapes to
  exercise the pipeline; they are not evidence of model accuracy. Evaluate
  accuracy with a live `--record` run and the agreement summary.
- `priority/*` is maintainer-set. The default 0.6 gate comes from the first live
  run on nanoclaw (50 items): at 0.8 it withheld 34 of 48 priority proposals, at
  0.6 it withheld 13 with every fired proposal agreeing with the existing label,
  and at 0.5 disagreements appeared.
- Existing labels are never sent to the model, so agreement is a fair comparison.
- Requests are sequential; 50 items take roughly a minute. A request that fails
  after retries stops the run, but completed items are still saved and printed
  (exit status 1, `partial` names the failing item).
- Exit codes: `0` success, `1` a real error or partial failure, `2` missing
  `TYPESAFE_API_KEY` in live mode, `3` nothing matched after fetch/filters — a
  distinct status from `1` so a caller can tell "nothing to do" from a
  failure without matching text against stderr.
