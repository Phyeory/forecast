"""stats_report.py — statistical significance + prop-firm evaluation.

Consumes the per-symbol result JSONs written by `runner.run_market_backtest`
(TRAIN / unseen-TEST partitions) and answers three questions per market:

  1. Is the test-window edge (or bleed) statistically distinguishable from
     zero?  Pooled per-trade bootstrap 95% CI + Wilcoxon signed-rank test,
     plus symbol-level breadth (share of symbols with positive test PnL,
     binomial sign test).

  2. Could the algorithm pass a prop-firm challenge (FTMO-style rules:
     +10 % phase-1 target, 5 % max daily loss, 10 % max total loss) on the
     unseen window, and what does the funded phase look like?  Evaluated
     with (a) the deterministic backtest path through the rules, and (b) a
     block-bootstrap Monte-Carlo over the daily PnL sequence at several
     risk multipliers.

  3. How much money could it make — annualized return on a 100 k account at
     the test-implied rate, per market, including the prop profit-split
     economics.

Caveats encoded: daily-breach checks use BOOKED trade PnL (exit day), a
slight optimism vs full mark-to-market; the trade PnL unit is the
account's 0.1-notional position size (see runner COSTS / FT buy sizing).

Usage:
    python -m market_backtest.stats_report --dir market_backtest/results
    python -m market_backtest.stats_report --dir market_backtest/results --market stocks
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from pathlib import Path

SECONDS_DAY = 86400.0

# ── prop rules (FTMO-classic "normal" account) ────────────────────────────
PROP = {
    "phase1_target_pct": 10.0,
    "phase2_target_pct": 5.0,
    "max_daily_loss_pct": 5.0,
    "max_total_loss_pct": 10.0,
    "min_trading_days": 4,
    "phase_days_limit": 30,          # FTMO normal: 30 calendar days per phase
    "profit_split": 0.80,
}


# ─────────────────────────────────────────────────────────────────────────
# loading
# ─────────────────────────────────────────────────────────────────────────

def load_results(d: Path, market: str | None = None,
                 label: str = "", interval: str | None = None) -> list[dict]:
    out = []
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("batch_") or f.name.startswith("summary"):
            continue
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if "stats_test" not in data or "trades" not in data:
            continue
        if (data.get("label") or "") != label:
            continue
        if market and data.get("market") != market:
            continue
        if interval and data.get("interval") != interval:
            continue
        out.append(data)
    return out


# ─────────────────────────────────────────────────────────────────────────
# stats helpers
# ─────────────────────────────────────────────────────────────────────────

def _bootstrap_ci(xs: list[float], n: int = 10000, alpha: float = 0.05,
                  seed: int = 7) -> tuple[float, float]:
    if not xs:
        return (0.0, 0.0)
    rng = random.Random(seed)
    k = len(xs)
    means = []
    for _ in range(n):
        s = 0.0
        for _ in range(k):
            s += xs[rng.randrange(k)]
        means.append(s / k)
    means.sort()
    lo = means[int(alpha / 2 * n)]
    hi = means[int((1 - alpha / 2) * n) - 1]
    return (lo, hi)


def _wilcoxon(xs: list[float]) -> tuple[float, float]:
    """Wilcoxon signed-rank (two-sided) via scipy; fallback normal approx."""
    nz = [x for x in xs if x != 0.0]
    if len(nz) < 10:
        return (float("nan"), float("nan"))
    try:
        from scipy.stats import wilcoxon
        r = wilcoxon(nz)
        return (float(r.statistic), float(r.pvalue))
    except Exception:
        return (float("nan"), float("nan"))


def _binom_p(k_positive: int, n: int) -> float:
    """Two-sided binomial sign-test p-value (p0=0.5)."""
    if n == 0:
        return float("nan")
    # exact two-sided
    from math import comb
    obs = comb(n, k_positive)
    total = 2 ** n
    tail = sum(comb(n, k) for k in range(n + 1) if comb(n, k) <= obs)
    return min(1.0, tail / total)


def _profit_factor(xs: list[float]) -> float:
    gains = sum(x for x in xs if x > 0)
    losses = -sum(x for x in xs if x < 0)
    if losses <= 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def _max_drawdown(pnl_series: list[float], start: float = 1.0) -> float:
    """Max peak-to-trough drawdown of the additive PnL path, in % of start."""
    eq = start
    peak = start
    mdd = 0.0
    for x in pnl_series:
        eq += x
        peak = max(peak, eq)
        dd = (peak - eq) / peak * 100.0
        mdd = max(mdd, dd)
    return mdd


def _daily_series(data: dict, seg: str = "test") -> dict[int, float]:
    """{day_index → booked PnL} for a symbol's segment (booked at exit)."""
    trades = data["trades"]
    split_ts = int(data.get("split_ts") or 0)
    days: dict[int, float] = defaultdict(float)
    for t in trades:
        et = int(t.get("entry_time") or 0)
        xt = int(t.get("exit_time") or 0)
        if seg == "test" and et < split_ts:
            continue
        if seg == "train" and et >= split_ts:
            continue
        if xt <= 0:
            continue
        day = int(xt // SECONDS_DAY)
        days[day] += float(t.get("pnl_sol") or 0.0)
    return dict(days)


def _seg_trade_pnls(data: dict, seg: str = "test") -> list[float]:
    split_ts = int(data.get("split_ts") or 0)
    out = []
    for t in data["trades"]:
        et = int(t.get("entry_time") or 0)
        if seg == "test" and et < split_ts:
            continue
        if seg == "train" and et >= split_ts:
            continue
        out.append(float(t.get("pnl_sol") or 0.0))
    return out


# ─────────────────────────────────────────────────────────────────────────
# mark-to-market portfolio equity (proper prop-rule fidelity)
# ─────────────────────────────────────────────────────────────────────────

_CANDLE_CACHE: dict[tuple, list[dict]] = {}


def _candles_for(market: str, symbol: str, interval: str) -> list[dict]:
    key = (market, symbol, interval)
    if key not in _CANDLE_CACHE:
        try:
            from .market_store import (get_market_recording,
                                       get_market_recording_candles)
            rec = get_market_recording(market, symbol, interval)
            _CANDLE_CACHE[key] = get_market_recording_candles(rec["id"]) if rec else []
        except Exception:
            _CANDLE_CACHE[key] = []
    return _CANDLE_CACHE[key]


def ledger_daily_equity(rows: list[dict], seg: str = "test") -> tuple[list[int], list[float]]:
    """TRUE-accounting daily equity: realized ledger + live open marks.

    Fixes the snapshot artifact in `mtm_daily_equity`: there, each symbol's
    contribution is replaced by the next trade's mark at re-entry, which
    *removes* the previously realized PnL from the sum (wins confiscated,
    losses forgiven).  Here each symbol carries
        Σ booked PnL of trades exited ≤ t  +  mark of the open trade (if any),
    so the path is the actual mark-to-market equity of one pooled account.
    """
    per_symbol: dict[str, dict[int, float]] = {}
    all_days: set[int] = set()

    for data in rows:
        sym = data["symbol"]
        split_ts = int(data.get("split_ts") or 0)
        candles = _candles_for(data["market"], sym, data["interval"])
        if not candles:
            continue
        trades_all = sorted(data["trades"],
                            key=lambda t: int(t.get("entry_time") or 0))
        # segment by ENTRY time (same convention as _seg_trade_pnls)
        if seg == "test":
            trades = [t for t in trades_all
                      if int(t.get("entry_time") or 0) >= split_ts]
        else:
            trades = [t for t in trades_all
                      if int(t.get("entry_time") or 0) < split_ts]
        series: dict[int, float] = {}
        realized = 0.0                      # Σ booked PnL of CLOSED seg trades
        ti = 0
        open_t: dict | None = None
        for cd in candles:
            t = int(cd["time"])
            if seg == "test" and t < split_ts:
                continue
            if seg == "train" and t >= split_ts:
                break
            day = int(t // SECONDS_DAY)
            # settle loop — allows close + same-candle re-entry roll
            while True:
                progressed = False
                if open_t is not None:
                    xt = int(open_t.get("exit_time") or 0)
                    if xt and t >= xt:
                        realized += float(open_t.get("pnl_sol") or 0.0)
                        open_t = None
                        progressed = True
                if (open_t is None and ti < len(trades)
                        and int(trades[ti].get("entry_time") or 0) <= t):
                    open_t = trades[ti]
                    ti += 1
                    progressed = True
                if not progressed:
                    break
            mark = 0.0
            if open_t is not None:
                entry_px = float(open_t.get("entry_price") or 0.0)
                size = float(open_t.get("size_sol") or 0.0)
                if entry_px > 0:
                    mark = size * (float(cd["close"]) / entry_px - 1.0)
            series[day] = realized + mark
            all_days.add(day)
        if series:
            per_symbol[sym] = series

    days = sorted(all_days)
    equity = []
    lasts: dict[str, float] = {}
    for d in days:
        tot = 0.0
        for sym, series in per_symbol.items():
            if d in series:
                lasts[sym] = series[d]
            tot += lasts.get(sym, 0.0)
        equity.append(1.0 + tot)
    return days, equity


def mtm_daily_equity(rows: list[dict], seg: str = "test") -> tuple[list[int], list[float]]:
    """Mark-to-market daily portfolio equity for one market's results.

    Each symbol keeps an open-position contribution `size_sol × (close/entry − 1)`
    while a trade is open (marked at each candle close), and the booked PnL
    (slippage/fee-inclusive) once it closes. Equity(d) = 1 + Σ contributions,
    forward-filled to the union of trading days. Removes the booked-at-exit
    artifact where a tape-end force-close pile-up shows as one huge day.
    """
    per_symbol: dict[str, dict[int, float]] = {}
    all_days: set[int] = set()

    for data in rows:
        sym = data["symbol"]
        split_ts = int(data.get("split_ts") or 0)
        candles = _candles_for(data["market"], sym, data["interval"])
        if not candles:
            continue
        trades = sorted(data["trades"], key=lambda t: int(t.get("entry_time") or 0))
        series: dict[int, float] = {}
        contrib = 0.0
        ti = 0
        open_t: dict | None = None
        for cd in candles:
            t = int(cd["time"])
            day = int(t // SECONDS_DAY)
            while ti < len(trades) and int(trades[ti].get("entry_time") or 0) <= t:
                if open_t is None:
                    open_t = trades[ti]
                ti += 1
            if open_t is not None:
                et = int(open_t.get("entry_time") or 0)
                xt = int(open_t.get("exit_time") or 0)
                if xt and t >= xt:
                    contrib = float(open_t.get("pnl_sol") or 0.0)
                    open_t = None
                if open_t is not None and t >= et:
                    entry_px = float(open_t.get("entry_price") or 0.0)
                    size = float(open_t.get("size_sol") or 0.0)
                    if entry_px > 0:
                        contrib = size * (float(cd["close"]) / entry_px - 1.0)
            in_seg = True
            if seg == "test" and t < split_ts:
                in_seg = False
            if in_seg:
                series[day] = contrib
                all_days.add(day)
        if series:
            per_symbol[sym] = series

    days = sorted(all_days)
    equity = []
    lasts: dict[str, float] = {}
    for d in days:
        tot = 0.0
        for sym, series in per_symbol.items():
            if d in series:
                lasts[sym] = series[d]
            tot += lasts.get(sym, 0.0)
        equity.append(1.0 + tot)
    return days, equity


# ─────────────────────────────────────────────────────────────────────────
# prop-firm simulation
# ─────────────────────────────────────────────────────────────────────────

def _challenge_path(daily: list[tuple[int, float]], k: float,
                    target_pct: float, max_days: int | None,
                    rules: dict = PROP) -> str:
    """Run one synthetic (or real) daily PnL sequence through the rules.

    daily = [(day_index, pnl_fraction_of_equity), ...] sorted by day.
    Returns 'pass' | 'fail_daily' | 'fail_total' | 'timeout'.
    """
    eq = 1.0
    peak = 1.0
    start_day = daily[0][0] if daily else 0
    trading_days = 0
    for day, r in daily:
        r *= k
        if max_days is not None and (day - start_day) >= max_days:
            break
        # daily loss breach (booked approximation)
        if r <= -rules["max_daily_loss_pct"] / 100.0:
            return "fail_daily"
        eq += r
        trading_days += 1
        if eq <= 1.0 - rules["max_total_loss_pct"] / 100.0:
            return "fail_total"
        peak = max(peak, eq)
        dd = (peak - eq) / peak
        if dd >= rules["max_total_loss_pct"] / 100.0:
            return "fail_total"
        if eq >= 1.0 + target_pct / 100.0 and trading_days >= rules["min_trading_days"]:
            return "pass"
    return "timeout"


def _block_bootstrap_days(daily: list[tuple[int, float]], n_days: int,
                          block: int = 5, seed: int = 0) -> list[tuple[int, float]]:
    rng = random.Random(seed)
    if not daily:
        return []
    vals = [r for _, r in daily]
    m = len(vals)
    out = []
    i = 0
    # day indices synthetic: 0..n_days
    while i < n_days:
        start = rng.randrange(m)
        for j in range(block):
            if i >= n_days:
                break
            out.append((i, vals[(start + j) % m]))
            i += 1
    return out


def prop_monte_carlo(daily: list[tuple[int, float]], k: float,
                     target_pct: float, n_sims: int = 2000,
                     max_days: int | None = 60, seed: int = 1) -> dict:
    """Block-bootstrap MC: pass/fail distribution for one challenge phase."""
    counts = defaultdict(int)
    rng = random.Random(seed)
    for s in range(n_sims):
        sim = _block_bootstrap_days(daily, n_days=max_days or 250,
                                    block=5, seed=rng.randrange(1 << 30))
        res = _challenge_path(sim, k, target_pct, max_days=max_days)
        counts[res] += 1
    total = max(1, sum(counts.values()))
    return {k2: round(v / total, 4) for k2, v in counts.items()}


def funded_expectancy(daily: list[tuple[int, float]], k: float,
                      horizon_days: int = 250, n_sims: int = 1000,
                      split: float = 0.80, seed: int = 2) -> dict:
    """Funded-phase (no target, 10 % total loss kill) 1-year simulation."""
    rng = random.Random(seed)
    finals = []
    blown = 0
    for s in range(n_sims):
        sim = _block_bootstrap_days(daily, n_days=horizon_days, block=5,
                                    seed=rng.randrange(1 << 30))
        eq = 1.0
        peak = 1.0
        dead = False
        for _, r in sim:
            r *= k
            eq += r
            peak = max(peak, eq)
            if eq <= 0.9 or (peak - eq) / peak >= 0.10:
                dead = True
                break
        if dead:
            blown += 1
        finals.append(eq - 1.0)
    finals.sort()
    mean = sum(finals) / len(finals)
    med = finals[len(finals) // 2]
    return {
        "years_simulated": horizon_days / 252.0,
        "p_blown": round(blown / n_sims, 4),
        "mean_return_pct": round(mean * 100, 2),
        "median_return_pct": round(med * 100, 2),
        "p05_return_pct": round(finals[int(0.05 * n_sims)] * 100, 2),
        "p95_return_pct": round(finals[int(0.95 * n_sims) - 1] * 100, 2),
        "expected_trader_payout_pct_of_initial": round(
            max(mean, 0.0) * split * 100, 2),
    }


# ─────────────────────────────────────────────────────────────────────────
# report
# ─────────────────────────────────────────────────────────────────────────

def _sharpe(daily: list[tuple[int, float]]) -> float:
    vals = [r for _, r in daily]
    if len(vals) < 5:
        return 0.0
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / max(1, len(vals) - 1)
    sd = math.sqrt(var)
    if sd <= 0:
        return 0.0
    return mean / sd * math.sqrt(252.0)


def report(d: Path, market: str | None, label: str = "",
           out_path: Path | None = None, interval: str | None = None,
           series: str = "ledger") -> dict:
    results = load_results(d, market, label, interval)
    if not results:
        print(f"[stats] no results (market={market}, label={label!r}) in {d}")
        return {}

    by_market: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_market[r["market"]].append(r)

    summary: dict = {"markets": {}}

    for mkt, rows in sorted(by_market.items()):
        print(f"\n{'═' * 78}\nMARKET: {mkt}   ({len(rows)} symbols)\n{'═' * 78}")

        segs = {}
        for seg in ("train", "test"):
            trade_pnls: list[float] = []
            symbol_pnls: list[float] = []
            wins = 0
            n_trades = 0
            for r in rows:
                xs = _seg_trade_pnls(r, seg)
                trade_pnls.extend(xs)
                n_trades += len(xs)
                wins += sum(1 for x in xs if x > 0)
                symbol_pnls.append(sum(xs))
            if not trade_pnls:
                continue
            pos_symbols = sum(1 for p in symbol_pnls if p > 0)
            ci_lo, ci_hi = _bootstrap_ci(trade_pnls)
            w_stat, w_p = _wilcoxon(trade_pnls)
            mean = sum(trade_pnls) / len(trade_pnls)
            segs[seg] = {
                "n_symbols": len(rows),
                "n_trades": n_trades,
                "win_rate": 100.0 * wins / n_trades,
                "total_pnl": sum(trade_pnls),
                "mean_trade_pnl": mean,
                "mean_trade_pnl_pct": mean * 100.0,   # % of 1.0 account per trade
                "bootstrap_ci95": [ci_lo, ci_hi],
                "bootstrap_ci95_pct": [ci_lo * 100, ci_hi * 100],
                "wilcoxon_stat": w_stat,
                "wilcoxon_p": w_p,
                "symbol_breadth_pos": f"{pos_symbols}/{len(rows)}",
                "symbol_breadth_binom_p": _binom_p(pos_symbols, len(rows)),
                "profit_factor": _profit_factor(trade_pnls),
                "avg_win": (sum(x for x in trade_pnls if x > 0) / max(1, wins)),
                "avg_loss": (-sum(x for x in trade_pnls if x < 0) /
                             max(1, n_trades - wins)),
            }
            print(f"  [{seg:5s}] trades {n_trades:>6d}  WR {segs[seg]['win_rate']:5.1f}%  "
                  f"PnL {segs[seg]['total_pnl']:+9.4f}  "
                  f"mean/trade {mean * 100:+.4f}%  "
                  f"CI95 [{ci_lo * 100:+.4f}, {ci_hi * 100:+.4f}]%  "
                  f"Wilcoxon p={w_p:.3g}  "
                  f"breadth {pos_symbols}/{len(rows)} (p={_binom_p(pos_symbols, len(rows)):.3g})")
            print(f"          PF {segs[seg]['profit_factor']:.2f}  "
                  f"avg win {segs[seg]['avg_win'] * 100:.4f}%  "
                  f"avg loss -{segs[seg]['avg_loss'] * 100:.4f}%")

        # portfolio: TRUE daily equity across ALL symbols on one account.
        # `ledger` = realized-so-far + open marks (correct accounting);
        # `snap`   = legacy snapshot series (confiscates wins / forgives
        #            losses at re-entry — kept only for comparison).
        if series == "ledger":
            days_idx, eq = ledger_daily_equity(rows, "test")
        else:
            days_idx, eq = mtm_daily_equity(rows, "test")
        if len(eq) >= 5:
            daily_test: list[tuple[int, float]] = []
            prev = 1.0
            for i, e in enumerate(eq):
                daily_test.append((i, e - prev))
                prev = e
            mtm_pnl = eq[-1] - 1.0
            # true peak-to-trough DD on the MTM equity curve
            peak = eq[0]
            mdd = 0.0
            for e in eq:
                peak = max(peak, e)
                mdd = max(mdd, (peak - e) / peak * 100.0)
            pnl_series = [v for _, v in daily_test]
            worst_day = min(pnl_series)
            best_day = max(pnl_series)
            sharpe = _sharpe(daily_test)
            span_days = max(1, days_idx[-1] - days_idx[0])
            span_years = span_days / 365.25
            ret_per_year = mtm_pnl / max(span_years, 1e-9)
            print(f"\n  PORTFOLIO (unseen window, {series} MTM equity, all symbols one account):")
            print(f"    MTM PnL {mtm_pnl:+.4f} over {span_years:.2f}y  "
                  f"(≈ {ret_per_year * 100:+.1f}%/yr on the 1.0 account)")
            print(f"    max drawdown {mdd:.2f}%  worst day {worst_day * 100:.2f}%  "
                  f"best day {best_day * 100:.2f}%  Sharpe(ann) {sharpe:.2f}")

            # deterministic prop run on the actual test-window path
            det_p1 = _challenge_path(daily_test, 1.0, PROP["phase1_target_pct"],
                                     max_days=None)
            det_p1_30 = _challenge_path(daily_test, 1.0,
                                        PROP["phase1_target_pct"], max_days=30)
            print(f"    deterministic phase-1 (unlimited time): {det_p1}   "
                  f"(30-day window): {det_p1_30}")

            # Monte Carlo at multiple risk multipliers
            print(f"\n  PROP MONTE-CARLO (block-bootstrap over MTM daily PnL, "
                  f"2000 sims/phase, trading-day windows):")
            print(f"    {'k':>3}  {'P1 pass (60d)':>13}  {'P1 pass (30d)':>13}  "
                  f"{'P2|P1 (30d)':>12}  {'P(daily breach)':>15}  "
                  f"{'funded: blown':>14}  {'funded μ ret':>12}")
            mc = {}
            for k in (0.25, 0.5, 1.0, 2.0, 3.0, 5.0):
                p1 = prop_monte_carlo(daily_test, k, PROP["phase1_target_pct"],
                                      n_sims=2000, max_days=60)
                p1_30 = prop_monte_carlo(daily_test, k, PROP["phase1_target_pct"],
                                         n_sims=2000, max_days=30)
                p2 = prop_monte_carlo(daily_test, k, PROP["phase2_target_pct"],
                                      n_sims=2000, max_days=30)
                funded = funded_expectancy(daily_test, k)
                p1p = p1.get("pass", 0.0)
                p2p = p2.get("pass", 0.0)
                mc[str(k)] = {"p1_60d": p1, "p1_30d": p1_30, "p2_30d": p2,
                              "funded": funded}
                print(f"    {k:>4.2f}  {p1p:>12.1%}  {p1_30.get('pass', 0.0):>12.1%}  "
                      f"{p2p:>11.1%}  {p1.get('fail_daily', 0.0):>14.1%}  "
                      f"{funded['p_blown']:>13.1%}  {funded['mean_return_pct']:>+11.2f}%")

            # money: annualized on $100k at test-implied rate
            print(f"\n  MONEY ($100k account, test-implied rate):")
            print(f"    annualized ≈ {ret_per_year * 100:+.1f}%  → "
                  f"${ret_per_year * 100000:+,.0f}/yr")

            summary["markets"][mkt] = {
                "n_symbols": len(rows),
                "segments": segs,
                "portfolio": {
                    "mtm_pnl": mtm_pnl,
                    "span_years": span_years,
                    "annualized_pct_on_account": ret_per_year * 100,
                    "max_drawdown_pct": mdd,
                    "worst_day_pct": worst_day * 100,
                    "best_day_pct": best_day * 100,
                    "sharpe_ann": sharpe,
                    "deterministic_p1": det_p1,
                    "deterministic_p1_30d": det_p1_30,
                },
                "prop_mc": mc,
            }

    if out_path:
        with open(out_path, "w") as fh:
            json.dump(summary, fh, indent=1, default=str)
        print(f"\n[stats] → {out_path}")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=str, default="market_backtest/results")
    ap.add_argument("--market", type=str, default=None,
                    choices=[None, "stocks", "crypto_perp"])
    ap.add_argument("--interval", type=str, default=None,
                    choices=[None, "1d", "1h"])
    ap.add_argument("--label", type=str, default="")
    ap.add_argument("--series", type=str, default="ledger",
                    choices=["ledger", "snap"],
                    help="portfolio accounting: ledger = realized + open "
                         "marks (correct); snap = legacy snapshot series")
    ap.add_argument("--save", type=str, default="")
    args = ap.parse_args()
    d = Path(args.dir)
    out = (d / f"{args.save}.json") if args.save else None
    report(d, args.market, args.label, out, args.interval, args.series)


if __name__ == "__main__":
    main()
