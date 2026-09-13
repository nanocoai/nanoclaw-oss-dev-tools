# Supervised provider authentication

Use this mode when the operator selects live sign-in while testing NanoClaw's
public wizard. It is available through `scripts/proxmox-wizard.py` and directly
through `scripts/wizard-run.py`. The exe.dev, Windows and headless lifecycle
entry points continue to require their supported credential-file methods.

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
handoff in exported artifacts, issue bodies or public comments.

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
vault entry and an actual retained agent configuration matching that provider.
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
reply; both new supervised methods still require live qualification.
