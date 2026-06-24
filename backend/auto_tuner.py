#!/usr/bin/env python3
"""
Auto-Tuner v14 — 60% WR / 280 Trades / +1.6 SOL Edition.

Target: WR≈60%  trades≈280  PnL≥+1.6 SOL  across all completed recordings.

═══════════════════════════════════════════════════════════════════════
NO LOOKAHEAD BIAS
═══════════════════════════════════════════════════════════════════════
The backtester already enforces a strict forward-only model:
  • Candles are fed in chronological order.
  • Each candle is broken into 4 intra-candle accumulated states.
  • A pending BUY/EXIT queued at candle N executes at State 1 of
    candle N+1 (next bar's open) — no same-candle fill.
The tuner itself does in-sample parameter search (expected for any
optimizer), but never peeks at results from future candles to make
decisions within a single backtest run.

═══════════════════════════════════════════════════════════════════════
SCORER v14
═══════════════════════════════════════════════════════════════════════
  WR band:   55–65% sweet spot, steep bonus at 60%
  Trade band: 250–310 sweet spot, peak at 280
  PnL:        reward PnL per trade, huge bonus at PnL≥1.6

═══════════════════════════════════════════════════════════════════════
PHASE STRUCTURE v14
═══════════════════════════════════════════════════════════════════════
  0. Baseline evaluation
  1. Sensitivity scan (find highest-impact params)
  T. Targeted configurations (pre-baked jumps)
  R. Random population search (300 configs)
  A→E outer loop (up to 4 rounds):
    A. Balanced CD on all params
    B. PnL-focused sweep
    C. Trade-count recovery
    D. Gradient climb
    E. Fine-tune HOF
"""
from __future__ import annotations
import argparse, hashlib, json, math, os, random, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from itertools import combinations
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))
from backtester import run_backtest_batch
from data_store import list_recordings

G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; C = "\033[96m"
B = "\033[1m";  D = "\033[2m";  X = "\033[0m"

def col(t, c): return f"{c}{t}{X}"


# ── DB auto-purge ─────────────────────────────────────────────────────────────
_PURGE_EVERY: int = 50

def _find_db_path() -> str:
    env = os.environ.get("BACKTEST_DB_PATH")
    if env: return env
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "backtest_data.db"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_data.db"),
        os.path.join(os.getcwd(), "backtest_data.db"),
    ]
    for p in candidates:
        if os.path.exists(p): return p
    return candidates[0]

_DB_PATH: str = _find_db_path()
_eval_lock:    threading.Lock = threading.Lock()
_eval_counter: int            = 0
_total_purged: int            = 0

def _purge_db(reason: str = "") -> None:
    global _total_purged
    freed = 0
    for path in [_DB_PATH, _DB_PATH + "-wal", _DB_PATH + "-shm"]:
        if os.path.exists(path):
            try:
                freed += os.path.getsize(path)
                os.remove(path)
            except OSError as e:
                print(col(f"  WARN purge {path}: {e}", Y))
    _total_purged += freed
    if freed > 0:
        print(col(f"  🗑  DB purged ({reason}): freed {freed/1_048_576:.1f} MB  "
                  f"(total {_total_purged/1_048_576:.1f} MB)", D))

def _tick_and_maybe_purge() -> None:
    global _eval_counter
    with _eval_lock:
        _eval_counter += 1
        should = (_eval_counter % _PURGE_EVERY == 0)
    if should:
        _purge_db(reason=f"eval #{_eval_counter}")


# ── Result cache ──────────────────────────────────────────────────────────────
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
_cache_hits = 0

def _params_key(params: dict) -> str:
    s = json.dumps(params, sort_keys=True, separators=(',', ':'))
    return hashlib.md5(s.encode()).hexdigest()

def _cache_get(params: dict) -> Optional[dict]:
    global _cache_hits
    k = _params_key(params)
    with _cache_lock:
        hit = _cache.get(k)
        if hit is not None: _cache_hits += 1
    return deepcopy(hit) if hit else None

def _cache_put(params: dict, result: dict) -> None:
    k = _params_key(params)
    with _cache_lock: _cache[k] = result


# ── Baseline params (synced with app.js) ──────────────────────────────────────
_BASELINE = {
    "ema_fast": 3, "ema_slow": 7, "atr_period": 7, "roc_period": 3, "warmup": 30,
    "signal_strong": 4, "signal_weak": 1.5, "signal_noise": 1.1535714285714287,
    "exhaustion_bars_limit": 1, "delta_threshold": 0.3, "kalman_gamma": 0.125,
    "min_trend_bars": 3, "reversal_confirm_bars": 2, "chop_atr_pct": 0.3,
    "chop_spread_pct": 0.05, "reversal_exit_confirm_bars": 0,
    "s_effective_threshold": 0.35, "exhaustion_persist_bars": 6, "regime_lookback": 6,
    "persistence_threshold": 2, "momentum_mean_threshold": 0.0, "ema_min_spread_pct": 0.02,
    "confidence_high": 0.79, "confidence_low": 0.19,
    "entry_confidence_high": 0.79, "entry_confidence_low": 0.19,
    "confidence_w1": 0.3, "confidence_w2": 0.25, "confidence_w3": 0.25, "confidence_w4": 0.2,
    "atr_floor_k": 0, "ema_cross_persist_bars": 2, "exhaustion_s_decay_bars": 1,
    "exhaustion_stall_bars": 6, "exhaustion_stall_atr_pct": 3, "local_range_bars": 80,
    "local_range_threshold_pct": 5, "sign_flip_threshold": 0, "stability_bars": 3,
    "spike_atr_multiplier": 1.2, "spike_lookback_bars": 9, "body_baseline_bars": 14,
    "overextension_k": 0.17, "momentum_peak_bars": 1, "consolidation_range_pct": 0,
    "confidence_very_high": 0.86, "ema_macro_period": 7,
    "stoploss_pct": 25, "takeprofit_pct": 0,
    "takeprofit_pct_low": 0, "takeprofit_pct_high": 0,
    "stoploss_pct_low": 0, "stoploss_pct_high": 0,
}

_best_json = os.path.join(os.path.dirname(__file__), "best_params.json")
if os.path.exists(_best_json):
    try:
        with open(_best_json) as f:
            content = f.read().strip()
        if content:
            _prev = json.loads(content)
            _prev_wr = _prev.get("win_rate", 0)
            _prev_trades = _prev.get("total_trades", 0)
            if _prev_wr >= 55.0 and _prev_trades >= 200:
                START = _prev.get("params", _BASELINE)
                print(col(f"Loaded previous best: WR={_prev_wr:.1f}%  "
                          f"trades={_prev_trades}  score={_prev.get('score',0):.4f}", C))
            else:
                START = deepcopy(_BASELINE)
                _prev = {}
                print(col(f"Discarded previous best (WR={_prev_wr:.1f}%, trades={_prev_trades}) — starting fresh.", Y))
        else:
            _prev = {}
            START = deepcopy(_BASELINE)
    except (json.JSONDecodeError, KeyError):
        _prev = {}
        START = deepcopy(_BASELINE)
else:
    _prev = {}
    START = deepcopy(_BASELINE)


# ── Search space ──────────────────────────────────────────────────────────────
# (lo, hi, step, is_int)
SPACE = {
    # ── A. ENTRY GATES ───────────────────────────────────────────────────────
    "confidence_high":          (0.40, 0.97, 0.01, False),
    "confidence_low":           (0.05, 0.55, 0.01, False),
    "entry_confidence_high":    (0.40, 0.97, 0.01, False),
    "entry_confidence_low":     (0.05, 0.55, 0.01, False),
    "confidence_very_high":     (0.50, 0.98, 0.01, False),
    "signal_strong":            (1.00, 6.00, 0.10, False),
    "signal_weak":              (0.50, 3.00, 0.10, False),
    "signal_noise":             (0.50, 2.50, 0.10, False),
    "s_effective_threshold":    (0.01, 0.80, 0.05, False),
    "min_trend_bars":           (1, 4, 1, True),
    "stability_bars":           (1, 4, 1, True),
    "ema_cross_persist_bars":   (1, 4, 1, True),
    "persistence_threshold":    (1, 6, 1, True),
    "warmup":                   (15, 60, 5, True),

    # ── B. EXIT SPEED ────────────────────────────────────────────────────────
    "reversal_confirm_bars":      (1, 3, 1, True),
    "reversal_exit_confirm_bars": (0, 2, 1, True),
    "exhaustion_bars_limit":      (1, 5, 1, True),
    "exhaustion_s_decay_bars":    (1, 4, 1, True),
    "momentum_peak_bars":         (1, 4, 1, True),
    "exhaustion_persist_bars":    (1, 6, 1, True),
    "exhaustion_stall_bars":      (1, 6, 1, True),
    "exhaustion_stall_atr_pct":   (0.5, 5.0, 0.5, False),

    # ── C. CHOP / NOISE FILTERS ──────────────────────────────────────────────
    "consolidation_range_pct":   (0.0, 8.0, 0.5, False),
    "local_range_threshold_pct": (0.05, 0.95, 0.05, False),
    "local_range_bars":          (5, 80, 5, True),
    "sign_flip_threshold":       (0, 10, 1, True),
    "chop_atr_pct":              (0.1, 2.0, 0.1, False),
    "chop_spread_pct":           (0.01, 0.80, 0.05, False),

    # ── D. BLOW-OFF TOP GUARDS ───────────────────────────────────────────────
    "overextension_k":       (0.02, 0.50, 0.01, False),
    "spike_atr_multiplier":  (0.5, 8.0, 0.5, False),
    "spike_lookback_bars":   (2, 15, 1, True),
    "body_baseline_bars":    (10, 80, 5, True),

    # ── E. SIGNAL PROCESSING ────────────────────────────────────────────────
    "kalman_gamma":    (0.01, 0.60, 0.01, False),
    "regime_lookback": (2, 15, 1, True),

    # ── F. TP / SL (NEW — previously missing from search) ────────────────────
    "stoploss_pct":         (-50.0, 50.0, 2.0, False),
    "takeprofit_pct":       (0.0, 100.0, 5.0, False),
    "takeprofit_pct_low":   (0.0, 80.0, 5.0, False),
    "takeprofit_pct_high":  (0.0, 150.0, 5.0, False),
    "stoploss_pct_low":     (-50.0, 50.0, 2.0, False),
    "stoploss_pct_high":    (-50.0, 50.0, 2.0, False),

    # ── G. MACRO TREND ──────────────────────────────────────────────────────
    "ema_macro_period":  (0, 20, 1, True),

    # ── H. CONFIDENCE WEIGHTS ───────────────────────────────────────────────
    "confidence_w1": (0.05, 0.60, 0.05, False),
    "confidence_w2": (0.05, 0.60, 0.05, False),
    "confidence_w3": (0.05, 0.60, 0.05, False),
    "confidence_w4": (0.05, 0.60, 0.05, False),
}

_ENTRY_GATE_PARAMS = [
    "confidence_high", "confidence_low", "confidence_very_high",
    "entry_confidence_high", "entry_confidence_low",
    "signal_strong", "signal_weak", "signal_noise", "s_effective_threshold",
    "min_trend_bars", "stability_bars", "ema_cross_persist_bars",
    "persistence_threshold", "warmup",
]

_EXIT_PARAMS = [
    "reversal_confirm_bars", "reversal_exit_confirm_bars",
    "exhaustion_bars_limit", "exhaustion_s_decay_bars", "momentum_peak_bars",
    "exhaustion_persist_bars", "exhaustion_stall_bars", "exhaustion_stall_atr_pct",
    "stoploss_pct", "takeprofit_pct",
    "takeprofit_pct_low", "takeprofit_pct_high",
    "stoploss_pct_low", "stoploss_pct_high",
]

_FILTER_PARAMS = [
    "consolidation_range_pct", "local_range_threshold_pct", "local_range_bars",
    "sign_flip_threshold", "chop_atr_pct", "chop_spread_pct",
    "overextension_k", "spike_atr_multiplier", "spike_lookback_bars", "body_baseline_bars",
]

# ── Target constants ──────────────────────────────────────────────────────────
TARGET_WR     = 60.0
TARGET_TRADES = 280
TARGET_PNL    = 1.6
BUY_SIZE      = 0.1   # SOL per trade


# ── Scorer ────────────────────────────────────────────────────────────────────

def _score(wr: float, trades: int, pnl: float,
           coins_w: int, total: int, max_dd: float,
           avg_hold_bars: float = 0.0) -> float:
    """
    v14 scorer — targets WR≈60%, trades≈280, PnL≥+1.6 SOL.

    Philosophy:
      • WR sweet spot: 57–63%. Gentle falloff outside.
      • Trade count sweet spot: 250–310, peak at 280.
      • PnL: huge bonus for reaching +1.6 SOL target.
      • Joint bonus: all three targets met → massive multiplier.
    """
    # ── WR score (0→2.0) — bell curve centered on 60% ────────────────────────
    wr_dev = abs(wr - TARGET_WR)
    if wr_dev <= 3:
        wr_s = 1.50 + (3 - wr_dev) / 3 * 0.50   # 1.50→2.00 within ±3%
    elif wr_dev <= 7:
        wr_s = 0.80 + (7 - wr_dev) / 4 * 0.70    # 0.80→1.50
    elif wr_dev <= 15:
        wr_s = 0.20 + (15 - wr_dev) / 8 * 0.60   # 0.20→0.80
    else:
        wr_s = max(0.01, 0.20 * max(0, 30 - wr_dev) / 15)

    # Bonus for being above 55% (profitable territory)
    if wr >= 55:
        wr_s *= 1.0 + min(0.3, (wr - 55) / 50)

    # ── Trade count score (0→2.0) — bell curve centered on 280 ───────────────
    tr_dev = abs(trades - TARGET_TRADES)
    if tr_dev <= 30:
        trade_s = 1.50 + (30 - tr_dev) / 30 * 0.50  # 1.50→2.00
    elif tr_dev <= 80:
        trade_s = 0.60 + (80 - tr_dev) / 50 * 0.90   # 0.60→1.50
    elif tr_dev <= 150:
        trade_s = 0.10 + (150 - tr_dev) / 70 * 0.50  # 0.10→0.60
    else:
        trade_s = max(0.01, 0.10 * max(0, 300 - tr_dev) / 150)

    # Hard floor: need at least some trades
    if trades < 50:
        trade_s *= max(0.05, trades / 50)

    # ── PnL score (0→2.5) ────────────────────────────────────────────────────
    if pnl <= 0:
        pnl_s = max(-0.5, pnl / 2.0)
    elif pnl < 0.5:
        pnl_s = pnl / 0.5 * 0.40           # 0→0.40
    elif pnl < 1.0:
        pnl_s = 0.40 + (pnl - 0.5) / 0.5 * 0.40  # 0.40→0.80
    elif pnl < TARGET_PNL:
        pnl_s = 0.80 + (pnl - 1.0) / 0.6 * 0.70  # 0.80→1.50
    else:
        pnl_s = 1.50 + min(1.0, (pnl - TARGET_PNL) / 2.0)  # 1.50→2.50

    # PnL per trade quality check
    ppt = pnl / max(1, trades)

    # Coin coverage
    tpc = trades / max(1, total) if total > 0 else 0

    # ── Weighted base ─────────────────────────────────────────────────────────
    base = 0.35 * wr_s + 0.30 * trade_s + 0.35 * pnl_s

    # ── Penalties ─────────────────────────────────────────────────────────────
    # Negative PnL kills score
    if pnl < 0:
        base *= max(0.01, 1.0 + pnl / (abs(pnl) + 0.5))

    # Coin coverage penalty
    if tpc < 0.20:
        base *= max(0.10, tpc / 0.20)

    # Drawdown guard
    if max_dd > 8.0:
        base *= max(0.05, 1.0 - (max_dd - 8.0) / 15.0)

    # Churn penalty: many trades with near-zero PnL per trade
    if trades >= 100 and pnl > 0 and ppt < 0.001:
        base *= max(0.30, ppt / 0.001)

    # ── Joint bonuses ─────────────────────────────────────────────────────────
    wr_hit    = 57 <= wr <= 63
    tr_hit    = 250 <= trades <= 310
    pnl_hit   = pnl >= TARGET_PNL

    if wr_hit and tr_hit and pnl_hit:
        base *= 3.00   # ALL THREE — massive
    elif wr_hit and tr_hit and pnl > 0:
        base *= 2.00   # WR + trades right, PnL positive
    elif wr_hit and pnl_hit:
        base *= 1.80   # WR + PnL right
    elif tr_hit and pnl_hit:
        base *= 1.60   # trades + PnL right
    elif wr_hit and pnl > 0:
        base *= 1.30
    elif tr_hit and pnl > 0:
        base *= 1.20
    elif pnl_hit:
        base *= 1.40

    # Broad target proximity bonus
    if 55 <= wr <= 65 and trades >= 200 and pnl >= 1.0:
        base *= 1.25

    return round(base, 6)


# ── Run a single parameter set ────────────────────────────────────────────────
def _run(params: dict) -> dict:
    cached = _cache_get(params)
    if cached is not None: return cached

    res = run_backtest_batch(
        engine_params=params,
        buy_size_sol=BUY_SIZE,
        priority_fee=0.0001,
        bribe_fee=0.00001,
        slippage_pct=1.0,
        starting_balance=1.0,
    )
    ok     = [r for r in res if "error" not in r]
    trd    = sum(r["stats"]["total_trades"]  for r in ok)
    wins   = sum(r["stats"]["winning_trades"] for r in ok)
    pnl    = sum(r["stats"]["total_pnl_sol"]  for r in ok)
    coins  = sum(1 for r in ok if r["stats"]["total_trades"] > 0)
    max_dd = max((r["stats"].get("max_drawdown_pct", 0) for r in ok), default=0.0)
    hv     = [r["stats"].get("avg_hold_bars", 0) for r in ok if r["stats"].get("avg_hold_bars")]
    avg_hold = sum(hv) / len(hv) if hv else 0.0
    n_total  = len(ok)

    wr  = (wins / trd * 100) if trd > 0 else 0.0
    sc  = _score(wr, trd, pnl, coins, n_total, max_dd, avg_hold)

    _tick_and_maybe_purge()

    result = {
        "params": deepcopy(params),
        "wr": wr, "trades": trd, "pnl": pnl,
        "coins": coins, "total": n_total,
        "score": sc, "avg_hold": avg_hold,
    }
    _cache_put(params, result)
    return result


def _fmt(r: dict) -> str:
    wr_c  = G if 57 <= r["wr"] <= 63 else (Y if 55 <= r["wr"] <= 65 else R)
    tr_c  = G if 250 <= r["trades"] <= 310 else (Y if r["trades"] >= 200 else R)
    pnl_c = G if r["pnl"] >= TARGET_PNL else (Y if r["pnl"] > 0 else R)
    hold  = f" h={r.get('avg_hold', 0):.1f}b" if r.get("avg_hold") else ""
    return "WR=%s tr=%s pnl=%s cov=%d/%d%s sc=%s" % (
        col("%.1f%%" % r["wr"], wr_c),
        col(str(r["trades"]), tr_c),
        col("%+.4f" % r["pnl"], pnl_c),
        r["coins"], r["total"], hold,
        col("%.4f" % r["score"], C),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────
def _clamp(k, v):
    lo, hi, _, is_int = SPACE[k]
    return max(lo, min(hi, int(round(v)) if is_int else round(v, 6)))

def _step_val(k, v, direction):
    _, _, st, _ = SPACE[k]
    return _clamp(k, v + st * direction)

def _all_values(k):
    lo, hi, st, is_int = SPACE[k]
    vals, v = [], lo
    while v <= hi + st * 0.01:
        vals.append(int(round(v)) if is_int else round(v, 6))
        v += st
    return vals

def eval_batch(candidates, workers):
    results = [None] * len(candidates)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fmap = {ex.submit(_run, p): i for i, p in enumerate(candidates)}
        for fut in as_completed(fmap):
            i = fmap[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                print(col(f"  WARN eval {i}: {e}", Y))
                results[i] = {
                    "params": candidates[i], "wr": 0, "trades": 0, "pnl": 0,
                    "coins": 0, "total": 0, "score": 0, "avg_hold": 0,
                }
    return [r for r in results if r is not None]

def maintain_hof(hof, new_results, max_size=30):
    hof.extend(new_results)
    hof.sort(key=lambda r: r["score"], reverse=True)
    seen, out = [], []
    for r in hof:
        k = round(r["score"], 2)
        if k not in seen or len(out) < 8:
            seen.append(k); out.append(r)
        if len(out) >= max_size: break
    hof[:] = out

def diff_str(a, b):
    d = [(k, a.get(k), b.get(k)) for k in SPACE if a.get(k) != b.get(k)]
    return ", ".join(f"{k}:{av}→{col(str(bv), B)}" for k, av, bv in d) or "(none)"


# ── Sensitivity Scan ──────────────────────────────────────────────────────────

def sensitivity_scan(base_result, workers, label="S"):
    base_params = base_result["params"]
    base_score  = base_result["score"]
    base_trades = base_result["trades"]

    candidates, meta = [], []
    for k in SPACE:
        lo, hi, _, _ = SPACE[k]
        if lo == hi: continue
        v = base_params.get(k, lo)
        for direction in (+1, -1):
            nv = _step_val(k, v, direction)
            if nv != v:
                p = deepcopy(base_params); p[k] = nv
                candidates.append(p); meta.append((k, direction))

    print(f"  [{label}] {len(candidates)} evals across {len(SPACE)} params …")
    t0 = time.time()
    results = eval_batch(candidates, workers)
    print(f"  [{label}] Done in {time.time()-t0:.0f}s")

    param_data = {}
    for (k, direction), r in zip(meta, results):
        param_data.setdefault(k, []).append((direction, r["score"] - base_score, r))

    sensitivity = []
    for k, data in param_data.items():
        max_impact = max(abs(d) for _, d, _ in data)
        grad       = sum(d * dr for dr, d, _ in data)
        grad_dir   = +1 if grad > 1e-6 else (-1 if grad < -1e-6 else 0)
        best_step  = max(data, key=lambda x: x[1])
        best_r     = best_step[2] if best_step[1] > 0 else None
        trade_imp  = max(abs(r["trades"] - base_trades) for _, _, r in data)
        sensitivity.append((k, max_impact, grad_dir, best_r, trade_imp))

    sensitivity.sort(key=lambda x: x[1], reverse=True)
    print(f"\n  [{label}] Top-15 by score impact:")
    for k, impact, gdir, _, t_imp in sensitivity[:15]:
        arrow = col("↑", G) if gdir > 0 else (col("↓", Y) if gdir < 0 else col("~", D))
        print(f"    {k:<42s} Δsc={impact:.4f} Δtr={t_imp:+.0f} {arrow}")

    return sensitivity


# ── Balanced Coordinate Descent ───────────────────────────────────────────────

def coordinate_descent(best, sensitivity, workers, top_n=20, label="CD"):
    """Full-range sweep on top-N score-sensitive params."""
    current  = deepcopy(best)
    improved = 0

    # Prioritize: sensitivity order, but ensure TP/SL params are included
    sens_keys = [entry[0] for entry in sensitivity[:top_n]]
    tpsl_keys = [k for k in _EXIT_PARAMS if k in SPACE and k not in sens_keys]
    ordered = sens_keys + tpsl_keys[:6]  # Ensure exit params get swept

    for rank, k in enumerate(ordered):
        if k not in SPACE: continue
        if SPACE[k][0] == SPACE[k][1]: continue

        vals = _all_values(k)
        # For large ranges, subsample to keep evals manageable
        if len(vals) > 30:
            step = max(1, len(vals) // 25)
            vals = vals[::step]
            # Always include current value
            cv = current["params"].get(k)
            if cv not in vals:
                vals.append(cv)
                vals.sort()

        candidates = [deepcopy(current["params"]) for _ in vals]
        for p, v in zip(candidates, vals): p[k] = v

        t0 = time.time()
        all_results = eval_batch(candidates, workers)
        elapsed = time.time() - t0
        best_r = max(all_results, key=lambda r: r["score"], default=None)

        if best_r and best_r["score"] > current["score"]:
            old_v  = current["params"].get(k)
            delta  = best_r["score"] - current["score"]
            current = best_r; improved += 1
            print(col(f"  {label} [{rank+1:2d}] {k:<38s} {old_v}→{current['params'][k]}  "
                      f"Δ={delta:+.4f}  {_fmt(current)}  ({elapsed:.0f}s)", G))
        else:
            print(f"  {label} [{rank+1:2d}] {k:<38s} no gain  ({elapsed:.0f}s)")

    print(col(f"\n  {label} done: {improved}/{len(ordered)}  {_fmt(current)}", C))
    return current


# ── PnL-Focused Sweep ────────────────────────────────────────────────────────

def pnl_sweep(best, workers, label="PnL"):
    """Sweep exit and TP/SL params to maximize PnL while maintaining WR/trades."""
    current = deepcopy(best)
    improved = 0

    pnl_params = _EXIT_PARAMS + ["kalman_gamma", "overextension_k", "spike_atr_multiplier"]

    for rank, k in enumerate(pnl_params):
        if k not in SPACE: continue
        lo, hi, _, _ = SPACE[k]
        if lo == hi: continue

        vals = _all_values(k)
        if len(vals) > 30:
            step = max(1, len(vals) // 25)
            vals = vals[::step]

        candidates = [deepcopy(current["params"]) for _ in vals]
        for p, v in zip(candidates, vals): p[k] = v

        t0 = time.time()
        all_results = eval_batch(candidates, workers)
        elapsed = time.time() - t0

        # Accept if score improves (scorer already balances WR/trades/PnL)
        best_r = max(all_results, key=lambda r: r["score"], default=None)

        if best_r and best_r["score"] > current["score"]:
            old_v = current["params"].get(k)
            current = best_r; improved += 1
            print(col(f"  {label} [{rank+1:2d}] {k:<38s} {old_v}→{current['params'][k]}  "
                      f"{_fmt(current)}  ({elapsed:.0f}s)", G))

    print(col(f"\n  {label} done: {improved} improved  {_fmt(current)}", C))
    return current


# ── Trade Count Recovery ──────────────────────────────────────────────────────

def trade_recovery(best, workers, label="TR"):
    """Sweep entry gates to push trades toward 280."""
    current = deepcopy(best)
    improved = 0

    for rank, k in enumerate(_ENTRY_GATE_PARAMS):
        if k not in SPACE: continue
        lo, hi, _, _ = SPACE[k]
        if lo == hi: continue

        vals = _all_values(k)
        candidates = [deepcopy(current["params"]) for _ in vals]
        for p, v in zip(candidates, vals): p[k] = v

        t0 = time.time()
        all_results = eval_batch(candidates, workers)
        elapsed = time.time() - t0

        best_r = max(all_results, key=lambda r: r["score"], default=None)

        if best_r and best_r["score"] > current["score"]:
            old_v = current["params"].get(k)
            current = best_r; improved += 1
            print(col(f"  {label} [{rank+1:2d}] {k:<38s} {old_v}→{current['params'][k]}  "
                      f"{_fmt(current)}  ({elapsed:.0f}s)", G))

    print(col(f"\n  {label} done: {improved} improved  {_fmt(current)}", C))
    return current


# ── Gradient Climb ────────────────────────────────────────────────────────────

def gradient_climb(best, hof, sensitivity, workers, max_iters=80):
    """Momentum-based gradient climb."""
    current     = deepcopy(best)
    hof_local   = list(hof)
    active      = [k for k, *_ in sensitivity if k in SPACE and SPACE[k][0] != SPACE[k][1]]
    probe_n     = min(10, len(active))
    beta        = 0.65
    velocity    = {k: 0.0 for k in active}
    no_improve  = 0
    patience    = 10

    for iteration in range(max_iters):
        offset = (iteration * probe_n) % max(1, len(active))
        subset = (active[offset:] + active[:offset])[:probe_n]

        cands, meta = [], []
        for k in subset:
            v = current["params"].get(k, SPACE[k][0])
            for d in (+1, -1):
                nv = _step_val(k, v, d)
                if nv != v:
                    p = deepcopy(current["params"]); p[k] = nv
                    cands.append(p); meta.append((k, d))

        if not cands: break
        t0 = time.time()
        probe_r = eval_batch(cands, workers)

        grad = {}
        for (k, d), r in zip(meta, probe_r):
            delta = r["score"] - current["score"]
            grad[k] = grad.get(k, 0.0) + delta * d

        guided = deepcopy(current["params"])
        moved  = []
        for k in subset:
            g = grad.get(k, 0.0)
            velocity[k] = beta * velocity[k] + (1 - beta) * g
            v_curr = current["params"].get(k, SPACE[k][0])
            nv = _step_val(k, v_curr, +1 if velocity[k] > 1e-5 else (-1 if velocity[k] < -1e-5 else 0))
            if nv != v_curr:
                guided[k] = nv; moved.append(k)

        elapsed = time.time() - t0
        if not moved: no_improve += 1; continue

        gr = _run(guided)
        maintain_hof(hof_local, [gr])

        if gr["score"] > current["score"]:
            delta = gr["score"] - current["score"]
            current = gr; no_improve = 0
            print(col(f"  GC {iteration+1:3d}: Δ={delta:+.4f}  {_fmt(current)}  ({elapsed:.0f}s)", G))
        else:
            no_improve += 1
            if iteration % 10 == 0:
                print(f"  GC {iteration+1:3d}: {_fmt(gr)}  no_imp={no_improve}  ({elapsed:.0f}s)")
            if no_improve >= patience:
                pool = [r for r in hof_local[:6] if r["score"] >= current["score"] * 0.97]
                if pool:
                    current = random.choice(pool)
                    velocity = {k: 0.0 for k in active}
                    no_improve = 0
                    print(col(f"  GC restart → HOF: {_fmt(current)}", Y))

        # Check if targets met
        if (57 <= current["wr"] <= 63 and
            250 <= current["trades"] <= 310 and
            current["pnl"] >= TARGET_PNL):
            print(col(f"\n  ✓ Targets met at iter {iteration+1}!", B + G)); break

    maintain_hof(hof, hof_local)
    return current


# ── Fine-Tune HOF ─────────────────────────────────────────────────────────────

def fine_tune(hof, workers, iters_per_base=25):
    overall_best = hof[0]
    non_pinned   = [k for k in SPACE if SPACE[k][0] != SPACE[k][1]]

    for base_idx, base_r in enumerate(hof[:5]):
        print(f"\n  FT base #{base_idx+1}: {_fmt(base_r)}")
        current = deepcopy(base_r); no_imp = 0

        for it in range(iters_per_base):
            n_p  = random.choices([1, 2, 3], weights=[0.5, 0.35, 0.15])[0]
            keys = random.sample(non_pinned, min(n_p, len(non_pinned)))
            cands = []
            for k in keys:
                v = current["params"].get(k)
                if v is None: continue
                for d in (+1, -1):
                    nv = _step_val(k, v, d)
                    if nv != v:
                        p = deepcopy(current["params"]); p[k] = nv; cands.append(p)

            if not cands: continue
            results = eval_batch(cands, workers)
            best_r  = max(results, key=lambda r: r["score"])

            if best_r["score"] > current["score"]:
                delta = best_r["score"] - current["score"]
                current = best_r; no_imp = 0
                maintain_hof(hof, [current])
                if current["score"] > overall_best["score"]: overall_best = current
                print(col(f"    ★ FT[b{base_idx+1} i{it+1}]: Δ={delta:+.4f}  {_fmt(current)}", B+G))
            else:
                no_imp += 1
                if no_imp >= 12: break

    return overall_best


# ── MAIN ──────────────────────────────────────────────────────────────────────

def run(workers=8, seed=42, purge_every=_PURGE_EVERY):
    global _PURGE_EVERY, _eval_counter, _total_purged
    _PURGE_EVERY = purge_every; _eval_counter = 0; _total_purged = 0
    random.seed(seed)

    recs      = list_recordings()
    completed = [r for r in recs if r.get("status") == "completed"]
    if not completed:
        print(col("No completed recordings found!", R)); sys.exit(1)

    print(col("═" * 78, C))
    print(col("  Auto-Tuner v14 — 60% WR / 280 Trades / +1.6 SOL Edition", B + C))
    print(col(f"  {len(completed)} coins | {workers} workers", C))
    print(col(f"  Target: WR≈{TARGET_WR}%  trades≈{TARGET_TRADES}  PnL≥+{TARGET_PNL} SOL", G))
    print(col("  No lookahead: 4-state intra-candle model, 1-bar delay execution", D))
    print(col("═" * 78, C))

    _purge_db(reason="startup")

    start = deepcopy(START)

    # ── Phase 0: Baseline ────────────────────────────────────────────────────
    print(f"\n{B}Phase 0 — Baseline{X}")
    t0   = time.time()
    best = _run(start)
    print(f"  Baseline: {_fmt(best)}  ({time.time()-t0:.0f}s)")
    hof  = [best]

    def upd(r, label):
        nonlocal best
        if r["score"] > best["score"]:
            best = r
            print(col(f"\n  ★ NEW BEST [{label}]  {_fmt(best)}", B + G))
            print(col(f"    Δ vs start: {diff_str(start, best['params'])}", Y))
            maintain_hof(hof, [best]); return True
        return False

    # ── Phase T: Targeted configurations ──────────────────────────────────────
    print(f"\n{B}Phase T — Targeted configurations{X}")

    _TARGET_CONFIGS = [
        # Config 1: Looser entry gates for more trades + trailing SL
        {"confidence_high": 0.65, "entry_confidence_high": 0.65,
         "confidence_low": 0.15, "entry_confidence_low": 0.15,
         "signal_strong": 3.0, "min_trend_bars": 2,
         "stoploss_pct": 20, "takeprofit_pct": 30},
        # Config 2: Medium gates + tight trailing stop
        {"confidence_high": 0.70, "entry_confidence_high": 0.70,
         "signal_strong": 3.5, "stability_bars": 2,
         "stoploss_pct": 15, "takeprofit_pct": 25},
        # Config 3: Very loose entry + aggressive TP
        {"confidence_high": 0.55, "entry_confidence_high": 0.55,
         "signal_strong": 2.5, "min_trend_bars": 1,
         "stoploss_pct": 30, "takeprofit_pct": 50},
        # Config 4: Balanced with confidence-scaled TP/SL
        {"confidence_high": 0.68, "entry_confidence_high": 0.68,
         "signal_strong": 3.0, "stoploss_pct": 20,
         "takeprofit_pct_low": 20, "takeprofit_pct_high": 40,
         "stoploss_pct_low": 25, "stoploss_pct_high": 15},
        # Config 5: Hard stop + TP
        {"stoploss_pct": -15, "takeprofit_pct": 35,
         "confidence_high": 0.72, "signal_strong": 3.5},
        # Config 6: Wide trailing + no TP (let winners run)
        {"stoploss_pct": 35, "takeprofit_pct": 0,
         "confidence_high": 0.60, "entry_confidence_high": 0.60,
         "signal_strong": 2.8, "min_trend_bars": 1},
        # Config 7: Default-ish but with TP/SL
        {"stoploss_pct": 25, "takeprofit_pct": 40,
         "confidence_high": 0.75, "signal_strong": 3.5},
        # Config 8: Lower confidence weights, more trades
        {"confidence_w1": 0.20, "confidence_w2": 0.30,
         "confidence_high": 0.58, "entry_confidence_high": 0.58,
         "signal_strong": 2.5, "stoploss_pct": 20, "takeprofit_pct": 35},
    ]

    t_cands = []
    for cfg in _TARGET_CONFIGS:
        p = deepcopy(start)
        for k, v in cfg.items():
            if k in SPACE:
                lo, hi, _, is_int = SPACE[k]
                p[k] = max(lo, min(hi, int(round(v)) if is_int else v))
            else:
                p[k] = v
        t_cands.append(p)

    print(f"  Evaluating {len(t_cands)} targeted configs …")
    t0 = time.time()
    t_results = eval_batch(t_cands, workers)
    print(f"  Done in {time.time()-t0:.0f}s")
    maintain_hof(hof, t_results)
    for i, r in enumerate(t_results):
        marker = col("★", G) if r["score"] > best["score"] else col("·", D)
        print(f"  {marker} Config {i+1}: {_fmt(r)}")
        upd(r, f"T{i+1}")

    # ── Phase R: Random population search ─────────────────────────────────────
    print(f"\n{B}Phase R — Random population search (300 configs){X}")

    def _random_params(seed_p: dict) -> dict:
        p = deepcopy(seed_p)
        for k in SPACE:
            lo, hi, _, is_int = SPACE[k]
            if lo == hi: continue
            sampled = random.uniform(lo, hi)
            p[k] = int(round(sampled)) if is_int else round(sampled, 6)
        return p

    r_cands = [_random_params(start) for _ in range(300)]
    print(f"  Evaluating 300 random configs …")
    t0 = time.time()
    r_results = eval_batch(r_cands, workers)
    print(f"  Done in {time.time()-t0:.0f}s")
    maintain_hof(hof, r_results)

    r_results.sort(key=lambda r: r["score"], reverse=True)
    print(f"\n  Top-10 from random search:")
    for r in r_results[:10]:
        marker = col("★", G) if r["score"] > best["score"] else "·"
        print(f"  {marker} {_fmt(r)}")
        upd(r, "R")

    print(col(f"\n  After T+R: {_fmt(best)}", C))

    # ── Phase 1: Sensitivity scan ─────────────────────────────────────────────
    print(f"\n{B}Phase 1 — Sensitivity scan{X}")
    sensitivity = sensitivity_scan(best, workers, "S1")
    for k, impact, gdir, best_step_r, t_imp in sensitivity:
        if best_step_r and best_step_r["score"] > best["score"]:
            upd(best_step_r, f"S1-{k}")

    # ═══════════════════════════════════════════════════════════════════════════
    # OUTER LOOP: repeat phases A→E until targets met, max 4 rounds
    # ═══════════════════════════════════════════════════════════════════════════
    for outer in range(4):
        print(col(f"\n{'═'*78}", C))
        print(col(f"  OUTER LOOP {outer+1}/4  —  WR={best['wr']:.1f}%  trades={best['trades']}  pnl={best['pnl']:+.4f}", B+C))
        print(col(f"{'═'*78}", C))

        targets_met = (57 <= best["wr"] <= 63 and
                       250 <= best["trades"] <= 310 and
                       best["pnl"] >= TARGET_PNL)
        if targets_met:
            print(col("  ✓ All targets met — skipping further loops.", G)); break

        # ── Phase A: Balanced CD ─────────────────────────────────────────────
        print(f"\n{B}Phase A — Balanced coordinate descent{X}")
        cd = coordinate_descent(best, sensitivity, workers, top_n=20, label=f"CD-{outer+1}")
        upd(cd, f"CD-{outer+1}"); maintain_hof(hof, [cd])

        # ── Phase B: PnL-focused sweep ───────────────────────────────────────
        print(f"\n{B}Phase B — PnL-focused sweep (TP/SL + exits){X}")
        ps = pnl_sweep(best, workers, label=f"PnL-{outer+1}")
        upd(ps, f"PnL-{outer+1}"); maintain_hof(hof, [ps])

        # ── Phase C: Trade count recovery ────────────────────────────────────
        print(f"\n{B}Phase C — Trade count recovery{X}")
        tr = trade_recovery(best, workers, label=f"TR-{outer+1}")
        upd(tr, f"TR-{outer+1}"); maintain_hof(hof, [tr])

        # ── Phase D: Gradient climb ──────────────────────────────────────────
        print(f"\n{B}Phase D — Gradient hill-climbing{X}")
        gc = gradient_climb(best, hof, sensitivity, workers, max_iters=80)
        upd(gc, f"GC-{outer+1}")

        # ── Phase E: Fine-tune ───────────────────────────────────────────────
        print(f"\n{B}Phase E — Fine-tune HOF{X}")
        ft = fine_tune(hof, workers, iters_per_base=25)
        upd(ft, f"FT-{outer+1}")

        # Refresh sensitivity for next loop
        sensitivity = sensitivity_scan(best, workers, f"Sloop{outer+1}")
        for k, impact, gdir, best_step_r, t_imp in sensitivity:
            if best_step_r and best_step_r["score"] > best["score"]:
                upd(best_step_r, f"Sloop{outer+1}-{k}")

    # ── Final report ──────────────────────────────────────────────────────────
    print(col("\n" + "═" * 78, C))
    print(col("  FINAL RESULTS", B + C))
    print(col("═" * 78, C))
    if _prev:
        pv = {"wr": _prev.get("win_rate",0), "trades": _prev.get("total_trades",0),
              "pnl": _prev.get("total_pnl",0), "coins":0, "total":len(completed),
              "score": _prev.get("score",0), "avg_hold":0}
        print(f"  Previous : {_fmt(pv)}")
    print(f"  Best     : {_fmt(best)}")

    wr_ok  = 57 <= best["wr"] <= 63
    tr_ok  = 250 <= best["trades"] <= 310
    pnl_ok = best["pnl"] >= TARGET_PNL
    all_ok = wr_ok and tr_ok and pnl_ok
    if all_ok:
        print(col("  ✓ ALL TARGETS MET!", B + G))
    else:
        gaps = []
        if not wr_ok:  gaps.append(f"WR={best['wr']:.1f}% (target 57-63%)")
        if not tr_ok:  gaps.append(f"trades={best['trades']} (target 250-310)")
        if not pnl_ok: gaps.append(f"PnL={best['pnl']:+.4f} (target ≥+{TARGET_PNL})")
        print(col(f"  ✗ Gaps: {', '.join(gaps)}", Y))

    changed = {k: v for k, v in best["params"].items()
               if k in _BASELINE and abs(float(best["params"].get(k,0)) - float(_BASELINE.get(k,0))) > 1e-9}
    print(f"\n  {B}Changed vs baseline ({len(changed)} params):{X}")
    for k, v in sorted(changed.items()):
        ds  = col(" ↑", G) if v > (_BASELINE.get(k,0)) else col(" ↓", Y)
        print(f"    {k:<42s}  {_BASELINE.get(k,'?')} → {col(str(v), B+G)}{ds}")

    out = {"params": best["params"], "win_rate": best["wr"],
           "total_trades": best["trades"], "total_pnl": best["pnl"], "score": best["score"]}
    with open(_best_json, "w") as f:
        json.dump(out, f, indent=2)
    print(col(f"\n  Saved → {_best_json}", C))

    print(f"\n  {B}Top 5 Hall of Fame:{X}")
    for i, r in enumerate(hof[:5]):
        print(f"  #{i+1}  {_fmt(r)}")

    _purge_db(reason="end of run")
    print(col(f"\n  DB stats: {_eval_counter} evals | "
              f"{_eval_counter//_PURGE_EVERY} purges | "
              f"{_total_purged/1_048_576:.1f} MB freed | "
              f"cache hits: {_cache_hits} ({100*_cache_hits/max(1,_eval_counter+_cache_hits):.0f}%)", D))
    return best


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Auto-Tuner v14 — 60% WR / 280 Trades / +1.6 SOL")
    p.add_argument("--workers",     type=int, default=8)
    p.add_argument("--seed",        type=int, default=42)
    p.add_argument("--purge-every", type=int, default=50)
    a = p.parse_args()
    run(workers=a.workers, seed=a.seed, purge_every=a.purge_every)