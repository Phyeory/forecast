"""market_calibration.py — recalibrate the V2 engine for non-memecoin tapes.

The production engine lives on 1-second memecoin candles.  These systems
feed it DAILY (stocks) or HOURLY/DAILY (perp futures) bars, so every
time-scale-dependent knob must be mistranslated... correctly, not copied.

# Engine time-model recap (why the rescale factors are what they are)

* Every engine.``update()`` intake sets `obs["dt"] = 1.0` — one intake is
  one unit time step regardless of wall-clock (strategy_engineV2.py:3944).
* ONE candle = FOUR intakes:  open → extreme1 → extreme2 → close
  (aggregate_expanded / 4-state expansion, invariant 2).
  ⇒ 1 candle = 4 dt units.
* All OU rates / shocks in DEFAULT_CONFIG are per-intake: rates must be
  divided by 4 (a per-bar rate applied 4×), shocks (a √σ·√dt process)
  divided by 2.  Couplings (kappa_mu, beta) are dt-free gains.
* The KDE potential's time base is INTAKE COUNT, not epoch
  (`now_t = float(self._bar_count)`, strategy_engineV2.py:2692), so
  lambda_0 (decay per intake) = 1 / tw_window in INTAKES.
* EVR timing knobs (eval_delay / flow_window / conc_window) run on the
  REAL epoch candle timestamps → recalibrated in true seconds.
* The entry noise gate (v2_sigma_t_min, posterior σ floor per intake)
  must shrink with the tape's absolute volatility (memecoin 1s r_std ~
  0.35%, stocks daily ~1-2%, perp 1h ~0.4-0.8%).

# Causality

Calibration runs on a PREFIX ONLY (first `calib_bars` completed candles),
never the traded window — the same discipline session_calibrator applies
(`AND time < before_unix`).  Per-coin online recalibration is explicitly
OFF (the engine's internal estimator would apply per-bar estimates at
per-intake cadence — 4× over-energetic), so the session's SDE coefficients
are frozen explicit params derived from the prefix.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

try:
    from session_calibrator import (
        _compute_rec_stats,
        _tau_vr_from_closes,
        TAU_VR_LADDER,
    )
except ImportError:  # running as a package from backend/
    from ..session_calibrator import (  # type: ignore
        _compute_rec_stats,
        _tau_vr_from_closes,
        TAU_VR_LADDER,
    )

INTAKES_PER_CANDLE = 4          # invariant-2 4-state expansion
KDE_MEM_BARS = {"stocks": 375, "crypto_perp_1h": 500, "crypto_perp_1d": 375}

# Per-intake clip ranges after rescale.  Rate floors guard UKF conditioning;
# shock floors sit at DEFAULT/8 (the memecoin DEFAULT/2 rule assumes 1s
# tapes where true noise ≥ 0.05 — on slower tapes honest per-intake noise
# is ~2 orders lower, so we soften but still bound away from collapse).
RATE_KEYS = ("lambda_mu", "alpha", "eta", "theta")
SHOCK_D2 = (                          # shocks → ÷2 (÷√4)
    ("sigma_mu", "shock"),
    ("sigma_phi", "shock"),
    ("sigma_h", "shock"),
    ("sigma_ell", "shock"),
    ("zeta", "zeta"),
)
_MARKET_CLAMP: dict[str, tuple[float, float]] = {
    "lambda_mu":  (5e-4, 0.25),
    "alpha":      (5e-4, 0.25),
    "eta":        (5e-4, 0.25),
    "theta":      (5e-4, 0.25),
    "sigma_mu":   (0.001, 0.20),
    "kappa_mu":   (0.002, 0.10),
    "sigma_phi":  (0.002, 0.20),
    "beta":       (0.30, 3.00),
    "sigma_h":    (0.002, 0.25),
    "sigma_ell":  (0.001, 0.20),
    "zeta":       (0.04, 0.50),
}

BAR_SPEC = {
    "stocks":         {"bar_seconds": 86400.0, "calib_bars": 505},
    "crypto_perp_1h": {"bar_seconds": 3600.0,  "calib_bars": 1500},
    "crypto_perp_1d": {"bar_seconds": 86400.0, "calib_bars": 505},
}


# ─────────────────────────────────────────────────────────────────────────────
# Strategy-threshold recalibration (percentiles of the prefix tape)
# ─────────────────────────────────────────────────────────────────────────────

def _forward_runup_dd(close: np.ndarray, high: np.ndarray,
                      window: int = 10) -> tuple[float, float]:
    """Median forward 10-bar max-up and max-down (%) — the tape's scale."""
    n = len(close)
    if n < window + 20:
        return (float("nan"), float("nan"))
    runups, dds = [], []
    for i in range(n - window):
        fmax = float(high[i + 1:i + 1 + window].max())
        runups.append((fmax / close[i] - 1.0) * 100.0)
        # disproportionate-percentage drawdown from the same anchor
        dds.append(-(close[i + window] / close[i] - 1.0) * 100.0 if
                   close[i + window] < close[i] else 0.0)
    return (float(np.median(runups)), float(-np.percentile(dds, 40)))


def calibrate_market_params(candles: list[dict], market_kind: str,
                            calib_bars: Optional[int] = None) -> dict:
    """Full recalibration of engine params from the tape PREFIX (causal).

    `market_kind` — "stocks" | "crypto_perp" | "crypto_perp_1d"
    Returns {"engine_params": dict, "audit": dict}.
    """
    spec = BAR_SPEC["crypto_perp_1d" if market_kind == "crypto_perp_1d" else market_kind]
    bar_seconds = spec["bar_seconds"]
    calib_bars = calib_bars or spec["calib_bars"]

    candles = sorted(candles, key=lambda c: c["time"])
    prefix = candles[:calib_bars]
    if len(prefix) < 300:
        raise ValueError(f"prefix too thin: {len(prefix)} bars")

    arr = np.array(
        [[1, c["open"], c["high"], c["low"], c["close"], c.get("volume", 0.0),
          c.get("buy_volume", 0.0), c.get("sell_volume", 0.0),
          c.get("pool_sol", 0.0)] for c in prefix], dtype=float)
    duration_s = bar_seconds * len(prefix)

    stats = _compute_rec_stats(arr, duration_s) or {}

    closes = arr[:, 4].astype(float)
    highs = arr[:, 2].astype(float)
    p = {}   # engine_params

    # ── SDE coefficients: per-bar estimates → per-intake params ─────
    for key in RATE_KEYS:
        v = stats.get(key)
        if v is not None and math.isfinite(v):
            p[key] = round(float(np.clip(v / INTAKES_PER_CANDLE,
                                         *_MARKET_CLAMP[key])), 6)
        else:
            p[key] = round(float(np.clip(0.10 / INTAKES_PER_CANDLE,
                                         *_MARKET_CLAMP[key])), 6)
    v = stats.get("sigma_mu")
    p["sigma_mu"] = round(float(np.clip(
        (v if v is not None and math.isfinite(v) else 0.10) / 2.0,
        *_MARKET_CLAMP["sigma_mu"])), 6)
    v = stats.get("sigma_phi")
    p["sigma_phi"] = round(float(np.clip(
        (v if v is not None and math.isfinite(v) else 0.15) / 2.0,
        *_MARKET_CLAMP["sigma_phi"])), 6)
    v = stats.get("sigma_h")
    p["sigma_h"] = round(float(np.clip(
        (v if v is not None and math.isfinite(v) else 0.20) / 2.0,
        *_MARKET_CLAMP["sigma_h"])), 6)
    # liquidity layer is inert on market tapes (pool_sol=0) — DEFAULT / rescaled
    p["kappa_mu"] = round(float(np.clip(
        stats.get("kappa_mu", 0.05) or 0.05, *_MARKET_CLAMP["kappa_mu"])), 6)
    p["beta"] = _MARKET_CLAMP["beta"][0]
    p["theta"] = round(float(np.clip(0.10 / INTAKES_PER_CANDLE,
                                     *_MARKET_CLAMP["theta"])), 6)
    p["sigma_ell"] = round(float(np.clip(0.10 / 2.0, *_MARKET_CLAMP["sigma_ell"])), 6)
    # ζ=0 DELIBERATE LONG-TAPE GUARD: the liquidity latent ℓ has no observable
    # anchor (see rbpf comments — "no observable L2 channel"), and every
    # 3×-volume day applies a ONE-SIDED jump decay −ζ.  On short memecoin
    # sessions that never matters; on multi-year tapes the Ω-random-walk
    # decays ℓ → −15 irreversibly (posterior pins at every clamp, P→0,
    # R_ema saturates → engine dead from the first earnings-gap day).
    # Verified empirically: AAPL pegged at candle 199 (first 3×-volume day)
    # with ζ∈{defaults, rescaled}; the ζ channel is the sole destroyer.
    p["zeta"] = 0.0

    # ── KDE potential memory: INTAKE-BASE time (engine now_t = bar_count) ──
    tw_bars = KDE_MEM_BARS.get(market_kind, KDE_MEM_BARS["stocks"])
    tw_intakes = tw_bars * INTAKES_PER_CANDLE
    p["lambda_0"] = round(1.0 / tw_intakes, 10)
    p["tw_window_seconds"] = float(tw_intakes)

    # ── Decision horizon τ (empirical, 2026-10-17) ────────────────────
    # Swept τ ∈ {20,40,60,120} on AAPL-1d and SOLUSDT-1h real replays.
    # Rates per intake (k_total) run ~10-25× hotter than memecoin
    # production on market tapes, so a fixed τ=60+ lets the Kramers
    # down-escape dominate the exit book (SOL-1h: 257 kramers_down churn
    # exits, WR 36%).  Interval-class τ (1d→40, 1h→20) restores the
    # production P⁰-balance behavior: SOL-1h churn halves (kramers 257→33,
    # WR 36→52%); AAPL-1d turns positive (WR 75%).  τ_min keeps the
    # engine's rate-window sane.
    if market_kind == "crypto_perp_1h":
        tau_max, tau_min_floor = 20.0, 3.0
    else:  # stocks, crypto_perp_1d
        tau_max, tau_min_floor = 40.0, 5.0
    p["tau_max"] = float(tau_max)
    p["tau_min"] = max(tau_min_floor, tau_max / 8.0)
    p["tau_step"] = 20.0

    # ── Entry noise gate from the filter's OWN posterior σ (causal) ────
    # The entry gate (v2_sigma_t_min) compares against the UKF posterior
    # σ_t, NOT the raw bar vol — and the posterior level depends on the
    # tape's cadence and filter state.  Calibrate it by replaying the
    # FAST prefix idempotently through a throwaway engine and reading the
    # post-warmup σ_t distribution.  Gate = 0.35 × p10 (blocks the truly
    # degenerate states, never the healthy floor).
    #
    # The replay is bounded to the LAST `_REPLAY_BARS` bars of the prefix:
    # with train/test splits the prefix can span tens of thousands of 1h
    # bars (a full replay would double the per-symbol cost), and a
    # cold-started 3000-bar window is statistically identical for a p10
    # read.  The engine used is the V2M CLONE (same math as V2 — the only
    # fork delta, ζ=0, is passed explicitly in `p` anyway — but keeps the
    # calibration path on the exact engine class the runner uses).
    _REPLAY_BARS = 3000
    replay = prefix[-_REPLAY_BARS:]
    try:
        from .strategy_engineV2M import StrategyEngineV2MAdapter
        eng = StrategyEngineV2MAdapter(**p)
        sig_post: list[float] = []
        for i, cand in enumerate(replay):
            t0 = int(cand["time"]); o = float(cand["open"])
            hi = float(cand["high"]); lo = float(cand["low"])
            cl = float(cand["close"])
            vv = float(cand.get("volume", 0.0))
            bv = float(cand.get("buy_volume", 0.0))
            sv = float(cand.get("sell_volume", 0.0))
            bull = cl >= o
            mf, ms = (hi, lo) if bull else (lo, hi)
            eng.update(time=t0, o=o, h=o, l=o, c=o, volume=0.0, _build_full_result=False)
            eng.update(time=t0, o=o, h=max(o, mf), l=min(o, mf), c=mf,
                       volume=0.0, _build_full_result=False)
            eng.update(time=t0, o=o, h=hi, l=lo, c=ms, volume=0.0, _build_full_result=False)
            eng.update(time=t0, o=o, h=hi, l=lo, c=cl, volume=vv,
                       buy_volume=bv, sell_volume=sv, _build_full_result=False)
            if (i + 1) * 4 > p["warmup_bars"]:
                st = getattr(eng, "_last_sigma_t", None)
                if st is not None and math.isfinite(st):
                    sig_post.append(float(st))
        if len(sig_post) >= 50:
            gate = 0.35 * float(np.percentile(sig_post, 10))
        else:
            gate = 0.0
    except Exception:
        sig_post = []
        gate = 0.0
    p["v2_sigma_t_min"] = round(max(gate, 1e-6), 8)

    # ── Strategy thresholds from the tape's own runup/drawdown scale ──
    runup_med, dd_med = _forward_runup_dd(closes, highs,
                                          10 if "1d" not in market_kind else 5)
    arm_pct = float(np.clip(0.55 * runup_med, 3.0, 14.0)) if math.isfinite(runup_med) else 10.0
    p["gain_retrace_arm_pct"] = round(arm_pct, 2)
    p["gain_retrace_give_frac"] = 0.5                                # unchanged
    p["v2_rate_split_arm_pct"] = round(arm_pct, 2)
    p["v2_rate_split_theta"] = 0.55                                  # unchanged
    p["v2_rate_split_persist"] = 12                                  # unchanged
    p["breakeven_arm_dd_pct"] = round(float(np.clip(6.0 * max(dd_med, 1.0), 15.0, 40.0)), 1)
    p["breakeven_buffer_pct"] = round(float(np.clip(0.35 * arm_pct, 1.0, 4.0)), 2)
    p["no_long_exit_bars"] = 60                                       # unchanged
    p["no_long_offside_pct"] = round(float(np.clip(1.3 * max(dd_med, 1.0),
                                                   12.0, 45.0)), 1)
    p["reversal_exit_bars"] = 8                                       # intakes = 2 candles
    p["iter05_decay_window"] = 60                                     # unchanged

    # ── EVR: the only knob family on REAL epoch timestamps ────────────
    eval_bars = 3
    p["v2_evr_enable"] = 1.0
    p["v2_evr_confirm_pct"] = round(float(np.clip(0.8 * runup_med /
                                                  (1 if "1d" not in market_kind else 2),
                                                  4.0, 20.0)), 2)
    p["v2_evr_eval_delay"] = int(eval_bars * bar_seconds)
    p["v2_evr_flow_window"] = int(2 * bar_seconds)
    p["v2_evr_skip_conc_window"] = int(3 * bar_seconds)
    p["v2_evr_buy_ratio_max"] = 0.45
    p["v2_evr_offside_min_pct"] = round(float(np.clip(1.2 * max(dd_med, 1.0),
                                                      8.0, 30.0)), 1)
    p["v2_evr_volume_min_sol"] = 0.0      # volume units meaningless cross-market; guard OFF
    p["v2_evr_require_offside"] = 1.0
    p["v2_evr_skip_sell_conc_min"] = 0.25

    # ── Warmup: 800 intakes (200 candles) for trading tapes ───────────
    p["warmup_bars"] = 800

    # ── Costs: stocks are exchange-grade liquid (sub-bp effective): perps ─
    # engine-side cost model s_0/s_1/fee_fraction is the POSITIVE-EV gate
    # (what the engine believes a round trip costs) — matches the runner's
    # actual fill costs (slippage 3-5 bp + taker fee 0.2-4 bp).
    if market_kind == "stocks":
        p["s_0"], p["s_1"], p["fee_fraction"] = 0.0003, 2e-6, 0.0002
    else:
        p["s_0"], p["s_1"], p["fee_fraction"] = 0.0005, 5e-6, 0.0004
    p["latency_seconds"] = 0.5
    p["liquidity_cap_frac"] = 0.10

    # ── Hard-off switches (per-coin/online layers are memecoin-specific) ─
    p["v2_percoin_cal_enable"] = 0.0     # engine estimator is per-BAR off
    p["v2_percoin_requires_mintcal"] = 0.0  # per-coin requires mintcal=1 otherwise dummy
    p["v2_percoin_geo_enable"] = 0.0
    p["v2_harvestcal_enable"] = 0.0
    p["v2_mintcal_enable"] = 0.0
    p["v2_popcal_enable"] = 0.0
    p["v2_chain_fetch_enable"] = 0.0
    p["v2_tau_vr_enable"] = 0.0          # engine VR path off — tau_max passed explicit
    p["v2_tau_vr_scale"] = 0.0
    p["v2_holder_flow_entry_block"] = 0.0
    p["v2_holder_flow_exit_enable"] = 0.0
    p["v2_drift_work_fraction"] = 0.0
    p["v2_entry_delay_seconds"] = 0.0
    p["v2_exit_delay_seconds"] = 0.0
    p["v2_exit_delay_armed_only"] = 0.0
    p["confidence_high"] = 0.79
    p["entry_confidence_high"] = 0.79

    audit = {
        "market_kind": market_kind,
        "bar_seconds": bar_seconds,
        "calib_bars": len(prefix),
        "prefix_first_close": float(closes[0]),
        "prefix_last_close": float(closes[-1]),
        "tau_rule": f"interval-class {market_kind} -> tau_max {p['tau_max']}",
        "runup_med_pct": runup_med,
        "dd_med_pct": dd_med,
        "tau_max": p["tau_max"],
        "lambda_0": p["lambda_0"],
        "v2_sigma_t_min": p["v2_sigma_t_min"],
        "gain_retrace_arm_pct": p["gain_retrace_arm_pct"],
        "stats_raw": {k: (v if isinstance(v, (int, float, str)) else None)
                      for k, v in stats.items()},
    }
    return {"engine_params": p, "audit": audit}
