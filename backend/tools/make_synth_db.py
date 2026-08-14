"""Generate a synthetic price_data.db so backtest performance can be profiled
without the (untracked, multi-GB) production recordings.

Usage:
    python backend/tools/make_synth_db.py --recordings 40 --candles 900

Produces recordings whose candle counts and volatility resemble the real
pump.fun 1s memecoin recordings (fast launch ramp + decay + chop).
"""
from __future__ import annotations

import argparse
import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import data_store as ds


def _synth_candles(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    px = rng.uniform(2e-8, 8e-7)
    t0 = 1_700_000_000 + seed * 10_000
    out = []
    # launch ramp for the first ~15%, then decay/chop
    ramp = int(n * 0.15)
    for i in range(n):
        if i < ramp:
            drift = rng.uniform(0.002, 0.02)
        else:
            drift = rng.gauss(-0.0008, 0.012)
        o = px
        px = max(px * (1.0 + drift), 1e-12)
        c = px
        hi = max(o, c) * (1.0 + abs(rng.gauss(0, 0.004)))
        lo = min(o, c) * (1.0 - abs(rng.gauss(0, 0.004)))
        vol = abs(rng.gauss(30, 25)) + 0.5
        buy = vol * rng.uniform(0.3, 0.7)
        out.append(
            {
                "time": t0 + i,
                "open": o,
                "high": hi,
                "low": lo,
                "close": c,
                "volume": vol,
                "buy_volume": buy,
                "sell_volume": vol - buy,
                "trade_count": int(abs(rng.gauss(20, 10))) + 1,
                "pool_sol": abs(rng.gauss(60, 20)) + 5,
                "market_cap_usd": px * 1e9 * 150,
            }
        )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recordings", type=int, default=40)
    ap.add_argument("--candles", type=int, default=900)
    ap.add_argument("--jitter", type=float, default=0.4,
                    help="fractional random variation in candle count")
    args = ap.parse_args()

    ds.init_price_db()
    ds.init_backtest_db()
    rng = random.Random(1234)

    for k in range(args.recordings):
        n = int(args.candles * (1.0 + rng.uniform(-args.jitter, args.jitter)))
        n = max(120, n)
        rid = ds.create_recording(
            mint=f"SYNTH{k:04d}" + "x" * 28,
            timeframe="1s",
            token_name=f"Synth Token {k}",
            token_symbol=f"SYN{k}",
        )
        ds.insert_candles_batch(rid, _synth_candles(n, seed=k + 1))
        ds.stop_recording(rid)
        ds.update_recording_candle_count(rid)

    recs = [r for r in ds.list_recordings() if r.get("status") == "completed"]
    total = sum(r.get("candle_count") or 0 for r in recs)
    print(f"synthetic DB ready: {len(recs)} completed recordings, {total} candles")


if __name__ == "__main__":
    main()
