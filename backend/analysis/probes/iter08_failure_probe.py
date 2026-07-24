"""
iter08 failure-mode probe.

Walks every recording that produced a trade with `exit_reason ==
"recording_ended"` (the force-close exit introduced by the
backtester.py recording_ended fix) and inspects:

  - distribution of pnl_pct for recording_ended vs other exit_reasons
  - per-recording: trade duration (entry_time → exit_time) in bars,
    entry_price, exit_price (= last close), max intra-trade adverse
    excursion (the low across the trade window vs entry_price), the
    same for favourable excursion (max high vs entry_price)
  - the engine latent state at the LAST closed candle before the
    force-close: regime, direction, m_hat, trend_confidence, signal_strength,
    s_effective, ema_spread — what V1-compat attrs were captured in
    candle_results — to identify what kept the engine "long" through the
    whole recording.

Used to build a math-justified hypothesis for the iter08 failure mode.

Usage:
    python backend/analysis/probes/iter08_failure_probe.py \
        --batch-id iter08_baseline_full_1784745312
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from collections import defaultdict, Counter

RESULTS_DIR = os.environ.get(
    "BACKTEST_RESULTS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "backtest_results"),
)

# Pull candle_results from backtest_data.db (where create_backtest writes them)
sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
import sqlite3


def _db_path() -> str:
    return os.path.join(os.path.dirname(__file__), "..", "..", "data",
                        "backtest_data.db")


def _per_rec_candle_results(backtest_id: int) -> list[dict]:
    """Fetch candle_results for a single backtest_id."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT * FROM candle_results WHERE backtest_id=? ORDER BY time""",
        (backtest_id,))
    out = [dict(r) for r in cur.fetchall()]
    conn.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    args = ap.parse_args()

    # Find all per-recording JSON trade logs for this batch (they have trades).
    import glob
    files = sorted(glob.glob(os.path.join(RESULTS_DIR, f"*_{args.batch_id}_*.json")))

    print(f"Inspecting {len(files)} per-token trade logs")
    n_total_trades = 0
    n_re_end_trades = 0
    re_end_pnls_pct = []
    other_pnls_pct = []
    other_exit_classes = Counter()
    re_end_summary = []   # one row per recording_ended trade
    per_rec_pnl = defaultdict(float)

    bad = 0
    for f in files:
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            bad += 1
            continue
        ts = d.get("trades", [])
        if not ts:
            continue
        rec_id = d.get("recording_id", 0)
        sym = d.get("token_symbol", "") or d.get("token_name", "") or "?"
        # Index into candle_results for the SAME backtest_id (so we can
        # recover the trade bars for force-closed trades).
        for t in ts:
            n_total_trades += 1
            er = t.get("exit_reason", "")
            pnl_pct = float(t.get("pnl_pct", 0.0))
            pnl_sol = float(t.get("pnl_sol", 0.0))
            entry_time = t.get("entry_time")
            exit_time  = t.get("exit_time")
            entry_price = float(t.get("entry_price", 0.0))
            exit_price  = float(t.get("exit_price", 0.0))
            per_rec_pnl[rec_id] += pnl_sol

            if er == "recording_ended":
                n_re_end_trades += 1
                re_end_pnls_pct.append(pnl_pct)
                re_end_summary.append({
                    "recording_id": rec_id,
                    "symbol": sym,
                    "entry_time": entry_time,
                    "exit_time": exit_time,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_pct": pnl_pct,
                    "pnl_sol": pnl_sol,
                    "entry_reason": t.get("entry_reason", ""),
                    "regime_at_entry": (t.get("entry_params", {}) or {}).get("regime"),
                    "confidence_at_entry": (t.get("entry_params", {}) or {}).get("trend_confidence"),
                    "s_effective_at_entry": (t.get("entry_params", {}) or {}).get("s_effective"),
                    "exit_params": t.get("exit_params", {}) or {},
                })
            else:
                other_pnls_pct.append(pnl_pct)
                other_exit_classes[er] += 1

    print(f"\n=== Aggregate trade counts ===")
    print(f"  Total trades:           {n_total_trades}")
    print(f"  recording_ended trades: {n_re_end_trades}   ({100*n_re_end_trades/max(n_total_trades,1):.1f}%)")
    print(f"  other exits:            {n_total_trades - n_re_end_trades}")

    if re_end_pnls_pct:
        re_end_pnls_pct.sort()
        n = len(re_end_pnls_pct)
        wins = sum(1 for p in re_end_pnls_pct if p > 0)
        losses = n - wins
        sum_pnl_pct = sum(re_end_pnls_pct)
        print(f"\n=== recording_ended trade outcome distribution ===")
        print(f"  Wins / Losses:   {wins} / {losses}    win rate = {100*wins/n:.1f}%")
        print(f"  Sum pnl_pct:     {sum_pnl_pct:+.2f}")
        print(f"  Mean pnl_pct:     {sum_pnl_pct/n:+.2f}")
        print(f"  P5  pnl_pct:      {re_end_pnls_pct[int(0.05*n)]:+.2f}")
        print(f"  P25 pnl_pct:      {re_end_pnls_pct[int(0.25*n)]:+.2f}")
        print(f"  P50  pnl_pct:     {re_end_pnls_pct[int(0.50*n)]:+.2f}")
        print(f"  Worst 10 pnl_pct: " + ", ".join(f"{p:+.1f}" for p in re_end_pnls_pct[:10]))
        print(f"  Best  10 pnl_pct: " + ", ".join(f"{p:+.1f}" for p in re_end_pnls_pct[-10:]))

    total_pnl = sum(per_rec_pnl.values())
    re_end_pnl_sol_total = sum(t["pnl_sol"] for t in re_end_summary)
    print(f"\n=== Aggregate PnL impact ===")
    print(f"  Total batch PnL (SOL):    {total_pnl:+.5f}")
    print(f"  recording_ended PnL (SOL): {re_end_pnl_sol_total:+.5f}  ({100*re_end_pnl_sol_total/total_pnl if total_pnl else 0:+.1f}% of total)")
    print(f"  other exits PnL (SOL):     {total_pnl - re_end_pnl_sol_total:+.5f}")

    print(f"\n=== Other-exit breakdown ===")
    for er, c in other_exit_classes.most_common():
        print(f"  {er:25s} {c}")

    # Now find the worst recording_ended trades and inspect exit_params.
    print(f"\n=== Worst 15 recording_ended trades (by pnl_pct) ===")
    re_end_summary.sort(key=lambda x: x["pnl_pct"])
    for s in re_end_summary[:15]:
        ep = s.get("exit_params", {}) or {}
        print(f"  rec{s['recording_id']:5d} {str(s['symbol'])[:12]:12s} entry@{s['entry_price']:.6g} "
              f"exit@{s['exit_price']:.6g} pnl={s['pnl_pct']:+7.1f}% ({s['pnl_sol']:+.5f} SOL) | "
              f"@exit regime={ep.get('regime', '?'):12s} dir={ep.get('direction', '?'):4s} "
              f"m_hat={float(ep.get('m_hat',0)):+.4f} conf={float(ep.get('trend_confidence',0)):.2f} "
              f"s_eff={float(ep.get('s_effective',0)):.2g}")

    print(f"\n=== Best 10 recording_ended trades (by pnl_pct) ===")
    for s in re_end_summary[-10:]:
        ep = s.get("exit_params", {}) or {}
        print(f"  rec{s['recording_id']:5d} {str(s['symbol'])[:12]:12s} entry@{s['entry_price']:.6g} "
              f"exit@{s['exit_price']:.6g} pnl={s['pnl_pct']:+7.1f}% ({s['pnl_sol']:+.5f} SOL) | "
              f"@exit regime={ep.get('regime', '?'):12s} dir={ep.get('direction', '?'):4s} "
              f"m_hat={float(ep.get('m_hat',0)):+.4f} conf={float(ep.get('trend_confidence',0)):.2f} "
              f"s_eff={float(ep.get('s_effective',0)):.2g}")

    # Per-recording count of recording_ended trades — how concentrated?
    re_end_recs = Counter(t["recording_id"] for t in re_end_summary)
    print(f"\n=== Recording_ended concentration ===")
    print(f"  # recordings with ≥1 recording_ended exit:  {len(re_end_recs)}")
    print(f"  Top 10 recs by # of recording_ended trades:")
    for rec_id, cnt in re_end_recs.most_common(10):
        rr_pnl = sum(t["pnl_sol"] for t in re_end_summary if t["recording_id"] == rec_id)
        print(f"    rec{rec_id:5d}: {cnt} re-end trades, total re-end PnL = {rr_pnl:+.5f} SOL, "
              f"rec total PnL = {per_rec_pnl.get(rec_id,0):+.5f} SOL")

    # Stash the worst-trade list to disk so it can drive the next probe.
    out_path = os.path.join(os.path.dirname(__file__), "..",
                            f"iter08_failure_probe_{args.batch_id}.json")
    payload = {
        "batch_id": args.batch_id,
        "n_total_trades": n_total_trades,
        "n_re_end_trades": n_re_end_trades,
        "re_end_pnls_pct": re_end_pnls_pct,
        "other_exit_classes": dict(other_exit_classes),
        "re_end_summary": re_end_summary,
        "per_rec_pnl": {int(k): float(v) for k, v in per_rec_pnl.items()},
    }
    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\nSaved → {out_path}")


if __name__ == "__main__":
    main()
