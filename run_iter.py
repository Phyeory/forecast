#!/usr/bin/env python3
"""Run the full batch and report winrate/PnL/big-losses/exit-reason distribution.

Reads per-trade JSON files written by the latest run (matched by the most recent
generated_at timestamp per recording_id), so per-trade analysis is accurate.
"""
import sys
import os
import glob
import json
import time
from collections import defaultdict

sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))
from backtester import run_backtest_batch


def _load_results(result_dicts):
    # backtester writes per-token JSON files. walk result_dicts to figure out
    # symbol+recording_id + we need newest matching file.
    file_for = {}
    for r in result_dicts:
        if 'stats' not in r:
            continue
        sym = r.get('token_symbol', '') or r.get('token_name', '') or 'unknown'
        rec = r.get('recording_id', 0)
        sym = re.sub(r'[^\w\-]', '_', sym).strip('_') or 'unknown'
        # Find files for this rec id, pick newest by ctime
        cands = glob.glob(f"backend/backtest_results/*_rec{rec}_*.json")
        if not cands:
            continue
        cands.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        file_for[rec] = cands[0]
    return file_for


if __name__ == "__main__":
    import re
    with open(os.path.join(os.path.dirname(__file__), "backend", "best_params.json")) as f:
        best_params = json.load(f)["params"]

    t0 = time.time()
    batch_id = str(int(time.time()))
    results = run_backtest_batch(
        engine_params=best_params,
        buy_size_sol=0.1,
        max_workers=6,
        batch_id=batch_id,
    )
    total_trades = sum(r['stats']['total_trades'] for r in results if 'stats' in r)
    winning_trades = sum(r['stats']['winning_trades'] for r in results if 'stats' in r)
    total_pnl = sum(r['stats']['total_pnl_sol'] for r in results if 'stats' in r)
    winrate = winning_trades / total_trades if total_trades > 0 else 0
    print(f"\n========== SUMMARY ({time.time()-t0:.1f}s) ==========")
    print(f"Total Trades:    {total_trades}")
    print(f"Winrate:          {winrate*100:.2f}%")
    print(f"Total PnL (SOL):  {total_pnl:.5f}")

    # Per-trade analysis from the just-written JSON files (filter by batch_id in file)
    big_losses = 0
    big_loss_trades = []
    exit_reason_stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0, "worst": 0.0, "big_losses": 0})
    files = glob.glob(f"backend/backtest_results/*_{batch_id}_*.json")
    for fn in files:
        try:
            d = json.load(open(fn))
        except Exception:
            continue
        for t in d.get('trades', []):
            pnl = t.get('pnl_pct', 0.0)
            pnl_sol = t.get('pnl_sol', 0.0)
            reason = t.get('exit_reason', '?')
            outcome = t.get('outcome', '')
            if pnl < -15.0:
                big_losses += 1
                big_loss_trades.append((d.get('token_symbol', ''), d.get('recording_id', 0), pnl, reason, t.get('entry_params', {}), t.get('entry_reason', '')))
            er = exit_reason_stats[reason]
            if outcome == 'W' or pnl > 0:
                er['w'] += 1
            else:
                er['l'] += 1
            er['pnl'] += pnl_sol
            er['worst'] = min(er['worst'], pnl)
            if pnl < -15.0:
                er['big_losses'] += 1

    print(f"Big losses (>-15%): {big_losses}")
    print(f"\n--- Exit reason distribution ---")
    print(f"{'reason':25s} {'W':>5s} {'L':>5s} {'winrate':>8s} {'pnl_sol':>10s} {'worst%':>8s} {'bigloss':>8s}")
    for reason, st in sorted(exit_reason_stats.items(), key=lambda kv: -abs(kv[1]['w']+kv[1]['l'])):
        n = st['w'] + st['l']
        wr = st['w'] / n if n else 0
        print(f"{str(reason):25s} {st['w']:>5d} {st['l']:>5d} {wr*100:>7.1f}% {st['pnl']:>10.4f} {st['worst']:>8.1f} {st['big_losses']:>8d}")

    if big_loss_trades:
        print(f"\n--- Worst big-loss tokens (top 10) ---")
        for sym, rec, pnl, reason, ep, er in sorted(big_loss_trades, key=lambda x: x[2])[:10]:
            cont = ep.get('buy_continuation', False)
            mp = ep.get('momentum_past_peak', False)
            regime = ep.get('regime', '?')
            conf = ep.get('trend_confidence', 0)
            se = ep.get('s_effective', '|S|=')
            print(f"  {str(sym):12s} rec{rec} {pnl:>7.1f}% {reason:18s} reason={er:18s} regime={regime:13s} mpast={mp} conf={conf:.2f}")
