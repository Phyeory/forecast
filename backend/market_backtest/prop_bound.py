"""prop_bound.py — prop-firm bound under a PERFECT trade-level stop.

The engine cannot implement a stop (the V1 stop family is dead code; every
live exit is geometry-gated), so a perfect per-trade stop is an UPPER BOUND
on what any stop-like calibration could contribute.  This script:

  1. loads per-symbol test trades,
  2. rebuilds a BOOKED daily PnL series from trade exit days with each
     trade's loss floored at -cap (cap in account fraction; price cap =
     cap/0.1 at the 0.1/1.0 sizing),
  3. runs the committed prop machinery (P1 60d / 30d, funded 1y) at a k grid.

Booked series (vs mark-to-market) ignores intra-trade marks — for a
BOUND this is secondary: stopped trades exit fast; the only mismatch is
un-stopped trades whose intra-trade path dipped below the cap.

Usage:
    python -m market_backtest.prop_bound --market stocks --interval 1d
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from market_backtest.stats_report import (
    PROP, load_results, _block_bootstrap_days, _challenge_path,
    funded_expectancy)

RESULTS_BASE = Path("market_backtest/results")


def booked_daily(rows: list[dict], cap: float | None, seg: str = "test"):
    """Booked daily PnL fractions at 1.0 account sizing, with loss floor."""
    by_day = defaultdict(float)
    for d in rows:
        for t in d.get("trades", []):
            lab = "train" if t["entry_time"] < d.get("split_ts", 0) else "test"
            if lab != seg:
                continue
            p = float(t["pnl_sol"])
            if cap is not None:
                p = max(p, -cap)
            day = int(t["exit_time"] // 86400)
            by_day[day] += p
    days = sorted(by_day)
    return [(d, by_day[d]) for d in days]


def mc(daily, k, target, max_days, n_sims, seed=11):
    import random
    rng = random.Random(seed)
    counts = defaultdict(int)
    for _ in range(n_sims):
        sim = _block_bootstrap_days(daily, n_days=max_days or 60, block=5,
                                    seed=rng.randrange(1 << 30))
        r = _challenge_path(sim, k, target, max_days)
        counts[r] += 1
    tot = max(1, sum(counts.values()))
    return {x: counts[x] / tot for x in counts}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True)
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--label", default="")
    ap.add_argument("--n-sims", type=int, default=1500)
    args = ap.parse_args()

    d = RESULTS_BASE / args.label if args.label else RESULTS_BASE
    rows = load_results(d, args.market, label=args.label, interval=args.interval)
    if not rows:
        print("[bound] no results")
        return

    ks = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0)
    caps = (None, 0.002, 0.005, 0.01)
    print(f"[bound] {args.market} {args.interval} label={args.label or '-'} "
          f"(BOOKED daily, perfect loss cap at trade level)")
    for cap in caps:
        daily = booked_daily(rows, cap)
        mu = sum(r for _, r in daily) / max(1, len(daily))
        tag = f"cap -{cap * 100:.1f}% acct (-{cap * 1000:g}% px)" if cap else "no cap"
        print(f"\n  {tag:<26} days={len(daily)} mu={mu * 100:+.4f}%/day")
        line = "    k  " + "".join(f"{k:>9.2f}" for k in ks)
        print(line)
        row60 = f"  P1-60d "
        row30 = f"  P1-30d "
        for k in ks:
            r = mc(daily, k, PROP["phase1_target_pct"], 60, args.n_sims)
            row60 += f"{r.get('pass', 0) * 100:>9.1f}"
            r2 = mc(daily, k, PROP["phase1_target_pct"], 30, args.n_sims)
            row30 += f"{r2.get('pass', 0) * 100:>9.1f}"
        print(row60)
        print(row30)
        # funded-phase survivorship on the SAME capped series (1-year sims)
        fb = " ".join(
            f"k={k:g}:{funded_expectancy(daily, k, n_sims=1000)['p_blown'] * 100:>5.1f}%"
            for k in (1.0, 2.0, 3.0))
        fm = " ".join(
            f"k={k:g}:{funded_expectancy(daily, k, n_sims=1000)['mean_return_pct']:>+6.2f}%"
            for k in (1.0, 2.0, 3.0))
        print(f"  funded blown (1y)  {fb}")
        print(f"  funded μ ret       {fm}")


if __name__ == "__main__":
    main()
