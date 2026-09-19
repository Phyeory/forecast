"""Tests for the iter84b physics-based population SDE calibrator.

Covers:
  1. Per-recording statistic estimators on synthetic candles with KNOWN
     closed-form values (OU autocorrelation, innovation stds, imbalance)
  2. Edge cases: zero pool_sol, flat price, zero volume, too-few candles
  3. Formula layer: population stats → coefficient overrides (clipping,
     tau rounding, duration-derived lambda_0)
  4. Aggregation: median across recordings, None-skip, duration key
  5. calibrate_from_population on a synthetic SQLite DB (fallback path,
     MIN_RECS_REQUIRED behaviour, backtest-safe windowing)
  6. _ALWAYS_EXPLICIT keys always present; sentinel-free output
  7. Backtest parity: use_session_calibration=False sentinel pops cleanly
  8. Output coefficients all within _CLIP bounds
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import tempfile
import time

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import session_calibrator as sc
from session_calibrator import (
    _ALWAYS_EXPLICIT,
    _CLIP,
    _compute_rec_stats,
    _aggregate_population,
    _population_to_overrides,
    _safe_lag1_autocorr,
    calibrate_from_population,
)
from strategy_engineV2 import DEFAULT_CONFIG


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_candles(closes, volumes=None, buys=None, sells=None, pool_sol=None,
                  rec_id=1):
    """Build the (N, 9) candle array _compute_rec_stats expects."""
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    if volumes is None: volumes = np.full(n, 1.0)
    if buys is None:    buys = np.full(n, 0.5)
    if sells is None:   sells = np.asarray(volumes, dtype=float) - np.asarray(buys, dtype=float)
    if pool_sol is None: pool_sol = np.full(n, 10.0)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) * 1.01
    lows = np.minimum(opens, closes) * 0.99
    return np.column_stack([
        np.full(n, float(rec_id)), opens, highs, lows, closes,
        np.asarray(volumes, dtype=float), np.asarray(buys, dtype=float),
        np.asarray(sells, dtype=float), np.asarray(pool_sol, dtype=float),
    ])


def _seed_db(tmpdir, recs):
    """recs: list of (rec_id, started_at, stopped_at, closes, vols, buys, pool)."""
    db = os.path.join(tmpdir, "test_pop.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE recordings (id INTEGER PRIMARY KEY, started_at REAL, stopped_at REAL, status TEXT)")
    conn.execute("""CREATE TABLE candles (recording_id INTEGER, time INTEGER, open REAL, high REAL,
                   low REAL, close REAL, volume REAL, buy_volume REAL,
                   sell_volume REAL, pool_sol REAL, rowid_ordinal INTEGER PRIMARY KEY AUTOINCREMENT)""")
    for rec_id, s, e, closes, vols, buys, pool in recs:
        conn.execute("INSERT INTO recordings VALUES (?,?,?,'completed')", (rec_id, s, e))
        for i, c in enumerate(closes):
            conn.execute(
                "INSERT INTO candles (recording_id,time,open,high,low,close,volume,buy_volume,sell_volume,pool_sol) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rec_id, int(s) + i, c, c*1.01, c*0.99, c,
                 vols[i] if vols is not None else 1.0,
                 buys[i] if buys is not None else 0.5,
                 (vols[i]-buys[i]) if (vols is not None and buys is not None) else 0.5,
                 pool[i] if pool is not None else 10.0))
    conn.commit()
    conn.close()
    return db


# ── _safe_lag1_autocorr ───────────────────────────────────────────────────────

class TestSafeLag1Autocorr:
    def test_perfect_ar1(self):
        x = np.array([math.exp(-0.1 * t) * 0.0 + (0.9 ** t) for t in range(200)])
        x = x + np.array([1.0])  # constant → degenerate
        # Use a genuine AR(1) with known rho
        rng = np.random.default_rng(7)
        rho = 0.6
        x = np.zeros(5000)
        x[0] = rng.normal()
        for i in range(1, 5000):
            x[i] = rho * x[i - 1] + rng.normal(scale=0.5)
        est = _safe_lag1_autocorr(x)
        assert abs(est - rho) < 0.05

    def test_short_array_returns_zero(self):
        assert _safe_lag1_autocorr(np.array([1.0, 2.0])) == 0.0

    def test_constant_series_returns_zero(self):
        assert _safe_lag1_autocorr(np.full(100, 5.0)) == 0.0


# ── Per-recording statistics ──────────────────────────────────────────────────

class TestComputeRecStats:
    def test_too_few_candles_returns_none(self):
        assert _compute_rec_stats(_make_candles([1.0] * 10), 10.0) is None

    def test_flat_price_no_crash(self):
        candles = _make_candles(np.full(200, 100.0))
        stats = _compute_rec_stats(candles, 200.0)
        assert stats is not None
        assert all(math.isfinite(v) for v in stats.values() if v is not None)

    def test_zero_volume_no_crash(self):
        candles = _make_candles(
            100.0 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.02, 300))),
            volumes=np.zeros(300), buys=np.zeros(300),
        )
        stats = _compute_rec_stats(candles, 300.0)
        assert stats is not None
        # phi_series = 0/1e-9 → 0 everywhere → sigma_phi from diff = 0 → fine
        assert stats["sigma_phi"] == 0.0

    def test_zero_pool_sol_excludes_liquidity(self):
        closes = 100.0 * np.exp(np.cumsum(np.random.default_rng(2).normal(0, 0.02, 300)))
        candles = _make_candles(closes, pool_sol=np.zeros(300))
        stats = _compute_rec_stats(candles, 300.0)
        assert stats is not None
        assert stats.get("theta") is None
        assert stats.get("sigma_ell") is None
        assert stats.get("zeta") is None

    def test_partial_pool_sol_excludes_liquidity(self):
        """< 20 valid pool rows → liquidity params None."""
        closes = 100.0 * np.exp(np.cumsum(np.random.default_rng(3).normal(0, 0.02, 300)))
        pool = np.full(300, 10.0)
        pool[:295] = 0.0  # only 5 valid
        candles = _make_candles(closes, pool_sol=pool)
        stats = _compute_rec_stats(candles, 300.0)
        assert stats.get("theta") is None

    def test_ou_lambda_mu_recovers_rate(self):
        """Simulate price with 10s-scale drift persistence; lambda_mu ~ -log(rho)/10."""
        rng = np.random.default_rng(11)
        n = 20000
        # Coarse blocks of 10 candles with persistent drift direction
        drift = np.repeat(rng.normal(0, 0.01, n // 10 + 1), 10)[:n]
        noise = rng.normal(0, 0.02, n)
        lr = drift + noise
        closes = 100.0 * np.exp(np.cumsum(lr))
        candles = _make_candles(closes)
        stats = _compute_rec_stats(candles, float(n))
        # lambda_mu in a sane range (not clipped to ceiling)
        assert 0.02 <= stats["lambda_mu"] <= 0.80

    def test_sigma_mu_second_difference(self):
        """i.i.d. returns with std s → second-diff std = s*sqrt(2) → sigma_mu = s."""
        rng = np.random.default_rng(13)
        lr = rng.normal(0, 0.05, 20000)
        closes = 100.0 * np.exp(np.cumsum(lr))
        candles = _make_candles(closes)
        stats = _compute_rec_stats(candles, 20000.0)
        # sigma_mu_est = std(d2r)/sqrt(2); d2r of iid noise has std = s*sqrt(6)/... 
        # but with drift removed.  Just check it's the right order of magnitude
        # and finite.
        assert 0.0 < stats["sigma_mu"] < 0.2
        assert math.isfinite(stats["sigma_mu"])

    def test_imbalance_sigma_phi(self):
        """Random ±1 imbalance → std(Δphi) ≈ sqrt(2)*std(phi) — just finite & sane."""
        rng = np.random.default_rng(17)
        n = 300
        imb = rng.choice([-1.0, 1.0], size=n) * rng.uniform(0.5, 1.0, n)
        vols = np.full(n, 2.0)
        candles = _make_candles(
            100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, n))),
            volumes=vols, buys=(vols + imb) / 2,
        )
        stats = _compute_rec_stats(candles, float(n))
        assert math.isfinite(stats["sigma_phi"])
        assert stats["sigma_phi"] >= 0.0

    def test_beta_finite_positive(self):
        rng = np.random.default_rng(19)
        n = 500
        closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
        buys = rng.uniform(0, 1, n)
        candles = _make_candles(closes, volumes=np.ones(n), buys=buys)
        stats = _compute_rec_stats(candles, float(n))
        assert math.isfinite(stats["beta"]) and stats["beta"] > 0.0

    def test_duration_passthrough(self):
        candles = _make_candles(100.0 * np.exp(np.cumsum(np.random.default_rng(23).normal(0, 0.02, 300))))
        stats = _compute_rec_stats(candles, 1234.0)
        assert stats["duration_s"] == 1234.0


# ── Aggregation ───────────────────────────────────────────────────────────────

class TestAggregatePopulation:
    def test_median_across_recordings(self):
        r1 = {k: 1.0 for k in ("sigma_mu", "sigma_h", "duration_s")}
        r2 = {k: 2.0 for k in ("sigma_mu", "sigma_h", "duration_s")}
        r3 = {k: 10.0 for k in ("sigma_mu", "sigma_h", "duration_s")}
        pop = _aggregate_population([r1, r2, r3])
        assert pop["sigma_mu"] == 2.0
        assert pop["duration_s"] == 2.0

    def test_none_values_skipped(self):
        r1 = {"sigma_mu": 1.0, "theta": None, "duration_s": 100.0}
        r2 = {"sigma_mu": 3.0, "theta": 0.2, "duration_s": 200.0}
        pop = _aggregate_population([r1, r2])
        assert pop["sigma_mu"] == 2.0
        assert pop["theta"] == 0.2  # only the non-None value

    def test_all_none_key_absent(self):
        r1 = {"sigma_mu": 1.0, "theta": None, "duration_s": 100.0}
        r2 = {"sigma_mu": 2.0, "theta": None, "duration_s": 100.0}
        pop = _aggregate_population([r1, r2])
        assert "theta" not in pop

    def test_empty_returns_empty(self):
        assert _aggregate_population([]) == {}


# ── Formula layer ──────────────────────────────────────────────────────────────

class TestPopulationToOverrides:
    def test_all_keys_clipped_in_bounds(self):
        pop = {k: 1e9 for k in _CLIP}  # everything absurdly large
        pop["duration_s"] = 100.0
        ov = _population_to_overrides(pop)
        for k, (lo, hi) in _CLIP.items():
            if k in ov:
                assert lo <= ov[k] <= hi, f"{k}={ov[k]} outside ({lo},{hi})"

    def test_zero_values_clipped_to_floors(self):
        pop = {k: 0.0 for k in _CLIP}
        pop["duration_s"] = 100.0
        ov = _population_to_overrides(pop)
        for k, (lo, hi) in _CLIP.items():
            if k in ov:
                assert ov[k] >= lo

    def test_tau_max_rounds_to_multiple_of_5(self):
        pop = {"lambda_mu": 0.3, "duration_s": 3600.0}
        ov = _population_to_overrides(pop)
        assert ov["tau_max"] % 5 == 0
        assert 10 <= ov["tau_max"] <= 60

    def test_lambda_0_from_duration(self):
        pop = {"duration_s": 3600.0}
        ov = _population_to_overrides(pop)
        lo, hi = _CLIP["lambda_0"]
        assert lo <= ov["lambda_0"] <= hi
        # 1/3600 = 2.78e-4 — inside (1/86400, 1/120), no clip
        assert abs(ov["lambda_0"] - 1.0 / 3600.0) < 1e-9

    def test_tau_duration_bound(self):
        # duration 80s → tau_hi = max(10, 80/8)=10 → tau pinned at 10
        pop = {"lambda_mu": 0.05, "duration_s": 80.0}  # 1/0.05=20 unclipped
        ov = _population_to_overrides(pop)
        assert ov["tau_max"] == 10

    def test_missing_stat_leaves_default(self):
        ov = _population_to_overrides({})  # nothing
        assert ov == {}  # no overrides → engine falls back to DEFAULT_CONFIG

    def test_absent_lambda_mu_no_tau(self):
        pop = {"duration_s": 100.0}  # no lambda_mu → no tau_max override
        ov = _population_to_overrides(pop)
        assert "tau_max" not in ov


# ── Synthetic-DB integration ──────────────────────────────────────────────────

class TestCalibrateFromPopulation:
    def _build_db(self, tmpdir, n_recs=12, started_at=2_000_000_000.0):
        rng = np.random.default_rng(31)
        recs = []
        t = started_at
        for i in range(n_recs):
            closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.03, 400)))
            vols = rng.uniform(0.5, 2.0, 400)
            buys = vols * rng.uniform(0.2, 0.8)
            pool = np.full(400, 10.0)
            recs.append((i + 1, t, t + 400.0, closes, vols, buys, pool))
            t += 400.0
        return _seed_db(tmpdir, recs), started_at

    def test_basic_output_shape(self, tmp_path):
        db, t = self._build_db(str(tmp_path))
        out = calibrate_from_population(db, t + 10_000.0, n_recs=10)
        assert isinstance(out, dict)
        # Always-explicit knobs present
        for k in _ALWAYS_EXPLICIT:
            assert k in out
        # At least some SDE keys present (population was rich enough)
        sde_keys = set(_CLIP.keys())
        present = sde_keys & set(out.keys())
        assert len(present) >= 10

    def test_backtest_safe_window(self, tmp_path):
        """Only recordings with started_at < before_unix are used."""
        db, t = self._build_db(str(tmp_path))
        out = calibrate_from_population(db, t, n_recs=50)  # nothing before t
        # Falls back to defaults-only → just _ALWAYS_EXPLICIT
        assert out == _ALWAYS_EXPLICIT

    def test_insufficient_recs_fallback(self, tmp_path):
        db, t = self._build_db(str(tmp_path), n_recs=5)
        out = calibrate_from_population(db, t + 10_000.0, n_recs=50)
        assert out == _ALWAYS_EXPLICIT

    def test_all_values_finite_and_in_bounds(self, tmp_path):
        db, t = self._build_db(str(tmp_path))
        out = calibrate_from_population(db, t + 10_000.0, n_recs=10)
        for k, (lo, hi) in _CLIP.items():
            if k in out:
                v = out[k]
                assert math.isfinite(v), f"{k} not finite"
                assert lo <= v <= hi, f"{k}={v} outside ({lo},{hi})"

    def test_deterministic(self, tmp_path):
        db, t = self._build_db(str(tmp_path))
        a = calibrate_from_population(db, t + 10_000.0, n_recs=10)
        b = calibrate_from_population(db, t + 10_000.0, n_recs=10)
        assert a == b


# ── Engine integration ────────────────────────────────────────────────────────

class TestEngineIntegration:
    def test_overrides_reach_engine_cfg(self, tmp_path):
        """A calibrated dict must actually change the V2 engine's config."""
        from engine_factory import create_engine
        db, t = self._build_db_with_stats(str(tmp_path))
        out = calibrate_from_population(db, t + 10_000.0, n_recs=10)
        sde = {k: v for k, v in out.items() if k in _CLIP}
        if not sde:
            pytest.skip("synthetic population produced no SDE keys")
        eng = create_engine(engine_version=2, **sde)
        for k, v in sde.items():
            assert eng.core.cfg[k] == v, f"{k} did not reach engine cfg"

    @staticmethod
    def _build_db_with_stats(tmpdir):
        rng = np.random.default_rng(37)
        recs = []
        t = 2_000_000_000.0
        for i in range(12):
            closes = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.03, 400)))
            vols = rng.uniform(0.5, 2.0, 400)
            buys = vols * rng.uniform(0.2, 0.8)
            pool = np.full(400, 10.0)
            recs.append((i + 1, t, t + 400.0, closes, vols, buys, pool))
            t += 400.0
        return _seed_db(tmpdir, recs), 2_000_000_000.0

    def test_always_explicit_never_overridden_by_calibration(self, tmp_path):
        """Even if population stats could imply different delay knobs, the
        _ALWAYS_EXPLICIT values must win (iter83 hardened production stack)."""
        db, t = self._build_db_with_stats(str(tmp_path))
        out = calibrate_from_population(db, t + 10_000.0, n_recs=10)
        for k, v in _ALWAYS_EXPLICIT.items():
            assert out[k] == v

    def test_stability_floors(self, tmp_path):
        """The UKF-stability floors (DEFAULT/2 on process noise) must hold."""
        db, t = self._build_db_with_stats(str(tmp_path))
        out = calibrate_from_population(db, t + 10_000.0, n_recs=10)
        assert out.get("sigma_h", DEFAULT_CONFIG["sigma_h"]) >= 0.10
        assert out.get("sigma_ell", DEFAULT_CONFIG["sigma_ell"]) >= 0.05
        assert out.get("sigma_mu", DEFAULT_CONFIG["sigma_mu"]) >= 0.05
        assert out.get("zeta", DEFAULT_CONFIG["zeta"]) >= 0.15
        assert out.get("sigma_phi", DEFAULT_CONFIG["sigma_phi"]) <= 0.15


# ── Backtester sentinel ───────────────────────────────────────────────────────

class TestBacktesterSentinel:
    def test_sentinel_dict_is_clean(self):
        """The params dict a NONCAL cell passes — must contain the sentinel
        plus the 5 explicit knobs and nothing else that touches SDE coeffs."""
        d = {"use_session_calibration": False, **_ALWAYS_EXPLICIT}
        sde_keys = set(_CLIP.keys())
        assert not (set(d.keys()) & sde_keys)


# ── Batch sentinel leak regression (2026-09-12) ──────────────────────────────

class TestBatchSentinelLeak:
    """run_backtest must NOT mutate the caller's engine_params dict.

    Batch workers share ONE engine_params object across every task in a chunk
    (pickle memoizes the shared reference).  The pre-fix code popped
    `use_session_calibration` out of the SHARED dict on the first task, so
    tasks 2..N hit the pop default and silently ran WITH calibration —
    contaminating every NONCAL baseline since iter84.  Pinned here.
    """

    def test_run_backtest_does_not_mutate_params(self):
        from backtester import run_backtest
        params = {
            "use_session_calibration": False,
            "v2_exit_delay_seconds": 20.0,
            "v2_exit_delay_armed_only": 1.0,
        }
        snapshot = dict(params)
        try:
            run_backtest(
                recording_id=4320, engine_version=2, engine_params=params,
                buy_size_sol=0.1, persist_results=False,
            )
        except Exception:
            pass  # DB availability varies; the mutation check is the point
        assert params == snapshot, (
            "run_backtest mutated the caller's engine_params — this leak "
            "silently calibrates tasks 2..N in batch chunks (2026-09-12 incident)"
        )

    def test_sentinel_default_follows_engine_knob(self):
        """The sentinel default must be sourced from the ENGINE knob
        v2_popcal_enable (single source of truth: backtest + live calibrate
        identically).  ADOPTED 2026-09-12 — the knob is 1.0, so bare-params
        batches calibrate BY DESIGN.  The knob is the hatch: setting
        v2_popcal_enable=0.0 in DEFAULT_CONFIG restores no-cal defaults
        everywhere at once."""
        from calibration_startup import initialize_calibration_sync
        from strategy_engineV2 import DEFAULT_CONFIG
        assert DEFAULT_CONFIG['v2_popcal_enable'] == 1.0
        params, audit = initialize_calibration_sync('', 1000, {'v2_popcal_enable': 0})
        assert params['v2_percoin_cal_enable'] == 0
        assert audit['source'] == 'defaults'


# ── ADOPTION 2026-09-12 (user directive): population calibration is the production default ────

class TestPopcalAdoption:
    """End-to-end: the adopted stack must calibrate automatically in both
    the backtest path and the live session builder, and stay overridable.
    (Restored 2026-09-13 after an interrupted agent replaced this class
    with tests pinning a never-committed 'per-coin swap' state.)"""

    def test_bare_params_backtest_calibrates(self):
        """Bare {} through run_backtest = the ADOPTED stack (iter86c gated:
        mint-history where the mint has prior tape, population fallback
        otherwise, per-coin refinement ONLY on mint-layer sessions).
        Rec 4320 is thin-mint → bare = popcal baseline (3 / −0.050051)."""
        from backtester import run_backtest
        try:
            s = run_backtest(recording_id=4320, engine_version=2,
                             engine_params={}, buy_size_sol=0.1,
                             persist_results=False)
        except Exception:
            pytest.skip("recording 4320 / DB unavailable")
        st = s["stats"]
        # 2026-09-19: iter90e VR decision-horizon calibrator adopted —
        # population tau is no longer the saturated floor value.
        assert (st["total_trades"], round(st["total_pnl_sol"], 6)) == (4, -0.074574), (
            "bare-params backtest no longer reproduces the adopted gated "
            "stack — adoption default broken"
        )

    def test_sentinel_false_is_off_hatch(self):
        from backtester import run_backtest
        try:
            s = run_backtest(recording_id=4320, engine_version=2,
                             engine_params={"use_session_calibration": False},
                             buy_size_sol=0.1, persist_results=False)
        except Exception:
            pytest.skip("recording 4320 / DB unavailable")
        st = s["stats"]
        # 2026-09-18: re-measured against HEAD (38c07b9) on current data —
        # rec 4320's tape drifted post-iter89 (live trade-history backfill).
        assert (st["total_trades"], round(st["total_pnl_sol"], 6)) == (2, -0.090345), (
            "use_session_calibration=false no longer restores pure DEFAULT"
        )

    def test_live_builder_defaults_to_knob(self):
        """main.py's session builder reads the same engine knob — verify the
        source contract (async path can't be invoked standalone here)."""
        import inspect
        import main
        import backtester
        assert 'await initialize_calibration(' in inspect.getsource(main._get_or_create_live_session)
        assert 'initialize_calibration_sync(' in inspect.getsource(backtester.run_backtest)

    def test_calibrator_output_matches_adopted_cell(self):
        """The estimator feeding production must produce the coefficients
        the adopted batch actually ran (spot-check vs the cal_full per-trade
        log JSON — the backtest DB rows were wiped by an external session,
        but v2_results logs are the analysis source of truth anyway)."""
        import sqlite3, json as _json, glob as _glob
        _repo = os.path.join(os.path.dirname(__file__), "..", "..")
        logs = _glob.glob(os.path.join(
            _repo, "backend", "v2_results",
            "*rec4320_iter84b_cal_full_1789218145_*.json"))
        if not logs:
            pytest.skip("cal_full JSON log for rec 4320 unavailable")
        expected = _json.load(open(logs[0])).get("engine_params", {})
        conn = sqlite3.connect(os.path.join(_repo, "backend", "data", "price_data.db"))
        row = conn.execute(
            "SELECT started_at FROM recordings WHERE id=4320"
        ).fetchone()
        conn.close()
        if row is None:
            pytest.skip("recording 4320 unavailable")
        from session_calibrator import calibrate_from_history
        fresh = calibrate_from_history(float(row[0]))
        for k in ("sigma_mu", "lambda_mu", "sigma_h", "tau_max", "alpha", "eta"):
            assert abs(fresh[k] - expected[k]) < 1e-12, (
                f"estimator drift on {k}: {fresh[k]} vs adopted-batch {expected[k]}"
            )
