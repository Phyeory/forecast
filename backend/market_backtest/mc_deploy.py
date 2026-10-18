"""mc_deploy.py — deployment-grade Monte Carlo + prop-firm economics for a
labeled market-backtest config (e.g. arm14).

Analyses
  1. Empirical test-window daily PnL profile + cap-safe sizing (k such that even
     the worst historical day stays inside a firm's daily loss cap).
  2. Prop challenge MC (FTMO-classic) with METHOD SENSITIVITY: iid / block
     {5, 10, 21} day bootstraps, k-grid.  Reports P1-60d pass, daily-breach,
     total-breach, timeout.
  3. Full-challenge pass probability (P1 60d -> P2 30d) per k.
  4. Funded-phase economics with monthly payouts: P(blown) within 1y,
     expected trader payouts (USD per 100k notional), payout distribution.
  5. Attempt EV: EV = p_full·(fee_refund + E[payouts]) - fee, per k.
  6. Personal-account frontier (no prop rules): 1y/3y growth distribution,
     max-drawdown distribution, ruin odds, Kelly reference.
  7. Trade-level bootstrap cross-check of (6).

Usage:
  python -m market_backtest.mc_deploy --dir market_backtest/results/arm14 \
      --label arm14 --market stocks --interval 1d \
      --sims 4000 --fee 540 --notional 100000 --split 0.80 ... 
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from market_backtest.stats_report import (
    PROP, load_results, ledger_daily_equity, _seg_trade_pnls,
    _block_bootstrap_days, _challenge_path, _sharpe)


# ─────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────

def _series_stats(daily: list[tuple[int, float]]) -> dict:
    vals = [r for _, r in daily]
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / max(1, n - 1)
    sd = math.sqrt(var)
    return {
        "n_days": n, "mean_pct": mean * 100, "sd_pct": sd * 100,
        "worst_pct": min(vals) * 100, "best_pct": max(vals) * 100,
        "sharpe": _sharpe(daily),
        "kelly_f": (mean / var) if var > 0 else float("inf"),
    }


def _challenge_mc(daily, k, target, n_sims, block, max_days=60, seed=1):
    counts: dict[str, int] = {}
    rng = random.Random(seed)
    for _ in range(n_sims):
        sim = _block_bootstrap_days(daily, n_days=max_days, block=block,
                                    seed=rng.randrange(1 << 30))
        res = _challenge_path(sim, k, target, max_days=max_days)
        counts[res] = counts.get(res, 0) + 1
    return {kk: v / n_sims for kk, v in counts.items()}


def _full_challenge_mc(daily, k, n_sims, block, seed=7):
    """P1 60d (+10%) then P2 30d (+5%), independent block draws."""
    rng = random.Random(seed)
    p1 = p2 = 0
    for s in range(n_sims):
        sim1 = _block_bootstrap_days(daily, n_days=60, block=block,
                                     seed=rng.randrange(1 << 30))
        if _challenge_path(sim1, k, 10.0, max_days=60) != "pass":
            continue
        p1 += 1
        sim2 = _block_bootstrap_days(daily, n_days=30, block=block,
                                     seed=rng.randrange(1 << 30))
        if _challenge_path(sim2, k, 5.0, max_days=30) == "pass":
            p2 += 1
    return p1 / n_sims, (p2 / p1 if p1 else 0.0)


def _funded_payout_mc(daily, k, split, n_sims, block,
                      horizon_days=252, payout_every=21, seed=3, reset=False):
    """Funded phase: static 10% total / 5% daily kill; monthly payouts on
    positive monthly PnL at `split`.  reset=False: payouts are book-member
    withdrawals against a paper equity (kill thresholds on original 1.0).
    reset=True (conservative): payouts reduce the trading balance after being
    taken (future returns apply to the reduced balance, which compounds less)."""
    rng = random.Random(seed)
    blown = 0
    payouts: list[float] = []
    for _ in range(n_sims):
        sim = _block_bootstrap_days(daily, n_days=horizon_days, block=block,
                                    seed=rng.randrange(1 << 30))
        eq = 1.0
        snap = 1.0
        total = 0.0
        dead = False
        for i, (_, r) in enumerate(sim):
            r *= k
            if r <= -PROP["max_daily_loss_pct"] / 100.0:
                dead = True
                break
            eq += r
            if eq <= 1.0 - PROP["max_total_loss_pct"] / 100.0:
                dead = True
                break
            if (i + 1) % payout_every == 0:
                delta = eq - snap
                if delta > 0:
                    pay = split * delta
                    total += pay
                    if reset:
                        eq -= pay
                snap = eq
        if dead:
            blown += 1
        payouts.append(total)
    payouts.sort()
    n = len(payouts)
    return {
        "p_blown": blown / n,
        "mean_payout": sum(payouts) / n,
        "median_payout": payouts[n // 2],
        "p90_payout": payouts[int(0.90 * n) - 1],
        "p_any_payout": sum(1 for p in payouts if p > 1e-12) / n,
    }


def _personal_mc(daily, k, n_sims, block, horizon_days=756, seed=11):
    """No prop rules: 3y growth + max drawdown distribution."""
    rng = random.Random(seed)
    finals, dds = [], []
    for _ in range(n_sims):
        sim = _block_bootstrap_days(daily, n_days=horizon_days, block=block,
                                    seed=rng.randrange(1 << 30))
        eq = 1.0
        peak = 1.0
        maxdd = 0.0
        for _, r in sim:
            eq += r * k
            peak = max(peak, eq)
            if peak > 0:
                maxdd = max(maxdd, (peak - eq) / peak)
            if eq <= 0.0:
                eq = 0.0
                break
        finals.append(eq - 1.0)
        dds.append(maxdd)
    finals.sort()
    dds.sort()
    n = len(finals)
    return {
        "median_cagr_pct": ((finals[n // 2] + 1.0) ** (1 / 3) - 1) * 100,
        "mean_cagr_pct": ((sum(finals) / n + 1.0) ** (1 / 3) - 1) * 100,
        "p05_cagr_pct": ((finals[int(0.05 * n)] + 1.0) ** (1 / 3) - 1) * 100,
        "p95_cagr_pct": ((finals[int(0.95 * n) - 1] + 1.0) ** (1 / 3) - 1) * 100,
        "median_maxdd_pct": dds[n // 2] * 100,
        "p95_maxdd_pct": dds[int(0.95 * n) - 1] * 100,
        "p_loss_3y": sum(1 for f in finals if f < 0) / n,
        "p_catastrophic": sum(1 for f in finals if f < -0.5) / n,
    }


def _trade_level_mc(trade_pnls, trades_per_year, k, n_sims, years=3, seed=13):
    rng = random.Random(seed)
    finals, dds = [], []
    n_trades = max(1, int(round(trades_per_year * years)))
    for _ in range(n_sims):
        eq = 1.0
        peak = 1.0
        maxdd = 0.0
        for _ in range(n_trades):
            eq *= (1.0 + k * rng.choice(trade_pnls))
            peak = max(peak, eq)
            if peak > 0:
                maxdd = max(maxdd, (peak - eq) / peak)
            if eq <= 0.0:
                eq = 0.0
                break
        finals.append(eq - 1.0)
        dds.append(maxdd)
    finals.sort()
    dds.sort()
    n = len(finals)
    return {
        "median_cagr_pct": ((finals[n // 2] + 1.0) ** (1 / years) - 1) * 100,
        "p05_cagr_pct": ((finals[int(0.05 * n)] + 1.0) ** (1 / years) - 1) * 100,
        "median_maxdd_pct": dds[n // 2] * 100,
        "p95_maxdd_pct": dds[int(0.95 * n) - 1] * 100,
    }


# ─────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="market_backtest/results/arm14")
    ap.add_argument("--label", default="arm14")
    ap.add_argument("--market", default="stocks")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--seg", default="test", choices=["test", "train"])
    ap.add_argument("--sims", type=int, default=4000)
    ap.add_argument("--fee", type=float, default=540.0)
    ap.add_argument("--notional", type=float, default=100000.0)
    ap.add_argument("--split", type=float, default=0.80)
    ap.add_argument("--payout-reset", action="store_true",
                    help="conservative: withdrawals reduce the trading balance")
    ap.add_argument("--save", default="")
    args = ap.parse_args()

    rows = load_results(Path(args.dir), args.market, args.label, args.interval)
    if not rows:
        raise SystemExit(f"no labeled results in {args.dir}")
    n_syms = len(rows)

    daily = []
    prev = 1.0
    for i, e in enumerate(ledger_daily_equity(rows, args.seg)[1]):
        daily.append((i, e - prev))
        prev = e
    if args.seg == "test":
        trades = _seg_trade_pnls(rows[0], "test")
        for r in rows[1:]:
            trades += _seg_trade_pnls(r, "test")
    else:
        trades = []
        for r in rows:
            trades += _seg_trade_pnls(r, "train")

    st = _series_stats(daily)
    print(f"[mc] {args.label} {args.market} {args.interval} seg={args.seg} "
          f"symbols={n_syms} trades={len(trades)} days={st['n_days']}")
    print(f"  empirical: mean {st['mean_pct']:+.4f}%/d  sd {st['sd_pct']:.3f}%/d  "
          f"worst {st['worst_pct']:+.2f}%  best {st['best_pct']:+.2f}%  "
          f"Sharpe {st['sharpe']:.2f}  full-Kelly k* = {st['kelly_f']:.2f}")

    cap_safe_k = (4.5 / abs(st["worst_pct"])) if st["worst_pct"] < 0 else 99.0
    print(f"  cap-safe k (worst day <= 4.5%): k <= {cap_safe_k:.2f}")

    k_grid = [0.5, 0.75, 1.0, 1.5, 2.0, 3.0]
    blocks = [1, 5, 10, 21]

    out: dict = {"label": args.label, "empirical": st, "cap_safe_k": cap_safe_k}

    # ── 2. challenge MC, method sensitivity ──────────────────────────────
    print("\n── Prop P1 (60d, +10% / 5% daily / 10% total) — method sensitivity")
    hdr = (f"{'k':>5} | "
           + "".join(f"{'pass'+str(b):>9}{'brd':>7}{'brt':>7}{'t/o':>7}" for b in blocks))
    print(hdr)
    out["challenge"] = {}
    for k in k_grid:
        row = []
        rec = {}
        for b in blocks:
            r = _challenge_mc(daily, k, 10.0, args.sims, block=b,
                              max_days=60, seed=hash((k, b)) % 1000 + 1)
            row.append(f"{r.get('pass',0)*100:>9.1f}{r.get('fail_daily',0)*100:>7.1f}"
                       f"{r.get('fail_total',0)*100:>7.1f}{r.get('timeout',0)*100:>7.1f}")
            rec[f"block{b}"] = {kk: round(v, 4) for kk, v in r.items()}
        print(f"{k:>5} | " + "".join(row))
        out["challenge"][str(k)] = rec

    # ── 3. full challenge (P1 -> P2) ────────────────────────────────────
    print("\n── Full challenge  P1(60d +10%) -> P2(30d +5%)   [block=5]")
    print(f"{'k':>5}{'p1%':>8}{'p2|p1%':>9}{'p_full%':>9}")
    out["full_challenge"] = {}
    for k in k_grid:
        p1, p2c = _full_challenge_mc(daily, k, args.sims, 5)
        print(f"{k:>5}{p1*100:>8.1f}{p2c*100:>9.1f}{p1*p2c*100:>9.1f}")
        out["full_challenge"][str(k)] = {"p1": p1, "p2_given_p1": p2c,
                                         "p_full": p1 * p2c}

    # ── 4+5. funded economics + attempt EV ──────────────────────────────
    print(f"\n── Funded phase economics (1y, monthly payouts, split "
          f"{args.split:.0%}, notional ${args.notional:,.0f})")
    print(f"{'k':>5}{'blown12m%':>11}{'E payout$':>12}{'med$':>10}{'P90$':>12}"
          f"{'P(any)$%':>10}{'EV/attempt$':>13}")
    out["funded"] = {}
    for k in k_grid:
        fu = _funded_payout_mc(daily, k, args.split, args.sims, 5,
                               reset=args.payout_reset)
        p_full = out["full_challenge"][str(k)]["p_full"]
        payout_ev = fu["mean_payout"] * args.notional
        ev = p_full * (args.fee + payout_ev) - args.fee
        print(f"{k:>5}{fu['p_blown']*100:>11.1f}{payout_ev:>12,.0f}"
              f"{fu['median_payout']*args.notional:>10,.0f}"
              f"{fu['p90_payout']*args.notional:>12,.0f}"
              f"{fu['p_any_payout']*100:>10.1f}{ev:>13,.0f}")
        out["funded"][str(k)] = {
            **{kk: round(vv, 5) for kk, vv in fu.items()},
            "payout_ev_usd": payout_ev, "full_pass": p_full, "ev_per_attempt": ev}

    # ── 6. personal account frontier ────────────────────────────────────
    print("\n── Personal account (no prop rules), 3y block-bootstrap")
    print(f"{'k':>5}{'med CAGR%':>11}{'mean%':>9}{'P5 CAGR%':>10}{'P95 CAGR%':>11}"
          f"{'med maxDD%':>12}{'P95 maxDD%':>11}{'P(loss)%':>10}{'P(<-50%)%':>11}")
    out["personal"] = {}
    for k in [0.5, 1.0, 1.5, 2.0, 3.0, 5.0]:
        pm = _personal_mc(daily, k, args.sims, 5)
        print(f"{k:>5}{pm['median_cagr_pct']:>11.1f}{pm['mean_cagr_pct']:>9.1f}"
              f"{pm['p05_cagr_pct']:>10.1f}{pm['p95_cagr_pct']:>11.1f}"
              f"{pm['median_maxdd_pct']:>12.1f}{pm['p95_maxdd_pct']:>11.1f}"
              f"{pm['p_loss_3y']*100:>10.1f}{pm['p_catastrophic']*100:>11.1f}")
        out["personal"][str(k)] = {kk: round(vv, 3) for kk, vv in pm.items()}

    # ── 7. trade-level cross-check ──────────────────────────────────────
    trades_per_year = len(trades) / 3.0  # test span ~3.0y
    print(f"\n── Trade-level bootstrap cross-check ({trades_per_year:.1f} "
          f"trades/yr, 3y)")
    print(f"{'k':>5}{'med CAGR%':>11}{'P5 CAGR%':>10}{'med maxDD%':>12}{'P95 maxDD%':>11}")
    out["trade_level"] = {}
    for k in [0.5, 1.0, 1.5, 2.0, 3.0]:
        tm = _trade_level_mc(trades, trades_per_year, k, args.sims)
        print(f"{k:>5}{tm['median_cagr_pct']:>11.1f}{tm['p05_cagr_pct']:>10.1f}"
              f"{tm['median_maxdd_pct']:>12.1f}{tm['p95_maxdd_pct']:>11.1f}")
        out["trade_level"][str(k)] = {kk: round(vv, 3) for kk, vv in tm.items()}

    if args.save:
        p = Path("market_backtest/results") / f"{args.save}.json"
        p.write_text(json.dumps(out, indent=1))
        print(f"\n[mc] → {p}")


if __name__ == "__main__":
    main()
