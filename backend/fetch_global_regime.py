"""iter57 — build/refresh the causal global harvest-regime cache.

Q(d) = clamp01( (mean gr_share over the trailing 3 QUALIFIED trading dates
strictly before d) − 0.35 ) / (0.70 − 0.35)

where gr_share(d) = (#gain_retrace exits) / (#closed trades) among trades
whose EXIT timestamp falls on date d, taken from a full-cohort batch's
per-token logs in backend/v2_results.  A date qualifies when it carries
>= MIN_TRADES closed trades (thin dates never enter the lag window).

Causality: Q(d) is a function of trade exits on dates strictly before d
only, so it is computable at 00:00 UTC of d in live operation (the exit
mix of the prior 3 trading days is fully known by then).  The written
cache is the single source of truth shared by backtest, forward-test and
the live poller (main.py _global_regime_pump).

Usage:
    python backend/fetch_global_regime.py --label iter57_baseline_full
    python backend/fetch_global_regime.py --panel   # rebuild from the
        canonical date_segmented_results.json panel instead (reproduction
        of the diagnosis artefact values)
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(HERE, "v2_results")
PANEL_PATH = os.path.join(HERE, "analysis", "date_segmented_results.json")
CACHE_PATH = os.path.join(HERE, "data", "global_regime_cache.json")

Q_LO = 0.35     # normalisation: gr_share mapping to Q = 0
Q_HI = 0.70     # normalisation: gr_share mapping to Q = 1
LAG_DAYS = 3    # trailing qualified-date window
MIN_TRADES = 10  # per-date qualification floor


def _utc_date(ts: int) -> str:
    return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).strftime("%Y-%m-%d")


def gr_share_from_batch(label: str) -> dict[str, dict]:
    """{date: {n, gr}} from every trade in the batch's per-token logs,
    grouped by EXIT date (closed trades only — causal at any instant)."""
    files = glob.glob(os.path.join(RESULTS_DIR, f"*_{label}_*.json"))
    if not files:
        sys.exit(f"no per-token logs found for label '{label}' in {RESULTS_DIR}")
    newest: dict[int, str] = {}
    for fn in files:
        m = re.search(r"_rec(\d+)_", os.path.basename(fn))
        if not m:
            continue
        rec = int(m.group(1))
        if rec not in newest or os.path.getmtime(fn) > os.path.getmtime(newest[rec]):
            newest[rec] = fn
    per_date: dict[str, dict] = {}
    for fn in newest.values():
        with open(fn) as f:
            d = json.load(f)
        for t in d.get("trades", []):
            day = _utc_date(t["exit_time"])
            slot = per_date.setdefault(day, {"n": 0, "gr": 0})
            slot["n"] += 1
            if t.get("exit_reason") == "gain_retrace":
                slot["gr"] += 1
    return per_date


def gr_share_from_panel() -> dict[str, dict]:
    """Reproduction path: the canonical 23-date panel grouped trades by
    recording-start date; kept for provenance cross-checks only."""
    data = json.load(open(PANEL_PATH))["results"]
    per_date = {}
    for r in data:
        gr = r.get("exit_reasons", {}).get("gain_retrace", {}).get("n", 0)
        per_date[r["date"]] = {"n": int(r.get("total_trades", 0)), "gr": int(gr)}
    return per_date


def build_q(per_date: dict[str, dict]) -> dict[str, float]:
    qualified = [d for d in sorted(per_date) if per_date[d]["n"] >= MIN_TRADES]
    gr = {d: per_date[d]["gr"] / per_date[d]["n"] for d in qualified}
    q_by_date: dict[str, float] = {}
    for i, d in enumerate(qualified):
        if i < LAG_DAYS:
            continue
        lag = [gr[qualified[i - k]] for k in range(1, LAG_DAYS + 1)]
        mean_lag = sum(lag) / LAG_DAYS
        q = (mean_lag - Q_LO) / (Q_HI - Q_LO)
        q_by_date[d] = min(1.0, max(0.0, q))
    return q_by_date


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default=None,
                    help="batch label in backend/v2_results (default: latest "
                         "iter57 baseline label present)")
    ap.add_argument("--panel", action="store_true",
                    help="build from date_segmented_results.json instead")
    args = ap.parse_args()

    if args.panel:
        per_date = gr_share_from_panel()
        source = PANEL_PATH
    else:
        label = args.label
        if label is None:
            cands = {m.group(1) for fn in os.listdir(RESULTS_DIR)
                     if (m := re.match(r".*_rec\d+_(iter\d+_baseline_full)_\d+\.json", fn))}
            if not cands:
                sys.exit("no baseline label found; pass --label explicitly")
            label = sorted(cands)[-1]
        per_date = gr_share_from_batch(label)
        source = f"v2_results label '{label}'"

    q_by_date = build_q(per_date)
    qualified = {d: v for d, v in per_date.items() if v["n"] >= MIN_TRADES}
    cache = {
        "q_by_date": q_by_date,
        "provenance": {
            "source": source,
            "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "q_lo": Q_LO,
            "q_hi": Q_HI,
            "lag_days": LAG_DAYS,
            "min_trades_per_date": MIN_TRADES,
            "n_dates": len(q_by_date),
            "qualified_dates": sorted(qualified),
            "gr_share_by_date": {d: round(qualified[d]["gr"] / qualified[d]["n"], 4)
                                 for d in sorted(qualified)},
            "diagnosis": "backend/analysis/iter57_diagnosis.md",
        },
    }
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)
    print(f"wrote {CACHE_PATH}: {len(q_by_date)} Q dates from {len(qualified)} "
          f"qualified dates (source: {source})")
    for d in sorted(q_by_date):
        print(f"  {d}  Q={q_by_date[d]:.4f}")


if __name__ == "__main__":
    main()
