# Phase 2: from a gate run to verified docs edits

Roles, files and prompts for the loop that `scripts/docs-drift-fix.py` supports. Models judge and write; code prepares
every input and checks every output. Nothing here pushes, opens a PR or posts anywhere.

## Setup

1. A docs worktree cut from current origin/main (the `worktrees` skill; never an old clone):
   `git -C <docs canonical clone> fetch origin && git -C <docs canonical clone> worktree add ~/dev/.worktrees/nanoclaw-docs/<slug> -b <branch> origin/main`.
2. A read-only code worktree detached at the release commit (`gate.json` `release.sha`).
3. A work directory outside both repositories, for example `<scratch>/phase2`. It holds `inputs/`, `verdicts/`,
   `proposals/` and the ledger.

All commands take `--out <work dir>`; `rows`, `drafts` and `proposals` also take `--gate-dir <run dir>`,
`--docs <docs worktree>` and `--code <code worktree>`.

## The loop

| Step | Who | Command or action | Output |
|---|---|---|---|
| 1 | code | `docs-drift-fix.py rows` | `inputs/row-*.json`, one per new DRIFT row, blind to the model's numbers |
| 2 | row verifier agent, one per input | prompt below | `verdicts/row-*.json` |
| 3 | code | `docs-drift-fix.py check` | `ledger.json`, `ledger.md`; REAL rows kept, the rest dropped |
| 4 | code | `docs-drift-fix.py drafts [--only N,N] [--skip N,N]` | `inputs/pr-*.draft.json` per queued pull request, `inputs/row-fix-*.draft.json` per kept row |
| 5 | drafter agent, one per input | prompt below | `proposals/*.json` |
| 6 | code | `docs-drift-fix.py proposals` | `inputs/*.verify.json`; proposals whose `old` text is not in the page exactly once are refused here |
| 7 | proposal verifier agent, one per input | prompt below | `verdicts/*.json` |
| 8 | code | `docs-drift-fix.py check` | the ledger again; CONFIRM and CONFIRM-WITH-CORRECTION kept, REJECT dropped |
| 9 | code | `docs-drift-fix.py apply --docs <worktree> [--dry-run]` | exact replacements in the docs worktree, all or nothing |
| 10 | agent | `mint validate` (and `mint broken-links` when links changed), then one local commit per concern, stacked, unsigned | local branches |

Use `--skip` for pull requests another docs change already tracks. Exit codes: `proposals` returns 1 when a proposal
is invalid or a draft has none, and `check --strict` returns 1 while anything is unverified or needs attention, so a
script cannot mistake a half-finished loop for a clean one. If a proposal or row input changes, its old verdict is moved
aside (`verdicts/stale-*.json`) and the verifier must run again. Use a cheaper model for the verifiers than for
the drafter, and give each verifier only its input file: the point of the blind step is that it does not see the
detector's score or the drafter's reasoning. Dropping a wrong row (about one in six in the benchmark) before anyone
drafts is the saving; the proposal verifier catches a wrong edit.

## Files

**Every verdict copies the input's `fingerprint` field unchanged.** `check` refuses a verdict whose fingerprint is not that of the current input, so a verifier that read an older version of a proposal can never approve a newer one.

**Row verdict** (`verdicts/<id>.json`): `{"id": "<id>", "fingerprint": "<from the input>", "verdict": "REAL | NOT-A-DOCS-ERROR | UNSURE", "evidence": "file:line and what the code does", "reason": "one sentence"}`.

**Proposal** (`proposals/<id>.json`, written by the drafter):

```json
{"id": "pr-4110",
 "no_change": false, "reason": "only when no_change", "paper_cut": "optional: a product bug the docs would otherwise work around",
 "changes": [{"page": "operate/ncl-cli", "heading": "groups", "old": "exact text, once in the page file",
              "new": "replacement text", "why": "one sentence"}],
 "evidence": [{"file": "src/cli/resources/groups.ts", "line": 120, "quote": "short quote from the code"}]}
```

**Proposal verdict** (`verdicts/<id>.json`): `{"id": "<id>", "fingerprint": "<from the input>", "verdict": "CONFIRM | CONFIRM-WITH-CORRECTION | REJECT", "evidence": "file:line and what the code does", "corrections": [{"index": 0, "new": "replacement text"}]}`. `corrections` only with CONFIRM-WITH-CORRECTION; the corrected text replaces the drafter's.

## Prompts

**Row verifier.**

> You verify one claim about NanoClaw against its code. Read `<input file>`. Open the code under `code.root` at the file and line it names and follow whatever you need to; read only, run nothing that writes. Decide whether the doc section in the input states something wrong, omits a step a reader needs, or is contradicted by the code on the point the claim makes. Judge from the code alone. Write the answer file named in the input: JSON with `id`, the input's `fingerprint` copied unchanged, `verdict` (REAL, NOT-A-DOCS-ERROR or UNSURE), `evidence` (file:line and what the code does) and `reason` (one sentence). Use UNSURE when the code does not settle it.

**Drafter.**

> You draft the smallest docs change a NanoClaw release needs. Read `<input file>` and its rules. The code checkout under `code.root` is at the release commit: read the pull request's commit (`git show <sha>`), the files it changed, and the code as it is at the release. Read the listed pages, which only cite the changed files, and grep the docs checkout for the command, setting or behaviour names the change touches: the page that should say it may not be listed. Decide whether any page now says something the code contradicts or leaves a reader unable to find something they need. If so, write the answer file named in the input with exact text replacements (`old` must occur exactly once in the page file) and evidence; if not, answer `no_change` with the reason. If the honest fix would be a warning or workaround for a product bug, answer `no_change` and describe the bug in `paper_cut`. Do not edit verified-against stamps. Do not write to the docs checkout.

**Proposal verifier.**

> You check proposed edits to NanoClaw docs against its code. Read `<input file>`. For each change, read the cited code and anything else you need under `code.root` (read only). Decide whether the new text is true of the code and fixes what was wrong in the old text without adding a claim the code does not support. You do not know who proposed this or why beyond the one-line `why`. Write the answer file named in the input: `id`, the input's `fingerprint` copied unchanged, `verdict` (CONFIRM, CONFIRM-WITH-CORRECTION or REJECT), `evidence`, and for CONFIRM-WITH-CORRECTION a `corrections` list with the replacement text for each change that needs one.

## Commits

One concern per branch (usually one pull request, or one page group), stacked on the previous branch. Subject
`docs(<area>): <what changed> (nanoclaw #N)`. Commits are unsigned and local; Ethan signs nothing here and decides the
push, the draft PR and the reviewers. Include a docs changelog entry when the docs repository's contributing guide asks
for one. Report product bugs the drafters named under `paper_cut` as product issues to raise, never as docs text.
