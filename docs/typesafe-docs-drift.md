# TypeSafe docs drift detector (phase 1)

`skills/typesafe-docs-drift/scripts/typesafe-docs-drift.py` finds where the
[nanoclaw-docs](https://github.com/glifocat/nanoclaw-docs) portal disagrees
with the NanoClaw code and ranks the disagreements. It writes to neither
repository. This page records the pipeline, the questions, the gate, the JSON
that phase 2 consumes, and how to run and replay it.

## Pipeline

1. **Extract facts with code, not the model.** Deterministic parsers read a
   NanoClaw checkout and emit `{id, area, statement, evidence: {file, line,
   excerpt}, terms}` records:

   | Area | Source | Facts (main @ `6e5008fe`) |
   |---|---|---|
   | `ncl` | `src/cli/resources/*.ts`: per resource the verbs and their `open`/`approval` access; per custom verb the flags it declares in `args` (strict) or, without an `args` block, the `--flags` named in its help text (the statement says which); per column its `enum` | 57 |
   | `env` | `src/config.ts` `readEnvFile([...])` keys with their `process.env` then `.env` precedence and default; `NANOCLAW_GATEWAY_PROVIDER` and `NANOCLAW_RUNTIME_DRIVER` defaults; `process.env.X \|\| 'default'` reads elsewhere under `src/`; `.env.example` keys | 18 |
   | `container-config` | `CREATE TABLE container_configs` and `ALTER TABLE ... ADD COLUMN` across `src/db/migrations/`, column defaults, the `--cli-scope` value check in `groups.ts` | 8 |
   | `skills` | `.claude/skills/*/SKILL.md` frontmatter `name` and `description`, plus one catalog fact listing them all | 53 |
   | `gateway` | `docs/gateway-seam.md` (`## Selection`, `## How a gateway gets installed`) when present, else `src/gateway-providers/index.ts` | 3 |
   | `timestamps` | the `## Timestamps` section of `CLAUDE.md`, one fact per bullet or paragraph | 3 |

2. **Find candidate sections lexically.** Every page in `docs.json` (changelog
   pages excluded) is split on `##`/`###`/`####` headings after stripping MDX
   comments and component tags. A BM25 index over section text, page keywords
   and headings, with extra weight for the fact's identifiers (backticked terms
   such as `cli_scope` or `--rebuild`), returns the top 3 sections per fact.
   Facts with no lexical hit are `MISSING` without a request.

3. **One fan-out request per fact x section.** State is
   `{fact: {area, statement, evidence}, doc_section: {page, title, heading,
   text}}` (section text capped at 6,000 characters). Three questions:

   | Question | Type | Meaning |
   |---|---|---|
   | `contradicts` | noul | The section states something incompatible with the fact (a different name, value, default, option set, verb, flag or rule). Silence is not a contradiction. |
   | `covers` | noul | This section is where a reader would expect the fact, judging by title, heading and content, whether or not it is currently correct. |
   | `staleness` | score | Four levels: current / slightly outdated / wrong / dangerous if followed. |

   Per fact, `documented_anywhere` is the maximum `covers` over its sections.

4. **Gate and rank.** With the default gates (`contradicts >= 0.7`,
   `covers >= 0.7`, staleness confidence `>= 0.5`):

   | Pair verdict | Rule |
   |---|---|
   | `DRIFT` | `contradicts >= 0.7` |
   | `OK` | `contradicts <= 0.3` and `covers >= 0.7` |
   | `UNRELATED` | `contradicts <= 0.3` and `covers <= 0.3` |
   | `UNSURE` | anything else, or a missing answer |

   A fact is `DRIFT` if any pair is (best = highest contradiction, then
   staleness), else `OK` if any pair is (best = highest covers), else `UNSURE`
   if any pair is or if any of its requests failed, else `MISSING`. Rows are ranked `DRIFT` by contradiction,
   then `MISSING`, `UNSURE`, `OK`. The staleness label is shown with `?` when
   the score's confidence is below the gate.

## Confidence gate

Noul answers are probabilities of yes with no separate confidence, so the gate
is symmetric: yes above `t`, no below `1 - t`, uncertain in between. Score
answers carry `confidence`; the staleness level is informational and only its
label is gated. Start values are 0.7 for the nouls and 0.5 for the score,
exposed as `--contradicts-threshold`, `--covers-threshold` and
`--staleness-threshold`; evaluate them on a live run before trusting a cut.

## The key

Live mode reads `TYPESAFE_API_KEY` from the environment when the first request
is built. If it is unset the script exits with status 2 and a message; it does
not look for a file. Export it for the shell session only, for example from a
password manager. Never commit it, never put it in `.env` or a fixture. The
script does not log request headers, error bodies that echo the key are
redacted, and the saved JSON contains only facts, sections, responses and
decisions.

## Run

Offline, using the shipped fixture (six real facts and six real doc sections
from 2026-09-17, section text truncated, with hand-written answers in the
documented shapes):

```bash
python3 skills/typesafe-docs-drift/scripts/typesafe-docs-drift.py \
  --fixture skills/typesafe-docs-drift/fixtures/nanoclaw-sample.json
```

Without a key, inspect the deterministic half:

```bash
python3 skills/typesafe-docs-drift/scripts/typesafe-docs-drift.py --code /path/to/nanoclaw --facts-only
python3 skills/typesafe-docs-drift/scripts/typesafe-docs-drift.py --code /path/to/nanoclaw --docs /path/to/nanoclaw-docs --plan
```

Live:

```bash
export TYPESAFE_API_KEY=...
python3 skills/typesafe-docs-drift/scripts/typesafe-docs-drift.py \
  --code /path/to/nanoclaw --docs /path/to/nanoclaw-docs --record /tmp/drift-record.json
```

Requests run with `--concurrency` 4 workers by default; 429 and 529 are
retried with backoff. A pair that fails after retries is reported in `partial`
and counted in its fact's `failed_pairs`; the fact is judged on the pairs that
completed and is never called `MISSING` on that basis. The summary ends with a
`partial:` line naming the failed pairs (an HTML error body, such as a proxy or
WAF page, is collapsed to its text), and the exit status stays 0 so a mostly
complete run is still usable; pass `--strict` to exit 1 on any failed pair.
With `--concurrency 1` the run stops after three consecutive failures.
`--record` writes facts, sections, candidates, lexical scores and raw
responses in the fixture format, dropping pairs that failed so the record
replays cleanly. Raw answers for every run are saved as
`drift-<UTC stamp>.json` under `skills/typesafe-docs-drift/output/`
(gitignored) or `--output-dir`; `--json` prints the same payload.

## JSON for phase 2

Phase 2 (an agent that writes doc PRs) should read the saved payload, keep
`results` rows with `verdict == "DRIFT"` that a human confirmed, and for each
use:

- `statement` and `evidence.file` / `evidence.line` / `evidence.excerpt` with
  `code.commit`: what the docs must say and where the proof is.
- `best.section.page`, `best.section.heading`, `best.section.line`: the MDX
  file (`<page>.mdx` under the docs root) and the source line of the heading
  to edit, with `docs.commit` as the base revision.
- `best.decision.contradicts`, `covers`, `staleness`, `staleness_label`: the
  model's numbers for the PR description.
- `pairs[]`: every candidate section with its numbers and `lexical_score`, for
  the cases where a second section also needs the change; `failed_pairs` says
  how many candidates got no answer.

`MISSING` rows list the closest section (highest `covers`) and are the input
for a "document this" pass rather than a fix. Nothing in the payload is
generated text; phase 2 writes the prose. Result rows also carry the fact's
`terms`.

## Release gate

`scripts/docs-drift-gate.py` wraps the detector for the moment a NanoClaw
release is cut. Usage and flags are in the skill's `SKILL.md`; this section
records how it works and what the first release run showed.

**Why more than the detector.** The detector sees six fact areas. The docs
sweep of 2026-10-09 fixed 143 findings, mostly channel flows, setup behaviour
and lifecycle prose, which no extractor reaches. Every docs page already
carries `verified-against: <files> @ <sha>` stamps, and every release is a list
of merged pull requests, so the gate asks git two free questions first.

**Pull request queue.** `git log --first-parent <previous stable tag>..<release>`
lists the merged pull requests (squash commits and `Merge pull request`
commits). For each, the files it changed (test files dropped) are matched
against every path any page cites; a page is hit only when its stamp for that
file does not already contain the pull request (`git merge-base
--is-ancestor`). A pull request is `documented` when its
number (four digits or more, so the docs repository's own numbers do not
collide) appears in the docs history or pages, and `internal` when its title is
a `ci`, `test`, `build`, `style`, `chore(release)` or `chore(deps)` change. The
rest is the queue, ranked by how many pages cite the files it touched.

**Stamp staleness.** A stamp body is split at each `@ <sha>` (optionally
`@ channels branch <sha>`). The words before an anchor are expanded
(`{a,b}` braces, nested), filtered to path-like tokens, and matched as an exact
file, a directory prefix or a glob. For each anchor, `git diff --name-only
<sha> <ref>` names the files that moved. The ref is the release; a hinted
`channels` or `providers` branch is preferred; an anchor newer than the
release is `ahead`; a SHA missing from the repository (another repository, a
rewritten branch) is `unknown-anchor`. On a busy tree this flags nearly every
page, so it is context for the queue, not a gate.

**Detector rows.** The phase-1 payload is read in process. A `DRIFT` row whose
best section never mentions any of the fact's terms is demoted to `UNSURE`. A
baseline file holds rows a person decided (`held`, `false-positive`,
`accepted`), matched by fact id and optionally section id, so a settled row is
not raised again at the next release.

**Extractor fixes from the first run.** Enums written as `[...CONSTANT]` were
read as empty lists and compared with the docs as "accepts exactly these
values: ." (`ncl approvals` `status`, `ncl messaging-groups`
`unknown_sender_policy`); constants are now resolved from `src/`, and an enum
that cannot be read is skipped. An env fact built from `process.env.X ||
envConfig.X || ''` claimed "unset by default" although the real default is
applied where the value is used (`NANOCLAW_ABSOLUTE_CEILING_MS`); an empty
literal now makes no default claim. Skill facts carried the SKILL.md
description as the claim, so a stale description (`/add-ollama-provider`) read
as a docs error; they now assert existence only and keep the description as
evidence.

**First release run.** 2026-10-21, NanoClaw `v2026.10.1-rc.1` (`2dfe9376`)
against nanoclaw-docs `main` (`0b86a2ab`):

| | |
|---|---|
| Pull requests in `v2026.10.0..rc.1` | 43 merged; 35 touch a cited file; 8 already named in the docs; 5 CI or test chores; 22 to document |
| Stamps | 52 of 55 pages cite a changed file; 11 anchors cannot be checked (other repositories, rewritten branches, two stamps with no path) |
| Detector, before the fixes | 148 facts, 444 requests, 30 s: DRIFT 4, MISSING 1, UNSURE 6, OK 137 |
| Detector, after the fixes | DRIFT 1, 0 and 1 on three runs (one 0.74 row the model scored differently each time; a baseline entry holds it), MISSING 1, UNSURE 5 to 7 |
| Wall time, whole gate with the detector | about 50 s |

All four first-run `DRIFT` rows were checked against the code and none needed
a docs change (see the extractor fixes). The queue includes two merged pull
requests whose docs follow-ups were already tracked by hand (#3980 and #4108),
which is the check that it finds the real backlog.

## Phase 2: verified docs edits

`scripts/docs-drift-fix.py` and `references/phase2.md` turn a gate run into docs edits without letting one model's
guess through. Code prepares every input and checks every output; three agent roles sit between: a **row verifier**
(blind to the detector's numbers; a wrong DRIFT row is dropped before anyone drafts), a **drafter** (one per queued
pull request and per kept row, answering with exact text replacements or `no_change`), and a **proposal verifier**
(blind to the drafter's reasoning; CONFIRM, CONFIRM-WITH-CORRECTION or REJECT). `apply` then makes the kept
replacements, all or nothing, in a docs worktree. A changed input moves its old verdict aside, a fix drafted from a row
is dropped with its row, a missing or invalid proposal fails `check --strict`, and every verdict must carry the digest
(`fingerprint`) of the exact input it read. The proof run below used the earlier revision of `check`, which tied a
verdict to its input by content and timestamps rather than by digest; its verdicts and numbers are unchanged by that.

**Proof run.** 2026-10-21, NanoClaw `v2026.10.1-rc.1` (`2dfe9376`, read-only worktree), docs cut from origin/main
`0b86a2a`, Sonnet subagents for every agent role, one input file each:

| | |
|---|---|
| Gate on real docs main | 148 facts, 444 requests, 0 DRIFT, 22 pull requests to document |
| Gate on a scratch copy with one seeded error (`ASSISTANT_NAME` default changed) | 148 facts, DRIFT 2: the seeded error (0.95) and one model false positive (0.77) |
| Row verifier | 6 rows in (the two above plus 4 replayed first-run rows): 1 REAL (the seeded error), 5 NOT-A-DOCS-ERROR; all 6 correct, 0 unverified |
| Drafters | 5 queued pull requests: 3 changes (#4110: 2 edits, #4141: 1, #4139: 15 across 4 pages), 2 `no_change` (#4106, #4120); 1 kept row |
| Proposal verifier | 4 proposals, 19 edits: 0 rejected; 8 edits corrected (7 of the 15 in #4139, the 1 row fix); 11 confirmed as drafted |
| Applied | 3 stacked local commits for the queue (each `mint validate`; the last also `mint broken-links`), each with its Docs updates entry; the row fix applied to the scratch copy |
| Agent tokens (harness-reported subagent totals) | about 1.42M: row verifiers 0.43M, drafters 0.61M, proposal verifiers 0.38M |
| Detector tokens | about 0.65M in and 24k out per live run; TypeSafe reports no price |

Two things to read from it. Drafting alone is not trustworthy: 8 of 19 edits needed a correction, which is the case for
the second verifier. And the loop finds more than the detector row it started from: while fixing the seeded line the
drafter and verifier found that `ASSISTANT_NAME` is deprecated and the host does not use it, so the sentence "also sets
the default trigger" on the configuration and environment pages is wrong on current docs.

## First live run

Run on 2026-09-16 22:29 UTC from this repository against nanocoai/nanoclaw
`6e5008fe` and nanoclaw-docs `e3e1546`, model `jev-latest`, default gates,
`--concurrency 4`:

| | |
|---|---|
| Facts / pairs / requests | 142 / 426 / 426 |
| Verdicts | DRIFT 7, MISSING 1, UNSURE 11, OK 123 |
| Per area (DRIFT / MISSING / UNSURE / OK) | ncl 5/0/7/45, env 1/0/2/15, skills 1/0/1/51, timestamps 0/1/1/1, container-config 0/0/0/8, gateway 0/0/0/3 |
| Tokens | 609,315 input, 22,578 output |
| Wall time | 71.5 s |

The seven `DRIFT` rows, highest contradiction first: the `ncl approvals`
`status` enum (docs list an extra `awaiting_reason` value; `reference/ncl-cli#approvals`,
0.96), `WEBHOOK_PORT` precedence (`operate/troubleshooting#webhook-channel-is-silent`,
0.93), the `ncl dropped-messages` `reason` enum (`reference/ncl-cli#dropped-messages`,
0.92), `ncl tasks cancel` and `ncl tasks update` declared flags
(`reference/ncl-cli#tasks`, 0.88 and 0.87), the `/add-ollama-provider`
description (`extend/providers#ollama-add-ollama-provider`, 0.76) and `ncl tasks
resume` flags (0.71, staleness confidence below the gate). The raw payload is
in the gitignored output directory of the machine that ran it; the numbers
above are the only part recorded here.

## Benchmark: TypeSafe versus a prompt-and-parse LLM

`skills/typesafe-docs-drift/scripts/drift-benchmark.py` runs every contender
on the same work and reports speed, cost, stability and accuracy.

**Workload.** 60 fact x section pairs sampled from the recorded live run above
(`fixtures/benchmark-workload.json`, seed 20260916): the 11 `DRIFT` pairs of
the 7 `DRIFT` facts, the best pair of the 1 `MISSING` and the 11 `UNSURE`
facts, and 37 seeded-random `OK` pairs. Every contender gets the same fact
statement, evidence excerpt and doc section text, and the same three
questions with the same criteria text.

**Contenders.**

- `typesafe-recorded`: the recorded answers; wall time is the sum of the
  recorded per-request latencies (the recorded run used 4 workers).
- `typesafe-live`: the same requests sent again one at a time.
- `claude:haiku`, `claude:sonnet`: `claude -p --model <m> --output-format json
  --tools "" --strict-mcp-config --no-session-persistence --system-prompt ...`,
  10 pairs per call, strict JSON array out (`contradicts` and `covers` as
  booleans, `staleness` 0 to 3). CLI defaults otherwise, so the model's default
  thinking is on. `codex` (`codex exec --ephemeral -c 'sandbox_mode="read-only"'`)
  is implemented as a fallback and was not needed.

**Metrics.** Wall time for the first run; p50 seconds per pair (for the CLI
contenders the call time divided by the batch size); `p50 adj` subtracts the
measured startup overhead (one trivial `Reply with the single word OK.` call,
which also contains one minimal API round trip, so the subtraction favors the
LLM); tokens and cost from the TypeSafe `usage` block and the CLI's `usage` and
`total_cost_usd`; parse failures (invalid JSON, a missing or mistyped field, or
a failed call, counted per pair); flip rate (share of pairs whose `contradicts`
verdict differs between two runs); pairwise agreement; precision and recall on
`contradicts` against the gold file. TypeSafe probabilities are binarized at
the skill's 0.7 gate. An unanswered gold pair counts as a predicted no.

**Gold set.** `fixtures/benchmark-gold.json` holds 30 labels with a one-line
justification each: all `DRIFT` and `MISSING` pairs plus 18 seeded-random
others, 12 positive. **The labels were written by Claude, the LLM-assisted
reviewer that built this tool**, by reading the NanoClaw source and the doc
section without looking at any contender's answer. No human has verified them;
three are marked `borderline`. Two biases to keep in mind: the sample was
stratified by TypeSafe's own verdicts, so most positives are pairs TypeSafe
already flagged, which favors its recall; and the labeler is a Claude model,
which may favor the Claude contenders' reading of borderline cases. A third
caveat: the gold answers the benchmark's question (does the section contradict
the extracted fact?), not "is the doc wrong?". Both `enum` positives in the set
(`ncl approvals` `status`, `ncl dropped-messages` `reason`) are cases where the
doc lists a value the database really uses and the CLI column `enum` is the
stale side, so a contender scores a hit for flagging a code bug as doc drift.
A human spot check of 10 seeded pairs on 2026-09-23 agreed with all 10
`contradicts` and `covers` labels and disagreed with one `borderline` flag.

**Results.** One run on 2026-09-17 10:12 to 10:34 UTC from one macOS machine,
60 pairs, two passes per live contender, `claude` CLI 2.1.273, `jev-latest`:

| Contender | Pairs | Wall time | p50 per pair | p50 per pair, startup subtracted | Tokens in / out | Cost | Parse failures (run 1 / run 2) | Flip rate on `contradicts` | Precision | Recall |
|---|---|---|---|---|---|---|---|---|---|---|
| typesafe-recorded | 60 | 40.1 s (sum of recorded latencies) | 0.66 s | 0.66 s | 95,720 / 3,180 | not reported | 0 | n/a | 1.00 | 0.92 |
| typesafe-live | 60 | 41.3 s | 0.68 s | 0.68 s | 94,880 / 3,180 | not reported | 0 / 0 | 3.3% (2/60) | 1.00 | 0.92 |
| claude:haiku | 60 | 359.6 s | 5.57 s | 5.09 s | 81,994 / 32,776 | $0.33 | 0 / 0 | 6.7% (4/60) | 0.85 | 0.92 |
| claude:sonnet | 60 | 284.8 s | 3.99 s | 3.66 s | 110,521 / 25,665 | $0.70 | 0 / 0 | 11.7% (7/60) | 0.83 | 0.83 |

- Measured startup overhead per CLI call: haiku 4.8 s, sonnet 3.3 s. Batch times
  ranged from 14.5 s to 114.2 s for 10 pairs.
- Every pair parsed for every contender. Haiku wrapped its array in a code
  fence or prose in all 6 calls of both runs and sonnet in 1 of 6; the parser
  recovered them, as a real pipeline would.
- Agreement on `contradicts` (run 1): TypeSafe recorded vs live 98.3%, TypeSafe
  live vs sonnet 90.0%, vs haiku 85.0%, haiku vs sonnet 88.3%. On `covers`:
  100%, 96.7%, 93.3%, 96.7%.
- The CLI's token counts include its cached system prompt tokens; TypeSafe
  does not report a price, so its cost column is empty rather than zero.
- Read precision and recall with the two gold-set biases above in mind. With 12
  positives, one pair moves recall by 8 points.

Reproduce (the LLM contenders spend tokens on the operator's Claude plan, and
`typesafe-live` needs `TYPESAFE_API_KEY`):

```bash
python3 skills/typesafe-docs-drift/scripts/drift-benchmark.py   # offline: recorded answers vs gold
python3 skills/typesafe-docs-drift/scripts/drift-benchmark.py \
  --contenders typesafe-recorded,typesafe-live,claude:haiku,claude:sonnet --runs 2
```

`--record <file> --results <drift-*.json> --write-workload <file>` rebuilds the
sample from another recorded run; `--limit` and `--seed` change it;
`--from-results` re-renders a saved `benchmark-*.json`. Tests:
`tests/test_drift_benchmark.py` (mocked subprocess and transport).

## Tests

`tests/test_typesafe_docs_drift.py` runs in the offline suite: every extractor
against a synthetic checkout (resources, env, migrations, skills, gateway doc
present and absent, timestamps), section splitting over a synthetic `docs.json`
(code fences, duplicate headings, comments, component tags, changelog
exclusion), lexical ranking, request shape, gate and fold logic, ranking and
summary, HTTP transport with a mocked opener (bearer header, private keys
stripped from the body, retry on 429/529, no retry on 422, redaction),
concurrent and sequential pair execution with partial failures, fixture replay
end to end, a mocked live run that records a replayable fixture, and a check
that a key in the environment never reaches stdout, stderr or the saved JSON.
No test contacts the network.
