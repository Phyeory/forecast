import warnings; warnings.filterwarnings('ignore')
import time, random
from data_store import list_recordings
from backtester import run_backtest

random.seed(42)
recs = [r for r in list_recordings() if r.get('status') == 'completed' and r.get('candle_count', 0) >= 120]
random.shuffle(recs)
sample = recs[:30]

results = []
t0 = time.time()
for i, r in enumerate(sample):
    try:
        res = run_backtest(
            recording_id=r['id'], engine_version=1, engine_params={},
            buy_size_sol=0.1, starting_balance=1.0, slippage_pct=1.0,
        )
        s = res.get('stats', {})
        results.append({
            'id': r['id'], 'symbol': r.get('token_symbol', '?'),
            'trades': s.get('total_trades', 0),
            'pnl': s.get('total_pnl_sol', 0.0),
            'bal': s.get('current_balance', 1.0),
            'winrate': s.get('win_rate', 0.0),
        })
    except Exception as e:
        print(f'  rec {r["id"]} FAIL:', e)
    if (i+1) % 5 == 0:
        print(f'  done {i+1}/30 elapsed={time.time()-t0:.1f}s')

n = len(results)
n_with_trades = sum(1 for r in results if r['trades'] > 0)
total_pnl = sum(r['pnl'] for r in results)
total_trades = sum(r['trades'] for r in results)
mean_winrate = sum(r['winrate'] for r in results if r['trades']>0) / max(n_with_trades,1)
profitable = sum(1 for r in results if r['pnl'] > 0)
print('V1 AUDIT (30 recordings, candle_count >= 120):')
print(f'  ran: {n}')
print(f'  recordings-with-trades: {n_with_trades}')
print(f'  total trades: {total_trades}')
print(f'  total pnl SOL: {total_pnl:.4f}')
print(f'  mean per-recording pnl (when traded): {total_pnl/max(n_with_trades,1):.4f}')
print(f'  profitable recordings: {profitable}/{n_with_trades}')
print(f'  mean winrate (when traded): {mean_winrate:.2%}')
