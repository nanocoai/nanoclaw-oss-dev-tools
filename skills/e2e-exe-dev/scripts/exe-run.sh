#!/usr/bin/env bash
# exe-run.sh — create (or clone) an exe.dev VM and run e2e-install.sh on it.
#
# Run from the NanoClaw checkout root, using this installed script's full path.
#
# Usage:
#   bash /path/to/e2e-exe-dev/scripts/exe-run.sh [options]
#
#   --name      VM name (default nanoclaw-e2e-<short sha>-<random suffix>)
#   --ref       local git ref to test (default HEAD); must be fetchable from repo
#   --repo      clone URL (default this checkout's origin)
#   --key-file  Anthropic key/token file (default ~/.nanoclaw-e2e/anthropic_key)
#   --base      clone this VM, keeping its data, vault and image cache
#   --snapshot  on success, copy the tested VM to this new name
#   --rm        delete this run's VM on success; failures are kept
#   --cpu       CPU count (default 4)
#   --memory    memory size (default 8GB)
#   --disk      disk size (default 40GB)
#   --result-file  record this invocation locally, then export installer result
#
# The installer comes from this skill, not the NanoClaw checkout being tested.
# Keys and forwarded installer settings travel over SSH stdin.
# See SKILL.md for prerequisites, supported environment variables and results.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REF=HEAD REPO="" KEY_FILE="${NANOCLAW_E2E_KEY_FILE:-$HOME/.nanoclaw-e2e/anthropic_key}"
NAME="" BASE="" SNAPSHOT="" RM=0 CPU=4 MEMORY=8GB DISK=40GB
RESULT_FILE=""

fail() { echo "[exe-run] $1" >&2; exit "${2:-64}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --name|--ref|--repo|--key-file|--base|--snapshot|--cpu|--memory|--disk|--result-file)
      [ $# -ge 2 ] && [ -n "$2" ] && [[ "$2" != --* ]] || fail "$1 requires a value" ;;
  esac
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
    --result-file) RESULT_FILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,23p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) fail "unknown flag: $1" ;;
  esac
done

command -v python3 >/dev/null || fail "python3 is required" 69
COMMIT="" PHASE=preflight RESULT_EXPORTED=0
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
write_driver_result() {
  python3 - "$RESULT_FILE" "$1" "$2" "$REF" "$COMMIT" "$PHASE" "$STARTED_AT" <<'PY'
import datetime, json, os, sys, tempfile
path, status, rc, ref, commit, phase, started = sys.argv[1:]
result = {
    "schema_version": 1,
    "status": status,
    "exit_code": int(rc) if rc else None,
    "commit": None,
    "requested_ref": ref,
    "requested_commit": commit or None,
    "phase": phase,
    "started_at": started,
    "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat() if rc else None,
}
fd, temporary = tempfile.mkstemp(prefix=".e2e-result-", dir=os.path.dirname(os.path.abspath(path)))
try:
    with os.fdopen(fd, "w") as out:
        json.dump(result, out, indent=2)
        out.write("\n")
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}
finish_driver_result() {
  local rc=$?
  trap - EXIT
  if [ "$RESULT_EXPORTED" -eq 0 ]; then
    [ "$rc" -ne 0 ] || rc=74
    write_driver_result failed "$rc" || echo "[exe-run] could not finalize local result: $RESULT_FILE" >&2
  fi
  exit "$rc"
}
if [ -n "$RESULT_FILE" ]; then
  [ -d "$(dirname "$RESULT_FILE")" ] && [ -w "$(dirname "$RESULT_FILE")" ] \
    && [ ! -d "$RESULT_FILE" ] || fail "--result-file needs a writable parent directory and a file path"
  # Invalidate an old local pass before preflight or VM work can fail. Until
  # a matching installer result is exported, failures get a driver report;
  # commit stays null because no tested revision has been confirmed.
  write_driver_result running "" || fail "could not initialize local result: $RESULT_FILE" 74
  trap finish_driver_result EXIT
fi
for cmd in git ssh; do command -v "$cmd" >/dev/null || fail "$cmd is required" 69; done
grep -q '"name": *"nanoclaw"' package.json 2>/dev/null \
  || fail "run this from the root of a NanoClaw checkout" 65
COMMIT="$(git rev-parse --verify --end-of-options "$REF^{commit}")" \
  || fail "cannot resolve local ref: $REF" 65
[ -n "$REPO" ] || REPO="$(git remote get-url origin)"
[ -r "$KEY_FILE" ] && [ -s "$KEY_FILE" ] || fail "key file is unreadable or empty: $KEY_FILE" 66
[ -n "$NAME" ] || NAME="nanoclaw-e2e-${COMMIT:0:7}-$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
# SSH joins lobby arguments into a command string. Permit only literal names
# and resource values here; repository URLs are shell-quoted separately below.
for value in "$NAME" "$BASE" "$SNAPSHOT"; do
  [ -z "$value" ] || [[ "$value" =~ ^[a-zA-Z0-9][a-zA-Z0-9-]*$ ]] || fail "invalid VM name: $value"
done
[[ "$CPU" =~ ^[1-9][0-9]*$ ]] || fail "invalid CPU count: $CPU"
for value in "$MEMORY" "$DISK"; do
  [[ "$value" =~ ^[1-9][0-9]*([KMGTP]i?B?)?$ ]] || fail "invalid resource size: $value"
done
if [ -n "$(git status --porcelain)" ]; then
  echo "[exe-run] local working-tree changes are not included; testing commit $COMMIT" >&2
fi

# Only a matching record from this create/copy response proves ownership.
# Never recover a missing record by looking up an existing VM by name.
created_host() {
  python3 -c '
import json, sys
try:
    record = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
if not isinstance(record, dict):
    sys.exit(1)
expected, source = sys.argv[1:]
name = record.get("vm_name")
if source:
    # Current cp responses use name/source; older records use vm_name.
    # The copy-specific shape must confirm both ends of the operation.
    if "vm_name" not in record:
        name = record.get("name")
        if record.get("source") != source:
            sys.exit(1)
    elif "source" in record and record["source"] != source:
        sys.exit(1)
if name != expected:
    sys.exit(1)
dest = record.get("ssh_dest") or record.get("ssh_host") or record.get("dns_name")
if not isinstance(dest, str) or not dest or dest.startswith("-"):
    sys.exit(1)
if any(c.isspace() or ord(c) < 32 for c in dest):
    sys.exit(1)
print(dest)
' "$1" "$2"
}

echo "[exe-run] vm=$NAME ref=$REF commit=$COMMIT repo=$REPO"
PHASE=create
if [ -n "$BASE" ]; then
  CREATED="$(ssh -o BatchMode=yes exe.dev cp "$BASE" "$NAME" --cpu="$CPU" --memory="$MEMORY" --disk="$DISK" --json)" \
    || fail "copy failed; VM creation was not confirmed" 69
else
  CREATED="$(ssh -o BatchMode=yes exe.dev new --name="$NAME" --cpu="$CPU" --memory="$MEMORY" --disk="$DISK" --no-email --json)" \
    || fail "VM creation failed" 69
fi
printf '%s\n' "$CREATED" >&2
HOST="$(printf '%s' "$CREATED" | created_host "$NAME" "$BASE")" \
  || fail "$NAME creation was not confirmed (name taken or invalid response); no VM will be contacted or removed" 69

VM=(ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5 "$HOST")
PHASE=connect
echo "[exe-run] waiting for ssh $HOST"
READY=0
for _ in $(seq 1 60); do
  if "${VM[@]}" true 2>/dev/null; then READY=1; break; fi
  sleep 2
done
[ "$READY" -eq 1 ] || fail "$HOST not reachable; kept $NAME for inspection" 69

# chmod also covers a file inherited from a base VM; umask alone does not.
PHASE=upload
"${VM[@]}" 'set -e; umask 077; mkdir -p ~/.nanoclaw-e2e; cat > ~/.nanoclaw-e2e/anthropic_key; chmod 600 ~/.nanoclaw-e2e/anthropic_key' < "$KEY_FILE"
"${VM[@]}" 'set -e; cat > ~/e2e-install.sh; chmod +x ~/e2e-install.sh' < "$HERE/e2e-install.sh"

# Forward only documented settings. A remote gateway token must never appear
# in an SSH command, process argument or driver log.
python3 - <<'PY' | "${VM[@]}" 'set -e; umask 077; cat > ~/.nanoclaw-e2e/run-env.sh; chmod 600 ~/.nanoclaw-e2e/run-env.sh'
import os, shlex
for name in (
    "NANOCLAW_ONECLI_API_HOST", "NANOCLAW_ONECLI_API_TOKEN",
    "NANOCLAW_DISPLAY_NAME", "NANOCLAW_E2E_TZ",
    "NANOCLAW_E2E_KEEP_AGENT", "NANOCLAW_E2E_FORCE_AUTH",
):
    if name in os.environ:
        print("export " + name + "=" + shlex.quote(os.environ[name]))
PY

REPO_QUOTED="$(python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$REPO")"
PHASE=checkout
set +e
"${VM[@]}" "set -e
  if [ -d ~/nanoclaw/.git ]; then
    mkdir -p ~/nanoclaw/logs/e2e
    printf '{\"schema_version\":1,\"status\":\"running\",\"phase\":\"checkout\",\"requested_commit\":\"$COMMIT\"}\n' > ~/nanoclaw/logs/e2e/result.json
  fi
  command -v git >/dev/null && command -v python3 >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq git python3; }
  if [ -d ~/nanoclaw/.git ]; then
    git -C ~/nanoclaw diff --quiet HEAD -- || { echo 'base checkout has tracked changes; refusing to overwrite them' >&2; exit 65; }
    git -C ~/nanoclaw remote set-url origin $REPO_QUOTED
  else
    git clone -q -- $REPO_QUOTED ~/nanoclaw
  fi
  git -C ~/nanoclaw fetch -q origin '$COMMIT'
  git -C ~/nanoclaw checkout -q --detach '$COMMIT'
  test \"\$(git -C ~/nanoclaw rev-parse HEAD)\" = '$COMMIT'
  . ~/.nanoclaw-e2e/run-env.sh
  rm ~/.nanoclaw-e2e/run-env.sh
  cd ~/nanoclaw && NANOCLAW_E2E_ROOT=\$HOME/nanoclaw bash ~/e2e-install.sh"
RC=$?
set -e

if [ -n "$RESULT_FILE" ]; then
  [ "$RC" -ne 0 ] || PHASE=export
  if "${VM[@]}" 'cat ~/nanoclaw/logs/e2e/result.json' | python3 -c '
import json, os, sys, tempfile
path, expected, rc = sys.argv[1:]
result = json.load(sys.stdin)
if not isinstance(result, dict) or result.get("schema_version") != 1:
    sys.exit("invalid E2E result schema")
if int(rc) == 0:
    if result.get("commit") != expected or result.get("status") != "pass" or result.get("exit_code") != 0:
        sys.exit("E2E result does not match the completed run")
elif result.get("commit") != expected or result.get("status") != "failed" or result.get("exit_code") != int(rc):
    sys.exit("no matching completed installer result for this failed run")
fd, temporary = tempfile.mkstemp(prefix=".e2e-result-", dir=os.path.dirname(os.path.abspath(path)))
try:
    with os.fdopen(fd, "w") as out:
        json.dump(result, out, indent=2)
        out.write("\n")
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
' "$RESULT_FILE" "$COMMIT" "$RC"; then
    RESULT_EXPORTED=1
    echo "[exe-run] saved result: $RESULT_FILE"
  else
    echo "[exe-run] could not export this run's result; keeping $NAME" >&2
    [ "$RC" -ne 0 ] || RC=74
  fi
fi

if [ "$RC" -eq 0 ] && [ -n "$SNAPSHOT" ]; then
  echo "[exe-run] snapshotting $NAME -> $SNAPSHOT"
  if COPIED="$(ssh -o BatchMode=yes exe.dev cp "$NAME" "$SNAPSHOT" --json)"; then
    COPY_RC=0
  else
    COPY_RC=$?
  fi
  printf '%s\n' "$COPIED" >&2
  if [ "$COPY_RC" -eq 0 ] \
    && printf '%s' "$COPIED" | created_host "$SNAPSHOT" "$NAME" >/dev/null; then
    echo "[exe-run] snapshot confirmed: $SNAPSHOT"
  else
    echo "[exe-run] snapshot $SNAPSHOT creation was not confirmed; keeping $NAME" >&2
    RC=70
  fi
fi
if [ "$RC" -eq 0 ] && [ "$RM" -eq 1 ]; then
  echo "[exe-run] deleting $NAME"
  ssh -o BatchMode=yes exe.dev rm "$NAME"
  echo "[exe-run] done rc=0 commit=$COMMIT (vm removed)"
  exit 0
fi
echo "[exe-run] done rc=$RC commit=$COMMIT · ssh $HOST · result in ~/nanoclaw/logs/e2e/result.json · delete with: ssh exe.dev rm $NAME"
exit "$RC"
