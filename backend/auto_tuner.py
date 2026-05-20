#!/usr/bin/env python3
"""
Auto-Tuner v6 — Systematic Search Edition.

Current state (v5 result): WR=55.3%, trades=38/81 coins, PnL=+0.25 SOL
Target:                     WR≥70%,  trades≥max(40, 60% coins), PnL>0 SOL

═══════════════════════════════════════════════════════════════════════
WHY v5 FAILED TO REACH TARGET
═══════════════════════════════════════════════════════════════════════

1. RANDOM SEARCH IS WASTEFUL
   With ~30 tuneable params each having 5-20 discrete values, the
   parameter space has billions of combinations. 80 random candidates
   explore ~0.000001% of it. Most evaluations land in useless regions
   far from any optimum. Coordinate descent with systematic sweeps
   is 10-100× more sample-efficient.

2. confidence_high FLOOR TOO HIGH (0.82)
   At 55.3% WR / 38 trades across 81 coins, the algorithm is *both*
   too selective (misses many setups) AND still picking bad ones.
   Pinning the minimum to 0.82 prevents the tuner from exploring the
   region around 0.72-0.82 where the WR/trade-count trade-off might
   be much better. Lowered to 0.68.

3. SIGNAL_STRONG FLOOR TOO HIGH (2.0)
   Same issue — prevents exploration of entry conditions that allow
   more trades with comparable WR. Lowered to 1.2.

4. TRADE COUNT UNDER-PENALISED
   38/81 coins = 0.47 trades/coin. For a scalper that should be
   finding local lows on short timeframes, this is catastrophically
   low. v5 scored trade count at 18%. Raised to 25%, and added a
   hard "trades-per-coin" ratio penalty below 0.5.

═══════════════════════════════════════════════════════════════════════
v6 OPTIMISATION ARCHITECTURE
═══════════════════════════════════════════════════════════════════════

Phase 0  Baseline evaluation
Phase 1  Sensitivity scan
         Evaluate ±1 step for EVERY parameter from the current best.
         Ranks params by |Δscore| to focus future compute.
         Any single-step improvement is banked immediately.
         Cost: ~2×|SPACE| evals ≈ 60 evals.

Phase 2  Coordinate descent (top-N sensitive params, 2 passes)
         For each sensitive param, sweep its FULL range while holding
         all others fixed. Takes the best value found.
         This finds the true 1-D optimum per dimension — vastly more
         efficient than hoping random search stumbles on it.
         Cost: ~15 values/param × 14 params × 2 passes ≈ 420 evals.

Phase 3  Gradient hill-climbing with momentum (Adam-style)
         At each iteration:
           a) Estimate finite-difference gradient for a subset of params.
           b) Accumulate momentum (β=0.65): v = β·v + (1-β)·grad.
           c) Move in the momentum-guided direction.
           d) If no improvement for 5 steps, restart from a HOF member.
         This navigates smoothly toward local optima without random detours.
         Cost: ~20 iters × (8 params × 2 evals + 1 guided) ≈ 340 evals.

Phase 4  HOF ensemble fine-tuning
         From the top-3 HOF members, run tight 1-2 param perturbation
         to squeeze out remaining gains from multiple basins.
         Cost: 3 × 15 iters × ~4 evals ≈ 180 evals.

Total: ~1000 evaluations. At 35s/eval / 6 workers ≈ 97 minutes.
Compare v5: 120+ evals with far less systematic coverage.

═══════════════════════════════════════════════════════════════════════
SCALP PURITY — WHAT "GOOD" LOOKS LIKE
═══════════════════════════════════════════════════════════════════════
A perfect scalp: entry at local low → exit at local high, 1-3 bars.
The scorer penalises:
  - avg_hold_bars > 3 (starting to hold through a reversal)
  - avg_hold_bars > 6 (definitely riding an up-then-down cycle)
  - max_drawdown_pct > 3% (bag-holding behaviour)
  - pnl_per_trade < 0.0015 SOL (too small to justify the risk)
  - trades/coin < 0.5 (algorithm is too picky — not finding setups)
"""
from __future__ import annotations
import argparse, json, math, os, random, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from typing import Optional

sys.path.insert(0, os.path.dirname(__file__))
from backtester import run_backtest_batch
from data_store import list_recordings

G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; C = "\033[96m"
B = "\033[1m";  D = "\033[2m";  X = "\033[0m"

def col(t, c): return f"{c}{t}{X}"

# ── Baseline params ───────────────────────────────────────────────────────────
_BASELINE = {
    "ema_fast": 3, "ema_slow": 7, "atr_period": 7, "roc_period": 3, "warmup": 30,
    "signal_strong": 4.4, "signal_weak": 0.8, "signal_noise": 1.0535714285714286,
    "exhaustion_bars_limit": 1, "delta_threshold": 0.3, "kalman_gamma": 0.27,
    "min_trend_bars": 3, "reversal_confirm_bars": 1, "chop_atr_pct": 0.3,
    "chop_spread_pct": 0.05, "reversal_exit_confirm_bars": 1,
    "s_effective_threshold": 0.35, "exhaustion_persist_bars": 3, "regime_lookback": 6,
    "persistence_threshold": 2, "ema_min_spread_pct": 0.02,
    "confidence_high": 0.785, "confidence_low": 0.53,
    "confidence_w1": 0.3, "confidence_w2": 0.25, "confidence_w3": 0.25, "confidence_w4": 0.2,
    "atr_floor_k": 0.6, "ema_cross_persist_bars": 2, "exhaustion_s_decay_bars": 1,
    "exhaustion_stall_bars": 3, "exhaustion_stall_atr_pct": 0.35, "local_range_bars": 20,
    "local_range_threshold_pct": 0.7, "sign_flip_threshold": 1, "stability_bars": 3,
    "spike_atr_multiplier": 1.2, "spike_lookback_bars": 5, "body_baseline_bars": 20,
    "overextension_k": 0.17, "momentum_peak_bars": 1, "consolidation_range_pct": 1.6,
    "confidence_very_high": 0.84, "ema_macro_period": 5,
}

_best_json = os.path.join(os.path.dirname(__file__), "best_params.json")
if os.path.exists(_best_json):
    with open(_best_json) as f:
        _prev = json.load(f)
    START = _prev.get("params", _BASELINE)
    print(col(
        f"Loaded previous best: WR={_prev.get('win_rate', 0):.1f}%  "
        f"trades={_prev.get('total_trades', 0)}  score={_prev.get('score', 0):.4f}", C
    ))
else:
    _prev = {}
    START = deepcopy(_BASELINE)


# ── Search space ──────────────────────────────────────────────────────────────
# (lo, hi, step, is_int)
#
# Key changes vs v5:
#   confidence_high  floor: 0.82 → 0.68  (allow tuner to explore lower thresholds)
#   signal_strong    floor: 2.00 → 1.20  (allow more entry opportunities)
#   signal_weak      floor: 0.80 → 0.40  (same reason)
#   min_trend_bars   floor: 2   → 1      (allow 1-bar entry confirmation)
#   persistence_threshold floor: 2 → 1   (allow weaker persistence requirement)
#
# Exit-speed params remain pinned at 1 (lo == hi means tuner writes exactly lo).
SPACE = {
    # ── A. ENTRY GATES ──────────────────────────────────────────────────────
    "confidence_high":          (0.68, 0.96, 0.01, False),  # v5 floor was 0.82
    "confidence_low":           (0.25, 0.58, 0.01, False),
    "confidence_very_high":     (0.72, 0.97, 0.01, False),

    "signal_strong":            (1.20, 4.50, 0.10, False),  # v5 floor was 2.00
    "signal_weak":              (0.40, 1.60, 0.05, False),  # v5 floor was 0.80
    "signal_noise":             (0.70, 2.00, 0.10, False),
    "s_effective_threshold":    (0.10, 0.80, 0.05, False),

    "min_trend_bars":           (1, 5, 1, True),   # v5 floor was 2
    "stability_bars":           (1, 6, 1, True),
    "ema_cross_persist_bars":   (1, 6, 1, True),
    "persistence_threshold":    (1, 5, 1, True),   # v5 floor was 2

    # ── B. EXIT SPEED (PINNED — lo == hi means always written as lo) ────────
    "reversal_confirm_bars":      (1, 1, 1, True),
    "reversal_exit_confirm_bars": (1, 1, 1, True),
    "exhaustion_bars_limit":      (1, 1, 1, True),
    "exhaustion_s_decay_bars":    (1, 1, 1, True),
    "momentum_peak_bars":         (1, 1, 1, True),

    # Near-pinned: allow 1-2 bars max to avoid holding through reversals
    "exhaustion_persist_bars":    (1, 3, 1, True),
    "exhaustion_stall_bars":      (1, 2, 1, True),
    "exhaustion_stall_atr_pct":   (0.05, 0.30, 0.05, False),

    # ── C. CHOP / NOISE FILTERS ─────────────────────────────────────────────
    "consolidation_range_pct":   (0.4, 3.5, 0.1, False),
    "local_range_threshold_pct": (0.08, 0.90, 0.05, False),
    "local_range_bars":          (5, 25, 5, True),
    "sign_flip_threshold":       (1, 7, 1, True),
    "chop_atr_pct":              (0.1, 1.5, 0.1, False),
    "chop_spread_pct":           (0.05, 0.60, 0.05, False),

    # ── D. BLOW-OFF TOP GUARDS ───────────────────────────────────────────────
    "overextension_k":       (0.02, 0.30, 0.01, False),
    "spike_atr_multiplier":  (1.0, 5.0, 0.2, False),
    "spike_lookback_bars":   (2, 8, 1, True),
    "body_baseline_bars":    (10, 50, 5, True),

    # ── E. SIGNAL PROCESSING ────────────────────────────────────────────────
    "kalman_gamma":     (0.05, 0.45, 0.01, False),
    "regime_lookback":  (3, 10, 1, True),
}


# ── Scoring ───────────────────────────────────────────────────────────────────
def _score(
    wr: float,
    trades: int,
    pnl: float,
    coins_w: int,
    total: int,
    max_dd: float,
    avg_hold_bars: float = 0.0,
) -> float:
    """
    Balanced scorer for local-low → local-high scalping.

    Weights:
      WR          60%  (dominant, but trade count now gets real weight)
      Trade count 25%  (raised from v5's 18% — 38/81 is not acceptable)
      PnL/trade   12%
      Coverage     3%

    Hard penalties (multiplicative, applied after base):
      WR < 65%                  → exponential crush (optimiser cannot sacrifice WR)
      trades/coin < 0.5         → severe penalty (too few opportunities found)
      avg_hold_bars > 3 bars    → graduated penalty (not a true scalp)
      max_drawdown_pct > 3%     → bag-hold guard
      pnl < 0                   → heavy penalty (negative PnL = fundamental failure)
    """
    # ── WR component (60%) ───────────────────────────────────────────────────
    if wr >= 75:
        wr_s = 1.0 + min(0.60, (wr - 75) / 8)      # up to 1.60 bonus
    elif wr >= 70:
        wr_s = 0.30 + (wr - 70) / 5 * 0.70          # 0.30 → 1.00
    elif wr >= 65:
        wr_s = 0.02 + (wr - 65) / 5 * 0.28          # 0.02 → 0.30 (narrow acceptable)
    else:
        wr_s = max(0.0, (wr / 65) * 0.02)            # near-zero below 65%

    # ── Trade count component (25%) ──────────────────────────────────────────
    # Target: at least 60% coin coverage or 40 absolute minimum
    target = max(40, total * 0.60)
    trades_per_coin = trades / max(1, total)

    if trades >= target:
        trade_s = 1.0 + min(0.30, (trades - target) / 50)
    elif trades >= target * 0.70:
        trade_s = 0.50 + (trades - target * 0.70) / (target * 0.30) * 0.50
    elif trades >= target * 0.40:
        trade_s = 0.15 + (trades - target * 0.40) / (target * 0.30) * 0.35
    else:
        trade_s = max(0.0, trades / (target * 0.40) * 0.15)

    # ── PnL per trade component (12%) ────────────────────────────────────────
    pnl_per_trade = pnl / max(1, trades)
    ppt_floor = 0.0015          # ~1.5% on a 0.1 SOL position
    if pnl_per_trade <= 0:
        pnl_s = max(-1.0, pnl_per_trade / ppt_floor)
    elif pnl_per_trade <= ppt_floor:
        pnl_s = pnl_per_trade / ppt_floor
    else:
        pnl_s = 1.0 + min(2.0, (pnl_per_trade - ppt_floor) / ppt_floor)

    # ── Coverage component (3%) ──────────────────────────────────────────────
    cov_s = min(1.0, coins_w / max(1, total))

    # ── Base score ───────────────────────────────────────────────────────────
    base = 0.60 * wr_s + 0.25 * trade_s + 0.12 * pnl_s + 0.03 * cov_s

    # ═══════════════════════════════════════════════════════════════════════
    # MULTIPLICATIVE PENALTIES
    # ═══════════════════════════════════════════════════════════════════════

    # WR cliff: below 65% the algorithm is fundamentally broken as a scalper
    if wr < 65:
        base *= max(0.01, (wr / 65) ** 3)

    # Too few trades per coin: < 0.5 trades/coin means the entry gate is
    # so tight the algo misses the vast majority of valid setups
    if trades_per_coin < 0.50:
        base *= max(0.05, trades_per_coin / 0.50)

    # Absolute floor: very low trade count signals overfit / cherry-picking
    if trades < total * 0.30:
        base *= max(0.05, trades / (total * 0.30))

    # Negative PnL: a winning-rate ≥ 70% with negative PnL means something
    # is deeply wrong with sizing/slippage — penalise hard
    if pnl < 0:
        base *= max(0.01, 1.0 + pnl / max(0.01, abs(pnl) + 0.1))

    # Hold-duration penalty: a proper scalp is 1-3 bars max
    if avg_hold_bars > 0:
        if avg_hold_bars > 10:
            base *= max(0.10, 1.0 - (avg_hold_bars - 10) * 0.08)
        elif avg_hold_bars > 6:
            base *= max(0.40, 1.0 - (avg_hold_bars - 6) * 0.08)
        elif avg_hold_bars > 3:
            base *= max(0.75, 1.0 - (avg_hold_bars - 3) * 0.05)

    # Drawdown guard: > 3% max drawdown means holding through a reversal
    if max_dd > 3.0:
        base *= max(0.01, 1.0 - (max_dd - 3.0) / 8.0)

    return round(base, 4)


# ── Run a single parameter set ────────────────────────────────────────────────
def _run(params: dict) -> dict:
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

    avg_hold = 0.0
    hold_vals = [r["stats"].get("avg_hold_bars", 0) for r in ok if r["stats"].get("avg_hold_bars")]
    if hold_vals:
        avg_hold = sum(hold_vals) / len(hold_vals)

    wr = (wins / trd * 100) if trd > 0 else 0.0
    sc = _score(wr, trd, pnl, coins, len(ok), max_dd, avg_hold)
    return {
        "params": deepcopy(params),
        "wr": wr, "trades": trd, "pnl": pnl,
        "coins": coins, "total": len(ok),
        "score": sc, "avg_hold": avg_hold,
    }


def _fmt(r: dict) -> str:
    wr_c  = G if r["wr"] >= 70 else (Y if r["wr"] >= 65 else R)
    tr_c  = G if r["trades"] >= 40 else (Y if r["trades"] >= 25 else R)
    pnl_c = G if r["pnl"] > 0 else R
    hold  = f" hold={r.get('avg_hold', 0):.1f}b" if r.get("avg_hold") else ""
    wr_s  = col(f"{r['wr']:.1f}%", wr_c)
    tr_s  = col(str(r["trades"]), tr_c)
    pnl_s = col(f"{r['pnl']:+.4f}", pnl_c)
    sc_s  = col(f"{r['score']:.4f}", C)
    return f"WR={wr_s} trades={tr_s} pnl={pnl_s} cov={r['coins']}/{r['total']}{hold} sc={sc_s}"


# ── Helpers ───────────────────────────────────────────────────────────────────
def _clamp(k: str, v):
    lo, hi, _, is_int = SPACE[k]
    v = int(round(v)) if is_int else v
    return max(lo, min(hi, v))


def _step_val(k: str, v, direction: int):
    """Move param by one step in given direction, clamped to bounds."""
    _, _, st, _ = SPACE[k]
    return _clamp(k, v + st * direction)


def _rand_val(k: str):
    lo, hi, st, is_int = SPACE[k]
    if is_int:
        return random.randint(int(lo), int(hi))
    n = int(round((hi - lo) / st))
    return round(lo + random.randint(0, n) * st, 6)


def _all_values(k: str) -> list:
    """Return every discrete value in the search range for param k."""
    lo, hi, st, is_int = SPACE[k]
    vals = []
    v = lo
    while v <= hi + st * 0.01:
        vals.append(int(round(v)) if is_int else round(v, 6))
        v += st
    return vals


def maintain_hof(hof: list, new_results: list, max_size: int = 20):
    hof.extend(new_results)
    hof.sort(key=lambda r: r["score"], reverse=True)
    seen, out = [], []
    for r in hof:
        k = round(r["score"], 2)
        if k not in seen or len(out) < 5:
            seen.append(k)
            out.append(r)
        if len(out) >= max_size:
            break
    hof[:] = out


def eval_batch(candidates: list, workers: int) -> list:
    """Evaluate a list of param dicts in parallel, preserving order."""
    results: list = [None] * len(candidates)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fmap = {ex.submit(_run, p): i for i, p in enumerate(candidates)}
        for fut in as_completed(fmap):
            i = fmap[fut]
            try:
                results[i] = fut.result()
            except Exception as e:
                print(col(f"  WARN eval {i}: {e}", Y))
                results[i] = {
                    "params": candidates[i], "wr": 0, "trades": 0,
                    "pnl": 0, "coins": 0, "total": 0, "score": 0, "avg_hold": 0,
                }
    return [r for r in results if r is not None]


def diff_str(a: dict, b: dict) -> str:
    d = [(k, a.get(k), b.get(k)) for k in SPACE if a.get(k) != b.get(k)]
    return ", ".join(f"{k}:{av}→{col(str(bv), B)}" for k, av, bv in d) or "(none)"


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 — Parameter Sensitivity Scan
# ─────────────────────────────────────────────────────────────────────────────

def sensitivity_scan(base_result: dict, workers: int) -> list:
    """
    Evaluate ±1 step for every non-pinned parameter from the current best.

    Returns a list of tuples sorted by sensitivity (descending):
        (param_name, max_impact, gradient_direction, best_single_step_result)

    max_impact:          max |Δscore| across both directions
    gradient_direction:  +1 if increasing helps, -1 if decreasing helps, 0 if flat
    best_single_step_result: the dict from the single-step evaluation that
                             improved score the most (can be banked immediately)

    This is the cheapest possible way to understand the loss landscape before
    committing to expensive coordinate sweeps.
    """
    base_params = base_result["params"]
    base_score  = base_result["score"]

    candidates = []
    meta = []  # (param_name, direction)

    for k in SPACE:
        lo, hi, _, _ = SPACE[k]
        if lo == hi:          # pinned — skip
            continue
        v = base_params.get(k, lo)
        for direction in (+1, -1):
            new_v = _step_val(k, v, direction)
            if new_v != v:    # didn't hit boundary
                p = deepcopy(base_params)
                p[k] = new_v
                candidates.append(p)
                meta.append((k, direction))

    print(f"  Sensitivity scan: {len(candidates)} evals over {len(SPACE)} params …")
    t0 = time.time()
    results = eval_batch(candidates, workers)
    elapsed = time.time() - t0
    print(f"  Scan finished in {elapsed:.0f}s")

    # Aggregate per param
    param_data: dict[str, list] = {}   # k → [(direction, delta, result)]
    for (k, direction), r in zip(meta, results):
        delta = r["score"] - base_score
        param_data.setdefault(k, []).append((direction, delta, r))

    sensitivity = []
    for k, data in param_data.items():
        max_impact = max(abs(d) for _, d, _ in data)
        # Net gradient: sum of (delta × direction) — positive means "raise helps"
        grad = sum(d * dr for dr, d, _ in data)
        grad_dir = +1 if grad > 1e-6 else (-1 if grad < -1e-6 else 0)
        # Best single-step result for immediate banking
        best_step = max(data, key=lambda x: x[1])  # highest delta
        best_r = best_step[2] if best_step[1] > 0 else None
        sensitivity.append((k, max_impact, grad_dir, best_r))

    sensitivity.sort(key=lambda x: x[1], reverse=True)

    # Print top-15
    print(f"\n  Top-15 most impactful parameters:")
    for k, impact, gdir, _ in sensitivity[:15]:
        arrow = col(" ↑ raise helps", G) if gdir > 0 else (
                col(" ↓ lower helps", Y) if gdir < 0 else col(" ~ flat", D))
        print(f"    {k:<42s} Δ={impact:.4f}{arrow}")

    return sensitivity


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 — Coordinate Descent
# ─────────────────────────────────────────────────────────────────────────────

def coordinate_descent(
    best: dict,
    sensitivity: list,
    workers: int,
    top_n: int = 14,
    label: str = "CD",
) -> dict:
    """
    For each of the top-N sensitive parameters, sweep its ENTIRE discrete range
    while holding all others fixed at the current best. Take the globally best
    value found for that parameter.

    Why this beats random search:
      - Random search at 80 iters may never try the optimal value for a given
        param if there are 15 discrete values (probability per iter = 1/15 = 7%).
      - Coordinate descent guarantees finding the 1-D optimum in O(range) evals.

    Parameters are optimised in sensitivity order (highest impact first) so
    each subsequent sweep benefits from already-improved params.
    """
    current = deepcopy(best)
    improved = 0

    for rank, (k, impact, grad_dir, _) in enumerate(sensitivity[:top_n]):
        if k not in SPACE:
            continue
        lo, hi, st, is_int = SPACE[k]
        if lo == hi:   # pinned
            continue

        vals = _all_values(k)
        if len(vals) <= 1:
            continue

        # Build one candidate per value
        candidates = [deepcopy(current["params"]) for _ in vals]
        for p, v in zip(candidates, vals):
            p[k] = v

        t0 = time.time()
        # Evaluate in parallel batches of `workers`
        best_val_result: Optional[dict] = None
        for i in range(0, len(candidates), workers):
            batch = candidates[i:i + workers]
            batch_results = eval_batch(batch, workers)
            for r in batch_results:
                if best_val_result is None or r["score"] > best_val_result["score"]:
                    best_val_result = r

        elapsed = time.time() - t0

        if best_val_result and best_val_result["score"] > current["score"]:
            old_v = current["params"].get(k)
            new_v = best_val_result["params"][k]
            delta = best_val_result["score"] - current["score"]
            current = best_val_result
            improved += 1
            print(col(
                f"  {label} [{rank+1:2d}/{top_n}] {k:<38s} "
                f"{old_v} → {new_v}  Δ={delta:+.4f}  {_fmt(current)}  ({elapsed:.0f}s)",
                G
            ))
        else:
            best_score_in_sweep = best_val_result["score"] if best_val_result else 0
            print(
                f"  {label} [{rank+1:2d}/{top_n}] {k:<38s} "
                f"no gain (best={best_score_in_sweep:.4f}, curr={current['score']:.4f})  ({elapsed:.0f}s)"
            )

    print(col(f"\n  {label} complete: {improved}/{top_n} params improved  {_fmt(current)}", C))
    return current


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3 — Gradient Hill-Climbing with Momentum
# ─────────────────────────────────────────────────────────────────────────────

def gradient_climb(
    best: dict,
    hof: list,
    sensitivity: list,
    workers: int,
    max_iters: int = 40,
) -> dict:
    """
    Adam-style gradient hill-climbing. Uses finite-difference gradient
    estimates with momentum to follow the loss landscape smoothly.

    Algorithm per iteration:
      1. Pick up to `probe_n` params from the sensitive set.
      2. Evaluate current±1step for each (finite-difference probe).
      3. Compute gradient: g_k = (score(k+1) - score(k-1)) / 2  [sign only]
      4. Update velocity with momentum: v_k = β·v_k + (1-β)·g_k
      5. Move each param by +1 or -1 step according to sign(v_k).
      6. Evaluate the momentum-guided candidate.
      7. If no improvement for `patience` steps, restart from HOF.

    The momentum term (β=0.65) smooths over noisy gradient estimates and
    helps navigate narrow ridges in the parameter space.
    """
    current = deepcopy(best)
    hof_copy = list(hof)   # local reference for restarts

    # Focus search on top sensitive params (non-pinned)
    active_params = [k for k, _, _, _ in sensitivity
                     if k in SPACE and SPACE[k][0] != SPACE[k][1]]
    if not active_params:
        active_params = [k for k in SPACE if SPACE[k][0] != SPACE[k][1]]

    probe_n = min(8, len(active_params))    # params probed per iter
    beta    = 0.65                           # momentum coefficient
    velocity: dict[str, float] = {k: 0.0 for k in active_params}

    no_improve = 0
    patience   = 6    # restarts after this many consecutive non-improvements

    print(f"  Gradient climb: up to {max_iters} iters, probing {probe_n} params/iter")

    for iteration in range(max_iters):
        # Rotate through sensitive params so all eventually get probed
        offset  = (iteration * probe_n) % max(1, len(active_params))
        subset  = (active_params[offset:] + active_params[:offset])[:probe_n]

        # Build probe candidates: ±1 step for each param in subset
        probe_cands = []
        probe_meta  = []   # (k, direction)
        for k in subset:
            v = current["params"].get(k, SPACE[k][0])
            for direction in (+1, -1):
                new_v = _step_val(k, v, direction)
                if new_v != v:
                    p = deepcopy(current["params"])
                    p[k] = new_v
                    probe_cands.append(p)
                    probe_meta.append((k, direction))

        if not probe_cands:
            break

        t0 = time.time()
        probe_results = eval_batch(probe_cands, workers)

        # Compute per-param gradient signal from probe results
        # g_k = mean of (delta × direction) across both ±1 evals
        grad_signal: dict[str, float] = {}
        for (k, direction), r in zip(probe_meta, probe_results):
            delta = r["score"] - current["score"]
            grad_signal[k] = grad_signal.get(k, 0.0) + delta * direction

        # Update velocity and build guided candidate
        guided_params = deepcopy(current["params"])
        moved_keys    = []
        for k in subset:
            g = grad_signal.get(k, 0.0)
            velocity[k] = beta * velocity[k] + (1 - beta) * g

            v_curr = current["params"].get(k, SPACE[k][0])
            if velocity[k] > 1e-5:
                new_v = _step_val(k, v_curr, +1)
            elif velocity[k] < -1e-5:
                new_v = _step_val(k, v_curr, -1)
            else:
                continue

            if new_v != v_curr:
                guided_params[k] = new_v
                moved_keys.append(k)

        elapsed = time.time() - t0

        if not moved_keys:
            no_improve += 1
            continue

        # Evaluate the momentum-guided step
        guided_result = _run(guided_params)
        maintain_hof(hof_copy, [guided_result])

        if guided_result["score"] > current["score"]:
            delta = guided_result["score"] - current["score"]
            current = guided_result
            no_improve = 0
            print(col(
                f"  GC iter {iteration+1:3d}: {_fmt(current)}  "
                f"Δ={delta:+.4f}  moved={len(moved_keys)}p  ({elapsed:.0f}s)",
                G
            ))
        else:
            no_improve += 1
            if iteration % 4 == 0 or no_improve >= patience:
                print(
                    f"  GC iter {iteration+1:3d}: {_fmt(guided_result)}  "
                    f"no improve ({no_improve}/{patience})  ({elapsed:.0f}s)"
                )

            # Restart from best HOF member not yet tried
            if no_improve >= patience:
                candidates_for_restart = [
                    r for r in hof_copy[:6]
                    if r["score"] >= current["score"] * 0.98
                ]
                if candidates_for_restart:
                    restart_base = random.choice(candidates_for_restart)
                    if restart_base is not current:
                        current = restart_base
                        velocity = {k: 0.0 for k in active_params}   # reset momentum
                        no_improve = 0
                        print(col(f"  GC restart → HOF member: {_fmt(current)}", Y))

        # Early exit if target reached
        if current["wr"] >= 70 and current["trades"] >= 40:
            print(col(f"\n  ✓ Target reached at GC iter {iteration + 1}!", B + G))
            break

    # Sync discoveries back to caller's HOF
    maintain_hof(hof, hof_copy)
    return current


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4 — HOF Ensemble Fine-Tuning
# ─────────────────────────────────────────────────────────────────────────────

def fine_tune(hof: list, workers: int, iters_per_base: int = 15) -> dict:
    """
    From the top-3 HOF members, run tight 1-2 param perturbations to
    squeeze out any remaining marginal gains.  Multiple basins → avoids
    getting stuck in the local optimum found by coordinate descent.

    Each iteration tries ±1 step for 1-2 randomly chosen non-pinned params
    and banks any improvement.
    """
    overall_best = hof[0]
    non_pinned   = [k for k in SPACE if SPACE[k][0] != SPACE[k][1]]

    for base_idx, base_result in enumerate(hof[:3]):
        print(f"\n  Fine-tune base #{base_idx + 1}: {_fmt(base_result)}")
        current   = deepcopy(base_result)
        no_improve = 0

        for it in range(iters_per_base):
            n_perturb = random.choices([1, 2], weights=[0.6, 0.4])[0]
            keys      = random.sample(non_pinned, min(n_perturb, len(non_pinned)))

            cands = []
            for k in keys:
                v = current["params"].get(k)
                for d in (+1, -1):
                    new_v = _step_val(k, v, d)
                    if new_v != v:
                        p = deepcopy(current["params"])
                        p[k] = new_v
                        cands.append(p)

            if not cands:
                continue

            results  = eval_batch(cands, workers)
            best_r   = max(results, key=lambda r: r["score"])

            if best_r["score"] > current["score"]:
                delta   = best_r["score"] - current["score"]
                current = best_r
                no_improve = 0
                maintain_hof(hof, [current])
                if current["score"] > overall_best["score"]:
                    overall_best = current
                print(col(
                    f"    ★ FT [b{base_idx+1} iter{it+1}]: Δ={delta:+.4f}  {_fmt(current)}",
                    B + G
                ))
            else:
                no_improve += 1
                if no_improve >= 8:
                    break   # this basin is exhausted

    return overall_best


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def run(workers: int = 6, seed: int = 42):
    random.seed(seed)

    recs      = list_recordings()
    completed = [r for r in recs if r.get("status") == "completed"]
    if not completed:
        print(col("No completed recordings found!", R))
        sys.exit(1)

    target_trades = max(40, int(len(completed) * 0.60))

    print(col("═" * 78, C))
    print(col("  Auto-Tuner v6 — Systematic Search Edition", B + C))
    print(col(f"  {len(completed)} coins | {workers} workers", C))
    print(col(f"  Current state: WR≈55.3%  trades≈38  PnL≈+0.25 SOL", Y))
    print(col(f"  Target:        WR≥70%    trades≥{target_trades}  PnL>0 SOL", G))
    print(col(f"  Method: Sensitivity → CoordDescent → GradientClimb → FineTune", C))
    print(col(f"  Est. time: 60–120 min depending on hardware", D))
    print(col("═" * 78, C))

    # Force all pinned exit params to their correct values in START
    start = deepcopy(START)
    for k in [
        "reversal_confirm_bars", "reversal_exit_confirm_bars",
        "exhaustion_bars_limit", "exhaustion_s_decay_bars", "momentum_peak_bars",
    ]:
        start[k] = 1

    # ── Phase 0: Baseline ────────────────────────────────────────────────────
    print(f"\n{B}Phase 0 — Baseline evaluation{X}")
    t0   = time.time()
    best = _run(start)
    print(f"  Baseline: {_fmt(best)}  ({time.time() - t0:.0f}s)")
    hof  = [best]

    def upd(r: dict, label: str) -> bool:
        nonlocal best
        if r["score"] > best["score"]:
            best = r
            print(col(f"\n  ★ NEW BEST [{label}]  {_fmt(best)}", B + G))
            print(col(f"    Δ vs start: {diff_str(start, best['params'])}", Y))
            maintain_hof(hof, [best])
            return True
        return False

    # ── Phase 1: Sensitivity Scan ────────────────────────────────────────────
    print(f"\n{B}Phase 1 — Parameter sensitivity scan{X}")
    sensitivity = sensitivity_scan(best, workers)

    # Bank any single-step improvements found during the scan
    banked = 0
    for k, impact, grad_dir, best_step_r in sensitivity:
        if best_step_r is not None and best_step_r["score"] > best["score"]:
            if upd(best_step_r, f"S1-{k}"):
                banked += 1
    if banked:
        print(col(f"  Banked {banked} single-step improvements from scan.", G))
        # Re-scan from new best (parameters have shifted)
        print(f"  Re-scanning from new best …")
        sensitivity = sensitivity_scan(best, workers)
        for k, impact, grad_dir, best_step_r in sensitivity:
            if best_step_r is not None and best_step_r["score"] > best["score"]:
                upd(best_step_r, f"S1b-{k}")

    # ── Phase 2: Coordinate Descent — Pass 1 ────────────────────────────────
    print(f"\n{B}Phase 2a — Coordinate descent (pass 1, top-14 params){X}")
    cd1_result = coordinate_descent(best, sensitivity, workers, top_n=14, label="CD1")
    if upd(cd1_result, "CD1"):
        # Params have changed — re-scan to update sensitivities for pass 2
        print(f"\n  Refreshing sensitivity after CD1 …")
        sensitivity = sensitivity_scan(best, workers)
        for k, impact, grad_dir, best_step_r in sensitivity:
            if best_step_r is not None and best_step_r["score"] > best["score"]:
                upd(best_step_r, f"S2-{k}")

    # ── Phase 2: Coordinate Descent — Pass 2 ────────────────────────────────
    # Second pass captures interactions: improving param A may have changed
    # the optimal value for param B.
    print(f"\n{B}Phase 2b — Coordinate descent (pass 2, top-10 params){X}")
    cd2_result = coordinate_descent(best, sensitivity, workers, top_n=10, label="CD2")
    upd(cd2_result, "CD2")
    maintain_hof(hof, [cd1_result, cd2_result])

    # ── Phase 3: Gradient Hill-Climbing ─────────────────────────────────────
    print(f"\n{B}Phase 3 — Gradient hill-climbing with momentum (≤40 iters){X}")
    gc_result = gradient_climb(best, hof, sensitivity, workers, max_iters=40)
    upd(gc_result, "GC")

    # ── Phase 4: Fine-Tuning ─────────────────────────────────────────────────
    print(f"\n{B}Phase 4 — HOF ensemble fine-tuning (top-3 bases, 15 iters each){X}")
    ft_result = fine_tune(hof, workers, iters_per_base=15)
    upd(ft_result, "FT")

    # ── Final Report ──────────────────────────────────────────────────────────
    print(col("\n" + "═" * 78, C))
    print(col("  FINAL RESULTS", B + C))
    print(col("═" * 78, C))

    if _prev:
        prev_display = {
            "wr": _prev.get("win_rate", 0), "trades": _prev.get("total_trades", 0),
            "pnl": _prev.get("total_pnl", 0), "coins": 0,
            "total": len(completed), "score": _prev.get("score", 0), "avg_hold": 0,
        }
        print(f"  Previous : {_fmt(prev_display)}")
    print(f"  Best     : {_fmt(best)}")

    wr_ok    = best["wr"] >= 70
    trade_ok = best["trades"] >= max(40, len(completed) * 0.5)
    all_ok   = wr_ok and trade_ok

    if all_ok:
        print(col("  ✓ ALL TARGETS MET!", B + G))
    else:
        gaps = []
        if not wr_ok:
            gaps.append(f"WR={best['wr']:.1f}%<70%")
        if not trade_ok:
            gaps.append(f"trades={best['trades']}<{max(40, int(len(completed)*0.5))}")
        print(col(f"  ✗ Gaps: {', '.join(gaps)}", Y))
        print(col(
            "  Tip: Try adding more recordings or running with --workers 8 "
            "for more parallelism.", D
        ))

    # Changed vs baseline
    changed = {
        k: v for k, v in best["params"].items()
        if abs(float(best["params"].get(k, 0)) - float(_BASELINE.get(k, 0))) > 1e-9
    }
    print(f"\n  {B}Changed vs baseline ({len(changed)} params):{X}")
    for k, v in sorted(changed.items()):
        direction_str = ""
        if k in SPACE:
            lo, hi, _, _ = SPACE[k]
            mid = (lo + hi) / 2
            direction_str = col(" ↑", G) if v > mid else col(" ↓", Y)
        bl_v = _BASELINE.get(k, "?")
        print(f"    {k:<42s}  {bl_v} → {col(str(v), B+G)}{direction_str}")

    # Save
    out = {
        "params":        best["params"],
        "win_rate":      best["wr"],
        "total_trades":  best["trades"],
        "total_pnl":     best["pnl"],
        "score":         best["score"],
    }
    with open(_best_json, "w") as f:
        json.dump(out, f, indent=2)
    print(col(f"\n  Saved → {_best_json}", C))

    print(f"\n  {B}Top 5 Hall of Fame:{X}")
    for i, r in enumerate(hof[:5]):
        print(f"  #{i+1}  {_fmt(r)}")

    return best


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Auto-Tuner v6 — Systematic Search")
    p.add_argument("--workers", type=int, default=6,
                   help="Parallel evaluation threads (default 6)")
    p.add_argument("--seed",    type=int, default=42,
                   help="Random seed for reproducibility")
    a = p.parse_args()
    run(workers=a.workers, seed=a.seed)