"""Aggregate per-trade JSON results from a V2 batch run.

Walks backend/backtest_results/ for files matching a given batch_id (or,
when None, the most recent per-recording_id file), and produces:

  * aggregate stats: total trades, win rate, total PnL, profit factor,
    expectancy, max drawdown (per-token sum), worst/best trade
  * per-token summary rows
  * exit-reason distribution
  * worst-trades breakdown with latent state at entry
  * regime-at-entry distribution of winners vs losers
  * a histogram of PnL outcomes

Outputs a JSON file under backend/analysis/<label>.json plus a textual
summary on stdout.  Used as the canonical "what did this batch do" tool
in the iterative research loop.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from typing import Iterable


RESULTS_DIR = os.environ.get(
    "BACKTEST_RESULTS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "backtest_results"),
)
ANALYSIS_DIR = os.path.dirname(__file__)


def _safe(x, default=0.0):
    try:
        if x is None:
            return default
        x = float(x)
        if x != x or x in (float("inf"), float("-inf")):
            return default
        return x
    except Exception:
        return default


def _gather(batch_id: str | None) -> list[dict]:
    """Return list of parsed trade-log dicts matching this batch id."""
    if batch_id:
        files = sorted(glob.glob(os.path.join(RESULTS_DIR, f"*_{batch_id}_*.json")))
    else:
        # All files; we'll filter by newest per rec below.
        files = sorted(glob.glob(os.path.join(RESULTS_DIR, "*.json")), key=os.path.getmtime)
    if not batch_id:
        # newest per (recording_id) — emulate run_iter behaviour
        latest_for_rec: dict[int, str] = {}
        for f in files:
            m = re.search(r"_rec(\d+)_", os.path.basename(f))
            if not m:
                continue
            rec = int(m.group(1))
            cur = latest_for_rec.get(rec)
            if cur is None or os.path.getmtime(f) > os.path.getmtime(cur):
                latest_for_rec[rec] = f
        files = list(latest_for_rec.values())

    out = []
    for fn in files:
        try:
            with open(fn) as f:
                d = json.load(f)
        except Exception:
            continue
        d["__file"] = fn
        out.append(d)
    return out


def _aggregate(records: list[dict]) -> dict:
    """Compute aggregate stats across all per-token files."""
    all_trades: list[dict] = []
    per_token: list[dict] = []
    for d in records:
        ts = d.get("trades", [])
        sym = d.get("token_symbol", "") or d.get("token_name", "") or "unknown"
        rec = d.get("recording_id", 0)
        s = d.get("summary", {})
        per_token.append({
            "symbol": sym,
            "recording_id": rec,
            "total_trades": s.get("total_trades", len(ts)),
            "winning_trades": s.get("winning_trades", sum(1 for t in ts if t.get("outcome") == "W")),
            "losing_trades":  s.get("losing_trades",  sum(1 for t in ts if t.get("outcome") == "L")),
            "win_rate_pct":   s.get("win_rate_pct", 0.0),
            "total_pnl_sol":  s.get("total_pnl_sol", 0.0),
            "max_drawdown_pct": s.get("max_drawdown_pct", 0.0),
        })
        for t in ts:
            t = dict(t)
            t["__symbol"] = sym
            t["__recording_id"] = rec
            all_trades.append(t)

    n = len(all_trades)
    wins = [t for t in all_trades if t.get("outcome") == "W" or _safe(t.get("pnl_pct")) > 0]
    losses = [t for t in all_trades if t not in wins and _safe(t.get("pnl_pct")) < 0]
    gross_win = sum(_safe(t.get("pnl_sol")) for t in wins)
    gross_loss = abs(sum(_safe(t.get("pnl_sol")) for t in losses))
    total_pnl = sum(_safe(t.get("pnl_sol")) for t in all_trades)
    winrate = (len(wins) / n * 100.0) if n else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    expectancy = (total_pnl / n) if n else 0.0

    pnl_list = sorted([_safe(t.get("pnl_sol")) for t in all_trades])
    pnl_pct_list = sorted([_safe(t.get("pnl_pct")) for t in all_trades])
    max_dd_token = max(per_token, key=lambda p: _safe(p.get("max_drawdown_pct"))) if per_token else {}

    # Exit reason distribution
    exit_reasons = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0.0, "worst": 0.0})
    for t in all_trades:
        r = t.get("exit_reason", "?")
        pnl_pct = _safe(t.get("pnl_pct"))
        pnl_sol = _safe(t.get("pnl_sol"))
        er = exit_reasons[r]
        er["n"] += 1
        if pnl_pct > 0:
            er["w"] += 1
        else:
            er["l"] += 1
        er["pnl"] += pnl_sol
        er["worst"] = min(er["worst"], pnl_pct)

    # Entry regime distribution
    entry_regimes = defaultdict(lambda: {"n": 0, "w": 0, "l": 0, "pnl": 0.0})
    for t in all_trades:
        ep = t.get("entry_params", {}) or {}
        rg = ep.get("regime", "?")
        pnl_pct = _safe(t.get("pnl_pct"))
        pnl_sol = _safe(t.get("pnl_sol"))
        er = entry_regimes[rg]
        er["n"] += 1
        if pnl_pct > 0:
            er["w"] += 1
        else:
            er["l"] += 1
        er["pnl"] += pnl_sol

    return {
        "total_trades": n,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": round(winrate, 3),
        "total_pnl_sol": round(total_pnl, 5),
        "gross_win_sol": round(gross_win, 5),
        "gross_loss_sol": round(gross_loss, 5),
        "profit_factor": round(profit_factor, 4) if profit_factor != float("inf") else float("inf"),
        "expectancy_sol": round(expectancy, 6),
        "worst_trade_pct": pnl_pct_list[0] if pnl_pct_list else 0.0,
        "best_trade_pct": pnl_pct_list[-1] if pnl_pct_list else 0.0,
        "worst_trade_sol": pnl_list[0] if pnl_list else 0.0,
        "best_trade_sol": pnl_list[-1] if pnl_list else 0.0,
        "max_token_drawdown_pct": _safe(max_dd_token.get("max_drawdown_pct")),
        "tokens_traded": len(per_token),
        "tokens_total": len(records),
        "per_token": per_token,
        "exit_reasons": {k: dict(v) for k, v in exit_reasons.items()},
        "entry_regimes": {k: dict(v) for k, v in entry_regimes.items()},
        "all_trades_compact": [
            {
                "sym": t.get("__symbol"),
                "rec": t.get("__recording_id"),
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"),
                "pnl_pct": _safe(t.get("pnl_pct")),
                "pnl_sol": _safe(t.get("pnl_sol")),
                "outcome": t.get("outcome"),
                "exit_reason": t.get("exit_reason"),
                "entry_reason": t.get("entry_reason"),
                "regime_at_entry": (t.get("entry_params") or {}).get("regime"),
                "confidence_at_entry": (t.get("entry_params") or {}).get("trend_confidence", 0.0),
                "s_effective_at_entry": (t.get("entry_params") or {}).get("s_effective"),
            }
            for t in all_trades
        ],
    }


def print_summary(label: str, agg: dict):
    print(f"\n========== {label} ==========")
    print(f"Tokens traded / total: {agg['tokens_traded']}/{agg['tokens_total']}")
    print(f"Total trades:          {agg['total_trades']}")
    print(f"  Wins / Losses:       {agg['winning_trades']} / {agg['losing_trades']}")
    print(f"Winrate:               {agg['win_rate_pct']:.2f}%")
    print(f"Total PnL (SOL):       {agg['total_pnl_sol']:.5f}")
    print(f"Gross win / loss:      {agg['gross_win_sol']:.5f} / {agg['gross_loss_sol']:.5f}")
    print(f"Profit factor:         {agg['profit_factor']:.4f}")
    print(f"Expectancy / trade:    {agg['expectancy_sol']:.6f} SOL")
    print(f"Best / Worst trade:    {agg['best_trade_pct']:.2f}% / {agg['worst_trade_pct']:.2f}%")
    print(f"Max token drawdown:    {agg['max_token_drawdown_pct']:.2f}%")
    print()
    print(f"--- Exit reasons ({len(agg['exit_reasons'])}) ---")
    print(f"{'reason':25s} {'n':>4s} {'W':>4s} {'L':>4s} {'win%':>7s} {'pnl':>9s} {'worst%':>8s}")
    for r, st in sorted(agg["exit_reasons"].items(), key=lambda kv: -kv[1]["n"]):
        n = st["n"]
        wr = st["w"] / n * 100 if n else 0.0
        print(f"{str(r):25s} {n:>4d} {st['w']:>4d} {st['l']:>4d} {wr:>6.1f}% {st['pnl']:>9.4f} {st['worst']:>8.1f}")
    print()
    print(f"--- Entry regimes ({len(agg['entry_regimes'])}) ---")
    print(f"{'regime':18s} {'n':>4s} {'W':>4s} {'L':>4s} {'win%':>7s} {'pnl':>9s}")
    for r, st in sorted(agg["entry_regimes"].items(), key=lambda kv: -kv[1]["n"]):
        n = st["n"]
        wr = st["w"] / n * 100 if n else 0.0
        print(f"{str(r):18s} {n:>4d} {st['w']:>4d} {st['l']:>4d} {wr:>6.1f}% {st['pnl']:>9.4f}")
    print()
    print(f"--- Worst 10 trades ---")
    worst = sorted(agg["all_trades_compact"], key=lambda t: t["pnl_pct"])[:10]
    for w in worst:
        print(f"  {str(w['sym'])[:14]:14s} rec{w['rec']:>4d} {w['pnl_pct']:>7.1f}% pnl={w['pnl_sol']:.5f} exit={w['exit_reason']:20s} regime={str(w['regime_at_entry']):12s} conf={w['confidence_at_entry']:.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", default=None,
                    help="batch_id of the run to aggregate (default: newest per rec)")
    ap.add_argument("--label", default="latest",
                    help="label used in printed header and saved JSON filename")
    ap.add_argument("--save", action="store_true",
                    help="write JSON analysis to backend/analysis/<label>.json")
    args = ap.parse_args()

    records = _gather(args.batch_id)
    if not records:
        print(f"No results found for batch_id={args.batch_id}", file=sys.stderr)
        sys.exit(2)
    agg = _aggregate(records)
    print_summary(args.label, agg)
    if args.save:
        out_path = os.path.join(ANALYSIS_DIR, f"{args.label}.json")
        with open(out_path, "w") as f:
            json.dump(agg, f, indent=2, default=str)
        print(f"\nSaved analysis → {out_path}")


if __name__ == "__main__":
    main()
