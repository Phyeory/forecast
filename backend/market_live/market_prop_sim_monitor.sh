#!/bin/zsh
# 30-min monitor for the FTMO prop-firm forward sim (paper-broker book).
# Appends one status line to data/live_market/prop_sim_monitor.log; fires a
# macOS notification ONLY on anomaly: poller gone / guard halted / equity at
# or below the firm floors / heartbeat stale.
cd /Users/jaime/pump-chart-market/backend
LOG=data/live_market/prop_sim_monitor.log
PY=/Users/jaime/pump-chart/backend/.venv/bin/python
ts=$(date "+%Y-%m-%d %H:%M")

line=$($PY - <<'PY2'
import glob, json, os, subprocess, time
out = {"ok": True, "issues": []}
out["alive"] = bool(subprocess.run(["pgrep", "-f", "paper_ftmo_sim"],
                                   capture_output=True, text=True).stdout.strip())
if not out["alive"]:
    out["ok"] = False
    out["issues"].append("POLLER GONE — launchd keepalive should restart; check poller.err.log")
# latest session of THIS sim (broker=paper + firm=ftmo_2step + warm_sync=False)
sess = None
for d in sorted(glob.glob("data/live_market/2*/"), reverse=True):
    so = os.path.join(d, "session_open.json")
    if os.path.exists(so):
        try:
            c = json.load(open(so)).get("config", {})
            if c.get("broker") == "paper" and c.get("firm") == "ftmo_2step" \
               and c.get("warm_sync") is False:
                sess = d
                break
        except Exception:
            pass
if sess is None:
    out["ok"] = False
    out["issues"].append("no prop-sim session dir found yet")
else:
    out["sess"] = os.path.basename(sess.rstrip("/"))
    hp = os.path.join(sess, "heartbeat.jsonl")
    if os.path.exists(hp):
        try:
            with open(hp) as f:
                last = json.loads(f.readlines()[-1])
            g = last.get("guard") or {}
            out["equity"] = last.get("equity")
            out["phase"] = g.get("phase")
            out["trading_days"] = g.get("trading_days")
            out["consistency"] = g.get("consistency")
            out["npos"] = len((last.get("broker") or {}).get("positions") or [])
            t = last.get("t") or 0
            out["hb"] = int(time.time() - t) if t else None
            if g.get("halted"):
                out["ok"] = False
                out["issues"].append(f"GUARD HALTED ({g.get('halt_reason')})")
            elif g.get("passed"):
                out["issues"].append(f"PHASE {g.get('phase')} PASSED — next: funded/wire the real account")
            eq = float(out.get("equity") or 1e18)
            floor = float(g.get("floor") or 0.0)
            dfloor = float(g.get("daily_floor") or 0.0)
            if floor and eq <= floor:
                out["ok"] = False
                out["issues"].append(f"MAX-LOSS BREACH eq={eq:,.0f} <= floor={floor:,.0f}")
            elif dfloor and eq <= dfloor:
                out["issues"].append(f"below daily 5% floor (eq={eq:,.0f})")
        except Exception as e:
            out["issues"].append(f"heartbeat parse failed: {type(e).__name__}")
    else:
        out["issues"].append("no heartbeat yet (warm-up?)")
hb_age = out.get("hb")
if hb_age is not None and out["alive"] and hb_age > 7200:
    out["issues"].append(f"heartbeat stale {hb_age}s")
    out["ok"] = False
print(json.dumps(out))
PY2
)

flat=$($PY - <<PY3
import json
d = json.loads('''$line''')
issues = '; '.join(d['issues']) if d['issues'] else 'OK'
eq = d.get('equity')
eqs = f"{eq:>10,.0f}" if isinstance(eq, (int, float)) else '         ?'
print(f"eq={eqs} pos={d.get('npos','?'):>2} phase={d.get('phase','?')} "
      f"days={d.get('trading_days','?')} cons={d.get('consistency')} hb={d.get('hb','?')}s "
      f"alive={'yes' if d.get('alive') else 'NO'} sess={d.get('sess','-')} :: {issues}")
PY3
)
echo "$ts | $flat" >> "$LOG"

bad=$($PY -c "import json; d=json.loads('''$line'''); print('BAD' if not d.get('ok') else 'OK')" 2>/dev/null)
if [ "$bad" = "BAD" ]; then
  /usr/bin/osascript -e "display notification \"$flat\" with title \"Prop Sim — PROBLEM\" sound name \"Sosumi\"" >/dev/null 2>&1
fi
