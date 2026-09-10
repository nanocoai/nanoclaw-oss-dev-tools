#!/usr/bin/env bash
# exe-run.sh — create (or clone) an exe.dev VM and run e2e-install.sh on it.
#
# Runs on the OPERATOR machine, from the checkout root. Needs `ssh exe.dev`
# working (exe.dev account + SSH key) and the API key in a local file.
#
# Usage:
#   scripts/exe-run.sh [--name NAME] [--ref GIT_REF] [--repo URL]
#                      [--key-file PATH] [--base VM] [--snapshot NAME]
#                      [--cpu N] [--memory SIZE] [--disk SIZE]
#
#   --name      VM name (default nanoclaw-e2e-<short sha of --ref>)
#   --ref       git ref to test (default: current HEAD sha)
#   --repo      clone URL (default: this checkout's `origin`)
#   --key-file  Anthropic API key / OAuth token file (default ~/.nanoclaw-e2e/anthropic_key)
#   --base      `ssh exe.dev cp BASE NAME` instead of `new` — a VM that already
#               ran this once has Docker, OneCLI and the agent image, so the
#               3–10 min build is skipped (the install steps are idempotent)
#   --snapshot  on success, `ssh exe.dev cp NAME SNAPSHOT` for future --base
#
# The installer is pushed over SSH from THIS checkout, so an older --ref that
# predates the skill still gets tested. Secrets travel over stdin, never argv.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REF="$(git rev-parse HEAD)"
REPO="$(git remote get-url origin)"
KEY_FILE="$HOME/.nanoclaw-e2e/anthropic_key"
NAME="" BASE="" SNAPSHOT="" CPU=4 MEMORY=8GB DISK=40GB

while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --key-file) KEY_FILE="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    --snapshot) SNAPSHOT="$2"; shift 2 ;;
    --cpu) CPU="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 64 ;;
  esac
done
[ -r "$KEY_FILE" ] || { echo "key file not readable: $KEY_FILE" >&2; exit 66; }
[ -n "$NAME" ] || NAME="nanoclaw-e2e-$(git rev-parse --short "$REF")"
HOST="$NAME.exe.xyz"

echo "[exe-run] vm=$NAME ref=$REF repo=$REPO"
if [ -n "$BASE" ]; then
  ssh exe.dev cp "$BASE" "$NAME" --cpu="$CPU" --memory="$MEMORY" --disk="$DISK"
else
  ssh exe.dev new --name="$NAME" --cpu="$CPU" --memory="$MEMORY" --disk="$DISK" --no-email
fi

echo "[exe-run] waiting for ssh $HOST"
for _ in $(seq 1 60); do
  ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$HOST" true 2>/dev/null && break
  sleep 2
done
ssh -o BatchMode=yes "$HOST" true || { echo "[exe-run] $HOST not reachable" >&2; exit 69; }

# Secret over stdin into a 0600 file the installer reads.
ssh "$HOST" 'umask 077; mkdir -p ~/.nanoclaw-e2e; cat > ~/.nanoclaw-e2e/anthropic_key' < "$KEY_FILE"
ssh "$HOST" 'cat > ~/e2e-install.sh; chmod +x ~/e2e-install.sh' < "$HERE/e2e-install.sh"

# Fresh clone at the requested ref; a --base VM keeps its data/ and vault, and
# every step below is idempotent against them.
ssh "$HOST" "set -e
  command -v git >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq git; }
  if [ -d ~/nanoclaw/.git ]; then git -C ~/nanoclaw fetch -q origin; else git clone -q '$REPO' ~/nanoclaw; fi
  git -C ~/nanoclaw checkout -q '$REF'
  cd ~/nanoclaw && NANOCLAW_E2E_ROOT=\$HOME/nanoclaw bash ~/e2e-install.sh"
RC=$?

if [ "$RC" -eq 0 ] && [ -n "$SNAPSHOT" ]; then
  echo "[exe-run] snapshotting $NAME -> $SNAPSHOT"
  ssh exe.dev cp "$NAME" "$SNAPSHOT"
fi
echo "[exe-run] done rc=$RC · ssh $HOST · logs in ~/nanoclaw/logs/{e2e,nanoclaw.log}"
exit "$RC"
