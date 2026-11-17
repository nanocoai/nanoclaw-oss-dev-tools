# TypeSafe triage

`skills/typesafe-triage/scripts/typesafe-triage.py` previews how TypeSafe's
System One model (`jev-latest`) would label open NanoClaw issues and pull
requests, and can optionally apply the two label questions proven out by a
live run. Dry run by default: no flag, no write. This page records the
questions, the confidence gate, the apply rules, where the key comes from, and
how to run and replay it.

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
gated question, the applied-label count, token usage and wall time.

## Applying labels (`--apply`)

Phase 2 applies only the two questions that measured 100% agreement on a first
live run: `kind` and `needs_repro`. Everything else — `area/*`, `priority/*`,
`pr_ready` (`Status: Needs Review` / `triage/needs-author`) — is proposal-only
forever; `--apply` does not change that.

Without `--apply`, nothing changes and the rules below never run. With
`--apply`, for every item:

- **`kind`**: if the proposal is not gated (confidence >= `--kind-threshold`)
  and the item has no existing `kind/*` label, the proposed `kind/*` label is
  added. An item that already carries any `kind/*` label is left alone, even
  if the proposal disagrees with it.
- **`needs_repro`** (issues only): if it resolved yes (p >= `--noul-threshold`)
  and the issue has no existing `triage/needs-repro` label, that label is
  added — but only when the `kind` question's own proposal is *also* ungated
  and is `kind/bug` or `kind/security`. `decide()`'s dry-run SKIP check only
  looks at the model's raw top `kind` choice, not whether that choice cleared
  its own confidence gate (and it explicitly treats a completely missing
  `kind` answer the same as a confirmed bug report, for the dry-run table);
  `--apply` adds this extra, apply-only check on top so a low-confidence,
  gated, or entirely absent `kind` answer never lets `triage/needs-repro`
  through, even though decide() itself would still propose it in the table.

No label is ever removed, no label outside these two questions is ever added,
and the script never comments. Labels are added one at a time via
`gh issue edit --add-label <label>` / `gh pr edit --add-label <label>` — the
same `gh` CLI the read-only fetch uses, through a subprocess helper that tests
mock instead of calling. A failed `gh` call raises `PartialFailure` the same
way a failed fetch does and stops the run; items already labeled earlier in
that run keep their result and their labels (additive-only, so a partial run
is always safe to rerun).

Eligibility above ("no existing `kind/*` label", "no existing
`triage/needs-repro`") is evaluated twice: once against the labels as they
stood when the item was fetched (to decide what to even attempt), and once
more, via one extra read-only `gh api` call, immediately before writing —
closing the gap between fetch and write during a run that can take a while, so
a label a human (or a concurrent run) added in between is respected instead of
silently getting a second, conflicting one. A narrow window remains between
that recheck read and the write itself (GitHub has no compare-and-swap for
labels); this only matters if someone else labels the exact same item in that
instant, and the result is at worst one extra additive `kind/*` label to
remove by hand — never a removal. The recheck only re-reads the label *set*,
not the item's content: the `kind`/`needs_repro` decisions themselves were
made from the title/body/files as they stood at fetch time, so if a reporter
adds reproduction steps or the item is closed between fetch and apply, the
label added still reflects the older content. Treat `--apply`'s output as
maintainer-review-quality, not ground truth that never needs a second look;
a `--since` window close to "now" and a schedule frequent enough that the
fetch-to-apply gap stays small are the practical mitigations, not a code fix.
If a write fails partway through an item
(one label added, the next failing), that item still appears in the run's
results with exactly the labels it actually got, instead of silently losing
the completed write; the run stops there as a `PartialFailure`, same as a
fetch failure. A `gh` call that itself reports failure (nonzero exit) is
recorded as not applied even in the rare case where GitHub actually committed
the label before the connection dropped and only the acknowledgement was
lost — there is no verify-after-write step, since one would double the `gh`
calls to close a network-ack race this narrow. The additive, idempotent
design already self-heals this: a later run's fresh fetch sees the label is
really there and correctly leaves it alone; only that one run's own local
report can be a beat behind reality. `--apply` is refused together with `--fixture`: a fixture's
items and labels are a frozen, hand-written snapshot, never the item's real
current state, so combining them would write real, possibly wrong labels from
stale data.

The table gains an `Applied` column (`applied` on the `kind`/`needs_repro` row
that got a label this run, `-` otherwise) and the summary prints
`labels applied: N`. `--json` includes an `applied` list of the labels added
per item, plus `summary.labels_applied` for the total.

Two flags narrow which items are considered, independent of `--apply`. Both
filter *after* `--issues`/`--prs` fetch their most-recently-updated window, so
an eligible item stuck behind enough already-labeled or older items can fall
outside that window and be missed this run. Raising `--issues`/`--prs` (the
scheduled workflow already does) helps *within one run*, but running the
schedule more often does not: `--issues`/`--prs` always take the *N
most-recently-updated* items, the same top-N window every time, so an item
that never gets touched again can be permanently stuck below it regardless of
how often the schedule fires — the fix is a large enough `--issues`/`--prs`
to outrun sustained update volume, or an occasional plain, wider live run
without `--only-unlabeled` to sweep up stragglers. `--only-unlabeled` skips by
`kind/*` presence alone (as specified),
so if a run adds `kind/bug` to an issue but then fails before adding
`triage/needs-repro` to that same issue (see the `PartialFailure` note above),
a later `--only-unlabeled` run will no longer reconsider it — the table for
the failing run shows exactly which label landed, so that one issue is a
one-off manual follow-up, not a routinely-recurring gap.

- `--since <ISO timestamp>` — only items created strictly after it (items with
  no recorded creation time are dropped, not guessed at; a timestamp with no
  UTC offset is assumed to be UTC).
- `--only-unlabeled` — skip items that already carry any `kind/*` label.

## Running it unattended (GitHub Actions)

`.github/workflows/typesafe-triage.yml` runs the script against
`nanocoai/nanoclaw` by default (overridable via the `repo` input) on
`workflow_dispatch`, and every 6 hours on a schedule. The scheduled run always
passes `--only-unlabeled` and a wider `--issues 100 --prs 60` fetch window
(the default 30/20 is what `--only-unlabeled` filters, so a narrower window
risks starving an unlabeled item stuck behind labeled ones — see the fetch/filter
caveat above); it only adds `--apply` when the repository variable
**`TYPESAFE_TRIAGE_APPLY`** is exactly `"true"` — otherwise it stays a dry run.
Every run's table is uploaded as a workflow artifact regardless of `--apply`,
and a run with nothing left to triage (`--only-unlabeled` emptied the set) is
treated as a routine no-op, not a failure. A manual `workflow_dispatch` run can
opt into `--apply` directly via its `apply` input, regardless of the
repository variable. Untrusted-looking inputs (`repo`, `apply`) are passed to
the run step through `env:` rather than interpolated into the shell script, so
they can never be read as shell code.

Two secrets must exist in the repository before the workflow can run:

- **`TYPESAFE_API_KEY`** — the TypeSafe System One key (see [The key](#the-key)
  below; same rules apply inside the workflow).
- **`TRIAGE_GH_TOKEN`** — a token scoped to the *target* repo (`nanocoai/nanoclaw`
  by default) with `issues:write` and `pull-requests:write`. This is not the
  workflow's own `GITHUB_TOKEN`: triage usually targets a different repo than
  the one the workflow runs in, and `GITHUB_TOKEN` only ever grants access to
  the repo running the workflow. Create a fine-grained personal access token
  restricted to the target repo with those two permissions, and store it as
  this repository secret.

**Rollback**: unset (or set to anything other than `"true"`) the
`TYPESAFE_TRIAGE_APPLY` repository variable to return the schedule to a dry
run. Labels already applied are additive and are not removed by turning the
variable off — undo them by hand in GitHub if needed.

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

Exit codes: `0` success (dry run or apply), `1` a real error or partial failure
(a fetch or request failed; check `partial`/stderr), `2` `TYPESAFE_API_KEY` is
unset in live mode, `3` nothing matched after fetching and filtering — a
distinct status from `1` specifically so an automated caller (the scheduled
workflow) can tell "nothing to do" apart from a failure by exit code alone,
without matching text against stderr (which an arbitrary upstream error body
could otherwise spoof by coincidence).

Live apply run, narrowed to what a scheduled run would see (new, unlabeled
items only):

```bash
export TYPESAFE_API_KEY=...
python3 skills/typesafe-triage/scripts/typesafe-triage.py \
  --since 2026-09-01T00:00:00Z --only-unlabeled --apply
```

## Tests

`tests/test_typesafe_triage.py` runs in the offline suite: question and request
shape, template-section detection, gate and verdict logic, `gh` output parsing
with a mocked runner, HTTP transport with a mocked opener (bearer header, retry
on 429/529, no retry on 422), the `--apply` label rules (no `gh` calls without
the flag, exact `gh issue edit`/`gh pr edit --add-label` calls for an eligible
item, no call when the label already exists, no call for a gated proposal) via
a mocked subprocess runner, `--since`/`--only-unlabeled` filtering, fixture
replay end to end, and a check that a key
in the environment never reaches stdout, stderr or the saved JSON. No test
contacts the network.
