#!/bin/zsh
# Persistent market-eval poller: warms once at start, then processes every
# newly closed US daily bar minutes after its close; opg orders fill at the
# next open (backtest-identical).  Missed bars (sleep/reboot) are replayed on
# wake via since_ts catch-up.  Managed by launchd (KeepAlive).
cd /Users/jaime/pump-chart-market/backend
source market_live/config/keys.env
exec /Users/jaime/pump-chart/backend/.venv/bin/python -m market_live.run_live live \
  --config market_live/config/alpaca_paper.json --feed alpaca --poll-seconds 60
