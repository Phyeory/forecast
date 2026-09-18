"""Shared causal calibration initialization for live and recorded sessions."""
from __future__ import annotations

import asyncio
import math

from session_calibrator import (
    _DB_PATH, _CLIP, calibrate_from_mint_history, calibrate_from_population,
)


def calibration_window(started_at: float, params: dict) -> tuple[float, float]:
    lag = float(params.get('v2_calibration_history_lag_seconds', 0.0))
    lookback = float(params.get('v2_calibration_history_seconds', 6000.0))
    if not math.isfinite(started_at) or started_at <= 0:
        raise ValueError('calibration start must be a positive timestamp')
    if not math.isfinite(lag) or lag < 0 or not math.isfinite(lookback) or lookback <= 0:
        raise ValueError('calibration history requires finite nonnegative lag and positive lookback')
    return float(math.floor(started_at - lag)), lookback


async def initialize_calibration(mint: str, started_at: float, params: dict | None = None,
                                 db_path: str = _DB_PATH) -> tuple[dict, dict]:
    from strategy_engineV2 import DEFAULT_CONFIG

    explicit = dict(params or {})
    enabled = bool(explicit.pop('use_session_calibration', explicit.get(
        'v2_popcal_enable', DEFAULT_CONFIG['v2_popcal_enable'])))
    cutoff, lookback = calibration_window(float(started_at), explicit)
    audit = {'started_at': float(started_at), 'cutoff': cutoff,
             'window_start': cutoff - lookback, 'source': 'defaults',
             'chain_attempted': False, 'chain_inserted': 0}
    if not enabled:
        explicit.setdefault('v2_percoin_cal_enable', 0.0)
        return explicit, audit

    mint_on = bool(explicit.get('v2_mintcal_enable', DEFAULT_CONFIG['v2_mintcal_enable']))
    base = {}
    if mint_on and mint:
        base = await asyncio.to_thread(calibrate_from_mint_history, mint, cutoff,
                                       db_path, lookback)
        chain_on = bool(explicit.get('v2_chain_fetch_enable', DEFAULT_CONFIG['v2_chain_fetch_enable']))
        if not base and chain_on:
            from mint_chain_history import fetch_and_persist
            audit['chain_attempted'] = True
            try:
                audit['chain_inserted'] = await asyncio.wait_for(
                    fetch_and_persist(mint, db_path=db_path, before_unix=cutoff,
                                      lookback_seconds=lookback), timeout=95.0)
                base = await asyncio.to_thread(calibrate_from_mint_history, mint, cutoff,
                                               db_path, lookback)
            except Exception as exc:
                audit['chain_error'] = type(exc).__name__
        if base:
            audit['source'] = 'mint_history'
            if not any(key in explicit for key in _CLIP):
                base['_calibration_sourced'] = 1
    if not base:
        base = await asyncio.to_thread(calibrate_from_population, db_path, cutoff)
        audit['source'] = 'population' if any(k in base for k in _CLIP) else 'defaults'
    audit['initial_coefficients'] = {k: v for k, v in base.items() if k in _CLIP}
    merged = {**base, **explicit}
    return merged, audit


def initialize_calibration_sync(mint: str, started_at: float, params: dict | None = None,
                                db_path: str = _DB_PATH) -> tuple[dict, dict]:
    return asyncio.run(initialize_calibration(mint, started_at, params, db_path))
