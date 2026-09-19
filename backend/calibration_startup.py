"""Shared causal calibration initialization for live and recorded sessions."""
from __future__ import annotations

import asyncio
import math

from session_calibrator import (
    _DB_PATH, _CLIP, GEOMETRY_KEYS, calibrate_from_mint_history,
    calibrate_from_population,
)


def calibration_window(started_at: float, params: dict) -> tuple[float, float]:
    lag = float(params.get('v2_calibration_history_lag_seconds', 0.0))
    lookback = float(params.get('v2_calibration_history_seconds', 6000.0))
    if not math.isfinite(started_at) or started_at <= 0:
        raise ValueError('calibration start must be a positive timestamp')
    if not math.isfinite(lag) or lag < 0 or not math.isfinite(lookback) or lookback <= 0:
        raise ValueError('calibration history requires finite nonnegative lag and positive lookback')
    return float(math.floor(started_at - lag)), lookback


def _strip_default_sde(explicit: dict, DEFAULT_CONFIG: dict) -> None:
    """Drop SDE clip keys whose value equals the engine default, in place.

    The UI mirror (frontend engineParamsV2) transmits every knob including
    the 13 SDE coefficients AT THEIR DEFAULT VALUES.  Treating those as user
    intent made `{**base, **explicit}` overwrite the calibrator's
    coefficients with defaults AND killed the `_calibration_sourced`
    sentinel (any _CLIP key in explicit kills it) — so every UI-driven
    backtest and UI-launched live session ran uncalibrated physics, found
    2026-09-19 via the user's 7-day batch rows in backtest_data.db.  Only a
    value that DEVIATES from DEFAULT_CONFIG is genuine intent (surgical
    overrides keep working).  Tolerant compare: the mirror rounds lambda_0
    (6.94444e-05 vs 6.9444444...e-05).
    """
    for key in list(_CLIP):
        if key not in explicit:
            continue
        try:
            v = float(explicit[key])
            d = float(DEFAULT_CONFIG[key])
        except (TypeError, ValueError):
            continue
        if math.isclose(v, d, rel_tol=1e-6, abs_tol=1e-9):
            del explicit[key]


async def initialize_calibration(mint: str, started_at: float, params: dict | None = None,
                                 db_path: str = _DB_PATH) -> tuple[dict, dict]:
    from strategy_engineV2 import DEFAULT_CONFIG

    explicit = dict(params or {})
    _strip_default_sde(explicit, DEFAULT_CONFIG)
    enabled = bool(explicit.pop('use_session_calibration', explicit.get(
        'v2_popcal_enable', DEFAULT_CONFIG['v2_popcal_enable'])))
    cutoff, lookback = calibration_window(float(started_at), explicit)
    audit = {'started_at': float(started_at), 'cutoff': cutoff,
             'window_start': cutoff - lookback, 'source': 'defaults',
             'chain_attempted': False, 'chain_inserted': 0}
    if not enabled:
        explicit.setdefault('v2_percoin_cal_enable', 0.0)
        # iter90: the NONCAL hatch kills the geometry layer too.
        explicit.setdefault('v2_harvestcal_enable', 0.0)
        explicit.setdefault('v2_percoin_geo_enable', 0.0)
        return explicit, audit

    harvest_on = bool(explicit.get('v2_harvestcal_enable',
                                   DEFAULT_CONFIG['v2_harvestcal_enable']))
    _geom_user_explicit = any(key in explicit for key in GEOMETRY_KEYS)

    # iter90d: variance-ratio decision horizon (τ) — flag + scale threaded
    # through every estimation layer (OFF = legacy lambda_mu inversion).
    tau_vr = bool(explicit.get('v2_tau_vr_enable',
                               DEFAULT_CONFIG['v2_tau_vr_enable']))
    tau_scale = float(explicit.get('v2_tau_vr_scale',
                                   DEFAULT_CONFIG['v2_tau_vr_scale']))

    mint_on = bool(explicit.get('v2_mintcal_enable', DEFAULT_CONFIG['v2_mintcal_enable']))
    base = {}
    if mint_on and mint:
        base = await asyncio.to_thread(calibrate_from_mint_history, mint, cutoff,
                                       db_path, lookback, tau_vr, tau_scale)
        chain_on = bool(explicit.get('v2_chain_fetch_enable', DEFAULT_CONFIG['v2_chain_fetch_enable']))
        if not base and chain_on:
            from mint_chain_history import fetch_and_persist
            audit['chain_attempted'] = True
            try:
                audit['chain_inserted'] = await asyncio.wait_for(
                    fetch_and_persist(mint, db_path=db_path, before_unix=cutoff,
                                      lookback_seconds=lookback), timeout=95.0)
                base = await asyncio.to_thread(calibrate_from_mint_history, mint, cutoff,
                                               db_path, lookback, tau_vr, tau_scale)
            except Exception as exc:
                audit['chain_error'] = type(exc).__name__
        if base:
            audit['source'] = 'mint_history'
            if not any(key in explicit for key in _CLIP):
                base['_calibration_sourced'] = 1
            if not _geom_user_explicit:
                base['_geometry_sourced'] = 1
    if not base:
        base = await asyncio.to_thread(calibrate_from_population, db_path, cutoff,
                                       tau_vr=tau_vr, tau_scale=tau_scale)
        audit['source'] = 'population' if any(k in base for k in _CLIP) else 'defaults'
        if not _geom_user_explicit and any(k in base for k in GEOMETRY_KEYS):
            base['_geometry_sourced'] = 1

    if not harvest_on:
        # Master switch OFF: the session's initial arm stays the production
        # default (adapter fallback) and the per-coin refinement stays off.
        for key in GEOMETRY_KEYS:
            base.pop(key, None)
        base.pop('_geometry_sourced', None)
        explicit.setdefault('v2_percoin_geo_enable', 0.0)

    audit['initial_coefficients'] = {k: v for k, v in base.items() if k in _CLIP}
    audit['initial_geometry'] = {k: v for k, v in base.items() if k in GEOMETRY_KEYS}
    audit['geometry_sourced'] = bool(base.get('_geometry_sourced', 0))
    audit['geometry_user_explicit'] = _geom_user_explicit
    merged = {**base, **explicit}
    return merged, audit


def initialize_calibration_sync(mint: str, started_at: float, params: dict | None = None,
                                db_path: str = _DB_PATH) -> tuple[dict, dict]:
    return asyncio.run(initialize_calibration(mint, started_at, params, db_path))
