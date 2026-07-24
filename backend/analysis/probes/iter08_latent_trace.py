"""
Iter08 latent-trajectory probe for ONE recording.

Re-runs a backtest with engine_version=2 on a recording, with a probe
hook that snapshots the V2 latent state per bar (regime, mu, P_up,
P_down, Kramers k_up/k_down, barrier heights, signal_strength, trend_
confidence), and writes a per-bar CSV for analysis.

Usage:
    python backend/analysis/probes/iter08_latent_trace.py \
        --recording-id 482 --entry-time <int> --exit-time <int>
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from data_store import get_recording, get_recording_candles

# We need to monkey-patch the V2 engine to capture decision per bar.
from engine_factory import create_engine


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recording-id", type=int, required=True)
    ap.add_argument("--out", default=None, help="output CSV path")
    args = ap.parse_args()

    rec = get_recording(args.recording_id)
    if not rec:
        print(f"Recording {args.recording_id} not found", file=sys.stderr)
        sys.exit(2)
    candles = get_recording_candles(args.recording_id)
    if not candles:
        print(f"Recording {args.recording_id} has no candles", file=sys.stderr)
        sys.exit(2)
    print(f"Recording {args.recording_id}: {len(candles)} candles, timeframe={rec['timeframe']}")

    # Build a fresh V2 engine via the adapter
    eng = create_engine(engine_version=2)
    # Wrap core.get_decision so we capture every call's output
    rows = []
    orig_get_decision = eng.core.get_decision
    last_decision = {}
    def _wrapped(*a, **kw):
        nonlocal last_decision
        d = orig_get_decision(*a, **kw)
        last_decision = dict(d)
        return d
    eng.core.get_decision = _wrapped

    # Also intercept compute_potential_and_barriers so we capture U barriers
    orig_pot = eng.core.compute_potential_and_barriers
    last_potential = {}
    def _wrapped_pot(*a, **kw):
        nonlocal last_potential
        r = orig_pot(*a, **kw)
        last_potential = dict(r)
        return r
    eng.core.compute_potential_and_barriers = _wrapped_pot

    last_bar = 0
    for i, candle in enumerate(candles):
        t   = int(candle["time"])
        o   = candle["open"]
        h   = candle["high"]
        l   = candle["low"]
        c   = candle["close"]
        vol = candle.get("volume", 0)
        bv  = candle.get("buy_volume", 0.0)
        sv  = candle.get("sell_volume", 0.0)

        bullish = c >= o
        if bullish:
            mid_first, mid_second = h, l
        else:
            mid_first, mid_second = l, h

        # 4-state expansion (mirror backtester)
        last_decision.clear()
        last_potential.clear()
        states = [
            (o, o, o, o, 0.0, 0.0, 0.0),
            (o, max(o, mid_first), min(o, mid_first), mid_first, 0.0, 0.0, 0.0),
            (o, h, l, mid_second, 0.0, 0.0, 0.0),
            (o, h, l, c, vol, bv, sv),
        ]
        decision_after_state = None
        for si, (so, sh, sl, sc, svol, sbv, ssv) in enumerate(states):
            last_decision.clear()
            last_potential.clear()
            r = eng.update(t, so, sh, sl, sc, svol, sbv, ssv, _build_full_result=True)
            if si == 3:
                decision_after_state = dict(last_decision) if last_decision else {}

        # take state at end of full candle
        st = eng.core._last_state
        row = {
            "i": i,
            "t": t,
            "open": o, "high": h, "low": l, "close": c, "vol": vol,
            "v2_x": float(st.get("x", 0)),
            "v2_mu": float(st.get("mu", 0)),
            "v2_h": float(st.get("h", 0)),
            "v2_phi": float(st.get("phi", 0)),
            "v2_ell": float(st.get("ell", 0)),
            "v2_regime": int(st.get("regime", 0)),
            "v2_sigma_t": float(eng.core._last_sigma_t or 0),
            "P_up":   float(decision_after_state.get("P_up", 0) if decision_after_state else 0),
            "P_down": float(decision_after_state.get("P_down", 0) if decision_after_state else 0),
            "P_zero": float(decision_after_state.get("P_zero", 0) if decision_after_state else 0),
            "k_up":   float(decision_after_state.get("k_up", 0) if decision_after_state else 0),
            "k_down": float(decision_after_state.get("k_down", 0) if decision_after_state else 0),
            "E_star": float(decision_after_state.get("E_star", 0) if decision_after_state else 0),
            "n_star": float(decision_after_state.get("n_star", 0) if decision_after_state else 0),
            "dec_dir": int(decision_after_state.get("direction", 0) if decision_after_state else 0),
            "dec_tau": float(decision_after_state.get("tau", 0) if decision_after_state else 0),
            "bar_lev": last_potential.get("bar_count_last_price", 0),
            # Capture engine-adapter exposed indicators
            "m_hat": float(eng.m_hat),
            "trend_confidence": float(eng.trend_confidence),
            "signal_strength": float(eng.signal_strength),
            "s_effective": float(eng.s_effective),
            "atr": float(eng.atr_val or 0),
            "regime_v1": eng.regime.value,
            "signal_v1": r.get("signal", "none") if r else "none",
            "in_position": int(eng.in_position),
            "bar_count_eng": int(eng.bar_count),
            "mu_dot_post_ema": float(getattr(eng.core, "_mu_dot_post_ema", 0.0)),
        }
        rows.append(row)

    out_path = args.out or os.path.join(
        os.path.dirname(__file__), "..",
        f"iter08_latent_trace_rec{args.recording_id}.csv")
    keys = list(rows[0].keys()) if rows else []
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"Wrote {len(rows)} rows → {out_path}")
    # Print first / last / mid bars summary
    if rows:
        print("\nFirst bar:")
        for k, v in rows[0].items():
            print(f"  {k:20s} {v}")
        print("\nLast bar:")
        for k, v in rows[-1].items():
            print(f"  {k:20s} {v}")


if __name__ == "__main__":
    main()
