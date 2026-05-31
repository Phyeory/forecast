#!/usr/bin/env python3
"""
Auto-Tuner v12 — 70% Win Rate Edition.

Target: WR≥70%  trades≥100  PnL>0  across all completed recordings.

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
SCORER v12
═══════════════════════════════════════════════════════════════════════
  WR weight:    65%  — steep ramp 68–75%, cliff below 68%
  Trade weight: 20%  — hard binary floor at 100 (×0.05 if below)
  PnL weight:   15%  — positive PnL guard, rewards efficiency

  Joint bonus: WR≥70 AND trades≥100 → ×1.25

═══════════════════════════════════════════════════════════════════════
PHASE STRUCTURE v12
═══════════════════════════════════════════════════════════════════════
  0. Baseline evaluation
  1. Sensitivity scan (find highest-impact params)
  A. WR-first CD  — no trade-floor penalty, push to WR≥70%
  B. Volume recovery — sweep entry gates with WR≥68% floor
  C. Balanced CD  — polish with full scorer
  D. Gradient climb
  E. Fine-tune HOF
  (Repeat A→E up to 3 outer loops until target met)
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
    "exhaustion_bars_limit": 1, "delta_threshold": 0.3, "kalman_gamma": 0.1,
    "min_trend_bars": 3, "reversal_confirm_bars": 2, "chop_atr_pct": 0.3,
    "chop_spread_pct": 0.05, "reversal_exit_confirm_bars": 0,
    "s_effective_threshold": 0.35, "exhaustion_persist_bars": 4, "regime_lookback": 6,
    "persistence_threshold": 2, "momentum_mean_threshold": 0.0, "ema_min_spread_pct": 0.02,
    "confidence_high": 0.79, "confidence_low": 0.23,
    "confidence_w1": 0.3, "confidence_w2": 0.25, "confidence_w3": 0.25, "confidence_w4": 0.2,
    "atr_floor_k": 0.6, "ema_cross_persist_bars": 2, "exhaustion_s_decay_bars": 1,
    "exhaustion_stall_bars": 3, "exhaustion_stall_atr_pct": 3.0, "local_range_bars": 80,
    "local_range_threshold_pct": 0.8, "sign_flip_threshold": 1, "stability_bars": 3,
    "spike_atr_multiplier": 1.2, "spike_lookback_bars": 7, "body_baseline_bars": 30,
    "overextension_k": 0.17, "momentum_peak_bars": 1, "consolidation_range_pct": 5.0,
    "confidence_very_high": 0.81, "ema_macro_period": 7, "stoploss_pct": 0.0,
}

_best_json = os.path.join(os.path.dirname(__file__), "best_params.json")
if os.path.exists(_best_json):
    with open(_best_json) as f:
        _prev = json.load(f)
    _prev_wr = _prev.get("win_rate", 0)
    if _prev_wr >= 70.0:
        START = _prev.get("params", _BASELINE)
        print(col(f"Loaded previous best: WR={_prev_wr:.1f}%  "
                  f"trades={_prev.get('total_trades',0)}  score={_prev.get('score',0):.4f}", C))
    else:
        START = deepcopy(_BASELINE)
        _prev = {}
        print(col(f"Discarded previous best (WR={_prev_wr:.1f}% < 70%) — starting fresh.", Y))
else:
    _prev = {}
    START = deepcopy(_BASELINE)


# ── Search space ──────────────────────────────────────────────────────────────
# (lo, hi, step, is_int)
SPACE = {
    # ── A. ENTRY GATES ───────────────────────────────────────────────────────
    "confidence_high":          (0.50, 0.97, 0.01, False),
    "confidence_low":           (0.10, 0.55, 0.01, False),
    "confidence_very_high":     (0.55, 0.98, 0.01, False),
    "signal_strong":            (1.00, 6.00, 0.10, False),
    "signal_weak":              (0.50, 3.00, 0.10, False),
    "signal_noise":             (0.50, 2.50, 0.10, False),
    "s_effective_threshold":    (0.01, 0.80, 0.05, False),
    "min_trend_bars":           (1, 4, 1, True),
    "stability_bars":           (1, 4, 1, True),
    "ema_cross_persist_bars":   (1, 4, 1, True),
    "persistence_threshold":    (1, 6, 1, True),
    "warmup":                   (15, 60, 5, True),

    # ── B. EXIT SPEED (keep fast exits pinned for WR) ────────────────────────
    "reversal_confirm_bars":      (1, 3, 1, True),
    "reversal_exit_confirm_bars": (0, 1, 1, True),
    "exhaustion_bars_limit":      (1, 3, 1, True),
    "exhaustion_s_decay_bars":    (1, 3, 1, True),
    "momentum_peak_bars":         (1, 3, 1, True),
    "exhaustion_persist_bars":    (1, 4, 1, True),
    "exhaustion_stall_bars":      (1, 3, 1, True),
    "exhaustion_stall_atr_pct":   (0.5, 5.0, 0.5, False),

    # ── C. CHOP / NOISE FILTERS ───────────────────────────────────────────────
    "consolidation_range_pct":   (0.5, 8.0, 0.5, False),
    "local_range_threshold_pct": (0.05, 0.95, 0.05, False),
    "local_range_bars":          (5, 80, 5, True),
    "sign_flip_threshold":       (1, 10, 1, True),
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
}

# Entry gate params: loosen → more trades, tighten → higher WR
_ENTRY_GATE_PARAMS = [
    "confidence_high", "confidence_low", "confidence_very_high",
    "signal_strong", "signal_weak", "signal_noise", "s_effective_threshold",
    "min_trend_bars", "stability_bars", "ema_cross_persist_bars",
    "persistence_threshold", "warmup",
]

# Filter params: tighten → higher WR, smaller trade cost than entry gates
_FILTER_PARAMS = [
    "consolidation_range_pct", "local_range_threshold_pct", "local_range_bars",
    "sign_flip_threshold", "chop_atr_pct", "chop_spread_pct",
    "overextension_k", "spike_atr_multiplier", "spike_lookback_bars", "body_baseline_bars",
    "exhaustion_persist_bars", "exhaustion_stall_bars", "exhaustion_stall_atr_pct",
]

# All params useful for pushing WR upward
_WR_PARAMS = _FILTER_PARAMS + [
    "confidence_high", "confidence_very_high", "signal_strong",
    "s_effective_threshold", "kalman_gamma",
]


# ── Scorers ───────────────────────────────────────────────────────────────────

def _score(wr: float, trades: int, pnl: float,
           coins_w: int, total: int, max_dd: float,
           avg_hold_bars: float = 0.0) -> float:
    """
    v13 scorer — 70% WR is a HARD target.

    Key philosophy:
      • WR < 70%: score is near-ZERO regardless of trades/PnL. The optimizer
        cannot escape the 70% WR requirement by compensating with more trades.
      • WR ≥ 70%: score rises steeply. Bonus multipliers stack up.
      • Trades: soft floor at 80 (penalty, not cliff). We want the optimizer
        to find 70%+ WR first, then recover volume. Dropping from 120→80
        trades is acceptable if WR goes from 55%→72%.
      • Joint bonus: WR≥70 AND trades≥100 → ×2.0 (massive reward).
    """
    # ── WR base score (0→1.5 range) ───────────────────────────────────────────
    if wr >= 80:
        wr_s = 1.50 + min(0.50, (wr - 80) / 10)  # 1.50→2.00
    elif wr >= 75:
        wr_s = 1.00 + (wr - 75) / 5 * 0.50       # 1.00→1.50
    elif wr >= 70:
        wr_s = 0.30 + (wr - 70) / 5 * 0.70       # 0.30→1.00  ← steep
    elif wr >= 65:
        wr_s = 0.02 + (wr - 65) / 5 * 0.28       # 0.02→0.30
    elif wr >= 55:
        wr_s = 0.001 + (wr - 55) / 10 * 0.019    # 0.001→0.02
    else:
        wr_s = max(0.0, (wr / 55) * 0.001)        # essentially 0

    # ── Trade count score (soft floor at 80) ──────────────────────────────────
    if trades >= 120:
        trade_s = 1.0 + min(0.5, (trades - 120) / 60)
    elif trades >= 100:
        trade_s = 0.70 + (trades - 100) / 20 * 0.30  # 0.70→1.00
    elif trades >= 80:
        trade_s = 0.40 + (trades - 80) / 20 * 0.30   # 0.40→0.70
    elif trades >= 50:
        trade_s = 0.10 + (trades - 50) / 30 * 0.30   # 0.10→0.40
    else:
        trade_s = max(0.0, trades / 50 * 0.10)

    # ── PnL score ─────────────────────────────────────────────────────────────
    ppt = pnl / max(1, trades)
    if ppt <= 0:
        pnl_s = max(-1.0, ppt / 0.001)
    elif ppt < 0.001:
        pnl_s = ppt / 0.001 * 0.50
    elif ppt < 0.003:
        pnl_s = 0.50 + (ppt - 0.001) / 0.002 * 0.50
    else:
        pnl_s = 1.0 + min(1.0, (ppt - 0.003) / 0.003)

    tpc = trades / max(1, total)

    # Weighted base: WR dominant (75%), trades (15%), pnl (10%)
    base = 0.75 * wr_s + 0.15 * trade_s + 0.10 * pnl_s

    # ── Multiplicative penalties ───────────────────────────────────────────────

    # HARD WR cliff: below 70% the entire score crumbles exponentially
    if wr < 70:
        # At 69%: factor ≈ 0.026, at 65%: ≈ 0.001, at 55%: ≈ 0
        cliff = max(0.0, (wr - 50) / 20) ** 4
        base *= cliff

    # Soft trade floor: penalty ramps from x1.0 at 100 → x0.25 at 50
    if trades < 100:
        base *= max(0.25, (trades / 100) ** 0.7)

    # Coin coverage
    if tpc < 0.30:
        base *= max(0.05, tpc / 0.30)
    elif tpc < 0.50:
        base *= max(0.50, tpc / 0.50)

    # Negative PnL kills score
    if pnl < 0:
        base *= max(0.01, 1.0 + pnl / (abs(pnl) + 0.05))

    # Churn penalty
    if trades >= 80 and 0 < ppt < 0.0003:
        base *= max(0.20, ppt / 0.0003)

    # Drawdown guard
    if max_dd > 5.0:
        base *= max(0.01, 1.0 - (max_dd - 5.0) / 12.0)

    # Hold duration penalty (pump-chasing strategies should exit quickly)
    if avg_hold_bars > 5:
        if avg_hold_bars > 15:
            base *= max(0.05, 1.0 - (avg_hold_bars - 15) * 0.06)
        elif avg_hold_bars > 8:
            base *= max(0.40, 1.0 - (avg_hold_bars - 8) * 0.08)
        else:
            base *= max(0.70, 1.0 - (avg_hold_bars - 5) * 0.10)

    # ── Joint bonus: MASSIVE reward for hitting both targets ───────────────────
    if wr >= 70 and trades >= 100 and pnl > 0:
        base *= 2.00   # ×2 — this is the sweet spot
    if wr >= 70 and trades >= 80 and pnl > 0:
        base *= 1.30   # partial credit
    if wr >= 75 and trades >= 100:
        base *= 1.40
    if wr >= 75 and trades >= 80:
        base *= 1.15

    return round(base, 6)


def _score_wr(wr: float, trades: int, pnl: float, total: int) -> float:
    """
    WR-only scorer for Phase A — no trade floor penalty at all.
    Purely rewards higher WR. PnL must be non-negative.
    """
    if pnl <= 0 and trades > 0:
        return max(0.0, wr / 100 * 0.01)  # tiny non-zero to break ties
    if wr >= 80:
        return 1.0 + min(1.0, (wr - 80) / 10)
    elif wr >= 75:
        return 0.60 + (wr - 75) / 5 * 0.40
    elif wr >= 70:
        return 0.20 + (wr - 70) / 5 * 0.40
    elif wr >= 65:
        return 0.05 + (wr - 65) / 5 * 0.15
    elif wr >= 55:
        return 0.005 + (wr - 55) / 10 * 0.045
    else:
        return max(0.0, wr / 55 * 0.005)


def _score_vol(wr: float, trades: int, total: int) -> float:
    """Pure trade-count scorer for volume recovery. WR floor 68%."""
    if wr < 68: return 0.0
    return min(2.0, trades / max(1, max(total, 80)))


# ── Run a single parameter set ────────────────────────────────────────────────
def _run(params: dict) -> dict:
    cached = _cache_get(params)
    if cached is not None: return cached

    res = run_backtest_batch(
        engine_params=params,
        buy_size_sol=0.1,
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
    wrf = _score_wr(wr, trd, pnl, n_total)
    vs  = _score_vol(wr, trd, n_total)

    _tick_and_maybe_purge()

    result = {
        "params": deepcopy(params),
        "wr": wr, "trades": trd, "pnl": pnl,
        "coins": coins, "total": n_total,
        "score": sc, "wr_score": wrf, "vol_score": vs,
        "avg_hold": avg_hold,
    }
    _cache_put(params, result)
    return result


def _fmt(r: dict) -> str:
    wr_c  = G if r["wr"] >= 70 else (Y if r["wr"] >= 65 else R)
    tr_c  = G if r["trades"] >= 100 else (Y if r["trades"] >= 80 else R)
    pnl_c = G if r["pnl"] > 0 else R
    hold  = f" h={r.get('avg_hold', 0):.1f}b" if r.get("avg_hold") else ""
    wr_s   = col("%.1f%%" % r["wr"],   wr_c)
    tr_s   = col(str(r["trades"]),      tr_c)
    pnl_s  = col("%+.4f" % r["pnl"],   pnl_c)
    sc_s   = col("%.4f"  % r["score"],  C)
    return "WR=%s tr=%s pnl=%s cov=%d/%d%s sc=%s" % (
        wr_s, tr_s, pnl_s, r["coins"], r["total"], hold, sc_s
    )


# ── Helpers ───────────────────────────────────────────────────────────────────
def _clamp(k, v):
    lo, hi, _, is_int = SPACE[k]
    return max(lo, min(hi, int(round(v)) if is_int else v))

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
                    "coins": 0, "total": 0, "score": 0, "wr_score": 0,
                    "vol_score": 0, "avg_hold": 0,
                }
    return [r for r in results if r is not None]

def maintain_hof(hof, new_results, max_size=25):
    hof.extend(new_results)
    hof.sort(key=lambda r: r["score"], reverse=True)
    seen, out = [], []
    for r in hof:
        k = round(r["score"], 2)
        if k not in seen or len(out) < 6:
            seen.append(k); out.append(r)
        if len(out) >= max_size: break
    hof[:] = out

def diff_str(a, b):
    d = [(k, a.get(k), b.get(k)) for k in SPACE if a.get(k) != b.get(k)]
    return ", ".join(f"{k}:{av}→{col(str(bv), B)}" for k, av, bv in d) or "(none)"


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 — Sensitivity Scan
# ─────────────────────────────────────────────────────────────────────────────

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
        gtype = col(" [GATE]", C) if k in _ENTRY_GATE_PARAMS else (
                col(" [FILT]", Y) if k in _FILTER_PARAMS else "")
        print(f"    {k:<42s} Δsc={impact:.4f} Δtr={t_imp:+.0f} {arrow}{gtype}")

    return sensitivity


# ─────────────────────────────────────────────────────────────────────────────
# PHASE A — WR-First Coordinate Descent (no trade-floor penalty)
# ─────────────────────────────────────────────────────────────────────────────

def wr_first_cd(best, workers, label="WRcd"):
    """
    Sweep all WR-improvement params using _score_wr() — no trade floor.
    Goal: push WR to 70%+ even if trades temporarily drop below 100.
    Phase B will recover trade count from this better WR baseline.
    """
    current  = deepcopy(best)
    improved = 0
    ordered  = _WR_PARAMS + [k for k in SPACE if k not in _WR_PARAMS and SPACE[k][0] != SPACE[k][1]]

    print(f"  WR-first CD over {len([k for k in SPACE if SPACE[k][0] != SPACE[k][1]])} params (no trade floor) …")

    for rank, k in enumerate(ordered):
        if k not in SPACE: continue
        lo, hi, _, _ = SPACE[k]
        if lo == hi: continue

        vals       = _all_values(k)
        candidates = [deepcopy(current["params"]) for _ in vals]
        for p, v in zip(candidates, vals): p[k] = v

        t0 = time.time()
        all_results = eval_batch(candidates, workers)
        elapsed = time.time() - t0

        best_r: Optional[dict] = max(
            (r for r in all_results if r["pnl"] > 0),
            key=lambda r: r["wr_score"], default=None
        )

        if best_r and best_r["wr_score"] > current.get("wr_score", 0):
            old_v    = current["params"].get(k)
            wr_delta = best_r["wr"] - current["wr"]
            current  = best_r; improved += 1
            print(col(
                f"  {label} [{rank+1:2d}] {k:<38s} {old_v}→{current['params'][k]}  "
                f"WR{wr_delta:+.1f}%  {_fmt(current)}  ({elapsed:.0f}s)", G
            ))

    print(col(f"\n  {label} done: {improved} improved  WR={current['wr']:.1f}%  {_fmt(current)}", C))
    return current


# ─────────────────────────────────────────────────────────────────────────────
# PHASE B — Volume Recovery (WR floor 68%)
# ─────────────────────────────────────────────────────────────────────────────

def volume_scan(base_result, workers):
    """Sweep entry-gate params for trade count. WR floor 68%."""
    base_params = base_result["params"]
    base_trades = base_result["trades"]

    print(f"\n  Volume scan: {len(_ENTRY_GATE_PARAMS)} entry-gate params, WR floor 68% …")
    all_candidates, all_meta = [], []
    for k in _ENTRY_GATE_PARAMS:
        if k not in SPACE: continue
        if SPACE[k][0] == SPACE[k][1]: continue
        for val in _all_values(k):
            p = deepcopy(base_params); p[k] = val
            all_candidates.append(p); all_meta.append((k, val))

    print(f"  Evaluating {len(all_candidates)} candidates …")
    t0 = time.time()
    results = eval_batch(all_candidates, workers)
    print(f"  Done in {time.time()-t0:.0f}s")

    param_curves = {}
    for (k, val), r in zip(all_meta, results):
        param_curves.setdefault(k, []).append((val, r["trades"], r["wr"], r))

    volume_ranking = []
    print(f"\n  Volume curve (best trades per param, WR≥68%+PnL>0):")
    for k, curve in param_curves.items():
        valid = [(v, t, wr, r) for v, t, wr, r in curve if wr >= 68.0 and r["pnl"] > 0]
        if not valid: continue
        best_v, best_t, best_wr, best_r = max(valid, key=lambda x: x[1])
        gain = best_t - base_trades
        print(f"    {k:<42s} →{best_v}  trades+{gain:+d}  WR={best_wr:.1f}%")
        volume_ranking.append((k, best_v, gain, best_wr, best_r))

    volume_ranking.sort(key=lambda x: x[2], reverse=True)
    return volume_ranking


def multi_param_volume_push(best, volume_ranking, workers, top_n=5):
    """2- and 3-param combinations from top volume levers. WR floor 68%."""
    usable = [(k, bv, g) for k, bv, g, _, _ in volume_ranking[:top_n] if g > 0]
    if len(usable) < 2:
        print(col("  MVP: <2 usable levers, skip", D)); return best

    current = deepcopy(best)
    candidates, combo_meta = [], []
    for r in (2, 3):
        if len(usable) < r: continue
        for combo in combinations(usable, r):
            p = deepcopy(best["params"])
            keys = []
            for k, bv, _ in combo:
                p[k] = bv; keys.append(k)
            candidates.append(p); combo_meta.append(keys)

    print(f"\n  MVP: {len(candidates)} combos from top-{len(usable)} levers …")
    t0 = time.time()
    results = eval_batch(candidates, workers)
    print(f"  Done in {time.time()-t0:.0f}s")

    best_r, best_keys = None, None
    for keys, r in zip(combo_meta, results):
        if r["wr"] < 68.0 or r["pnl"] <= 0: continue
        if best_r is None or r["score"] > best_r["score"]:
            best_r = r; best_keys = keys

    if best_r and (best_r["score"] > current["score"] or
                   (best_r["trades"] > current["trades"] * 1.15 and best_r["wr"] >= 68)):
        print(col(f"  ★ MVP winner ({'+'.join(best_keys)}): {_fmt(best_r)}", B + G))
        return best_r

    print(f"  MVP: no combo beat current  {_fmt(current)}")
    return current


def volume_biased_cd(best, volume_ranking, workers, top_n=12):
    """Sequential volume-biased CD. WR floor 68%, pnl_per_trade guard."""
    current  = deepcopy(best)
    improved = 0

    print(f"\n  VCD: top-{top_n} volume levers, WR≥68%, pnl>0 …")
    for rank, (k, best_val, gain, _, _) in enumerate(volume_ranking[:top_n]):
        if gain <= 0: continue
        old_val = current["params"].get(k)
        if old_val == best_val: continue

        test_p = deepcopy(current["params"]); test_p[k] = best_val
        t0 = time.time(); r = _run(test_p); elapsed = time.time() - t0

        ppt_ok = (r["pnl"] / max(1, r["trades"])) >= 0.0005
        wr_ok  = r["wr"] >= 68.0
        pnl_ok = r["pnl"] > 0

        if wr_ok and pnl_ok and ppt_ok and (r["trades"] > current["trades"] or r["score"] > current["score"]):
            current = r; improved += 1
            print(col(f"  VCD [{rank+1}] {k:<38s} {old_val}→{best_val}  "
                      f"trades+{r['trades']-best['trades']}  {_fmt(current)}  ({elapsed:.0f}s)", G))
        else:
            reason = ("WR<68" if not wr_ok else
                      "PnL≤0" if not pnl_ok else
                      "ppt<0.5ms" if not ppt_ok else "no gain")
            print(f"  VCD [{rank+1}] {k:<38s} rejected ({reason})  ({elapsed:.0f}s)")

    print(col(f"\n  VCD done: {improved} improved  {_fmt(current)}", C))
    return current


# ─────────────────────────────────────────────────────────────────────────────
# PHASE C — Balanced Coordinate Descent
# ─────────────────────────────────────────────────────────────────────────────

def coordinate_descent(best, sensitivity, workers, top_n=15, label="CD"):
    """Standard full-range sweep on top-N score-sensitive params."""
    current  = deepcopy(best)
    improved = 0

    for rank, entry in enumerate(sensitivity[:top_n]):
        k = entry[0]
        if k not in SPACE: continue
        if SPACE[k][0] == SPACE[k][1]: continue

        vals       = _all_values(k)
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

    print(col(f"\n  {label} done: {improved}/{top_n}  {_fmt(current)}", C))
    return current


def wr_recovery_sweep(best, workers):
    """
    When WR < 70%: sweep filter + entry-tightening params.
    Accepts only moves that keep trades ≥ 100 and improve WR.
    """
    current     = deepcopy(best)
    improved    = 0

    print(f"  WR recovery sweep: WR={current['wr']:.1f}%  trade floor=100")

    for k in _WR_PARAMS:
        if k not in SPACE: continue
        if SPACE[k][0] == SPACE[k][1]: continue

        vals       = _all_values(k)
        candidates = [deepcopy(current["params"]) for _ in vals]
        for p, v in zip(candidates, vals): p[k] = v

        t0 = time.time()
        all_results = eval_batch(candidates, workers)
        elapsed = time.time() - t0

        best_r = max(
            (r for r in all_results
             if r["wr"] > current["wr"] and r["trades"] >= 100 and r["pnl"] > 0),
            key=lambda r: r["wr"], default=None
        )

        if best_r:
            old_v    = current["params"].get(k)
            wr_gain  = best_r["wr"] - current["wr"]
            tr_delta = best_r["trades"] - current["trades"]
            current  = best_r; improved += 1
            print(col(f"  WRS [{k:<38s}] {old_v}→{current['params'][k]}  "
                      f"WR+{wr_gain:.1f}%  tr{tr_delta:+d}  {_fmt(current)}  ({elapsed:.0f}s)", G))
        else:
            print(f"  WRS [{k:<38s}] no improvement  ({elapsed:.0f}s)")

    print(col(f"\n  WRS done: {improved} improved  {_fmt(current)}", C))
    return current


# ─────────────────────────────────────────────────────────────────────────────
# PHASE D — Gradient Climb
# ─────────────────────────────────────────────────────────────────────────────

def gradient_climb(best, hof, sensitivity, workers, max_iters=60):
    """Momentum-based gradient climb."""
    current     = deepcopy(best)
    hof_local   = list(hof)
    active      = [k for k, *_ in sensitivity if k in SPACE and SPACE[k][0] != SPACE[k][1]]
    gate_params = [k for k in _ENTRY_GATE_PARAMS if k in SPACE and SPACE[k][0] != SPACE[k][1]]
    probe_n     = min(8, len(active))
    beta        = 0.65
    velocity    = {k: 0.0 for k in active}
    no_improve  = 0
    patience    = 8

    for iteration in range(max_iters):
        use_vol = (iteration % 5 == 4)

        if use_vol:
            subset = random.sample(gate_params, min(probe_n, len(gate_params)))
        else:
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
            delta = (r["vol_score"] - current.get("vol_score", 0)) if use_vol else (r["score"] - current["score"])
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
        tag = col("VOL", Y) if use_vol else col("SCR", C)

        if gr["score"] > current["score"]:
            delta = gr["score"] - current["score"]
            current = gr; no_improve = 0
            print(col(f"  GC[{tag}] {iteration+1:3d}: Δ={delta:+.4f}  {_fmt(current)}  ({elapsed:.0f}s)", G))
        else:
            no_improve += 1
            if iteration % 5 == 0:
                print(f"  GC[{tag}] {iteration+1:3d}: {_fmt(gr)}  no_imp={no_improve}  ({elapsed:.0f}s)")
            if no_improve >= patience:
                pool = [r for r in hof_local[:6] if r["score"] >= current["score"] * 0.97]
                if pool:
                    current = random.choice(pool)
                    velocity = {k: 0.0 for k in active}
                    no_improve = 0
                    print(col(f"  GC restart → HOF: {_fmt(current)}", Y))

        if current["wr"] >= 70 and current["trades"] >= 100 and current["pnl"] > 0:
            print(col(f"\n  ✓ Targets met at iter {iteration+1}!", B + G)); break

    maintain_hof(hof, hof_local)
    return current


# ─────────────────────────────────────────────────────────────────────────────
# PHASE E — Fine-Tune HOF
# ─────────────────────────────────────────────────────────────────────────────

def fine_tune(hof, workers, iters_per_base=20):
    overall_best = hof[0]
    non_pinned   = [k for k in SPACE if SPACE[k][0] != SPACE[k][1]]

    for base_idx, base_r in enumerate(hof[:4]):
        print(f"\n  FT base #{base_idx+1}: {_fmt(base_r)}")
        current = deepcopy(base_r); no_imp = 0

        for it in range(iters_per_base):
            n_p  = random.choices([1, 2, 3], weights=[0.5, 0.35, 0.15])[0]
            keys = random.sample(non_pinned, min(n_p, len(non_pinned)))
            cands = []
            for k in keys:
                v = current["params"].get(k)
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
                if no_imp >= 10: break

    return overall_best


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run(workers=8, seed=42, purge_every=_PURGE_EVERY):
    global _PURGE_EVERY, _eval_counter, _total_purged
    _PURGE_EVERY = purge_every; _eval_counter = 0; _total_purged = 0
    random.seed(seed)

    recs      = list_recordings()
    completed = [r for r in recs if r.get("status") == "completed"]
    if not completed:
        print(col("No completed recordings found!", R)); sys.exit(1)

    target_trades = max(len(completed), 100)

    print(col("═" * 78, C))
    print(col("  Auto-Tuner v12 — 70% Win Rate Edition", B + C))
    print(col(f"  {len(completed)} coins | {workers} workers", C))
    print(col(f"  Target: WR≥70%  trades≥{target_trades}  PnL>0", G))
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

    def upd_wr(r, label):
        """Adopt if WR improved, even if composite score dropped."""
        nonlocal best
        if r["wr"] > best["wr"] and r["pnl"] >= 0:
            best = r
            print(col(f"\n  ★ WR-BEST [{label}]  {_fmt(best)}", B + Y))
            maintain_hof(hof, [best]); return True
        return upd(r, label)

    # ── Phase T: Targeted high-WR configurations ──────────────────────────────
    # These are pre-baked multi-param "jumps" into regions of the SPACE
    # known to produce higher WR. Single-step hill-climbing can't reach them.
    print(f"\n{B}Phase T — Targeted high-WR configurations{X}")

    _TIGHT_CONFIGS = [
        # Config 1: Ultra-high confidence entry gate
        {"confidence_high": 0.93, "confidence_very_high": 0.96,
         "signal_strong": 5.5, "min_trend_bars": 4, "stability_bars": 4,
         "persistence_threshold": 4, "s_effective_threshold": 0.55,
         "exhaustion_persist_bars": 1, "reversal_confirm_bars": 1},
        # Config 2: Strict chop filter + tight range
        {"confidence_high": 0.90, "confidence_very_high": 0.93,
         "signal_strong": 5.0, "chop_atr_pct": 0.8, "chop_spread_pct": 0.4,
         "consolidation_range_pct": 2.0, "local_range_threshold_pct": 0.5,
         "sign_flip_threshold": 4, "min_trend_bars": 4},
        # Config 3: Momentum quality gating
        {"confidence_high": 0.88, "confidence_very_high": 0.91,
         "signal_strong": 5.0, "signal_weak": 2.5, "signal_noise": 1.8,
         "stability_bars": 4, "ema_cross_persist_bars": 3,
         "persistence_threshold": 3, "min_trend_bars": 4},
        # Config 4: Kalman + overextension focus
        {"kalman_gamma": 0.05, "overextension_k": 0.08,
         "spike_atr_multiplier": 3.0, "body_baseline_bars": 50,
         "confidence_high": 0.87, "signal_strong": 5.0,
         "exhaustion_persist_bars": 2, "reversal_confirm_bars": 1},
        # Config 5: WR maximalist — everything tight
        {"confidence_high": 0.95, "confidence_very_high": 0.97,
         "signal_strong": 6.0, "signal_weak": 2.0,
         "min_trend_bars": 4, "stability_bars": 4,
         "persistence_threshold": 5, "s_effective_threshold": 0.65,
         "chop_atr_pct": 1.0, "consolidation_range_pct": 1.5,
         "exhaustion_persist_bars": 1, "reversal_confirm_bars": 1,
         "warmup": 40},
        # Config 6: Regime quality + fast exit
        {"regime_lookback": 10, "persistence_threshold": 4,
         "confidence_high": 0.91, "confidence_very_high": 0.94,
         "signal_strong": 5.5, "exhaustion_persist_bars": 1,
         "reversal_confirm_bars": 1, "sign_flip_threshold": 5},
        # Config 7: Local range focus + tight filters
        {"local_range_bars": 30, "local_range_threshold_pct": 0.4,
         "consolidation_range_pct": 2.5, "sign_flip_threshold": 6,
         "confidence_high": 0.88, "signal_strong": 4.5,
         "min_trend_bars": 4, "stability_bars": 4},
        # Config 8: Low kalman noise = cleaner signal
        {"kalman_gamma": 0.03, "signal_strong": 5.0, "signal_noise": 1.8,
         "confidence_high": 0.90, "confidence_very_high": 0.93,
         "exhaustion_persist_bars": 2, "exhaustion_stall_bars": 2,
         "min_trend_bars": 4},
    ]

    t_cands = []
    for cfg in _TIGHT_CONFIGS:
        p = deepcopy(start)
        for k, v in cfg.items():
            if k in SPACE:
                lo, hi, _, is_int = SPACE[k]
                p[k] = max(lo, min(hi, int(round(v)) if is_int else v))
        t_cands.append(p)

    print(f"  Evaluating {len(t_cands)} targeted configs …")
    t0 = time.time()
    t_results = eval_batch(t_cands, workers)
    print(f"  Done in {time.time()-t0:.0f}s")
    maintain_hof(hof, t_results)
    for i, r in enumerate(t_results):
        marker = col("★", G) if r["score"] > best["score"] else col("·", D)
        print(f"  {marker} Config {i+1}: {_fmt(r)}")
        upd_wr(r, f"T{i+1}")

    # ── Phase R: Random population search ─────────────────────────────────────
    # Sample configs from the parameter space biased toward tight-filter
    # (high WR) regions. 200 random points escape local minima.
    print(f"\n{B}Phase R — Random population search (200 configs){X}")

    def _random_params(seed_p: dict, tight_bias: float = 0.6) -> dict:
        """
        Random config: with probability `tight_bias` each WR param is sampled
        from its upper 40% (tighter filter = higher WR gate); otherwise uniform.
        """
        p = deepcopy(seed_p)
        for k in SPACE:
            lo, hi, _, is_int = SPACE[k]
            if lo == hi: continue
            rng = hi - lo
            # For WR-improving params, bias toward tighter values
            if k in _WR_PARAMS and random.random() < tight_bias:
                # WR params: higher value = tighter for most
                sampled = random.uniform(lo + rng * 0.5, hi)
            elif k in _ENTRY_GATE_PARAMS and random.random() < tight_bias:
                # Entry gates: mid-to-high range
                sampled = random.uniform(lo + rng * 0.4, hi)
            else:
                sampled = random.uniform(lo, hi)
            p[k] = int(round(sampled)) if is_int else round(sampled, 6)
        return p

    r_cands = [_random_params(start, tight_bias=0.65) for _ in range(200)]
    print(f"  Evaluating 200 random configs …")
    t0 = time.time()
    r_results = eval_batch(r_cands, workers)
    print(f"  Done in {time.time()-t0:.0f}s")
    maintain_hof(hof, r_results)

    # Sort random results by WR and show top-10
    r_results.sort(key=lambda r: r["wr"], reverse=True)
    print(f"\n  Top-10 by WR from random search:")
    for r in r_results[:10]:
        marker = col("★", G) if r["score"] > best["score"] else "·"
        print(f"  {marker} {_fmt(r)}")
        upd_wr(r, "R")

    print(col(f"\n  After T+R: WR={best['wr']:.1f}%  {_fmt(best)}", C))

    # ── Phase 1: Sensitivity scan (from best found so far) ────────────────────
    print(f"\n{B}Phase 1 — Sensitivity scan{X}")
    sensitivity = sensitivity_scan(best, workers, "S1")
    banked = 0
    for k, impact, gdir, best_step_r, t_imp in sensitivity:
        if best_step_r and best_step_r["score"] > best["score"]:
            if upd(best_step_r, f"S1-{k}"): banked += 1
    if banked:
        print(col(f"  Banked {banked} scan improvements — rescanning …", G))
        sensitivity = sensitivity_scan(best, workers, "S1b")
        for k, impact, gdir, best_step_r, t_imp in sensitivity:
            if best_step_r and best_step_r["score"] > best["score"]:
                upd(best_step_r, f"S1b-{k}")


    # ═════════════════════════════════════════════════════════════════════════
    # OUTER LOOP: repeat phases A→E until WR≥70 AND trades≥100, max 3 rounds
    # ═════════════════════════════════════════════════════════════════════════
    for outer in range(3):
        print(col(f"\n{'═'*78}", C))
        print(col(f"  OUTER LOOP {outer+1}/3  —  WR={best['wr']:.1f}%  trades={best['trades']}", B+C))
        print(col(f"{'═'*78}", C))

        if best["wr"] >= 70 and best["trades"] >= target_trades:
            print(col("  ✓ Both targets already met — skipping further loops.", G)); break

        # ── Phase A: WR-first CD ─────────────────────────────────────────────
        print(f"\n{B}Phase A — WR-first coordinate descent (no trade floor){X}")
        wra = wr_first_cd(best, workers, f"WRcd-{outer+1}")
        maintain_hof(hof, [wra])
        # Adopt if WR improved by ANY amount (even if composite score dipped)
        if wra["wr"] > best["wr"] and wra["pnl"] >= 0:
            best = wra
            print(col(f"  Adopted WR-first result: WR={best['wr']:.1f}%", Y))
            maintain_hof(hof, [best])
        else:
            upd(wra, f"WRcd-{outer+1}")

        # ── Phase B: Volume recovery ─────────────────────────────────────────
        print(f"\n{B}Phase B — Volume recovery (WR floor 68%){X}")
        volume_ranking = volume_scan(best, workers)

        print(f"\n{B}Phase B2 — Multi-param volume push{X}")
        mvp = multi_param_volume_push(best, volume_ranking, workers, top_n=5)
        upd(mvp, f"MVP-{outer+1}"); maintain_hof(hof, [mvp])

        print(f"\n{B}Phase B3 — Volume-biased CD (WR≥68%){X}")
        vcd = volume_biased_cd(best, volume_ranking, workers, top_n=12)
        if upd(vcd, f"VCD-{outer+1}"):
            sensitivity = sensitivity_scan(best, workers, f"S{outer+2}")
            for k, impact, gdir, best_step_r, t_imp in sensitivity:
                if best_step_r and best_step_r["score"] > best["score"]:
                    upd(best_step_r, f"S{outer+2}-{k}")
        maintain_hof(hof, [vcd])

        # ── Phase C: Balanced CD ─────────────────────────────────────────────
        print(f"\n{B}Phase C — Balanced coordinate descent{X}")
        cd = coordinate_descent(best, sensitivity, workers, top_n=15, label=f"CD-{outer+1}")
        upd(cd, f"CD-{outer+1}"); maintain_hof(hof, [cd])

        # ── Phase C2: WR recovery (if still below 70%) ───────────────────────
        if best["wr"] < 70:
            print(f"\n{B}Phase C2 — WR recovery sweep (WR={best['wr']:.1f}% < 70%){X}")
            wrs = wr_recovery_sweep(best, workers)
            upd(wrs, f"WRS-{outer+1}"); maintain_hof(hof, [wrs])

        # ── Phase D: Gradient climb ──────────────────────────────────────────
        print(f"\n{B}Phase D — Gradient hill-climbing{X}")
        gc = gradient_climb(best, hof, sensitivity, workers, max_iters=60)
        upd(gc, f"GC-{outer+1}")

        # ── Phase E: Fine-tune ───────────────────────────────────────────────
        print(f"\n{B}Phase E — Fine-tune HOF{X}")
        ft = fine_tune(hof, workers, iters_per_base=20)
        upd(ft, f"FT-{outer+1}")

        # Refresh sensitivity for next outer loop
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
              "score": _prev.get("score",0), "avg_hold":0, "wr_score":0, "vol_score":0}
        print(f"  Previous : {_fmt(pv)}")
    print(f"  Best     : {_fmt(best)}")

    wr_ok  = best["wr"]     >= 70
    tr_ok  = best["trades"] >= 100
    pnl_ok = best["pnl"]    > 0
    all_ok = wr_ok and tr_ok and pnl_ok
    if all_ok:
        print(col("  ✓ ALL TARGETS MET!", B + G))
    else:
        gaps = []
        if not wr_ok:  gaps.append(f"WR={best['wr']:.1f}%<70%")
        if not tr_ok:  gaps.append(f"trades={best['trades']}<100")
        if not pnl_ok: gaps.append(f"PnL={best['pnl']:+.4f}≤0")
        print(col(f"  ✗ Gaps: {', '.join(gaps)}", Y))

    changed = {k: v for k, v in best["params"].items()
               if abs(float(best["params"].get(k,0)) - float(_BASELINE.get(k,0))) > 1e-9}
    print(f"\n  {B}Changed vs baseline ({len(changed)} params):{X}")
    for k, v in sorted(changed.items()):
        ds  = col(" ↑", G) if k in SPACE and v > (_BASELINE.get(k,0)) else col(" ↓", Y)
        cat = col(" [GATE]", C) if k in _ENTRY_GATE_PARAMS else (col(" [FILT]", Y) if k in _FILTER_PARAMS else "")
        print(f"    {k:<42s}  {_BASELINE.get(k,'?')} → {col(str(v), B+G)}{ds}{cat}")

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
    p = argparse.ArgumentParser(description="Auto-Tuner v12 — 70% Win Rate Edition")
    p.add_argument("--workers",     type=int, default=8)
    p.add_argument("--seed",        type=int, default=42)
    p.add_argument("--purge-every", type=int, default=50)
    a = p.parse_args()
    run(workers=a.workers, seed=a.seed, purge_every=a.purge_every)