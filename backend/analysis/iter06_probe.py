"""
iter06 hypothesis probe — capture barrier heights (du_up, du_down, T_t,
P_up, P_down, P_zero, mu_hat, peak_rho_pos) at every trade entry, then
correlate with the eventual pnl.

Goal: empirically validate that the spec §3 barrier-adjusted signal
        S_eff_spec = (|m_hat| / ATR) / max(|du_up| / T_t, eps)
    separates the iter04 worst-30 (slow-bleed catastrophic losers)
    from the best-30 (strong winners) better than the V2 raw
    `signal_strength = |m_hat_pct| / atr_pct` does.

Usage:
    BACKTEST_RESULTS_DIR=backend/v2_results \
      python backend/analysis/iter06_probe.py \
        --recording-ids-file /tmp/iter06_probe_recids.json \
        --label iter06_probe --max-workers 4
"""
from __future__ import annotations
import os, sys, json, time, glob, argparse, sqlite3
from dataclasses import dataclass, field
from typing import Any
import multiprocessing as mp

# Set the BACKTEST_RESULTS_DIR BEFORE importing backtester (which reads it at import time).
_DEFAULT_RESULTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'v2_results'))
os.environ.setdefault("BACKTEST_RESULTS_DIR", _DEFAULT_RESULTS_DIR)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# We monkey-patch `forward_tester.ForwardTester._capture_entry_params`
# to inject the new barrier-context fields into the entry snapshot.
import forward_tester as ftm
import strategy_engineV2 as se2
import numpy as np
from process_watchdog import guard_parent


# ── V2 adapter hook ────────────────────────────────────────────────────────
def _v2_spec_s_effective(eng: se2.StrategyEngineV2Adapter) -> dict:
    """
    Probe-only helper.  Inspect the V2 core's last-computed potential
    from the active session and compute:

      du_up_norm   = (U(x_up_barrier)   - U(x_t)) / T_t   # entropic barrier only
                  uses _U_entropy = -T_t * log(rho) only (liquidity cost removed)
      rho_up_pos   = grid_idx(highest rho peak above idx_t) - idx_t (in grid units)
      rho_down_pos = grid_idx(highest rho peak below idx_t) - idx_t
      rho_up_max   = rho at that sub-peak
      rho_down_max = rho at that sub-peak
      S_eff_spec   = signal_strength / max(du_up_norm, ε)   [long-only]

    Also compute ENTROPY-ONLY barriers (V_liq removed) and the
    "first upward rho GAP" — the distance in grid index from idx_t to the
    first frame where rho drops to < 0.5 × rho_at_t.  This corresponds to
    the entropic wall encountered moving upward in log-price space; when
    the wall is close, we expect momentum to stall (low du_up_norm).

    All quantities are READ-ONLY here — no state mutation.
    """
    core = getattr(eng, "core", None)
    if core is None or core._last_potential is None:
        return dict(spec_s_eff=0.0, du_up_norm=0.0, du_down_norm=0.0,
                    rho_up_pos=0, rho_down_pos=0,
                    rho_up_max=0.0, rho_down_max=0.0,
                    T_t=0.0, idx_up=0, idx_down=0, rho_max=0.0, rho_at_t=0.0,
                    U_entropy_up=0.0, U_entropy_down=0.0, U_entropy_basin=0.0,
                    grid_span=0.0, sigma_t_V2=0.0, dist_x_to_up=0.0, dist_x_to_down=0.0,
                    rho_drop_50_up=0, rho_drop_10_up=0,
                    rho_drop_50_down=0, rho_drop_10_down=0,
                    U_ent_up_norm=0.0, U_ent_down_norm=0.0,
                    U_ent_drop_50_up=0.0, U_ent_drop_10_up=0.0,
                    U_ent_drop_50_down=0.0, U_ent_drop_10_down=0.0,
                    spec_s_eff_ent=0.0, spec_s_eff_drop50=0.0)
    pot = core._last_potential
    grid = pot["grid"]
    rho = pot["rho"]
    T_t = float(pot["T_t"])
    st = core._last_state
    x_t = float(st["x"]) if st else 0.0
    if grid is None or rho is None or len(grid) == 0:
        return dict(spec_s_eff=0.0, du_up_norm=0.0, du_down_norm=0.0,
                    rho_up_pos=0, rho_down_pos=0,
                    rho_up_max=0.0, rho_down_max=0.0,
                    T_t=0.0, idx_up=0, idx_down=0, rho_max=0.0, rho_at_t=0.0,
                    U_entropy_up=0.0, U_entropy_down=0.0, U_entropy_basin=0.0,
                    grid_span=0.0, sigma_t_V2=0.0, dist_x_to_up=0.0, dist_x_to_down=0.0,
                    rho_drop_50_up=0, rho_drop_10_up=0,
                    rho_drop_50_down=0, rho_drop_10_down=0,
                    U_ent_up_norm=0.0, U_ent_down_norm=0.0,
                    U_ent_drop_50_up=0.0, U_ent_drop_10_up=0.0,
                    U_ent_drop_50_down=0.0, U_ent_drop_10_down=0.0,
                    spec_s_eff_ent=0.0, spec_s_eff_drop50=0.0)
    idx_t = int(np.argmin(np.abs(grid - x_t)))
    eps_rho = 1e-12
    U_ent = -T_t * np.log(np.maximum(rho, eps_rho))
    U_ent_basin = float(U_ent[idx_t])
    # Left & right highest DENSITY sub-peak (entropic MINIMUM - lowest U_ent)
    # Scan right of idx_t: find the minimum of U_ent (= rho peak) before coming
    # back UP > delta. Take idx_up = the minimum of U_ent after the FIRST peak.
    n = rho.size
    # Build pseudo U_ent barriers via _barrier_find_kernel (looks for max U == rho valley)
    idx_basin, idx_up, idx_down, U_basin_p, U_up_p, U_down_p = se2._barrier_find_kernel(U_ent, idx_t)
    du_up = float(U_up_p - U_basin_p)
    du_down = float(U_down_p - U_basin_p)
    du_up_norm = du_up / max(T_t, 1e-12)
    du_down_norm = du_down / max(T_t, 1e-12)
    rho_max = float(rho.max())
    rho_at_t = float(rho[idx_t])
    grid_span = float(grid[-1] - grid[0])

    # find HIGHEST rho peak strictly above / below x_t
    right = rho[idx_t + 1:] if idx_t + 1 < n else np.array([])
    left  = rho[:idx_t] if idx_t > 0 else np.array([])
    idx_up_pos = int(np.argmax(right)) + (idx_t + 1) if right.size else idx_t
    idx_down_pos = int(np.argmax(left)) if left.size else 0
    rho_up_max = float(rho[idx_up_pos]) if 0 <= idx_up_pos < n else 0.0
    rho_down_max = float(rho[idx_down_pos]) if 0 <= idx_down_pos < n else 0.0
    rho_up_pos = idx_up_pos - idx_t
    rho_down_pos = idx_down_pos - idx_t
    sigma_t_V2 = float(pot.get("sigma_t", 0.0))
    dist_x_to_up   = float(grid[idx_up_pos]   - x_t) if 0 <= idx_up_pos < n else 0.0
    dist_x_to_down = float(grid[idx_down_pos] - x_t) if 0 <= idx_down_pos < n else 0.0

    S = float(eng.signal_strength or 0.0)
    eps = 1e-3
    spec_s_eff = S / max(du_up_norm, eps) if du_up_norm > 0 else 0.0
    spec_s_eff_ent = S / max(du_up_norm, eps) if du_up_norm > 0 else 0.0

    # ALSO compute the "rho drop" — distance from idx_t along the grid until rho
    # first drops below 0.5 × rho_at_t.  This is the entropic wall location in grid
    # units; closer wall ⇒ smaller room to move ⇒ expect smaller momentum.
    def _first_drop(rho_arr, idx_t_local, factor, step):
        thresh = factor * rho_at_t
        i = idx_t_local + step
        while 0 <= i < n:
            if rho_arr[i] <= thresh:
                return i - idx_t_local  # grid index offset (negative for left side)
            i += step
        return (n - 1 - idx_t_local) * step  # hit boundary

    rho_drop_50_up = _first_drop(rho, idx_t, 0.5, +1)
    rho_drop_10_up = _first_drop(rho, idx_t, 0.1, +1)
    rho_drop_50_down = _first_drop(rho, idx_t, 0.5, -1)
    rho_drop_10_down = _first_drop(rho, idx_t, 0.1, -1)

    # Entropy at the rho_drop_50 locations (showing actual wall heights above basin)
    U_ent_drop_50_up = 0.0
    U_ent_drop_10_up = 0.0
    U_ent_drop_50_down = 0.0
    U_ent_drop_10_down = 0.0
    if 0 <= idx_t + rho_drop_50_up < n:
        U_ent_drop_50_up = float(U_ent[idx_t + rho_drop_50_up] - U_ent_basin)
    if 0 <= idx_t + rho_drop_10_up < n:
        U_ent_drop_10_up = float(U_ent[idx_t + rho_drop_10_up] - U_ent_basin)
    if 0 <= idx_t + rho_drop_50_down < n:
        U_ent_drop_50_down = float(U_ent[idx_t + rho_drop_50_down] - U_ent_basin)
    if 0 <= idx_t + rho_drop_10_down < n:
        U_ent_drop_10_down = float(U_ent[idx_t + rho_drop_10_down] - U_ent_basin)
    # Wall heights normalized by T_t (i.e. in "kT" units)
    U_ent_up_norm = U_ent_drop_50_up / max(T_t, 1e-12) if T_t > 0 else 0.0
    U_ent_down_norm = U_ent_drop_50_down / max(T_t, 1e-12) if T_t > 0 else 0.0
    spec_s_eff_drop50 = S / max(U_ent_up_norm, eps) if U_ent_up_norm > 0 else 0.0

    return dict(
        spec_s_eff=float(spec_s_eff),
        spec_s_eff_ent=float(spec_s_eff_ent),
        spec_s_eff_drop50=float(spec_s_eff_drop50),
        du_up_norm=float(du_up_norm),
        du_down_norm=float(du_down_norm),
        T_t=float(T_t),
        idx_up=int(idx_up), idx_down=int(idx_down),
        rho_max=rho_max, rho_at_t=rho_at_t,
        U_entropy_up=float(U_up_p), U_entropy_down=float(U_down_p),
        U_entropy_basin=float(U_ent_basin),
        grid_span=grid_span, sigma_t_V2=sigma_t_V2,
        rho_up_pos=rho_up_pos, rho_down_pos=rho_down_pos,
        rho_up_max=rho_up_max, rho_down_max=rho_down_max,
        dist_x_to_up=dist_x_to_up, dist_x_to_down=dist_x_to_down,
        rho_drop_50_up=int(rho_drop_50_up), rho_drop_10_up=int(rho_drop_10_up),
        rho_drop_50_down=int(rho_drop_50_down), rho_drop_10_down=int(rho_drop_10_down),
        U_ent_up_norm=float(U_ent_up_norm), U_ent_down_norm=float(U_ent_down_norm),
        U_ent_drop_50_up=float(U_ent_drop_50_up), U_ent_drop_10_up=float(U_ent_drop_10_up),
        U_ent_drop_50_down=float(U_ent_drop_50_down), U_ent_drop_10_down=float(U_ent_drop_10_down),
    )


# Original captor we wrap
_orig_capture_entry = ftm.ForwardTester._capture_entry_params

# Cache the most recent kramers decision so we can correlate with trade outcome.
import collections as _c
_last_decision_snapshot: dict = {}

# Patch the core engine's get_decision to store its return on `_last_decision`.
_orig_get_decision = se2.MemecoinStrategyEngine.get_decision


def _patched_get_decision(self, horizon: int = 30) -> dict:
    res = _orig_get_decision(self, horizon=horizon)
    try:
        self._last_decision = res
    except Exception:
        pass
    return res


se2.MemecoinStrategyEngine.get_decision = _patched_get_decision


def _patched_capture_entry(self, exec_price: float):
    snap = _orig_capture_entry(self, exec_price)
    eng = self.engine
    # Add the latest engine decision snapshot
    if isinstance(eng, se2.StrategyEngineV2Adapter):
        try:
            barrier_ctx = _v2_spec_s_effective(eng)
            snap.update(barrier_ctx)
            snap["__iter06_probe__"] = True
            # Read the latest decision from the core engine (set via patched get_decision).
            dec = getattr(getattr(eng, "core", eng), "_last_decision", None)
            if isinstance(dec, dict):
                snap["dec_P_up"]   = float(dec.get("P_up",   0.0))
                snap["dec_P_down"] = float(dec.get("P_down", 0.0))
                snap["dec_P_zero"] = float(dec.get("P_zero", 0.0))
                snap["dec_k_up"]   = float(dec.get("k_up",   0.0))
                snap["dec_k_down"] = float(dec.get("k_down", 0.0))
                snap["dec_k_total"]= float(dec.get("k_total",0.0))
                snap["dec_du_up"]  = float(dec.get("du_up",  0.0))
                snap["dec_du_down"]= float(dec.get("du_down",0.0))
                snap["dec_mu_hat_tau"]      = float(dec.get("mu_hat_tau", 0.0))
                snap["dec_n_star"]          = float(dec.get("n_star",      0.0))
                snap["dec_E_star"]          = float(dec.get("E_star",     0.0))
                snap["dec_direction"]       = int(dec.get("direction", 0))
                snap["dec_P_up_minus_down"] = snap["dec_P_up"] - snap["dec_P_down"]
                snap["dec_P_up_div_down"]   = (snap["dec_P_up"] / max(snap["dec_P_down"], 1e-15))
                snap["dec_du_up_minus_down"] = snap["dec_du_up"] - snap["dec_du_down"]
        except Exception as e:
            snap["__iter06_probe_err__"] = repr(e)
    return snap


ftm.ForwardTester._capture_entry_params = _patched_capture_entry


# ── Backtest batch runner (boilerplate copied from backtester.py) ───────────
def run_one_recording(rec_id, label):
    from backtester import run_backtest
    out_dir = os.environ["BACKTEST_RESULTS_DIR"]
    os.makedirs(out_dir, exist_ok=True)
    try:
        result = run_backtest(
            recording_id=rec_id,
            engine_params={},
            engine_version=2,
            batch_id=label,
        )
        return rec_id, result
    except Exception as e:
        return rec_id, {"error": repr(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recording-ids-file", required=True)
    ap.add_argument("--label", default="iter06_probe")
    ap.add_argument("--max-workers", type=int, default=4)
    args = ap.parse_args()
    with open(args.recording_ids_file) as f:
        recs = json.load(f)
    print(f"probing {len(recs)} recordings on {args.max_workers} workers", flush=True)

    with mp.Pool(args.max_workers, initializer=guard_parent) as pool:
        out = pool.starmap(run_one_recording, [(r, args.label) for r in recs])

    # Now load all output JSONs into a trade-level aggregate
    out_dir = os.environ["BACKTEST_RESULTS_DIR"]
    rows = []
    for rec_id, _ in out:
        # Find the JSON we just wrote
        sticks = glob.glob(os.path.join(out_dir, f"*_rec{rec_id}_{args.label}_*.json"))
        if not sticks:
            continue
        d = json.load(open(sticks[-1]))
        for tt in d.get("trades", []):
            ep = tt.get("entry_params", {})
            if not ep.get("__iter06_probe__"):
                continue
            rows.append(dict(
                rec=rec_id, sym=d.get("token_symbol", ""),
                pnl_pct=tt["pnl_pct"], pnl_sol=tt["pnl_sol"], outcome=tt.get("outcome", ""),
                spec_s_eff=ep.get("spec_s_eff", 0.0),
                spec_s_eff_ent=ep.get("spec_s_eff_ent", 0.0),
                spec_s_eff_drop50=ep.get("spec_s_eff_drop50", 0.0),
                du_up_norm=ep.get("du_up_norm", 0.0),
                du_down_norm=ep.get("du_down_norm", 0.0),
                rho_up_pos=ep.get("rho_up_pos", 0),
                rho_down_pos=ep.get("rho_down_pos", 0),
                rho_up_max=ep.get("rho_up_max", 0.0),
                rho_down_max=ep.get("rho_down_max", 0.0),
                T_t=ep.get("T_t", 0.0),
                rho_at_t=ep.get("rho_at_t", 0.0),
                rho_max=ep.get("rho_max", 0.0),
                U_entropy_up=ep.get("U_entropy_up", 0.0),
                U_entropy_down=ep.get("U_entropy_down", 0.0),
                U_entropy_basin=ep.get("U_entropy_basin", 0.0),
                grid_span=ep.get("grid_span", 0.0),
                sigma_t_V2=ep.get("sigma_t_V2", 0.0),
                dist_x_to_up=ep.get("dist_x_to_up", 0.0),
                dist_x_to_down=ep.get("dist_x_to_down", 0.0),
                rho_drop_50_up=ep.get("rho_drop_50_up", 0),
                rho_drop_10_up=ep.get("rho_drop_10_up", 0),
                rho_drop_50_down=ep.get("rho_drop_50_down", 0),
                rho_drop_10_down=ep.get("rho_drop_10_down", 0),
                U_ent_up_norm=ep.get("U_ent_up_norm", 0.0),
                U_ent_down_norm=ep.get("U_ent_down_norm", 0.0),
                U_ent_drop_50_up=ep.get("U_ent_drop_50_up", 0.0),
                U_ent_drop_10_up=ep.get("U_ent_drop_10_up", 0.0),
                U_ent_drop_50_down=ep.get("U_ent_drop_50_down", 0.0),
                U_ent_drop_10_down=ep.get("U_ent_drop_10_down", 0.0),
                signal_strength=ep.get("signal_strength", 0.0),
                s_effective=ep.get("s_effective", 0.0),
                bar_count=ep.get("bar_count", 0),
                confidence_at_entry=ep.get("trend_confidence", 0.0),
                exit_reason=tt.get("exit_reason", ""),
                dec_P_up=ep.get("dec_P_up", 0.0), dec_P_down=ep.get("dec_P_down", 0.0),
                dec_P_zero=ep.get("dec_P_zero", 0.0),
                dec_P_up_minus_down=ep.get("dec_P_up_minus_down", 0.0),
                dec_P_up_div_down=ep.get("dec_P_up_div_down", 0.0),
                dec_k_up=ep.get("dec_k_up", 0.0), dec_k_down=ep.get("dec_k_down", 0.0),
                dec_k_total=ep.get("dec_k_total", 0.0),
                dec_du_up=ep.get("dec_du_up", 0.0), dec_du_down=ep.get("dec_du_down", 0.0),
                dec_du_up_minus_down=ep.get("dec_du_up_minus_down", 0.0),
                dec_mu_hat_tau=ep.get("dec_mu_hat_tau", 0.0),
                dec_n_star=ep.get("dec_n_star", 0.0), dec_E_star=ep.get("dec_E_star", 0.0),
                dec_direction=ep.get("dec_direction", 0),
            ))
    out_json = os.path.join(os.path.dirname(__file__), f"{args.label}_results.json")
    with open(out_json, "w") as f:
        json.dump(rows, f, indent=2, default=float)
    print(f"wrote {len(rows)} probed trades to {out_json}")

    # Quick quintile analysis on the probe set
    if rows:
        import statistics
        print()
        print(f"--- {len(rows)} probed trades ---")
        # Take winners vs losers
        win = [r for r in rows if r["pnl_sol"] > 0]
        los = [r for r in rows if r["pnl_sol"] < 0]
        print(f"Winners: {len(win)}  total PnL: {sum(r['pnl_sol'] for r in win):+.4f}")
        print(f"Losers:  {len(los)}  total PnL: {sum(r['pnl_sol'] for r in los):+.4f}")

        def stats(group, key):
            vals = [r[key] for r in group if r[key] not in (None, float('inf'), float('-inf'))]
            if not vals: return "n/a"
            return f"med={statistics.median(vals):.3e} min={min(vals):.3e} max={max(vals):.3e}"

        print("\nField comparisons (worst-losers vs best-winners on this probe set):")
        los_worst = sorted(los, key=lambda r: r["pnl_sol"])[:min(30, len(los))]
        win_best = sorted(win, key=lambda r: -r["pnl_sol"])[:min(30, len(win))]
        for key in ["spec_s_eff", "spec_s_eff_ent", "spec_s_eff_drop50",
                    "du_up_norm", "du_down_norm", "rho_up_pos", "rho_down_pos",
                    "rho_up_max", "rho_down_max", "T_t", "rho_at_t",
                    "U_entropy_up", "U_entropy_down", "U_entropy_basin",
                    "grid_span", "sigma_t_V2",
                    "dist_x_to_up", "dist_x_to_down",
                    "rho_drop_50_up", "rho_drop_10_up",
                    "rho_drop_50_down", "rho_drop_10_down",
                    "U_ent_up_norm", "U_ent_down_norm",
                    "U_ent_drop_50_up", "U_ent_drop_10_up",
                    "U_ent_drop_50_down", "U_ent_drop_10_down",
                    "signal_strength", "s_effective",
                    "bar_count", "confidence_at_entry",
                    "dec_P_up", "dec_P_down", "dec_P_zero", "dec_P_up_minus_down", "dec_P_up_div_down",
                    "dec_k_up", "dec_k_down", "dec_k_total",
                    "dec_du_up", "dec_du_down", "dec_du_up_minus_down",
                    "dec_mu_hat_tau", "dec_n_star", "dec_E_star", "dec_direction"]:
            print(f"  {key:>22} | worst-los | {stats(los_worst, key):<40} | best-win | {stats(win_best, key)}")


if __name__ == "__main__":
    main()
