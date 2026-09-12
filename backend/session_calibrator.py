"""session_calibrator.py — Physics-based SDE coefficient calibration (iter84b).

Estimates all 13 free SDE coefficients from the *observed token population*:
the last N completed recordings before the current session start.  This is the
ground truth about what the engine will actually trade — not global macro signals.

Every coefficient has a closed-form relationship to observable candle statistics
(log-return autocorrelation, order-flow diffusion, pool-SOL dynamics), derived
by inverting each SDE's stationary distribution equations.

Public API
----------
    # Sync (backtests — reads DB, no network):
    overrides = calibrate_from_history(recording_started_at_unix)

    # Async (live sessions — runs DB query in executor thread):
    overrides = await calibrate_async()

Both return a dict ready to merge into engine_kwargs.  Falls back to
_ALWAYS_EXPLICIT (no SDE changes, production stack byte-parity) on any error.

Architecture invariants
-----------------------
1. Pure function of price_data.db candle history; no network calls.
2. Backtest-safe: uses only recordings with started_at < before_unix.
3. Fallback on any DB error / insufficient data → _ALWAYS_EXPLICIT only.
4. The 5 delay/gate knobs (_ALWAYS_EXPLICIT) are always included to prevent
   pop-fallback drift (AGENTS.md invariant 4).
5. All estimators are clipped to physically plausible ranges to prevent
   degenerate coefficient vectors from noise in thin populations.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import sqlite3
import time
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── DB path ──────────────────────────────────────────────────────────────────
_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "price_data.db")

# ── Number of recent recordings to use as population sample ──────────────────
N_POPULATION_RECS = 50

# ── Minimum recordings required to output calibrated values ──────────────────
MIN_RECS_REQUIRED = 10

# ── Always-explicit knobs (invariant 4: prevent pop-fallback drift) ───────────
_ALWAYS_EXPLICIT: dict = {
    "v2_exit_delay_seconds":     20.0,
    "v2_exit_delay_armed_only":   1.0,
    "v2_entry_delay_seconds":     0.0,
    "v2_holder_flow_entry_block": 0.0,
    "v2_holder_flow_exit_enable": 0.0,
}

# ── Clipping bounds for each estimated coefficient ────────────────────────────
# (lo, hi) — derived from the SDE physics: values outside these ranges would
# make the filter either degenerate (collapse) or useless (pure noise).
#
# STABILITY RULE: process-noise floors set to ≥ DEFAULT/2.
# A floor below DEFAULT/2 causes UKF overconfidence (P too tight → ill-conditioned
# Cholesky → sigma-point degeneration → catastrophic state collapse).
# This was empirically verified by binary-search isolation on crash recordings.
_CLIP: dict[str, tuple[float, float]] = {
    "sigma_mu":   (0.05, 0.50),   # drift shock — floor 0.05 (phi-saturation stability)
    "lambda_mu":  (0.02, 0.80),   # drift OU rate
    "kappa_mu":   (0.01, 0.20),   # flow→drift coupling
    "sigma_phi":  (0.05, 0.15),   # phi noise — ceiling=DEFAULT (phi→mu→h cascade risk)
    "alpha":      (0.05, 1.00),   # order-flow OU rate
    "beta":       (0.50, 3.00),   # volume→flow coupling
    "eta":        (0.02, 0.50),   # log-var OU rate
    "sigma_h":    (0.10, 0.35),   # vol-of-vol — floor 0.10 (overconfident h crashes)
    "theta":      (0.02, 0.50),   # liquidity OU rate
    "sigma_ell":  (0.05, 0.50),   # liquidity diffusion — floor 0.05 (overconfident ell crashes)
    "zeta":       (0.15, 1.00),   # liq jump magnitude — floor 0.15 (zeta=0.05 alone crashes)
    "lambda_0":   (1/86400, 1/120),
    "tau_max":    (10, 60),
}

# ── SQL: fetch last N completed recordings before a timestamp ─────────────────
_REC_QUERY = """
SELECT id, started_at, stopped_at
FROM recordings
WHERE status='completed'
  AND stopped_at IS NOT NULL
  AND started_at < ?
  AND started_at > 0
ORDER BY started_at DESC
LIMIT ?
"""

# ── SQL: get candles for a set of recording IDs ───────────────────────────────
# Requires only that close > 0 (pool_sol may be 0 for pre-vault-diff recordings;
# the per-recording estimator handles missing pool data gracefully).
_CANDLE_QUERY = """
SELECT recording_id, open, high, low, close, volume, buy_volume, sell_volume, pool_sol
FROM candles
WHERE recording_id IN ({placeholders})
  AND close > 0
  AND open > 0
ORDER BY recording_id, rowid
"""


# ── Per-recording statistics ──────────────────────────────────────────────────

def _safe_lag1_autocorr(arr: np.ndarray) -> float:
    """Lag-1 autocorrelation, returns 0.0 on degenerate input."""
    if len(arr) < 4:
        return 0.0
    x = arr[:-1]
    y = arr[1:]
    mx, my = x.mean(), y.mean()
    cov = float(np.mean((x - mx) * (y - my)))
    vx = float(np.mean((x - mx) ** 2))
    vy = float(np.mean((y - my) ** 2))
    denom = math.sqrt(max(vx * vy, 1e-30))
    rho = cov / denom
    return float(np.clip(rho, -0.9999, 0.9999))


def _compute_rec_stats(candles: np.ndarray, duration_s: float) -> Optional[dict]:
    """Compute statistics for one recording from its candle array.

    Parameters
    ----------
    candles : ndarray shape (N, 9) — [rec_id, open, high, low, close,
              volume, buy_volume, sell_volume, pool_sol]
    duration_s : float — recording duration in seconds

    Returns None if too few candles.
    """
    if len(candles) < 20:
        return None

    closes   = candles[:, 4].astype(float)
    volumes  = candles[:, 5].astype(float)
    buy_vol  = candles[:, 6].astype(float)
    sell_vol = candles[:, 7].astype(float)
    pool_sol = candles[:, 8].astype(float)

    # Log-returns (1-second candles → Δt = 1s)
    lr = np.diff(np.log(np.maximum(closes, 1e-30)))
    if len(lr) < 4:
        return None

    # ── sigma_mu: std of second-difference of log-returns ────────────────────
    # The second difference removes linear drift, isolating the innovation.
    # For OU: Var(Δ²r) ≈ 2·sigma_mu²·dt at short lags.
    if len(lr) >= 3:
        d2r = np.diff(lr)
        sigma_mu_est = float(np.std(d2r)) / math.sqrt(2.0)
    else:
        sigma_mu_est = float(np.std(lr))

    # ── lambda_mu: drift OU rate ──────────────────────────────────────────────
    # At 1-second candle resolution, individual log-returns are near-random
    # (lag-1 autocorr ≈ 0), so the tick-level autocorrelation estimator
    # collapses to the clip ceiling.  Instead, estimate from the coarser
    # 10-candle smoothed return series to capture the economically meaningful
    # drift persistence timescale.
    SMOOTH_W = 10
    if len(closes) >= SMOOTH_W * 2:
        coarse_lr = np.array([
            math.log(max(closes[i + SMOOTH_W], 1e-30) / max(closes[i], 1e-30))
            for i in range(0, len(closes) - SMOOTH_W, SMOOTH_W)
        ])
        rho1_coarse = _safe_lag1_autocorr(coarse_lr)
        abs_rho1 = max(abs(rho1_coarse), 1e-6)
        # ρ_1(coarse) = exp(-lambda_mu * SMOOTH_W) → lambda_mu = -log(ρ)/SMOOTH_W
        lambda_mu_est = max(-math.log(abs_rho1) / SMOOTH_W, 0.0)
    else:
        rho1_price = _safe_lag1_autocorr(lr)
        abs_rho1 = max(abs(rho1_price), 1e-6)
        lambda_mu_est = max(-math.log(abs_rho1), 0.0)

    # ── kappa_mu: order-flow → drift coupling ─────────────────────────────────
    # Estimate as cov(Δr, Δφ) / var(Δφ), the regression coeff.
    # φ = normalized buy-sell imbalance
    tot_vol = np.maximum(volumes, 1e-9)
    phi_series = (buy_vol - sell_vol) / tot_vol
    if len(phi_series) >= 4:
        dphi = np.diff(phi_series)
        dlr  = lr[:len(dphi)]
        dphi_var = float(np.var(dphi))
        if dphi_var > 1e-12:
            kappa_mu_est = float(np.cov(dlr, dphi)[0, 1]) / dphi_var
        else:
            kappa_mu_est = 0.05
    else:
        kappa_mu_est = 0.05

    # ── sigma_phi: order-flow diffusion coefficient ───────────────────────────
    # The observed std of φ = stationary std = sigma_phi / sqrt(2*alpha).
    # Invert: sigma_phi_sde = obs_std * sqrt(2*alpha).
    # We compute alpha_est below; for the first pass use a provisional alpha
    # from coarse autocorrelation.  After alpha_est is finalised we recompute.
    obs_phi_std = float(np.std(phi_series)) if len(phi_series) > 1 else 0.15

    # ── alpha: order-flow OU rate ─────────────────────────────────────────────
    # φ is also near-random at tick level; use same coarse-smoothing approach.
    if len(phi_series) >= SMOOTH_W * 2:
        coarse_phi = np.array([
            float(np.mean(phi_series[i:i + SMOOTH_W]))
            for i in range(0, len(phi_series) - SMOOTH_W, SMOOTH_W)
        ])
        rho1_phi_c = _safe_lag1_autocorr(coarse_phi)
        abs_rho1_phi = max(abs(rho1_phi_c), 1e-6)
        alpha_est = max(-math.log(abs_rho1_phi) / SMOOTH_W, 0.0)
    else:
        rho1_phi = _safe_lag1_autocorr(phi_series)
        abs_rho1_phi = max(abs(rho1_phi), 1e-6)
        alpha_est = max(-math.log(abs_rho1_phi), 0.0)

    # ── sigma_phi: phi process noise (unmodeled residual in the OU update) ─────
    # In the engine, phi propagates deterministically: phi_{t+1} = phi_t*(1-alpha*dt)
    # + beta*obs_ratio*dt.  sigma_phi is the Q-matrix entry for phi — the
    # filter's uncertainty about how much unmodeled noise phi has each step.
    # Estimator: std of the first-difference Δphi ≈ tick-to-tick phi change,
    # which upper-bounds the residual (includes the OU + observed-flow components).
    # The SDE diffusion coefficient must remain modest (< 0.20) to avoid
    # large Kalman gains in phi that cascade through kappa_mu → mu → h blowup.
    if len(phi_series) >= 4:
        dphi_innov = np.diff(phi_series)
        sigma_phi_est = float(np.std(dphi_innov))
    else:
        sigma_phi_est = 0.15

    # ── beta: volume → flow coupling ─────────────────────────────────────────
    # The engine feeds beta*(buy-sell)/(vol+eps) into the phi observation step.
    # We want typical flow inputs to have magnitude ~ obs_phi_std.
    # imbalance_ratio = (buy-sell)/(vol+eps) is dimensionless ±1.
    # So beta ≈ obs_phi_std / std(imbalance_ratio); clipped to [0.50, 3.00].
    imbalance_ratio = (buy_vol - sell_vol) / np.maximum(volumes, 1e-9)
    std_ratio = float(np.std(imbalance_ratio)) if len(imbalance_ratio) > 1 else 1.0
    # Use observed phi std (not SDE diffusion coeff) for beta scaling
    beta_est = obs_phi_std / max(std_ratio, 1e-6)

    # ── eta: log-variance OU rate ─────────────────────────────────────────────
    # Use coarse smoothing for the same reason as lambda_mu.
    lr_sq = lr ** 2
    lr_sq_safe = np.maximum(lr_sq, 1e-20)
    log_r2 = np.log(lr_sq_safe)
    if len(log_r2) >= SMOOTH_W * 2:
        coarse_log_r2 = np.array([
            float(np.mean(log_r2[i:i + SMOOTH_W]))
            for i in range(0, len(log_r2) - SMOOTH_W, SMOOTH_W)
        ])
        rho1_h_c = _safe_lag1_autocorr(coarse_log_r2)
        abs_rho1_h = max(abs(rho1_h_c), 1e-6)
        eta_est = max(-math.log(abs_rho1_h) / SMOOTH_W, 0.0)
    else:
        rho1_h = _safe_lag1_autocorr(log_r2)
        abs_rho1_h = max(abs(rho1_h), 1e-6)
        eta_est = max(-math.log(abs_rho1_h), 0.0)

    # ── sigma_h: vol-of-vol ───────────────────────────────────────────────────
    # Estimate from EWMA variance series rather than raw log(r²).
    # Raw log(r²) hits -46 on near-zero-return candles, making Δlog(r²)
    # enormous and always pushing sigma_h to the clip ceiling → filter blowup.
    # Instead: compute a 10-tick EWMA of r² (smoothed realised variance),
    # then take the IQR-normalised std of its log-differences.
    if len(lr) >= 20:
        ewma_var = np.zeros(len(lr))
        a_ewma = 2.0 / (SMOOTH_W + 1)   # span-10 EWMA decay factor
        ewma_var[0] = lr[0] ** 2
        for i in range(1, len(lr)):
            ewma_var[i] = a_ewma * lr[i] ** 2 + (1 - a_ewma) * ewma_var[i - 1]
        ewma_var = np.maximum(ewma_var, 1e-20)
        log_ewma = np.log(ewma_var)
        delta_lv = np.diff(log_ewma)
        # IQR-based robust std (÷ 1.349 converts IQR to σ for normal)
        q25, q75 = np.percentile(delta_lv, [25, 75])
        iqr = q75 - q25
        sigma_h_est = float(iqr / 1.349) if iqr > 0 else 0.20
    else:
        sigma_h_est = 0.20

    # ── Liquidity parameters — only from candles with real pool data ──────────
    pool_mask = pool_sol > 0
    pool_valid = pool_sol[pool_mask]
    vol_valid  = volumes[pool_mask]

    if np.sum(pool_mask) >= 20:
        log_pool = np.log(np.maximum(pool_valid, 1e-9))

        # theta: pool-SOL OU rate (coarse-smoothed)
        if len(log_pool) >= SMOOTH_W * 2:
            coarse_pool = np.array([
                float(np.mean(log_pool[i:i + SMOOTH_W]))
                for i in range(0, len(log_pool) - SMOOTH_W, SMOOTH_W)
            ])
            rho1_ell_c = _safe_lag1_autocorr(coarse_pool)
            abs_rho1_ell = max(abs(rho1_ell_c), 1e-6)
            theta_est = max(-math.log(abs_rho1_ell) / SMOOTH_W, 0.0)
        else:
            rho1_ell = _safe_lag1_autocorr(log_pool)
            abs_rho1_ell = max(abs(rho1_ell), 1e-6)
            theta_est = max(-math.log(abs_rho1_ell), 0.0)

        # sigma_ell: std of Δlog(pool_sol), trimmed
        if len(log_pool) >= 3:
            dlog_pool = np.diff(log_pool)
            dlog_pool_clip = dlog_pool[np.abs(dlog_pool) < 3.0]
            sigma_ell_est = float(np.std(dlog_pool_clip)) if len(dlog_pool_clip) > 1 else 0.10
        else:
            sigma_ell_est = 0.10

        # zeta: median |Δlog(pool_sol)| on high-volume spikes
        med_vol = float(np.median(vol_valid)) if len(vol_valid) > 0 else 0.0
        if med_vol > 0 and len(log_pool) > 1:
            spike_mask = vol_valid[:-1] > 3.0 * med_vol  # align to diff
            dlog = np.diff(log_pool)
            spike_vals = dlog[spike_mask[:len(dlog)]]
            zeta_est = float(np.median(np.abs(spike_vals))) if len(spike_vals) > 0 else 0.30
        else:
            zeta_est = 0.30
    else:
        # No pool data for this recording — leave liquidity params as None
        # so they're excluded from population median and fall back to DEFAULT_CONFIG
        theta_est = None
        sigma_ell_est = None
        zeta_est = None

    stats: dict = {
        "sigma_mu":  sigma_mu_est,
        "lambda_mu": lambda_mu_est,
        "kappa_mu":  kappa_mu_est,
        "sigma_phi": sigma_phi_est,
        "alpha":     alpha_est,
        "beta":      beta_est,
        "eta":       eta_est,
        "sigma_h":   sigma_h_est,
        "duration_s": duration_s,
    }
    if theta_est is not None:
        stats["theta"] = theta_est
    if sigma_ell_est is not None:
        stats["sigma_ell"] = sigma_ell_est
    if zeta_est is not None:
        stats["zeta"] = zeta_est
    return stats


# ── Population aggregation (median across recordings) ────────────────────────

def _aggregate_population(rec_stats: list[dict]) -> dict:
    """Aggregate per-recording stats into population-level medians."""
    if not rec_stats:
        return {}

    # Collect all keys that appear across any recording (union)
    all_keys = set()
    for s in rec_stats:
        all_keys.update(s.keys())
    all_keys.discard("duration_s")

    result = {}
    for k in all_keys:
        vals = [
            s[k] for s in rec_stats
            if s.get(k) is not None and math.isfinite(s[k])
        ]
        if vals:
            result[k] = float(np.median(vals))
        # else: key absent → falls back to DEFAULT_CONFIG for that coeff

    durations = [s["duration_s"] for s in rec_stats if s.get("duration_s", 0) > 0]
    if durations:
        result["duration_s"] = float(np.median(durations))

    return result


# ── Formula layer: population stats → coefficient overrides ──────────────────

def _population_to_overrides(pop: dict) -> dict:
    """Apply physics formulas to translate population medians → SDE coefficients.

    Each formula inverts or maps an estimator derived from the token population
    to the corresponding engine coefficient, then clips to the physical range.
    """
    overrides: dict = {}

    def clip(key: str, val: float) -> float:
        lo, hi = _CLIP[key]
        return float(np.clip(val, lo, hi))

    # ── sigma_mu: drift shock std (per √s) ───────────────────────────────────
    if "sigma_mu" in pop:
        overrides["sigma_mu"] = clip("sigma_mu", pop["sigma_mu"])

    # ── lambda_mu: drift OU rate ─────────────────────────────────────────────
    if "lambda_mu" in pop:
        lm = pop["lambda_mu"]
        # Very low autocorr → lambda_mu estimate → ∞; cap at 0.80
        # Very high autocorr → lambda_mu → 0; floor at 0.02
        overrides["lambda_mu"] = clip("lambda_mu", lm)

    # ── kappa_mu: order-flow coupling ────────────────────────────────────────
    if "kappa_mu" in pop:
        overrides["kappa_mu"] = clip("kappa_mu", abs(pop["kappa_mu"]))

    # ── sigma_phi: order-flow diffusion ──────────────────────────────────────
    if "sigma_phi" in pop:
        overrides["sigma_phi"] = clip("sigma_phi", pop["sigma_phi"])

    # ── alpha: order-flow OU rate ─────────────────────────────────────────────
    if "alpha" in pop:
        overrides["alpha"] = clip("alpha", pop["alpha"])

    # ── beta: volume → flow coupling ─────────────────────────────────────────
    if "beta" in pop:
        # beta_est = sigma_phi / median_vol; scale so typical flow = sigma_phi
        overrides["beta"] = clip("beta", pop["beta"])

    # ── eta: log-variance OU rate ─────────────────────────────────────────────
    if "eta" in pop:
        overrides["eta"] = clip("eta", pop["eta"])

    # ── sigma_h: vol-of-vol ───────────────────────────────────────────────────
    if "sigma_h" in pop:
        overrides["sigma_h"] = clip("sigma_h", pop["sigma_h"])

    # ── theta: liquidity OU rate ──────────────────────────────────────────────
    if "theta" in pop:
        overrides["theta"] = clip("theta", pop["theta"])

    # ── sigma_ell: liquidity diffusion ────────────────────────────────────────
    if "sigma_ell" in pop:
        overrides["sigma_ell"] = clip("sigma_ell", pop["sigma_ell"])

    # ── zeta: liquidity jump magnitude ────────────────────────────────────────
    if "zeta" in pop:
        overrides["zeta"] = clip("zeta", pop["zeta"])

    # ── lambda_0: KDE decay rate = 1 / median_recording_duration ─────────────
    if "duration_s" in pop and pop["duration_s"] > 0:
        lo, hi = _CLIP["lambda_0"]
        lambda_0_est = 1.0 / max(pop["duration_s"], 1.0)
        overrides["lambda_0"] = float(np.clip(lambda_0_est, lo, hi))

    # ── tau_max: decision horizon ─────────────────────────────────────────────
    # Tied to the drift mean-reversion timescale (1/lambda_mu), but capped at
    # a fraction of the typical recording duration so the engine doesn't try
    # to project further than the typical token lifetime.
    # tau_max = clamp(1/lambda_mu, 10, min(60, duration/8)), rounded to 5s.
    if "lambda_mu" in overrides and overrides["lambda_mu"] > 0:
        tau_from_lm = 1.0 / overrides["lambda_mu"]
        # Upper-bound by a fraction of session duration (never project > duration/8)
        if "duration_s" in pop and pop["duration_s"] > 0:
            tau_hi = min(60.0, max(10.0, pop["duration_s"] / 8.0))
        else:
            tau_hi = 60.0
        lo, _ = _CLIP["tau_max"]
        tau_raw = float(np.clip(tau_from_lm, lo, tau_hi))
        tau_rounded = int(round(tau_raw / 5.0) * 5)
        tau_rounded = max(min(tau_rounded, int(tau_hi)), int(lo))
        overrides["tau_max"] = tau_rounded

    return overrides


# ── Core calibration function ─────────────────────────────────────────────────

def calibrate_from_population(
    db_path: str,
    before_unix: float,
    n_recs: int = N_POPULATION_RECS,
) -> dict:
    """Estimate SDE coefficients from recent recording population.

    Parameters
    ----------
    db_path : str
        Path to price_data.db.
    before_unix : float
        Only use recordings that started before this timestamp (backtest-safe).
    n_recs : int
        Number of recent recordings to include in the population sample.

    Returns
    -------
    dict
        Engine kwargs overrides merged with _ALWAYS_EXPLICIT.
        Returns _ALWAYS_EXPLICIT only on any error or thin population.
    """
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 1. Fetch last N recordings before the session start
        rows = cur.execute(_REC_QUERY, (before_unix, n_recs)).fetchall()
        if len(rows) < MIN_RECS_REQUIRED:
            logger.info(
                f"[SessionCalibrator] only {len(rows)} recordings before {before_unix:.0f},"
                f" need {MIN_RECS_REQUIRED} — using defaults"
            )
            conn.close()
            return dict(_ALWAYS_EXPLICIT)

        rec_ids   = [r["id"] for r in rows]
        durations = {
            r["id"]: max(float(r["stopped_at"] or 0) - float(r["started_at"]), 0)
            for r in rows
        }

        # 2. Fetch all candles for those recordings in one query
        placeholders = ",".join("?" * len(rec_ids))
        candle_rows = cur.execute(
            _CANDLE_QUERY.format(placeholders=placeholders),
            rec_ids,
        ).fetchall()
        conn.close()

        if not candle_rows:
            logger.info("[SessionCalibrator] no candle data — using defaults")
            return dict(_ALWAYS_EXPLICIT)

        # 3. Group candles by recording_id
        by_rec: dict[int, list] = {}
        for row in candle_rows:
            rid = row[0]
            by_rec.setdefault(rid, []).append(row)

        # 4. Compute per-recording statistics
        rec_stats: list[dict] = []
        for rid, crows in by_rec.items():
            arr = np.array(crows, dtype=float)
            dur = durations.get(rid, 0.0)
            stats = _compute_rec_stats(arr, dur)
            if stats is not None:
                rec_stats.append(stats)

        if len(rec_stats) < MIN_RECS_REQUIRED:
            logger.info(
                f"[SessionCalibrator] only {len(rec_stats)} recs with sufficient candles"
                f" — using defaults"
            )
            return dict(_ALWAYS_EXPLICIT)

        # 5. Aggregate to population medians
        pop = _aggregate_population(rec_stats)

        # 6. Apply physics formulas to get coefficient overrides
        overrides = _population_to_overrides(pop)

        # 7. Merge with the explicit delay/gate knobs
        result = {**overrides, **_ALWAYS_EXPLICIT}

        logger.info(
            f"[SessionCalibrator] calibrated from {len(rec_stats)} recordings "
            f"(before {before_unix:.0f}): "
            + ", ".join(f"{k}={v:.4g}" for k, v in sorted(overrides.items()))
        )
        return result

    except Exception as e:
        logger.warning(f"[SessionCalibrator] calibrate_from_population error: {e}")
        return dict(_ALWAYS_EXPLICIT)


# ── iter86: mint-history calibration (user proposal — full token picture) ───

# Minimum prior same-mint candles required for a mint-history estimate.
MIN_MINT_CANDLES = 120
# Cap on prior same-mint candles fed to the estimator (rolling tail — the
# most recent history is the relevant physics; also bounds query cost).
MAX_MINT_CANDLES = 6000

_MINT_CANDLE_QUERY = """
SELECT c.open, c.high, c.low, c.close, c.volume,
       c.buy_volume, c.sell_volume, c.pool_sol
FROM candles c
JOIN recordings r ON r.id = c.recording_id
WHERE r.mint = ?
  AND r.status = 'completed'
  AND r.started_at > 0
  AND r.started_at < ?
  AND c.close > 0 AND c.open > 0
ORDER BY r.started_at ASC, c.rowid ASC
"""


def calibrate_from_mint_history(
    mint: str,
    started_at_unix: float,
    db_path: str = _DB_PATH,
) -> dict:
    """Estimate SDE coefficients from THIS TOKEN's own prior tape.

    The user's proposal: instead of waiting for the live session's own first
    ~120 candles, use the token's real recorded history — the full picture of
    the coin being traded, available at tick 0 of the session.  In backtest
    this reads every candle from prior completed recordings of the SAME mint
    before the session start (deterministic, no lookahead — the session's own
    candles are never touched).  In live, the same-mint recordings in the DB
    play the same role; a chain-fetch path can extend coverage for mints the
    DB has never seen.

    Returns {} when this mint has < MIN_MINT_CANDLES prior candles — the
    caller falls through to the next calibration layer (population / DEFAULT).
    """
    if not mint:
        return {}
    try:
        conn = sqlite3.connect(db_path, timeout=10.0)
        rows = conn.execute(
            _MINT_CANDLE_QUERY, (mint, float(started_at_unix))
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning(f"[MintCal] query error: {e}")
        return {}

    if len(rows) < MIN_MINT_CANDLES:
        return {}

    # Keep only the rolling tail (most recent physics), then build the
    # (N, 9) candle array the estimator expects (rec-id column unused here).
    rows = rows[-MAX_MINT_CANDLES:]
    arr = np.array(
        [[0.0] + list(r) for r in rows], dtype=float
    )
    duration_s = float(len(rows))  # 1s candles → duration ≈ candle count

    try:
        stats = _compute_rec_stats(arr, duration_s)
    except Exception as e:
        logger.warning(f"[MintCal] estimator error: {e}")
        return {}
    if stats is None:
        return {}

    overrides = _population_to_overrides(stats)
    if not overrides:
        return {}

    logger.info(
        f"[MintCal] {mint[:8]} calibrated from {len(rows)} prior same-mint "
        f"candles: "
        + ", ".join(f"{k}={v:.4g}" for k, v in sorted(overrides.items()))
    )
    return overrides


# ── Public backtest path (sync) ───────────────────────────────────────────────

def calibrate_from_history(started_at_unix: float) -> dict:
    """Return override dict for a recording starting at `started_at_unix`.

    Reads directly from price_data.db — no network calls.

    Parameters
    ----------
    started_at_unix : float
        The recording's `started_at` Unix timestamp.

    Returns
    -------
    dict
        Engine kwargs overrides (only _ALWAYS_EXPLICIT if population too thin).
    """
    return calibrate_from_population(_DB_PATH, float(started_at_unix))


# ── iter85: per-coin online estimation ──────────────────────────────────────

# Minimum candles of THIS coin's own tape before a per-coin estimate is
# trusted.  Below this the tape is too thin for the autocorrelation/
# vol-of-vol estimators (SMOOTH_W=10 coarse windows need ≥~60 points).
MIN_SESSION_CANDLES = 120

# Rec-ids are irrelevant for per-coin estimation (single "recording");
# _compute_rec_stats indexes candles[:, 0] only for grouping.
_PERCOIN_REC_ID = -1


def estimate_from_session_arrays(
    closes: "np.ndarray",
    volumes: "np.ndarray",
    buy_volumes: "np.ndarray",
    sell_volumes: "np.ndarray",
    pool_sols: "np.ndarray",
    duration_s: float,
) -> dict:
    """Estimate SDE coefficients from ONE coin's own candle tape.

    iter85 per-coin precision calibration: the "population" is the coin's
    rolling session window rather than the historical recording population.
    Same physics formulas, same _CLIP stability bounds, deterministic
    (pure numpy, no RNG).

    Returns {} when the tape is too thin or degenerate — callers keep their
    current coefficients (no partial updates).
    """
    n = len(closes)
    if n < MIN_SESSION_CANDLES:
        return {}

    # Degenerate-tape guard: a flat/ossified price series (std of log-returns
    # ~ 0) gives autocorrelation estimators pure noise — every coefficient
    # would clip to a bound.  Keep current coefficients instead.
    lr = np.diff(np.log(np.maximum(np.asarray(closes, dtype=float), 1e-30)))
    if len(lr) < 4 or float(np.std(lr)) < 1e-6:
        return {}

    candles = np.column_stack([
        np.full(n, float(_PERCOIN_REC_ID)),
        closes, closes, closes, closes,          # o/h/l/c (stats use close only)
        volumes, buy_volumes, sell_volumes, pool_sols,
    ])
    try:
        stats = _compute_rec_stats(candles, float(max(duration_s, 1.0)))
    except Exception as e:
        logger.warning(f"[PerCoinCal] estimator error: {e}")
        return {}
    if stats is None:
        return {}

    overrides = _population_to_overrides(stats)
    if not overrides:
        return {}
    return overrides


# ── Public live path (async) ──────────────────────────────────────────────────

_live_cache: dict = {"ts": 0.0, "overrides": None}
_LIVE_CACHE_TTL = 300.0  # 5 minutes


async def calibrate_async(force_refresh: bool = False) -> dict:
    """Return override dict by querying recent recordings from the DB.

    Runs the DB query in a thread-pool executor so it doesn't block the
    asyncio event loop.  Caches the result for 5 minutes.

    Parameters
    ----------
    force_refresh : bool
        If True, bypass the in-process cache.

    Returns
    -------
    dict
        Engine kwargs overrides.
    """
    global _live_cache
    now = time.time()
    if not force_refresh and _live_cache["overrides"] is not None:
        if now - _live_cache["ts"] < _LIVE_CACHE_TTL:
            return dict(_live_cache["overrides"])

    loop = asyncio.get_event_loop()
    overrides = await loop.run_in_executor(
        None,
        calibrate_from_population,
        _DB_PATH,
        now,  # before_unix = now: use all completed recordings
        N_POPULATION_RECS,
    )

    _live_cache["ts"] = now
    _live_cache["overrides"] = overrides
    return dict(overrides)


# ── Diagnostic helper ──────────────────────────────────────────────────────────

def summarize_population(before_unix: Optional[float] = None) -> dict:
    """Return raw population statistics (for diagnostics / smoke test).

    Returns the per-coeff medians before the physics formula layer,
    plus the number of recordings used.
    """
    if before_unix is None:
        before_unix = time.time()
    try:
        conn = sqlite3.connect(_DB_PATH, timeout=10.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = cur.execute(_REC_QUERY, (before_unix, N_POPULATION_RECS)).fetchall()
        if not rows:
            conn.close()
            return {"n_recs": 0}
        rec_ids   = [r["id"] for r in rows]
        durations = {
            r["id"]: max(float(r["stopped_at"] or 0) - float(r["started_at"]), 0)
            for r in rows
        }
        placeholders = ",".join("?" * len(rec_ids))
        candle_rows = cur.execute(
            _CANDLE_QUERY.format(placeholders=placeholders), rec_ids
        ).fetchall()
        conn.close()
        by_rec: dict[int, list] = {}
        for row in candle_rows:
            by_rec.setdefault(row[0], []).append(row)
        rec_stats = []
        for rid, crows in by_rec.items():
            arr = np.array(crows, dtype=float)
            dur = durations.get(rid, 0.0)
            stats = _compute_rec_stats(arr, dur)
            if stats is not None:
                rec_stats.append(stats)
        pop = _aggregate_population(rec_stats)
        overrides = _population_to_overrides(pop)
        return {
            "n_recs": len(rec_stats),
            "population_medians": pop,
            "coefficient_overrides": overrides,
        }
    except Exception as e:
        return {"error": str(e)}
