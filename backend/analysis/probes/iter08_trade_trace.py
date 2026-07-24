"""
Iter08 single-trade latent-state deep trace.

Runs a real V2 backtest on a recording but INSTRUMENTS the engine to capture
per-candle latent state and the kramers decision. Then extracts state only
for the bar window of a specified trade.

Usage:
    python backend/analysis/probes/iter08_trade_trace.py \
        --recording-id 482 \
        --entry-time 1780433068 \
        --exit-time 1780443541
"""
from __future__ import annotations
import argparse
import csv
import os
import sys
import math

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
from data_store import get_recording, get_recording_candles
from engine_factory import create_engine


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recording-id", type=int, required=True)
    ap.add_argument("--entry-time", type=int, required=True)
    ap.add_argument("--exit-time", type=int, required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    rec = get_recording(args.recording_id)
    if not rec:
        sys.exit(f"Recording {args.recording_id} not found")
    candles = get_recording_candles(args.recording_id)
    print(f"Recording {args.recording_id}: {len(candles)} candles")

    eng = create_engine(engine_version=2)

    # Snapshot latest decision after EACH state-update of every 4-state expansion.
    snapshots_per_bar: list[dict] = []
    last_decision = {}
    last_pot = {}

    orig_get_decision = eng.core.get_decision
    orig_pot = eng.core.compute_potential_and_barriers

    def _wrap_dec(*a, **kw):
        nonlocal last_decision
        last_decision = dict(orig_get_decision(*a, **kw))
        return last_decision
    def _wrap_pot(*a, **kw):
        nonlocal last_pot
        r = dict(orig_pot(*a, **kw))
        last_pot = r
        return r
    eng.core.get_decision = _wrap_dec
    eng.core.compute_potential_and_barriers = _wrap_pot

    for i, candle in enumerate(candles):
        t   = int(candle["time"])
        o   = candle["open"]; h = candle["high"]; l = candle["low"]; c = candle["close"]
        vol = candle.get("volume", 0)
        bv  = candle.get("buy_volume", 0.0)
        sv  = candle.get("sell_volume", 0.0)

        bullish = c >= o
        if bullish:
            mid_first, mid_second = h, l
        else:
            mid_first, mid_second = l, h

        # Mirror backtester — all 4 states fast-path false (we need full result)
        # so the potential is computed every bar.
        results = []
        for so, sh, sl, sc, svol, sbv, ssv in [
            (o, o, o, o, 0.0, 0.0, 0.0),
            (o, max(o, mid_first), min(o, mid_first), mid_first, 0.0, 0.0, 0.0),
            (o, h, l, mid_second, 0.0, 0.0, 0.0),
            (o, h, l, c, vol, bv, sv),
        ]:
            last_decision = {}
            last_pot = {}
            r = eng.update(t, so, sh, sl, sc, svol, sbv, ssv, _build_full_result=True)
            results.append((r, dict(last_decision) if last_decision else {}, dict(last_pot) if last_pot else {}))

        # Final-state snapshot for this bar.
        full_r, dec, pot = results[-1]
        st = eng.core._last_state
        # Pull barrier heights from the market potential's cached grid.
        U_grid = eng.core.potential.last_U
        # The split-side potentials only exist after compute_potential_and_barriers();
        # fall back to the symmetric field when not yet populated.
        U_up = getattr(eng.core.potential, "last_U_up", None)
        U_down = getattr(eng.core.potential, "last_U_down", None)
        grid = eng.core.potential.last_grid
        rho = eng.core.potential.last_rho
        T_t = eng.core.potential.last_T
        sigma_t = getattr(eng.core.potential, "last_sigma_t", None) or eng.core._last_sigma_t
        span = getattr(eng.core.potential, "last_grid_span", 0.0)
        # Find x_t's index in the grid
        x_t = float(st.get("x", 0))
        if grid is not None and len(grid) > 0:
            idx_t = int((x_t - (x_t - span)) / (2 * span) * (len(grid) - 1)) if span > 0 else 0
            idx_t = max(0, min(len(grid) - 1, idx_t))
            U_basin = float(U_grid[idx_t]) if U_grid is not None else 0.0
        else:
            idx_t = -1
            U_basin = 0.0

        # depth avg
        depth = eng.core.potential.last_depth

        snapshots_per_bar.append({
            "i": i,
            "t": t,
            "open": o, "high": h, "low": l, "close": c, "vol": vol,
            # State
            "x_t": float(st.get("x", 0)),
            "mu": float(st.get("mu", 0)),
            "h": float(st.get("h", 0)),
            "phi": float(st.get("phi", 0)),
            "ell": float(st.get("ell", 0)),
            "v2_regime": int(st.get("regime", 0)),
            "sigma_t": float(sigma_t or 0),
            # Decision
            "P_up":   float(dec.get("P_up", 0)),
            "P_down": float(dec.get("P_down", 0)),
            "P_zero": float(dec.get("P_zero", 0)),
            "k_up":   float(dec.get("k_up", 0)),
            "k_down": float(dec.get("k_down", 0)),
            "E_star": float(dec.get("E_star", 0)),
            "dec_dir": int(dec.get("direction", 0)),
            # adapter exposed
            "m_hat": float(eng.m_hat),
            "regime_v1": eng.regime.value,
            "direction_v1": eng.direction.value,
            "signal_v1": full_r.get("signal", "none") if full_r else "none",
            "trend_confidence": float(eng.trend_confidence),
            "signal_strength": float(eng.signal_strength),
            "s_effective": float(eng.s_effective),
            "atr": float(eng.atr_val or 0),
            "in_position": int(eng.in_position),
            # barrier geometry (at x_t)
            "T_t": float(T_t or 0),
            "U_basin": float(U_basin),
            "grid_idx_t": idx_t,
            "grid_span": float(span or 0),
            "rho_max_above": float(rho.max()) if rho is not None and len(rho) else 0,
            "rho_at_x_t": float(rho[idx_t]) if rho is not None and 0 <= idx_t < len(rho) else 0,
            "U_basin_up":   float(U_up[idx_t]) if U_up is not None and 0 <= idx_t < len(U_up) else 0,
            "U_basin_down": float(U_down[idx_t]) if U_down is not None and 0 <= idx_t < len(U_down) else 0,
            "peak_U_up":   float(U_up.max())    if U_up is not None else 0,
            "min_U_up":    float(U_up.min())     if U_up is not None else 0,
            "peak_U_down": float(U_down.max())   if U_down is not None else 0,
            "min_U_down":  float(U_down.min())    if U_down is not None else 0,
            # mu_dot_post plumbing (already on core)
            "mu_dot_post_ema": float(getattr(eng.core, "_mu_dot_post_ema", 0)),
        })

    # Filter to the requested trade window.
    rows = [s for s in snapshots_per_bar if args.entry_time <= s["t"] <= args.exit_time]
    print(f"Trade window: bars {len(rows)} from t={args.entry_time} to t={args.exit_time}")

    out_path = args.out or os.path.join(
        os.path.dirname(__file__), "..",
        f"iter08_trade_trace_rec{args.recording_id}.csv")
    keys = list(rows[0].keys()) if rows else []
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"Wrote {len(rows)} rows → {out_path}")

    # Summary statistics: distribution of key indicators over the trade window
    if rows:
        print("\nKey indicator ranges across trade window:")
        def stat(name, vals):
            vals2 = sorted(vals)
            n = len(vals2)
            return (f"  {name:25s} min={vals2[0]:+.6g}  "
                    f"p25={vals2[n//4]:+.6g}  med={vals2[n//2]:+.6g}  "
                    f"p75={vals2[3*n//4]:+.6g}  max={vals2[-1]:+.6g}")
        print(stat("close", [r["close"] for r in rows]))
        print(stat("mu",  [r["mu"] for r in rows]))
        print(stat("ml_hat", [r["m_hat"] for r in rows]))
        print(stat("trend_confidence", [r["trend_confidence"] for r in rows]))
        print(stat("P_up",   [r["P_up"]   for r in rows]))
        print(stat("P_down", [r["P_down"] for r in rows]))
        print(stat("P_zero", [r["P_zero"] for r in rows]))
        print(stat("k_up",   [r["k_up"]   for r in rows]))
        print(stat("k_down", [r["k_down"] for r in rows]))
        print(stat("E_star", [r["E_star"] for r in rows]))
        print(stat("T_t",    [r["T_t"]    for r in rows]))
        print(stat("U_basin", [r["U_basin"] for r in rows]))
        print(stat("peak_U_up", [r["peak_U_up"] for r in rows]))
        print(stat("peak_U_down", [r["peak_U_down"] for r in rows]))
        print(stat("mu_dot_post_ema", [r["mu_dot_post_ema"] for r in rows]))
        # how often is regime REVERSAL?
        n_rev = sum(1 for r in rows if r["v2_regime"] == 6)
        n_trend = sum(1 for r in rows if r["v2_regime"] == 2)
        print(f"  v2_regime==reversal(6): {n_rev} / {len(rows)}    v2_regime==trend(2): {n_trend}")


if __name__ == "__main__":
    main()
