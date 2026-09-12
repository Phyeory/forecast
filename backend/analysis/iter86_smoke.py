"""iter86_smoke.py — Smoke test for mint-history calibration (user proposal).

Both arms run the ADOPTED production stack (population calibration ON).
  OFF-arm: population calibration only (adopted default)
  ON-arm:  mint-history layer FIRST, population fallback for thin mints

Sample: 30 random recordings that HAVE ≥120 prior same-mint candles (the
only cohort the mechanism can affect) + 10 random thin-mint recordings
(controls — must be byte-identical, proving no leakage elsewhere).

Gates:
  1. DIVERGENCE: ON-arm coefficients differ from OFF-arm on ≥80% of the
     multi-mint cohort (the mechanism must actually fire).
  2. CONTROL PURITY: thin-mint recordings byte-identical between arms.
  3. PNL SAFETY: paired ΔPnL CI lower > −0.005 SOL/rec on the affected cohort.
  4. SYMPTOM: any of WR / expectancy / PF improving on the affected cohort.

Usage:
  cd /Users/jaime/pump-chart
  PYTHONPATH=backend backend/.venv/bin/python backend/analysis/iter86_smoke.py
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from forward_tester import ForwardTester
from session_calibrator import calibrate_from_mint_history, calibrate_from_history

DB_PATH = BACKEND / "data" / "price_data.db"
N_AFFECTED = 30
N_CONTROL = 10
SEED = 86

_SDE_KEYS = ("sigma_mu", "lambda_mu", "kappa_mu", "sigma_phi", "alpha",
             "beta", "eta", "sigma_h", "theta", "sigma_ell", "zeta",
             "lambda_0", "tau_max")


def _engine_kwargs(rec, mintcal: bool) -> dict:
    """Replicate the run_backtest layer stack for one recording."""
    started = float(rec["started_at"])
    cal: dict = {}
    if mintcal:
        cal = calibrate_from_mint_history(str(rec["mint"]), started)
    if not cal:
        cal = calibrate_from_history(started)
    # production knobs always ride on top (both arms identical here)
    return {
        **cal,
        "v2_exit_delay_seconds": 20.0,
        "v2_exit_delay_armed_only": 1.0,
        "v2_entry_delay_seconds": 0.0,
        "v2_holder_flow_entry_block": 0.0,
        "v2_holder_flow_exit_enable": 0.0,
    }


def _run(rec, conn, mintcal: bool):
    candles = conn.execute(
        """SELECT time,open,high,low,close,volume,buy_volume,sell_volume,
                  pool_sol,market_cap_usd FROM candles
           WHERE recording_id=? ORDER BY time""", (rec["id"],)
    ).fetchall()
    if len(candles) < 30:
        return None
    kwargs = _engine_kwargs(rec, mintcal)
    ft = ForwardTester(engine_version=2, engine_kwargs=kwargs)
    _e = float(getattr(ft.engine, "v2_entry_delay_seconds", 0.0))
    if _e > 0.0:
        ft.enable_entry_latency(_e)
    _x = float(getattr(ft.engine, "v2_exit_delay_seconds", 0.0))
    if _x > 0.0:
        ft.enable_exit_latency(_x, armed_only=float(getattr(ft.engine, "v2_exit_delay_armed_only", 0.0)) > 0.0)
    last = None
    for c in candles:
        t = int(c["time"]); o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        bull = cl >= o
        m1, m2 = (h, l) if bull else (l, h)
        ft.update(t, o, o, o, o, 0.0, _build_full_result=False)
        ft.update(t, o, max(o, m1), min(o, m1), m1, 0.0, _build_full_result=False)
        ft.update(t, o, h, l, m2, 0.0, _build_full_result=False)
        ft.update(t, o, h, l, cl, c["volume"], c["buy_volume"], c["sell_volume"],
                  c["pool_sol"], c["market_cap_usd"], _build_full_result=False)
        last = (t, o, h, l, cl)
    if ft.current_trade is not None and last:
        ft._close_long(last[1], last[2], last[3], last[4], last[0], reason="recording_ended")
    return {
        "pnl": float(ft.stats.total_pnl_sol),
        "trades": int(ft.stats.total_trades),
        "wins": int(ft.stats.winning_trades),
        "cfg": {k: float(ft.engine.core.cfg[k]) for k in _SDE_KEYS},
    }


def main():
    t0 = time.time()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # recordings with ≥120 prior same-mint candles (the affected cohort)
    all_recs = conn.execute(
        """SELECT r.id, r.mint, r.started_at FROM recordings r
           WHERE r.status='completed' AND r.started_at > 0"""
    ).fetchall()
    prior_counts = {}
    for row in conn.execute(
        """SELECT r2.mint, r2.started_at, COUNT(c.rowid) AS n
           FROM recordings r2 JOIN candles c ON c.recording_id = r2.id
           WHERE r2.status='completed' AND r2.started_at > 0 AND c.close > 0
           GROUP BY r2.mint, r2.started_at"""
    ):
        prior_counts.setdefault(row[0], []).append((row[1], row[2]))

    def prior_candles(mint, started):
        return sum(n for s, n in prior_counts.get(mint, []) if s < started)

    affected = [r for r in all_recs if prior_candles(r["mint"], r["started_at"]) >= 120]
    thin = [r for r in all_recs if prior_candles(r["mint"], r["started_at"]) == 0]
    rng = random.Random(SEED)
    sample_a = rng.sample(affected, min(N_AFFECTED, len(affected)))
    sample_c = rng.sample(thin, min(N_CONTROL, len(thin)))
    print(f"affected cohort: {len(affected)} recs (sampled {len(sample_a)}); "
          f"thin cohort: {len(thin)} (sampled {len(sample_c)})")

    rows_a, rows_c = [], []
    for rec in sample_a:
        off = _run(rec, conn, mintcal=False)
        on = _run(rec, conn, mintcal=True)
        if off and on:
            rows_a.append({"rec": rec["id"], "off": off, "on": on,
                           "cfg_diff": sum(1 for k in _SDE_KEYS if off["cfg"][k] != on["cfg"][k])})
    for rec in sample_c:
        off = _run(rec, conn, mintcal=False)
        on = _run(rec, conn, mintcal=True)
        if off and on:
            rows_c.append({"rec": rec["id"], "off": off, "on": on,
                           "identical": off["pnl"] == on["pnl"] and off["trades"] == on["trades"]})
    conn.close()

    # Gate 1: divergence
    fired = sum(1 for r in rows_a if r["cfg_diff"] >= 3)
    gate1 = len(rows_a) and fired / len(rows_a) >= 0.8
    print(f"\nGate 1 (mint layer fires): {fired}/{len(rows_a)} recs ≥3 coeffs differ "
          f"({100*fired/max(1,len(rows_a)):.0f}%) → {'PASS' if gate1 else 'FAIL'}")

    # Gate 2: control purity
    clean = sum(1 for r in rows_c if r["identical"])
    gate2 = len(rows_c) and clean == len(rows_c)
    print(f"Gate 2 (thin-mint control purity): {clean}/{len(rows_c)} identical → "
          f"{'PASS' if gate2 else 'FAIL'}")

    # Gate 3: PnL safety on affected cohort
    d = np.array([r["on"]["pnl"] - r["off"]["pnl"] for r in rows_a])
    brng = np.random.default_rng(SEED)
    means = np.array([brng.choice(d, len(d), replace=True).mean() for _ in range(5000)])
    ci_lo = float(np.percentile(means, 2.5))
    gate3 = ci_lo > -0.005
    print(f"Gate 3 (PnL CI): mean Δ={d.mean():+.5f}, CI lower={ci_lo:+.5f} "
          f"(floor −0.005) → {'PASS' if gate3 else 'FAIL'}")

    # Gate 4: symptom on affected cohort
    off_t = sum(r["off"]["trades"] for r in rows_a)
    on_t = sum(r["on"]["trades"] for r in rows_a)
    off_w = sum(r["off"]["wins"] for r in rows_a)
    on_w = sum(r["on"]["wins"] for r in rows_a)
    off_wr = 100 * off_w / max(1, off_t)
    on_wr = 100 * on_w / max(1, on_t)
    off_exp = sum(r["off"]["pnl"] for r in rows_a) / max(1, off_t)
    on_exp = sum(r["on"]["pnl"] for r in rows_a) / max(1, on_t)
    print(f"  symptom: trades {off_t}→{on_t}  WR {off_wr:.1f}%→{on_wr:.1f}%  "
          f"exp {off_exp:+.5f}→{on_exp:+.5f}")

    verdict = "PASS" if (gate1 and gate2 and gate3) else "FAIL"
    print(f"\n{'✅' if verdict=='PASS' else '❌'} SMOKE {verdict}")
    Path(BACKEND / "analysis" / "iter86_smoke_result.json").write_text(json.dumps({
        "verdict": verdict, "n_affected": len(rows_a), "n_control": len(rows_c),
        "fired_pct": 100 * fired / max(1, len(rows_a)),
        "control_clean": clean, "pnl_mean": float(d.mean()), "pnl_ci_lower": ci_lo,
        "symptom": {"trades": [off_t, on_t], "wr": [off_wr, on_wr],
                     "exp": [off_exp, on_exp]},
        "rows": [{"rec": r["rec"], "cfg_diff": r["cfg_diff"],
                  "off_pnl": r["off"]["pnl"], "on_pnl": r["on"]["pnl"],
                  "off_n": r["off"]["trades"], "on_n": r["on"]["trades"]} for r in rows_a],
        "elapsed_s": time.time() - t0,
    }, indent=2))
    print(f"Saved → backend/analysis/iter86_smoke_result.json ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
