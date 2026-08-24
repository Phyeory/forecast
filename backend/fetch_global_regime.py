"""iter57 — build/refresh the causal global harvest-regime cache.

Q(d) = clamp01( (mean gr_share over the 3 most recent QUALIFIED trading
dates strictly before d − 0.35) / (0.70 − 0.35) )

where gr_share(d) = (#gain_retrace exits) / (#closed trades) among trades
whose EXIT timestamp falls on date d.  A date qualifies when it carries
>= MIN_TRADES closed trades (thin dates never enter the lag window).

Two regions of the emitted map:
  * historical  — Q for each qualified date, from the 3 prior qualified
    dates (sparse: unqualified dates have no entry; exactly the semantics
    the iter57 candidate batch was validated with).
  * forward     — Q for every calendar day from the last qualified date + 1
    through TODAY (the live frontier).  While no new trading dates close,
    the window is frozen, which is the correct causal projection: "no new
    evidence ⇒ regime unchanged".  Each new qualified date shifts the
    window automatically on the next refresh.

Causality: Q(d) is a function of trade exits on dates strictly before d,
so it is computable at 00:00 UTC of d in live operation.  There is
deliberately NO stale-Q fallback in the engine — a date missing from the
map runs NEUTRAL (base give_frac), the safe, validated degradation.

The cache is maintained two ways:
  * manually   — `python backend/fetch_global_regime.py --label <batch>`
                 (full rebuild from a baseline batch's per-token logs);
  * automated  — `merge_refresh()` called by the main.py maintenance task,
                 which incrementally backtests NEW recordings (pinned to
                 `v2_rate_split_enable=0.0` measurement semantics — iter64;
                 formerly `v2_regime_enable=0.0`) and merges
                 their exits into the persisted per-date accumulators.

Cache layout (backend/data/global_regime_cache.json):
  q_by_date        {date: Q}                     — engine-facing map
  accumulators     {date: [n_closed, n_gain_retrace]}  — incremental merge state
  measured_rec_ids [recording ids already measured]    — avoids re-running
  provenance       source / generated_utc / constants / gr_share_by_date
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
BACKTEST_DB = os.path.join(HERE, "data", "backtest_data.db")

Q_LO = 0.35     # normalisation: gr_share mapping to Q = 0
Q_HI = 0.70     # normalisation: gr_share mapping to Q = 1
LAG_DAYS = 3    # trailing qualified-date window
MIN_TRADES = 10  # per-date qualification floor


def _utc_today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _utc_date(ts: float) -> str:
    return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).strftime("%Y-%m-%d")


# ── cache I/O ────────────────────────────────────────────────────────────

def load_cache(path: str = CACHE_PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def write_cache(cache: dict, path: str = CACHE_PATH) -> None:
    """Atomic write (tmp + rename) so live pumps never read a torn file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    os.replace(tmp, path)


# ── exit-mix extraction ──────────────────────────────────────────────────

def gr_share_from_batch(label: str) -> dict[str, dict]:
    """{date: {n, gr}} from every trade in the batch's per-token logs,
    grouped by EXIT date (closed trades only — causal at any instant).
    When several batch_ids share the label, the newest log per recording
    wins."""
    files = glob.glob(os.path.join(RESULTS_DIR, f"*_{label}_*.json"))
    if not files:
        return {}
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


def _measured_ids_from_db(label: str) -> list[int]:
    """All recording ids processed by the NEWEST batch carrying `label`
    (includes zero-trade recordings that left no per-token log)."""
    try:
        import sqlite3
        conn = sqlite3.connect(BACKTEST_DB)
        row = conn.execute(
            "SELECT batch_id FROM backtests WHERE batch_id LIKE ? "
            "ORDER BY batch_id DESC LIMIT 1", (f"{label}_%",)).fetchone()
        ids = []
        if row:
            ids = [r[0] for r in conn.execute(
                "SELECT recording_id FROM backtests WHERE batch_id = ?",
                (row[0],))]
        conn.close()
        return ids
    except Exception:
        return []


# ── Q construction ───────────────────────────────────────────────────────

def _q_from_window(gr: dict[str, float], window: list[str]) -> float:
    mean_lag = sum(gr[d] for d in window) / len(window)
    return min(1.0, max(0.0, (mean_lag - Q_LO) / (Q_HI - Q_LO)))


def build_q(accumulators: dict[str, list], today: str | None = None) -> dict[str, float]:
    """Q map from per-date [n_closed, n_gr] accumulators.

    Historical region reproduces the validated iter57 candidate semantics
    exactly (qualified dates only).  Forward region covers every calendar
    day after the last qualified date through `today` (default: now), so a
    live session always finds today's entry; the window stays frozen while
    no new qualified dates close."""
    today = today or _utc_today()
    # today itself is excluded from qualification: its exits are still
    # accruing, so its gr_share is not a closed measurement
    qualified = [d for d in sorted(accumulators)
                 if accumulators[d][0] >= MIN_TRADES and d < today]
    if not qualified:
        return {}
    gr = {d: accumulators[d][1] / accumulators[d][0] for d in qualified}
    q_by_date: dict[str, float] = {}
    for i, d in enumerate(qualified):
        if i < LAG_DAYS:
            continue
        q_by_date[d] = _q_from_window(gr, qualified[i - LAG_DAYS:i])
    # forward frontier: last qualified + 1 .. today
    d = (_dt.datetime.strptime(qualified[-1], "%Y-%m-%d")
         + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    while d <= today:
        q_by_date[d] = _q_from_window(gr, qualified[-LAG_DAYS:])
        d = (_dt.datetime.strptime(d, "%Y-%m-%d")
             + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    return q_by_date


# ── cache assembly ───────────────────────────────────────────────────────

def _assemble(accumulators: dict[str, list], measured_ids: list[int],
              source: str, today: str | None = None) -> dict:
    q_by_date = build_q(accumulators, today)
    qualified = {d: v for d, v in accumulators.items() if v[0] >= MIN_TRADES}
    return {
        "q_by_date": q_by_date,
        "accumulators": {d: list(v) for d, v in sorted(accumulators.items())},
        "measured_rec_ids": sorted(measured_ids),
        "provenance": {
            "source": source,
            "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "q_lo": Q_LO,
            "q_hi": Q_HI,
            "lag_days": LAG_DAYS,
            "min_trades_per_date": MIN_TRADES,
            "n_dates": len(q_by_date),
            "live_frontier": today or _utc_today(),
            "note": "historical region = qualified trading dates (validated "
                    "iter57 semantics); forward region = causal daily "
                    "projection through the live frontier.  Engine runs "
                    "NEUTRAL on dates absent from the map (no stale-Q "
                    "fallback by design).",
            "qualified_dates": sorted(qualified),
            "gr_share_by_date": {d: round(v[1] / v[0], 4)
                                 for d, v in sorted(qualified.items())},
            "diagnosis": "backend/analysis/iter57_diagnosis.md",
        },
    }


def merge_refresh(new_per_date: dict[str, dict], new_rec_ids: list[int],
                  source: str, today: str | None = None,
                  path: str = CACHE_PATH) -> dict:
    """Incremental automation entry point (main.py maintenance task): merge
    a fresh measurement batch's exit mix into the persisted accumulators,
    rebuild Q through today, and atomically rewrite the cache."""
    cache = load_cache(path)
    accumulators: dict[str, list] = {
        d: list(v) for d, v in (cache.get("accumulators") or {}).items()}
    measured = set(cache.get("measured_rec_ids") or [])
    for d, v in new_per_date.items():
        a = accumulators.setdefault(d, [0, 0])
        a[0] += int(v["n"])
        a[1] += int(v["gr"])
    measured.update(new_rec_ids)
    out = _assemble(accumulators, sorted(measured), source, today)
    write_cache(out, path)
    return out


# ── CLI ──────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default=None,
                    help="batch label in backend/v2_results (default: latest "
                         "iter57 baseline label present)")
    ap.add_argument("--panel", action="store_true",
                    help="build from date_segmented_results.json instead")
    ap.add_argument("--today", default=None,
                    help="override the live-frontier date (testing)")
    args = ap.parse_args()

    if args.panel:
        per_date = gr_share_from_panel()
        source = PANEL_PATH
        measured: list[int] = []
    else:
        label = args.label
        if label is None:
            cands = {m.group(1) for fn in os.listdir(RESULTS_DIR)
                     if (m := re.match(r".*_rec\d+_(iter\d+_baseline_full)_\d+\.json", fn))}
            if not cands:
                sys.exit("no baseline label found; pass --label explicitly")
            label = sorted(cands)[-1]
        per_date = gr_share_from_batch(label)
        if not per_date:
            sys.exit(f"no per-token logs found for label '{label}' in {RESULTS_DIR}")
        source = f"v2_results label '{label}'"
        # prefer the full processed cohort from the backtest DB (includes
        # zero-trade recordings) so incremental automation never re-runs them
        measured = _measured_ids_from_db(label)
        if not measured:
            measured = [int(m.group(1)) for fn in
                        glob.glob(os.path.join(RESULTS_DIR, f"*_{label}_*.json"))
                        if (m := re.search(r"_rec(\d+)_", os.path.basename(fn)))]

    accumulators = {d: [v["n"], v["gr"]] for d, v in per_date.items()}
    cache = _assemble(accumulators, measured, source, args.today)
    write_cache(cache)
    q = cache["q_by_date"]
    print(f"wrote {CACHE_PATH}: {len(q)} Q dates from "
          f"{len(cache['provenance']['qualified_dates'])} qualified dates, "
          f"{len(measured)} measured recordings (source: {source})")
    for d in sorted(q)[-8:]:
        print(f"  {d}  Q={q[d]:.4f}")
    if len(q) > 8:
        print(f"  ... ({len(q) - 8} earlier dates)")


if __name__ == "__main__":
    main()
