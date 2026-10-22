#!/usr/bin/env bash
# Daily prop-sim step: resume the account, decide every bar that closed since
# the last run, book the fills, persist state.  Runs on GitHub Actions (free,
# no card) and is idempotent — a second run with nothing new does nothing.
#
#   repo root/
#     backend/            the market_live runtime
#     deploy/run_daily.sh this script
#     runtime/            state.json, guard_state.json, snap.pkl.gz, journal/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY=python3
$PY -m pip install --quiet --disable-pip-version-check numpy numba scipy || \
  $PY -m pip install --quiet --disable-pip-version-check numpy scipy

echo "[prop-sim] $(date -u +%FT%TZ) — starting daily step (cwd=$ROOT)"
PYTHONPATH="$ROOT/backend" $PY -m market_live.run_live live \
    --config "$ROOT/backend/market_live/config/paper_ftmo_sim_gh.json" \
    --feed alpaca --poll-seconds 60 --once

echo "[prop-sim] state after step:"
$PY - <<'PY2'
import json
d = json.load(open("runtime/state.json"))
b = d["book"]
print(f"  equity={b.get('equity'):,.2f} cash={b.get('cash'):,.2f} "
      f"realized={b.get('realized'):,.2f} fills={b.get('n_fills')}")
print(f"  cursor={sorted(set(d['last_ts'].values()))}")
PY2
