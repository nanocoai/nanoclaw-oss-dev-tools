# TypeSafe triage dry run

`skills/typesafe-triage/scripts/typesafe-triage.py` previews how TypeSafe's
System One model (`jev-latest`) would label open NanoClaw issues and pull
requests. It never writes to GitHub. This page records the questions, the
confidence gate, where the key comes from, and how to run and replay it.

## What one request contains

Every item gets exactly one `POST https://api.typesafe.ai/v1/systemone` with a
structured `state` and four questions (speculative fan-out: all questions are
answered in parallel and code decides which answers matter).

State: `repository`, `item_type`, `number`, `title`, `author_association`,
`body` (first 12,000 characters). Pull requests add `draft`, `changed_files`
(first 80 paths), `changed_file_count` and `template` (whether the
`nanoclaw-pr-template:v2` marker is present and which of its eight sections are
`filled`, `empty` or `missing`). Existing labels are deliberately not sent.

| Question | Type | Options and outcome |
|---|---|---|
| `area` | choice | The 16 `area/*` labels; one line of rubric each, derived from NanoClaw's CLAUDE.md, docs and `.github/labeler.yml` path mapping. |
| `kind` | choice | `kind/bug`, `kind/feature`, `kind/documentation`, `kind/question`, `kind/security`, `kind/hardening`, `kind/cleanup`, using the label descriptions. |
| `priority` | score | Four ordered levels with concrete descriptions; the score rounds to `priority/low`, `medium`, `high` or `critical`. |
| `needs_repro` (issues) | noul | Yes means the report lacks reproduction steps or a concrete failing case, proposing `triage/needs-repro`. Skipped when the top `kind` is not `kind/bug` or `kind/security`. |
| `pr_ready` (PRs) | noul | Yes means the template is filled, tests or a reason for none are stated and the diff matches the title, proposing `Status: Needs Review`; a confident no proposes `triage/needs-author`. |

## Confidence gate

Choice and score answers carry `confidence` (0 to 1) derived from their
probability distribution. Noul answers are a probability of yes.

| Question | Default gate | Flag |
|---|---|---|
| `area`, `kind` | confidence >= 0.6 | `--area-threshold`, `--kind-threshold` |
| `priority` | confidence >= 0.6 | `--priority-threshold` |
| `needs_repro`, `pr_ready` | yes if p >= 0.7, no if p <= 0.3, otherwise uncertain | `--noul-threshold` |

Below the gate the script proposes `triage/unresolved` and shows the model's top
answer in parentheses instead of guessing. Priority is maintainer-set in NanoClaw,
which is why its gate is the strictest.

Each proposal is compared with the item's existing labels: `AGREE` when the label
is present, `DISAGREE` when a different label of the same `area/`, `kind/` or
`priority/` family is present, `NEW` when the family is absent. The summary gives
the agreement rate per question over compared items, the count of items with a
gated question, token usage and wall time.

## The key

Live mode reads `TYPESAFE_API_KEY` from the environment when the first request
is built. If it is unset the script exits with status 2 and a message; it does
not look for a file. Export it for the shell session only, for example from a
password manager. Never commit it, never put it in `.env` or a fixture, and never
paste it into an issue. The script does not log request headers, and the saved
JSON contains only items, responses and decisions.

## Run

Offline, using the shipped fixture (five real nanocoai/nanoclaw items fetched
read-only on 2026-09-16 with hand-written answers in the documented shapes):

```bash
python3 skills/typesafe-triage/scripts/typesafe-triage.py \
  --fixture skills/typesafe-triage/fixtures/nanoclaw-sample.json
```

Live dry run (30 most recently updated open issues, 20 open PRs):

```bash
export TYPESAFE_API_KEY=...
python3 skills/typesafe-triage/scripts/typesafe-triage.py --record /tmp/triage-record.json
```

`--record` writes items plus raw responses in the fixture format so the run can
be replayed with `--fixture` without spending tokens. Raw answers for every run
are saved as `triage-<UTC stamp>.json` under `skills/typesafe-triage/output/`
(gitignored) or `--output-dir`; `--json` prints the same payload to stdout.
`--repo`, `--issues` and `--prs` change the target and sample size. The issues
endpoint interleaves pull requests, so the fetch pages until it has enough real
issues. Requests run one at a time; if one fails after retries (429 and 529 are
retried with backoff), the completed items are still saved and printed, the
failing item is named in `partial`, and the exit status is 1.

## Tests

`tests/test_typesafe_triage.py` runs in the offline suite: question and request
shape, template-section detection, gate and verdict logic, `gh` output parsing
with a mocked runner, HTTP transport with a mocked opener (bearer header, retry
on 429/529, no retry on 422), fixture replay end to end, and a check that a key
in the environment never reaches stdout, stderr or the saved JSON. No test
contacts the network.
