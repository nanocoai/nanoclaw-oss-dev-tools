#!/usr/bin/env bash
# exe-run.sh — create (or clone) an exe.dev VM and run e2e-install.sh on it.
#
# Runs on the OPERATOR machine, from the checkout root. Needs `ssh exe.dev`
# working (exe.dev account + SSH key; see the SSH config stanza in SKILL.md)
# and the API key in a local file.
#
# Usage:
#   scripts/exe-run.sh [--name NAME] [--ref GIT_REF] [--repo URL]
#                      [--key-file PATH] [--base VM] [--snapshot NAME] [--rm]
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
#   --rm        on success, `ssh exe.dev rm NAME` (a failed VM is always kept
#               for inspection)
#
# Two SSH destinations (exe.dev's own skill spells this out): `ssh exe.dev
# <cmd>` is the lobby — VM lifecycle only, no shell/scp; `ssh <ssh_dest>` is
# the VM — full shell. The installer is pushed over the VM connection from
# THIS checkout, so an older --ref that predates the skill still gets tested.
# Secrets travel over stdin, never argv.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REF="$(git rev-parse HEAD)"
REPO="$(git remote get-url origin)"
KEY_FILE="$HOME/.nanoclaw-e2e/anthropic_key"
NAME="" BASE="" SNAPSHOT="" RM=0 CPU=4 MEMORY=8GB DISK=40GB

while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --key-file) KEY_FILE="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    --snapshot) SNAPSHOT="$2"; shift 2 ;;
    --rm) RM=1; shift ;;
    --cpu) CPU="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 64 ;;
  esac
done
[ -r "$KEY_FILE" ] || { echo "key file not readable: $KEY_FILE" >&2; exit 66; }
[ -n "$NAME" ] || NAME="nanoclaw-e2e-$(git rev-parse --short "$REF")"

# `new --json` / `cp --json` return a VM record; `ls --json` returns
# {"vms": [...], "team_shared_vms": [...]}. Owned VMs carry ssh_dest (and
# ssh_host); team-shared ones only dns_name/ssh_command. Modern VMs report
# "<name>.exe.xyz", legacy ones "vm+<name>@vm.exe.xyz" — ssh takes either
# verbatim, so never synthesize the hostname from the name. Records nest
# objects (access, sharing, …), so parse JSON properly rather than by regex.
ssh_dest_of() {  # stdin: JSON; $1 (optional): vm_name to select inside a list
  python3 -c '
import json, sys
want = sys.argv[1] if len(sys.argv) > 1 else None
d = json.load(sys.stdin)
recs = [d] if "vm_name" in d else (d.get("vms", []) + d.get("team_shared_vms", []))
for r in recs:
    if want and r.get("vm_name") != want: continue
    dest = r.get("ssh_dest") or r.get("ssh_host") or r.get("dns_name")
    if dest: print(dest); break
' "$@"
}

echo "[exe-run] vm=$NAME ref=$REF repo=$REPO"
if [ -n "$BASE" ]; then
  CREATED="$(ssh exe.dev cp "$BASE" "$NAME" --cpu="$CPU" --memory="$MEMORY" --disk="$DISK" --json | tee /dev/stderr)"
else
  CREATED="$(ssh exe.dev new --name="$NAME" --cpu="$CPU" --memory="$MEMORY" --disk="$DISK" --no-email --json | tee /dev/stderr)"
fi
# The lobby answers a taken name with a plain "name … is not available" line
# and no record (VM names are global across exe.dev, like hostnames), so an
# empty resolution here means the VM was not created.
HOST="$(printf '%s' "$CREATED" | ssh_dest_of "$NAME" 2>/dev/null || true)"
[ -n "$HOST" ] || HOST="$(ssh exe.dev ls --json | ssh_dest_of "$NAME")"
[ -n "$HOST" ] || { echo "[exe-run] $NAME was not created (name taken? see the lobby output above; try another --name)" >&2; exit 69; }

# First contact with a fresh VM would otherwise block on the host-key prompt.
VM=(ssh -o StrictHostKeyChecking=accept-new "$HOST")

echo "[exe-run] waiting for ssh $HOST"
for _ in $(seq 1 60); do
  "${VM[@]}" -o BatchMode=yes -o ConnectTimeout=5 true 2>/dev/null && break
  sleep 2
done
"${VM[@]}" -o BatchMode=yes true || { echo "[exe-run] $HOST not reachable" >&2; exit 69; }

# Secret over stdin into a 0600 file the installer reads.
"${VM[@]}" 'umask 077; mkdir -p ~/.nanoclaw-e2e; cat > ~/.nanoclaw-e2e/anthropic_key' < "$KEY_FILE"
"${VM[@]}" 'cat > ~/e2e-install.sh; chmod +x ~/e2e-install.sh' < "$HERE/e2e-install.sh"

# Fresh clone at the requested ref; a --base VM keeps its data/ and vault, and
# every step below is idempotent against them.
set +e
"${VM[@]}" "set -e
  command -v git >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq git; }
  if [ -d ~/nanoclaw/.git ]; then git -C ~/nanoclaw fetch -q origin; else git clone -q '$REPO' ~/nanoclaw; fi
  git -C ~/nanoclaw checkout -q '$REF'
  cd ~/nanoclaw && NANOCLAW_E2E_ROOT=\$HOME/nanoclaw bash ~/e2e-install.sh"
RC=$?
set -e

if [ "$RC" -eq 0 ] && [ -n "$SNAPSHOT" ]; then
  echo "[exe-run] snapshotting $NAME -> $SNAPSHOT"
  ssh exe.dev cp "$NAME" "$SNAPSHOT"
  if [ -z "$(ssh exe.dev ls --json | ssh_dest_of "$SNAPSHOT")" ]; then
    echo "[exe-run] snapshot $SNAPSHOT was not created (name taken? pick a globally unique one)" >&2
    RC=70
  fi
fi
if [ "$RC" -eq 0 ] && [ "$RM" -eq 1 ]; then
  echo "[exe-run] deleting $NAME"
  ssh exe.dev rm "$NAME"
  echo "[exe-run] done rc=0 (vm removed)"
  exit 0
fi
echo "[exe-run] done rc=$RC · ssh $HOST · logs in ~/nanoclaw/logs/{e2e,nanoclaw.log} · delete with: ssh exe.dev rm $NAME"
exit "$RC"
