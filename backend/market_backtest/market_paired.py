"""market_paired.py — paired winner-vs-baseline significance on market tapes.

Mirrors the memecoin accept gate on the market backtest:
  * per-symbol test-segment PnL diff (candidate − baseline),
  * Wilcoxon signed-rank p on the per-symbol diffs,
  * 10k-bootstrap 95% CI on the mean diff,
  * breadth (share of symbols improved) + binomial p under p=0.5.

Usage:
    python -m market_backtest.market_paired \
        --cand-dir market_backtest/results/win1 --cand-label win1 \
        --market stocks --interval 1d
Baseline defaults to the root results dir with label "".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from market_backtest.stats_report import (
    load_results, _wilcoxon, _bootstrap_ci, _binom_p)

RESULTS_BASE = Path("market_backtest/results")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cand-dir", required=True)
    ap.add_argument("--cand-label", default="")
    ap.add_argument("--base-dir", default=str(RESULTS_BASE))
    ap.add_argument("--base-label", default="")
    ap.add_argument("--market", required=True)
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--seg", default="test", choices=["test", "train", "all"])
    args = ap.parse_args()

    base = {r["symbol"]: r for r in load_results(
        Path(args.base_dir), args.market, label=args.base_label,
        interval=args.interval)}
    cand = {r["symbol"]: r for r in load_results(
        Path(args.cand_dir), args.market, label=args.cand_label,
        interval=args.interval)}
    syms = sorted(set(base) & set(cand))
    if not syms:
        print("[paired] no overlapping symbols")
        return

    diffs, tot_b, tot_c, nb, nc = [], 0.0, 0.0, 0, 0
    for s in syms:
        if args.seg == "all":
            b = base[s]["stats"]["total_pnl"]
            c = cand[s]["stats"]["total_pnl"]
            nb += base[s]["stats"]["trades"]
            nc += cand[s]["stats"]["trades"]
        else:
            b = base[s][f"stats_{args.seg}"]["total_pnl"]
            c = cand[s][f"stats_{args.seg}"]["total_pnl"]
            nb += base[s][f"stats_{args.seg}"]["trades"]
            nc += cand[s][f"stats_{args.seg}"]["trades"]
        diffs.append(c - b)
        tot_b += b
        tot_c += c

    n = len(diffs)
    mean_d = sum(diffs) / n
    pos = sum(1 for d in diffs if d > 0)
    w_stat, w_p = _wilcoxon(diffs)
    ci_lo, ci_hi = _bootstrap_ci(diffs, n=10000)
    binom_p = _binom_p(pos, n)
    print(f"[paired] {args.market} {args.interval} seg={args.seg}  "
          f"symbols={n}  base n={nb} Σ={tot_b:+.4f}  cand n={nc} Σ={tot_c:+.4f}")
    print(f"  mean per-symbol Δ = {mean_d:+.5f}  (median {sorted(diffs)[n // 2]:+.5f})")
    print(f"  Wilcoxon p = {w_p:.3g}  (W={w_stat:.1f})")
    print(f"  bootstrap 95% CI of mean Δ = [{ci_lo:+.5f}, {ci_hi:+.5f}]")
    print(f"  breadth (improved) = {pos}/{n} = {100 * pos / n:.0f}%  "
          f"(binom p={binom_p:.3g})")


if __name__ == "__main__":
    main()
