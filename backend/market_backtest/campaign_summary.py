"""campaign_summary.py — one ranked table over every sweep run in sweeps/.

Every cell of every round (A/B/probes/C) with train+test aggregates.
The final artifact for the "no reachable cell passes the gate" claim.

Usage: python -m market_backtest.campaign_summary
"""
from __future__ import annotations

import glob
import json
import os


def main() -> None:
    files = sorted(glob.glob("market_backtest/sweeps/run*.json"))
    rows = []
    for f in files:
        tag = os.path.basename(f)[3:-5]          # strip "run" prefix + ".json"
        try:
            data = json.load(open(f))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for cell, d in data.items():
            if not isinstance(d, dict) or "train" not in d or "test" not in d:
                continue
            tr, te = d["train"], d["test"]
            if not isinstance(tr, dict) or "mean" not in tr:
                continue
            rows.append((tag, cell,
                         tr.get("n", 0), tr.get("wr", 0.0), tr.get("mean", 0.0),
                         te.get("n", 0), te.get("wr", 0.0), te.get("mean", 0.0),
                         tr.get("pnl", 0.0), te.get("pnl", 0.0)))
    # rank by ACCOUNT TOTAL (balance units) — the user-facing metric.
    # A cell that trades less can lose less per account while showing a worse
    # per-trade mean; the opposite also happens with churn cells.
    rows.sort(key=lambda r: -r[9])

    hdr = (f"{'run':<15}{'cell':<18}{'trN':>6}{'trMean%':>9}{'trSum':>8}"
           f"{'teN':>6}{'teMean%':>9}{'teSum':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r[0]:<15}{r[1]:<18}{r[2]:>6}{r[4] * 100:>9.3f}{r[8]:>8.3f}"
              f"{r[5]:>6}{r[7] * 100:>9.3f}{r[9]:>8.3f}")

    pos_tr = [r for r in rows if r[8] > 0]
    pos_te = [r for r in rows if r[9] > 0]
    print(f"\ncells: {len(rows)}   train Σ > 0: {len(pos_tr)}   "
          f"test Σ > 0: {len(pos_te)}")
    for r in pos_tr:
        print(f"  TRAIN+ {r[0]}/{r[1]}: trΣ {r[8]:+.4f} teΣ {r[9]:+.4f}")
    for r in pos_te:
        print(f"  TEST+  {r[0]}/{r[1]}: trΣ {r[8]:+.4f} teΣ {r[9]:+.4f}")


if __name__ == "__main__":
    main()
