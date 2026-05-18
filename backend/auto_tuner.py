#!/usr/bin/env python3
"""
Targeted Tuner v4 — Precision Scalp Edition.

Philosophy: The algorithm should ONLY enter at local lows and exit immediately
at local highs. It must never hold long enough to experience ups AND downs —
that is gambling, not trading.

Key changes from v3:
  1. WR threshold raised to 70% (heavy cliff below it)
  2. Score PUNISHES low trade frequency relative to coins covered
  3. New "precision" bonus: rewards strategies with tight PnL variance per trade
  4. Search space heavily focused on:
       - Exit speed params (get out fast at local high)
       - Entry strictness (only enter confirmed local lows)
       - Duration caps (exhaustion_bars_limit kept short)
       - Reversal/confirm bars kept minimal (fast reaction)
  5. Baseline starts from best_params.json if available

Target: WR ≥ 70%, trades ≥ 40, PnL > 0.20 SOL
"""
from __future__ import annotations
import argparse, json, math, os, random, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy

sys.path.insert(0, os.path.dirname(__file__))
from backtester import run_backtest_batch
from data_store import list_recordings

G="\033[92m"; Y="\033[93m"; R="\033[91m"; C="\033[96m"; B="\033[1m"; D="\033[2m"; X="\033[0m"
def col(t,c): return f"{c}{t}{X}"

# ── Baseline params (conservative starting point) ───────────────────────────
_BASELINE = {
    "ema_fast":3,"ema_slow":7,"atr_period":7,"roc_period":3,"warmup":10,
    "signal_strong":3.5,"signal_weak":0.8,"signal_noise":1.0535714285714286,
    "exhaustion_bars_limit":1,"delta_threshold":0.3,"kalman_gamma":0.23,
    "min_trend_bars":2,"reversal_confirm_bars":1,"chop_atr_pct":0.3,
    "chop_spread_pct":0.15,"reversal_exit_confirm_bars":1,
    "s_effective_threshold":0.35,"exhaustion_persist_bars":5,"regime_lookback":5,
    "persistence_threshold":2,"ema_min_spread_pct":0.02,
    "confidence_high":0.785,"confidence_low":0.45571428571428574,
    "confidence_w1":0.3,"confidence_w2":0.25,"confidence_w3":0.25,"confidence_w4":0.2,
    "atr_floor_k":0.6,"ema_cross_persist_bars":3,"exhaustion_s_decay_bars":1,
    "exhaustion_stall_bars":3,"exhaustion_stall_atr_pct":0.35,"local_range_bars":13,
    "local_range_threshold_pct":0.7,"sign_flip_threshold":2,"stability_bars":5,
    "spike_atr_multiplier":1.2,"spike_lookback_bars":5,"body_baseline_bars":20,
    "overextension_k":0.17,"momentum_peak_bars":2,"consolidation_range_pct":1.7,
    "confidence_very_high":0.8448571428571429,"ema_macro_period":5,
}

_best_json = os.path.join(os.path.dirname(__file__), "best_params.json")
if os.path.exists(_best_json):
    with open(_best_json) as f:
        _prev = json.load(f)
    START = _prev.get("params", _BASELINE)
    print(col(f"Loaded previous best: WR={_prev.get('win_rate',0):.1f}%  trades={_prev.get('total_trades',0)}  score={_prev.get('score',0):.4f}", C))
else:
    _prev = {}
    START = deepcopy(_BASELINE)

# ── Search space — precision scalp focused ───────────────────────────────────
# Philosophy: enter at local low (strict confidence), exit FAST at local high.
# Every param here is chosen to either:
#   A) Tighten ENTRY gates (higher quality signals only)
#   B) Speed up EXIT (short exhaustion windows, fast reversal detection)
#   C) Block "gambling" holds (chop filters, overextension guards)
SPACE = {
    # ── ENTRY STRICTNESS (A) ─────────────────────────────────────────────────
    # Raise the bar: only enter when confidence is very high
    "confidence_high":          (0.78, 0.95, 0.01, False),  # was 0.72–0.92
    "confidence_low":           (0.35, 0.60, 0.01, False),  # dead-zone floor
    "confidence_very_high":     (0.80, 0.97, 0.01, False),  # fast-entry gate

    # Signal quality thresholds — demand strong, clear signals
    "signal_strong":            (1.50, 3.50, 0.10, False),  # higher = stricter
    "signal_weak":              (0.80, 1.60, 0.05, False),
    "signal_noise":             (0.80, 1.80, 0.10, False),  # noise floor
    "s_effective_threshold":    (0.20, 0.80, 0.05, False),  # barrier-adjusted gate

    # Momentum confirmation before entry — must see real trend forming
    "min_trend_bars":           (2, 6, 1, True),    # need several bars of trend
    "stability_bars":           (2, 5, 1, True),    # stable momentum required
    "ema_cross_persist_bars":   (2, 7, 1, True),    # EMA cross must persist
    "persistence_threshold":    (2, 5, 1, True),    # momentum persistence gate

    # ── EXIT SPEED (B) ───────────────────────────────────────────────────────
    # Exit fast when momentum fades — don't ride it back down
    "reversal_confirm_bars":        (1, 1, 1, True),  # force 1 bar
    "reversal_exit_confirm_bars":   (1, 1, 1, True),  # force 1 bar
    "exhaustion_bars_limit":        (1, 1, 1, True),  # force 1 bar
    "exhaustion_persist_bars":      (1, 2, 1, True),  # exit trigger sensitivity
    "exhaustion_s_decay_bars":      (1, 1, 1, True),  # how fast signal must decay
    "exhaustion_stall_bars":        (1, 2, 1, True),  # stall = exit signal
    "exhaustion_stall_atr_pct":     (0.1, 0.3, 0.05, False),  # tight stall threshold
    "momentum_peak_bars":           (1, 1, 1, True),  # force 1 bar

    # ── ANTI-GAMBLING / CHOP FILTERS (C) ────────────────────────────────────
    # Aggressively block sideways/choppy markets
    "consolidation_range_pct":  (0.3, 2.5, 0.2, False),  # tighter = blocks more chop
    "local_range_threshold_pct":(0.2, 0.7, 0.05, False), # local chop gate
    "local_range_bars":         (5, 20, 5, True),          # window for chop check
    "sign_flip_threshold":      (2, 6, 1, True),           # flip count = chop signal
    "chop_atr_pct":             (0.3, 0.9, 0.1, False),   # ATR-relative chop gate
    "chop_spread_pct":          (0.05, 0.35, 0.05, False), # EMA spread chop gate

    # Blow-off / overextension guards (block buying the very top)
    "overextension_k":          (0.02, 0.10, 0.01, False),
    "spike_atr_multiplier":     (1.5, 4.0, 0.2, False),
    "spike_lookback_bars":      (2, 6, 1, True),
    "body_baseline_bars":       (10, 40, 5, True),

    # Kalman filter speed (affects lag on entry/exit timing)
    "kalman_gamma":             (0.05, 0.28, 0.01, False),

    # Regime lookback — shorter = reacts to local structure faster
    "regime_lookback":          (3, 8, 1, True),
}


def _score(wr: float, trades: int, pnl: float, coins_w: int, total: int, max_dd: float) -> float:
    """
    Scoring function designed for precision scalping:

    WR (55% weight): Cliff function. 70% is the minimum acceptable.
      - Below 60%: near-zero (algorithm is gambling)
      - 60–70%: linear ramp to 0.4 (acceptable but not target)
      - 70–75%: strong ramp to 1.0 (in the zone)
      - 75%+:   bonus territory, capped at 1.5

    Trades (20% weight): Want 40+. Penalise < 25 hard (too few = cherry-picking).
      - Under 15: near-zero (not enough sample)
      - 15–40:   ramp to 1.0
      - 40+:     bonus up to 1.2

    PnL (20% weight): Positive PnL per trade is key, not raw total.
      - Normalise by trade count → "quality per trade"
      - Cap at 0.015 SOL/trade as "excellent"

    Coverage (5% weight): More coins covered = more robust.

    NOTE: WR below 60% incurs a multiplicative penalty on the entire score.
    This forces the optimiser to never sacrifice WR for trades/PnL.
    """
    # ── WR score ──────────────────────────────────────────────────────────────
    if wr >= 75:
        wr_s = 1.0 + min(0.5, (wr - 75) / 10)  # bonus up to 1.5
    elif wr >= 70:
        wr_s = 0.4 + (wr - 70) / 5 * 0.6       # 0.4 → 1.0
    elif wr >= 60:
        wr_s = 0.05 + (wr - 60) / 10 * 0.35    # 0.05 → 0.4
    else:
        wr_s = max(0.0, wr / 60 * 0.05)         # basically zero

    # ── Trade count score ────────────────────────────────────────────────────
    target_trades = max(40, total * 0.5)
    if trades >= target_trades:
        trade_s = 1.0 + min(0.2, (trades - target_trades) / 30)
    elif trades >= target_trades * 0.6:
        trade_s = 0.5 + (trades - target_trades * 0.6) / (target_trades * 0.4) * 0.5
    elif trades >= target_trades * 0.3:
        trade_s = 0.2 + (trades - target_trades * 0.3) / (target_trades * 0.3) * 0.3
    else:
        trade_s = max(0.0, trades / (target_trades * 0.3) * 0.2)

    # ── PnL-per-trade quality score ──────────────────────────────────────────
    # A strategy that makes 0.3 SOL on 50 trades (0.006/trade) is better than
    # 0.3 SOL on 10 trades (0.03/trade) because it's more statistically robust.
    # Target is >4% gain per trade. 4% of 0.1 SOL buy size = 0.004 SOL per trade.
    pnl_per_trade = pnl / max(1, trades)
    if pnl_per_trade <= 0.004:
        pnl_s = max(-0.5, pnl_per_trade / 0.004)
    else:
        # Reward big wins as long as they appear alongside high WR and trade frequency
        pnl_s = 1.0 + min(2.0, (pnl_per_trade - 0.004) / 0.004)

    # ── Coverage score ───────────────────────────────────────────────────────
    cov_s = min(1.0, coins_w / max(1, total))

    # ── Base score ───────────────────────────────────────────────────────────
    # Emphasize WR and trade counts, de-emphasize PnL score slightly
    base = 0.60 * wr_s + 0.25 * trade_s + 0.10 * pnl_s + 0.05 * cov_s

    # ── Hard penalty: if WR < 60%, algorithm is gambling → crush score ───────
    if wr < 60:
        penalty = max(0.1, wr / 60)  # 0.1 at 0%, 1.0 at 60%
        base *= penalty

    # ── Hard penalty: too few trades means overfit / cherry-picking ──────────
    if trades < total * 0.5:
        base *= max(0.1, trades / (total * 0.5))
        
    # ── Hard penalty: PnL per trade too low (target > 4% / ~0.004 SOL) ────────
    if pnl_per_trade < 0.0035:
        # penalize heavily if we average less than ~3.5% gain per trade
        base *= max(0.1, pnl_per_trade / 0.0035)

    # ── Hard penalty: Massive Drawdown penalty ──────────────────────────────
    # A max drawdown above 5% across the batch indicates holding losers (bag holding cross-trend).
    if max_dd > 5.0:
        dd_penalty = max(0.001, 1.0 - ((max_dd - 5.0) / 10.0))  # Scales down to near 0 rapidly
        base *= dd_penalty

    return round(base, 4)


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
    trd    = sum(r["stats"]["total_trades"]   for r in ok)
    wins   = sum(r["stats"]["winning_trades"]  for r in ok)
    pnl    = sum(r["stats"]["total_pnl_sol"]   for r in ok)
    coins  = sum(1 for r in ok if r["stats"]["total_trades"] > 0)
    
    # Calculate the worst drawdown across all coins to check bag holding behavior
    max_dd = max([r["stats"]["max_drawdown_pct"] for r in ok], default=0.0)

    wr     = (wins / trd * 100) if trd > 0 else 0.0
    sc     = _score(wr, trd, pnl, coins, len(ok), max_dd)
    return {
        "params": deepcopy(params),
        "wr": wr, "trades": trd, "pnl": pnl,
        "coins": coins, "total": len(ok), "score": sc,
    }


def _fmt(r: dict) -> str:
    wr_c  = G if r["wr"] >= 70 else (Y if r["wr"] >= 60 else R)
    tr_c  = G if r["trades"] >= 40 else (Y if r["trades"] >= 25 else R)
    pnl_c = G if r["pnl"] > 0 else R
    wr    = col(f"{r['wr']:.1f}%", wr_c)
    tr    = col(str(r["trades"]), tr_c)
    pnl   = col(f"{r['pnl']:+.3f}", pnl_c)
    sc    = col(f"{r['score']:.4f}", C)
    return f"WR={wr} trades={tr} pnl={pnl} cov={r['coins']}/{r['total']} sc={sc}"


def _rand_val(k: str):
    lo, hi, st, is_int = SPACE[k]
    if is_int:
        return random.randint(int(lo), int(hi))
    n = int(round((hi - lo) / st))
    return round(lo + random.randint(0, n) * st, 6)


def _clamp(k: str, v):
    lo, hi, _, is_int = SPACE[k]
    v = int(round(v)) if is_int else v
    return max(lo, min(hi, v))


def perturb(base: dict, n: int = 4) -> dict:
    """Random perturbation — move n random params to new random values."""
    p = deepcopy(base)
    for k in random.sample(list(SPACE), min(n, len(SPACE))):
        p[k] = _rand_val(k)
    return p


def neighbour(base: dict, n: int = 2, scale: float = 1.0) -> dict:
    """Local neighbourhood step — nudge n params by ±1 step."""
    p = deepcopy(base)
    for k in random.sample(list(SPACE), min(n, len(SPACE))):
        _, _, st, _ = SPACE[k]
        p[k] = _clamp(k, p[k] + st * scale * random.choice([-1, 1]))
    return p


def scalp_bias(base: dict) -> dict:
    """
    Directionally biased mutation: push params toward the 'fast scalp' direction.
    Used periodically to nudge the search toward the target behaviour even when
    random search would drift away.
    """
    p = deepcopy(base)
    # Exit faster
    for k in ["reversal_confirm_bars", "reversal_exit_confirm_bars",
              "exhaustion_bars_limit", "exhaustion_persist_bars", "momentum_peak_bars"]:
        if k in SPACE:
            lo, _, st, _ = SPACE[k]
            p[k] = max(lo, p[k] - st)  # push toward minimum (faster exit)
    # Raise entry bar
    for k in ["confidence_high", "confidence_very_high", "signal_strong", "min_trend_bars"]:
        if k in SPACE:
            _, hi, st, _ = SPACE[k]
            p[k] = min(hi, p[k] + st)  # push toward maximum (stricter entry)
    # Tighten chop filter
    for k in ["consolidation_range_pct", "sign_flip_threshold"]:
        if k in SPACE:
            lo, _, st, _ = SPACE[k]
            p[k] = max(lo, p[k] - st)  # tighter chop detection
    return p


def eval_batch(candidates: list, workers: int) -> list:
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
                    "params": candidates[i], "wr": 0, "trades": 0,
                    "pnl": 0, "coins": 0, "total": 0, "score": 0,
                }
    return [r for r in results if r]


def diff(a: dict, b: dict) -> str:
    d = [(k, a.get(k), b.get(k)) for k in SPACE if a.get(k) != b.get(k)]
    return ", ".join(f"{k}:{av}→{col(str(bv), B)}" for k, av, bv in d) or "(none)"


def maintain_hof(hof: list, new: list, max_size: int = 15):
    """Update hall-of-fame, keeping diverse set of high scorers."""
    hof.extend(new)
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


def run(workers: int = 6, rand_iters: int = 80, hill_iters: int = 40, seed: int = 42):
    random.seed(seed)

    recs = list_recordings()
    completed = [r for r in recs if r.get("status") == "completed"]
    if not completed:
        print(col("No completed recordings!", R))
        sys.exit(1)

    total_evals = 1 + rand_iters + hill_iters
    est_min = total_evals // workers * 35 // 60

    # Target: WR ≥ 70%, trades ≥ 40, PnL > 4% avg gain, max drawdown < 15%
    print(col("═" * 78, C))
    print(col(f"  Precision Scalp Tuner v4 — {len(completed)} coins | {workers} workers", B + C))
    print(col(f"  ~{total_evals} evals | est {est_min}min", C))
    print(col(f"  Objective: WR ≥ 70%  trades ≥ max(40, 50% coins)  pnl/trade ≥ ~0.0035 SOL", C))
    print(col(f"             max drawdown penalty active (target < 10%)", C))
    print(col("═" * 78, C))

    # ── Phase 0: Evaluate starting params ────────────────────────────────────
    print(f"\n{B}Phase 0: Baseline evaluation{X}")
    t0 = time.time()
    best = _run(START)
    print(f"  start  {_fmt(best)}  ({time.time()-t0:.0f}s)")

    hof = [best]

    def upd(r: dict, label: str) -> bool:
        nonlocal best
        if r["score"] > best["score"]:
            best = r
            print(col(f"  ★ NEW BEST [{label}]  {_fmt(best)}", B + G))
            print(col(f"    Δ from start: {diff(START, best['params'])}", Y))
            return True
        return False

    # ── Phase 1: Biased random search ────────────────────────────────────────
    # Mix of:
    #   - Random perturbations from HOF members
    #   - Scalp-biased directional mutations
    #   - Pure random candidates to avoid local optima
    print(f"\n{B}Phase 1: Biased random search ({rand_iters} iters, {workers} parallel){X}")
    n_batches = math.ceil(rand_iters / workers)

    for b in range(n_batches):
        cands = []
        for i in range(workers):
            r = random.random()
            if r < 0.35 and hof:
                # HOF perturb (moderate mutation)
                cands.append(perturb(random.choice(hof[:5])["params"], random.randint(2, 5)))
            elif r < 0.55 and hof:
                # Scalp-biased: push toward fast exit / strict entry
                cands.append(scalp_bias(random.choice(hof[:3])["params"]))
            elif r < 0.75 and hof:
                # Heavy mutation from best
                cands.append(perturb(hof[0]["params"], random.randint(5, 9)))
            else:
                # Pure random from baseline (exploration)
                cands.append(perturb(START, random.randint(4, 8)))

        t1 = time.time()
        batch = eval_batch(cands, workers)
        bst = max(batch, key=lambda r: r["score"])
        imp = upd(bst, f"r{b+1}")
        maintain_hof(hof, batch)

        if not imp:
            print(f"  b{b+1:3d}/{n_batches}  {_fmt(bst)}  ({time.time()-t1:.0f}s)")

        # Early exit if target met
        if best["wr"] >= 70 and best["trades"] >= max(40, len(completed) * 0.5):
            print(col(f"\n  ✓ Target reached at batch {b+1}!", B + G))
            break

    # ── Phase 2: Directed hill-climb ─────────────────────────────────────────
    # Fine-tune the best found params with small steps.
    # Also inject occasional scalp-bias nudges to prevent drift.
    print(f"\n{B}Phase 2: Hill-climb + scalp nudges ({hill_iters} iters){X}")
    no_imp = 0
    n_hill_batches = math.ceil(hill_iters / workers)

    for b in range(n_hill_batches):
        # Cooling schedule: start with larger steps, refine over time
        scale = max(0.2, 1.0 - b / max(1, n_hill_batches))

        cands = []
        for i in range(workers):
            if i < workers // 2:
                # Standard hill-climb: small neighbour steps
                base = random.choice(hof[:3])["params"]
                cands.append(neighbour(base, random.randint(1, 3), scale))
            else:
                # Scalp-biased nudge every other slot
                cands.append(scalp_bias(random.choice(hof[:2])["params"]))

        t1 = time.time()
        batch = eval_batch(cands, workers)
        bst = max(batch, key=lambda r: r["score"])
        imp = upd(bst, f"h{b+1}")
        maintain_hof(hof, batch)

        if imp:
            no_imp = 0
        else:
            no_imp += 1
            print(f"  h{b+1:3d}  {_fmt(bst)}  ({time.time()-t1:.0f}s)")
            if no_imp >= 6:
                print(col("  Converged.", D))
                break

    # ── Final report ──────────────────────────────────────────────────────────
    print(col("\n" + "═" * 78, C))
    print(col("  FINAL RESULTS", B + C))
    print(col("═" * 78, C))

    if _prev:
        prev_r = {
            "wr": _prev.get("win_rate", 0), "trades": _prev.get("total_trades", 0),
            "pnl": _prev.get("total_pnl", 0), "coins": 0,
            "total": len(completed), "score": _prev.get("score", 0),
        }
        print(f"  Previous : {_fmt(prev_r)}")
    print(f"  Best     : {_fmt(best)}")

    # Goal check
    wr_ok     = best["wr"] >= 70
    trade_ok  = best["trades"] >= max(40, len(completed) * 0.5)
    all_ok    = wr_ok and trade_ok

    status = col("  ✓ ALL TARGETS MET!", B + G) if all_ok else (
        "  ✗ Gaps:"
        + (f" WR={best['wr']:.1f}%<70%" if not wr_ok else "")
        + (f" trades={best['trades']}<max(40, 50% coins)" if not trade_ok else "")
    )
    print(col(status, G + B if all_ok else Y))

    # Show what changed vs baseline
    changed = {k: v for k, v in best["params"].items()
               if best["params"].get(k) != _BASELINE.get(k)}
    print(f"\n  {B}Changed vs baseline:{X}")
    for k, v in sorted(changed.items()):
        direction = ""
        if k in SPACE:
            lo, hi, _, _ = SPACE[k]
            mid = (lo + hi) / 2
            direction = col(" ↑", G) if v > mid else col(" ↓", Y)
        print(f"    {k:<40s}  {_BASELINE.get(k)} → {col(str(v), B+G)}{direction}")

    # Save best
    out = {
        "params": best["params"],
        "win_rate": best["wr"],
        "total_trades": best["trades"],
        "total_pnl": best["pnl"],
        "score": best["score"],
    }
    with open(_best_json, "w") as f:
        json.dump(out, f, indent=2)
    print(col(f"\n  Saved → {_best_json}", C))

    # Top 5 HOF
    print(f"\n  {B}Top 5 Hall of Fame:{X}")
    for i, r in enumerate(hof[:5]):
        print(f"  #{i+1} {_fmt(r)}")

    return best


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Precision Scalp Tuner v4")
    p.add_argument("--workers", type=int, default=6,
                   help="Parallel evaluation workers (default: 6)")
    p.add_argument("--iters", type=int, default=80,
                   help="Random search iterations (default: 80)")
    p.add_argument("--hill", type=int, default=40,
                   help="Hill-climb iterations (default: 40)")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed (default: 42)")
    a = p.parse_args()
    run(workers=a.workers, rand_iters=a.iters, hill_iters=a.hill, seed=a.seed)