#!/usr/bin/env bash
# exe-run.sh — create (or clone) an exe.dev VM and run a NanoClaw E2E test.
#
# Run from the NanoClaw checkout root, using this installed script's full path.
#
# Usage:
#   bash /path/to/e2e-exe-dev/scripts/exe-run.sh [options]
#
#   --name      VM name (default nanoclaw-e2e-<short sha>-<random suffix>)
#   --ref       local git ref to test (default HEAD); must be fetchable from repo
#   --repo      clone URL (default this checkout's origin)
#   --credential-file  provider credential file (default legacy Anthropic path)
#   --key-file  compatibility alias for --credential-file
#   --base      clone this VM, keeping its data, vault and image cache
#   --snapshot  on success, copy the tested VM to this new name
#   --rm        guarded delete on success; requires validated --result-file evidence
#   --cpu       CPU count (default 4)
#   --memory    memory size (default 8GB)
#   --disk      disk size (default 40GB)
#   --result-file  record this invocation locally, then export installer result
#   --interactive  drive the real public wizard (fresh VM, requires --result-file)
#   --provider  provider value selected from provider-options.py
#   --auth-method  provider-owned auth method value
#   --gateway   onecli (default) or iron-proxy (env NANOCLAW_E2E_GATEWAY); needs a
#               gateway-seam ref; headless runs pass it to the installer, wizard
#               runs to `nanoclaw.sh --gateway-provider`; bound into the result
#   --supervised-human-auth  wizard only: allow Codex device pairing; the
#               pairing link and code land in <result-file>.handoff (0600)
#   --opencode-model  full backend/model ID for OpenCode wizard runs
#   --opencode-base-url  custom HTTP(S) API endpoint
#   --opencode-provider  API scheme for a custom endpoint (default: openai)
#   --payload-ref  fetched payload ref for a branch-owned provider
#   --artifacts-dir  sanitized evidence (default <result-file>.artifacts)
#   --wizard-timeout  total PTY timeout in seconds (default 1200)
#
# The installer comes from this skill, not the NanoClaw checkout being tested.
# Keys and forwarded installer settings travel over SSH stdin.
# See SKILL.md for prerequisites, supported environment variables and results.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REF=HEAD REPO="" KEY_FILE="${NANOCLAW_E2E_KEY_FILE:-$HOME/.nanoclaw-e2e/anthropic_key}"
NAME="" BASE="" SNAPSHOT="" RM=0 CPU=4 MEMORY=8GB DISK=40GB
RESULT_FILE="" INTERACTIVE=0 ARTIFACTS_DIR="" WIZARD_TIMEOUT=1200 SUPERVISED=0
PROVIDER="" AUTH_METHOD="" PAYLOAD_REF="" OPENCODE_MODEL="" OPENCODE_BASE_URL="" OPENCODE_PROVIDER=""
GATEWAY="${NANOCLAW_E2E_GATEWAY:-onecli}" GATEWAY_SELECTED=0
MODEL_ARGS=()
KEY_SELECTED=0
WIZARD_DIR="$HERE/../../e2e-wizard"
RUN_ID=""
AUTH_SOURCE_COMMIT="" PAYLOAD_COMMIT=""
DEV_TOOLS_COMMIT="" HARNESS_SHA256="" REMOVAL_REQUESTED=0

fail() { echo "[exe-run] $1" >&2; exit "${2:-64}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    --name|--ref|--repo|--key-file|--credential-file|--base|--snapshot|--cpu|--memory|--disk|--result-file|--artifacts-dir|--wizard-timeout|--provider|--auth-method|--payload-ref|--opencode-model|--opencode-base-url|--opencode-provider|--gateway)
      [ $# -ge 2 ] && [ -n "$2" ] && [[ "$2" != --* ]] || fail "$1 requires a value" ;;
  esac
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --key-file|--credential-file) KEY_FILE="$2"; KEY_SELECTED=1; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    --snapshot) SNAPSHOT="$2"; shift 2 ;;
    --rm) RM=1; shift ;;
    --cpu) CPU="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    --result-file) RESULT_FILE="$2"; shift 2 ;;
    --interactive) INTERACTIVE=1; shift ;;
    --supervised-human-auth) SUPERVISED=1; shift ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --auth-method) AUTH_METHOD="$2"; shift 2 ;;
    --gateway) GATEWAY="$2"; GATEWAY_SELECTED=1; shift 2 ;;
    --payload-ref) PAYLOAD_REF="$2"; shift 2 ;;
    --opencode-model) OPENCODE_MODEL="$2"; shift 2 ;;
    --opencode-base-url) OPENCODE_BASE_URL="$2"; shift 2 ;;
    --opencode-provider) OPENCODE_PROVIDER="$2"; shift 2 ;;
    --artifacts-dir) ARTIFACTS_DIR="$2"; shift 2 ;;
    --wizard-timeout) WIZARD_TIMEOUT="$2"; shift 2 ;;
    -h|--help) sed -n '2,35p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) fail "unknown flag: $1" ;;
  esac
done

command -v python3 >/dev/null || fail "python3 is required" 69
RUN_ID="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
COMMIT="" PHASE=preflight RESULT_EXPORTED=0
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
write_driver_result() {
  python3 - "$RESULT_FILE" "$1" "$2" "$REF" "$COMMIT" "$PHASE" "$STARTED_AT" "$PROVIDER" "$AUTH_METHOD" "$AUTH_SOURCE_COMMIT" "$RUN_ID" "$GATEWAY" "$INTERACTIVE" <<'PY'
import datetime, json, os, sys, tempfile
(path, status, rc, ref, commit, phase, started, provider, auth_method, auth_source_commit,
 run_id, gateway, interactive) = sys.argv[1:]
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
    "provider": provider or None,
    "auth_method": auth_method or None,
    "auth_source_commit": auth_source_commit or None,
    "gateway": None,
    "requested_gateway": gateway,
    "run_id": run_id,
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
if [ "$RM" -eq 1 ] && [ -z "$RESULT_FILE" ]; then
  fail "--rm requires --result-file so validated evidence exists before teardown"
fi
if [ -n "$ARTIFACTS_DIR" ] && [ -z "$RESULT_FILE" ]; then
  fail "--artifacts-dir requires --result-file"
fi
# A local report must never alias the credential through an absolute/relative
# spelling, symlink, or hard link. Check this before invalidating an old report;
# the normal credential validation remains after that fail-safe write.
if [ -n "$RESULT_FILE" ]; then
  KEY_RESULT_ALIAS=0
  python3 - "$KEY_FILE" "$RESULT_FILE" <<'PY' || KEY_RESULT_ALIAS=$?
import os, sys

key_path, result_path = sys.argv[1:]
try:
    same = os.path.samefile(key_path, result_path)
except OSError:
    same = os.path.abspath(key_path) == os.path.abspath(result_path)
raise SystemExit(2 if same else 0)
PY
  [ "$KEY_RESULT_ALIAS" -eq 0 ] \
    || fail "--result-file cannot overwrite the credential file" 66
fi
if [ -n "$RESULT_FILE" ]; then
  [ -d "$(dirname "$RESULT_FILE")" ] && [ -w "$(dirname "$RESULT_FILE")" ] \
    && [ ! -d "$RESULT_FILE" ] || fail "--result-file needs a writable parent directory and a file path"
  [ -n "$ARTIFACTS_DIR" ] || ARTIFACTS_DIR="$RESULT_FILE.artifacts"
  [ ! -e "$ARTIFACTS_DIR" ] && [ ! -L "$ARTIFACTS_DIR" ] && [ -d "$(dirname "$ARTIFACTS_DIR")" ] \
    && [ -w "$(dirname "$ARTIFACTS_DIR")" ] || fail "artifacts need a new path with a writable parent"
  command -v tar >/dev/null || fail "tar is required for evidence export" 69
  # Invalidate an old local pass before preflight or VM work can fail. Until
  # a matching installer result is exported, failures get a driver report;
  # commit stays null because no tested revision has been confirmed.
  write_driver_result running "" || fail "could not initialize local result: $RESULT_FILE" 74
  trap finish_driver_result EXIT
fi
[ -n "$PROVIDER" ] && [ -n "$AUTH_METHOD" ] \
  || fail "--provider and --auth-method are required"
[[ "$PROVIDER" =~ ^[a-z0-9]+([a-z0-9-]*[a-z0-9])?$ ]] || fail "invalid provider value"
[[ "$AUTH_METHOD" =~ ^[a-z0-9]+([a-z0-9-]*[a-z0-9])?$ ]] || fail "invalid auth method value"
[[ "$GATEWAY" =~ ^(onecli|iron-proxy)$ ]] || fail "invalid gateway: $GATEWAY (onecli or iron-proxy)"
export NANOCLAW_E2E_GATEWAY="$GATEWAY"
if [ "$SUPERVISED" -eq 1 ]; then
  # Only Codex device pairing needs no return channel: the guest prints a
  # link and code, which this driver relays into a private local file.
  [ "$INTERACTIVE" -eq 1 ] || fail "--supervised-human-auth requires --interactive"
  [ "$PROVIDER:$AUTH_METHOD" = codex:device ] \
    || fail "--supervised-human-auth on exe.dev supports codex device pairing only; Claude subscription sign-in needs the Proxmox or direct wizard entry points"
  [ "$KEY_SELECTED" -eq 0 ] || fail "human handoff authentication must not use --credential-file" 66
fi
if [ "$PROVIDER" = opencode ] && [[ "$AUTH_METHOD" =~ ^(custom|local)$ ]]; then
  [ "$KEY_SELECTED" = 1 ] || fail "custom/local OpenCode requires explicit --credential-file" 66
fi
if [ "$INTERACTIVE" -eq 1 ]; then
  [ -n "$RESULT_FILE" ] || fail "--interactive requires --result-file"
  [ -z "$PAYLOAD_REF" ] || [[ "$PAYLOAD_REF" =~ ^[A-Za-z0-9._/-]+$ ]] || fail "invalid payload ref"
  [ -z "$BASE" ] || fail "the fresh wizard scenario does not accept --base"
  [[ "$WIZARD_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || fail "invalid wizard timeout"
  for file in requirements.txt scenarios/fresh-cli.json scripts/provider-options.py scripts/wizard-run.py scripts/wizard-install.sh scripts/collect-wizard.py; do
    [ -r "$WIZARD_DIR/$file" ] || fail "install the companion e2e-wizard skill: missing $file" 66
  done
elif [ "$WIZARD_TIMEOUT" != 1200 ] || [ -n "$PAYLOAD_REF" ]; then
  fail "wizard timeout/payload options require --interactive"
elif [ "$PROVIDER" != claude ] || [[ ! "$AUTH_METHOD" =~ ^(api|oauth|existing)$ ]]; then
  fail "headless mode supports claude with api, oauth, or existing auth; use --interactive for another offered provider"
fi
if [ "$INTERACTIVE" -eq 0 ] && [ -n "$RESULT_FILE" ]; then
  [ -r "$HERE/e2e-evidence.py" ] || fail "installed skill is incomplete: missing e2e-evidence.py" 66
fi
for cmd in git ssh; do command -v "$cmd" >/dev/null || fail "$cmd is required" 69; done
grep -q '"name": *"nanoclaw"' package.json 2>/dev/null \
  || fail "run this from the root of a NanoClaw checkout" 65
COMMIT="$(git rev-parse --verify --end-of-options "$REF^{commit}")" \
  || fail "cannot resolve local ref: $REF" 65
[ -n "$REPO" ] || REPO="$(git remote get-url origin)"
if [ "$INTERACTIVE" -eq 1 ]; then
  python3 - "$WIZARD_DIR/scripts/provider-options.py" "$PROVIDER" "$AUTH_METHOD" "$OPENCODE_MODEL" "$OPENCODE_BASE_URL" "$OPENCODE_PROVIDER" <<'PYMODEL' || fail "invalid OpenCode model selection" 64
import importlib.util, sys
spec = importlib.util.spec_from_file_location('provider_options', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
try:
    module.validate_model(*sys.argv[2:])
except module.DiscoveryError as error:
    sys.exit(str(error))
PYMODEL
  [ -z "$OPENCODE_MODEL" ] || MODEL_ARGS=(--opencode-model "$OPENCODE_MODEL")
  [ -z "$OPENCODE_BASE_URL" ] || MODEL_ARGS+=(--opencode-base-url "$OPENCODE_BASE_URL")
  [ -z "$OPENCODE_PROVIDER" ] || MODEL_ARGS+=(--opencode-provider "$OPENCODE_PROVIDER")
  DISCOVERY=(python3 "$WIZARD_DIR/scripts/provider-options.py" --root "$PWD" --revision "$COMMIT" --provider "$PROVIDER")
  [ -z "$PAYLOAD_REF" ] || DISCOVERY+=(--payload-ref "$PAYLOAD_REF")
  PROVIDER_JSON="$("${DISCOVERY[@]}")" || fail "could not discover provider/auth choices from the exact revision" 65
  AUTH_SOURCE_COMMIT="$(printf '%s' "$PROVIDER_JSON" | python3 -c '
import json, sys
provider, method, supervised = sys.argv[1:]
selected = json.load(sys.stdin)["selected"]
matches = [item for item in selected["auth_methods"] if item["value"] == method]
if len(matches) != 1 or not matches[0]["usable_for_e2e"]:
    sys.exit("selected auth method is not usable for E2E")
automation = matches[0]["automation"]
if automation == "human-handoff" and supervised != "1":
    sys.exit("selected auth method requires a live human handoff; add --supervised-human-auth")
if automation not in ("credential-file", "human-handoff"):
    sys.exit("selected auth method is unsupported by the wizard driver")
# A branch-owned payload (alone or beside a bundled block) is pinned by its own commit.
kind = selected.get("payload_kind")
payload = selected["auth_source_commit"] if kind == "branch" else next(
    (source["commit"] for source in selected.get("payload_sources", []) if source["kind"] == "branch"), "")
print(selected["auth_source_commit"] + " " + payload)
' "$PROVIDER" "$AUTH_METHOD" "$SUPERVISED")" || fail "provider/auth selection is unavailable to the unattended wizard" 64
  PAYLOAD_COMMIT="${AUTH_SOURCE_COMMIT#* }"; AUTH_SOURCE_COMMIT="${AUTH_SOURCE_COMMIT%% *}"
  [ -z "$PAYLOAD_COMMIT" ] || [[ "$PAYLOAD_COMMIT" =~ ^[a-f0-9]{40}$ ]] || fail "invalid payload commit"
else
  [ -z "$OPENCODE_MODEL$OPENCODE_BASE_URL$OPENCODE_PROVIDER" ] || fail "OpenCode options require --interactive"
  AUTH_SOURCE_COMMIT="$COMMIT"
fi
if DEV_TOOLS_ROOT="$(git -C "$HERE/../../.." rev-parse --show-toplevel 2>/dev/null)"; then
  DEV_TOOLS_COMMIT="$(git -C "$DEV_TOOLS_ROOT" rev-parse --verify HEAD 2>/dev/null || true)"
fi
HARNESS_FILES=("$HERE/exe-run.sh")
if [ "$INTERACTIVE" -eq 1 ]; then
  HARNESS_FILES+=(
    "$WIZARD_DIR/scenarios/fresh-cli.json"
    "$WIZARD_DIR/scripts/provider-options.py"
    "$WIZARD_DIR/scripts/wizard-run.py"
    "$WIZARD_DIR/scripts/wizard-install.sh"
    "$WIZARD_DIR/scripts/collect-wizard.py"
  )
else
  HARNESS_FILES+=("$HERE/e2e-install.sh")
  [ -z "$RESULT_FILE" ] || HARNESS_FILES+=("$HERE/e2e-evidence.py")
fi
HARNESS_SHA256="$(python3 - "${HARNESS_FILES[@]}" <<'PY'
import hashlib, pathlib, sys
digest = hashlib.sha256()
for raw in sys.argv[1:]:
    path = pathlib.Path(raw)
    data = path.read_bytes()
    label = "/".join(path.parts[-2:]).encode()
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)
print(digest.hexdigest())
PY
)" || fail "could not identify the installed E2E harness" 66
KEY_CHECK=0
[ "$SUPERVISED" -eq 0 ] || KEY_FILE=""
[ -z "$KEY_FILE" ] || python3 - "$KEY_FILE" <<'PY' || KEY_CHECK=$?
import os, stat, sys

try:
    key = os.lstat(sys.argv[1])
except OSError:
    raise SystemExit(1)
if (not stat.S_ISREG(key.st_mode) or key.st_mode & 0o077
        or key.st_uid != os.getuid() or key.st_size == 0):
    raise SystemExit(1)
PY
[ "$KEY_CHECK" -eq 0 ] \
  || fail "credential must be a nonempty private regular file owned by this user (0600): $KEY_FILE" 66
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

echo "[exe-run] vm=$NAME ref=$REF commit=$COMMIT repo=$REPO gateway=$GATEWAY$([ "$INTERACTIVE" -eq 1 ] && echo " (wizard)")"
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
python3 - "$NAME" "$HOST" "$RUN_ID" "$COMMIT" "$DEV_TOOLS_COMMIT" "$HARNESS_SHA256" <<'PY' | \
  "${VM[@]}" 'set -e; umask 077; mkdir -p ~/.nanoclaw-e2e; cat > ~/.nanoclaw-e2e/run-owner.json; chmod 600 ~/.nanoclaw-e2e/run-owner.json'
import datetime, json, sys
name, host, run_id, commit, dev_tools_commit, harness_sha256 = sys.argv[1:]
json.dump({
    "schema_version": 1,
    "vm_name": name,
    "ssh_dest": host,
    "run_id": run_id,
    "nanoclaw_commit": commit,
    "dev_tools_commit": dev_tools_commit or None,
    "harness_sha256": harness_sha256,
    "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}, sys.stdout)
sys.stdout.write("\n")
PY
if [ -n "$KEY_FILE" ]; then
  "${VM[@]}" 'set -e; umask 077; mkdir -p ~/.nanoclaw-e2e; cat > ~/.nanoclaw-e2e/credential; chmod 600 ~/.nanoclaw-e2e/credential' < "$KEY_FILE"
fi
if [ "$INTERACTIVE" -eq 1 ]; then
  COPYFILE_DISABLE=1 tar -C "$WIZARD_DIR" -cf - requirements.txt scenarios/fresh-cli.json \
    scripts/provider-options.py scripts/wizard-run.py scripts/wizard-install.sh scripts/collect-wizard.py | \
    "${VM[@]}" 'set -e; umask 077; mkdir -p ~/.nanoclaw-e2e/wizard; tar -xf - -C ~/.nanoclaw-e2e/wizard'
else
  "${VM[@]}" 'set -e; cat > ~/e2e-install.sh; chmod +x ~/e2e-install.sh' < "$HERE/e2e-install.sh"
  if [ -n "$RESULT_FILE" ]; then
    "${VM[@]}" 'set -e; umask 077; cat > ~/.nanoclaw-e2e/e2e-evidence.py; chmod 700 ~/.nanoclaw-e2e/e2e-evidence.py' < "$HERE/e2e-evidence.py"
  fi
fi

# Forward only documented settings. A remote gateway token must never appear
# in an SSH command, process argument or driver log.
python3 - "$INTERACTIVE" <<'PY' | "${VM[@]}" 'set -e; umask 077; cat > ~/.nanoclaw-e2e/run-env.sh; chmod 600 ~/.nanoclaw-e2e/run-env.sh'
import os, shlex, sys
if sys.argv[1] == "1":
    sys.exit(0)  # The wizard takes every product choice through its UI.
for name in (
    "NANOCLAW_ONECLI_API_HOST", "NANOCLAW_ONECLI_API_TOKEN",
    "NANOCLAW_DISPLAY_NAME", "NANOCLAW_E2E_TZ",
    "NANOCLAW_E2E_KEEP_AGENT", "NANOCLAW_E2E_FORCE_AUTH",
    "NANOCLAW_E2E_GATEWAY",
):
    if name in os.environ:
        print("export " + name + "=" + shlex.quote(os.environ[name]))
PY

REPO_QUOTED="$(python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$REPO")"
PHASE=checkout
INSTALL_COMMAND="NANOCLAW_E2E_ROOT=\$HOME/nanoclaw NANOCLAW_E2E_KEY_FILE=\$HOME/.nanoclaw-e2e/credential NANOCLAW_E2E_PROVIDER='$PROVIDER' NANOCLAW_E2E_AUTH_METHOD='$AUTH_METHOD' NANOCLAW_E2E_AUTH_SOURCE_COMMIT='$AUTH_SOURCE_COMMIT' bash ~/e2e-install.sh"
if [ "$INTERACTIVE" -eq 1 ]; then
  INSTALL_COMMAND="bash ~/.nanoclaw-e2e/wizard/scripts/wizard-install.sh --run-id '$RUN_ID' --timeout '$WIZARD_TIMEOUT' --provider '$PROVIDER' --auth-method '$AUTH_METHOD' --gateway '$GATEWAY'"
  if [ "$SUPERVISED" -eq 1 ]; then
    INSTALL_COMMAND="$INSTALL_COMMAND --supervised-human-auth"
  else
    INSTALL_COMMAND="$INSTALL_COMMAND --credential-file \$HOME/.nanoclaw-e2e/credential"
  fi
  MODEL_QUOTED="$(python3 -c 'import shlex, sys; print(shlex.join(sys.argv[1:]))' ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"})"
  INSTALL_COMMAND="$INSTALL_COMMAND $MODEL_QUOTED"
  INSTALL_COMMAND="$INSTALL_COMMAND --expected-auth-source-commit '$AUTH_SOURCE_COMMIT'"
  if [ -n "$PAYLOAD_REF" ]; then
    INSTALL_COMMAND="$INSTALL_COMMAND --payload-ref '$PAYLOAD_REF'"
  fi
  if [ -n "$PAYLOAD_COMMIT" ]; then
    INSTALL_COMMAND="$INSTALL_COMMAND --expected-payload-commit '$PAYLOAD_COMMIT'"
  fi
fi
# A supervised wizard run writes its pairing request on the guest
# (~/.nanoclaw-e2e/auth-handoffs/<run_id>/request.json). Relay it once into a
# private local file next to the result; never into this log or the evidence.
HANDOFF_FILE=""
# Prints the guest wizard's exit code once its result is terminal, else nothing.
remote_wizard_exit() {
  local value
  value="$("${VM[@]}" 'python3 - <<"PY" 2>/dev/null
import json, os
try:
    result = json.load(open(os.path.expanduser("~/nanoclaw/logs/e2e/result.json")))
except Exception:
    raise SystemExit(0)
if result.get("status") in ("pass", "failed") and isinstance(result.get("exit_code"), int):
    print(result["exit_code"])
PY' 2>/dev/null)"
  [[ "$value" =~ ^[0-9]+$ ]] && printf '%s' "$value"
  return 0
}
relay_handoff() {
  local remote="\$HOME/.nanoclaw-e2e/auth-handoffs/$RUN_ID/request.json" polls=0
  while kill -0 "$1" 2>/dev/null; do
    sleep 5
    polls=$((polls + 1))
    # A guest that failed before pairing (and whose session lingers) must not
    # keep this relay waiting; the completion watcher takes over.
    if [ $((polls % 3)) -eq 0 ] && [ -n "$(remote_wizard_exit)" ]; then
      return
    fi
    if REQUEST="$("${VM[@]}" "cat $remote 2>/dev/null" 2>/dev/null)" && [ -n "$REQUEST" ]; then
      # Create the private file exclusively (no symlink following, no reuse of
      # a file something else placed at this path) and print only its path.
      if printf '%s\n' "$REQUEST" | python3 -c '
import os, sys
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(sys.argv[1], flags, 0o600)
with os.fdopen(fd, "w") as out:
    out.write(sys.stdin.read())
' "$HANDOFF_FILE"; then
        echo "[exe-run] device pairing requested: open the link and enter the code from $HANDOFF_FILE (private, 0600)" >&2
      else
        echo "[exe-run] could not create the private handoff file $HANDOFF_FILE; read the request on the VM instead" >&2
      fi
      return
    fi
  done
}
# The guest wizard writes its own terminal result (status pass/failed with an
# exit code) before its SSH session ends. Twice on 2026-09-22 the session then
# stayed open although nothing on the VM still held it, so once that result
# exists the driver waits one more minute and then takes the wizard's exit
# code itself instead of hanging on SSH; evidence is exported separately.
wait_for_wizard() {
  local remote_exit="" grace=0
  while kill -0 "$1" 2>/dev/null; do
    sleep 15
    kill -0 "$1" 2>/dev/null || break
    if [ -z "$remote_exit" ]; then
      remote_exit="$(remote_wizard_exit)"
    else
      grace=$((grace + 1))
      if [ "$grace" -ge 4 ]; then
        echo "[exe-run] the wizard finished with exit $remote_exit but its SSH session did not close; continuing without it" >&2
        kill "$1" 2>/dev/null
        wait "$1" 2>/dev/null
        WIZARD_EXIT_OVERRIDE="$remote_exit"
        return
      fi
    fi
  done
}
WIZARD_EXIT_OVERRIDE=""
set +e
"${VM[@]}" "set -e
  printf '%s\n' '$RUN_ID' > ~/.nanoclaw-e2e/controller-active
  trap 'rm -f ~/.nanoclaw-e2e/controller-active' EXIT
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
  cd ~/nanoclaw && $INSTALL_COMMAND" </dev/null &
INSTALL_PID=$!
if [ "$SUPERVISED" -eq 1 ]; then
  HANDOFF_FILE="$RESULT_FILE.handoff"
  rm -f "$HANDOFF_FILE"
  relay_handoff "$INSTALL_PID"
fi
if [ "$INTERACTIVE" -eq 1 ]; then
  wait_for_wizard "$INSTALL_PID"
fi
wait "$INSTALL_PID"
RC=$?
[ -z "$WIZARD_EXIT_OVERRIDE" ] || RC="$WIZARD_EXIT_OVERRIDE"
set -e

if [ "$INTERACTIVE" -eq 1 ]; then
  PHASE=export
  # Only the runner-owned sanitized directory crosses SSH. Validate archive
  # membership, checksums, credentials and this run before publishing a pass.
  if "${VM[@]}" 'COPYFILE_DISABLE=1 tar -C ~/nanoclaw/logs/e2e-wizard -cf - .' | \
    python3 "$WIZARD_DIR/scripts/collect-wizard.py" \
      --artifacts-dir "$ARTIFACTS_DIR" --result-file "$RESULT_FILE" \
      --commit "$COMMIT" --run-id "$RUN_ID" --exit-code "$RC" ${KEY_FILE:+--key-file "$KEY_FILE"} \
      --provider "$PROVIDER" --auth-method "$AUTH_METHOD" --gateway "$GATEWAY" \
      --auth-source-commit "$AUTH_SOURCE_COMMIT" ${PAYLOAD_COMMIT:+--payload-commit "$PAYLOAD_COMMIT"} \
      ${MODEL_ARGS[@]+"${MODEL_ARGS[@]}"}; then
    RESULT_EXPORTED=1
  else
    echo "[exe-run] could not export sanitized wizard evidence; keeping $NAME" >&2
    [ "$RC" -ne 0 ] || RC=74
  fi
elif [ -n "$RESULT_FILE" ]; then
  [ "$RC" -ne 0 ] || PHASE=export
  REMOTE_EVIDENCE="\$HOME/nanoclaw/logs/e2e-headless-$RUN_ID"
  if "${VM[@]}" "python3 \$HOME/.nanoclaw-e2e/e2e-evidence.py export \
      --root \$HOME/nanoclaw --destination $REMOTE_EVIDENCE \
      --credential-file \$HOME/.nanoclaw-e2e/credential \
      --run-id '$RUN_ID' --provider '$PROVIDER' --auth-method '$AUTH_METHOD' \
      --auth-source-commit '$AUTH_SOURCE_COMMIT' --gateway '$GATEWAY' \
      --dev-tools-commit '$DEV_TOOLS_COMMIT' \
      --harness-sha256 '$HARNESS_SHA256'"; then
    if "${VM[@]}" "COPYFILE_DISABLE=1 tar -C $REMOTE_EVIDENCE -cf - ." | \
      python3 "$HERE/e2e-evidence.py" collect \
        --artifacts-dir "$ARTIFACTS_DIR" --result-file "$RESULT_FILE" \
        --credential-file "$KEY_FILE" --commit "$COMMIT" --run-id "$RUN_ID" \
        --exit-code "$RC" --provider "$PROVIDER" --auth-method "$AUTH_METHOD" \
        --auth-source-commit "$AUTH_SOURCE_COMMIT" --gateway "$GATEWAY" \
        --dev-tools-commit "$DEV_TOOLS_COMMIT" \
        --harness-sha256 "$HARNESS_SHA256"; then
      RESULT_EXPORTED=1
      echo "[exe-run] saved sanitized evidence: $ARTIFACTS_DIR"
    else
      echo "[exe-run] local evidence validation failed; keeping $NAME" >&2
      [ "$RC" -ne 0 ] || RC=74
    fi
  else
    echo "[exe-run] remote evidence export failed; keeping $NAME" >&2
    [ "$RC" -ne 0 ] || RC=74
  fi
fi

if ! "${VM[@]}" 'rm -f ~/.nanoclaw-e2e/credential ~/.nanoclaw-e2e/auth-handoffs/*/request.json'; then
  echo "[exe-run] could not remove the staged credential; keeping $NAME" >&2
  [ "$RC" -ne 0 ] || RC=74
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
  PHASE=teardown
  if OWNER_RECORD="$("${VM[@]}" 'cat ~/.nanoclaw-e2e/run-owner.json')" \
    && printf '%s' "$OWNER_RECORD" | python3 -c '
import json, sys
record = json.load(sys.stdin)
name, host, run_id, commit, dev_tools_commit, harness_sha256 = sys.argv[1:]
expected = {
    "schema_version": 1,
    "vm_name": name,
    "ssh_dest": host,
    "run_id": run_id,
    "nanoclaw_commit": commit,
    "dev_tools_commit": dev_tools_commit or None,
    "harness_sha256": harness_sha256,
}
if not isinstance(record, dict) or any(record.get(key) != value for key, value in expected.items()):
    sys.exit("run ownership marker does not match this invocation")
' "$NAME" "$HOST" "$RUN_ID" "$COMMIT" "$DEV_TOOLS_COMMIT" "$HARNESS_SHA256" \
    && "${VM[@]}" "test ! -e ~/.nanoclaw-e2e/controller-active \
      && command -v pgrep >/dev/null \
      && ! pgrep -f '([e]2e-install\\.sh|[w]izard-(run\\.py|install\\.sh))' >/dev/null"; then
    echo "[exe-run] verified run ownership and inactive harness for $NAME"
  else
    echo "[exe-run] teardown precheck failed; keeping $NAME" >&2
    RC=70
  fi
fi
if [ "$RC" -eq 0 ] && [ "$RM" -eq 1 ]; then
  echo "[exe-run] deleting $NAME"
  if REMOVED="$(ssh -o BatchMode=yes exe.dev rm "$NAME" --json)"; then
    REMOVAL_REQUESTED=1
    printf '%s\n' "$REMOVED" >&2
  else
    echo "[exe-run] exe.dev removal command failed; inspect $NAME" >&2
    RC=70
  fi
fi
if [ "$RC" -eq 0 ] && [ "$RM" -eq 1 ]; then
  REMOVAL_VERIFIED=0
  for _ in $(seq 1 10); do
    if INVENTORY="$(ssh -o BatchMode=yes exe.dev ls --json)" \
      && printf '%s' "$INVENTORY" | python3 -c '
import json, sys
inventory = json.load(sys.stdin)
expected = sys.argv[1]
vms = inventory.get("vms") if isinstance(inventory, dict) else None
if not isinstance(vms, list) or any(not isinstance(vm, dict) for vm in vms):
    sys.exit("invalid exe.dev VM inventory")
if any((vm.get("vm_name") or vm.get("name")) == expected for vm in vms):
    sys.exit(1)
' "$NAME"; then
      REMOVAL_VERIFIED=1
      break
    fi
    sleep 2
  done
  if [ "$REMOVAL_VERIFIED" -ne 1 ]; then
    echo "[exe-run] removal was requested but absence was not verified for $NAME" >&2
    RC=70
  fi
fi
if [ "$RC" -eq 0 ] && [ "$RM" -eq 1 ]; then
  if python3 - "$ARTIFACTS_DIR" "$NAME" "$HOST" "$RUN_ID" "$COMMIT" <<'PY'
import datetime, json, os, pathlib, sys
artifacts, name, host, run_id, commit = sys.argv[1:]
directory = pathlib.Path(artifacts)
if directory.is_symlink() or not directory.is_dir():
    raise SystemExit("validated artifacts directory is unavailable")
receipt = directory / "teardown-receipt.json"
temporary = directory / (".teardown-receipt." + os.urandom(6).hex())
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(fd, "w") as output:
        json.dump({
            "schema_version": 1,
            "resource_type": "exe.dev-vm",
            "vm_name": name,
            "ssh_dest": host,
            "run_id": run_id,
            "nanoclaw_commit": commit,
            "removed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "verification": {"command": "ssh exe.dev ls --json", "vm_absent": True},
        }, output, indent=2)
        output.write("\n")
    os.replace(temporary, receipt)
finally:
    temporary.unlink(missing_ok=True)
PY
  then
    echo "[exe-run] done rc=0 commit=$COMMIT (vm removal verified; receipt: $ARTIFACTS_DIR/teardown-receipt.json)"
    exit 0
  fi
  echo "[exe-run] VM absence was verified but the teardown receipt could not be written" >&2
  exit 74
fi
if [ "$REMOVAL_REQUESTED" -eq 1 ]; then
  echo "[exe-run] done rc=$RC commit=$COMMIT · removal requested but absence unverified for $NAME"
  exit "$RC"
fi
echo "[exe-run] done rc=$RC commit=$COMMIT · ssh $HOST · result in ~/nanoclaw/logs/e2e/result.json · delete with: ssh exe.dev rm $NAME"
exit "$RC"
