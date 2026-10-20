#!/bin/zsh
# Reinstall + load both launchd services (poller + 30-min monitor) and go.
PL=/Users/jaime/pump-chart-market/backend/market_live
cat > ~/Library/LaunchAgents/com.jaime.market-eval.plist <<'P1'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.jaime.market-eval</string>
    <key>ProgramArguments</key><array>
        <string>/bin/zsh</string><string>/Users/jaime/pump-chart-market/backend/market_live/market_eval_poller.sh</string>
    </array>
    <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>120</integer>
    <key>StandardOutPath</key><string>/Users/jaime/pump-chart-market/backend/data/live_market/poller.out.log</string>
    <key>StandardErrorPath</key><string>/Users/jaime/pump-chart-market/backend/data/live_market/poller.err.log</string>
    <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
</dict></plist>
P1
cat > ~/Library/LaunchAgents/com.jaime.market-eval-monitor.plist <<'P2'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.jaime.market-eval-monitor</string>
    <key>ProgramArguments</key><array>
        <string>/bin/zsh</string><string>/Users/jaime/pump-chart-market/backend/market_live/market_eval_monitor.sh</string>
    </array>
    <key>StartInterval</key><integer>1800</integer><key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>/Users/jaime/pump-chart-market/backend/data/live_market/monitor.launchd.log</string>
    <key>StandardErrorPath</key><string>/Users/jaime/pump-chart-market/backend/data/live_market/monitor.launchd.err</string>
</dict></plist>
P2
launchctl load ~/Library/LaunchAgents/com.jaime.market-eval.plist
launchctl load ~/Library/LaunchAgents/com.jaime.market-eval-monitor.plist
sleep 3
launchctl list | grep market-eval
echo "— poller + 30-min monitor running again. Warm-up ≈ 6–8 min; watch data/live_market/poller.out.log"
