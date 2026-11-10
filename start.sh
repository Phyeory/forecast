#!/usr/bin/env bash
set -e
# macOS launchd default soft fd limit is 256 — live sessions (WS + RPC subs +
# per-insert SQLite conns) exhaust it under feed bursts, surfacing as
# sqlite3.OperationalError: unable to open database file (2026-11-06 20:35).
ulimit -n 65536
cd "$(dirname "$0")/backend"
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo ""
echo "  pump-chart → http://localhost:8000"
echo ""
python main.py
