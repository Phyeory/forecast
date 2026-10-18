"""prop_frontier.py — how far is the current test tape from prop-viability?

Two clean bounds on top of the committed prop machinery:

  (A) MEAN-SHIFT frontier: every day's MTM PnL fraction gets +c (the most
      generous possible interpretation of "the calibration improves
      expectancy by c/day") — how large must c be before any (k, phase)
      cell reaches P(pass) >= 50 %?  This converts "profitable in big
      markets" into a number the calibration layer could in principle hit.

  (B) DAILY-CAP frontier: each day's MTM PnL fraction is floored at
      -cap/100 (a *perfect* daily stop — a bound no calibration can
      literally implement in this engine, since the V1 stop family is dead
      code) — how much of the breach risk survives even with a perfect
      daily stop?

Both act on the same block-bootstrap MC as stats_report.py, so numbers are
directly comparable to the committed prop tables.

Usage:
    python -m market_backtest.prop_frontier --market stocks --interval 1d
    python -m market_backtest.prop_frontier --market crypto_perp --interval 1h \
        --label <label>
"""

from __future__ import annotations

import argparse
from pathlib import Path

from market_backtest.stats_report import (
    PROP, load_results, mtm_daily_equity, ledger_daily_equity,
    _block_bootstrap_days, _challenge_path, funded_expectancy,
)

RESULTS_BASE = Path("market_backtest/results")


def _mc(transform, base_daily, k, target, max_days, n_sims=1500, seed=7):
    """Block-bootstrap MC over a transformed daily series (lists in, counts out).

    `transform(r, k)` maps a raw daily PnL fraction (backtest sizing) to the
    doctored fraction at sizing k.  The pass/fail rule then applies at k=1.
    """
    from collections import defaultdict
    import random
    daily = [(d, transform(r, k)) for d, r in base_daily]
    counts = defaultdict(int)
    rng = random.Random(seed)
    for _ in range(n_sims):
        sim = _block_bootstrap_days(daily, n_days=max_days or 60, block=5,
                                    seed=rng.randrange(1 << 30))
        res = _challenge_path(sim, 1.0, target, max_days=max_days)
        counts[res] += 1
    tot = max(1, sum(counts.values()))
    return {r: counts[r] / tot for r in ("pass", "fail_daily", "fail_total",
                                         "timeout")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True,
                    choices=["stocks", "crypto_perp"])
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--label", default="")
    ap.add_argument("--n-sims", type=int, default=1500)
    ap.add_argument("--series", default="ledger", choices=["ledger", "snap"])
    args = ap.parse_args()

    d = RESULTS_BASE / args.label if args.label else RESULTS_BASE
    rows = load_results(d, args.market, label=args.label, interval=args.interval)
    if not rows:
        print(f"[frontier] no results in {d} for {args.market} {args.interval}")
        return
    days_idx, eq = (ledger_daily_equity if args.series == "ledger"
                    else mtm_daily_equity)(rows, "test")
    base_daily = []
    prev = eq[0] if eq else 1.0
    for i, e in enumerate(eq[1:], start=1):
        base_daily.append((i, e - prev))
        prev = e
    n = len(base_daily)
    mu_day = sum(r for _, r in base_daily) / max(1, n)
    print(f"[frontier] {args.market} {args.interval} label={args.label or '-'} "
          f"days={n} mu={mu_day * 100:+.4f}%/day "
          f"target P1 {PROP['phase1_target_pct']}% / P2 "
          f"{PROP['phase2_target_pct']}%")

    # ── (A) mean-shift frontier ────────────────────────────────────────────
    ks = (0.5, 1.0, 2.0, 3.0, 5.0)
    shifts = (0.0, 0.0005, 0.001, 0.002, 0.004, 0.008)
    print("\n(A) MEAN-SHIFT frontier — P1-pass(60d)   [rows: k, cols: +c/day]")
    hdr = "   k  " + "".join(f"{c * 100:>8.2f}%" for c in shifts)
    print(hdr)
    for k in ks:
        line = f"{k:>5.2f}"
        for c in shifts:
            r = _mc(lambda r_, k_, c=c: k_ * r_ + c, base_daily, k,
                    PROP["phase1_target_pct"], 60, n_sims=args.n_sims)
            line += f"{r['pass'] * 100:>9.1f}"
        print(line)

    # ── (B) daily-cap frontier ─────────────────────────────────────────────
    caps = (None, 0.03, 0.02, 0.015, 0.01)
    print("\n(B) PERFECT-DAILY-STOP frontier — P1-pass(60d)  [cols: floor at -cap]")
    hdr = "   k  " + "".join(f"{(str(c * 100) + '%') if c else 'none':>9}"
                             for c in caps)
    print(hdr)
    for k in ks:
        line = f"{k:>5.2f}"
        for c in caps:
            if c is None:
                f = lambda r_, k_: k_ * r_
            else:
                f = lambda r_, k_, c=c: max(k_ * r_, -c)
            r = _mc(f, base_daily, k, PROP["phase1_target_pct"], 60,
                    n_sims=args.n_sims)
            line += f"{r['pass'] * 100:>9.1f}"
        print(line)

    # ── funded survival at the pass-optimal cells ─────────────────────────
    print("\nFUNDED 1y survival (p_blown) at k = 1 / 2 / 3 (no doctoring):")
    for k in (1.0, 2.0, 3.0):
        fe = funded_expectancy(base_daily, k, n_sims=1000)
        print(f"  k={k:.2f}  p_blown={fe['p_blown'] * 100:.1f}%  "
              f"median ret {fe['median_return_pct']:+.2f}%  "
              f"μ ret {fe['mean_return_pct']:+.2f}%")


if __name__ == "__main__":
    main()
