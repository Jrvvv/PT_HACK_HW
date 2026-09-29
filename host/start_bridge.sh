#!/usr/bin/env bash
# Start USB bridge + web UI.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -U pip
  .venv/bin/pip install -r requirements.txt
fi
# free port / serial if a previous bridge is stuck
fuser -k 8080/tcp 2>/dev/null || true
fuser -k /dev/ttyUSB0 2>/dev/null || true
sleep 0.4
exec .venv/bin/python -u host/server.py
