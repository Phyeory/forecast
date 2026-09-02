"""Run a V2 batch backtest, then aggregate its results.

This is the canonical "Iteration step" entry point:

  python run_iteration.py --label iter01_baseline [--params <file.json>]

  1. Run backtest_batch on all completed recordings (engine_version=2)
  2. Save the run's batch_id (time-based) and stamp each per-trade logfile
  3. Run analysis/aggregate_results.py against that batch_id
  4. Append header + headline numbers to RESEARCH_LOG.md
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
from collections import defaultdict

sys.path.append(os.path.join(os.path.dirname(__file__), "backend"))
from backtester import run_backtest_batch
from analysis.aggregate_results import _gather, _aggregate, print_summary


def _engine_params_from_file(p: str | None) -> dict:
    if not p:
        return {}
    with open(p) as f:
        return json.load(f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, help="iteration label e.g. iter01_baseline")
    ap.add_argument("--params", default=None, help="path to JSON with V2 engine params override")
    ap.add_argument("--buy-size-sol", type=float, default=0.1)
    ap.add_argument("--max-workers", type=int, default=8,
                    help="worker processes (default: 8)")
    ap.add_argument("--engine-version", type=int, default=2,
                    help="strategy engine version (default: 2; 4=V4 post-nuke "
                         "reversion, 6=V6 insider-dump absorption)")
    ap.add_argument("--recording-ids", nargs="*", type=int, default=None,
                    help="restrict to a subset of recording IDs")
    ap.add_argument("--recording-ids-file", default=None,
                    help="JSON file containing a list of recording_ids to restrict to")
    # iter78: exec-latency overlay cells (iter73's pending first study) —
    # additive, default 0.0 = instant-fill baseline, byte-identical.
    ap.add_argument("--entry-latency-seconds", type=float, default=0.0,
                    help="defer entry fills to t_signal + N s on the recorded path (iter73 overlay)")
    ap.add_argument("--exit-latency-seconds", type=float, default=0.0,
                    help="defer exit fills to t_signal + N s on the recorded path (iter73 overlay)")
    args = ap.parse_args()

    params = _engine_params_from_file(args.params)
    recording_ids = args.recording_ids
    if recording_ids is None and args.recording_ids_file:
        with open(args.recording_ids_file) as f:
            recording_ids = json.load(f)
    batch_id = args.label + "_" + str(int(time.time()))
    t0 = time.time()
    max_workers = args.max_workers
    print(f"Running backtest batch (engine_version={args.engine_version}, "
          f"batch_id={batch_id})...", flush=True)

    results = run_backtest_batch(
        engine_version=args.engine_version,
        engine_params=params,
        buy_size_sol=args.buy_size_sol,
        max_workers=max_workers,
        batch_id=batch_id,
        recording_ids=recording_ids,
        entry_latency_seconds=args.entry_latency_seconds,
        exit_latency_seconds=args.exit_latency_seconds,
    )
    elapsed = time.time() - t0
    errors_total = sum(1 for r in results if "error" in r)
    print(f"\nCompleted {len(results)} backtests in {elapsed:.1f}s  |  errors: {errors_total}", flush=True)

    # Aggregate from per-trade JSON
    agg_records = _gather(batch_id)
    if not agg_records:
        print("WARNING: no per-trade JSON files found — tokens made no trades?", file=sys.stderr)
        return
    agg = _aggregate(agg_records)
    print_summary(args.label, agg)

    # Save aggregate JSON analysis
    out_path = os.path.join(os.path.dirname(__file__), "backend", "analysis", f"{args.label}.json")
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(agg, f, indent=2, default=str)
        print(f"\nSaved aggregate analysis → {out_path}")
    except Exception as e:
        print(f"Could not save aggregate JSON: {e}", file=sys.stderr)

    # One-line headline to stdout
    print(f"\nHEADLINE | {args.label} | trades={agg['total_trades']} "
          f"wr={agg['win_rate_pct']:.1f}% pnl={agg['total_pnl_sol']:.4f} "
          f"pf={agg['profit_factor']:.2f} exp={agg['expectancy_sol']:.5f} "
          f"errors={errors_total} elapsed={elapsed:.0f}s")


if __name__ == "__main__":
    main()
