#!/usr/bin/env bash
# Install only the test harness dependency, then drive bash nanoclaw.sh in a PTY.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! python3 -m venv "$HOME/.nanoclaw-e2e/wizard-venv"; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3-venv
  python3 -m venv "$HOME/.nanoclaw-e2e/wizard-venv"
fi
"$HOME/.nanoclaw-e2e/wizard-venv/bin/python" -m pip install --disable-pip-version-check --quiet \
  --require-hashes --only-binary=:all: -r "$HERE/../requirements.txt"
exec "$HOME/.nanoclaw-e2e/wizard-venv/bin/python" "$HERE/wizard-run.py" "$@"
