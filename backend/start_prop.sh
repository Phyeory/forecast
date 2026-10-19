#!/usr/bin/env bash
# start_prop.sh — one command: preflight -> daily session -> report.
#   ./start_prop.sh                      # FTMO 100k production config
#   ./start_prop.sh <path-to-config.json>
set -euo pipefail
cd "$(dirname "$0")"

CONFIG="${1:-market_live/config/prop_ftmo_100k.json}"
PY="./.venv/bin/python"
[ -x "$PY" ] || PY="/Users/jaime/pump-chart/backend/.venv/bin/python"

echo "== preflight =="
"$PY" -m market_live.setup_prop --config "$CONFIG"

echo "== session =="
"$PY" -m market_live.run_live live --config "$CONFIG" --once

echo "== monitor =="
"$PY" -m market_live.forward_monitor --latest || true
