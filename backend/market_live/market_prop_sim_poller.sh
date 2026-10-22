#!/bin/zsh
# Prop-firm forward sim poller (FTMO 2-step simulated on a paper broker).
# Warm once, then process every newly closed US daily bar minutes after its
# close; orders fill at the next open (backtest-identical).  since_ts
# catch-up replays missed bars after sleep/reboot (state + engine snapshot
# make the restart exact).  Managed by launchd (KeepAlive).
cd /Users/jaime/pump-chart-market/backend
source market_live/config/keys.env
# caffeinate -i: keep the machine from idling to sleep so a US close is
# never missed (the process is the lifetime anchor).
exec /usr/bin/caffeinate -i /Users/jaime/pump-chart/backend/.venv/bin/python -m market_live.run_live live \
  --config market_live/config/paper_ftmo_sim.json --feed alpaca --poll-seconds 60
