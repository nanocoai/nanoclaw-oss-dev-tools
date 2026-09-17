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
completed and is never called `MISSING` on that basis, and the exit status
is 1. With `--concurrency 1` the run stops after three consecutive failures.
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
generated text; phase 2 writes the prose.

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
which may favor the Claude contenders' reading of borderline cases.

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
