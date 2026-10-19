#!/bin/zsh
# Daily forward-test loop: process the closed US bar, submit next-open orders,
# print the prop-eval status.  22:40 London = 17:40 ET (>=1h after close).
cd /Users/jaime/pump-chart-market/backend
source market_live/config/keys.env
PY=/Users/jaime/pump-chart/backend/.venv/bin/python
echo "=== $(date) ==="
$PY -m market_live.run_live live --config market_live/config/alpaca_paper.json --feed alpaca --once 2>&1 | tail -25
$PY -m market_live.challenge_status --latest 2>&1 | tail -12
