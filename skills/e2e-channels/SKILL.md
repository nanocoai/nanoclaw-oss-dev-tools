---
name: e2e-channels
description: Find the test credentials each NanoClaw messaging channel needs (Slack, Telegram, Discord, Teams and the rest), from a file, NC_E2E_* environment variables or ~/.nanoclaw-e2e/channels/, and report every channel as ready or skipped with the missing variable named. Use before a channel E2E run, to see which channels can be tested, or to fill local credential files from 1Password.
license: MIT
---

# Channel test credentials

NanoClaw ships 20 channels: the built-in CLI plus the `add-*` channel skills.
Testing a real channel needs two sets of secrets: the **bot side** that goes
into NanoClaw's `.env` on the target, and the **tester side** the harness uses to
post as a human and read the agent's reply. This skill says which variables each
channel needs, finds them, and reports each channel as `ready` or `skipped`.
It never fails a channel for missing credentials and never counts a skip as a pass.

This skill resolves credentials only. Driving a message through a real channel
(the `--channel` flag on the E2E drivers, the tunnel for webhook channels and
the reply proof) is later work; until then the CLI channel remains the only
qualified one.

## What each channel needs

[channels.json](channels.json) lists variable names, never values. The names are
NanoClaw's own `.env` names from its `add-<channel>` skills, so a value goes
straight into the target's `.env`.

| Field | Meaning |
|---|---|
| `bot` | NanoClaw `.env` variables, all required |
| `bot_any_of` | Alternative groups; one complete group is required (Linear: client ID and secret, or an API key) |
| `tester` | Harness-only variables; never copied to the target |
| `shapes` | A regex, or `service_account` for Google service-account JSON, that a value must match |
| `session` | Also needs a linked-device session directory |
| `provisioned_in_run` | Nothing stored; the run creates its own (CLI, Emacs, container Matrix and Mattermost) |
| `blocked` | Not testable yet; always skipped with this reason |
| `wave` | Rollout order from the channel-testing plan |

Show the table with `python3 "$SKILL_DIR/scripts/channel-creds.py" list`,
where `SKILL_DIR` is this skill's installed directory.

## Lookup order

For channel X the loader takes the **first source holding a complete, valid
set** and never mixes values across sources:

1. `--channel-env-file PATH` (or `CHANNEL=PATH` when checking several channels).
2. Environment variables `NC_E2E_<X>_<VAR>`, for example
   `NC_E2E_SLACK_SLACK_BOT_TOKEN`. Hyphens in the channel name become underscores
   (`NC_E2E_WHATSAPP_CLOUD_...`). This is how CI provides them.
3. `~/.nanoclaw-e2e/channels/<x>.env` merged with `<x>.tester.env`
   (`NC_E2E_HOME` overrides `~/.nanoclaw-e2e`).

Session directories come from `NC_E2E_<X>_SESSION_DIR`, then
`~/.nanoclaw-e2e/channels/<x>/session/`; it must be a non-empty directory.

A value is not usable when it is empty, a placeholder (`NC_E2E_PLACEHOLDER`,
`<...>`, `changeme`, `PASTE_...`, `YOUR_...`), an unresolved `op://` reference
or the wrong shape. Empty and placeholder values are reported as missing; the
others as invalid, with the reason.
Files readable by group or others are ignored with a reason; keep them `0600`.

## Check channels

```bash
python3 "$SKILL_DIR/scripts/channel-creds.py" check --all
python3 "$SKILL_DIR/scripts/channel-creds.py" check slack telegram --json-out result.json
```

Each line reads `slack: ready (environment)` or
`telegram: skipped (missing TELEGRAM_BOT_TOKEN)`. The JSON report holds
`status`, `source`, `variables`, `reason`, `missing` and `invalid`, by name
only. Exit status is 0 whatever the result; add `--require-ready` to exit 3
when any selected channel is skipped.

To stage the bot side for a target, write it to a private file and send that
file over SSH stdin like the provider key:

```bash
python3 "$SKILL_DIR/scripts/channel-creds.py" write-env slack --out "$RUN_DIR/slack.env"
```

It writes mode `0600`, bot-side variables only (`--side tester` for the other
half), and exits 3 without writing anything if the channel is skipped. Delete
the file when the run ends.

## Store credentials in 1Password

The source of truth is the `nanoclaw-e2e` vault: one item per channel, titled
with the channel name from `channels.json` (`slack`, `whatsapp-cloud`), with one
field per variable, labelled with the variable name. The reference for a field
is `op://nanoclaw-e2e/<channel>/<VAR>`.

Create the items with placeholders, signed in to an account that can write the
vault (the CI service account is read-only):

```bash
op signin
python3 "$SKILL_DIR/scripts/op-create-items.py" --dry-run   # show the plan
python3 "$SKILL_DIR/scripts/op-create-items.py"             # all channels
python3 "$SKILL_DIR/scripts/op-create-items.py" slack telegram --account my.1password.com
```

Each item is a Secure Note tagged `nanoclaw-e2e`, with concealed fields in a
`bot` and a `tester` section, each set to `NC_E2E_PLACEHOLDER`. Running it again
only adds fields that are missing (for example after `channels.json` grows); it
never changes a field that exists, so real values are safe. Channels that store
nothing (CLI, Emacs, Matrix, Mattermost, and the session-only WhatsApp, WeChat
and Dial) get no item.

Replace a placeholder with the real value in 1Password. A placeholder or empty
field counts as missing, so the channel stays skipped until every field it needs
is real. For alternatives, fill one group and leave the other as a placeholder
(Linear: `LINEAR_API_KEY`, or `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET`).

Fill local files from the vault without copying values by hand:

```bash
umask 077; mkdir -p ~/.nanoclaw-e2e/channels
python3 "$SKILL_DIR/scripts/channel-creds.py" template slack > /tmp/slack.tpl
op inject -i /tmp/slack.tpl -o ~/.nanoclaw-e2e/channels/slack.env
python3 "$SKILL_DIR/scripts/channel-creds.py" template slack --side tester > /tmp/slack.tester.tpl
op inject -i /tmp/slack.tester.tpl -o ~/.nanoclaw-e2e/channels/slack.tester.env
```

Templates hold only references, so they are safe to keep. Never print, log or
commit a value; reports and logs name variables only.

## CI

[`.github/workflows/channel-e2e.yml`](../../.github/workflows/channel-e2e.yml)
runs on manual dispatch and nightly, never on pull requests, so fork code cannot
reach the secrets. One matrix job per channel runs in the protected `channel-e2e`
GitHub Environment, whose only secret is `OP_SERVICE_ACCOUNT_TOKEN`: a 1Password
service account with read access to the `nanoclaw-e2e` vault.

Each job writes the channel's `NC_E2E_*=op://...` references (`refs` command)
to the job environment, loads them with `1password/load-secrets-action`
(values are masked), and runs `check`. A vault item or field that does not exist
yet makes the load step fail softly; the unread references stay `op://` strings,
so the channel is reported skipped with those variables named. A final job
writes a table of every channel's status to the run summary.

## Source

Variable names were taken from NanoClaw `main` at `e533969` (2026-11-17):
`.claude/skills/add-<channel>/SKILL.md` and their `nc:env-set` blocks. Re-check
them when a channel skill changes its `.env` names.
