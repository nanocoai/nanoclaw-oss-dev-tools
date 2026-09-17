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
