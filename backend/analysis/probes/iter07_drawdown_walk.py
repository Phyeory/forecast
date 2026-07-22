"""iter07 trade-walk drawdown simulator.

For every iter04_full trade:
  1. Open the per-token JSON trade log to get entry_time, exit_time, entry_price.
  2. Walk the candle stream from entry_time to exit_time and find the
     minimum-low-price the engine saw during the trade.
  3. Compute `drawdown_pct = (min_low − entry_price) / entry_price × 100`.

Then simulate hard cap exits:
  - If `min_low` ≤ `entry × (1 + thresh/100)`, the new SL cap fires.
  - pnl_sold becomes `thresh * 0.1` (sized SOL) instead of original.
  - Net PnL improvement across all winners + losers is the iter07 candidate.

Output:
  /Users/jaime/pump-chart/backend/analysis/iter04_intra_trade_drawdowns.json
    — every trade with: pnl_pct, pnl_sol, drawdown_pct, duration, etc.

Run:
  /Users/jaime/pump-chart/backend/.venv/bin/python3 \
    /Users/jaime/pump-chart/backend/analysis/probes/iter07_drawdown_walk.py
"""
import json
import sqlite3
import glob
import os
import sys

DB_PATH = '/Users/jaime/pump-chart/backend/data/price_data.db'
TRADE_LOG_DIR = '/Users/jaime/pump-chart/backend/v2_results'
AGGREGATE = '/Users/jaime/pump-chart/backend/analysis/iter04_full.json'
OUTPUT = '/Users/jaime/pump-chart/backend/analysis/iter04_intra_trade_drawdowns.json'

DB = sqlite3.connect(DB_PATH)


def main() -> None:
    print(f'Loading aggregate from {AGGREGATE}...')
    with open(AGGREGATE) as f:
        d = json.load(f)
    print(f'Aggregate has {len(d["all_trades_compact"])} compact trade records.')

    files = sorted(
        glob.glob(os.path.join(TRADE_LOG_DIR, '*_iter04_full_*.json'))
    )
    print(f'{len(files)} per-token trade-log files.')

    walk = []
    cache: dict[int, list[tuple[int, float, float]]] = {}
    n_done = 0
    for fpath in files:
        with open(fpath) as f:
            fd = json.load(f)
        rid = fd['recording_id']
        if rid not in cache:
            rows = DB.execute(
                "SELECT time, high, low FROM candles WHERE recording_id=? ORDER BY time",
                (rid,),
            ).fetchall()
            cache[rid] = rows
        rows = cache[rid]
        if not rows:
            continue
        candle_map = {r[0]: (r[1], r[2]) for r in rows}
        times = sorted(candle_map.keys())
        for t in fd['trades']:
            et = t['entry_time']
            xt = t['exit_time']
            ep = t['entry_price']
            min_low = ep
            for tt in times:
                if tt < et:
                    continue
                if tt > xt:
                    break
                low = candle_map[tt][1]
                if low < min_low:
                    min_low = low
            u_pct = (min_low - ep) / ep * 100
            walk.append(dict(
                rec=rid,
                sym=fd.get('token_symbol', ''),
                pnl_pct=t['pnl_pct'],
                pnl_sol=t['pnl_sol'],
                pnl_sol_max=(t['pnl_pct'] / 100.0) * 0.1,
                outcome=t['outcome'],
                entry_price=ep,
                exit_price=t['exit_price'],
                entry_time=et,
                exit_time=xt,
                duration=xt - et,
                drawdown_pct=u_pct,
            ))
        n_done += 1
        if n_done % 200 == 0:
            print(f' processed {n_done} files')

    print(f'walked {len(walk)} trades')
    winners = [t for t in walk if t['outcome'] == 'W']
    losers = [t for t in walk if t['outcome'] == 'L']
    print(f'Winners: {len(winners)}  PnL total: {sum(t["pnl_sol"] for t in winners):+.4f}')
    print(f'Losers:  {len(losers)}   PnL total: {sum(t["pnl_sol"] for t in losers):+.4f}')

    print()
    print('Trade walk simulation: hard SL cap = max underwater % allowed before exit.')
    print(f'{"thresh":>8} | {"win-dip":>8} {"L-dip":>8} | {"win-cost":>9} {"lose-save":>9} {"net":>9}')
    base_size_sol = 0.1  # buy_size default in backtester
    SL_THRESHES_TENTATIVE = (-7.5, -10, -12.5, -15, -20, -25)
    for thresh in SL_THRESHES_TENTATIVE:
        w_dipped = sum(1 for t in winners if t['drawdown_pct'] <= thresh)
        l_dipped = sum(1 for t in losers if t['drawdown_pct'] <= thresh)
        # If SL cap fires, the trade exits at -thresh (vs entry). pnl_sol delta:
        #   winner:   original_pnl → thresh_pnl  (loses)
        #   loser:    original_pnl → thresh_pnl  (saves)
        w_cost = sum((thresh - t['pnl_pct']) / 100.0 * base_size_sol
                     for t in winners if t['drawdown_pct'] <= thresh)
        l_save = sum((t['pnl_pct'] - thresh) / 100.0 * base_size_sol
                     for t in losers if t['drawdown_pct'] <= thresh)
        net = l_save + w_cost
        print(f'  {thresh:>+7.2f}% | {w_dipped:>8d} {l_dipped:>8d} | '
              f'{w_cost:>+9.4f} {l_save:>+9.4f} {net:>+9.4f}')

    with open(OUTPUT, 'w') as f:
        json.dump(walk, f, default=str, indent=2)
    print(f'\nSaved {len(walk)} walked trades -> {OUTPUT}')


if __name__ == '__main__':
    main()
