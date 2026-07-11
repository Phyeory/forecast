import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'backend'))

import json
from backtester import run_backtest_batch
from pprint import pprint

with open(os.path.join(os.path.dirname(__file__), "backend", "best_params.json")) as f:
    best_params = json.load(f)["params"]

if __name__ == "__main__":
    results = run_backtest_batch(
        engine_params=best_params,
        buy_size_sol=0.1,
        max_workers=6
    )

    print(f"Completed {len(results)} backtests.")

    total_trades = sum(r['stats']['total_trades'] for r in results if 'stats' in r)
    winning_trades = sum(r['stats']['winning_trades'] for r in results if 'stats' in r)
    total_pnl = sum(r['stats']['total_pnl_sol'] for r in results if 'stats' in r)

    winrate = winning_trades / total_trades if total_trades > 0 else 0
    print(f"Total Trades: {total_trades}")
    print(f"Winrate: {winrate*100:.2f}%")
    print(f"Total PnL (SOL): {total_pnl:.5f}")
