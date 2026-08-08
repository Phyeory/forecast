"""
Strategy Engine V2 — Non-equilibrium Statistical Mechanics for memecoin trading.

This is the mathematical core described in `strategyV2.md`.  It replaces the
Langevin / Kalman regime detector (V1) with a fully Bayesian state estimator:

  * Rao-Blackwellised Particle Filter (RBPF) — discrete regime label sampled
    per particle, continuous state propagated by a per-particle Unscented
    Kalman Filter (UKF).
  * Volume-weighted Kernel Density Estimate of the price distribution, used
    to build the non-equilibrium market potential U(x,t).
  * Liquidity cost field V_liq(x,t) integrated from L2-style depth.
  * Topological regime derivation driven ONLY by derived noise floors,
    not hardcoded thresholds.
  * Modified Kramers escape rates for upward / downward barrier crossings.
  * Kelly-optimal sizing with slippage, fees, and latency-drift costs.

The math-critical inner loops (UKF propagation, KDE evaluation, barrier /
minima grid search) are JIT-compiled with `numba` to meet the sub-2ms
latency requirement.

────────────────────────────────────────────────────────────────────────────────
V1-COMPATIBLE ADAPTER
────────────────────────────────────────────────────────────────────────────────
The production pipelines (ForwardTester, Backtester, LiveTrader, main.py,
frontend) speak the V1 `StrategyEngine.update(time, o, h, l, c, ...)` /
`{regime, direction, signal, ...}` contract.  Re-engineering every call
site would be invasive and error-prone.

Instead, `StrategyEngineV2Adapter` (defined at the bottom of this file)
exposes the EXACT V1 surface, internally mapping each OHLCV `update()`
call to a 1-second `obs` bucket plus a `get_decision()` query, then
projects the V2 latent state back onto the V1 `Regime / Signal /
Direction / regime-confidence` vocabulary.  Drop-in replacement.

`build_engine(version, kwargs)` in `engine_factory.py` returns the right
object based on `engine_version` (1 → V1, 2 → V2 adapter).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# Import V1 Enums so the V1-compatible adapter (StrategyEngineV2Adapter) can
# emit the exact same `.value` strings the rest of the pipeline expects.
# This module is intentionally co-imported and works whether or not the
# adapter is actually used — the core V2 math class is fully independent.
from strategy_engine import Regime as _V1Regime, \
                            Signal as _V1Signal, \
                            Direction as _V1Direction

# numba is optional at import time so the module can still be imported in
# environments where the JIT compiler is unavailable (kernels fall back to
# a pure-NumPy path).  In production both `numba` and `scipy` are installed.
try:
    from numba import njit
    _HAVE_NUMBA = True
except Exception:  # pragma: no cover — import safety only
    _HAVE_NUMBA = False

    def njit(*args, **kwargs):  # type: ignore
        # Decorator that simply returns the function unchanged.
        def _wrap(fn):
            return fn
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return _wrap

try:
    from scipy.signal import find_peaks
    _HAVE_SCIPY = True
except Exception:  # pragma: no cover
    _HAVE_SCIPY = False

    def find_peaks(x, prominence=None):  # type: ignore
        x = np.asarray(x)
        peaks = []
        for i in range(1, len(x) - 1):
            if x[i] > x[i - 1] and x[i] >= x[i + 1]:
                peaks.append(i)
        return np.asarray(peaks, dtype=np.int64), None


# ─────────────────────────────────────────────────────────────────────────────
# 1.  State & Observation Definitions
# ─────────────────────────────────────────────────────────────────────────────

# Discrete regime labels (integer codes used inside the particle filter).
R_IDLE            = 0
R_CONSOLIDATION   = 1
R_TREND           = 2
R_CONTINUATION    = 3
R_EXHAUSTION      = 4
R_TRANSITION      = 5
R_REVERSAL        = 6

_REGIME_NAMES = {
    R_IDLE:          "idle",
    R_CONSOLIDATION: "consolidation",
    R_TREND:         "trend",
    R_CONTINUATION:  "continuation",
    R_EXHAUSTION:    "exhaustion",
    R_TRANSITION:    "transition",
    R_REVERSAL:      "reversal",
}


@dataclass
class Observation:
    """A single 1-second bucket of market observations (`obs` dict)."""
    dt:           float = 1.0
    log_return:   float = 0.0
    volume:       float = 0.0
    signed_delta: float = 0.0
    spread:       float = 0.0
    bid_depth:    float = 0.0
    ask_depth:    float = 0.0


@dataclass
class LatentState:
    """Continuous latent state vector Ψ_t = (x, μ, h, φ, ℓ)."""
    x:    float = 0.0   # log-price
    mu:   float = 0.0   # instantaneous drift
    h:    float = 0.0   # log-variance  (so σ² = exp(h))
    phi:  float = 0.0   # order-flow pressure
    ell:  float = 0.0   # log-liquidity
    regime: int = R_IDLE


# ─────────────────────────────────────────────────────────────────────────────
# 2.  Configuration — 16 free parameters with memecoin-tuned defaults
# ─────────────────────────────────────────────────────────────────────────────

# Names correspond 1:1 to the equations in strategyV2.md §2.
DEFAULT_CONFIG = {
    # Drift-mean reversion + coupling to order-flow imbalance
    "lambda_mu":  0.15,    # μ mean-reversion rate
    "kappa_mu":   0.05,    # coupling (φ - φ̄) → μ
    "sigma_mu":   0.10,    # drift shock std (per √s)

    # Log-variance (Hestig-like) OU process
    "eta":        0.10,    # h mean-reversion rate
    "sigma_h":    0.20,    # log-var shock std

    # Order-flow pressure AR(1)
    "alpha":      0.20,    # φ mean-reversion rate
    "beta":       1.00,    # δ_k / (v_k + ε) coefficient
    "sigma_phi":  0.15,    # φ shock std

    # Liquidity OU + jump dampener
    "theta":      0.10,    # ℓ mean-reversion rate
    "sigma_ell":  0.10,    # ℓ shock std
    "zeta":       0.30,    # liquidity-jump decay magnitude

    # Volume-profile decay (λ_d = 1/T_w).  iter16k: T_w = 14400 s (full
    # recording-lifetime KDE); legacy 300 s wiggle window is gone.
    "lambda_0":   1.0 / 14400.0,   # KDE exponential decay rate
    "lambda_1":   0.10,           # secondary slow-decay component

    # Jump-intensity Poisson rate per second (governs ℓ jumps)
    "kappa_J":    0.05,

    # Execution cost model coefficients  s(n, ℓ) = s_0(ℓ) + s_1(ℓ)·n
    # iter16h cost-calibration to the real 1.11% one-way cost:
    # ForwardTester at buy_size 0.1 SOL / 1% slippage ≈ 1.1% entry slippage
    # + flat fees → pre-calibration the engine was modelling ~0.2% (10×
    # undercount) and the spec §7 positive-EV gate was near-vacuous.
    "s_0":        0.011,   # base slippage fraction
    "s_1":        0.0005,  # marginal slippage per unit size

    # Sz-fixed meta-parameters (NOT counted among the 16 — set via config too)
    "n_particles":       200,    # N_p  — particle count
    "n_grid":            200,    # spatial grid for U(x,t)
    "grid_sigma_extent":  5.0,    # ±k·σ_t·√T_w  grid half-width
    "tw_window_seconds": 14400.0,  # T_w  (iter16k structural KDE memory)
    "tau_min":           5.0,    # shortest prediction horizon
    "tau_max":           30.0,   # longest prediction horizon
    "tau_step":          5.0,    # horizon sweep step
    "eps_div":           1.0,    # ε in δ_k / (v_k + ε)  (spec: ε=1.0)
    "fee_fraction":      0.0011,  # f   (iter16h cost-cal, ~0.11%)
    "latency_seconds":   0.5,   # Δ_lat
    "liquidity_cap_frac":0.10,   # n*  ≤ 0.1 · L_t
    "warmup_seconds":    30,     # bars below which no decision is emitted
    "ticks_per_state":   4,      # V1 4-state intra-candle expansion count
                                 #   (adapter-only; pure V2 ignores)
    "sigma_floor":       1e-6,   # numerical floor on σ
    "logprob_floor":     -50.0,  # clamp for log-likelihoods
    # iter21 hypothesis B — fraction of drift-work `±½μΔx` term restored
    # in Kramers `du_±` exponents (iter16d removed the full term).
    # Default 0.0 = iter21_k60_offs40 production parity.  Sweep [0.0, 0.1,
    # 0.3, 0.5].  REJECTED at smoke test (causes iter09-style churn).
    "v2_drift_work_fraction": 0.0,
}


def _merge_config(user: dict) -> dict:
    """Layer user config on top of the default config (case-insensitive)."""
    cfg = dict(DEFAULT_CONFIG)
    for k, v in (user or {}).items():
        if v is None:
            continue
        # accept user passing the 16 names verbatim
        cfg[k] = v
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# 3.  Per-particle Unscented Kalman Filter  (continuous layer)
# ─────────────────────────────────────────────────────────────────────────────
#
# State: y = [x, μ, h, φ, ℓ]^T   (5-D continuous latent state per particle).
# SDEs (discretised Euler-Maruyama, dt = Δt):
#   x_k  = x_{k-1} + (μ + φ/L)     Δt + σ_eff        ε_x
#   μ_k  = μ  (1 - λ_μ Δt)         + κ_μ (φ - φ̄) Δt + σ_μ √Δt ε_μ
#   h_k  = h  (1 - η Δt) + η h̄ Δt                  + σ_h √Δt ε_h
#   φ_k  = φ  (1 - α Δt)          + β δ/(v+ε) Δt   + σ_φ √Δt ε_φ
#   ℓ_k  = ℓ  (1 - θ Δt) + θ ℓ̄ Δt - ζ 1_{jump}     + σ_ℓ √Δt ε_ℓ
#
# σ_eff = exp(h/2)  — passed in from the discretized volatility equation.
#
# The UKF uses the scaled unscented transform (Merwe & Wan):
#   λ = α² (L + κ) − L ;  Wm_0 = λ/(L+λ),  Wc_0 = λ/(L+λ) + (1−α²+β),
#   Wm_i = Wc_i = 1/(2(L+λ))   for i>0
# Sigma points: χ_i = μ ± (√((L+λ) P))_i

_STATE_DIM = 5


@njit(cache=True)
def _ukf_predict_step(
    mu_prev, P_prev,           # state mean / cov at k-1
    sqrt_P,                     # pre-computed cholesky factor of P_prev (lower)
    cfg,                        # 1-D float array of the 16 scalars in fixed order
    dt,                         # time step
    bar_phi, bar_h, bar_ell,    # global EMAs of φ, h, ℓ
    sigma_eff,                  # exp(h_prev/2) — current volatility level
    obs_delta_volume_ratio,     # β · δ_k / (v_k + ε)  (pre-computed host-side)
    jump_indicator,             # 1.0 if a liquidity jump occurred this bucket
):
    """
    UKF prediction step for one particle.

    Computes sigma points from (mu_prev, P_prev), propagates each through
    the discretised SDE, then returns the predicted mean / cov and the
    propagated sigma points (the sigma points are needed again in the
    update step — we return them so the caller avoids recomputing them).

    cfg ordering (must match `_pack_cfg_kernels`):
      0: λ_μ   1: κ_μ   2: σ_μ   3: η   4: σ_h   5: α    6: β
      7: σ_φ   8: θ    9: σ_ℓ  10: ζ   11: λ_0  12: λ_1 13: κ_J
      14: s_0  15: s_1   16: ε_div
    """
    L = 5
    # Guard against non-finite or non-positive `dt`.  Spec passes Δt = 1.0
    # the normal case; the guard below is defensive-cover for any synthetic
    # replay / batch path that could feed 0 / NaN.
    if not math.isfinite(dt) or dt <= 0.0:
        dt = 1.0
    sqrt_dt = math.sqrt(dt)

    # UKF scalar auxiliaries (van der Merwe scaled unscented transform).
    # alpha=0.3, beta=2.0, kappa=0  — standard for a 5-D latent state.
    # The prior `alpha=1e-3` collapses L+λ → 0 and triggers 0/0 inside the
    # weight normalisation; we use the SciPy / filterpy default instead.
    alpha = 0.3
    beta = 2.0
    kappa = 0.0
    lam = alpha * alpha * (L + kappa) - L

    # Numerical floor — `L + lam` is a compile-time constant (0.45) and
    # always strictly positive, but defensively guard against FP underflow
    # so the JIT-emitted code never sees a 0/0 in the sigma-point weights.
    denom_w = L + lam
    if not math.isfinite(denom_w) or denom_w <= 0.0:
        denom_w = max(denom_w, 1e-12)
    Wm0 = lam / denom_w
    Wc0 = lam / denom_w + (1.0 - alpha * alpha + beta)
    Wi = 1.0 / (2.0 * denom_w)

    # ── Generate 2L+1 = 11 sigma points ──────────────────────────────────
    # sqrt_P is the lower-triangular Cholesky factor of P_prev, shape (L, L).
    # Sigma point i (for i in 1..L) = mu + scale · sqrt_P[:, i-1]
    #                i (for i in L+1..2L) = mu - scale · sqrt_P[:, i-L-1]
    SP = np.empty((2 * L + 1, L))
    for d in range(L):
        SP[0, d] = mu_prev[d]

    scale = math.sqrt(L + lam)
    for j in range(L):  # j-th column of sqrt_P
        # j+1 → +sigma_j ;  j+1+L → -sigma_j
        for r in range(L):
            col_val = scale * sqrt_P[r, j]   # entry (r, j) of scaled Cholesky
            SP[1 + j, r]     = mu_prev[r] + col_val
            SP[1 + L + j, r] = mu_prev[r] - col_val

    # ── Propagate each sigma point through the SDE ─────────────────────
    # Extract params
    lam_mu   = cfg[0]
    kappa_mu = cfg[1]
    sig_mu   = cfg[2] * sqrt_dt
    eta      = cfg[3]
    sig_h    = cfg[4] * sqrt_dt
    alpha_p  = cfg[5]
    beta_p   = cfg[6]
    sig_phi  = cfg[7] * sqrt_dt
    theta_p  = cfg[8]
    sig_ell  = cfg[9] * sqrt_dt
    zeta_p   = cfg[10]

    Y = np.empty((2 * L + 1, L))
    for i in range(2 * L + 1):
        x   = SP[i, 0]
        mu  = SP[i, 1]
        hv  = SP[i, 2]
        phi = SP[i, 3]
        ell = SP[i, 4]
        # Liquidity L = exp(ℓ) ; clamp to avoid blow-ups.
        L_t = math.exp(ell) if ell < 15.0 else math.exp(15.0)
        if L_t <= 1e-3:
            L_t = 1e-3   # iter04 — defensive floor that bounds |phi/L_t| ≤ 50/1e-3 = 5e4

        # x update
        x_new = x + (mu + phi / L_t) * dt + sigma_eff * 0.0  # ε_x sampled below
        # μ update (mean-reverting, coupled to φ)
        mu_new = mu * (1.0 - lam_mu * dt) + kappa_mu * (phi - bar_phi) * dt
        # h update (OU around global bar_h)
        h_new  = hv * (1.0 - eta * dt) + eta * bar_h * dt
        # φ update — deterministic drift from observed δ_k
        phi_new = phi * (1.0 - alpha_p * dt) + beta_p * obs_delta_volume_ratio * dt
        # ℓ update — OU around bar_ell + liquidity-jump decay
        ell_new = ell * (1.0 - theta_p * dt) + theta_p * bar_ell * dt - zeta_p * jump_indicator

        # Per-state hard clamps — sigma-eff and OU forces can otherwise
        # blow up in extreme memecoin regimes where δ_k / v_k saturates
        # at ±1, and the beta_p coupling amplifies φ every step.
        # These bounds are physiologically nonsensical anyway (drift >
        # 100% per second on a memecoin is NaN-prone) and protecting the
        # kernel ensures the rest of the pipeline stays causal.
        if x_new > 1e6:    x_new = 1e6
        elif x_new < -1e6: x_new = -1e6
        if mu_new > 50.0:   mu_new = 50.0
        elif mu_new < -50.0: mu_new = -50.0
        if h_new > 15.0:    h_new = 15.0
        elif h_new < -15.0: h_new = -15.0
        if phi_new > 50.0:  phi_new = 50.0
        elif phi_new < -50.0: phi_new = -50.0
        if ell_new > 15.0:  ell_new = 15.0
        elif ell_new < -15.0: ell_new = -15.0

        Y[i, 0] = x_new
        Y[i, 1] = mu_new
        Y[i, 2] = h_new
        Y[i, 3] = phi_new
        Y[i, 4] = ell_new

    # Add process noise to the mean prediction (sigma points already
    # enclose the covariance; we add the diagonal Q separately so the
    # predicted covariance is correct).
    mu_pred = np.zeros(L)
    for i in range(2 * L + 1):
        w = Wm0 if i == 0 else Wi
        for d in range(L):
            mu_pred[d] += w * Y[i, d]

    # Post-aggregation per-state clamp on the weighted mean (Wm0 can be
    # negative under the UKF alpha=0.3 tuning, so the weighted mean can
    # exceed every individual sigma-point value by an order of magnitude
    # when the sigma points are saturated at their per-state clamps.
    # Without re-clamping here, mu_pred[0] can be ±1e7 even though every
    # Y[i,0] ∈ [-1e6, 1e6], which the rest of the pipeline then has to
    # fight back from.  Mirror the per-state ranges here.
    if mu_pred[0] > 1e6:    mu_pred[0] = 1e6
    elif mu_pred[0] < -1e6: mu_pred[0] = -1e6
    if mu_pred[1] > 50.0:   mu_pred[1] = 50.0
    elif mu_pred[1] < -50.0: mu_pred[1] = -50.0
    if mu_pred[2] > 15.0:   mu_pred[2] = 15.0
    elif mu_pred[2] < -15.0: mu_pred[2] = -15.0
    if mu_pred[3] > 50.0:   mu_pred[3] = 50.0
    elif mu_pred[3] < -50.0: mu_pred[3] = -50.0
    if mu_pred[4] > 15.0:   mu_pred[4] = 15.0
    elif mu_pred[4] < -15.0: mu_pred[4] = -15.0

    # Augment the predicted covariance with process noise.
    # Q is diagonal, entries scaled by dt to match SDE diffusion coefficients.
    Q = np.zeros((L, L))
    Q[0, 0] = (sigma_eff ** 2) * dt
    Q[1, 1] = sig_mu * sig_mu
    Q[2, 2] = sig_h * sig_h
    Q[3, 3] = sig_phi * sig_phi
    Q[4, 4] = sig_ell * sig_ell

    P_pred = np.zeros((L, L))
    for i in range(2 * L + 1):
        w = Wc0 if i == 0 else Wi
        for r in range(L):
            for c in range(L):
                d_r = Y[i, r] - mu_pred[r]
                d_c = Y[i, c] - mu_pred[c]
                P_pred[r, c] += w * d_r * d_c
    # Symmetrise + add Q
    for r in range(L):
        for c in range(L):
            P_pred[r, c] += Q[r, c]
            # ensure symmetry
            if r < c:
                v = 0.5 * (P_pred[r, c] + P_pred[c, r])
                P_pred[r, c] = v
                P_pred[c, r] = v
            # Diagonal floor + cap (prevents runaway ill-conditioning
            # that would otherwise toxify the next predict step).
            if r == c:
                if P_pred[r, r] < 1e-12:
                    P_pred[r, r] = 1e-12
                if P_pred[r, r] > 1e3:
                    P_pred[r, r] = 1e3
            else:
                if P_pred[r, c] > 1e3:
                    P_pred[r, c] = 1e3
                elif P_pred[r, c] < -1e3:
                    P_pred[r, c] = -1e3

    return mu_pred, P_pred, Y


@njit(cache=True)
def _ukf_update_step(
    mu_pred, P_pred, Y,            # predicted state + sigma points
    obs_log_return,                # measurement y_k = Δx_k
    R,                             # measurement variance (scalar)
    prev_x,                        # mu_prev[0] — required to compute Δx in Z
):
    """
    UKF measurement-update step.  Observation model:  y = Δx_k = x_k - x_{k-1}.
    prev_x is the previous posterior mean of x, allowing Z[i] = Y[i,0] - prev_x.
    Returns (mu_post, P_post, Pzz, z_pred):
        mu_post — posterior state mean
        P_post  — posterior state covariance
        Pzz     — innovation variance, used by the particle filter for the
                  marginal likelihood weight update
        z_pred  — predicted measurement mean, used by the PF for the residual
    """
    L = mu_pred.shape[0]
    alpha = 0.3
    beta = 2.0
    kappa = 0.0
    lam = alpha * alpha * (L + kappa) - L
    denom_w = L + lam
    if not math.isfinite(denom_w) or denom_w <= 0.0:
        denom_w = max(denom_w, 1e-12)
    Wm0 = lam / denom_w
    Wc0 = lam / denom_w + (1.0 - alpha * alpha + beta)
    Wi = 1.0 / (2.0 * denom_w)

    # Predicted measurement for each sigma point.
    # Measurement y_k = Δx_k = x_k - x_{k-1}.  Each sigma point's x was
    # propagated from mu_prev[0] (the prior x); the predicted measurement
    # is the *change* in x, which is what the spec observes — NOT the level.
    # prev_x is the previous posterior mean of x passed in by the caller.
    Z = np.empty(2 * L + 1)
    for i in range(2 * L + 1):
        Z[i] = Y[i, 0] - prev_x

    z_pred = Wm0 * Z[0]
    for i in range(1, 2 * L + 1):
        z_pred += Wi * Z[i]

    Pzz = Wc0 * (Z[0] - z_pred) ** 2 + R
    for i in range(1, 2 * L + 1):
        Pzz += Wi * (Z[i] - z_pred) ** 2
    # Numerical floor — innovation variance must be strictly positive.
    # NB. `<= 0` leaves exact zeros (FP) that crash `1/Pzz` downstream.
    if Pzz < 1e-12:
        Pzz = 1e-12
    # Also guard against pathological overflow (residuals rarely above 1e6).
    if not math.isfinite(Pzz) or Pzz > 1e6:
        Pzz = 1e6

    Pxz = np.zeros(L)
    for i in range(2 * L + 1):
        w = Wc0 if i == 0 else Wi
        for d in range(L):
            Pxz[d] += w * (Y[i, d] - mu_pred[d]) * (Z[i] - z_pred)

    K = np.zeros(L)
    inv_Pzz = 1.0 / Pzz
    for d in range(L):
        kd = Pxz[d] * inv_Pzz
        # Clamp the Kalman gain magnitude.  When `Pzz` is tiny the gain
        # can align with even short cross-covariances and amplify tiny
        # residuals into state teleportations that no downstream clamp
        # can fully recover.  |K| ≤ 100 is plenty for a well-behaved
        # filter and prevents runaway behaviour on degenerate markets.
        if kd > 100.0:    kd = 100.0
        elif kd < -100.0: kd = -100.0
        K[d] = kd

    residual = obs_log_return - z_pred

    mu_post = np.zeros(L)
    for d in range(L):
        v = mu_pred[d] + K[d] * residual
        # Posterior state clamp — keeps the filter causally stable in
        # extreme memecoin regimes where a single wild `residual / Pzz`
        # Kalman update can teleport the latent state across 5 orders of
        # magnitude in one bar.  These bounds mirror the predict-step
        # per-state clamps so the SDE / measurement update pair stays
        # conservative across pathological data.
        if d == 0:                          # x — log-price, ±1e6 ≈ ±ln(1e6)
            if v >  1e6:    v =  1e6
            elif v < -1e6:  v = -1e6
        elif d == 1:                        # μ  — drift
            if v >  50.0:   v =  50.0
            elif v < -50.0: v = -50.0
        elif d == 2:                        # h  — log-variance
            if v >  15.0:   v =  15.0
            elif v < -15.0: v = -15.0
        elif d == 3:                        # φ  — order-flow pressure
            if v >  50.0:   v =  50.0
            elif v < -50.0: v = -50.0
        elif d == 4:                        # ℓ  — log-liquidity
            if v >  15.0:   v =  15.0
            elif v < -15.0: v = -15.0
        if not math.isfinite(v):
            v = mu_pred[d]
        mu_post[d] = v

    P_post = np.zeros((L, L))
    for r in range(L):
        for c in range(L):
            # Standard UKF measurement update:
            #   P_post = P_pred - K · Pxz^T
            # where Pxz is the (L,) cross-cov vector.  Subtracting K[r]*Pxz[c]
            # (= Pxz[r]/Pzz · Pxz[c]) is the correct Joseph-form shorthand:
            # K·Pxz^T subtracts the rank-1 product scaled by 1/Pzz.
            P_post[r, c] = P_pred[r, c] - K[r] * Pxz[c]
            # Symmetrise (the rank-1 product is already symmetric, but
            # embedding into the matmul can leave FP noise on the off-diag).
            if r < c:
                v = 0.5 * (P_post[r, c] + P_post[c, r])
                P_post[r, c] = v
                P_post[c, r] = v
            # Diagonal floor to preserve positive-definiteness.
            if r == c:
                if P_post[r, r] < 1e-12:
                    P_post[r, r] = 1e-12
                # Cap the diagonal too — a single wild covariance
                # otherwise toxifies the next predict step (1e34 etc.).
                if P_post[r, r] > 1e3:
                    P_post[r, r] = 1e3
            # Saturate off-diagonals (defend against pathological blow-ups
            # when residuals are extreme — keeps the filter runnable).
            if r != c:
                if P_post[r, c] > 1e3:
                    P_post[r, c] = 1e3
                elif P_post[r, c] < -1e3:
                    P_post[r, c] = -1e3

    return mu_post, P_post, Pzz, z_pred


# ─────────────────────────────────────────────────────────────────────────────
# 4.  Volume-Weighted KDE  (ρ(x, t))
# ─────────────────────────────────────────────────────────────────────────────
#
# ρ(x, t) = Σ_i w_i(t) · K_h(x - x_i)
#
#   w_i(t) = exp(-λ_d · (t - t_i))   (λ_d = 1/T_w)
#   K_h(u) = exp(-u² / (2 h²))       (Gaussian kernel; normalisation folded out
#                                     by the caller since we operate on ratios).
#
# Silverman's rule adapted for volume weights:
#   h = 0.9 · σ_x · n^{-1/5}        — here σ_x is a rolling weighted std.

@njit(cache=True)
def _kde_eval_kernel(
    grid_x,            # (G,) spatial grid in log-price
    trade_prices,      # (T,) prices of historical trades (log-price)
    trade_volumes,     # (T,) volumes of historical trades
    trade_times,       # (T,) unix timestamps of historical trades (s)
    decay_rate,        # λ_d
    bandwidth,         # h
    now_t,             # current time (s)
):
    """Vectorised weighted-Gaussian KDE evaluation on `grid_x` with spatial pruning.
    
    Each trade only contributes measurably to grid points within ±4.3σ (bandwidth).
    Beyond that, exp(-u²/2h²) < 1e-4 and is negligible. We binary-search the
    active grid range [g_lo, g_hi) per trade, reducing O(T·G) to O(T·w) where
    w << G for typical bandwidth/grid spacing ratios.
    """
    G = grid_x.shape[0]
    T = trade_prices.shape[0]
    out = np.zeros(G)
    inv_2h2 = 1.0 / (2.0 * bandwidth * bandwidth) if bandwidth > 0 else 0.0
    if bandwidth <= 0.0 or T == 0:
        return out
    
    # Spatial pruning cutoff: exp(-(4.3²/2)) ≈ 1e-4
    CUTOFF_SIGMAS = 4.3
    cutoff_dist = CUTOFF_SIGMAS * bandwidth
    
    # Grid spacing (assumed uniform)
    if G > 1:
        dx = grid_x[1] - grid_x[0]
    else:
        dx = 1.0
    if dx <= 0.0:
        dx = 1e-12
    
    for i in range(T):
        dt = now_t - trade_times[i]
        if dt < 0.0:
            dt = 0.0
        w = math.exp(-decay_rate * dt) * trade_volumes[i]
        if w <= 0.0:
            continue
        xp = trade_prices[i]
        
        # Active grid range: grid_x[g] ∈ [xp - cutoff, xp + cutoff]
        g_lo = int(math.ceil((xp - cutoff_dist - grid_x[0]) / dx))
        g_hi = int(math.floor((xp + cutoff_dist - grid_x[0]) / dx)) + 1
        if g_lo < 0:
            g_lo = 0
        if g_hi > G:
            g_hi = G
        if g_lo >= g_hi:
            continue
        
        for g in range(g_lo, g_hi):
            u = grid_x[g] - xp
            out[g] += w * math.exp(-u * u * inv_2h2)
    return out


@njit(cache=True)
def _liquidity_cost_kernel(
    grid_x,        # (G,) spatial grid
    x_t,           # current log-price
    L_t,           # current liquidity scalar
    depth_grid,    # (G,) interpolated local depth density D(u, t)
):
    """
    V_liq(x) = ∫_{x_t}^{x} |u - x_t| / sqrt(L_t · D(u, t)) du

    Computed as a running trapezoidal sum along `grid_x` outward from the
    index nearest to x_t.  Returns (V_lo, V_hi) — cost for moves below
    and above the current price, respectively, evaluated at every grid
    point.
    """
    G = grid_x.shape[0]
    V_lo = np.zeros(G)
    V_hi = np.zeros(G)
    if G < 2 or L_t <= 0:
        return V_lo, V_hi

    sqrt_L = math.sqrt(L_t)

    # Locate the index closest to x_t
    i_t = 0
    best_d = abs(grid_x[0] - x_t)
    for i in range(1, G):
        d = abs(grid_x[i] - x_t)
        if d < best_d:
            best_d = d
            i_t = i

    # Upward sweep: from i_t to G-1
    integral_hi = 0.0
    V_hi[i_t] = 0.0
    for i in range(i_t, G - 1):
        u0 = grid_x[i]
        u1 = grid_x[i + 1]
        d0 = depth_grid[i] if depth_grid[i] > 1e-12 else 1e-12
        d1 = depth_grid[i + 1] if depth_grid[i + 1] > 1e-12 else 1e-12
        # integrand f(u) = |u - x_t| / sqrt(L_t · D(u, t))
        f0 = abs(u0 - x_t) / (sqrt_L * math.sqrt(d0))
        f1 = abs(u1 - x_t) / (sqrt_L * math.sqrt(d1))
        du = u1 - u0
        integral_hi += 0.5 * (f0 + f1) * du
        V_hi[i + 1] = integral_hi

    # Downward sweep: from i_t down to 0
    integral_lo = 0.0
    V_lo[i_t] = 0.0
    for i in range(i_t, 0, -1):
        u0 = grid_x[i]
        u1 = grid_x[i - 1]
        d0 = depth_grid[i] if depth_grid[i] > 1e-12 else 1e-12
        d1 = depth_grid[i - 1] if depth_grid[i - 1] > 1e-12 else 1e-12
        f0 = abs(u0 - x_t) / (sqrt_L * math.sqrt(d0))
        f1 = abs(u1 - x_t) / (sqrt_L * math.sqrt(d1))
        du = u0 - u1
        integral_lo += 0.5 * (f0 + f1) * du
        V_lo[i - 1] = integral_lo

    return V_lo, V_hi


@njit(cache=True)
def _barrier_find_kernel(U_grid, x_idx):
    """
    Locate the nearest local minimum (basin, idx = x_idx) and the nearest
    local maxima to the right and left of x_idx that act as barriers.

    Returns (idx_basin, idx_barrier_up, idx_barrier_down, U_basin,
             U_up, U_down).
    The "barrier" on each side is the highest U value encountered before
    the local minimum on that side, OR the boundary of the grid, whichever
    comes first.

    NB. We carry out the search as: peak_to_next_min on each side, picking
    the smallest U value after the peak.
    """
    G = U_grid.shape[0]

    # Right sweep: find local max → next local min.
    idx_up_peak = -1
    idx_up_min  = -1
    U_up_max = -1e18
    for i in range(x_idx + 1, G):
        if U_grid[i] > U_up_max:
            U_up_max = U_grid[i]
            idx_up_peak = i
        elif U_grid[i] < U_grid[i - 1]:
            # Begin descending after a peak → walk to the local min.
            j = i
            while j + 1 < G and U_grid[j + 1] < U_grid[j]:
                j += 1
            idx_up_min = j
            break
    if idx_up_peak < 0:
        # U monotonically increasing to the right — boundary is the barrier.
        idx_up_peak = G - 1

    # Left sweep
    idx_down_peak = -1
    idx_down_min  = -1
    U_down_max = -1e18
    for i in range(x_idx - 1, -1, -1):
        if U_grid[i] > U_down_max:
            U_down_max = U_grid[i]
            idx_down_peak = i
        elif U_grid[i] < U_grid[i + 1]:
            j = i
            while j - 1 >= 0 and U_grid[j - 1] < U_grid[j]:
                j -= 1
            idx_down_min = j
            break
    if idx_down_peak < 0:
        idx_down_peak = 0

    return (x_idx, idx_up_peak, idx_down_peak,
            U_grid[x_idx], U_grid[idx_up_peak], U_grid[idx_down_peak])


@njit(cache=True)
def _second_derivative_grid(U_grid, dx):
    """Central-difference second derivative along a 1-D grid.

    (iter16f: `dx` added — the previous form returned the raw second
    DIFFERENCE ×0.5 without dividing by Δx², understating U'' by
    ~Δx²/2 ≈ 4e-5 on typical grids.  That made the Kramers attempt
    frequency √(ω₀²·ω_b²) ~160× too small and silenced all escapes once
    the anti-physical vol_corr inflation was removed.)
    """
    G = U_grid.shape[0]
    d2 = np.zeros(G)
    if G < 3 or dx <= 0:
        return d2
    inv_dx2 = 1.0 / (dx * dx)
    for i in range(1, G - 1):
        d2[i] = (U_grid[i + 1] - 2.0 * U_grid[i] + U_grid[i - 1]) * inv_dx2
    return d2


# ─────────────────────────────────────────────────────────────────────────────
# 5.  Rao-Blackwellised Particle Filter (RBPF)
# ─────────────────────────────────────────────────────────────────────────────
#
# Discrete layer: each particle carries a regime label `r_i` and an instance
# of a 5-D UKF state (mu_i ∈ R^5, P_i ∈ R^{5×5}).  Between observations
# each particle first *predicts* via its UKF (continuous propagation), then
# the measurement log-likelihood is computed, the particle weight is
# updated, and finally the standard multinomial resampling step runs when
# ESS falls below N_p / 2.

# A 1-D float array with the 17 kernel scalars in a fixed canonical order.
# Indices:
#   0:λ_μ 1:κ_μ 2:σ_μ 3:η 4:σ_h 5:α 6:β 7:σ_φ 8:θ 9:σ_ℓ 10:ζ
#   11:λ_0 12:λ_1 13:κ_J 14:s_0 15:s_1 16:ε_div
def _pack_cfg_kernels(c: dict) -> np.ndarray:
    arr = np.empty(17, dtype=np.float64)
    arr[0]  = float(c["lambda_mu"])
    arr[1]  = float(c["kappa_mu"])
    arr[2]  = float(c["sigma_mu"])
    arr[3]  = float(c["eta"])
    arr[4]  = float(c["sigma_h"])
    arr[5]  = float(c["alpha"])
    arr[6]  = float(c["beta"])
    arr[7]  = float(c["sigma_phi"])
    arr[8]  = float(c["theta"])
    arr[9]  = float(c["sigma_ell"])
    arr[10] = float(c["zeta"])
    arr[11] = float(c["lambda_0"])
    arr[12] = float(c["lambda_1"])
    arr[13] = float(c["kappa_J"])
    arr[14] = float(c["s_0"])
    arr[15] = float(c["s_1"])
    arr[16] = float(c["eps_div"])
    return arr


@njit(cache=True)
def _chol_lower_5x5(P):
    """Cholesky factor (lower-triangular) of a 5×5 SPD matrix P."""
    L = np.zeros((5, 5))
    for i in range(5):
        for j in range(i + 1):
            s = P[i, j]
            for k in range(j):
                s -= L[i, k] * L[j, k]
            if i == j:
                if s <= 0.0:
                    s = 1e-12
                L[i, j] = math.sqrt(s)
            else:
                L[i, j] = s / L[j, j] if L[j, j] > 0 else 0.0
    return L


# ─────────────────────────────────────────────────────────────────────────────
# Batched RBPF kernels (iter16 perf).  These replace the three Python
# per-particle loops in `RaoBlackwellisedParticleFilter.step()` with one
# tight njit loop over stacked (N,5)/(N,5,5) arrays.  Numerically byte-
# equivalent to the prior `_step_particle`/`_posterior_mean`/`derive_regime_
# topological` Python paths — same predict math, same Gaussian likelihood,
# same Mehra R update, same topological regime decision tree.
# ─────────────────────────────────────────────────────────────────────────────

@njit(cache=True)
def _rbpf_step_batched(
    mu_arr,        # (N,5)  in/out — particle mu (latent state)
    P_arr,         # (N,5,5) in/out — particle P
    sqrtP_arr,     # (N,5,5) in/out — particle Cholesky L
    log_w,         # (N,) in/out — particle log-weights
    cfg_arr,       # (17,)
    dt,
    obs_log_return,
    obs_delta_ratio,
    jump_indicator,
    R_meas,
    bar_phi,
    bar_h,
    bar_ell,
):
    """
    In-place RBPF predict+update+likelihood for all N particles.

    Returns (resid_mean_sq, pzz_mean) for the caller's Mehra R update.
    """
    N = mu_arr.shape[0]
    resid_acc = 0.0
    pzz_acc = 0.0
    for i in range(N):
        mu_i = mu_arr[i]
        P_i = P_arr[i]
        sqrtP_i = sqrtP_arr[i]

        h_i = mu_i[2]
        if h_i < 15.0:
            sigma_eff = math.exp(0.5 * h_i)
        else:
            sigma_eff = math.exp(7.5)
        if sigma_eff < 1e-6:
            sigma_eff = 1e-6

        prev_x = mu_i[0]

        mu_pred, P_pred, Y_sigma = _ukf_predict_step(
            mu_i, P_i, sqrtP_i, cfg_arr, dt,
            bar_phi, bar_h, bar_ell,
            sigma_eff, obs_delta_ratio, jump_indicator,
        )

        mu_post, P_post, Pzz, z_pred_from_update = _ukf_update_step(
            mu_pred, P_pred, Y_sigma, obs_log_return, R_meas, prev_x,
        )

        resid = obs_log_return - z_pred_from_update
        var_z = Pzz if Pzz > 0 else 1e-12
        log_lik = -0.5 * (resid * resid) / var_z - 0.5 * math.log(2.0 * math.pi * var_z)
        if not math.isfinite(log_lik):
            log_lik = -50.0

        # Write posterior state back into the stacked arrays in-place.
        for a in range(5):
            mu_arr[i, a] = mu_post[a]
        for a in range(5):
            for b in range(5):
                P_arr[i, a, b] = P_post[a, b]
        # Refresh Cholesky in-place.
        # Inlined _chol_lower_5x5 to avoid	view overhead of allocating
        # return array on each particle (kernel is only 5x5=15 flops).
        L = sqrtP_arr[i]
        for r in range(5):
            for c in range(r + 1):
                s = P_post[r, c]
                for k in range(c):
                    s -= L[r, k] * L[c, k]
                if r == c:
                    if s <= 0.0:
                        s = 1e-12
                    L[r, c] = math.sqrt(s)
                else:
                    L[r, c] = s / L[c, c] if L[c, c] > 0 else 0.0
            for c in range(r + 1, 5):
                L[r, c] = 0.0

        new_log_w = log_w[i] + log_lik
        if not math.isfinite(new_log_w):
            new_log_w = -1e18
        log_w[i] = new_log_w

        if math.isfinite(resid) and math.isfinite(Pzz):
            resid_acc += resid * resid
            pzz_acc  += Pzz

    resid_mean_sq = (resid_acc / N) if N > 0 else 1e-6
    pzz_mean       = (pzz_acc / N)  if N > 0 else 1e-6
    return resid_mean_sq, pzz_mean


@njit(cache=True)
def _posterior_mean_batched(mu_arr, weights):
    """
    NaN-safe weighted mean of N particle state vectors.  Returns the
    5-vector posterior mean (x, mu, h, phi, ell).
    """
    out = np.zeros(5)
    weight_sum = 0.0
    for i in range(mu_arr.shape[0]):
        w = weights[i]
        if (not math.isfinite(w)
                or not math.isfinite(mu_arr[i, 0])
                or not math.isfinite(mu_arr[i, 1])
                or not math.isfinite(mu_arr[i, 2])
                or not math.isfinite(mu_arr[i, 3])
                or not math.isfinite(mu_arr[i, 4])):
            continue
        weight_sum += w
        for a in range(5):
            out[a] += w * mu_arr[i, a]
    if weight_sum > 0:
        for a in range(5):
            out[a] /= weight_sum
    if not math.isfinite(out[0]): out[0] = 0.0
    if not math.isfinite(out[1]): out[1] = 0.0
    if not math.isfinite(out[2]): out[2] = -4.0
    if not math.isfinite(out[3]): out[3] = 0.0
    if not math.isfinite(out[4]): out[4] = 0.0
    return out


@njit(cache=True)
def _posterior_var_batched(mu_arr, weights, mean):
    """
    NaN-safe weighted posterior VARIANCE of the N particle state vectors,
    given the posterior mean 5-vector.  Returns the 5-vector of second
    central moments (var_x, var_mu, var_h, var_phi, var_ell).

    iter16j: the Kelly horizon-variance σ²_τ needs the posterior variance
    of the flow state φ — Var(∫₀^τ φ ds) ≈ Var(φ)·τ².  The legacy code
    plugged in E[φ]² instead, which by the variance identity
    E[φ]² = Var(φ) + E[φ]² systematically overstates the second moment
    by the squared posterior mean.
    """
    out = np.zeros(5)
    weight_sum = 0.0
    for i in range(mu_arr.shape[0]):
        w = weights[i]
        if (not math.isfinite(w)
                or not math.isfinite(mu_arr[i, 0])
                or not math.isfinite(mu_arr[i, 1])
                or not math.isfinite(mu_arr[i, 2])
                or not math.isfinite(mu_arr[i, 3])
                or not math.isfinite(mu_arr[i, 4])):
            continue
        weight_sum += w
        for a in range(5):
            d = mu_arr[i, a] - mean[a]
            out[a] += w * d * d
    if weight_sum > 0:
        for a in range(5):
            out[a] /= weight_sum
    for a in range(5):
        if not math.isfinite(out[a]) or out[a] < 0.0:
            out[a] = 0.0
    return out


@njit(cache=True)
def _derive_regimes_batched(mu_arr, regime_arr, mu_dot_post,
                            sigma_t_post, sigma_phi_post,
                            alpha, tau):
    """
    Vectorised `derive_regime_topological` over all particles, writing
    regime labels into `regime_arr` (N,) int32.  Per-particle mu/phi/h/ell
    are read from `mu_arr`; the per-particle mu_dot is approximated by the
    global posterior mu_dot_post, matching the prior Python loop.
    """
    mu_star  = sigma_t_post / math.sqrt(max(tau, 1e-9))
    phi_star = sigma_phi_post / math.sqrt(max(alpha, 1e-9))
    if not math.isfinite(mu_star):
        mu_star = 0.0
    if not math.isfinite(phi_star):
        phi_star = 0.0

    for i in range(mu_arr.shape[0]):
        mu   = mu_arr[i, 1]
        h    = mu_arr[i, 2]
        phi  = mu_arr[i, 3]
        ell  = mu_arr[i, 4]
        mu_dot = mu_dot_post

        abs_mu  = abs(mu)
        abs_phi = abs(phi)

        if abs_mu <= mu_star and abs_phi <= phi_star:
            regime_arr[i] = R_IDLE
            continue
        if abs_mu <= mu_star and abs_phi > phi_star:
            regime_arr[i] = R_CONSOLIDATION
            continue

        mu_aligned = (mu > 0.0 and mu_dot >= 0.0) or (mu < 0.0 and mu_dot <= 0.0)
        if abs_mu > mu_star and mu_aligned:
            flow_aligned_with_mu = (mu > 0.0 and phi > 0.0) or (mu < 0.0 and phi < 0.0)
            if flow_aligned_with_mu:
                regime_arr[i] = R_TREND
            else:
                regime_arr[i] = R_TRANSITION
            continue

        mu_decelerating = (mu > 0.0 and mu_dot < 0.0) or (mu < 0.0 and mu_dot > 0.0)
        if abs_mu > mu_star and mu_decelerating:
            flow_against_mu = (mu > 0.0 and phi < -phi_star) or (mu < 0.0 and phi > phi_star)
            if flow_against_mu:
                regime_arr[i] = R_REVERSAL
            else:
                regime_arr[i] = R_EXHAUSTION
            continue

        regime_arr[i] = R_CONTINUATION


@njit(cache=True)
def _systematic_resample_indices(weights, u):
    """
    Standard systematic resampling.  `weights` is the (N,) normalised
    weight vector; `u` is a single uniform [0, 1) draw.  Returns an (N,)
    int64 index array.
    """
    N = weights.shape[0]
    positions = np.empty(N, dtype=np.float64)
    for i in range(N):
        positions[i] = (u + i) / N

    cum = np.empty(N, dtype=np.float64)
    s = 0.0
    for i in range(N):
        s += weights[i]
        cum[i] = s
    if N > 0:
        cum[N - 1] = 1.0  # guard rounding

    idx = np.empty(N, dtype=np.int64)
    j = 0
    for i in range(N):
        pos = positions[i]
        while j < N - 1 and pos > cum[j]:
            j += 1
        idx[i] = j
    return idx


class _Particle:
    """One RBPF particle = (regime label, continuous UKF state)."""
    __slots__ = ("regime", "mu", "P", "sqrt_P", "weight", "log_w")

    def __init__(self, regime: int, mu: np.ndarray, P: np.ndarray,
                 weight: float):
        self.regime = regime
        self.mu = mu.astype(np.float64).copy()
        self.P  = P.astype(np.float64).copy()
        self.sqrt_P = _chol_lower_5x5(self.P)
        self.weight = float(weight)
        self.log_w = math.log(self.weight) if self.weight > 0 else -1e18


class RaoBlackwellisedParticleFilter:
    """
    Maintain N_p particles, each carrying a regime label and a per-particle
    UKF.  Provides `predict()` then `update(obs)` calls.

    The continuous state is the discretized SDE system of §2 in
    strategyV2.md.  The discrete regime label is sampled purely by the
    topological partition logic (`derive_regime_topological`) — particles
    of all regimes co-exist; their population weights reflect the posterior
    over regimes.
    """

    def __init__(self, n_particles: int, x0: float):
        N = int(n_particles)
        self.N = N
        # Initial covariance (mild inflation so the first step has spread).
        P0 = np.eye(5) * 0.01
        sqrt0 = _chol_lower_5x5(P0)
        # ── Batched state (iter16 perf) ─────────────────────────────────
        # All per-particle state is held in stacked numpy arrays so the
        # njit kernels in `_rbpf_step_batched` / `_posterior_mean_batched`
        # / `_derive_regimes_batched` can iterate the whole population in
        # one tight loop, eliminating ~600 Python-level dispatch ops per
        # state update.  Public attributes (`.particles`, `.bar_ell`, etc.)
        # remain externally observable so call-sites stay unchanged.
        regimes_all = [R_IDLE, R_CONSOLIDATION, R_TREND, R_CONTINUATION,
                       R_EXHAUSTION, R_TRANSITION, R_REVERSAL]
        mu_arr = np.zeros((N, 5), dtype=np.float64)
        for i in range(N):
            mu_arr[i, 0] = x0
            mu_arr[i, 2] = -4.0      # h=ln(σ²) → exp(-4)≈0.018
            mu_arr[i, 4] = math.log(1.0)   # ℓ=0 → L = 1
        P_arr    = np.broadcast_to(P0, (N, 5, 5)).astype(np.float64).copy()
        sqrtP_arr = np.broadcast_to(sqrt0, (N, 5, 5)).astype(np.float64).copy()
        log_w   = np.full(N, math.log(1.0 / N), dtype=np.float64)
        weights = np.full(N, 1.0 / N, dtype=np.float64)
        regime_arr = np.zeros(N, dtype=np.int32)
        for i in range(N):
            regime_arr[i] = regimes_all[i % len(regimes_all)]
        self._mu_arr    = mu_arr
        self._P_arr     = P_arr
        self._sqrtP_arr = sqrtP_arr
        self._log_w     = log_w
        self._weights   = weights
        self._regime_arr = regime_arr
        self.particles: list[_Particle] = []   # legacy list kept empty (see `.particles` property)

        # Global EMAs (recursive): bar_φ, bar_h, bar_ℓ.
        # ── OBSERVABLE-ANCHORED ANCHORS (iter02) ───────────────────────────────
        # The spec §2 says "recursive EMAs for φ̄_t and h̄_t must be updated
        # globally using the posterior mean estimates".  The original
        # implementation anchored h̄ to EMA(h_posterior) — but a self-
        # reference of an OU target to its own posterior mean has no
        # observable signal: the only stable point is the per-state clamp,
        # so h collapses to -15 catastrophically (see
        # backend/analysis/iter01_baseline_failure_modes.md §A).
        #
        # The Bayesian/SV literature standard (cf. Barndorff-Nielsen 1967,
        # Harvey 2016, Heston 1993) anchors the log-variance OU to the
        # *observable realised log-variance* of the return stream,
        # computed via EWMA(r²) in log-space.  This still respects the
        # state-space formulation (the latent h remains a recursive
        # estimate driven by μ⁄φ via the kernel-pulled posterior), but
        # the OU mean-reversion level is now fed from an observable
        # quantity — i.e. it is exactly the recursive "target" implied
        # by the spec.
        #
        # h̄_obs = log(EWMA(r²) + ε)                       (anchor for h OU)
        # φ̄_obs = EWMA(δ/(v+ε))                            (anchor for φ OU)
        # ℓ̄ is unchanged (no observable analogue — private L2 not streaming)
        #
        # The same EMA smoothing coefficient (0.05) is reused; ε is the
        # already-configured `eps_div`, σ_floor² guards log(0).
        self.bar_phi = 0.0
        self.bar_h = -4.0
        self.bar_ell = 0.0
        # EMA smoothing coefficient (matches kernel sigma_h speed).
        self._ema_alpha = 0.05

        # Observable-anchored EMAs — set inside step() each iteration.
        # `bar_obs_r2` tracks EWMA(r²) in raw log-return-squared units;
        # `bar_obs_dr` tracks EWMA(δ_k/(v_k+ε)); the log-space anchor
        # `bar_obs_log_r2 = max(log(bar_obs_r2), -15)` is then passed to
        # the UKF predict step as `bar_h` (overriding the prior latent
        # self-reference).  This is the mathematically correct SV-OU
        # anchor and is the same magnitude bar_h would have been if the
        # filter were dispatching the observation channel correctly.
        self.bar_obs_r2 = 1e-8        # Warm start at var ~ 10^{-8}
        self.bar_obs_dr = 0.0

        # Adaptive innovation-based R estimator (iter02).
        # Standard Mehra 1970 / Bayesian adaptive Kalman: estimate R
        # recursively from EWMA(innovation²).  Anchored to the true
        # measurement-noise process instead of the quoted-spread heuristic
        # — gives the filter a meaningful residual magnitude even when the
        # L2 spread is degenerate or the latent σ has stabilised.
        # Same smoothing coefficient (0.05).
        self.R_ema = 1e-6

        self._step_count = 0

    # ── Backwards-compatible `.particles` materialiser (iter16 perf) ─────
    # External code (offline introspection / pytest) may read `.particles`.
    # The batched state is now held in stacked arrays, so we materialise a
    # list of `_Particle` views on demand.  Hot paths must not call this —
    # they should read the stacked arrays directly.
    @property
    def particles_list(self) -> list[_Particle]:
        ps: list[_Particle] = []
        for i in range(self.N):
            p = _Particle(int(self._regime_arr[i]),
                          self._mu_arr[i].copy(),
                          self._P_arr[i].copy(),
                          float(self._weights[i]))
            p.sqrt_P = self._sqrtP_arr[i].copy()
            p.log_w = float(self._log_w[i])
            ps.append(p)
        return ps

    # ── individual-particle predict / update ─────────────────────────────
    def _step_particle(
        self,
        p: _Particle,
        cfg_arr: np.ndarray,
        dt: float,
        obs_log_return: float,
        obs_delta_ratio: float,
        jump_indicator: float,
        R_meas: float,
        process_noise_required: bool,
    ) -> tuple[float, float, float]:
        """
        One-particle predict + measurement update.

        Returns
        -------
        log_lik : float
            Marginal measurement log-likelihood (used for weight updates).
        resid   : float
            Innovation residual (obs_log_return - z_pred).
        Pzz     : float
            Innovation variance.  Together with `resid` these are used by
            `step()` to update the adaptive R estimate (iter02 fix for
            failure mode B).
        """
        sigma_eff = math.exp(0.5 * p.mu[2]) if p.mu[2] < 15.0 else math.exp(7.5)
        # Clamp volatility for numerical safety.
        if sigma_eff < 1e-6:
            sigma_eff = 1e-6

        # The previous x (posterior x at k-1) is the particle's current mu[0].
        prev_x = float(p.mu[0])

        mu_pred, P_pred, Y_sigma = _ukf_predict_step(
            p.mu, p.P, p.sqrt_P, cfg_arr, dt,
            self.bar_phi, self.bar_h, self.bar_ell,
            sigma_eff, obs_delta_ratio, jump_indicator,
        )

        mu_post, P_post, Pzz, z_pred_from_update = _ukf_update_step(
            mu_pred, P_pred, Y_sigma, obs_log_return, R_meas, prev_x,
        )

        # Measurement likelihood (Gaussian) for particle weight update.
        # Per RBPF theory, the marginal likelihood uses the UKF INNOVATION
        # variance `Pzz` (= predicted measurement covariance), NOT the
        # prior state variance P_pred[0,0] + R.  The UKF has already
        # accounted for cross-covariances (Pxz) when forming K, so Pzz is
        # the correct uncertainty for the observation residual.
        resid = obs_log_return - z_pred_from_update
        var_z = Pzz if Pzz > 0 else 1e-12
        log_lik = -0.5 * (resid * resid) / var_z - 0.5 * math.log(2.0 * math.pi * var_z)
        if not math.isfinite(log_lik):
            log_lik = -50.0

        # Refresh state on the particle.
        p.mu = mu_post
        p.P  = P_post
        p.sqrt_P = _chol_lower_5x5(p.P)
        p.log_w = p.log_w + log_lik
        return float(log_lik), float(resid), float(Pzz)

    def step(
        self,
        cfg: dict,
        cfg_arr: np.ndarray,
        dt: float,
        obs_log_return: float,
        obs_delta_ratio: float,
        jump_indicator: float,
        R_meas: float,
    ) -> dict:
        """
        One full RBPF step: predict + update + weight normalise + resample +
        derive aggregate regime + posterior state mean.
        """
        self._step_count += 1

        # ── Observable-anchored OU targets (iter02 fix for failure A) ────
        # Update bar_obs_r2 = EWMA(obs_log_return²) and bar_obs_dr = EWMA(δ-ratio)
        # BEFORE stepping particles, so the OU anchor h̄ used in each
        # particle's predict reflects the latest observable variance.
        a = self._ema_alpha
        r2_obs = float(obs_log_return) * float(obs_log_return)
        # Defensive against non-finite inputs (rare NaN from catastrophic log(0)).
        if not math.isfinite(r2_obs):
            r2_obs = self.bar_obs_r2
        self.bar_obs_r2 = (1.0 - a) * self.bar_obs_r2 + a * r2_obs
        # Tiny floor to keep log() mathematically defined.
        if self.bar_obs_r2 < 1e-30:
            self.bar_obs_r2 = 1e-30
        if not math.isfinite(obs_delta_ratio):
            obs_delta_ratio = self.bar_obs_dr
        self.bar_obs_dr = (1.0 - a) * self.bar_obs_dr + a * obs_delta_ratio

        # Override the OU anchors used by _ukf_predict_step:  the latency
        # `bar_h` and `bar_phi` are now the observable-anchored EWMA
        # estimates.  `bar_ell` retains the existing latent EMA because
        # there is no observable L2 channel feeding it (V1 surface passes
        # only buy/sell volume, not real L2 depth distributions).
        bar_h_obs = math.log(self.bar_obs_r2)        # log(EWMA(r²))
        # Clamp to per-state h bounds [−15, 15] consistent with the predict.
        if bar_h_obs > 15.0:   bar_h_obs = 15.0
        elif bar_h_obs < -15.0: bar_h_obs = -15.0
        # Persist so _posterior_mean / get_decision can observe the
        # observable anchor (the latent p.mu[2] still evolves per
        # particle via the kernel-pulled posterior).
        self.bar_h_obs = float(bar_h_obs)
        # bar_phi is the EWMA(δ-ratio) — directly observable.
        # Clamp to per-state φ bounds [−50, 50].
        phi_obs = float(self.bar_obs_dr)
        if phi_obs > 50.0:    phi_obs = 50.0
        elif phi_obs < -50.0: phi_obs = -50.0
        self.bar_phi_obs = phi_obs
        # Temporarily install observable anchors as the active OU targets
        # for the predict step (particular particles still pull their own
        # posterior via the UKF after the predict).
        # NB: We restore the latent EMAs after stepping particles for any
        # caller that introspects bar_h / bar_phi cosmetically — they're
        # always overwritten next step anyway.
        self.bar_h = bar_h_obs
        self.bar_phi = phi_obs

        # 1. Step every particle (batched njit — iter16 perf).
        # In-place predict+update+chol+likelihood on the stacked arrays.
        # `_rbpf_step_batched` returns the residual² / Pzz means used by
        # the Mehra R-ema update below.  All per-particle maths is byte-
        # equivalent to the prior `_step_particle` Python call.
        resid_mean_sq, pzz_mean = _rbpf_step_batched(
            self._mu_arr, self._P_arr, self._sqrtP_arr, self._log_w,
            cfg_arr, dt, obs_log_return, obs_delta_ratio,
            jump_indicator, R_meas,
            self.bar_phi, self.bar_h, self.bar_ell,
        )

        # ── Adaptive measurement variance R (iter02 fix for failure B) ──
        # Standard Mehra 1970 "innovation covariance estimate" — EWMA of
        # (residual²) per step.  This is no new free parameter: the
        # smoothing coefficient `a` is the same EMA bandwidth already used
        # by every other recursive estimator in this class, and the update
        # is in standard form R_ema ← (1-a) R_ema + a r².
        if math.isfinite(resid_mean_sq) and resid_mean_sq > 0:
            new_R = (1.0 - a) * self.R_ema + a * resid_mean_sq
            if new_R > 0 and math.isfinite(new_R):
                # Keep R_ema strictly positive; bin to a sensible range so a
                # single wild observation can't drive R to NaN-domination
                # in the next step.
                if new_R < 1e-20:
                    new_R = 1e-20
                if new_R > 1.0:
                    new_R = 1.0
                self.R_ema = float(new_R)

        # 2. Normalise weights in log-space (log-sum-exp).
        log_ws = self._log_w
        m = float(log_ws.max())
        if not math.isfinite(m):
            # All-NaN — reset to uniform.
            for i in range(self.N):
                self._log_w[i] = math.log(1.0 / self.N)
                self._weights[i] = 1.0 / self.N
        else:
            shifted = log_ws - m
            w = np.exp(shifted)
            Z = float(w.sum())
            if Z <= 0:
                w = np.full(self.N, 1.0 / self.N)
            else:
                w = w / Z
            for i in range(self.N):
                wi = float(w[i])
                self._weights[i] = wi
                self._log_w[i] = math.log(max(wi, 1e-300))

        # 3. ESS check + systematic resampling (batched).
        ess = 1.0 / float(np.sum(self._weights ** 2))
        if ess < self.N / 2.0:
            self._systematic_resample()

        # 4. Global EMA updates on posterior mean (recursive EMAs of §2).
        # NaN-safe weighted mean via njit reduction.
        pm = _posterior_mean_batched(self._mu_arr, self._weights)
        x_post, mu_post, h_post, phi_post, ell_post = (float(pm[0]), float(pm[1]),
                                                       float(pm[2]), float(pm[3]),
                                                       float(pm[4]))
        # iter16j: posterior second central moments (needed for the Kelly
        # horizon-variance σ²_τ's flow term — see _kramers_escape_and_decision).
        pv = _posterior_var_batched(self._mu_arr, self._weights, pm)
        # NB: bar_phi and bar_h are now observable-anchored (above) — they
        # are NOT re-EMA'd against the latent posterior mean, fixing the
        # self-referential positive feedback that catastrophically
        # collapsed every particle onto the -15 clamp in iter01.
        self.bar_ell = (1.0 - self._ema_alpha) * self.bar_ell + self._ema_alpha * ell_post

        # μ̇ for topological regime derivation — needs a one-step finite
        # difference of the posterior μ.  Iter05: also lift this so the
        # adapter exit logic can detect sustained-decline exits before the
        # kramers_down_exit threshold (P_down ≥ 0.5) fires.
        if not hasattr(self, "_mu_prev_for_dot"):
            self._mu_prev_for_dot = mu_post
        mu_dot_post = (mu_post - self._mu_prev_for_dot) / max(dt, 1e-6)
        self._mu_prev_for_dot = mu_post
        self.mu_dot_post = mu_dot_post

        # ── Per-particle topological regime re-derivation (iter02 fix for
        # failure D).  The spec §4 says regime is a *function* of the
        # continuous posterior phase vector, sampled deterministically.
        # Originally particle regime labels were frozen at initialisation,
        # so the regime posterior distribution stayed near-uniform forever
        # → entropy → confidence ≈ 0 → entry gate never opens.
        #
        # We re-derive each particle's regime label per step from its own
        # continuous posterior state, exactly as spec §4 mandates.  The
        # population distribution over particle regimes then becomes the
        # Bayesian regime posterior: a strong trend collapses to a delta
        # mass on R_TREND, ambiguous mixtures spread over 2-3 regimes, etc.
        sigma_t_post = math.exp(0.5 * h_post) if h_post < 15.0 else math.exp(7.5)
        if sigma_t_post < 1e-6:
            sigma_t_post = 1e-6
        sigma_phi_post = float(cfg["sigma_phi"]) / math.sqrt(
            2.0 * max(float(cfg["alpha"]), 1e-6))
        # Batched njit regime derivation — each particle reads its own
        # mu/phi/h/ell, the per-particle mu_dot is approximated by the
        # global posterior mu_dot_post (matches the prior Python loop).
        _derive_regimes_batched(
            self._mu_arr, self._regime_arr, mu_dot_post,
            sigma_t_post, sigma_phi_post,
            float(cfg["alpha"]), float(cfg["tau_max"]),
        )

        # 5. Regime distribution (population-weighted histogram of labels).
        regime_counts = np.zeros(7)
        for i in range(self.N):
            regime_counts[int(self._regime_arr[i])] += float(self._weights[i])
        regime_dist = regime_counts  # already normalised post-resample

        # Argmax regime = best-supported discrete label.
        best_regime = int(np.argmax(regime_counts))

        return {
            "x":    x_post,
            "mu":   mu_post,
            "h":    h_post,
            "phi":  phi_post,
            "ell":  ell_post,
            "var_phi": float(pv[3]),
            "regime": best_regime,
            "regime_dist": regime_dist,
        }

    # ── helpers ───────────────────────────────────────────────────────────
    def _posterior_mean(self) -> tuple[float, float, float, float, float]:
        """Weighted mean of particle state vectors (NaN-safe) — legacy shim
        over the batched kernel; kept for any external caller."""
        pm = _posterior_mean_batched(self._mu_arr, self._weights)
        return (float(pm[0]), float(pm[1]), float(pm[2]),
                float(pm[3]), float(pm[4]))

    def _systematic_resample(self):
        """Standard systematic-resampling step on the particle indices.

        Operates on the stacked arrays (`_mu_arr`, `_P_arr`, `_sqrtP_arr`,
        `_regime_arr`) — copies selected particles' state into fresh
        arrays so subsequent in-place updates don't mutate resampled
        siblings."""
        u = float(np.random.random())
        idx = _systematic_resample_indices(self._weights, u)
        N = self.N
        new_mu    = np.empty_like(self._mu_arr)
        new_P     = np.empty_like(self._P_arr)
        new_sqrtP = np.empty_like(self._sqrtP_arr)
        new_reg   = np.empty_like(self._regime_arr)
        for j in range(N):
            src = idx[j]
            new_mu[j]    = self._mu_arr[src]
            new_P[j]     = self._P_arr[src]
            new_sqrtP[j] = self._sqrtP_arr[src]
            new_reg[j]   = self._regime_arr[src]
        # Uniform weights after resampling.
        new_log_w   = np.full(N, math.log(1.0 / N), dtype=np.float64)
        new_weights = np.full(N, 1.0 / N, dtype=np.float64)
        self._mu_arr    = new_mu
        self._P_arr     = new_P
        self._sqrtP_arr = new_sqrtP
        self._regime_arr = new_reg
        self._log_w     = new_log_w
        self._weights   = new_weights


# ─────────────────────────────────────────────────────────────────────────────
# 6.  Topological Regime Derivation  (§4 of strategyV2.md)
# ─────────────────────────────────────────────────────────────────────────────
#
# Regime is determined ONLY by sign-patterns of the phase vector
#     p_t = (μ_t, μ̇_t, φ_t, h_t, ℓ_t, d_t)
#
# relative to the dynamically derived noise floors
#     μ* = σ_t / √τ         (drift noise floor)
#     φ* = σ_φ / √α         (order-flow noise floor)
#
# No fixed numeric thresholds.

def derive_regime_topological(
    mu: float, mu_dot: float, phi: float, h: float, ell: float,
    spread: float,
    sigma_t: float, sigma_phi: float,
    alpha: float, tau: float,
) -> int:
    """
    Return one of R_* integer codes, derived purely from sign components of
    the phase vector relative to the current dynamic noise floors.
    """
    mu_star  = sigma_t / math.sqrt(max(tau, 1e-9))
    phi_star = sigma_phi / math.sqrt(max(alpha, 1e-9))

    if not math.isfinite(mu_star):
        mu_star = 0.0
    if not math.isfinite(phi_star):
        phi_star = 0.0

    abs_mu  = abs(mu)
    abs_phi = abs(phi)

    # ── Idle: everything below the noise floor ──────────────────────
    if abs_mu <= mu_star and abs_phi <= phi_star:
        return R_IDLE

    # ── Consolidation: drift below floor but flow is pressuring hard ─
    #   (low-volatility environment, accumulators at work).
    if abs_mu <= mu_star and abs_phi > phi_star:
        return R_CONSOLIDATION

    # ── Trend: drift above floor and accelerating, flow aligned ─────
    mu_aligned = (mu > 0 and mu_dot >= 0.0) or (mu < 0 and mu_dot <= 0.0)
    if abs_mu > mu_star and mu_aligned:
        # Strong flow / weak exhaustion → trend.  If flow direction has
        # already flipped against the drift → transition pending.
        flow_aligned_with_mu = (mu > 0 and phi > 0) or (mu < 0 and phi < 0)
        if flow_aligned_with_mu:
            return R_TREND
        return R_TRANSITION

    # ── Exhaustion: drift above floor but decelerating ──────────────
    mu_decelerating = (mu > 0 and mu_dot < 0) or (mu < 0 and mu_dot > 0)
    if abs_mu > mu_star and mu_decelerating:
        # Strong opposing flow → reversal pending, otherwise mild exhaustion.
        flow_against_mu = (mu > 0 and phi < -phi_star) or (mu < 0 and phi > phi_star)
        if flow_against_mu:
            return R_REVERSAL
        return R_EXHAUSTION

    # ── Continuation: drift aligned with flow, weak deceleration ────
    # (post-exhaustion re-launch in the same direction).
    return R_CONTINUATION


# ─────────────────────────────────────────────────────────────────────────────
# 7.  Market Potential  (§3 of strategyV2.md)
# ─────────────────────────────────────────────────────────────────────────────
#
# U(x, t) = - T_t · log ρ(x, t) + V_liq(x, t)     with  T_t = exp(h_t) / 2


class MarketPotential:
    """
    Rolling buffer of (price, volume, timestamp) historical trades, with
    incremental KDE evaluation on a spatial grid centred on the current
    log-price x_t.

    The grid is re-built per state update (cheap: ~200 pts × numba kernel).
    """

    def __init__(self, n_grid: int, tw_seconds: float, lambda_decay: float):
        self.n_grid = int(n_grid)
        self.tw_seconds = float(tw_seconds)
        self.lambda_decay = float(lambda_decay)

        # Rolling trade buffer (price=ln, volume, time).  Cap the buffer
        # at 2·T_w seconds worth of one-per-second observations (max ~600).
        self._max_buffer = int(self.tw_seconds * 2)
        self._prices: list[float] = []
        self._volumes: list[float] = []
        self._times: list[float] = []
        
        # Lazy cached ndarrays (iter16 perf — avoid repeated list→array conversion)
        self._buf_dirty = True
        self._prices_arr = np.zeros(self._max_buffer, dtype=np.float64)
        self._volumes_arr = np.zeros(self._max_buffer, dtype=np.float64)
        self._times_arr = np.zeros(self._max_buffer, dtype=np.float64)

        # Most-recent potential state (lazily computed in `compute`).
        self.last_grid:    np.ndarray = np.zeros(self.n_grid)
        self.last_U:       np.ndarray = np.zeros(self.n_grid)
        self.last_T:       float = 0.0
        self.last_rho:     np.ndarray = np.zeros(self.n_grid)
        self.last_V_lo:    np.ndarray = np.zeros(self.n_grid)
        self.last_V_hi:    np.ndarray = np.zeros(self.n_grid)
        self.last_depth:   np.ndarray = np.zeros(self.n_grid)
        self.last_bandwidth: float = 1e-3

    def add_trade(self, log_price: float, volume: float, t_seconds: float):
        self._prices.append(log_price)
        self._volumes.append(max(float(volume), 0.0))
        self._times.append(float(t_seconds))
        self._buf_dirty = True
        # Trim buffer (FIFO) + global decay prune
        if len(self._prices) > self._max_buffer:
            self._prices  = self._prices[-self._max_buffer:]
            self._volumes = self._volumes[-self._max_buffer:]
            self._times   = self._times[-self._max_buffer:]
        # Aggressive decay pruning — drop anything older than 3·T_w since
        # exp(-λ_d · 3T_w) = exp(-3) ≈ 0.05 already, well below floor.
        if self._times:
            cutoff = self._times[-1] - 3.0 * self.tw_seconds
            # First non-stale index (linear scan — buffer is small).
            i = 0
            while i < len(self._times) and self._times[i] < cutoff:
                i += 1
            if i > 0:
                self._prices  = self._prices[i:]
                self._volumes = self._volumes[i:]
                self._times   = self._times[i:]

    def _refresh_buffer_arrays(self):
        """Copy list buffers into pre-allocated ndarrays (iter16 perf)."""
        if not self._buf_dirty:
            return
        n = len(self._prices)
        if n > 0:
            # Use numpy array assignment instead of Python loop (100x+ faster)
            self._prices_arr[:n] = self._prices
            self._volumes_arr[:n] = self._volumes
            self._times_arr[:n] = self._times
        self._buf_dirty = False

    def _silverman_bandwidth(self) -> float:
        if len(self._prices) < 2:
            return 1e-3
        self._refresh_buffer_arrays()
        n = len(self._prices)
        x = self._prices_arr[:n]
        v = self._volumes_arr[:n]
        # Volume-weighted std σ.
        if v.sum() <= 0:
            s = float(np.std(x))
        else:
            w = v / v.sum()
            mean = float(np.sum(w * x))
            var = float(np.sum(w * (x - mean) ** 2))
            s = math.sqrt(max(var, 1e-12))
        h = 0.9 * s * (n ** (-1.0 / 5.0))
        if h <= 0:
            h = 1e-3
        return float(h)

    def compute(self, x_t: float, h_t: float, bid_depth: float, ask_depth: float):
        """
        Evaluate the potential U(x, t) on a 200-point grid centred on x_t.

        bid_depth / ask_depth are L2-style depth quantities at this moment —
        we linearly interpolate them across the grid as a crude depth
        density D(u, t).  A true L2 snapshot isn't available in this pure
        engine, so we model it as a side-asymmetric exponential taper.
        """
        sigma_t = math.exp(0.5 * h_t) if h_t < 15.0 else math.exp(7.5)
        if sigma_t < 1e-6:
            sigma_t = 1e-6
        grid_half = self.n_grid * 1e-3  # fallback
        # ±5 σ √T_w window
        span = 5.0 * sigma_t * math.sqrt(self.tw_seconds)
        if span < 1e-3:
            span = 1e-3
        grid = np.linspace(x_t - span, x_t + span, self.n_grid)

        h_bw = self._silverman_bandwidth()
        self.last_bandwidth = h_bw

        if self._prices:
            self._refresh_buffer_arrays()
            n = len(self._prices)
            P = self._prices_arr[:n]
            V = self._volumes_arr[:n]
            T_ = self._times_arr[:n]
            now_t = float(self._times[-1])
            rho = _kde_eval_kernel(grid, P, V, T_,
                                   self.lambda_decay, h_bw, now_t)
        else:
            rho = np.ones(self.n_grid) * 1e-12

        # Guard against zero density (log of 0).
        rho_max = float(rho.max()) if rho.size else 0.0
        if rho_max <= 0:
            rho = np.full(self.n_grid, 1e-12)
            rho_max = 1e-12
        rho = rho / rho_max  # normalise to [0, 1] for stability

        # ── Depth density D(u, t): asymmetric taper based on bid/ask depth ──
        # Above x_t we use ask-side depth; below we use bid-side depth.
        depth = np.empty(self.n_grid)
        for i in range(self.n_grid):
            u = grid[i]
            if u >= x_t:
                # exponential taper from ask_depth at x_t to 1e-6 at x_t+span
                frac = (u - x_t) / span if span > 0 else 0.0
                frac = min(max(frac, 0.0), 1.0)
                base_d = max(ask_depth, 1e-6)
                depth[i] = base_d * math.exp(-3.0 * frac) + 1e-6
            else:
                frac = (x_t - u) / span if span > 0 else 0.0
                frac = min(max(frac, 0.0), 1.0)
                base_d = max(bid_depth, 1e-6)
                depth[i] = base_d * math.exp(-3.0 * frac) + 1e-6

        # ── Liquidity cost V_liq(x, t) ────────────────────────────────────
        L_t = math.exp(0.0)  # L_t = exp(ℓ_t) supplied by caller ideally
        # We use exp(ℓ_posterior) for L_t — passed in as separate param? No.
        # Simpler: derive from sum of bid+ask depth.
        L_now = max(bid_depth + ask_depth, 1e-6)
        V_lo, V_hi = _liquidity_cost_kernel(grid, x_t, L_now, depth)

        T_t = math.exp(h_t) * 0.5 if h_t < 15.0 else math.exp(15.0) * 0.5

        # U(x) = - T_t · log ρ(x)            (KDE-native potential)
        #
        # (iter16d, 2026-07-29: V_liq REMOVED from the potential.  On fresh
        # 1 s memecoin data T_t = σ²/2 ≈ 5e-5 while V_liq ≈ 0.05–0.7, so
        # the legacy composite U = -T·lnρ + V_liq was entirely dominated by
        # the synthetic depth taper:  barrier-find then picked ~T-amplitude
        # KDE wiggles on the V_liq ramp (noise barriers) and the Boltzmann
        # factor exp(-ΔU/T) ≡ 0 degenerated k± to the {0, 1e6} clamp —
        # the engine's decision collapsed to a binary 1 s drift-sign
        # detector (24% WR in both drift-sign conventions; see iter16b).
        # The spec's structure (§3.3 + §4.3) assigns V_liq the friction
        # role — γ_t = 1/L_t in the rate prefactor (eq. 14) — while the
        # escape geometry comes from the volume-density landscape.  With
        # U = -T·lnρ (HVN = potential well — the dip-buy geometry whose
        # entries were validated profitable in iter16k/l).  The iter16n
        # experiment (U = +T·lnρ, HVN = barrier) fixed crash-detection
        # but destroyed entry quality (smoke +0.43 → -0.05): the two
        # interpretations are reconciled by keeping the well geometry
        # for the landscape and fixing the boundary-barrier artifact in
        # _barrier_find_kernel (iter16o) — during crashes the down side
        # has NO barrier (monotonic to grid edge) → open escape at the
        # attempt rate → P_down can fire, without touching the wells.
        # V_lo/V_hi are still computed above for diagnostics.)
        eps_rho = 1e-12
        U_kde   = -T_t * np.log(np.maximum(rho, eps_rho))
        U_up    = U_kde
        U_down  = U_kde
        U       = U_kde

        self.last_grid      = grid
        self.last_U         = U
        self.last_U_up      = U_up
        self.last_U_down    = U_down
        self.last_T         = T_t
        self.last_rho       = rho
        self.last_V_lo      = V_lo
        self.last_V_hi      = V_hi
        self.last_depth     = depth
        self.last_sigma_t   = sigma_t
        self.last_grid_span  = span
        return grid, U, U_up, U_down, T_t, rho


# ─────────────────────────────────────────────────────────────────────────────
# 8.  Modified Kramers Escape & Decision Logic  (§5 + §6 of strategyV2.md)
# ─────────────────────────────────────────────────────────────────────────────

def _kramers_escape_and_decision(
    cfg: dict,
    potential: MarketPotential,
    state: dict,           # latest RBPF posterior
    mu_dot: float,         # μ̇_t estimate
    sigma_t: float,
    sigma_phi: float,
    L_t: float,            # current liquidity scalar (exp(ℓ_posterior))
    tau: float,            # horizon (s)
) -> dict:
    """
    Compute Kramers escape rates k±, transition probabilities P±, P0 over
    horizon `tau`, and the Kelly-optimal decision.

    All math in this routine mirrors §5 + §6 of the spec exactly:
      ω_0²  = U''(x_min)         — basin curvature
      ω_b²  = |U''(x_saddle)|     — barrier curvature
      ρ ratio, volatility correction, slow-variation transition probs.
      Kelly sizing: n* = (μ̂_τ - f - s_0 - μ·Δ_lat) / (σ²_τ + ∂s/∂n)
                     capped at 0.1·L_t.
    """
    grid = potential.last_grid
    U = potential.last_U
    T_t = potential.last_T
    rho = potential.last_rho
    eps = cfg["sigma_floor"]

    # Locate x_t index on the grid.
    x_t = float(state["x"])
    idx_t = int(np.argmin(np.abs(grid - x_t)))

    # Find barriers around current basin.
    idx_basin, idx_up, idx_down, U_basin, U_up, U_down = _barrier_find_kernel(U, idx_t)

    # Barrier energies — KDE-native (iter16e):
    #   ΔU±_t = U(x±_t) - U(x_t)  =  -T_t · [ln ρ(x±) - ln ρ(x_t)]
    #
    # Spec §4.1 eq. 12 replaces the Boltzmann factor with the empirical
    # density ratio; the drift-work term of §3.4 is therefore subsumed in
    # the escape exponent and the posterior drift enters the DECISION
    # through the Kelly moments instead — μ̂_τ = P⁺d⁺ − P⁻d⁻ (§7.5
    # eq. 34) and the latency cost C = f + s₀ + |μ_t|Δ_lat (§7.1).
    # This is also a scale necessity: at the 1 s memecoin scale
    # ½μΔx/T ~ 10–100, so any drift-inclusive exponent swamps the
    # density geometry with tick-level μ noise (iter16d smoke: 1421
    # trades / 8 recordings, 15.7% WR — the exponent reduced to a
    # e^{±μΔx/2T} drift-sign detector again).
    # With ΔU±/T = Δlnρ ~ O(0.1–5) the Boltzmann factor IS the density
    # ratio, and the vol-of-vol correction ½(ΔU±/T)²·Var(h) is finite
    # and unsaturated for the first time on any dataset.
    mu_t = float(state["mu"])
    # iter21 hypothesis B (REJECTED at smoke test): restoring a fraction of
    # the iter16d-removed drift-work term `±½·μ_t·Δx_±` in `du_±` causes
    # immediate sign-flip churn on rec70 (NO counter-trade):
    #   f=0.0 (iter19): 2 trades @ +0.0096 SOL
    #   f=0.1:         10 trades @ -0.0248 SOL (20% WR — iter09-style churn)
    #   f=0.5:         27 trades @ -0.0425 SOL (22% WR)
    # The drift-work term couples the OU-process μ_t (which oscillates
    # tick-to-tick) directly to the barrier energy, so each μ sign-flip
    # now flips k_up/k_down ordering — replicating iter09's pathology even
    # at small f.  Kept as cfg knob with default 0.0 = iter21_k60_offs40
    # production parity.
    f_drift = float(cfg.get("v2_drift_work_fraction", 0.0))
    if f_drift > 0.0 and idx_up is not None and idx_down is not None:
        dw_up   = 0.5 * mu_t * (float(grid[idx_up])   - x_t)
        dw_down = 0.5 * mu_t * (float(grid[idx_down]) - x_t)
        du_up   = (U_up   - U_basin) + f_drift * dw_up
        du_down = (U_down - U_basin) + f_drift * dw_down
    else:
        du_up   = U_up   - U_basin
        du_down = U_down - U_basin

    # Second derivatives (curvatures) via central difference.
    # Grid spacing Δx is required for true U'' (iter16f).
    n_g = int(grid.shape[0])
    dx_g = float(grid[1] - grid[0]) if n_g > 1 else 1.0
    d2 = _second_derivative_grid(U, dx_g)
    omega0_sq = float(d2[idx_t])  # U'' at the current basin (x ≈ x_min)
    if omega0_sq <= 0:
        omega0_sq = 1e-6   # numerical guard
    omega_b_up   = abs(float(d2[idx_up]))   if idx_up is not None else 1e-6
    omega_b_down = abs(float(d2[idx_down])) if idx_down is not None else 1e-6
    if omega_b_up   <= 0:
        omega_b_up = 1e-6
    if omega_b_down <= 0:
        omega_b_down = 1e-6

    # Non-equilibrium density ratio ρ(x⁺)/ρ(x) , ρ(x⁻)/ρ(x).
    rho_at_t = float(rho[idx_t]) if rho[idx_t] > 0 else 1e-12
    rho_up   = float(rho[idx_up])   if 0 <= idx_up   < rho.shape[0] else 1e-12
    rho_down = float(rho[idx_down]) if 0 <= idx_down < rho.shape[0] else 1e-12
    ratio_up   = rho_up   / rho_at_t
    ratio_down = rho_down / rho_at_t

    # Volatility correction: DISABLED (iter16f).
    # Spec §4.2 eq. 13 averages the Boltzmann factor over the volatility
    # posterior:  ⟨e^{-ΔU/T}⟩ ≈ e^{-ΔU/T_t} · exp[½(ΔU/T_t)²·Var(h)].
    # That Gaussian-moment (lognormal-mgf) expansion is only valid for
    # |ΔU/T|·√Var(h) ≪ 1.  On fresh 1 s data |ΔU/T| ~ 5–30 and the
    # correction ANTI-INVERTS the physics: because exp[½(ΔU/T)²Var(h)]
    # grows faster than e^{-ΔU/T} decays, FARTHER barriers receive
    # HIGHER escape rates (rec205 trace: du_up/T ≈ 31 → Boltzmann 1e-14
    # but vol_corr = e^50 clamp → k_up = 8e6 ≫ k_down → P_up ≡ 1.00 on
    # 690/1352 bars — constant buy pressure, 33 churn trades).
    # Moreover Var(h) was the hard-coded OU stationary variance
    # σ_h²/(2η) = 0.2, not a posterior estimate.  The §4.1 eq. 12 exact
    # density ratio already incorporates the finite-temperature escape
    # statistics, so the moment correction is dropped entirely.
    var_h = (cfg["sigma_h"] ** 2) / (2.0 * max(cfg["eta"], 1e-6))
    vol_corr_up = 1.0
    vol_corr_down = 1.0

    # ── Modified Kramers escape rates ──────────────────────────────────
    # k± = (√(ω₀²·ω_b±²) / (2π γ)) · exp(-ΔU±/T) · vol_corr±
    #
    # With the KDE-native potential (iter16d), the Boltzmann factor
    #   exp(-ΔU±/T) = [ρ(x±)/ρ(x_t)] · exp(±½μΔx/T)
    # already IS the §4.1 eq. 12 empirical density ratio (times the
    # drift-work Boltzmann factor of §3.4), so the legacy separate
    # `ratio±` multiplier is subsumed — multiplying it again would square
    # the density ratio.  The friction γ = 1/L_t (eq. 14) carries the
    # liquidity term's non-equilibrium role.  The exponent is clamped to
    # ±50 for numerical safety — this replaces the legacy `du ≤ 0 →
    # k = 1e6` clamp that binary-degenerated the decision.
    gamma = 1.0 / max(L_t, 1e-6)

    arg_rate_up   = -du_up   / T_t if T_t > 0 else 0.0
    arg_rate_down = -du_down / T_t if T_t > 0 else 0.0
    arg_rate_up   = max(-50.0, min(50.0, arg_rate_up))
    arg_rate_down = max(-50.0, min(50.0, arg_rate_down))

    k_up = (math.sqrt(omega0_sq * omega_b_up) / (2.0 * math.pi * gamma)) \
           * math.exp(arg_rate_up) * vol_corr_up
    k_down = (math.sqrt(omega0_sq * omega_b_down) / (2.0 * math.pi * gamma)) \
             * math.exp(arg_rate_down) * vol_corr_down

    # ── Transition probabilities over horizon τ  (slow-variation approx) ──
    #   P±(τ) = (k± / k_total) · (1 - exp(-k_total · τ))
    #   P0(τ) = exp(-k_total · τ)
    k_total = k_up + k_down
    if k_total <= 0 or not math.isfinite(k_total):
        k_total = 1e-9
    exp_term = math.exp(-k_total * tau)
    P_up   = (k_up / k_total) * (1.0 - exp_term)
    P_down = (k_down / k_total) * (1.0 - exp_term)
    P_zero = exp_term

    # ── Kelly sizing n* ─────────────────────────────────────────────────────
    # μ̂_τ per spec §7.5 eq. 34: the posterior expected log-return over the
    # horizon is the probability-weighted distance to the barriers,
    #   μ̂_τ = P⁺(τ)·d⁺ − P⁻(τ)·d⁻,
    # with d± = |x± − x_t|.  BOUNDED by the barrier geometry, unlike the
    # legacy extrapolation μ̂_τ = μ_t·τ + (φ_t/α)·τ which projected the
    # instantaneous 1 s drift 30 s forward (μ̂_τ ~ 1.5–150 log units at
    # entries → E_star ≈ +12 always → the Kelly gate was vacuous).
    # σ²_τ keeps the existing posterior variance proxy.
    d_up_dist = abs(float(grid[idx_up]) - x_t) if idx_up is not None else 0.0
    d_down_dist = abs(x_t - float(grid[idx_down])) if idx_down is not None else 0.0
    mu_hat_tau = P_up * d_up_dist - P_down * d_down_dist
    # iter16j: σ²_τ = posterior variance of the log-return over horizon τ.
    #   diffusion term:      σ_t²·τ           (random-walk variance ∝ τ)
    #   flow-uncertainty:    Var_RBPF(φ)·τ²   (variance of ∫φ ds)
    # The legacy plug-in used E[φ]² for the flow term — the squared
    # posterior MEAN instead of the posterior VARIANCE.  With real order
    # flow (|φ̄| ~ 0.1–0.15) that overstates σ²_τ by ~10⁴× vs the diffusion
    # term (φ²·τ² ~ 324 vs σ_t²·τ ~ 0.03 at τ = 120), collapsing
    # n* → ~0, making the ℰ* > 0 Kelly gate vacuous, and pinning the
    # τ-sweep to its minimum (σ² ∝ τ² dominates).  Using the particle
    # population's actual posterior variance restores the intended
    # risk-discrimination of the Kelly gate.
    var_phi = float(state.get("var_phi", state["phi"] ** 2))
    if var_phi < 0.0 or not math.isfinite(var_phi):
        var_phi = 0.0
    sigma2_tau = (sigma_t ** 2) * tau + var_phi * (tau ** 2)
    if sigma2_tau <= 0:
        sigma2_tau = 1e-12

    f_fee = float(cfg["fee_fraction"])
    s_0 = float(cfg["s_0"])
    s_1 = float(cfg["s_1"])
    lat = float(cfg["latency_seconds"])
    mu_lat_cost = mu_t * lat
    numerator = abs(mu_hat_tau) - f_fee - s_0 - abs(mu_lat_cost)
    denom = sigma2_tau + s_1
    if denom <= 0:
        n_star = 0.0
    else:
        n_star = numerator / denom
    # Cap by 0.1 · L_t (liquidity cap).
    cap = cfg["liquidity_cap_frac"] * max(L_t, 0.0)
    if cap > 0 and n_star > cap:
        n_star = cap
    if n_star < 0:
        n_star = 0.0
    # ── DIRECTION DETERMINATION (iter03 — spec §5 correct interpretation) ──
    # The spec §6 says "Determine direction z* ∈ {-1, 0, 1}" and §5 makes
    # the Bayesian transition probabilities P^+, P^-, P^0 explicit
    # outputs of the engine.  The mathematically correct determination
    # (and the form that maximises expected utility over the Bayesian
    # mixture of upward/downward/zero escape) is the comparison:
    #     z* = +1  iff  P^+ > P^-   and   P^+ > P^0
    #     z* = -1  iff  P^- > P^+   and   P^- > P^0
    #     z* = 0   otherwise.
    # The original implementation instead used sign(mu_hat_τ) — but
    # mu_hat_τ is a stochastic point estimate dominated by instantaneous
    # filter noise (see backend/analysis/iter01_baseline_failure_modes.md
    # §E, recorded during iter02 testing).  Particles already integrate
    # the whole posterior over U(x,t), barrier heights, curvatures, and
    # drift work terms — so P^± are far smoother and more meaningful than
    # sign(mu_hat_τ).  Using P^± as the decision basis is the spec-
    # mandated Bayesian form of the decision output.
    direction = 0
    # ≥ (not >) so a degenerate tie `P^+ = P^0` resolves to "no trade"
    if P_up > P_down and P_up > P_zero:
        direction = +1
    elif P_down > P_up and P_down > P_zero:
        direction = -1
    # Coerce n* to zero if direction is zero; the E_star of an ambiguous
    # trade is undefined for the spec's Kelly formulation and is reported
    # as ‹None› per §6 ("Return None if ℰ* ≤ 0" — we encode None as a
    # non-trade, consistent with the V1 contract).
    if direction == 0 or n_star <= 0:
        return {
            "k_up": float(k_up), "k_down": float(k_down), "k_total": float(k_total),
            "P_up": float(P_up), "P_down": float(P_down), "P_zero": float(P_zero),
            "du_up": float(du_up), "du_down": float(du_down),
            "mu_hat_tau": float(mu_hat_tau), "sigma2_tau": float(sigma2_tau),
            "n_star": 0.0, "direction": 0,
            "E_star": -1e3, "tau": tau,
        }

    # Kelly expected log-wealth increment ℰ* = n*·μ̂_τ - ½·n*²·σ²_τ - fees -
    # slippage - latency cost.
    E_star = direction * n_star * mu_hat_tau \
             - 0.5 * (n_star ** 2) * sigma2_tau \
             - f_fee * n_star \
             - s_0 * n_star \
             - 0.5 * s_1 * (n_star ** 2) \
             - direction * n_star * mu_lat_cost
    if E_star <= 0:
        n_star = 0.0
        direction = 0
    return {
        "k_up": float(k_up), "k_down": float(k_down), "k_total": float(k_total),
        "P_up": float(P_up), "P_down": float(P_down), "P_zero": float(P_zero),
        "du_up": float(du_up), "du_down": float(du_down),
        "mu_hat_tau": float(mu_hat_tau), "sigma2_tau": float(sigma2_tau),
        "n_star": float(n_star), "direction": int(direction),
        "E_star": float(E_star), "tau": tau,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 9.  The Primary Engine  —  `MemecoinStrategyEngine`
# ─────────────────────────────────────────────────────────────────────────────


class MemecoinStrategyEngine:
    """
    Core mathematical engine as specified in `strategyV2.md`.

    Public interface:

        engine = MemecoinStrategyEngine(config)

        engine.update_state(obs)                # ingest one 1-second bucket
        engine.compute_potential_and_barriers() # build U(x, t), find barriers
        decision = engine.get_decision(horizon) # Kelly-optimal decision

    All three methods are designed to run in <2ms combined on a modern CPU
    thanks to numba-compiled UKF, KDE, barrier and ∂² kernels.
    """

    def __init__(self, config: dict):
        self.cfg = _merge_config(config)
        self._cfg_arr = _pack_cfg_kernels(self.cfg)
        self._alpha_regime = float(self.cfg["alpha"])
        self._tau_default = float(self.cfg["tau_max"])

        # ── State ─────────────────────────────────────────────────────
        self._rbpf = RaoBlackwellisedParticleFilter(
            n_particles=int(self.cfg["n_particles"]),
            x0=0.0,
        )
        self.potential = MarketPotential(
            n_grid=int(self.cfg["n_grid"]),
            tw_seconds=float(self.cfg["tw_window_seconds"]),
            lambda_decay=float(self.cfg["lambda_0"]),
        )

        # ── Recursive state holders ───────────────────────────────────
        self._x_prev:       float = 0.0
        self._mu_prev:      float = 0.0
        self._mu_dot:       float = 0.0
        self._bar_count:    int = 0
        # ── iter05: sustained-posterior-decay tracker (early-exit gate). ──
        # On each state update the EMA filter checks the one-step posterior
        # momentum derivative `mu_dot_post`.  We EMA-smooth it (α=0.1) for
        # noise rejection and count consecutive state-updates for which the
        # smoothed derivative is negative (i.e. the posterior drift is
        # rolling toward the down-barrier).  When persist exceeds the
        # `iter05_decay_persist_bars` threshold AND the trade is deeper
        # underwater than `iter05_decay_offside_pct`, exit early — before
        # the kramers P_down ≥ 0.5 flip happens.  Mathematical motivation
        # in RESEARCH_LOG.md (iter05 hypothesis).
        self._mu_dot_post_ema: float = 0.0
        self._mu_dot_post_decline_persist: int = 0
        # iter05 fix: tick-level persistence counter (sp) resets to 0 the
        # instant sma turns positive, so on a catastrophic-coin dump the
        # engine fires BUY exactly on a one-tick upward bounce — passing
        # the entry gate even though the prior 50 ticks were all decaying.
        # The windowed decay prevalence counter measures the *fraction* of
        # the last `iter05_decay_window` ticks for which sma < 0; this is
        # robust to the one-tick bounce that fools the instant `sp`.
        # Maintained as a deque of bool flags from which count is O(1) via
        # rolling sum.
        import collections as _collections
        self._mu_dot_post_sign_window = _collections.deque(maxlen=60)
        self._mu_dot_post_sign_neg_count: int = 0
        self._last_obs:      Optional[Observation] = None
        self._last_state:    dict = {
            "x": 0.0, "mu": 0.0, "h": -4.0, "phi": 0.0, "ell": 0.0,
            "regime": R_IDLE, "regime_dist": np.zeros(7),
        }
        self._last_decision: dict = {}
        self._last_potential: dict = {}
        sigma_t_init = math.exp(0.5 * -4.0)
        self._last_sigma_t:  float = sigma_t_init
        self._last_sigma_phi: float = self.cfg["sigma_phi"]

    # ── Spec method 1: update_state ────────────────────────────────────
    def update_state(self, obs: dict) -> dict:
        """
        Ingest a 1-second bucket of market observations.

        Returns the current estimated latent state and market-relevant
        auxiliary statistics.

        obs keys (per §1 strategyV2.md):
          dt (float)         — time step in seconds (default 1.0)
          log_return (float) — Δx_k
          volume (float)     — v_k (bucket volume)
          signed_delta(float)— δ_k (signed volume imbalance; +BUY -SELL)
          spread (float)     — q_k (quoted spread in log-price units)
          bid_depth (float)  — d^b_k (tokens within range)
          ask_depth (float)  — d^a_k
        """
        o = Observation(
            dt=float(obs.get("dt", 1.0)),
            log_return=float(obs.get("log_return", 0.0)),
            volume=float(obs.get("volume", 0.0)),
            signed_delta=float(obs.get("signed_delta", 0.0)),
            spread=float(obs.get("spread", 0.0)),
            bid_depth=float(obs.get("bid_depth", 0.0)),
            ask_depth=float(obs.get("ask_depth", 0.0)),
        )
        self._last_obs = o
        self._bar_count += 1

        # Pre-computed ratio β · δ_k / (v_k + ε) (spec §2.4 line).
        eps_div = self.cfg["eps_div"]
        delta_ratio = o.signed_delta / (o.volume + eps_div)

        # Measurement variance R (iter02 — failure B fix).
        # Originally a pointwise `max(spread², sigma_floor²)`.  When the
        # latent σ was killed by failure A, R collapsed and Kalman gains
        # saturated — see backend/analysis/iter01_baseline_failure_modes.md.
        # The Adaptive Kalman literature (Mehra 1970) estimates R
        # recursively from the innovation sequence directly.  The RBPF
        # updates `self._rbpf.R_ema` from the previous step's residual²
        # in `step()`.  Here we use the EMA-smoothed value as R_meas and
        # floor only with `σ_floor²` for numerical safety; we also OR-in
        # a tiny `spread²` minimum so that on a one-shot catastrophic bar
        # R isn't under-weighted.
        spread_sq = float(o.spread) * float(o.spread)
        R_meas = max(self._rbpf.R_ema, spread_sq, self.cfg["sigma_floor"] ** 2)

        # Liquidity jump indicator ℓ_k ← 1_{jump}: heuristic on volume surge.
        # A jump is declared if this bucket's volume is ≥3× the rolling
        # average (here rolling avg is approximated by bar_ℓ's exp).
        bar_ell = self._rbpf.bar_ell
        L_t = math.exp(bar_ell) if bar_ell < 15 else math.exp(15)
        vol_ref = max(L_t, 1e-6)
        jump_indicator = 1.0 if o.volume > 3.0 * vol_ref else 0.0

        # ── 1. Run one RBPF step (continuous predict/update) ────────
        state = self._rbpf.step(
            cfg=self.cfg,
            cfg_arr=self._cfg_arr,
            dt=o.dt,
            obs_log_return=o.log_return,
            obs_delta_ratio=self.cfg["beta"] * delta_ratio,
            jump_indicator=jump_indicator,
            R_meas=R_meas,
        )

        # ── 2. Update derived quantities (μ̇_t, σ_t, σ_φ) ─────────────
        self._mu_dot = (state["mu"] - self._mu_prev) / max(o.dt, 1e-6)
        self._mu_prev = state["mu"]
        # σ_t² = exp(h_t)
        sigma_t_sq = math.exp(state["h"]) if state["h"] < 20 else math.exp(20)
        sigma_t = math.sqrt(max(sigma_t_sq, self.cfg["sigma_floor"] ** 2))
        self._last_sigma_t = sigma_t
        # σ_φ approximation: OU stationary std = σ_φ_param / √(2 α)
        self._last_sigma_phi = self.cfg["sigma_phi"] / math.sqrt(2.0 * max(self.cfg["alpha"], 1e-6))

        # ── iter05: EMA-smoothed posterior momentum derivative + sustained-
        # decline counter.  Used by the adapter's leading-decline exit gate.
        # The raw one-step mu_dot_post is too noisy; an EMA on α=0.1 gives a
        # 10-step ~ 1-half-life-of-30-seconds signal that nevertheless
        # precedes the Kramers escape-rate integral by a few seconds.
        mu_dot_post_raw = getattr(self._rbpf, "mu_dot_post", 0.0)
        a_ema = 0.1
        self._mu_dot_post_ema = (1.0 - a_ema) * self._mu_dot_post_ema + a_ema * mu_dot_post_raw
        if self._mu_dot_post_ema < 0.0:
            self._mu_dot_post_decline_persist += 1
        else:
            self._mu_dot_post_decline_persist = 0
        # iter05 fix: sliding-window decay prevalence.  Push the current
        # tick's sign to a maxlen-N deque; maintain a rolling count of
        # negative-sign ticks.  When the deque is full and ≥
        # `iter05_decay_window_thresh` of the last N ticks had sma<0, we
        # consider the trajectory "locked downward" and the entry-side
        # block is eligible to fire.
        sign_neg = self._mu_dot_post_ema < 0.0
        # The deque auto-discards the oldest when at maxlen; we replay
        # the evicted value into the neg_count below.
        if len(self._mu_dot_post_sign_window) == self._mu_dot_post_sign_window.maxlen:
            evicted = self._mu_dot_post_sign_window[0]
            if evicted:
                self._mu_dot_post_sign_neg_count -= 1
        self._mu_dot_post_sign_window.append(sign_neg)
        if sign_neg:
            self._mu_dot_post_sign_neg_count += 1

        L_t_now = math.exp(state["ell"]) if state["ell"] < 15 else math.exp(15)
        self._last_L_t = float(L_t_now)

        # ── 3. Add this bucket to the trade-KDE buffer ─────────────────
        # Convert the log-return to an absolute log-price for storage.
        # We use the running predicted x as the bucket's representative price.
        abs_log_price = state["x"]
        # Only add if the volume is non-trivial (avoid cluttering buffer).
        if o.volume > 0:
            # Clamp bucket time to "now" if obs provided one — we use the bar count.
            now_t = float(self._bar_count)
            self.potential.add_trade(abs_log_price, o.volume, now_t)

        # ── 4. Topological regime derivation ──────────────────────────
        regime = derive_regime_topological(
            mu=state["mu"], mu_dot=self._mu_dot,
            phi=state["phi"], h=state["h"], ell=state["ell"],
            spread=o.spread,
            sigma_t=sigma_t,
            sigma_phi=self._last_sigma_phi,
            alpha=self._alpha_regime,
            tau=self._tau_default,
        )
        # Stash onto regime distribution slot of returned dict.
        state["regime"] = regime
        # Mark canonical mains.
        self._last_state = state
        self._last_obs = o

        return state

    # ── Spec method 2: compute_potential_and_barriers ─────────────────
    def compute_potential_and_barriers(self) -> dict:
        if self._last_state is None:
            return {}
        s = self._last_state
        bid = self._last_obs.bid_depth if self._last_obs else 1e-6
        ask = self._last_obs.ask_depth if self._last_obs else 1e-6
        grid, U, U_up, U_down, T_t, rho = self.potential.compute(
            x_t=float(s["x"]),
            h_t=float(s["h"]),
            bid_depth=max(bid, 1e-6),
            ask_depth=max(ask, 1e-6),
        )
        self._last_potential = {
            "grid": grid,
            "U": U,
            "U_up": U_up,
            "U_down": U_down,
            "T_t": T_t,
            "rho": rho,
            "sigma_t": self._last_sigma_t,
        }
        return self._last_potential

    # ── Spec method 3: get_decision ───────────────────────────────────
    def get_decision(self, horizon: int = 30) -> dict:
        if self._bar_count < int(self.cfg["warmup_seconds"]):
            return {
                "n_star": 0.0, "direction": 0, "E_star": -1e3, "tau": float(horizon),
                "reason": "warmup",
            }
        # Force potential computation if the caller forgot.
        if not self._last_potential:
            self.compute_potential_and_barriers()
        # Sweep horizons to find the one with highest E_star (spec §6
        # doesn't strictly require a sweep, but the horizon `horizon` is
        # returned when called as `get_decision(horizon=τ)`).
        best = None
        tau_step = float(self.cfg["tau_step"])
        taus = np.arange(
            float(self.cfg["tau_min"]),
            min(float(self.cfg["tau_max"]), float(horizon)) + 1.0,
            tau_step,
        )
        if taus.size == 0:
            taus = np.array([float(horizon)], dtype=np.float64)
        for tau in taus:
            d = _kramers_escape_and_decision(
                cfg=self.cfg,
                potential=self.potential,
                state=self._last_state,
                mu_dot=self._mu_dot,
                sigma_t=self._last_sigma_t,
                sigma_phi=self._last_sigma_phi,
                L_t=self._last_L_t,
                tau=float(tau),
            )
            if best is None or d["E_star"] > best["E_star"]:
                best = d
        if best is None:
            return {"n_star": 0.0, "direction": 0, "E_star": -1e3, "tau": float(horizon)}
        # Augment with regime and state for caller convenience.
        best["regime"] = self._last_state.get("regime", R_IDLE)
        best["state"]  = self._last_state
        best["horizon_searched"] = list(map(float, taus))
        return best


# ─────────────────────────────────────────────────────────────────────────────
# 10.  Convenience exports
# ─────────────────────────────────────────────────────────────────────────────

def regime_name(code: int) -> str:
    return _REGIME_NAMES.get(int(code), "unknown")


# ─────────────────────────────────────────────────────────────────────────────
# 11.  V1-compatible Adapter
# ─────────────────────────────────────────────────────────────────────────────
#
# The production pipelines (ForwardTester, Backtester, LiveTrader, main.py)
# speak V1's `update(time, o, h, l, c, ...)` / `{regime, signal, ...}` API.
# `StrategyEngineV2Adapter` exposes that exact surface while internally
# running V2's `MemecoinStrategyEngine`.
#
# The mapping:
#
#   V1 OHLCV bar  →  V2 1-second bucket `obs`  (log_return = ln(c/prev_c), etc.)
#   V2 regime    →  V1 Regime enum  (some V2 codes project onto the same name)
#   V2 decision  (direction, E_star, n_star) →  V1 Signal enum
#
# The adapter also mirrors every attribute the pipeline layers read on the
# engine object (m_hat, ema_fast_val, trend_confidence, etc.).  They are
# recomputed from the V2 latent state so the backtester / ForwardTester /
# LiveTrader code paths can stay byte-for-byte identical to V1.

# Map V2 integer regime codes onto V1 Regime enum members.
_V2_TO_V1_REGIME = {
    R_IDLE:          _V1Regime.IDLE,
    R_CONSOLIDATION: _V1Regime.IDLE,         # V1 has no consolidation regime
    R_TREND:         _V1Regime.TREND,
    R_CONTINUATION:  _V1Regime.CONTINUATION,
    R_EXHAUSTION:    _V1Regime.EXHAUSTION,
    R_TRANSITION:    _V1Regime.EXHAUSTION,   # close V1 analogue
    R_REVERSAL:      _V1Regime.REVERSAL,
}


class StrategyEngineV2Adapter:
    """
    V1-surface wrapper around `MemecoinStrategyEngine`.

    Exposes:
      * `update(time, o, h, l, c, volume, buy_volume, sell_volume,
               _build_full_result)` — exactly like V1 `StrategyEngine.update`
      * `notify_trade_opened(entry_price, direction)`,
        `notify_trade_closed()` — V1 hooks used by all pipelines.
      * All attributes the pipeline layers read directly from the engine
        (m_hat, ema_fast_val, trend_confidence, _pre_entry_stable, ...).

    Implements simple but faithful V1 risk-management semantics:
      * Trailing-stop and hard-stop using `stoploss_pct` (positive = trail,
        negative = hard stop) — V1 contract.
      * Take-profit at `takeprofit_pct`.
      * Discipline-induced exits (e.g. underwater in the first N bars after
        entry) carried over from V1 if configured.
    """

    # ── List of V1 Regime / Signal / Direction members used as enums ──
    # We assign instances of V1 enums so adapter.regime.value matches V1.

    def __init__(self, **engine_kwargs):
        # Pull V1 TPSL params if provided (passed through engine_kwargs).
        self._v1_takeprofit_low  = float(engine_kwargs.pop("takeprofit_pct_low", 30.0))
        self._v1_takeprofit_high = float(engine_kwargs.pop("takeprofit_pct_high", 300.0))
        self._v1_stoploss_low    = float(engine_kwargs.pop("stoploss_pct_low", 12.0))
        self._v1_stoploss_high   = float(engine_kwargs.pop("stoploss_pct_high", 25.0))
        # Hard-stop removed (iter18b): stoploss_pct=0 disables all price-floor
        # exits.  The engine relies exclusively on Bayesian posterior exits
        # (Kramers P_down, reversal regime, gain-retrace, mu_drift_down_exit).
        self.stoploss_pct         = float(engine_kwargs.pop("stoploss_pct", 0.0))
        self.takeprofit_pct       = float(engine_kwargs.pop("takeprofit_pct", 0.0))
        self._warmup_seconds      = int(engine_kwargs.pop("warmup", 30))

        # Forward remaining kwargs into V2 config (the 16 free params +
        # themselves listed in strategyV2.md §2 require N_p, n_grid, etc.).
        # But we should NOT silently pass V1-only kwargs (ema_fast, ...).
        # Strip any V1-only keys silently so the adapter stays runnable
        # across mixed param sets.
        _pass_through_v2_keys = set(DEFAULT_CONFIG.keys())
        v2_cfg = {}
        for k, v in engine_kwargs.items():
            if k in _pass_through_v2_keys:
                v2_cfg[k] = v
        # Re-inject needed control knobs that don't crash V2.
        if self._warmup_seconds:
            v2_cfg.setdefault("warmup_seconds", self._warmup_seconds)

        # Build the V2 core engine.
        self.core = MemecoinStrategyEngine(v2_cfg)
        self.cfg = self.core.cfg

        # Exposed enum instance objects used by the pipeline.
        self.regime: _V1Regime = _V1Regime.IDLE
        self.direction: _V1Direction = _V1Direction.NONE
        self.prev_direction: _V1Direction = _V1Direction.NONE
        self.trend_before_exhaustion: _V1Direction = _V1Direction.NONE

        # ── Position state tracked by V1 hooks ──
        self.in_position = False
        self.entry_price = 0.0
        self._peak_price = 0.0
        self._pool_sol = 0.0   # iter28: latest pool liquidity depth (SOL in curve)
        self._be_armed = False  # iter17 breakeven-scratch arming flag
        self.entry_bar_count = 0
        self.exit_signal_reason = ""
        # iter18a: post-entry EMA-smoothed mu for noise rejection
        # (alpha=0.1, ~10s half-life).  The raw posterior mu oscillates
        # every tick at 1s scale; EMA smoothing is the same noise-
        # rejection technique used in iter05 mu_dot_post_ema.  Used to
        # populate _mu_post_neg_window below (which iter21 hypothesis K
        # reads via _check_exit_v2's kelly_flat guard).
        self._mu_post_ema: float = 0.0
        # iter18a: sliding window fraction tracker (deque of bool flags).
        # Fraction of last N ticks with smoothed mu_t < 0, maintained
        # identically to _mu_dot_post_sign_window in iter05.  Read by the
        # iter21 kelly_flat μ-guard (hypothesis K, default OFF).
        import collections as _col18
        self._mu_post_neg_window: _col18.deque = _col18.deque(maxlen=60)
        self._mu_post_neg_count: int = 0

        # ── "Indicator" attributes the V1 capture reads ──
        self.m_hat = 0.0
        self.prev_m_hat = 0.0
        self.p_hat = 0.0
        self.momentum_acceleration = 0.0
        self.signal_strength = 0.0
        self.s_effective = 0.0
        self.ema_fast_val = None
        self.ema_slow_val = None
        self.ema_macro_val = None
        self.ema_spread = 0.0
        self.prev_ema_spread = 0.0
        self.spread_expanding = False
        self.atr_val = None
        self.atr_floor = 0.0
        self.trend_confidence = 0.0
        self.is_trending = False
        self._ema_cross_valid = False
        self._ema_cross_persist_count = 0
        self._pre_entry_stable = False
        self._pre_entry_stable_up = False
        self._pre_entry_stable_down = False
        self._in_local_chop = False
        self._price_overextended_flag = False
        self._momentum_past_peak_flag = False
        self._momentum_peak_declining_count = 0

        # Trend-anchor + bar-counter attrs read by the capture dict.
        self.bar_count = 0
        self.trend_bar_count = 0
        self.exhaustion_bar_count = 0
        self.exhaustion_persist_count = 0
        self.reversal_confirm_count = 0
        self.trend_reversal_confirm_count = 0
        self.reversal_bar_count = 0
        self.no_motion_count = 0
        self._exhaustion_s_decay_count = 0
        self.trend_start_bar = 0
        self.trend_start_price = 0.0
        self.trend_start_atr = 0.0
        self._exhaustion_phase_high = 0.0

        # Rolling window used to derive ATR / EMA-fast/slow "indicators"
        # that the V1 ForwardTester expects on the engine object.
        self._ema_alpha_fast = 2.0 / (3 + 1)   # EMA(3)
        self._ema_alpha_slow = 2.0 / (7 + 1)   # EMA(7)
        self._ema_alpha_macro = 2.0 / (7 + 1)  # EMA(7) macro
        self._atr_period = 7
        self._prev_close: Optional[float] = None
        self._v1_trend_confidence = 0.0

        # ── V1 config knobs the capture enumerates (cfg_*) ──
        # We echo them onto `eng` so the ForwardTester's _capture_entry_params
        # dictionary doesn't AttributeError.  All defaults, mirroring V1.
        self.confidence_high = float(engine_kwargs.pop("confidence_high", 0.79))
        self.confidence_low = float(engine_kwargs.pop("confidence_low", 0.19))
        self.entry_confidence_high = float(engine_kwargs.pop("entry_confidence_high", 0.79))
        self.entry_confidence_low = float(engine_kwargs.pop("entry_confidence_low", 0.19))
        self.confidence_very_high = float(engine_kwargs.pop("confidence_very_high", 0.86))
        # V2 Bayesian entry floor on P_up — iter17a counterfactual-validated
        # (404-trade logged-batch replay) — 0.62 raised WR & PnL ~7× vs 0.35.
        self._v2_p_up_min = float(engine_kwargs.pop("v2_p_up_min", 0.62))
        # iter17: counterfactual-validated entry/exit overlays (see
        # RESEARCH_LOG.md iter17 — 404-trade logged-batch replay).
        # Defaults below = the iter17a validated config (the
        # `iter17a.json` params file merely re-states them; the default
        # path now reproduces iter17a exactly on a full batch).  All
        # knobs remain overridable via engine_kwargs for sweeps:
        #   v2_sigma_t_min:        entry gate on posterior σ_t (vol floor)
        #   v2_require_past_peak:  entry gate on _momentum_past_peak (iter17b REJECTED — default off)
        #   gain_retrace_arm_pct:  arm profit-lock at +A% peak gain
        #   gain_retrace_give_frac: exit when gain retraces to peak_gain*(1-g)
        #   breakeven_arm_dd_pct:  arm scratch exit after -X% drawdown
        #   breakeven_buffer_pct:  scratch exit level (entry*(1+buf))
        self._v2_sigma_t_min       = float(engine_kwargs.pop("v2_sigma_t_min", 0.021))
        self._v2_require_past_peak = float(engine_kwargs.pop("v2_require_past_peak", 0.0))
        # iter18b_opt optimized exit parameters
        self._gain_retrace_arm_pct  = float(engine_kwargs.pop("gain_retrace_arm_pct", 10.0))
        # iter27: give_frac default 0.4 → 0.5.  Post-exit regret analysis on the
        # 606-recording fresh dataset showed gain_retrace exited far too early
        # (median winner ran a further +48.7% after exit).  Full-batch sweep:
        #   g=0.4 (legacy):  407 trades, 77.6% WR, +0.818 SOL, PF 1.31
        #   g=0.5 (accept):  403 trades, 75.9% WR, +1.077 SOL, PF 1.41  (+31.7% PnL)
        #   g=0.6:           400 trades, 73.5% WR, +1.015 SOL, PF 1.37
        # g=0.5 is the Pareto-optimal give-back: captures runner upside via
        # later kramers/tp_v2 exits while limiting the modest-winner→scratch
        # conversion that erodes win rate at looser trails.
        self._gain_retrace_give_frac = float(engine_kwargs.pop("gain_retrace_give_frac", 0.5))
        self._breakeven_arm_dd_pct  = float(engine_kwargs.pop("breakeven_arm_dd_pct", 25.0))
        self._breakeven_buffer_pct  = float(engine_kwargs.pop("breakeven_buffer_pct", 2.5))
        # iter18a: posterior-drift persistence exit params.
        # mu_exit_neg_thresh: fraction of the last 60 ticks (sliding window)
        #   for which EMA-smoothed mu_t < 0 must exceed before exit is
        #   eligible.  0.75 means 45/60 ticks; conservative enough that
        #   normal intra-trend pullbacks (mu oscillates but averages positive)
        #   do not trigger.  Crashers and slow bleeds maintain mu_EMA < 0
        #   for > 85% of ticks post-entry.
        # mu_exit_persist_bars: legacy alias (iter18a-0) -- deprecated, use neg_thresh.
        # mu_exit_offside_pct: minimum drawdown from entry (%) before the
        #   window exit can fire — guards normal winner pullbacks.
        self._mu_exit_persist_bars  = int(engine_kwargs.pop("mu_exit_persist_bars", 30))
        self._mu_exit_offside_pct   = float(engine_kwargs.pop("mu_exit_offside_pct", 5.0))
        self._mu_exit_neg_thresh    = float(engine_kwargs.pop("mu_exit_neg_thresh", 0.75))
        # iter18b: reversal-regime persistence guard.  Number of consecutive
        # REVERSAL-regime bars required before the reversal_exit fires.
        # Default 2 (optimized in iter18b_opt): requires 2s of sustained
        # REVERSAL posterior majority to distinguish trend reversal from noise.
        self._reversal_exit_bars    = int(engine_kwargs.pop("reversal_exit_bars", 2))
        # ── iter21: sustained-no-long-Kelly exit ("kelly_flat" / exit #7). ──
        # Bayesian decision-theoretic theorem: a long position ceases to be
        # optimal when the engine's own per-tick Kelly utility for "stay long"
        # goes non-positive.  Entry gate is `direction == +1 AND E_star > 0`;
        # the exit-side mirror is "engine says not-long for C_K consecutive
        # ticks AND the trade is offside".  Static-mask analysis
        # (backend/analysis/iter20_static_mask_v2.py) on iter19 rec-data:
        #   K=80 ticks, offside=-20%: losers saved +0.385 / winners lost
        #   -0.120 ⇒ net +0.265 over 36-recording cohort.
        # Offside guard prevents chopping trades near breakeven that then
        # recover into winners (winners chop at ≥+3% in the no-guard mask).
        # _no_long_exit_bars counts consecutive ticks where direction != +1
        # AND E_star <= 0 (engine wants flat).  PRODUCTION DEFAULTS (iter21
        # acceptance vs iter16_baseline_full): no_long_exit_bars=60,
        # no_long_offside_pct=40 (full batch: 259 trades, 77.2% WR, +0.884
        # SOL, PF 1.55, exp +0.00341 — all 5 paired-diff gates cleared vs
        # iter16_baseline_full; +62% PnL vs iter19_clean). The K=60 + offs=40
        # tuning minimises W→L regressions (4) at the cost of catching fewer
        # losers than K=20 — this is the optimal trade per static-mask
        # analysis (winner cut at offs=-40% drops to ~1 from ~10).
        # _no_long_offside_pct: the trade must be at least X% underwater
        # before this exit can fire (guards against the natural consolidation
        # E_long-neg phases that healthy winners pass through).
        self._no_long_exit_bars     = int(engine_kwargs.pop("no_long_exit_bars", 60))
        self._no_long_offside_pct   = float(engine_kwargs.pop("no_long_offside_pct", 40.0))
        # iter21 hypothesis K (μ-persistence guard): require that
        # fraction of trailing 60 ticks with negative EMA-smoothed μ_t is
        # at least this high.  REJECTED at full batch (iter21k_mu75 vs
        # iter21_k60_offs40: Δ=-0.072 SOL, 3/14 breadth) — recoverable
        # pullbacks and continuation slides are NOT statistically
        # distinguishable from OU drift (rec70 W→L regression had
        # mu_neg_frac=1.00, highest possible).  Default 0.0 = OFF
        # (preserves iter21_k60_offs40 production parity).
        # 0.75 means 75% of last 60 ticks had EMA-mu < 0.
        self._no_long_mu_neg_frac   = float(engine_kwargs.pop("no_long_mu_neg_frac", 0.0))
        self._no_long_streak        = 0
        self._entry_E_star = 0.0
        self.confidence_w1 = 0.3
        self.confidence_w2 = 0.25
        self.confidence_w3 = 0.25
        self.confidence_w4 = 0.2
        self.ema_fast_p = 3
        self.ema_slow_p = 7
        self.atr_period = 7
        self.roc_period = 3
        self.warmup = self._warmup_seconds
        self.S_strong = 4.0
        self.S_weak = 2.0
        self.S_noise = 1.15
        self.exhaustion_bars_limit = 3
        self.delta_threshold = 0.3
        self.min_trend_bars = 2
        self.reversal_confirm_bars = 2
        self.chop_atr_pct = 0.3
        self.chop_spread_pct = 0.05
        self.reversal_exit_confirm_bars = 0
        self.s_effective_threshold = 0.5
        self.exhaustion_persist_bars = 6
        self.regime_lookback = 6
        self.persistence_threshold = 2
        self.momentum_mean_threshold = 0.0
        self.ema_min_spread_pct = 0.02
        self.atr_floor_k = 0.0
        self.ema_cross_persist_bars = 2
        self.exhaustion_s_decay_bars = 1
        self.exhaustion_stall_bars = 6
        self.exhaustion_stall_atr_pct = 3.0
        self.local_range_bars = 80
        self.local_range_threshold_pct = 10.0
        self.sign_flip_threshold = 0
        self.stability_bars = 5
        self.spike_atr_multiplier = 1.2
        self.spike_lookback_bars = 9
        self.body_baseline_bars = 160
        self.overextension_k = 0.08
        self.momentum_peak_bars = 1
        self.consolidation_range_pct = 25.0
        self.ema_macro_period = 7
        self.takeprofit_pct_low = self._v1_takeprofit_low
        self.takeprofit_pct_high = self._v1_takeprofit_high
        self.stoploss_pct_low = self._v1_stoploss_low
        self.stoploss_pct_high = self._v1_stoploss_high
        # V1-only adapter-visible attrs (queried by ForwardTester's
        # _capture_entry_params() but not used by the V2 core).
        self.max_entry_bar_count   = int(engine_kwargs.pop("max_entry_bar_count", 5700))
        self.forbidden_bc_lo       = int(engine_kwargs.pop("forbidden_bc_lo", 2000))
        self.forbidden_bc_hi       = int(engine_kwargs.pop("forbidden_bc_hi", 3000))
        self.trail_floor_pct       = float(engine_kwargs.pop("trail_floor_pct", 13.0))
        self.reversal_exit_bars_max = int(engine_kwargs.pop("reversal_exit_bars_max", 20))
        # ── iter05 leading-decline exit knobs (configurable for sweeping). ──
        # `iter05_decay_persist_bars`: how many consecutive state-updates
        #   with EMA-smoothed posterior μ̇ < 0 must accrue before the
        #   leading-decline exit is eligible (default 6 — about 1.5s of
        #   4-state-expanded candles).  Too low → noise exit; too high →
        #   misses the leading signal.
        # `iter05_decay_offside_pct`: minimum underwater % vs entry for
        #   the exit to fire (default 8.0).  A winning pullback rarely
        #   exceeds 6%, and the catastrophic iter04 losses all occurred
        #   with double-digit intra-trade downside.
        self.iter05_decay_persist_bars = int(engine_kwargs.pop("iter05_decay_persist_bars", 6))
        self.iter05_decay_offside_pct  = float(engine_kwargs.pop("iter05_decay_offside_pct", 8.0))
        # `iter05_decay_window` (default 60): sliding-window size for the
        # decay prevalence counter — the(rbish)rolling-fraction signal.
        # 60 ticks ≈ 15 candles under 4-state expansion.  Empirically the
        # catastrophic-coin dumps (BREAD, WEN, CHAIRSEM) lose -40% to -70%
        # within ~10-15 minutes of state-time, so a 60-tick window captures
        # the prolonged decay preceding a re-entry purchase.
        self.iter05_decay_window       = int(engine_kwargs.pop("iter05_decay_window", 60))
        # `iter05_decay_window_thresh` (default 0.8): fraction of the
        # window for which sma must be negative to declare the trajectory
        # "locked downward" and gate new entries.
        self.iter05_decay_window_thresh = float(engine_kwargs.pop("iter05_decay_window_thresh", 0.8))
        # `iter05_decay_exit_enable` (default 0.0 = OFF): toggle the
        # leading_decay EXIT.  Smoke-test 2026-07-21 showed the exit
        # alone causes net-negative re-entry churn on KISUNLAF5 rec1194
        # (10 trades / 50% WR / -0.014 SOL vs iter04 5 trades / 80% WR /
        # +0.041 SOL) because it fires AT 8% offside — late relative to
        # the kramers_down exit (~-26%) — so the entry churn it
        # triggers more than offsets its loss-saving.
        self.iter05_decay_exit_enable  = float(engine_kwargs.pop("iter05_decay_exit_enable", 0.0))
        # `iter05_decay_entry_block` (default 1.0 = ON): toggle the
        # entry-side decay BLOCK.  When ≥ `iter05_decay_window_thresh`
        # of the last `iter05_decay_window` state-updates had sma<0,
        # refuse new BUY entries.  Mathematical motivation: a sustained
        # negative μ̇_post EMA means the SDE's deterministic drift
        # -λ_μ μ_t + κ_μ (φ_t - φ̄_t) is integrated negative over the
        # KDE memory window — the basin geometry is actively drifting
        # downward regardless of the posterior-direction (P_up > P_down)
        # derived from the barrier topology, which is a lagging
        # observable.  Empirical evidence: 18 of the 30 worst iter04-full
        # losses are follow-up trades that immediately re-enter after a
        # `kramers_down_exit` on a token whose μ̇ has been continuously
        # declining.
        self.iter05_decay_entry_block  = float(engine_kwargs.pop("iter05_decay_entry_block", 1.0))
        # `iter05_s_effective_min` (default 0.0 = OFF): minimum
        # `s_effective` required for the entry gate to admit a new BUY.
        # `s_effective` is the V2 barrier-ANCHORED signal-strength proxy;
        # analysis of iter04-full (RESEARCH_LOG iter05) shows monotone
        # quintiles of `s_effective` map to WR 70%/80%/80%/81%/87% —
        # filtering s ≥ 1e8 keeps 1904 of 2547 trades and lifts
        # expectancy from +0.00730 SOL/trade (baseline) to +0.00866
        # (+18.6% gain) while removing 1 of 30 catastrophic worst-trades.
        # The remaining 14 worst-trade removals at s ≥ 5e8 cost too
        # much profit (cuts to +10.56 SOL).  Mathematical interpretation:
        # higher s_effective ⇒ either stronger m_hat/ATR momentum or
        # closer to U(x) barrier — both correspond to high-confidence
        # Kramers-escape foundations per spec §3.6.
        self.iter05_s_effective_min   = float(engine_kwargs.pop("iter05_s_effective_min", 0.0))
        # V2 only — used by ForwardTester when it generates the cfg dict
        # via the V1 attr names; defaults = spec defaults
        self.lambda_mu = self.cfg["lambda_mu"]
        self.kappa_mu = self.cfg["kappa_mu"]
        self.sigma_mu = self.cfg["sigma_mu"]
        self.eta = self.cfg["eta"]
        self.sigma_h = self.cfg["sigma_h"]
        self.alpha = self.cfg["alpha"]
        self.beta = self.cfg["beta"]
        self.sigma_phi = self.cfg["sigma_phi"]
        self.theta = self.cfg["theta"]
        self.sigma_ell = self.cfg["sigma_ell"]
        self.zeta = self.cfg["zeta"]
        self.lambda_0 = self.cfg["lambda_0"]
        self.lambda_1 = self.cfg["lambda_1"]
        self.kappa_J = self.cfg["kappa_J"]
        self.s_0 = self.cfg["s_0"]
        self.s_1 = self.cfg["s_1"]
        self._n_particles = int(self.cfg["n_particles"])
        self._n_grid = int(self.cfg["n_grid"])

        # ── Holder-flow knobs (iter36: dev/insider sell detection) ────────
        # `v2_holder_flow_entry_block` (default 0.0 = OFF as of iter38): when
        #   > 0, block BUY entries if a dev/insider sell (amount_usd ≥ threshold)
        #   occurred within `v2_holder_flow_entry_window_seconds` before the bar.
        # `v2_holder_flow_exit_enable` (default 0.0 = OFF as of iter38): when
        #   > 0, fire an immediate `dev_sell_exit` if a dev/insider sell occurs
        #   while in position (checked on every bar close).
        # `v2_holder_flow_min_usd` (default 100.0): minimum sell amount (USD)
        #   to consider significant — filters dust.
        # `v2_holder_flow_require_tag` (default 1.0 = ON): when > 0, only events
        #   with a non-empty provenance `tag` (dev/sniper/bundler/rat_trader)
        #   count as a "dev sell".  When 0, any large sell (tag or not) counts
        #   — the iter36/38 legacy behaviour.  Set to 1.0 once the wallet
        #   registry is reliably populated so the gate tracks true insiders.
        # NOTE (iter38): the entry gate and exit trigger are DEFAULT-OFF while
        #   the holder-flow data-collection fixes (registry population + tag
        #   enrichment) are validated on a fresh night of recordings.  The
        #   monitor still captures and persists every event regardless of these
        #   knobs — they only gate the engine's trading response.  When no
        #   holder_flow events are loaded, `_has_recent_dev_sell` always returns
        #   False, so behaviour is byte-identical to the pre-iter36 engine.
        self._v2_holder_flow_entry_block = float(engine_kwargs.pop("v2_holder_flow_entry_block", 1.0))
        self._v2_holder_flow_exit_enable = float(engine_kwargs.pop("v2_holder_flow_exit_enable", 1.0))
        self._v2_holder_flow_require_tag = float(engine_kwargs.pop("v2_holder_flow_require_tag", 0.0))
        self._v2_holder_flow_min_usd     = float(engine_kwargs.pop("v2_holder_flow_min_usd", 100.0))
        self._v2_holder_flow_entry_window_seconds = int(engine_kwargs.pop("v2_holder_flow_entry_window_seconds", 30))
        self._v2_holder_flow_exit_window_seconds  = int(engine_kwargs.pop("v2_holder_flow_exit_window_seconds", 15))
        # ── Market-cap bound trade block ────────────────────────────────────
        # Block all BUY entries when the USD market cap is below mcap_low_usd
        # or above mcap_high_usd.  Both default to 0 = deactivated.
        self._mcap_low_usd  = float(engine_kwargs.pop("mcap_low_usd", 0.0))
        self._mcap_high_usd = float(engine_kwargs.pop("mcap_high_usd", 0.0))
        self._market_cap_usd: float = 0.0
        # Holder-flow events: list of dicts with keys time, wallet, tag, side,
        # amount_usd, amount_sol, tx_hash.  Passed in by the pipeline layer
        # (ForwardTester / LiveTrader) that loaded them from the recording DB.
        self._holder_flow_events: list[dict] = []
        self._holder_flow_index: dict[int, list[dict]] = {}

    # ── Holder-flow helpers ───────────────────────────────────────────────

    def set_holder_flow_events(self, events: list[dict]):
        """Load holder-flow events for this recording (called by pipeline)."""
        self._holder_flow_events = events
        self._holder_flow_index = {}
        for event in events:
            t = int(event.get("time", 0))
            if t not in self._holder_flow_index:
                self._holder_flow_index[t] = []
            self._holder_flow_index[t].append(event)

    def append_holder_flow_events(self, events: list[dict]):
        """Incrementally append new events (live path) without rebuilding index."""
        for event in events:
            self._holder_flow_events.append(event)
            t = int(event.get("time", 0))
            if t not in self._holder_flow_index:
                self._holder_flow_index[t] = []
            self._holder_flow_index[t].append(event)

    # Verified dev/insider provenance tags (must match holder_flow._TRACKED_TAGS).
    # The iter38 "whale" fallback tag is deliberately EXCLUDED — it marks a large
    # sell with no verified insider provenance, so it must not satisfy a
    # require_tag gate.
    _DEV_TAGS = frozenset({"dev", "sniper", "bundler", "rat_trader"})

    def _is_dev_sell(self, event: dict, min_usd: float) -> bool:
        """True if an event qualifies as a significant dev/insider sell.

        When `v2_holder_flow_require_tag` > 0 the event must carry a verified
        provenance tag in `_DEV_TAGS` (dev/sniper/bundler/rat_trader); the
        iter38 "whale" fallback and untagged events do NOT qualify.  When
        `require_tag` is 0, any large sell qualifies (legacy iter36/38
        behaviour — the "big-seller circuit breaker").
        """
        if event.get("side") != "sell":
            return False
        if event.get("amount_usd", 0) < min_usd:
            return False
        if self._v2_holder_flow_require_tag > 0.0:
            return event.get("tag", "") in self._DEV_TAGS
        return True

    def _has_recent_dev_sell(self, time: int, window_seconds: int, min_usd: float) -> bool:
        """Check if a significant dev/insider sell occurred within the window before the given time."""
        if not self._holder_flow_index:
            return False
        cutoff = time - window_seconds
        for t in range(cutoff, time + 1):
            for event in self._holder_flow_index.get(t, []):
                if self._is_dev_sell(event, min_usd):
                    return True
        return False

    def _get_recent_dev_sell(self, time: int, window_seconds: int) -> Optional[dict]:
        """Get the most recent dev/insider sell event within the window before the given time."""
        if not self._holder_flow_index:
            return None
        cutoff = time - window_seconds
        for t in range(time, cutoff - 1, -1):
            for event in self._holder_flow_index.get(t, []):
                if self._is_dev_sell(event, self._v2_holder_flow_min_usd):
                    return event
        return None

    # ── V1 surface ────────────────────────────────────────────────────
    def _passes_engine_version_check(self):
        # Pass through.  No-op standalone — the symbol is there in case
        # `main.py` introspects it.
        return 2

    def notify_trade_opened(self, entry_price: float, direction: _V1Direction):
        self.in_position = True
        self.entry_price = float(entry_price)
        self.position_direction = direction
        self._peak_price = float(entry_price)
        self.entry_bar_count = self.bar_count
        self._be_armed = False  # iter17 breakeven-scratch arming flag
        # Seed EMA from the current posterior mu so the window starts calibrated.
        _cur_mu = getattr(self, '_mu_post_ema', 0.0)  # keep current EMA value
        self._mu_post_neg_window.clear()
        self._mu_post_neg_count = 0

    def notify_trade_closed(self):
        self.in_position = False
        self.entry_price = 0.0
        self.position_direction = _V1Direction.NONE
        self._peak_price = 0.0
        self._be_armed = False
        self._mu_post_neg_window.clear()
        self._mu_post_neg_count = 0
        self._no_long_streak = 0  # iter21: reset on close

    def _update_peak_price(self, h: float, l: float):
        if not self.in_position:
            return
        if self.position_direction == _V1Direction.UP:
            if h > self._peak_price:
                self._peak_price = h
        elif self.position_direction == _V1Direction.DOWN:
            if l < self._peak_price or self._peak_price == 0.0:
                self._peak_price = l

    def _price_overextended(self, c: float) -> bool:
        if not self.p_hat:
            return False
        return c > self.p_hat * (1.0 + self.overextension_k)

    def _momentum_past_peak(self, c=None) -> bool:
        return self._momentum_past_peak_flag

    def _is_chop_zone(self, c: float) -> bool:
        return self._in_local_chop if self.atr_val else False

    # --- V1 stoploss / takeprofit support (confidence-scaled) ---
    def _confidence_lerp(self, low_val: float, high_val: float) -> float:
        # Same formula as V1: linear interp with current trend_confidence.
        lo = low_val if low_val != 0.0 else high_val
        hi = high_val if high_val != 0.0 else low_val
        if lo == hi:
            return lo
        t = min(max(self.trend_confidence, 0.0), 1.0)
        # Map [confidence_low, confidence_high] → [lo, hi].
        if self.confidence_high > self.confidence_low:
            frac = (t - self.confidence_low) / (self.confidence_high - self.confidence_low)
        else:
            frac = 0.0
        frac = max(0.0, min(1.0, frac))
        return low_val + (high_val - low_val) * frac

    def _effective_stoploss_pct(self) -> float:
        if self.stoploss_pct_low != 0.0 or self.stoploss_pct_high != 0.0:
            is_hard = (self.stoploss_pct_low < 0) or (self.stoploss_pct_high < 0)
            sign = -1.0 if is_hard else 1.0
            lo = abs(self.stoploss_pct_low) if self.stoploss_pct_low != 0.0 else abs(self.stoploss_pct)
            hi = abs(self.stoploss_pct_high) if self.stoploss_pct_high != 0.0 else abs(self.stoploss_pct)
            mag = self._confidence_lerp(lo, hi)
            return sign * mag
        return 0.0

    def _effective_takeprofit_pct(self) -> float:
        if self.takeprofit_pct_low > 0.0 or self.takeprofit_pct_high > 0.0:
            lo = self.takeprofit_pct_low if self.takeprofit_pct_low > 0.0 else self.takeprofit_pct
            hi = self.takeprofit_pct_high if self.takeprofit_pct_high > 0.0 else 100.0
            return self._confidence_lerp(lo, hi)
        return self.takeprofit_pct

    def _global_stoploss_pct(self) -> float:
        return self.stoploss_pct

    # ── Intra-bar indicator maintenance (cheap, mirrors V1 closely) ────
    def _maintain_v1_indicators(self, o: float, h: float, l: float, c: float,
                                 volume: float):
        """Update EMAs / ATR / m_hat / p_hat / pseudo-confidence.

        These mirror V1 closely enough for the chart + capture dict to
        display meaningful values.  Crucially, the *regime* and *signal*
        are produced by the V2 core (MemecoinStrategyEngine); this routine
        only fills in the auxiliary indicator attributes the pipeline reads.
        """
        if self._prev_close is None:
            # Spark EMAs from the close directly.
            self.ema_fast_val = c
            self.ema_slow_val = c
            self.ema_macro_val = c
            tr = h - l
            self.atr_val = tr
            tr = 0.0 if not tr else tr
        else:
            # EMAs
            self.ema_fast_val = self.ema_fast_val + self._ema_alpha_fast * (c - self.ema_fast_val)
            self.ema_slow_val = self.ema_slow_val + self._ema_alpha_slow * (c - self.ema_slow_val)
            if self.ema_macro_period > 0:
                self.ema_macro_val = self.ema_macro_val + self._ema_alpha_macro * (c - self.ema_macro_val)
            # True range
            prev_c = self._prev_close
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            atr_alpha = 2.0 / (self.atr_period + 1)
            self.atr_val = self.atr_val + atr_alpha * (tr - self.atr_val)
            # ATR floor = rolling median (cheap approx).
            self.atr_floor = self.atr_val

        self._prev_close = c

        self.prev_ema_spread = self.ema_spread
        self.ema_spread = self.ema_fast_val - self.ema_slow_val
        self.spread_expanding = abs(self.ema_spread) > abs(self.prev_ema_spread)

        # m_hat / p_hat / accel  → use V2 latent posterior.
        st = self.core._last_state
        self.prev_m_hat = self.m_hat
        # Scale the drift μ_t to a per-second "log return" m_hat.
        # We multiply by 100 so S has sensible scale vs ATR_pct (V1 parity).
        self.m_hat = float(st["mu"]) * 200.0
        if c > 0:
            self.p_hat = c
        self.momentum_acceleration = self.m_hat - self.prev_m_hat
        if abs(self.m_hat) < abs(self.prev_m_hat):
            self._momentum_peak_declining_count += 1
        else:
            self._momentum_peak_declining_count = 0
        self._momentum_past_peak_flag = self._momentum_peak_declining_count >= self.momentum_peak_bars
        self._price_overextended_flag = self._price_overextended(c)

        # Signal strength S (V1 contract: |m_hat_pct|/ATR_pct)
        if self.atr_val and self.atr_val > 0 and c > 0:
            m_hat_pct = (self.m_hat / c) * 100 * self.roc_period
            atr_pct   = (self.atr_val / c) * 100
            self.signal_strength = (abs(m_hat_pct) / atr_pct) if atr_pct > 0 else 0.0
        else:
            self.signal_strength = 0.0
        self.s_effective = self.signal_strength   # V2 already accounts for barriers

        # ── Fake confidence from the V2 regime distribution entropy ────
        # Higher posterior certainty (lower entropy) → higher confidence.
        # We scale so confidence ∈ [0, 1] for the V1 gate / TP-SL lerp.
        regime_dist = st.get("regime_dist", None)
        if regime_dist is not None:
            ps = np.asarray(regime_dist, dtype=np.float64)
            ps = ps[ps > 0]
            if ps.size > 0:
                H = float(-(ps * np.log(ps)).sum())
                # Normalise: H_max for 7 regimes = log(7) ≈ 1.946
                H_max = math.log(7)
                confidence_from_entropy = 1.0 - (H / H_max if H_max > 0 else 1.0)
                # Combine with V2 μ magnitude relative to noise floor
                sigma_t = self.core._last_sigma_t
                mu_star = sigma_t / math.sqrt(max(self.core.cfg["tau_max"], 1.0)) if sigma_t > 0 else 1e-6
                if mu_star > 0:
                    mu_conf = min(abs(float(st["mu"])) / (5.0 * mu_star), 1.0)
                else:
                    mu_conf = 0.0
                self.trend_confidence = max(0.0, min(1.0,
                    0.6 * confidence_from_entropy + 0.4 * mu_conf))
                self._v1_trend_confidence = self.trend_confidence
            else:
                self.trend_confidence = 0.0
        else:
            self.trend_confidence = self._v1_trend_confidence

        self.is_trending = float(self.trend_confidence) >= self.confidence_high

        # EMA cross validation (cheap approx)
        if c > 0:
            spread_mag_pct = abs(self.ema_spread) / c * 100 if c > 0 else 0.0
            self._ema_cross_valid = (spread_mag_pct > self.ema_min_spread_pct) and self.is_trending
            if self._ema_cross_valid:
                self._ema_cross_persist_count += 1
            else:
                self._ema_cross_persist_count = 0

        # Pre-entry stability (long-only): m_hat has same sign as the regime confidence ≥ 70%
        if self.trend_confidence >= self.confidence_very_high:
            self._pre_entry_stable_up = self.m_hat > 0
        else:
            self._pre_entry_stable_up = bool(self._pre_entry_stable and self.m_hat > 0)
        self._pre_entry_stable = self._pre_entry_stable_up
        self._in_local_chop = False   # V2's internal chop handling covers this

    # ── Main entry point ────────────────────────────────────────────
    def update(
        self,
        time: int,
        o: float, h: float, l: float, c: float,
        volume: float = 0.0,
        buy_volume: float = 0.0,
        sell_volume: float = 0.0,
        pool_sol: float = 0.0,
        market_cap_usd: float = 0.0,
        _build_full_result: bool = True,
    ) -> dict:
        self.bar_count += 1
        self._current_time = int(time)  # iter36: store for holder-flow checks
        # iter28: real pool liquidity depth (SOL in bonding curve), 0.0 when the
        # feed does not carry reserves.  Tracks the latest non-zero observation.
        if pool_sol > 0.0:
            self._pool_sol = pool_sol
        if market_cap_usd > 0.0:
            self._market_cap_usd = market_cap_usd

        # Snapshot the previous close BEFORE `_maintain_v1_indicators`
        # overwrites it with the current bar's close — otherwise the V2
        # log_return measurement below degenerates to ln(c/c) = 0 every
        # bar and the filter never registers any price movement.
        _prev_close_for_v2 = self._prev_close

        # Maintain V1 indicator attrs in lockstep with the OHLCV.
        self._maintain_v1_indicators(o, h, l, c, volume)

        # ── Convert V1 OHLCV → V2 obs bucket ──────────────────────────
        sigma_t = self.core._last_sigma_t
        log_return = 0.0
        if _prev_close_for_v2 and _prev_close_for_v2 > 0:
            log_return = math.log(c / _prev_close_for_v2) if c > 0 else 0.0
        spread = abs(math.log(o / c)) if o > 0 and c > 0 else 1e-3
        # signed_delta from V1 buy/sell split (V1-buy pressure positive).
        signed_delta = float(buy_volume - sell_volume)
        # Bid / ask depth: proxy via 1.0e3 each (we don't have L2 data in
        # the V1 surface).
        bid_depth = max(float(buy_volume + 1.0), 1.0)
        ask_depth = max(float(sell_volume + 1.0), 1.0)

        obs = {
            "dt": 1.0,
            "log_return": float(log_return),
            "volume": float(volume),
            "signed_delta": signed_delta,
            "spread": spread,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
        }
        state = self.core.update_state(obs)

        # ── Compute the V2 market potential lazily (every full bar) ────
        if _build_full_result and getattr(self.core, "_last_potential", None) or buy_volume + sell_volume > 0:
            self.core.compute_potential_and_barriers()

        # ── Map V2 regime → V1 Regime instance ────────────────────────
        v2_regime = int(state["regime"])
        v1_regime = _V2_TO_V1_REGIME.get(v2_regime, _V1Regime.IDLE)
        self.prev_direction = self.direction
        # Update trend_anchor / counter bookkeeping similar to V1
        if v1_regime == _V1Regime.TREND:
            if self.regime != _V1Regime.TREND:
                self.trend_bar_count = 0
                self.trend_start_price = c
                self.trend_start_atr = self.atr_val or 0.0
                self.trend_start_bar = self.bar_count
            self.trend_bar_count += 1
        else:
            self.trend_bar_count = 0
        if v1_regime == _V1Regime.REVERSAL:
            self.reversal_bar_count += 1
        elif v1_regime != _V1Regime.REVERSAL:
            self.reversal_bar_count = 0
        if self.regime == _V1Regime.EXHAUSTION and v1_regime == _V1Regime.EXHAUSTION:
            self.exhaustion_bar_count += 1
        if v1_regime == _V1Regime.EXHAUSTION:
            if c > self._exhaustion_phase_high:
                self._exhaustion_phase_high = c
        else:
            self._exhaustion_phase_high = 0.0

        self.regime = v1_regime

        # Determine V1 direction from V2 μ.
        if float(state["mu"]) > 0:
            self.direction = _V1Direction.UP
        elif float(state["mu"]) < 0:
            self.direction = _V1Direction.DOWN
        else:
            self.direction = _V1Direction.NONE

        # ── Compute the V2 trading decision → V1 Signal ────────────
        # (iter16i: horizon wired to cfg["tau_max"] so the existing
        # tau_min/tau_max/tau_step parameters actually control the sweep —
        # previously hardcoded to 30 engine-s = 7.5 physical s, 4× below
        # the spec's own 5–30 s horizon range at the 4-state cadence.)
        decision = self.core.get_decision(horizon=int(self.core.cfg.get("tau_max", 30)))
        v1_signal: _V1Signal = _V1Signal.NONE

        # iter17: parity-safe logging stash — the latest V2 decision snapshot,
        # consumed by ForwardTester._capture_entry_params for per-trade entry
        # feature logging (counterfactual analysis).  Write-only; no effect on
        # signal/exit logic.  Reflects the most recent update() call, so at a
        # fill (state 1 of the entry bar) it holds the fill-bar decision —
        # consistent with the rest of the entry_params snapshot semantics.
        self._v2_last_decision = decision

        # iter21: update the "no-long-Kelly-persistence" streak counter.
        # When `direction != +1 AND E_star <= 0` (the engine either wants
        # flat or wants short with no Kelly-positive counter-trade), the
        # current long position has no Bayesian justification to be held.
        # Reset on any long-viable tick.  Used in _check_exit_v2.
        _dec_dir = int(decision.get("direction", 0) or 0)
        _dec_E = float(decision.get("E_star", -1e3) or -1e3)
        if _dec_dir == 1 and _dec_E > 0:
            self._no_long_streak = 0
        else:
            self._no_long_streak += 1

        # iter18a: update the post-entry posterior-drift persistence counter.
        # mu_t is the UKF posterior latent drift (OU process); when it is
        # persistently negative, the OU model expects continued downward
        # price movement: E[x_{t+s}|x_t,mu_t] ≈ x_t + mu_t/lambda_mu*(1-e^{-λs}).
        # Use an EMA-smoothed mu (α=0.1, ~10-tick half-life) to reject
        # tick-level OU oscillation, then track the windowed fraction of
        # negative-EMA-mu ticks (identical bookkeeping to iter05's
        # _mu_dot_post_sign_window).  Updated always; used only in
        # _check_exit_v2 when in position (the gate is offside-guarded).
        _mu_raw = float(state["mu"])
        _a18 = 0.1  # EMA alpha for mu smoothing
        self._mu_post_ema = (1.0 - _a18) * self._mu_post_ema + _a18 * _mu_raw
        _mu_sign_neg = self._mu_post_ema < 0.0
        if len(self._mu_post_neg_window) == self._mu_post_neg_window.maxlen:
            _evicted = self._mu_post_neg_window[0]
            if _evicted:
                self._mu_post_neg_count -= 1
        self._mu_post_neg_window.append(_mu_sign_neg)
        if _mu_sign_neg:
            self._mu_post_neg_count += 1

        if self.bar_count <= max(self.warmup, 60):
            v1_signal = _V1Signal.NONE
            self.exit_signal_reason = ""
        elif self.in_position:
            # When in position, check Bayesian exits (defined in
            # `_check_exit_v2`).  iter04 consolidated all exit logic
            # there; no additional flip handling is needed here.
            exit_signal = self._check_exit_v2(c, l=l, h=h, decision=decision)
            if exit_signal is not None:
                v1_signal = exit_signal
        else:
            # Open new positions based on V2 decision.
            if decision.get("direction") == 1 and decision.get("E_star", -1.0) > 0:
                # ── Market-cap bound trade block ───────────────────────────────
                # Block all BUY entries when the USD market cap is below
                # mcap_low_usd or above mcap_high_usd.  Both default to 0
                # = deactivated.
                _mcap = self._market_cap_usd
                _mcap_blocked = False
                if _mcap > 0.0:
                    if self._mcap_low_usd > 0.0 and _mcap < self._mcap_low_usd:
                        _mcap_blocked = True
                    if self._mcap_high_usd > 0.0 and _mcap > self._mcap_high_usd:
                        _mcap_blocked = True
                if _mcap_blocked:
                    self.exit_signal_reason = "mcap_bound_block"
                # ── Holder-flow entry gate (iter36): block entry on recent dev sell ──
                elif self._v2_holder_flow_entry_block > 0.0 and self._has_recent_dev_sell(
                    getattr(self, "_current_time", 0), self._v2_holder_flow_entry_window_seconds, self._v2_holder_flow_min_usd
                ):
                    sell_event = self._get_recent_dev_sell(getattr(self, "_current_time", 0), self._v2_holder_flow_entry_window_seconds)
                    # Suppress the BUY signal — log for debugging
                    self.exit_signal_reason = f"holder_flow_block:{sell_event.get('wallet','')[:8] if sell_event else 'unknown'}"
                # Apply V1-style entry gate (confidence).
                elif self._v2_passes_entry_gate(c, decision):
                    v1_signal = _V1Signal.BUY
                    self._entry_E_star = float(decision.get("E_star", 0.0))
                    self.exit_signal_reason = ""
                    self._no_long_streak = 0

        # ── Update peak price AFTER exit checks (matches V1) ────────
        if self.in_position and v1_signal != _V1Signal.EXIT:
            self._update_peak_price(h, l)

        # ── Build the result dict ───────────────────────────────────
        minimal = {
            "time": time,
            "regime": self.regime.value,
            "direction": self.direction.value,
            "signal": v1_signal.value,
            "exit_reason": self.exit_signal_reason,
        }
        if not _build_full_result:
            return minimal

        full = dict(minimal)
        full["indicators"] = {
            "ema_fast": self.ema_fast_val,
            "ema_slow": self.ema_slow_val,
            "ema_macro": self.ema_macro_val,
            "atr": self.atr_val,
            "atr_floor": self.atr_floor,
            "roc": self.m_hat,
            "m_hat": self.m_hat,
            "p_hat": self.p_hat,
            "signal_strength": self.signal_strength,
            "momentum_acceleration": self.momentum_acceleration,
            "s_effective": self.s_effective,
            "ema_spread": self.ema_spread,
            "spread_expanding": self.spread_expanding,
            "trend_confidence": self.trend_confidence,
            "is_trending": self.is_trending,
            "ema_cross_valid": self._ema_cross_valid,
            "pre_entry_stable": self._pre_entry_stable,
            "in_local_chop": self._in_local_chop,
            # V2-only indicators (display insight in the dashboard)
            "v2_mu": float(state["mu"]),
            "v2_phi": float(state["phi"]),
            "v2_h": float(state["h"]),
            "v2_sigma_t": self.core._last_sigma_t,
            "v2_ell": float(state["ell"]),
            "v2_k_up": float(decision.get("k_up", 0.0)),
            "v2_k_down": float(decision.get("k_down", 0.0)),
            "v2_P_up": float(decision.get("P_up", 0.0)),
            "v2_P_down": float(decision.get("P_down", 0.0)),
            "v2_E_star": float(decision.get("E_star", 0.0)),
            "v2_n_star": float(decision.get("n_star", 0.0)),
            "v2_direction": int(decision.get("direction", 0)),
            "price_overextended": self._price_overextended(c),
            "momentum_past_peak": self._momentum_past_peak(),
        }
        full["volume_profiles"] = []   # V2 uses a KDE vector, not bins — front-end OK with empty
        full["in_position"] = self.in_position
        full["entry_price"] = self.entry_price
        full["peak_price"] = self._peak_price
        full["trail_stop_price"] = self._compute_trail_stop_price()
        full["exhaustion_bars"] = self.exhaustion_bar_count
        full["in_chop"] = self._is_chop_zone(c) or self._in_local_chop
        full["trend_bars"] = self.trend_bar_count
        return full

    def _compute_trail_stop_price(self):
        if not self.in_position:
            return None
        sl = self._global_stoploss_pct()
        if sl == 0:
            return None
        if sl > 0:
            return self._peak_price * (1.0 - abs(sl) / 100.0)
        else:
            return self.entry_price * (1.0 - abs(sl) / 100.0)

    # ── Risk-management exit checks ──────────────────────────────────
    def _check_exit_v2(self, c: float, l: float = 0.0, h: float = 0.0,
                       decision: Optional[dict] = None) -> Optional[_V1Signal]:
        """
        Trigger Bayesian exit logic per spec §6.  iter04 consolidated all
        exit logic around the mathematical posterior; iter18b removes the
        hard-stop floor entirely so the engine relies exclusively on:

          1. TP  — `c ≥ entry·(1 + tp/100)` (spec-mandated harvest).
          2b. Gain-retrace profit lock (iter17): arm at +A% peak gain, exit
              when gain retraces to peak_gain*(1-g).  Tail-preserving.
          2c. Breakeven scratch (iter17): arm after -X% drawdown, exit when
              price recovers to entry+buf%.
          3. Reversal regime (spec §4 reversal label).
          4. Leading-decay exit (iter05, disabled by default).
          5. Kramers down exit: P_down ≥ P_up and P_down ≥ P_zero and ≥ 0.5.
          6. Bayesian flip: direction != +1 AND E_star > 0.

        No hard-stop floor (iter18b): stoploss_pct=0 disables all price-floor
        exits.  Memecoin pullbacks of 15–30% are normal intra-trend noise;
        a hard floor at -25% terminates 0%-WR trades that the Bayesian exits
        (Kramers P_down, reversal) would handle once enough KDE evidence
        accumulates — but the floor also kills healthy pullbacks of winners.
        Removing it restores the pure Bayesian contract.
        """
        assert self.in_position

        if decision is None:
            decision = {"direction": 0, "E_star": -1.0}

        tp_pct = self._effective_takeprofit_pct()
        sl_pct = self._effective_stoploss_pct()
        g_sl_pct = self._global_stoploss_pct()

        if self.position_direction == _V1Direction.UP:
            entry = self.entry_price
            # 1. Take-profit (V1 contract, spec-friendly harvest).
            if entry > 0 and tp_pct > 0 and c >= entry * (1.0 + tp_pct / 100.0):
                self.exit_signal_reason = "tp_v2"
                return _V1Signal.EXIT
            # 2b. iter17 gain-retrace profit lock (tail-preserving trail).
            #     Counterfactual on the 404-trade logged batch: converts
            #     give-back-all-the-way losers into locked wins WITHOUT
            #     capping the right tail (a +60% peak with g=0.6 exits at
            #     +24%, not +6%).  Arm at +A% peak gain; exit when the
            #     current gain retraces to peak_gain*(1-g).  Justification:
            #     path-dependent trailing Kramers barrier — recapture of
            #     the trailing level = escape from the profit region; same
            #     execution-risk family as the spec-listed hard_stop.
            #     Peak tracked from state highs via _update_peak_price.
            if self._gain_retrace_arm_pct > 0.0 and entry > 0:
                peak_gain = self._peak_price / entry - 1.0
                if peak_gain * 100.0 >= self._gain_retrace_arm_pct:
                    floor_gain = peak_gain * (1.0 - self._gain_retrace_give_frac)
                    if c <= entry * (1.0 + floor_gain):
                        self.exit_signal_reason = "gain_retrace"
                        return _V1Signal.EXIT
            # 2c. iter17 breakeven scratch.  Arm once the trade has been
            #     ≥X% offside (low touches entry*(1-X/100)); if price then
            #     recovers to entry*(1+buf), exit at a cost-covering
            #     scratch instead of riding the second leg down.  Winner
            #     intra-MAE p5=-30% on the fresh dataset, so X=20 arms
            #     only on genuinely-distressed trades.
            if self._breakeven_arm_dd_pct > 0.0 and entry > 0:
                if (not self._be_armed and l > 0.0
                        and l <= entry * (1.0 - self._breakeven_arm_dd_pct / 100.0)):
                    self._be_armed = True
                if self._be_armed and c >= entry * (1.0 + self._breakeven_buffer_pct / 100.0):
                    self.exit_signal_reason = "breakeven_scratch"
                    return _V1Signal.EXIT
            # 3. Reversal regime (spec §4 reversal label) with persistence guard.
            #    Mathematical basis: the V2 per-particle regime is derived from
            #    the posterior phase vector (mu_t, mu_dot_t, phi_t).  A single-
            #    tick REVERSAL can be driven by a transient phi spike (phi OU
            #    noise std ~ sigma_phi/√(2α) ≈ 0.24) that reverts within 2-3s.
            #    Exiting immediately on a single-tick reversal = exiting on
            #    posterior noise rather than on a sustained regime shift.
            #    Requiring `reversal_exit_bars` consecutive REVERSAL bars ensures
            #    the phi-driven order-flow component has genuinely sustained
            #    against the drift for K × dt seconds — a temporal coherence
            #    condition equivalent to integrating the regime posterior over
            #    a K-bar window before committing to an exit.
            #    Default K=3 (3 × 4-state × 1s = 3s hold of reversal posterior).
            _rev_k = int(getattr(self, "_reversal_exit_bars", 3))
            if self.regime == _V1Regime.REVERSAL and self.reversal_bar_count >= _rev_k:
                self.exit_signal_reason = "reversal_exit"
                return _V1Signal.EXIT
            # 4. iter05 leading-decline exit:  fire BEFORE kramers_down_exit
            #    when the EMA-smoothed posterior momentum derivative has
            #    been negative for ≥ `iter05_decay_persist_bars` consecutive
            #    state-updates AND the trade is deeper offside than
            #    `iter05_decay_offside_pct`.  Mathematical motivation: the
            #    SDE's deterministic drift is -λ_μ μ_t + κ_μ (φ_t - φ̄_t);
            #    a sustained negative `μ̇` (one-step derivative of the
            #    posterior μ) signals that the OU mean-reversion force has
            #    reversed AND the order-flow pressure has turned against
            #    the position.  The integral this represents over the KDE
            #    memory window T_w has not yet accumulated enough
            #    down-crossings to flip `P_down ≥ 0.5`, but its trajectory
            #    is locked in.  The "offside" guard prevents premature
            #    exits during normal noise (a winning pullback rarely
            #    goes deeper than 6% from entry); 8% is the threshold
            #    that isolates genuinely-bleeding trades.
            #
            #    DISABLED BY DEFAULT (iter05_decay_exit_enable = 0.0) —
            #    the entry-side block (`iter05_decay_entry_block`) is the
            #    preferred mechanism after the smoke test documented at
            #    `backend/analysis/iter05_followups.md` showed exit-alone
            #    produces net-negative re-entry churn.
            if float(getattr(self, "iter05_decay_exit_enable", 0.0)) > 0.0:
                decay_persist = int(getattr(self, "iter05_decay_persist_bars", 6))
                decay_offside_pct = float(getattr(self, "iter05_decay_offside_pct", 8.0))
                # Use windowed decay prevalence (true if ≥80% of last 60
                # ticks had sma<0) — more robust than the tick-level
                # persist counter which resets on a 1-tick bounce.
                window = int(getattr(self, "iter05_decay_window", 60))
                thresh = float(getattr(self, "iter05_decay_window_thresh", 0.8))
                neg_count = int(getattr(self.core, "_mu_dot_post_sign_neg_count", 0))
                sma = getattr(self.core, "_mu_dot_post_ema", 0.0)
                if (sma < 0.0 and neg_count >= thresh * window and
                    entry > 0 and c <= entry * (1.0 - decay_offside_pct / 100.0)):
                    self.exit_signal_reason = "leading_decay_exit"
                    return _V1Signal.EXIT
            # 5. Bayesian exit B: posterior downward escape majority.
            #    iter03 evidence: 23 trades, WR=69.6%, +0.317 SOL on a 30-token
            #    sample — the Bayesian escape-rate exit is the engine's crown logic.
            p_up   = float(decision.get("P_up",   0.0))
            p_down = float(decision.get("P_down", 0.0))
            p_zero = float(decision.get("P_zero", 0.0))
            if p_down > p_up and p_down > p_zero and p_down >= 0.5:
                self.exit_signal_reason = "kramers_down_exit"
                return _V1Signal.EXIT
            # 6. Bayesian exit A: direction has flipped away from +1 with
            #    positive counter-direction Kelly utility.
            if decision.get("direction", 0) != 1 and decision.get("E_star", -1.0) > 0:
                self.exit_signal_reason = "bayesian_flip"
                return _V1Signal.EXIT
            # 7. iter21 sustained-no-long-Kelly exit ("kelly_flat").
            #    Bayesian decision theory: while in position, the engine's
            #    own per-tick Kelly utility for "continue holding long" is
            #    E_star when direction == +1.  If direction has devolved to
            #    ≤ 0 (engine-wants-flat or engine-wants-short WITHOUT a
            #    Kelly-positive counter-trade) AND this condition has held
            #    for self._no_long_exit_bars consecutive ticks AND the trade
            #    is below self._no_long_offside_pct under water, holding
            #    cannot be rational — the engine itself asserts the long
            #    has no positive expected log-wealth increment.  The static
            #    -mask analysis (iter20_static_mask_v2) on the 36-recording
            #    worst-loser cohort at K=20-candles (= 80 ticks) and -20%
            #    offside showed +0.385 SOL saved on losers vs -0.120 SOL
            #    lost on winners; the production tuning K=60 + offs=-40%
            #    (iter21_k60_offs40 batch: 259 trades @ 77.2% WR, +0.884
            #    SOL, PF 1.55) cleared all 5 paired-diff gates vs
            #    iter16_baseline_full (+1.682 SOL Δ, Wilcoxon p=0.004) at
            #    the cost of 4 W→L regression cuts during exogenous
            #    recoveries (rec70, rec527, rec21, rec555).
            #    Distinct from exit #5 (Bayesian down-flip) which requires
            #    the engine's POSTERIOR P_down to dominate; this exit is
            #    the entry-gate's mirror image: entry requires direction=1
            #    AND E_star > 0; exit when direction !=1  AND E_star <= 0
            #    sustained.
            # iter21 hypothesis K (μ-persistence guard): require also that
            # the post-entry EMA-smoothed UKF drift (μ_t) has been negative
            # for the majority of the trailing 60-tick window.  Intended to
            # distinguish genuine slides (drift falling → OU continued-slide
            # forecast) from winning-trade pullback dips (drift rising → OU
            # recovery forecast).  REJECTED at full batch:
            #   iter21k_mu75 vs iter21_k60_offs40 REJECTED (Δ=-0.072 SOL,
            #   3 improved / 11 regressed) — the rec70/rec21 W→L regressions
            #   had mu_neg_frac=1.00 at the cut (HIGHEST possible; cannot be
            #   filtered by any `<=1.0` threshold), while several L→W
            #   improvements (rec828 mu=0.00, rec346 mu=0.25, rec657 mu=0.18,
            #   rec635 mu=0.27) had mu_neg_frac < 0.75 and were BLOCKED by
            #   the guard.  Genuine slides and recovery-pullback dips are
            #   NOT statistically distinguishable from the OU drift posterior
            #   at the kelly_flat decision moment — recovery is exogenous.
            # Default 0.0 = OFF (preserves iter21_k60_offs40 parity).
            _mu_neg_frac = (self._mu_post_neg_count /
                            self._mu_post_neg_window.maxlen
                            if self._mu_post_neg_window.maxlen else 0.0)
            if (self._no_long_exit_bars > 0
                    and self._no_long_offside_pct > 0.0
                    and self._no_long_streak >= self._no_long_exit_bars
                    and self._no_long_mu_neg_frac <= _mu_neg_frac
                    and entry > 0 and c <= entry * (1.0 - self._no_long_offside_pct / 100.0)):
                self.exit_signal_reason = "kelly_flat"
                return _V1Signal.EXIT
            # 8. iter36 holder-flow exit: dev/insider sell while in position.
            #    A dev/insider sell is an exogenous adverse event that the
            #    engine's internal state cannot see — the KDE posterior has
            #    not yet accumulated enough down-crossings to flip P_down,
            #    but the on-chain evidence is unambiguous.  Default OFF
            #    (v2_holder_flow_exit_enable = 0.0) — requires holder_flow
            #    events to be loaded via set_holder_flow_events().
            if (self._v2_holder_flow_exit_enable > 0.0
                    and self._has_recent_dev_sell(
                        getattr(self, "_current_time", 0),
                        self._v2_holder_flow_exit_window_seconds,
                        self._v2_holder_flow_min_usd
                    )):
                sell_event = self._get_recent_dev_sell(
                    getattr(self, "_current_time", 0), self._v2_holder_flow_exit_window_seconds
                )
                self.exit_signal_reason = (
                    f"dev_sell_exit:{sell_event.get('wallet','')[:8] if sell_event else 'unknown'}"
                )
                return _V1Signal.EXIT
        return None

    # ── V2 entry gate (uses V2 confidence + V1-style secondary gates) ──
    def _v2_passes_entry_gate(self, c: float, decision: dict) -> bool:
        # Need both confidence above threshold and Kramers upward prob.
        if self.trend_confidence < self.entry_confidence_high:
            return False
        if decision.get("P_up", 0.0) < self._v2_p_up_min:
            return False
        # iter17: posterior-vol floor — counterfactual showed σ_t ≥ 0.021
        # entries carry the PnL (removes break-even churn, PF 1.57→1.87).
        if self._v2_sigma_t_min > 0.0:
            if float(getattr(self.core, "_last_sigma_t", 0.0) or 0.0) < self._v2_sigma_t_min:
                return False
        # iter17b: require momentum_past_peak — on the 404-trade logged
        # batch, entries made WHILE momentum is past peak carried the win
        # rate and 96% of the PnL on just 34% of the trades (the past-peak
        # flag marks a parabolic breakout/continuation rather than a knife
        # catch); past_peak=False entries were 28% hard-stop crashes vs 9%.
        if self._v2_require_past_peak > 0.0 and not self._momentum_past_peak():
            return False
        # Long-only: drift must be positive.
        if float(decision.get("direction", 0)) != 1:
            return False
        # Macro trend gate (V1 contract).
        if self.ema_macro_val is not None and c < self.ema_macro_val:
            return False
        # iter05 leading-decay ENTRY BLOCK (toggleable).
        # Refuse new BUY entries while the EMA-smoothed posterior μ̇ has
        # been negative for ≥ `iter05_decay_window_thresh` of the last
        # `iter05_decay_window` state-updates.  This is the principled
        # "posterior trajectory is locked downward" gate — see the
        # docstring of `iter05_decay_entry_block`.
        if float(getattr(self, "iter05_decay_entry_block", 1.0)) > 0.0:
            window = int(getattr(self, "iter05_decay_window", 60))
            thresh = float(getattr(self, "iter05_decay_window_thresh", 0.8))
            neg_count = int(getattr(self.core, "_mu_dot_post_sign_neg_count", 0))
            # Only gate when window is fully populated; otherwise engine
            # would over-block at the cold-start of a token.
            if neg_count >= thresh * window:
                return False
        # iter05 s_effective gate — empirically grounded (see adapter
        # docstring of iter05_s_effective_min).  Acts as a barrier-/SNR-
        # quality floor; trades entered below this proxy lose more often.
        s_min = float(getattr(self, "iter05_s_effective_min", 0.0))
        if s_min > 0.0 and float(getattr(self, "s_effective", 0.0)) < s_min:
            return False
        return True



