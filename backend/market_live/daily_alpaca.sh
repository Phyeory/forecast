#!/bin/zsh
# Daily forward-test loop: process the closed US bar, submit next-open orders,
# print the prop-eval status.  22:40 London = 17:40 ET (>=1h after close).
cd /Users/jaime/pump-chart-market/backend
source market_live/config/keys.env
PY=/Users/jaime/pump-chart/backend/.venv/bin/python
echo "=== $(date) ==="
$PY -m market_live.run_live live --config market_live/config/alpaca_paper.json --feed alpaca --once 2>&1 | tail -25
$PY -m market_live.challenge_status --latest 2>&1 | tail -12

# ── weekly recalibration candidate (Sunday) ──────────────────────────────
# Guard: only after >= 4 weeks (28 distinct UTC days) of recorded live tape.
# This computes a CANDIDATE snapshot + gate report.  It never adopts: live
# adoption is a manual `recalibrate_market adopt` + next session_open.
if [ "$(date +%u)" = "7" ]; then
  DAYS=$($PY -c '
import json,glob,os
d=sorted(glob.glob("data/live_market/*/equity.jsonl"))
if not d: print(0); raise SystemExit
days=set()
for line in open(d[-1]):
    try: days.add(json.loads(line).get("ts","")[:10])
    except Exception: pass
print(len([x for x in days if x]))
' 2>/dev/null)
  if [ "${DAYS:-0}" -lt 28 ]; then
    echo "[recal] weekly skip — ${DAYS:-0}/28 live days recorded"
  else
    $PY -m market_backtest.recalibrate_market snapshot \
      --asof "$(date +%Y-%m-%d)" --selection stats_only \
      --tag "weekly_$(date +%Y%m%d)" --workers 4 2>&1 | tail -6
    echo "[recal] candidate written — adoption is manual (see GO_LIVE.md)"
  fi
fi
