"""iter86_screen.py — Mint-history calibration vs the ADOPTED production stack.

Baseline: iter84b_cal_full (population calibration — the adopted production).
Candidate: iter86_mintcal on the 702 recordings with ≥120 prior same-mint
candles (the only cohort the mint layer can affect; all other recordings are
byte-identical to baseline by control-purity verification in the smoke).

Full-DB reconstruction: candidate_full = iter86_mintcal (on affected) ⊕
iter84b_cal_full (everywhere else) — paired against iter84b_cal_full on the
FULL completed cohort with era and train/holdout splits.

Usage:
  cd /Users/jaime/pump-chart
  backend/.venv/bin/python backend/analysis/iter86_screen.py
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys

import numpy as np
from scipy.stats import wilcoxon

RESULTS_DIR = "backend/v2_results"
ERA_CUT = 1787184000
BASE_BATCH = "iter84b_cal_full_1789218145"
MINT_BATCH = None  # discovered
ANALYSIS = os.path.dirname(os.path.abspath(__file__))


def find_batch(label):
    conn = sqlite3.connect("backend/data/backtest_data.db")
    row = conn.execute(
        "SELECT batch_id FROM backtests WHERE batch_id LIKE ? "
        "ORDER BY id DESC LIMIT 1", (f"%{label}%",)
    ).fetchone()
    conn.close()
    if not row:
        print(f"ERROR: no batch for {label}", file=sys.stderr); sys.exit(2)
    return row[0]


def load_batch(batch_id):
    out = {}
    for f in glob.glob(os.path.join(RESULTS_DIR, f"*_{batch_id}_*.json")):
        with open(f) as fh:
            d = json.load(fh)
        out[d["recording_id"]] = float(d["summary"]["total_pnl_sol"])
    return out


def paired_test(b, c, ids, label):
    b = np.array([b.get(i, 0.0) for i in ids])
    c = np.array([c.get(i, 0.0) for i in ids])
    d = c - b
    nz = d[d != 0]
    if len(nz) == 0:
        print(f"{label:14s} n={len(ids):>4}  no differing recordings")
        return None
    _, p_g = wilcoxon(nz, alternative="greater")
    _, p_2 = wilcoxon(nz, alternative="two-sided")
    rng = np.random.default_rng(86)
    means = np.array([rng.choice(d, len(d), replace=True).mean() for _ in range(10000)])
    lo, hi = np.percentile(means, [2.5, 97.5])
    pos, neg = int((d > 0).sum()), int((d < 0).sum())
    print(f"{label:14s} n={len(ids):>4}  Δ={d.sum():+.4f}  mean/rec={d.mean():+.5f}  "
          f"CI[{lo:+.5f},{hi:+.5f}]  p_g={p_g:.4g} p_2={p_2:.4g}  "
          f"{pos}↑/{neg}↓ breadth={100*pos/(pos+neg):.1f}%")
    return {"n": len(ids), "delta": float(d.sum()), "mean": float(d.mean()),
            "ci_lo": float(lo), "ci_hi": float(hi),
            "p_greater": float(p_g), "p_two": float(p_2),
            "improved": pos, "regressed": neg}


def symptoms(batch_id, label, ids=None):
    trades = []
    for f in glob.glob(os.path.join(RESULTS_DIR, f"*_{batch_id}_*.json")):
        with open(f) as fh:
            d = json.load(fh)
        if ids is None or d["recording_id"] in ids:
            trades.extend(d.get("trades", []))
    if not trades:
        print(f"[{label}] no trades"); return
    pnls = np.array([float(t.get("pnl_sol", 0) or 0) for t in trades])
    wins = pnls > 0
    print(f"[{label}] trades={len(trades):>3}  WR={100*wins.mean():.1f}%  "
          f"exp={pnls.mean():+.5f}  PnL={pnls.sum():+.4f}")


if __name__ == "__main__":
    mint_batch = find_batch("iter86_mintcal")
    print(f"baseline: {BASE_BATCH}\nmint cell: {mint_batch}\n")

    base = load_batch(BASE_BATCH)
    mint = load_batch(mint_batch)
    affected = set(json.load(open(os.path.join(ANALYSIS, "iter86_affected_ids.json"))))
    print(f"affected cohort: {len(affected)} recs; mint logs: {len(mint)}\n")

    # sanity: mint logs must be a subset of affected
    stray = set(mint) - affected
    assert not stray, f"mint cell produced logs for non-affected recs: {list(stray)[:5]}"

    # ── affected-cohort paired test (the mechanism's own turf) ──
    print("── Affected cohort (702 recs, paired mint-vs-population) ──")
    paired_test(base, mint, sorted(affected), "AFFECTED")

    # ── full-DB reconstruction ──
    conn = sqlite3.connect("backend/data/price_data.db")
    recs = conn.execute(
        "SELECT id, started_at FROM recordings WHERE status='completed' AND started_at > 0"
    ).fetchall()
    conn.close()
    rec_map = {r[0]: r[1] for r in recs}
    all_ids = list(rec_map)
    # candidate full = mint on affected, base elsewhere
    cand_full = {i: (mint.get(i, base.get(i, 0.0)) if i in affected else base.get(i, 0.0))
                 for i in all_ids}

    split = json.load(open(os.path.join(ANALYSIS, "iter83_split.json")))
    train_ids = set(split["train_ids"]); holdout_ids = set(split["holdout_ids"])

    print("\n── Full-DB completed-cohort (candidate reconstruction vs adopted baseline) ──")
    results = {}
    pre = [i for i in all_ids if rec_map[i] < ERA_CUT]
    post = [i for i in all_ids if rec_map[i] >= ERA_CUT]
    for label, ids in [
        ("FULL DB", all_ids), ("PRE-era", pre), ("POST-era", post),
        ("TRAIN", [i for i in all_ids if i in train_ids]),
        ("HOLDOUT", [i for i in all_ids if i in holdout_ids]),
        ("POST×TRAIN", [i for i in post if i in train_ids]),
        ("POST×HOLDOUT", [i for i in post if i in holdout_ids]),
    ]:
        results[label] = paired_test(base, cand_full, ids, label)

    # ── symptoms on the affected cohort ──
    print("\n── Symptom metrics (affected cohort, logged trades) ──")
    symptoms(BASE_BATCH, "POP (base)", ids=affected)
    symptoms(mint_batch, "MINT      ", ids=affected)

    out = {
        "mint_batch": mint_batch, "base_batch": BASE_BATCH,
        "affected": len(affected),
        "cohorts": results,
    }
    path = os.path.join(ANALYSIS, "iter86_screen_result.json")
    json.dump(out, open(path, "w"), indent=2)
    print(f"\nSaved → {path}")
