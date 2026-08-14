"""Run V2 backtest batch across all completed recordings using V2 defaults.

Mirrors run_batch.py but uses engine_version=2 with V2's default config
(derived from DEFAULT_CONFIG in strategy_engineV2).  Result summary is
aggregated so we can compare against V1's run_batch.py output.
"""
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

from backtester import run_backtest_batch

if __name__ == "__main__":
    results = run_backtest_batch(
        engine_version=2,
        engine_params={},          # use V2 DEFAULT_CONFIG
        buy_size_sol=0.1,
        max_workers=8,
    )

    print(f"\nCompleted {len(results)} backtests (engine_version=2).")

    total_trades = sum(r.get('stats', {}).get('total_trades', 0) for r in results)
    winning_trades = sum(r.get('stats', {}).get('winning_trades', 0) for r in results)
    losing_trades = sum(r.get('stats', {}).get('losing_trades', 0) for r in results)
    total_pnl = sum(r.get('stats', {}).get('total_pnl_sol', 0.0) for r in results)
    n_traded = sum(1 for r in results if r.get('stats', {}).get('total_trades', 0) > 0)
    n_errors = sum(1 for r in results if 'error' in r)

    winrate = winning_trades / total_trades if total_trades > 0 else 0.0
    print(f"  Records traded:    {n_traded}/{len(results)}")
    print(f"  Total trades:      {total_trades}")
    print(f"  Winning trades:    {winning_trades}")
    print(f"  Losing trades:     {losing_trades}")
    print(f"  Winrate:           {winrate*100:.2f}%")
    print(f"  Total PnL (SOL):   {total_pnl:.5f}")
    if n_traded:
        print(f"  Avg PnL/traded:    {total_pnl/n_traded:.5f}")
    print(f"  Errors:            {n_errors}")
