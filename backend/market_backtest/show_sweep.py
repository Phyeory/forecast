"""show_sweep.py — formatted readout of a sweep run JSON.

Usage: python -m market_backtest.show_sweep market_backtest/sweeps/runevrprobe.json [--sort train|test]
Prints per-cell train/test aggregates + exit decomposition + exit counts.
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--sort", default="train", choices=["train", "test"])
    ap.add_argument("--exit-n", action="store_true", help="show exit reason counts")
    args = ap.parse_args()

    data = json.loads(open(args.file).read())
    rows = []
    for cell, d in data.items():
        if not isinstance(d, dict) or "train" not in d:
            continue
        rows.append((cell, d["train"], d["test"]))
    rows.sort(key=lambda r: -r[1]["mean"] if args.sort == "train" else -r[2]["mean"])

    hdr = (f"{'cell':<16}{'trN':>6}{'trWR':>7}{'trPnL':>9}{'trMean%':>9}{'tr+':>6}"
           f" | {'teN':>6}{'teWR':>7}{'tePnL':>9}{'teMean%':>9}{'te+':>6}")
    print(hdr)
    print("-" * len(hdr))
    for cell, tr, te in rows:
        print(f"{cell:<16}{tr['n']:>6}{tr['wr']:>7.1f}{tr['pnl']:>9.3f}"
              f"{tr['mean'] * 100:>9.3f}{tr['symbols_pos']:>6}"
              f" | {te['n']:>6}{te['wr']:>7.1f}{te['pnl']:>9.3f}"
              f"{te['mean'] * 100:>9.3f}{te['symbols_pos']:>6}")

    print("\nexit-pnl decomposition (train | test):")
    for cell, tr, te in rows:
        print(f"  {cell:<16} tr {tr.get('exit_pnl')}")
        print(f"  {'':<16} te {te.get('exit_pnl')}")
        if args.exit_n:
            print(f"  {'':<16} tr-n {tr.get('exit_n')}")
            print(f"  {'':<16} te-n {te.get('exit_n')}")


if __name__ == "__main__":
    main()
