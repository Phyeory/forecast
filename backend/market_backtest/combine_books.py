"""combine_books.py — does adding other markets improve the account?

Each `market_scan` run produces a labeled per-market book (one $1 account,
10% notional per position).  This pools several books onto ONE account,
aligning on the epoch-day axis, and reports:

  * per-market contribution and pairwise daily correlation
  * combined CAGR / Sharpe / maxDD / worst day
  * prop MC (FTMO 2-Step) on the combined ledger

Usage:
    python -m market_backtest.combine_books \
        --book stocks=market_backtest/results/arm14 \
        --book intl_etf=market_backtest/results/scan_intl_etf_arm14 \
        --book sector_etf=market_backtest/results/scan_sector_etf_arm14 \
        --label combo_stocks_plus
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from .stats_report import load_results, ledger_daily_equity
from .market_scan import _score


def _book(d: str, market: str, label: str) -> dict[int, float]:
    rows = load_results(Path(d), market, label, "1d")
    if not rows:
        raise SystemExit(f"no rows in {d} for {market}/{label}")
    days, eq = ledger_daily_equity(rows, "test")
    pnl: dict[int, float] = {}
    prev = 1.0
    for i, e in enumerate(eq):
        pnl[int(days[i])] = e - prev
        prev = e
    return pnl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", action="append", required=True,
                    help="market=results_dir")
    ap.add_argument("--label", default="combo")
    ap.add_argument("--prop", action="store_true")
    args = ap.parse_args()

    books: dict[str, dict[int, float]] = {}
    for spec in args.book:
        market, _, d = spec.partition("=")
        books[market] = _book(d, market, "arm14")

    all_days = sorted({d for b in books.values() for d in b})
    print(f"\n[combine] {len(books)} books, {len(all_days)} common days")
    print(f"{'book':<12}{'days':>6}{'Σ pnl':>9}{'mean%':>8}{'Sharpe':>7}{'maxDD%':>8}")

    per_book_series: dict[str, list[float]] = {}
    for m, b in books.items():
        vals = [b.get(d, 0.0) for d in all_days]
        per_book_series[m] = vals
        sd = (sum((v - sum(vals) / len(vals)) ** 2 for v in vals) /
              max(1, len(vals) - 1)) ** 0.5
        mean = sum(vals) / len(vals)
        print(f"{m:<12}{len(b):>6}{sum(vals):>9.3f}{mean*100:>8.3f}"
              f"{(mean/sd*math.sqrt(252)) if sd else 0:>7.2f}"
              f"{_score(list(enumerate(vals)), len(vals)/252)['maxdd_pct']:>8.1f}")

    # correlations
    print("\n[combine] daily-pnl correlation")
    names = list(books)
    print("            " + "".join(f"{n[:9]:>10}" for n in names))
    for a in names:
        row = ""
        for b in names:
            va, vb = per_book_series[a], per_book_series[b]
            ma, mb = sum(va) / len(va), sum(vb) / len(vb)
            cov = sum((x - ma) * (y - mb) for x, y in zip(va, vb))
            sa = sum((x - ma) ** 2 for x in va) ** 0.5
            sb = sum((y - mb) ** 2 for y in vb) ** 0.5
            row += f"{(cov / (sa * sb)) if sa and sb else 0:>10.2f}"
        print(f"{a:<12}" + row)

    combined = [sum(per_book_series[m][i] for m in names)
                for i in range(len(all_days))]
    sc = _score(list(enumerate(combined)), len(all_days) / 252)
    print(f"\n[combine] COMBINED: Σ {sum(combined):+.3f}  "
          f"CAGR {sc['cagr_pct']:+.2f}%  Sharpe {sc['sharpe']:.2f}  "
          f"maxDD {sc['maxdd_pct']:.1f}%  worst day {sc['worst_day_pct']:.2f}%")

    out = {"books": {m: {"sum": sum(v), "days": len(books[m])}
                     for m, v in per_book_series.items()},
           "combined": {**sc, "total_pnl": sum(combined)},
           "n_days": len(all_days)}

    if args.prop:
        try:
            from .mc_firms import TEMPLATES, full_challenge_mc, funded_mc
            rules = TEMPLATES["ftmo_2step"]
            series = list(enumerate(combined))
            prop = {}
            for k in (0.5, 0.75, 1.0, 1.5):
                fc = full_challenge_mc(series, k, rules, 1200)
                fu = funded_mc(series, k, rules, 0.8, 600, reset=True)
                prop[str(k)] = {"p_full": fc["p_full"], "p1": fc["p1"],
                                "blown": fu["p_blown"],
                                "E_payout": fu["mean_payout"]}
                print(f"  prop k={k:<5} pass {fc['p_full']*100:5.1f}%  "
                      f"blown {fu['p_blown']*100:5.1f}%  "
                      f"E payout ${fu['mean_payout']*100000:,.0f}")
            out["prop_ftmo_2step"] = prop
        except Exception as exc:  # noqa: BLE001
            print("prop MC failed:", exc)

    p = Path("market_backtest/results") / f"{args.label}.json"
    p.write_text(json.dumps(out, indent=1, default=str))
    print(f"\n[combine] → {p}")


if __name__ == "__main__":
    main()
