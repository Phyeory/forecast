"""recal_prop_report.py — consolidate mc_firms JSONs for the M11 re-proof.

Usage:
    python -m market_backtest.recal_prop_report \
        --baseline campaign=market_backtest/results/mc_arm14.json \
        --candidate recal=market_backtest/results/mc_recal.json \
        --out market_backtest/results/recal/prop_report.json

The mc_firms schema is `{tag, seg, empirical, templates: {tpl: {k: row}}}`.
This prints the deployment-relevant columns only (P1 / full / funded blown /
EV per attempt / $/mo at reset) and the candidate-minus-baseline EV.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(spec: str) -> tuple[str, dict]:
    label, _, path = spec.partition("=")
    if not path:
        label, path = Path(spec).stem, spec
    return label, json.loads(Path(path).read_text())


def _fmt_row(r: dict) -> dict:
    fu = r.get("funded", {})
    fur = r.get("funded_reset", {})
    return {
        "p1": r.get("p1", 0.0),
        "p_full": r.get("p_full", 0.0),
        "blown_1y": fu.get("p_blown", 0.0),
        "ev": r.get("ev_per_attempt", 0.0),
        "ev_reset": r.get("ev_per_attempt_reset", 0.0),
        "payout_reset": r.get("payout_ev_reset_usd", 0.0),
        "per_month": r.get("payout_ev_reset_usd", 0.0) / 12.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="")
    ap.add_argument("--candidate", action="append", default=[])
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    base = _load(args.baseline)[1] if args.baseline else None
    cands = [_load(c) for c in args.candidate]
    rows = {}
    for label, data in cands:
        tpls = data.get("templates", {})
        rows[label] = {}
        print(f"\n── {label}  ({data.get('tag', '')}, seg {data.get('seg', '')})")
        for tpl, ks in tpls.items():
            print(f"  {tpl}")
            print(f"    {'k':>5}{'P1%':>7}{'full%':>7}{'blown%':>8}"
                  f"{'EV$':>9}{'EVreset$':>10}{'$/mo':>9}"
                  f"{'ΔEV vs base':>13}")
            rows[label][tpl] = {}
            for k, r in ks.items():
                f = _fmt_row(r)
                delta = ""
                if base and tpl in base.get("templates", {}) \
                        and k in base["templates"][tpl]:
                    b = _fmt_row(base["templates"][tpl][k])
                    delta = f"{f['ev_reset'] - b['ev_reset']:+,.0f}"
                print(f"    {float(k):>5.2f}{f['p1']*100:>7.1f}"
                      f"{f['p_full']*100:>7.1f}{f['blown_1y']*100:>8.1f}"
                      f"{f['ev']:>9,.0f}{f['ev_reset']:>10,.0f}"
                      f"{f['per_month']:>9,.0f}{delta:>13}")
                rows[label][tpl][k] = {**f, "delta_ev_reset_vs_base":
                                       (f["ev_reset"] - _fmt_row(
                                           base["templates"][tpl][k])["ev_reset"]
                                        if base and tpl in base.get("templates", {})
                                        and k in base["templates"][tpl] else None)}
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1))
        print(f"\n[report] → {args.out}")


if __name__ == "__main__":
    main()
