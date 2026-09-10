#!/usr/bin/env bash
# e2e-install.sh — headless NanoClaw install + CLI-channel ping, for e2e runs.
#
# Runs ON the target machine (an exe.dev VM, a CI runner, any Debian/Ubuntu
# box with sudo). Composes the setup wizard's own step processes — the exact
# `pnpm exec tsx setup/index.ts --step <name>` spawn setup/lib/runner.ts uses —
# in the wizard's order (setup/auto.ts), with every prompt replaced by an env
# var or pre-seeded state. Ends with the same round-trip the wizard's
# first-chat step performs (`pnpm run chat ping`, setup/lib/agent-ping.ts).
#
# Inputs (env):
#   NANOCLAW_E2E_ROOT          checkout to install (default: the current
#                              directory, which must be a NanoClaw checkout)
#   NANOCLAW_E2E_KEY_FILE      file holding an Anthropic API key / OAuth token
#                              (default ~/.nanoclaw-e2e/anthropic_key). Only
#                              read when the vault has no anthropic secret yet.
#   NANOCLAW_ONECLI_API_HOST   use a remote OneCLI gateway instead of installing
#   NANOCLAW_ONECLI_API_TOKEN    one locally (same vars setup/auto.ts honors)
#   NANOCLAW_DISPLAY_NAME      operator name for the e2e agent (default: E2E)
#   NANOCLAW_E2E_TZ            IANA zone written to .env (default: UTC)
#   NANOCLAW_E2E_KEEP_AGENT    1 (default) keeps the e2e agent so `verify` sees
#                              a registered group; 0 deletes it after the ping
#   NANOCLAW_E2E_FORCE_AUTH    1 replaces an existing vault secret with the key
#                              file (token rotation on a --base VM)
#
# Exit codes: 0 pass · 1 a step failed (see the status block above the failure
# and logs/e2e/<step>.log) · 2 ping got no reply · 3 CLI socket unreachable.

set -euo pipefail

# The checkout to install: NANOCLAW_E2E_ROOT, else the current directory.
# (The script ships in a plugin, so its own location says nothing about it.)
ROOT="${NANOCLAW_E2E_ROOT:-$PWD}"
cd "$ROOT"
grep -q '"name": *"nanoclaw"' package.json 2>/dev/null \
  || { echo "[e2e] FAIL: $ROOT is not a NanoClaw checkout (no package.json named nanoclaw); set NANOCLAW_E2E_ROOT" >&2; exit 1; }
export NANOCLAW_NO_DIAGNOSTICS=1 NANOCLAW_SKIP_CLAUDE_ASSIST=1
# setup/verify.ts counts any GITHUB_TOKEN in the environment as a configured
# channel (its has() reads process.env) — keep the report about this install.
unset GITHUB_TOKEN
export PATH="$HOME/.local/bin:$PATH"
KEY_FILE="${NANOCLAW_E2E_KEY_FILE:-$HOME/.nanoclaw-e2e/anthropic_key}"
LOGS="$ROOT/logs/e2e"
mkdir -p "$LOGS"

say() { printf '\n[e2e] %s\n' "$*"; }
die() { printf '[e2e] FAIL: %s\n' "$*" >&2; exit "${2:-1}"; }

# Run one wizard step exactly as setup/lib/runner.ts spawns it, capture the
# last `=== NANOCLAW SETUP: … === … === END ===` block (setup/status.ts) and
# expose its fields via $STATUS / field(). Returns the step's exit code.
LAST_BLOCK=""
STATUS=""
step() {
  local name="$1"; shift
  local out="$LOGS/$name.log"
  # Never echo a secret: the auth step carries the token as `--value <tok>`.
  local shown="" prev=""
  for a in "$@"; do [ "$prev" = "--value" ] && a="<redacted>"; shown="$shown $a"; prev="$a"; done
  say "step $name$shown"
  set +e
  pnpm exec tsx setup/index.ts --step "$name" "$@" </dev/null 2>&1 | tee "$out"
  local rc=${PIPESTATUS[0]}
  set -e
  LAST_BLOCK="$(awk '/^=== NANOCLAW SETUP: /{b=""} {b=b $0 "\n"} /^=== END ===/{last=b} END{printf "%s", last}' "$out")"
  STATUS="$(field STATUS)"
  echo "[e2e] $name -> exit=$rc status=${STATUS:-none}"
  return "$rc"
}
field() {
  printf '%s' "$LAST_BLOCK" | awk -v k="$1: " 'index($0, k)==1 {print substr($0, length(k)+1)}' | tail -n1
}

# ── 1. Basics: Node 22 + pnpm + `pnpm install --frozen-lockfile` ──────────────
# setup.sh is the launcher's prompt-free bootstrap (nanoclaw.sh runs it under a
# spinner); it calls setup/install-node.sh when Node is missing or < 22.
say "bootstrap: bash setup.sh"
if ! bash setup.sh > "$LOGS/bootstrap.log" 2>&1; then
  tail -40 "$LOGS/bootstrap.log"
  die "setup.sh failed (logs/e2e/bootstrap.log)"
fi
hash -r
if ! command -v pnpm >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
  export PATH="$(npm prefix -g)/bin:$PATH"
fi
command -v pnpm >/dev/null 2>&1 || die "pnpm not on PATH after setup.sh"

# ── 2. Docker: install if missing (setup's own script), make the socket usable ─
# setup/container.ts does this for its own step, but the onecli step (a
# docker-compose install) needs the daemon first — so handle it once, here.
if ! command -v docker >/dev/null 2>&1; then
  bash setup/install-docker.sh 2>&1 | tee "$LOGS/install-docker.log"
fi
if ! docker info >/dev/null 2>&1; then
  sudo systemctl start docker 2>/dev/null || sudo service docker start 2>/dev/null || true
fi
if ! docker info >/dev/null 2>&1; then
  if [ "${NANOCLAW_E2E_SG:-}" = 1 ]; then die "docker socket still not accessible under sg docker"; fi
  if [ "$(id -u)" -eq 0 ]; then die "docker daemon not reachable"; fi
  # Supplementary groups are fixed at login; re-exec with docker as the
  # primary group (same trick as setup/container.ts).
  if ! id -nG | tr ' ' '\n' | grep -qx docker; then sudo usermod -aG docker "$USER"; fi
  say "re-executing under sg docker"
  export NANOCLAW_E2E_SG=1
  exec sg docker -c "bash $(printf '%q' "${BASH_SOURCE[0]}")"
fi

# ── 3. Wizard steps, in setup/auto.ts order ───────────────────────────────────
step environment || die "environment step failed"

if [ -n "${NANOCLAW_ONECLI_API_HOST:-}" ]; then
  step onecli --remote-url "$NANOCLAW_ONECLI_API_HOST" || die "onecli (remote) failed"
elif command -v onecli >/dev/null 2>&1 && [ -f .env ] && grep -q '^ONECLI_URL=' .env; then
  step onecli --reuse || die "onecli (reuse) failed"
else
  step onecli || die "onecli install failed"
fi
export PATH="$HOME/.local/bin:$PATH"; hash -r

# Auth: the wizard's runAuthStep short-circuits when the vault already holds an
# anthropic secret; mirror that, otherwise seed it the way setup/auth.ts does.
step auth --check || true
# NANOCLAW_E2E_FORCE_AUTH=1 replaces an existing vault secret (e.g. after
# rotating the token on a --base VM whose snapshot still holds the old one).
if [ "${NANOCLAW_E2E_FORCE_AUTH:-0}" = 1 ] && [ "$STATUS" = "success" ]; then
  [ -r "$KEY_FILE" ] || die "NANOCLAW_E2E_FORCE_AUTH=1 but $KEY_FILE is unreadable"
  step auth --create --force --value "$(tr -d '\r\n' < "$KEY_FILE")" || die "auth --create --force failed"
  [ "$STATUS" = "success" ] || die "auth --create --force reported $STATUS"
elif [ "$STATUS" = "missing" ]; then
  [ -r "$KEY_FILE" ] || die "vault has no anthropic secret and $KEY_FILE is unreadable"
  # --value rides argv into the step (which then execFileSync's onecli) — it
  # is visible to `ps` on this machine for the duration. Disposable VMs only.
  step auth --create --value "$(tr -d '\r\n' < "$KEY_FILE")" || die "auth --create failed"
  [ "$STATUS" = "success" ] || die "auth --create reported $STATUS"
elif [ "$STATUS" != "success" ]; then
  die "auth --check reported ${STATUS:-nothing} (is the OneCLI gateway up?)"
fi

# Local image build unless .env sets NANOCLAW_HARDENED_IMAGE=true (pull path).
# 3–10 minutes cold; snapshot the VM afterwards (see SKILL.md) to skip it.
step container || die "container step failed"
step mounts --empty || die "mounts step failed"
step timezone --tz "${NANOCLAW_E2E_TZ:-UTC}" || die "timezone step failed"

# Service: stamps the upgrade marker, builds, installs systemd (system unit as
# root, user unit otherwise) or — with no user systemd — only WRITES the nohup
# wrapper without starting it (SERVICE_LOADED: false). Start it ourselves.
step service || die "service step failed"
SERVICE_TYPE="$(field SERVICE_TYPE)"
if [ "$SERVICE_TYPE" = "nohup" ]; then
  say "no systemd — starting via ./start-nanoclaw.sh"
  bash ./start-nanoclaw.sh
fi

# ── 4. Wire an agent to the always-on cli channel and ping it ─────────────────
say "init-cli-agent"
pnpm exec tsx scripts/init-cli-agent.ts \
  --display-name "${NANOCLAW_DISPLAY_NAME:-E2E}" --agent-name "E2E Agent" --folder e2e-agent \
  2>&1 | tee "$LOGS/init-cli-agent.log"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "init-cli-agent failed"

say "waiting for data/cli.sock"
for _ in $(seq 1 60); do [ -S data/cli.sock ] && break; sleep 1; done
[ -S data/cli.sock ] || die "host never opened data/cli.sock — see logs/nanoclaw.error.log" 3

# Same probe as setup/lib/agent-ping.ts: exit 0 + stdout = ok, 2 = socket,
# 3 = no reply (chat.ts has its own 120s hard stop); auth failures show up in
# the reply text.
say "ping (first container boot: 30–60s)"
set +e
PING_OUT="$(timeout 150 pnpm --silent run chat ping 2>"$LOGS/ping.err")"
PING_RC=$?
set -e
printf '%s\n' "$PING_OUT" | tee "$LOGS/ping.out"
if printf '%s\n%s' "$PING_OUT" "$(cat "$LOGS/ping.err")" | grep -qiE 'Invalid bearer token|authentication[_ ]error|Failed to authenticate|Please run /login|Not logged in|Invalid API key'; then
  die "ping reply is an auth error — check the vault secret" 2
fi
case "$PING_RC" in
  0) [ -n "$PING_OUT" ] || die "ping exited 0 with an empty reply" 2 ;;
  2) die "CLI socket unreachable (chat.ts exit 2)" 3 ;;
  *) die "no reply from the agent (chat.ts exit $PING_RC) — logs/nanoclaw.log, logs/e2e/ping.err" 2 ;;
esac

if [ "${NANOCLAW_E2E_KEEP_AGENT:-1}" = 0 ]; then
  pnpm exec tsx scripts/delete-cli-agent.ts --folder e2e-agent
fi

# ── 5. The wizard's end-of-run health check ───────────────────────────────────
step verify || die "verify reported ${STATUS:-failure}"

cat <<SUMMARY

=== NANOCLAW E2E: RESULT ===
STATUS: pass
ROOT: $ROOT
SERVICE_TYPE: $SERVICE_TYPE
PING: ok
REPLY: $(printf '%s' "$PING_OUT" | head -c 200 | tr '\n' ' ')
LOG: logs/e2e/
=== END ===
SUMMARY
