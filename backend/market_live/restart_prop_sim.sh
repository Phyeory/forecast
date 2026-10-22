#!/bin/zsh
# One command: (re)install + load the FTMO prop-firm forward sim
# (poller + 30-min anomaly monitor) and go.  Log: data/live_market/
# prop_sim_monitor.log; session journals: data/live_market/<ts>/
PL=/Users/jaime/pump-chart-market/backend/market_live
cat > ~/Library/LaunchAgents/com.jaime.prop-sim.plist <<'P1'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.jaime.prop-sim</string>
    <key>ProgramArguments</key><array>
        <string>/bin/zsh</string><string>/Users/jaime/pump-chart-market/backend/market_live/market_prop_sim_poller.sh</string>
    </array>
    <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>120</integer>
    <key>StandardOutPath</key><string>/Users/jaime/pump-chart-market/backend/data/live_market/prop_sim_poller.out.log</string>
    <key>StandardErrorPath</key><string>/Users/jaime/pump-chart-market/backend/data/live_market/prop_sim_poller.err.log</string>
    <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
</dict></plist>
P1
cat > ~/Library/LaunchAgents/com.jaime.prop-sim-monitor.plist <<'P2'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.jaime.prop-sim-monitor</string>
    <key>ProgramArguments</key><array>
        <string>/bin/zsh</string><string>/Users/jaime/pump-chart-market/backend/market_live/market_prop_sim_monitor.sh</string>
    </array>
    <key>StartInterval</key><integer>1800</integer><key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/Users/jaime/pump-chart-market/backend/data/live_market/monitor.launchd.log</string>
    <key>StandardErrorPath</key><string>/Users/jaime/pump-chart-market/backend/data/live_market/monitor.launchd.err</string>
</dict></plist>
P2
launchctl unload ~/Library/LaunchAgents/com.jaime.prop-sim.plist 2>/dev/null
launchctl unload ~/Library/LaunchAgents/com.jaime.prop-sim-monitor.plist 2>/dev/null
launchctl load ~/Library/LaunchAgents/com.jaime.prop-sim.plist
launchctl load ~/Library/LaunchAgents/com.jaime.prop-sim-monitor.plist
sleep 3
launchctl list | grep prop-sim
echo "— prop sim poller + 30-min monitor running. Warm-up ≈ 6–8 min; watch data/live_market/prop_sim_poller.out.log"
