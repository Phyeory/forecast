"""mc_firms.py — firm-rule Monte Carlo: pass / blow / payouts / EV-per-attempt
under REAL prop-firm templates (verified against ftmo.com/trading-objectives,
2026-10-18).

Why: the earlier prop MC imposed 30/60-day windows.  FTMO's 2-Step has NO
time limit (10%/5% targets, 5% daily, static 10% max loss, min 4 days) —
so the binding constraint is barrier arithmetic, not a deadline.  This script
sweeps firm templates x size-k on any labeled daily PnL series (one market,
or SEVERAL markets pooled into one prop account).

Templates encode: targets (1/2-step), daily cap, max-loss (static vs trailing),
time limit (None = unlimited).

Funded phase: same kill rules + monthly payouts at `split` (paper accounting,
and conservative `--reset` where withdrawals shrink the trading balance).

Usage (single market):
  python -m market_backtest.mc_firms --dir market_backtest/results/arm14 \
      --label arm14 --market stocks --interval 1d --sims 3000

Usage (pooled account across markets — pass --inputs, skip --dir):
  python -m market_backtest.mc_firms \
      --inputs arm14@market_backtest/results/arm14:stocks \
      --inputs arm14@market_backtest/results/arm14_idx:idx_fut \
      --sims 3000

  (pooling is exact: daily ledgers live on an epoch-day axis, summed across
   markets into one account equity path)
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from market_backtest.stats_report import (
    load_results, ledger_daily_equity, _block_bootstrap_days, _sharpe)


# ─────────────────────────────────────────────────────────────────────────
# firm templates (dims that bind): targets [p1, p2...], daily cap, max loss,
# trailing?, time limit days (None = unlimited)
# ─────────────────────────────────────────────────────────────────────────
TEMPLATES: dict[str, dict] = {
    # FTMO 2-Step: 10%/5% targets, 5% daily, STATIC 10% max, no time limit
    "ftmo_2step":        dict(targets=[10.0, 5.0], daily=5.0, max_loss=10.0,
                              trailing=False, time_limit=None, consistency=50.0),
    "ftmo_2step_nocons": dict(targets=[10.0, 5.0], daily=5.0, max_loss=10.0,
                              trailing=False, time_limit=None, consistency=None),
    # generic 8%/5% 2-step, no time limit (FundingPips/E8 class)
    "firm_8_5":          dict(targets=[8.0, 5.0], daily=5.0, max_loss=10.0,
                              trailing=False, time_limit=None),
    # 1-step no-daily-limit family (some firms only run a max-loss rule)
    "nodaily_10":        dict(targets=[10.0], daily=None, max_loss=10.0,
                              trailing=False, time_limit=None),
    "nodaily_8":         dict(targets=[10.0], daily=None, max_loss=8.0,
                              trailing=False, time_limit=None),
    "nodaily_6":         dict(targets=[10.0], daily=None, max_loss=6.0,
                              trailing=False, time_limit=None),
    # FTMO 1-Step: 10% target, 3% daily, EOD-trailing 10% max, no time limit
    "ftmo_1step":        dict(targets=[10.0], daily=3.0, max_loss=10.0,
                              trailing=True, time_limit=None, consistency=50.0),
    # futures props: trailing drawdowns (EOD granularity here)
    "topstep_style":     dict(targets=[6.0], daily=2.0, max_loss=4.0,
                              trailing=True, time_limit=None),
    "apex_style":        dict(targets=[6.0], daily=None, max_loss=4.0,
                              trailing=True, time_limit=None),
    # FTMO FREE TRIAL (14 days, target halved to 5%, 5% daily, 10% static,
    # 2 min days, NO consistency) — a sprint, not the real challenge
    "ftmo_trial_2step":  dict(targets=[5.0], daily=5.0, max_loss=10.0,
                              trailing=False, time_limit=14, consistency=None),
    "ftmo_trial_1step":  dict(targets=[5.0], daily=3.0, max_loss=10.0,
                              trailing=True, time_limit=14, consistency=50.0),
    # legacy: the same FTMO 2-step but WITH a 30-day cap per phase (control)
    "ftmo_2step_30d":    dict(targets=[10.0, 5.0], daily=5.0, max_loss=10.0,
                              trailing=False, time_limit=30),
}


def _load_pooled(inputs: list[str], seg: str) -> tuple[list[dict], float]:
    """inputs: 'label@dir[:market[:interval]]' entries; returns pooled rows."""
    rows: list[dict] = []
    for spec in inputs:
        label, _, rest = spec.partition("@")
        parts = rest.split(":")
        d = Path(parts[0]) if parts and parts[0] else Path(
            "market_backtest/results") / label
        market = parts[1] if len(parts) > 1 and parts[1] else None
        interval = parts[2] if len(parts) > 2 and parts[2] else None
        got = load_results(d, market, label, interval)
        if not got:
            raise SystemExit(f"no labeled results for {spec} (dir={d})")
        rows.extend(got)
    return rows, len(rows)


def _daily_series(rows: list[dict], seg: str) -> list[tuple[int, float]]:
    days, eq = ledger_daily_equity(rows, seg)
    out: list[tuple[int, float]] = []
    prev = 1.0
    for i, e in enumerate(eq):
        out.append((i, e - prev))
        prev = e
    return out


def _series_stats(daily: list[tuple[int, float]]) -> dict:
    vals = [r for _, r in daily]
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / max(1, n - 1)
    sd = math.sqrt(var)
    return {"n_days": n, "mean_pct": mean * 100, "sd_pct": sd * 100,
            "worst_pct": min(vals) * 100, "sharpe": _sharpe(daily),
            "kelly": (mean / var) if var > 0 else float("inf")}


def sim_challenge(daily, k, rules, rng, horizon=2520,
                  consistency: float | None = None) -> str:
    """One no-deadline (or capped) single-phase run; first barrier wins.

    `consistency` (e.g. 50.0) models the FTMO-style rule: the best single day
    may not exceed that share of TOTAL profit.  Hitting the target before the
    rule is satisfied does NOT pass — the sim keeps trading (as the firm
    requires), so the pass can be delayed or never happen.
    """
    path = _block_bootstrap_days(daily, n_days=horizon, block=5,
                                 seed=rng.randrange(1 << 30))
    eq = 1.0
    peak = 1.0
    profits: list[float] = []
    for i, (_, r) in enumerate(path):
        if rules["time_limit"] is not None and i >= rules["time_limit"]:
            return "timeout"
        r *= k
        if rules["daily"] is not None and r <= -rules["daily"] / 100.0:
            return "fail_daily"
        eq += r
        profits.append(r)
        if eq > peak:
            peak = eq
        base = peak if rules["trailing"] else 1.0
        if eq <= base - rules["max_loss"] / 100.0:
            return "fail_total"
        if eq >= 1.0 + rules["targets"][0] / 100.0:
            if consistency and len(profits) > 1:
                total = eq - 1.0
                best = max(profits)
                if total > 0 and best / total > consistency / 100.0:
                    continue          # keep trading until the rule is met
            return "pass"
    return "long"


def challenge_mc(daily, k, rules, n_sims, seed=11) -> dict:
    rng = random.Random(seed)
    counts: dict[str, int] = {}
    pass_days: list[int] = []
    cons = rules.get("consistency")
    for s in range(n_sims):
        res = sim_challenge(daily, k, rules, rng, consistency=cons)
        counts[res] = counts.get(res, 0) + 1
    return {kk: v / n_sims for kk, v in counts.items()}


def full_challenge_mc(daily, k, rules, n_sims, seed=17) -> dict:
    """P1 -> P2 (each with the same caps); returns p1, p2|p1, p_full."""
    rng = random.Random(seed)
    p1 = p2 = 0
    cons = rules.get("consistency")
    for s in range(n_sims):
        if sim_challenge(daily, k, rules, rng, consistency=cons) == "pass":
            p1 += 1
            r2 = dict(rules)
            r2["targets"] = [rules["targets"][1]] + list(rules["targets"][2:]) \
                if len(rules["targets"]) > 1 else rules["targets"]
            if sim_challenge(daily, k, r2, rng, consistency=cons) == "pass":
                p2 += 1
    return {"p1": p1 / n_sims,
            "p2_given_p1": (p2 / p1 if p1 else 0.0),
            "p_full": (p2 / n_sims) if len(rules["targets"]) > 1 else p1 / n_sims}


def funded_mc(daily, k, rules, split, n_sims, horizon=252,
              payout_every=21, reset=False, seed=23) -> dict:
    """Funded account: same kill rules + payouts every `payout_every` days on
    positive deltas of the paper equity.  reset=True: withdrawals reduce the
    trading balance (conservative)."""
    rng = random.Random(seed)
    blown = 0
    payouts: list[float] = []
    months_paid = 0
    for _ in range(n_sims):
        path = _block_bootstrap_days(daily, n_days=horizon, block=5,
                                     seed=rng.randrange(1 << 30))
        eq, peak, snap = 1.0, 1.0, 1.0
        total = 0.0
        dead = False
        for i, (_, r) in enumerate(path):
            r *= k
            if rules["daily"] is not None and r <= -rules["daily"] / 100.0:
                dead = True
                break
            eq += r
            if eq > peak:
                peak = eq
            base = peak if rules["trailing"] else 1.0
            if eq <= base - rules["max_loss"] / 100.0:
                dead = True
                break
            if (i + 1) % payout_every == 0:
                delta = eq - snap
                if delta > 0:
                    pay = split * delta
                    total += pay
                    months_paid += 1
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
        "mean_months_paid": months_paid / n,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="")
    ap.add_argument("--label", default="")
    ap.add_argument("--market", default="")
    ap.add_argument("--interval", default="")
    ap.add_argument("--inputs", action="append", default=[],
                    help="label@dir[:market[:interval]] (repeatable; pooled)")
    ap.add_argument("--seg", default="test", choices=["test", "train"])
    ap.add_argument("--sims", type=int, default=3000)
    ap.add_argument("--ks", default="0.5,0.75,1.0,1.5,2.0,3.0")
    ap.add_argument("--fee", type=float, default=540.0)
    ap.add_argument("--notional", type=float, default=100000.0)
    ap.add_argument("--split", type=float, default=0.80)
    ap.add_argument("--templates", default="")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--fund-horizon", type=int, default=252)
    ap.add_argument("--save", default="")
    args = ap.parse_args()

    if args.inputs:
        rows, n_src = _load_pooled(args.inputs, args.seg)
        tag = "pooled:" + ",".join(s.split("@")[0] for s in args.inputs[:1]) + \
              f"+{len(args.inputs) - 1}more"
    else:
        rows = load_results(Path(args.dir), args.market, args.label,
                            args.interval or None)
        n_src = 1
        tag = f"{args.label}@{args.dir}"
    if not rows:
        raise SystemExit("no labeled results loaded")

    daily = _daily_series(rows, args.seg)
    st = _series_stats(daily)
    print(f"[mc_firms] {tag} seg={args.seg} markets/symbols={n_src} "
          f"days={st['n_days']}  mean {st['mean_pct']:+.4f}%/d  "
          f"sd {st['sd_pct']:.3f}%/d  worst {st['worst_pct']:+.2f}%  "
          f"Sharpe {st['sharpe']:.2f}  kelly*={st['kelly']:.1f}")

    ks = [float(x) for x in args.ks.split(",")]
    names = ([t for t in args.templates.split(",") if t] or list(TEMPLATES))
    out: dict = {"tag": tag, "seg": args.seg, "empirical": st, "templates": {}}

    for name in names:
        rules = TEMPLATES[name]
        targets = rules["targets"]
        tl = rules["time_limit"]
        print(f"\n── {name}: targets {targets} daily "
              f"{rules['daily']}% max {rules['max_loss']}% "
              f"{'trail' if rules['trailing'] else 'static'} "
              f"TL={'none' if tl is None else str(tl) + 'd'}")
        print(f"{'k':>5}{'P1%':>7}{'P2|P1%':>8}{'pfull%':>8}"
              f"{'brd%':>6}{'brt%':>6}{'t/o%':>6} | "
              f"{'blown1y%':>9}{'(reset)%':>9}{'Epayout$':>10}"
              f"{'(reset)$':>10}{'EV/att$':>9}{'(reset)$':>9}{'$/mo$':>8}")
        tpl_out: dict = {}
        for k in ks:
            if len(targets) > 1:
                fc = full_challenge_mc(daily, k, rules, args.sims)
                p1, p2c, pf = fc["p1"], fc["p2_given_p1"], fc["p_full"]
                ch = challenge_mc(daily, k, rules, max(1, args.sims // 3))
            else:
                ch = challenge_mc(daily, k, rules, args.sims)
                p1 = ch.get("pass", 0.0)
                p2c, pf = 1.0, p1
            fu = funded_mc(daily, k, rules, args.split, max(1, args.sims // 2),
                           horizon=args.fund_horizon, reset=False)
            fu_r = funded_mc(daily, k, rules, args.split, max(1, args.sims // 2),
                             horizon=args.fund_horizon, reset=True)
            payout_ev = fu["mean_payout"] * args.notional
            payout_ev_r = fu_r["mean_payout"] * args.notional
            ev = pf * (args.fee + payout_ev) - args.fee
            ev_r = pf * (args.fee + payout_ev_r) - args.fee
            print(f"{k:>5}{p1*100:>7.1f}{p2c*100:>8.1f}{pf*100:>8.1f}"
                  f"{ch.get('fail_daily',0)*100:>6.1f}"
                  f"{ch.get('fail_total',0)*100:>6.1f}"
                  f"{ch.get('timeout',0)*100:>6.1f} | "
                  f"{fu['p_blown']*100:>9.1f}{fu_r['p_blown']*100:>9.1f}"
                  f"{payout_ev:>10,.0f}{payout_ev_r:>10,.0f}"
                  f"{ev:>9,.0f}{ev_r:>9,.0f}"
                  f"{payout_ev_r/12:>8,.0f}")
            tpl_out[str(k)] = {
                "p1": p1, "p2_given_p1": p2c, "p_full": pf,
                "challenge_raw": {kk: round(vv, 4) for kk, vv in ch.items()},
                "funded": {kk: round(vv, 5) for kk, vv in fu.items()},
                "funded_reset": {kk: round(vv, 5) for kk, vv in fu_r.items()},
                "payout_ev_usd": payout_ev,
                "payout_ev_reset_usd": payout_ev_r,
                "ev_per_attempt": ev, "ev_per_attempt_reset": ev_r,
            }
        out["templates"][name] = tpl_out

    if args.save:
        p = Path(args.save)
        p.write_text(json.dumps(out, indent=1))
        print(f"\n[mc_firms] → {p}")


if __name__ == "__main__":
    main()
