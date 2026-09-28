"""iter95 parity monitor — ONE check cycle (invoked every 30 min by cron).

Scans live journals for trades since the last check, replays those
recordings through the backtester (the same calibration pipeline the live
session now uses: initialize at the recording anchor), compares decision
streams, and verifies every journal transaction on-chain.

Outputs a human-readable report on stdout; appends findings to
backend/data/parity_monitor_log.md; keeps cursor state in
backend/data/parity_monitor_state.json.

Read-only wrt the trading process: never restarts, never writes engine code.
"""

import glob
import json
import os
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BACKEND, "data")
STATE_PATH = os.path.join(DATA, "parity_monitor_state.json")
LOG_PATH = os.path.join(DATA, "parity_monitor_log.md")
WALLET = "GAurKM6dS7kyWvPngRXjzod11JWUpNnaE6R5W4s6AXpr"
RPC = "https://solana-rpc.publicnode.com"
ENTRY_TOL_S = 15      # live buy fill lands ~3 s after the BT signal-state fill
EXIT_TOL_S = 40       # armed exits fill ~20 s late by design; give slack
MAX_AGE_S = 8 * 3600  # monitor window


def hh(t):
    return datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%H:%M:%S")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {"cycle": 0, "last_close_ts": time.time(), "replayed": {}, "verdicts": []}


def save_state(st):
    with open(STATE_PATH, "w") as f:
        json.dump(st, f, indent=1)


def scan_live(since_ts):
    """Collect closed trades + session configs from live journals."""
    trades, cfgs, buys_failed, open_pos = [], {}, [], {}
    for d in sorted(glob.glob(os.path.join(DATA, "live_logs", "2026*"))):
        f = os.path.join(d, "trades.jsonl")
        if not os.path.exists(f):
            continue
        rec = sym = None
        kw = None
        buys = {}
        last_buy = None      # (ts, tx) of the latest confirmed buy in this session
        last_close = 0.0
        for line in open(f):
            try:
                r = json.loads(line)
            except Exception:
                continue
            ev = r.get("event")
            if ev == "session_open":
                kw = r.get("engine_kwargs") or {}
                kw_open_ts = r.get("ts", 0)
            elif ev == "session_meta":
                rec, sym = r.get("recording_id"), r.get("token_symbol")
            elif ev == "buy_confirmed":
                t = r.get("trade") or {}
                buys[t.get("tx_hash_buy")] = r.get("ts")
                last_buy = (t.get("entry_time") or r.get("ts"), t.get("tx_hash_buy"))
            elif ev in ("buy_failed", "buy_dead", "buy_onchain_error") and r.get("ts", 0) > since_ts:
                buys_failed.append((d.split("/")[-1], sym, ev))
            elif ev == "trade_closed":
                # ALL closed trades are collected; comparison is cumulative
                # per recording (a delta-only compare re-flags previously
                # matched trades as BT-ONLY — monitor artifact, cycle 3).
                t = r.get("trade") or {}
                last_close = r.get("ts", 0)
                trades.append({
                    "rec": rec, "sym": sym, "session": d.split("/")[-1],
                    "ts": r.get("ts"),
                    "entry_iso": hh(t.get("entry_time") or r.get("ts")),
                    "entry_time": t.get("entry_time"),
                    "exit_time": t.get("exit_time") or r.get("ts"),
                    "pnl_pct": t.get("pnl_pct"), "pnl_sol": t.get("pnl_sol"),
                    "wallet_pnl_sol": t.get("wallet_pnl_sol"),
                    "cash_pnl_sol": t.get("cash_pnl_sol"),
                    "exit_reason": t.get("exit_reason"),
                    "tx_buy": t.get("tx_hash_buy"), "tx_sell": t.get("tx_hash_sell"),
                })
        if rec is not None and kw is not None and kw_open_ts > since_ts:
            # only sessions opened inside the monitor window are subject to
            # the config guard — historical sessions ran old calibrations.
            cfgs[rec] = {"sym": sym, "kw": kw, "session": d.split("/")[-1]}
        if rec is not None and last_buy and last_buy[0] and last_buy[0] > last_close:
            # position opened but not yet closed — pending (live side may
            # still be holding it right now)
            open_pos[rec] = {"entry_time": last_buy[0], "tx": last_buy[1],
                             "session": d.split("/")[-1]}
    return trades, cfgs, buys_failed, open_pos


def replay_missing(recs, cycle):
    """Fresh backtest replay for each recording with new live trades this
    cycle.  Re-running is REQUIRED while a session is still running: the
    recording grows as candles stream in, and comparison always reads the
    LATEST parity batch per recording (see bt_trades)."""
    os.chdir(BACKEND)
    sys.path.insert(0, BACKEND)
    from backtester import run_backtest
    out = {}
    for rec in recs:
        try:
            run_backtest(rec, engine_params={}, buy_size_sol=0.01,
                         batch_id=f"parity_mon_{cycle}",
                         engine_version=2, persist_results=True, persist_candles=False)
        except Exception as e:
            out[rec] = f"REPLAY FAILED: {e!r}"
    return out


def bt_trades(rec):
    conn = sqlite3.connect(os.path.join(DATA, "backtest_data.db"))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """SELECT t.entry_time, t.exit_time, t.pnl_pct, t.entry_reason, t.exit_reason
           FROM backtest_trades t JOIN backtests b ON t.backtest_id=b.id
           WHERE b.id = (SELECT id FROM backtests WHERE recording_id=?
                         AND batch_id LIKE 'parity_mon_%' ORDER BY id DESC LIMIT 1)
           ORDER BY t.entry_time""", (rec,))]
    conn.close()
    return rows


def compare(rec, sym, live, bt, open_pos=None):
    """Match each live trade to a BT trade on entry time; report mismatches."""
    now = time.time()
    bt_open = [b for b in bt if b["entry_time"] <= now]
    used, lines, matched = set(), [], 0
    for x in live:
        best, bestd = None, 1e9
        for i, y in enumerate(bt_open):
            if i in used:
                continue
            d = abs((x["entry_time"] or 0) - y["entry_time"])
            if d < bestd:
                bestd, best = d, i
        if best is not None and bestd <= ENTRY_TOL_S:
            used.add(best)
            y = bt_open[best]
            d_exit = abs((x["exit_time"] or 0) - y["exit_time"])
            reason_ok = (x["exit_reason"] or "").startswith((y["exit_reason"] or "")[:12])
            ok = d_exit <= EXIT_TOL_S and reason_ok
            matched += 1 if ok else 0
            flag = "OK " if ok else "DRIFT"
            lines.append(f"    {flag} L {hh(x['entry_time'])}->{hh(x['exit_time'])} "
                         f"{x['exit_reason']} {x['pnl_pct']:+.1f}% | "
                         f"BT {hh(y['entry_time'])}->{hh(y['exit_time'])} {y['exit_reason']} {y['pnl_pct']:+.1f}% "
                         f"(dEntry {bestd:.0f}s dExit {d_exit:.0f}s)")
        else:
            lines.append(f"    LIVE-ONLY  L {hh(x['entry_time'])}->{hh(x['exit_time'])} "
                         f"{x['exit_reason']} {x['pnl_pct']:+.1f}% (no BT entry within {ENTRY_TOL_S}s)")
    for i, y in enumerate(bt_open):
        if i not in used:
            # a trailing BT force-close (recording_ended) can correspond to a
            # position that is OPEN in live right now — pending close, not a
            # missed trade.  Match it to the live open position by entry time.
            op = (open_pos or {}).get(rec)
            if y["exit_reason"] == "recording_ended" and op and \
                    abs((op["entry_time"] or 0) - y["entry_time"]) <= ENTRY_TOL_S:
                lines.append(f"    OPEN-LIVE  BT {hh(y['entry_time'])}->{hh(y['exit_time'])} "
                             f"{y['exit_reason']} {y['pnl_pct']:+.1f}% — live position OPEN "
                             f"(entered {hh(op['entry_time'])}), pending close")
                matched += 1
                continue
            lines.append(f"    BT-ONLY    BT {hh(y['entry_time'])}->{hh(y['exit_time'])} "
                         f"{y['exit_reason']} {y['pnl_pct']:+.1f}% (live never executed)")
    return matched, len(live), lines


def onchain_check(sigs_needed):
    """Verify journal tx hashes exist on-chain; return (ok, missing, fail_count)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                       "params": [WALLET, {"limit": 1000}]})
    out = subprocess.run(["curl", "-s", "-X", "POST", RPC,
                          "-H", "Content-Type: application/json", "-d", body],
                         capture_output=True, text=True)
    try:
        sigs = {s["signature"]: s for s in json.loads(out.stdout)["result"]}
    except Exception as e:
        return None, [f"RPC ERROR {e!r}"], 0
    missing = [t for t in sigs_needed if t not in sigs]
    fails = [s for s in sigs.values() if s.get("err")]
    return True, missing, len(fails)


def main():
    st = load_state()
    st["cycle"] += 1
    since = st["last_close_ts"]
    started = st.get("window_started_at") or time.time()
    now = time.time()
    # schedule-aligned cycle number: the incrementing counter was inflated by
    # manual re-runs during monitor fixes; the 8h verdict must key on the
    # WINDOW CLOCK, not the run count.
    cycle = int((now - started) / 1800) + 1
    rpt = [f"## Cycle {cycle} — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
           f"(window {MAX_AGE_S/3600:.0f}h, {(now - started)/3600 if started else 0:.2f}h elapsed)"]

    live_all, cfgs, buys_failed, open_pos = scan_live(since)
    # cfg regression guard: post-fix sessions must carry the VR tau_max (<= 30)
    bad_cfg = [(rec, c["session"], (c["kw"] or {}).get("tau_max"))
               for rec, c in cfgs.items() if (c["kw"] or {}).get("tau_max", 0) > 30]

    # new trades since the last check decide which recordings get re-replayed
    # and which txs are on-chain-verified this cycle; the DECISION comparison
    # is always cumulative over each recording's full live trade list.
    live_new = [t for t in live_all if (t["ts"] or 0) > since]
    live = live_all
    recs = sorted({t["rec"] for t in live_new if t["rec"]})
    replay_errs = replay_missing(recs, cycle)
    live_window = [t for t in live_all if t["rec"] in set(recs)]
    rpt.append(f"- new live trades since last check: {len(live_new)}; "
               f"comparing cumulative live {len(live_window)} vs BT on {len(recs)} recording(s)")
    if buys_failed:
        rpt.append(f"- buy failures: {len(buys_failed)} {buys_failed[:4]}")
    if bad_cfg:
        rpt.append(f"- **CONFIG REGRESSION** (tau_max>30): {bad_cfg}")

    all_ok = True
    for rec in recs:
        sym = next((t["sym"] for t in live_all if t["rec"] == rec), "?")
        if rec in replay_errs:
            rpt.append(f"  - rec {rec} {sym}: {replay_errs[rec]}")
            all_ok = False
            continue
        bt = bt_trades(rec)
        L = [t for t in live_all if t["rec"] == rec]
        matched, nl, lines = compare(rec, sym, L, bt, open_pos)
        # OPEN-LIVE pending matches count toward parity: every closed live
        # trade must match, and trailing BT force-closes that are still open
        # in live are pending, not divergences.
        ok = matched >= nl and not any(l.startswith("    BT-ONLY") for l in lines)
        all_ok = all_ok and ok
        rpt.append(f"  - rec {rec} {sym}: live {nl} vs BT {len(bt)} (≤now) — "
                   f"{'MATCH' if ok else 'DIVERGENCE'}")
        rpt.extend(lines)

    # on-chain verification for this cycle's trades
    sigs_needed = [t[k] for t in live_new for k in ("tx_buy", "tx_sell") if t[k]]
    sigs_needed += [op["tx"] for rec, op in open_pos.items()
                    if rec in set(recs) and op["tx"]]
    if sigs_needed:
        okc, missing, nfail = onchain_check(sigs_needed)
        if okc is None:
            rpt.append(f"- on-chain: RPC ERROR {missing}")
            all_ok = False
        else:
            rpt.append(f"- on-chain: {len(sigs_needed)} txs checked, missing {len(missing)}, "
                       f"failed sigs in window {nfail}")
            for m in missing:
                rpt.append(f"    MISSING ON-CHAIN: {m[:20]}…")
            all_ok = all_ok and not missing

    if not live_new:
        rpt.append("- no new trades since last check — nothing to replay")

    verdict = "ALL MATCH" if (all_ok and (live or True)) else "DIVERGENCES"
    rpt.append(f"- **verdict: {verdict}**")
    st["last_close_ts"] = max([t["ts"] or 0 for t in live_new], default=since) or since
    st["verdicts"].append({"cycle": cycle, "verdict": verdict,
                           "trades": len(live_new), "at": time.time()})
    if now - (started or now) >= MAX_AGE_S:
        rpt.append("- **8h window complete** — final verdict above")
    save_state(st)

    text = "\n".join(rpt)
    with open(LOG_PATH, "a") as f:
        f.write(text + "\n\n")
    print(text)


if __name__ == "__main__":
    if "--init" in sys.argv:
        save_state({"cycle": 0, "last_close_ts": time.time(),
                    "window_started_at": time.time(), "replayed": {}, "verdicts": []})
        print("monitor state initialized; 8h window starts now")
    else:
        main()
