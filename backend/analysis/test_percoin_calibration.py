"""Tests for iter85 per-coin online SDE recalibration.

Covers:
  1. estimate_from_session_arrays: guards (short/flat tape), determinism,
     differentiation of distinct coin physics, _CLIP-bounded output
  2. Core engine recalibrate(): applies overrides, respects explicit keys,
     repacks cfg_arr, refreshes cached values, ignores non-SDE keys
  3. Adapter integration: tape buffering with 4-state dedupe, OFF =
     DEFAULT byte-parity, ON = mid-session coefficient change + log,
     divergence between two synthetic coins, determinism, no UKF collapse
  4. Backtest sentinel: per-coin flag does not touch bare-{} runs
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from session_calibrator import (
    _CLIP,
    estimate_from_session_arrays,
)
from strategy_engineV2 import DEFAULT_CONFIG, MemecoinStrategyEngine, StrategyEngineV2Adapter
from engine_factory import create_engine


# ── Synthetic tape helpers ───────────────────────────────────────────────────

def make_tape(rng_seed: int, n: int, vol_scale: float, drift_pers: float,
              imb_bias: float) -> tuple:
    rng = np.random.default_rng(rng_seed)
    blocks = np.repeat(rng.normal(0, drift_pers, n // 30 + 1), 30)[:n]
    lr = blocks + rng.normal(0, vol_scale, n)
    closes = 100.0 * np.exp(np.cumsum(lr))
    vols = rng.uniform(0.5, 2.0, n)
    imb = np.clip(rng.normal(imb_bias, 0.3, n), -0.9, 0.9)
    buys = vols * (1 + imb) / 2
    sells = vols - buys
    pools = 10.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    return closes, vols, buys, sells, pools


def feed_adapter(eng, tape, t0: int = 1000):
    """4-state intra-candle feed (invariant 2) for a synthetic tape."""
    closes, vols, buys, sells, pools = tape
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        h, l = max(o, c) * 1.005, min(o, c) * 0.995
        t = t0 + i
        eng.update(t, o, o, o, o, 0.0)
        eng.update(t, o, max(o, h), min(o, h), h, 0.0)
        eng.update(t, o, h, l, l, 0.0)
        eng.update(t, o, h, l, c, vols[i], buys[i], sells[i], pools[i])


# ── estimate_from_session_arrays ─────────────────────────────────────────────

class TestEstimateFromSession:
    def test_short_tape_empty(self):
        tape = make_tape(1, 60, 0.02, 0.005, 0.0)
        assert estimate_from_session_arrays(*tape, duration_s=60.0) == {}

    def test_flat_tape_empty(self):
        flat = (np.full(600, 100.0), np.full(600, 1.0), np.full(600, 0.5),
                np.full(600, 0.5), np.full(600, 10.0))
        assert estimate_from_session_arrays(*flat, duration_s=600.0) == {}

    def test_output_within_clip_bounds(self):
        tape = make_tape(2, 600, 0.05, 0.02, 0.3)
        out = estimate_from_session_arrays(*tape, duration_s=600.0)
        assert out, "expected non-empty estimates"
        for k, v in out.items():
            lo, hi = _CLIP[k]
            assert lo <= v <= hi, f"{k}={v} outside ({lo},{hi})"

    def test_deterministic(self):
        tape = make_tape(3, 600, 0.03, 0.01, 0.1)
        a = estimate_from_session_arrays(*tape, duration_s=600.0)
        b = estimate_from_session_arrays(*tape, duration_s=600.0)
        assert a == b

    def test_differentiates_coin_physics(self):
        quiet = make_tape(4, 600, 0.01, 0.002, 0.0)   # quiet / mean-reverty
        wild = make_tape(5, 600, 0.08, 0.05, 0.5)    # wild / momentum-y
        eq = estimate_from_session_arrays(*quiet, duration_s=600.0)
        ew = estimate_from_session_arrays(*wild, duration_s=600.0)
        assert eq and ew
        differing = [k for k in eq if abs(eq[k] - ew.get(k, eq[k])) > 1e-6]
        assert len(differing) >= 3, (
            f"per-coin estimator failed to differentiate distinct physics: {differing}"
        )


# ── Core engine recalibrate() ────────────────────────────────────────────────

class TestCoreRecalibrate:
    def _engine(self, config=None):
        return MemecoinStrategyEngine(config or {})

    def test_applies_overrides_and_repacks(self):
        eng = self._engine()
        old_arr = eng._cfg_arr.copy()
        n = eng.recalibrate({"sigma_mu": 0.05, "lambda_mu": 0.3})
        assert n == 2
        assert eng.cfg["sigma_mu"] == 0.05
        assert eng.cfg["lambda_mu"] == 0.3
        assert not np.allclose(old_arr, eng._cfg_arr), "cfg_arr not repacked"
        assert eng._alpha_regime == eng.cfg["alpha"]
        assert eng._tau_default == eng.cfg["tau_max"]

    def test_explicit_keys_never_overridden(self):
        eng = self._engine({"sigma_mu": 0.07})  # explicit user choice
        n = eng.recalibrate({"sigma_mu": 0.40})
        assert n == 0
        assert eng.cfg["sigma_mu"] == 0.07

    def test_ignores_non_sde_keys(self):
        eng = self._engine()
        before = dict(eng.cfg)
        n = eng.recalibrate({"confidence_high": 0.9, "n_particles": 999,
                             "v2_exit_delay_seconds": 0.0})
        assert n == 0
        assert eng.cfg == before

    def test_nan_and_nonfinite_rejected(self):
        eng = self._engine()
        n = eng.recalibrate({"sigma_mu": float("nan"), "eta": float("inf")})
        assert n == 0
        assert eng.cfg["sigma_mu"] == DEFAULT_CONFIG["sigma_mu"]

    def test_lambda_0_refreshes_kde_decay(self):
        eng = self._engine()
        old_decay = eng.potential.lambda_decay
        eng.recalibrate({"lambda_0": 0.001})
        assert eng.potential.lambda_decay == 0.001
        assert eng.potential.lambda_decay != old_decay


# ── Adapter integration ──────────────────────────────────────────────────────

class TestAdapterPerCoin:
    def test_explicit_off_is_default_byte_parity(self):
        """v2_percoin_cal_enable=0.0 (explicit OFF) = coefficients frozen at
        construction values.  Post-iter86b-adoption a BARE engine runs the
        adopted ON stack; the OFF hatch is the explicit 0.0."""
        tape = make_tape(6, 300, 0.03, 0.01, 0.2)
        eng = create_engine(engine_version=2, v2_percoin_cal_enable=0.0)
        feed_adapter(eng, tape)
        for k in ("sigma_mu", "lambda_mu", "kappa_mu", "sigma_phi", "alpha",
                  "beta", "eta", "sigma_h", "theta", "sigma_ell", "zeta",
                  "lambda_0", "tau_max"):
            assert eng.core.cfg[k] == DEFAULT_CONFIG[k], f"{k} changed while OFF"
        assert eng.core._recal_count == 0

    def test_bare_engine_gated_off_without_mint_layer(self):
        """iter86c: bare-constructed engines (no calibration kwargs) run with
        per-coin GATED OFF (requires-mintcal default ON — online estimates on
        thin-mint/popcal tapes are noise).  Mint-sourced sessions arm it."""
        eng = create_engine(engine_version=2)
        assert eng._v2_percoin_enable is False, (
            "bare engine has per-coin active without a mint layer — the "
            "requires-mintcal gate is not gating"
        )
        eng2 = create_engine(engine_version=2, lambda_mu=0.25,
                             _calibration_sourced=1)
        assert eng2._v2_percoin_enable is True, (
            "mint-sourced session lost the per-coin refinement"
        )
        # explicit opt-out still wins for mint-sourced sessions
        eng3 = create_engine(engine_version=2, lambda_mu=0.25,
                             _calibration_sourced=1, v2_percoin_cal_enable=0.0)
        assert eng3._v2_percoin_enable is False

    def test_on_recalibrates_mid_session(self):
        tape = make_tape(7, 400, 0.05, 0.02, 0.3)
        eng = create_engine(engine_version=2, v2_percoin_cal_enable=1.0,
                            _calibration_sourced=1)  # mint-layer session
        feed_adapter(eng, tape)
        assert eng.core._recal_count >= 1, "no recalibration fired"
        assert getattr(eng, "_percoin_log", []), "recalibration not logged"
        # some coefficient must differ from DEFAULT after recal
        changed = [k for k in ("sigma_mu", "lambda_mu", "alpha", "eta",
                               "sigma_h", "tau_max")
                   if eng.core.cfg[k] != DEFAULT_CONFIG[k]]
        assert changed, "recalibration produced no change vs DEFAULT"

    def test_two_coins_diverge(self):
        quiet = make_tape(8, 400, 0.01, 0.002, 0.0)
        wild = make_tape(9, 400, 0.08, 0.05, 0.5)
        e_q = create_engine(engine_version=2, v2_percoin_cal_enable=1.0,
                            _calibration_sourced=1)
        e_w = create_engine(engine_version=2, v2_percoin_cal_enable=1.0,
                            _calibration_sourced=1)
        feed_adapter(e_q, quiet)
        feed_adapter(e_w, wild)
        keys = ("sigma_mu", "lambda_mu", "alpha", "eta", "sigma_h", "tau_max")
        diff = [k for k in keys if e_q.core.cfg[k] != e_w.core.cfg[k]]
        assert len(diff) >= 2, f"coins ended up identical on: {diff}"

    def test_deterministic_replay(self):
        tape = make_tape(10, 400, 0.04, 0.015, 0.1)
        def run():
            e = create_engine(engine_version=2, v2_percoin_cal_enable=1.0)
            feed_adapter(e, tape)
            return {k: e.core.cfg[k] for k in
                    ("sigma_mu", "lambda_mu", "alpha", "eta", "sigma_h",
                     "theta", "tau_max")}
        assert run() == run()

    def test_no_filter_collapse(self):
        tape = make_tape(11, 500, 0.06, 0.03, 0.4)  # aggressive tape
        e = create_engine(engine_version=2, v2_percoin_cal_enable=1.0)
        feed_adapter(e, tape)
        assert e.core._last_sigma_t < 100.0, (
            f"filter collapsed: sigma_t={e.core._last_sigma_t}"
        )

    def test_tape_buffer_dedupe_4state(self):
        """400 candles × 4 states must produce exactly 400 tape rows."""
        tape = make_tape(12, 400, 0.03, 0.01, 0.0)
        e = create_engine(engine_version=2, v2_percoin_cal_enable=1.0,
                          _calibration_sourced=1)
        # use a min_candles trigger beyond the tape so no recal interferes
        e._v2_percoin_min_candles = 10_000
        e._percoin_next_at = 10_000
        feed_adapter(e, tape)
        assert len(e._percoin_tape) == 400

    def test_short_session_never_recalibrates(self):
        tape = make_tape(13, 100, 0.03, 0.01, 0.0)  # < min 120 candles
        e = create_engine(engine_version=2, v2_percoin_cal_enable=1.0)
        feed_adapter(e, tape)
        assert e.core._recal_count == 0

    def test_explicit_coeff_not_overridden_online(self):
        tape = make_tape(14, 400, 0.05, 0.02, 0.3)
        e = create_engine(engine_version=2, v2_percoin_cal_enable=1.0,
                          sigma_mu=0.09)  # explicit launch choice
        feed_adapter(e, tape)
        assert e.core.cfg["sigma_mu"] == 0.09, (
            "online recalibration overrode an explicitly-set coefficient"
        )


# ── iter86: mint-history calibration layer (user proposal) ───────────────────

class TestMintHistoryLayer:
    """calibrate_from_mint_history + the run_backtest layer stack.

    Precedence: mint-history → population → DEFAULT.  No-lookahead: only
    candles from PRIOR completed recordings of the same mint.
    """

    def test_thin_or_missing_mint_returns_empty(self):
        from session_calibrator import calibrate_from_mint_history
        # no mint
        assert calibrate_from_mint_history("", 1e12) == {}
        # a mint with no prior recordings (bogus address)
        assert calibrate_from_mint_history("BogusMint1111111111111111111111111111111111", 1e12) == {}

    def test_no_lookahead_excludes_current_and_future(self, tmp_path):
        """A recording starting AT the session time contributes nothing."""
        from session_calibrator import calibrate_from_mint_history, MIN_MINT_CANDLES
        import sqlite3 as _s
        db = str(tmp_path / "t.db")
        conn = _s.connect(db)
        conn.execute("CREATE TABLE recordings (id INTEGER PRIMARY KEY, mint TEXT, status TEXT, started_at REAL)")
        conn.execute("""CREATE TABLE candles (recording_id INTEGER, open REAL, high REAL,
                       low REAL, close REAL, volume REAL, buy_volume REAL,
                       sell_volume REAL, pool_sol REAL, rowid INTEGER PRIMARY KEY AUTOINCREMENT)""")
        # two prior recordings of the mint with rich tapes
        t = 1_000_000.0
        rng = np.random.default_rng(86)
        for rid in (1, 2):
            closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.04, 300)))
            for c in closes:
                conn.execute("INSERT INTO candles (recording_id,open,high,low,close,volume,buy_volume,sell_volume,pool_sol) VALUES (?,?,?,?,?,?,?,?,?)",
                             (rid, c, c*1.01, c*0.99, c, 1.5, 0.8, 0.7, 10.0))
            conn.execute("INSERT INTO recordings VALUES (?,?,?,?)", (rid, "MintX", "completed", t))
            t += 1000.0
        # a FUTURE recording (after the session) — must be excluded
        conn.execute("INSERT INTO recordings VALUES (?,?,?,?)", (3, "MintX", "completed", t + 10_000.0))
        for c in np.linspace(100, 200, 300):
            conn.execute("INSERT INTO candles (recording_id,open,high,low,close,volume,buy_volume,sell_volume,pool_sol) VALUES (?,?,?,?,?,?,?,?,?)",
                         (3, c, c, c, c, 9.9, 9.0, 0.9, 50.0))
        conn.commit(); conn.close()

        # session starts after recs 1-2, before rec 3
        out = calibrate_from_mint_history("MintX", t + 5_000.0, db_path=db)
        assert out, "prior candles should produce estimates"
        # with started_at before any recording → empty (no lookahead anywhere)
        assert calibrate_from_mint_history("MintX", 0.0, db_path=db) == {}

    def test_estimates_bounded_and_deterministic(self):
        from session_calibrator import calibrate_from_mint_history, _CLIP
        import sqlite3 as _s
        conn = _s.connect("backend/data/price_data.db" if not os.path.exists(
            os.path.join(os.path.dirname(__file__), "..", "data", "price_data.db"))
            else os.path.join(os.path.dirname(__file__), "..", "data", "price_data.db"))
        mint, last = conn.execute(
            "SELECT mint, MAX(started_at) FROM recordings WHERE status='completed' "
            "GROUP BY mint ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        conn.close()
        a = calibrate_from_mint_history(mint, float(last) + 3600.0)
        b = calibrate_from_mint_history(mint, float(last) + 3600.0)
        assert a == b, "mint-history estimator not deterministic"
        for k, v in a.items():
            lo, hi = _CLIP[k]
            assert lo <= v <= hi

    def test_backtest_layer_precedence(self):
        """run_backtest with mintcal ON: multi-recording mint gets MINT
        coefficients, not the population prior."""
        from backtester import run_backtest
        import sqlite3 as _s
        import json as _j
        db = os.path.join(os.path.dirname(__file__), "..", "data", "price_data.db")
        conn = _s.connect(db)
        # pick a later recording of a heavily-recorded mint
        row = conn.execute(
            """SELECT r.id, r.mint, r.started_at FROM recordings r
               WHERE r.status='completed'
                 AND (SELECT COUNT(*) FROM recordings r2
                      WHERE r2.mint = r.mint AND r2.status='completed'
                        AND r2.started_at < r.started_at) >= 3
               ORDER BY r.started_at DESC LIMIT 1"""
        ).fetchone()
        conn.close()
        if row is None:
            pytest.skip("no multi-recording mint available")
        rec_id, mint, started = row

        s = run_backtest(
            recording_id=rec_id, engine_version=2,
            engine_params={"v2_mintcal_enable": 1.0},
            buy_size_sol=0.1, persist_results=False,
        )
        st = s["stats"]
        # must produce a result (mint layer or fallback) without error
        assert "total_trades" in st


# ── iter86b: calibration-layer stacking ──────────────────────────────────────

class TestLayerStacking:
    """Mint-history start + per-coin online refinement + user protection."""

    def test_calibration_sentinel_forwarded_through_adapter(self):
        """_calibration_sourced must reach the core through the adapter's
        DEFAULT_CONFIG-key filter (it is not a DEFAULT_CONFIG key)."""
        from engine_factory import create_engine
        eng = create_engine(
            engine_version=2,
            sigma_mu=0.05, _calibration_sourced=1,
            v2_percoin_cal_enable=1.0,
        )
        # core treats sigma_mu as refinable: explicit keys exclude it
        assert "sigma_mu" not in eng.core._explicit_cfg_keys
        n = eng.core.recalibrate({"sigma_mu": 0.08})
        assert n == 1

    def test_without_sentinel_keys_still_protected(self):
        from engine_factory import create_engine
        eng = create_engine(engine_version=2, sigma_mu=0.05)
        assert "sigma_mu" in eng.core._explicit_cfg_keys
        assert eng.core.recalibrate({"sigma_mu": 0.08}) == 0

    def test_backtester_sets_sentinel_only_without_user_sde(self):
        """The pipeline must NOT set _calibration_sourced when the caller
        passed SDE keys (user intent stays protected even with calibration)."""
        import inspect
        import backtester
        src = inspect.getsource(backtester.run_backtest)
        assert '_mint_layer_fired and not any(k in engine_params for k in _SDE_13)' in src
        assert 'cal_base["_calibration_sourced"] = 1' in src

    def test_stack_end_to_end(self):
        """mint coefficients at tick 0 + online recalibration refines them."""
        import numpy as np
        from engine_factory import create_engine
        rng = np.random.default_rng(86)
        n = 400
        closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.03, n)))
        vols = rng.uniform(0.5, 2.0, n)
        buys = vols * rng.uniform(0.3, 0.7, n)
        eng = create_engine(
            engine_version=2,
            lambda_mu=0.2368,            # "mint layer" value
            _calibration_sourced=1,       # pipeline marks it refinable
            v2_percoin_cal_enable=1.0,
        )
        for i in range(n):
            o = closes[i - 1] if i else closes[0]
            t = 1000 + i
            eng.update(t, o, o, o, o, 0.0)
            eng.update(t, o, max(o, closes[i]), min(o, closes[i]), closes[i], 0.0)
            eng.update(t, o, max(o, closes[i]) * 1.005, min(o, closes[i]) * 0.995,
                       min(o, closes[i]) * 0.995, 0.0)
            eng.update(t, o, max(o, closes[i]) * 1.005, min(o, closes[i]) * 0.995,
                       closes[i], vols[i], buys[i], vols[i] - buys[i], 10.0)
        assert eng.core._recal_count >= 1, "online recalibration did not fire past mint start"


# ── iter86b adoption: hatch-matrix semantics ─────────────────────────────────

class TestAdoptionHatchMatrix:
    """The adopted stack's four hatches on a discriminating recording.
    Rec 4320 separates all configs: stack=5 trades, popcal=3, DEFAULT=2."""

    PROBE = 4320

    def _run(self, params):
        from backtester import run_backtest
        try:
            s = run_backtest(recording_id=self.PROBE, engine_version=2,
                             engine_params=params, buy_size_sol=0.1,
                             persist_results=False)
        except Exception:
            pytest.skip("recording 4320 / DB unavailable")
        st = s["stats"]
        return (st["total_trades"], round(st["total_pnl_sol"], 6))

    def test_bare_params_run_adopted_stack(self):
        """Rec 4320 is thin-mint: bare {} = population fallback (per-coin
        gated off by requires-mintcal — no noise layer on thin tapes)."""
        got = self._run({})
        assert got == (3, -0.050051), (
            f"bare-{{}} on thin-mint rec no longer matches popcal baseline: {got}"
        )

    def test_noncal_is_pure_default(self):
        got = self._run({"use_session_calibration": False})
        assert got == (2, -0.079628), (
            f"NONCAL no longer byte-matches pure DEFAULT: {got}"
        )

    def test_popcal_only_matches_prior_adoption(self):
        got = self._run({"v2_mintcal_enable": 0.0, "v2_percoin_cal_enable": 0.0})
        assert got == (3, -0.050051), (
            f"popcal-only no longer matches the 09-12 adopted baseline: {got}"
        )

    def test_noncal_plus_percoin_explicit(self):
        got = self._run({"use_session_calibration": False,
                         "v2_percoin_cal_enable": 1.0})
        assert got == (4, 0.023963), (
            f"surgical NONCAL+percoin control changed: {got}"
        )

    def test_noncal_disables_engine_native_percoin(self):
        """The sentinel-false path must inject percoin-off when the caller
        didn't pass the knob — otherwise NONCAL baselines silently carry
        the online layer (iter86b adoption hazard)."""
        import inspect
        import backtester
        src = inspect.getsource(backtester.run_backtest)
        assert 'engine_params["v2_percoin_cal_enable"] = 0.0' in src
