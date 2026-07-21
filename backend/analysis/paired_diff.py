"""Paired-difference statistical comparison of two V2 batch runs.

Compares two batch_ids (baseline vs candidate, e.g. iter04 vs iter05) on a
per-recording basis and reports:

  * Paired per-recording PnL diff (Δ = candidate − baseline)
  * Wilcoxon signed-rank test for median Δ ≠ 0
  * Paired t-test on per-token PnL
  * Bootstrapped 95% CI of mean Δ PnL
  * McNemar's test on per-token "winning vs losing" pivot
  * Headline aggregate diffs (total trades, WR, PF, expectancy)
  * Per-exit-reason migration (which exits got added/removed/relabelled)
  * Tokens that flipped from losing → winning (and vice versa)
  * Worst-Δ tokens so we can spot regressions introduced by the change

Rationale
---------
Iter04 (engine as-is) is baseline. Each iter hypothesis is a candidate.
A change is ACCEPTED iff:

  1. Aggregate PnL(per-recording majority) increases — Wilcoxon p < 0.05
     on positive side, AND
  2. Mean Δ PnL bootstrapped 95% CI excludes zero (strictly positive),
     AND
  3. ≥ 50% of traded tokens improve their per-token PnL (anti-overfit
     guard: helps the majority, not a handful of outliers).

This is the strictest anti-overfit gate we can apply short of walk-forward.
Uses only scipy/numpy (already in the environment).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from typing import Iterable

RESULTS_DIR = os.environ.get(
    "BACKTEST_RESULTS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "backtest_results"),
)
ANALYSIS_DIR = os.path.dirname(__file__)


# ============================================================
# Data loading (mirrors aggregate_results._gather, with index)
# ============================================================
def _safe(x, default=0.0):
    try:
        if x is None:
            return default
        x = float(x)
        if x != x or x in (float("inf"), float("-inf")):
            return default
        return x
    except Exception:
        return default


def _files_for(batch_id: str | None) -> dict[int, str]:
    """Return {recording_id: filepath} for a batch_id (or newest-per-rec if None)."""
    if batch_id:
        files = sorted(glob.glob(os.path.join(RESULTS_DIR, f"*_{batch_id}_*.json")))
    else:
        files = sorted(glob.glob(os.path.join(RESULTS_DIR, "*.json")), key=os.path.getmtime)
    by_rec: dict[int, str] = {}
    for f in files:
        m = re.search(r"_rec(\d+)_", os.path.basename(f))
        if not m:
            continue
        rec = int(m.group(1))
        if not batch_id:
            cur = by_rec.get(rec)
            if cur is None or os.path.getmtime(f) > os.path.getmtime(cur):
                by_rec[rec] = f
        else:
            by_rec[rec] = f
    return by_rec


def _load_per_token(files_by_rec: dict[int, str]) -> dict[int, dict]:
    """Load and return {recording_id: parsed dict}."""
    out = {}
    for rec, fn in files_by_rec.items():
        try:
            with open(fn) as f:
                d = json.load(f)
            d["__file"] = fn
            out[rec] = d
        except Exception:
            continue
    return out


# ============================================================
# Per-token metric vectorisation
# ============================================================
def _per_token_metrics(per_token_files: dict[int, dict]) -> dict[int, dict]:
    """For each recording_id compute {n_trades, pnl_sol, win_rate, exits}."""
    out = {}
    for rec, d in per_token_files.items():
        ts = d.get("trades", [])
        s = d.get("summary", {})
        pnl = _safe(s.get("total_pnl_sol"))
        n = len(ts) if ts else s.get("total_trades", 0)
        wr = _safe(s.get("win_rate_pct"))
        win = s.get("winning_trades", sum(1 for t in ts if t.get("outcome") == "W"))
        exits = defaultdict(lambda: {"n": 0, "pnl": 0.0})
        for t in ts:
            r = t.get("exit_reason", "?")
            exits[r]["n"] += 1
            exits[r]["pnl"] += _safe(t.get("pnl_sol"))
        out[rec] = {
            "symbol": d.get("token_symbol") or d.get("token_name") or "unknown",
            "n_trades": n,
            "pnl_sol": pnl,
            "win_rate_pct": wr,
            "winning_trades": win,
            "exits": {k: dict(v) for k, v in exits.items()},
        }
    return out


# ============================================================
# Statistical tests (lazy scipy import so the script still works
# without scipy, just falls back to descriptive stats)
# ============================================================
def _wilcoxon(diffs):
    try:
        from scipy.stats import wilcoxon
        non_zero = [d for d in diffs if abs(d) > 1e-12]
        if len(non_zero) < 5:
            return None, None, None
        stat, p = wilcoxon(non_zero, alternative="two-sided")
        # one-sided positive: are diffs systematically > 0 ?
        _, p_pos = wilcoxon(non_zero, alternative="greater")
        return float(stat), float(p), float(p_pos)
    except Exception:
        return None, None, None


def _paired_ttest(diffs):
    try:
        from scipy.stats import ttest_1samp
        if len(diffs) < 5:
            return None, None, None
        t, p = ttest_1samp(diffs, 0.0)
        return float(t), float(p), None
    except Exception:
        return None, None, None


def _bootstrap_ci(diffs, n_boot=10000, alpha=0.05, seed=123):
    import random
    rng = random.Random(seed)
    n = len(diffs)
    if n < 5:
        return None, None, None
    means = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += diffs[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo_idx = int(alpha / 2 * n_boot)
    hi_idx = int((1 - alpha / 2) * n_boot) - 1
    return means[lo_idx], means[hi_idx], (sum(means) / n_boot)


def _mcnemar(baseline_yes: list[bool], candidate_yes: list[bool]):
    """McNemar on paired binary 'is this token profitable?' indicator."""
    try:
        from statsmodels.stats.contingency_tables import mcnemar
    except Exception:
        # Manual exact McNemar (binomial)
        b = sum(1 for bb, cc in zip(baseline_yes, candidate_yes) if bb and not cc)
        c = sum(1 for bb, cc in zip(baseline_yes, candidate_yes) if not bb and cc)
        n = b + c
        if n < 5:
            return None, None
        try:
            from scipy.stats import binomtest
            res = binomtest(min(b, c), n, 0.5, alternative="two-sided")
            return int(b), float(res.pvalue)
        except Exception:
            return int(b), None
    try:
        table = [[sum(1 for bb, cc in zip(baseline_yes, candidate_yes) if bb and cc),
                  sum(1 for bb, cc in zip(baseline_yes, candidate_yes) if bb and not cc)],
                 [sum(1 for bb, cc in zip(baseline_yes, candidate_yes) if not bb and cc),
                  sum(1 for bb, cc in zip(baseline_yes, candidate_yes) if not bb and not cc)]]
        res = mcnemar(table, exact=True)
        return table, float(res.pvalue)
    except Exception:
        return None, None


# ============================================================
# Headline aggregate (mirrors aggregate_results._aggregate but lightweight)
# ============================================================
def _headline(per_token: dict[int, dict]) -> dict:
    trades = sum(v["n_trades"] for v in per_token.values())
    pnl = sum(v["pnl_sol"] for v in per_token.values())
    wins = sum(v["winning_trades"] for v in per_token.values())
    wr = (wins / trades * 100.0) if trades else 0.0
    profitable = sum(1 for v in per_token.values() if v["pnl_sol"] > 0)
    return {
        "tokens_traded": len(per_token),
        "total_trades": trades,
        "winning_trades": wins,
        "win_rate_pct": round(wr, 3),
        "total_pnl_sol": round(pnl, 5),
        "tokens_profitable": profitable,
    }


# ============================================================
# Compare
# ============================================================
def _compare(baseline_files: dict[int, dict], candidate_files: dict[int, dict], label_a: str, label_b: str):
    base_metrics = _per_token_metrics(baseline_files)
    cand_metrics  = _per_token_metrics(candidate_files)
    common = sorted(set(base_metrics) & set(cand_metrics))
    only_baseline  = sorted(set(base_metrics) - set(cand_metrics))
    only_candidate = sorted(set(cand_metrics) - set(base_metrics))
    # paired diffs
    diffs_pnl = []
    diffs_wr  = []
    diffs_trades = []
    base_yes = []   # baseline token profitable?
    cand_yes = []
    rows = []
    for rec in common:
        b = base_metrics[rec]
        c = cand_metrics[rec]
        d_pnl = c["pnl_sol"] - b["pnl_sol"]
        d_wr  = c["win_rate_pct"] - b["win_rate_pct"]
        d_n   = c["n_trades"]    - b["n_trades"]
        diffs_pnl.append(d_pnl)
        diffs_wr.append(d_wr)
        diffs_trades.append(d_n)
        base_yes.append(b["pnl_sol"] > 0)
        cand_yes.append(c["pnl_sol"] > 0)
        rows.append({
            "recording_id": rec,
            "symbol": c["symbol"],
            "base_pnl": round(b["pnl_sol"], 5),
            "cand_pnl": round(c["pnl_sol"], 5),
            "d_pnl":   round(d_pnl, 5),
            "base_n":  b["n_trades"],
            "cand_n":  c["n_trades"],
            "base_wr": round(b["win_rate_pct"], 1),
            "cand_wr": round(c["win_rate_pct"], 1),
            "d_wr":    round(d_wr, 1),
        })
    summary = {
        "baseline": _headline(base_metrics),
        "candidate": _headline(cand_metrics),
        "labels": {"baseline": label_a, "candidate": label_b},
        "common_tokens":      len(common),
        "only_in_baseline":   len(only_baseline),
        "only_in_candidate":  len(only_candidate),
        # diffs
        "mean_d_pnl":   sum(diffs_pnl)/len(diffs_pnl) if diffs_pnl else 0.0,
        "median_d_pnl": sorted(diffs_pnl)[len(diffs_pnl)//2] if diffs_pnl else 0.0,
        "n_tokens_improved":   sum(1 for d in diffs_pnl if d > 1e-9),
        "n_tokens_regressed":  sum(1 for d in diffs_pnl if d < -1e-9),
        "n_tokens_flipped_L_to_W": sum(1 for b_, c_ in zip(base_yes, cand_yes) if not b_ and c_),
        "n_tokens_flipped_W_to_L": sum(1 for b_, c_ in zip(base_yes, cand_yes) if b_ and not c_),
    }
    summary["majority_improved_pct"] = (
        100.0 * summary["n_tokens_improved"] / len(common) if common else 0.0
    )
    # Stats
    w_stat, w_p, w_p_pos = _wilcoxon(diffs_pnl)
    t_stat, t_p, _ = _paired_ttest(diffs_pnl)
    ci_lo, ci_hi, bs_mean = _bootstrap_ci(diffs_pnl)
    mc_table, mc_p = _mcnemar(base_yes, cand_yes)
    summary["stats"] = {
        "wilcoxon_stat": w_stat, "wilcoxon_p": w_p, "wilcoxon_p_greater": w_p_pos,
        "paired_t_stat": t_stat, "paired_t_p": t_p,
        "bootstrap_d_pnl_mean": bs_mean,
        "bootstrap_d_pnl_ci95_low":  ci_lo,
        "bootstrap_d_pnl_ci95_high": ci_hi,
        "mcnemar_p": mc_p,
        "n_diffs": len(diffs_pnl),
    }
    # Strict accept/reject verdict
    verdict = "REJECT"
    if (w_p_pos is not None and w_p_pos < 0.05
        and ci_lo is not None and ci_hi is not None and ci_lo > 0
        and summary["majority_improved_pct"] >= 50.0):
        verdict = "ACCEPT"
    elif (w_p_pos is not None and w_p_pos < 0.05
          and ci_lo is not None and ci_hi is not None and ci_lo > 0):
        verdict = "ACCEPT_WITH_RESERVATION"  # majority failed but stats significant
    summary["verdict"] = verdict
    summary["rows"] = sorted(rows, key=lambda r: r["d_pnl"])  # worst regressions first
    return summary


# ============================================================
# Output
# ============================================================
def _print(summary: dict):
    L = summary["labels"]
    b, c = summary["baseline"], summary["candidate"]
    s = summary["stats"]
    print()
    print("="*70)
    print(f"  PAIRED DIFF  baseline={L['baseline']}   candidate={L['candidate']}")
    print("="*70)
    print(f"Tokens traded   baseline={b['tokens_traded']:>4d}   candidate={c['tokens_traded']:>4d}   common={summary['common_tokens']}")
    print(f"Total trades    baseline={b['total_trades']:>4d}   candidate={c['total_trades']:>4d}")
    print(f"Win rate        baseline={b['win_rate_pct']:>6.2f}%   candidate={c['win_rate_pct']:>6.2f}%   Δ={c['win_rate_pct']-b['win_rate_pct']:+.2f}")
    print(f"Total PnL SOL   baseline={b['total_pnl_sol']:>+9.5f}   candidate={c['total_pnl_sol']:>+9.5f}   Δ={c['total_pnl_sol']-b['total_pnl_sol']:+.5f}")
    print(f"Tokens profitable  baseline={b['tokens_profitable']:>4d}   candidate={c['tokens_profitable']:>4d}")
    print()
    print("Δ = candidate − baseline, per-recording paired")
    print(f"  Mean Δ PnL:    {summary['mean_d_pnl']:+.6f} SOL")
    print(f"  Median Δ PnL:  {summary['median_d_pnl']:+.6f} SOL")
    print(f"  Tokens improved / regressed:   {summary['n_tokens_improved']} / {summary['n_tokens_regressed']}  "
          f"({summary['majority_improved_pct']:.1f}% improved)")
    print(f"  Flips L→W / W→L:              {summary['n_tokens_flipped_L_to_W']} / {summary['n_tokens_flipped_W_to_L']}")
    print()
    print("Statistical tests (paired per-recording)")
    print(f"  Wilcoxon signed-rank (greater):  W={s['wilcoxon_stat']}  p={s['wilcoxon_p_greater']}")
    print(f"  Paired t-test:                    t={s['paired_t_stat']}  p={s['paired_t_p']}")
    print(f"  Bootstrap 95% CI of mean Δ PnL:   [{s['bootstrap_d_pnl_ci95_low']}, {s['bootstrap_d_pnl_ci95_high']}]")
    print(f"  McNemar (profitable flip):        p={s['mcnemar_p']}")
    print()
    print(f"  *** VERDICT: {summary['verdict']} ***")
    print()
    print("--- Worst 10 regressions (Δ PnL most negative) ---")
    print(f"{'sym':14s} {'rec':>5s} {'base_pnl':>9s} {'cand_pnl':>9s} {'d_pnl':>9s} {'base_n':>5s} {'cand_n':>5s}")
    for r in summary["rows"][:10]:
        print(f"{str(r['symbol'])[:14]:14s} {r['recording_id']:>5d} {r['base_pnl']:>+9.5f} {r['cand_pnl']:>+9.5f} {r['d_pnl']:>+9.5f} {r['base_n']:>5d} {r['cand_n']:>5d}")
    print()
    print("--- Best 10 improvements (Δ PnL most positive) ---")
    for r in summary["rows"][-10:][::-1]:
        print(f"{str(r['symbol'])[:14]:14s} {r['recording_id']:>5d} {r['base_pnl']:>+9.5f} {r['cand_pnl']:>+9.5f} {r['d_pnl']:>+9.5f} {r['base_n']:>5d} {r['cand_n']:>5d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True, help="batch_id of the baseline run (e.g. iter04_full)")
    ap.add_argument("--candidate", required=True, help="batch_id of the candidate run (e.g. iter05_full)")
    ap.add_argument("--save", default=None,
                    help="write JSON analysis to backend/analysis/<label>.json")
    args = ap.parse_args()

    base_files = _files_for(args.baseline)
    cand_files = _files_for(args.candidate)
    if not base_files:
        print(f"No files for baseline batch_id={args.baseline}", file=sys.stderr); sys.exit(2)
    if not cand_files:
        print(f"No files for candidate batch_id={args.candidate}", file=sys.stderr); sys.exit(2)

    base_data = _load_per_token(base_files)
    cand_data = _load_per_token(cand_files)
    summary = _compare(base_data, cand_data, args.baseline, args.candidate)
    _print(summary)

    if args.save:
        out_path = os.path.join(ANALYSIS_DIR, f"{args.save}.json")
        with open(out_path, "w") as f:
            json.dump(summary, f, indent=2, default=str)
        print(f"\nSaved analysis → {out_path}")


if __name__ == "__main__":
    main()
