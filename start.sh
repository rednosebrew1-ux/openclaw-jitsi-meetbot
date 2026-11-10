#!/usr/bin/env bash
# Run the voice server and the Jitsi bridge natively (macOS or Linux).
# First run sets up a Python environment and downloads Chromium; later runs start right away.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "No .env yet: copy .env.example to .env and fill it in first." >&2
  exit 1
fi

PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "Setting up .venv (first run)..."
  "$PY" -m venv .venv
  .venv/bin/pip install --upgrade pip >/dev/null
fi
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  .venv/bin/pip install -r requirements.txt
  .venv/bin/python -m playwright install chromium
  touch .venv/.installed
fi

.venv/bin/python -m voice &
VOICE=$!
trap 'kill "$VOICE" 2>/dev/null || true' EXIT INT TERM
# Give the voice server a moment to start listening before the bridge connects.
sleep 2
.venv/bin/python -m jitsi_bridge
