# Supervised provider authentication

Use this mode when the operator selects live sign-in while testing NanoClaw's
public wizard. It is available through `scripts/proxmox-wizard.py` and directly
through `scripts/wizard-run.py`; the exe.dev driver (`exe-run.sh --interactive
--supervised-human-auth`) supports Codex device pairing only, because that
handoff needs no return channel. Claude subscription sign-in, the Windows and
the headless lifecycle entry points keep their existing rules.

Resolve the NanoClaw and provider-payload commits and show the source-discovered
auth choices as described in the parent skill. Confirm the chosen method and
the operator's availability for the live handoff before starting. If the user
has already requested that sign-in and is ready, do not ask again. No account
creation or reuse of a personal CLI login is implied.

## Select the provider's actual flow

Add `--supervised-human-auth` to the normal Proxmox or direct wizard command and
omit `--credential-file`:

| Provider | Auth method | Human action |
|---|---|---|
| `codex` | `device` | Open the verification URL and enter the displayed one-time code in the browser. |
| `claude` | `subscription` | Open the authorization URL; if the browser returns a code, return it through the private response file below. |

These methods must be offered by the exact source revision. Other browser or
subscription methods remain unsupported and fail before provisioning.
Claude API/OAuth-token and Codex API-key paths keep their existing behavior.

Claude uses `claude setup-token`, which opens the normal Claude authorization
flow and prints a subscription token after approval. NanoClaw captures that
token and registers it in OneCLI. Do not ask the user to paste that long-lived
token into chat. Claude's remote authorization-code return is different from
Codex's code-entry-in-browser flow. See the official
[Claude authentication documentation](https://code.claude.com/docs/en/authentication).

## Handle the private handoff

The runner creates a private directory for its run ID and writes
`~/.nanoclaw-e2e/auth-handoffs/<run_id>/request.json` on the test guest with mode
`0600`. The lifecycle result supplies the run ID. The request contains `run_id`,
`nonce` and `created_at`, plus either:

- Codex: `verification_url` and `user_code`. No response file is needed.
- Claude: `provider: "claude"`, `auth_method: "subscription"`,
  `authorization_url` and `response_required: true`.

Copy the request privately from the owned guest without printing its contents
to a transcript or outer run log. Show it through a private file or the live
terminal so the operator can complete the browser step. Do not include the
handoff in exported artifacts, issue bodies or public comments. The exe.dev
driver does this itself: while the guest wizard runs it polls for the request
and writes it once to `<result-file>.handoff` (mode `0600`), printing only
that path. Tell the operator to open the link and enter the code from that
file; the runner waits up to 10 minutes at the pairing prompt.

The tested payload's `codex login` runs on the guest host, so the host needs
the Codex CLI. Payloads with a manifest-pinned fallback start it themselves
(`--require-codex-cli-fallback` proves that path); for a payload without one
(nanoclaw `290aa683` bundles its auth hook and has no fallback), the runner
installs the exact `@openai/codex` pin from the tested `add-codex` skill under
`~/.local` when the wizard reaches the auth prompt, and records it as
`host_codex_cli` in the Codex receipt (also on a failed run). A host that
already has `codex` keeps it: exe.dev images ship one under
`/usr/local/bin`. `--require-codex-cli-fallback` and that install are
mutually exclusive.

If Claude asks for a returned code, prepare a private response using that
request's exact `run_id` and `nonce`, with the browser's value in
`authorization_code`. Send the JSON through SSH stdin to the guest file
`~/.nanoclaw-e2e/auth-handoffs/<run_id>/response.json` with mode `0600` and the test user's
ownership; do not put the code in command arguments. The runner consumes it
only while the known code prompt is active, validates the request binding,
writes it directly to the PTY and removes the response after use. An old
request's response cannot satisfy another run.

The runner submits the setup script's prefilled `claude setup-token` command
and supplies a returned code only at the known code prompt. Unknown prompts
still stop the test. A handoff gets
a bounded wait within the overall wizard deadline. The runner removes its own
request and valid response; malformed files stay private on the retained target
for inspection. Remove private controller copies when the handoff is finished.
Missing input or expired authentication is a failed run,
never permission to select another method or inject a saved credential.

## Required proof

All normal wizard, retained-reply, service, socket, UTC, evidence and ownership
checks still apply. Supervised auth additionally requires the selected provider's
vault entry (a OneCLI listing, or under Iron Proxy the adapter's own
`has('codex')` plus its broker-and-two-secrets metadata) and an actual retained
session whose effective provider matches that provider. The verifier reads the group configuration and every retained session
through `ncl`, then calls the exact installed `resolveProviderName`. A null group
provider and null session override correctly resolve to Claude; a non-null session
override takes precedence and an effective mismatch fails verification.
Claude's `auth: interactive` status is accepted only for supervised subscription
auth, with evidence of token capture and vault registration. It is not a generic
replacement for a successful setup step.

For the Codex missing-host-CLI regression, also pass
`--require-codex-cli-fallback`. This requires supervised Codex device pairing and
adds proof that the host CLI was absent and NanoClaw invoked the CLI version
pinned in its installed manifest. Ordinary device pairing can use an existing
host CLI. An installable provider's copied payload is compared with its selected
commit before sign-in and again before accepting the result.

The source contracts were inspected at NanoClaw
`3f9ed607b7e7a4872747295f75286f1c377d7c33`: `setup/auto.ts` and
`setup/register-claude-token.sh` for Claude, plus the separately recorded Codex
provider payload's `setup/providers/codex.ts`. Recheck those contracts when
testing another revision. Offline tests do not qualify a live sign-in or model
reply. On 2026-09-13, a fresh Debian 13.6 Proxmox run at tooling commit
`7200211d9c86592a25e66694451b6e503202f014` passed Codex device pairing against
that core and PR #3792 payload `b6faffcfd83ee477ed8f477724985c78cce450eb`.
It verified all 22 copied provider files, the missing-host-CLI fallback to
`@openai/codex@0.146.0`, a dedicated vault entry, an actual retained Codex-agent
reply, and the running systemd user service and socket. The first attempt failed
in the harness before human sign-in and remains a failed, retained run.

On 2026-09-13, a fresh Debian 13.6 Proxmox LXC completed the public wizard at
that NanoClaw core through a shared, visible real PTY. A human completed Claude
subscription authorization directly in the terminal. The run verified one
Anthropic vault entry, the retained agent's answer to `350 * 193`, saved session
history, the running systemd user service, its exact checkout entrypoint, and
the CLI socket after SSH logout. NanoClaw's null group-provider and null
session-provider fields resolved to Claude through the exact installed resolver.
This qualifies the visible manual public-wizard flow only; the unattended
handoff runner still awaits a successful live pass.

An earlier fresh Claude attempt at tooling
`4d2c8309a604a0e1fa7decdfe479a73f00ad226b` reached
Claude Code 2.1.270's code prompt but timed out before human authorization:
that CLI prints `https://claude.com/cai/oauth/authorize`, which the older
recognizer did not support. The retained failure drove regression coverage
for the current and legacy endpoints, wrapped URLs, blank lines before the
code prompt, and authorization-link redaction independent of prompt detection.
The collector also rejects remaining authorization URLs before accepting an
export. A second attempt at tooling `2d564277bf1e228fc17a0331a1e80fb9d26aaf86`
also timed out before handoff. Its export correctly redacted the link. The
exact raw transport mismatch remains unverified; Claude handoff and response
gating now use the terminal emulator's current rendered screen. Synthetic
nested-PTY, redraw, wrapping, stale-prompt and exactly-once input regressions
pass. The unattended correction still requires its own successful live run.
Subsequent split-chunk and owning-remote
transport corrections have offline regression coverage; the Codex live result
applies to its recorded tooling commit.
