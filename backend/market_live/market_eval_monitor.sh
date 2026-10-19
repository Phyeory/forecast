#!/bin/zsh
# 30-min monitor for the market-eval poller. Appends one status line to
# data/live_market/monitor.log; fires a macOS notification ONLY on anomaly:
#   poller gone / guard halted / equity at or below floors / heartbeat stale.
cd /Users/jaime/pump-chart-market/backend
LOG=data/live_market/monitor.log
PY=/Users/jaime/pump-chart/backend/.venv/bin/python
ts=$(date "+%Y-%m-%d %H:%M")

line=$($PY - <<'PY2'
import glob, json, os, subprocess, time
out = {"ok": True, "issues": []}
out["alive"] = bool(subprocess.run(["pgrep", "-f", "market_live.run_live live"],
                                   capture_output=True, text=True).stdout.strip())
if not out["alive"]:
    out["ok"] = False
    out["issues"].append("POLLER GONE — launchd keepalive should restart it; check poller.err.log")
try:
    env = {}
    for l in open("market_live/config/keys.env"):
        l = l.strip()
        if l.startswith("export "):
            k, _, v = l[7:].partition("=")
            env[k.strip()] = v.strip().strip('"')
    import urllib.request
    H = {"APCA-API-KEY-ID": env["APCA_API_KEY_ID"],
         "APCA-API-SECRET-KEY": env["APCA_API_SECRET_KEY"]}
    def get(path):
        return json.loads(urllib.request.urlopen(urllib.request.Request(
            "https://paper-api.alpaca.markets" + path, headers=H), timeout=15).read())
    acct = get("/v2/account")
    out["equity"] = float(acct["equity"])
    pos = get("/v2/positions")
    out["positions"] = len(pos)
    out["unrealized"] = round(sum(float(p["unrealized_pl"]) for p in pos), 2)
    if out["equity"] <= 90000.0:
        out["ok"] = False
        out["issues"].append(f"STATIC FLOOR BREACH equity={out['equity']}")
    elif out["equity"] <= 95000.0:
        out["issues"].append(f"below daily floor 95000 (equity={out['equity']})")
except Exception as e:
    out["ok"] = False
    out["issues"].append(f"account check failed: {type(e).__name__}:{e}")
hb_age = None
sess = sorted(glob.glob("data/live_market/2*/"), reverse=True)
for d in sess[:3]:
    gs = os.path.join(d, "guard_state.json")
    if os.path.exists(gs):
        g = json.load(open(gs))
        out["phase"] = g.get("phase")
        if g.get("halted"):
            out["ok"] = False
            out["issues"].append("GUARD HALTED — evaluation dead, manual restart required")
    hb = os.path.join(d, "heartbeat.jsonl")
    if os.path.exists(hb):
        try:
            with open(hb) as f:
                last = json.loads(f.readlines()[-1])
            t = last.get("t") or last.get("time") or 0
            hb_age = int(time.time() - t)
        except Exception:
            pass
        break
out["hb"] = hb_age
if hb_age is not None and out["alive"] and hb_age > 3600:
    out["issues"].append(f"heartbeat stale {hb_age}s (poller alive but not journaling?)")
    out["ok"] = False
print(json.dumps(out))
PY2
)

flat=$($PY -c "
import json
d = json.loads('''$line''')
issues = '; '.join(d['issues']) if d['issues'] else 'OK'
print(f\"equity={d.get('equity','?'):>10} pos={d.get('positions','?'):>2} unrl={d.get('unrealized','?'):>8} phase={d.get('phase','?')} hb={d.get('hb','?')}s alive={'yes' if d.get('alive') else 'NO'} :: {issues}\")
" 2>/dev/null || echo "parse-error :: $line")
echo "$ts | $flat" >> "$LOG"

bad=$($PY -c "import json; d=json.loads('''$line'''); print('BAD' if not d.get('ok') else 'OK')" 2>/dev/null)
if [ "$bad" = "BAD" ]; then
  /usr/bin/osascript -e "display notification \"$line\" with title \"Market Eval — PROBLEM\" sound name \"Sosumi\"" >/dev/null 2>&1
fi
