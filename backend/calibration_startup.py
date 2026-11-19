"""Shared causal calibration initialization for live and recorded sessions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math

from session_calibrator import (
    _DB_PATH, _CLIP, GEOMETRY_KEYS, calibrate_from_mint_history,
    calibrate_from_population,
)

# Default reach of the mint-history (primary) calibration read.  See
# calibration_window() for why this is not 6000 s.
DEFAULT_CALIBRATION_LOOKBACK_SECONDS = 604800.0   # 7 days
# Bump when the initial-calibration semantics change; old live snapshots must
# remain replayable rather than being silently reinterpreted by a new stack.
CALIBRATION_CACHE_VERSION = 1


def _initial_cache_key(
    mint: str, cutoff: float, lookback: float, tau_vr: bool,
    tau_scale: float, popcal_source: str, mint_on: bool,
) -> str:
    """Identify the history/query inputs that determine a live snapshot."""
    payload = json.dumps({
        "version": CALIBRATION_CACHE_VERSION,
        "mint": mint,
        "cutoff": cutoff,
        "lookback": lookback,
        "tau_vr": bool(tau_vr),
        "tau_scale": float(tau_scale),
        "popcal_source": popcal_source,
        "mint_on": bool(mint_on),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def calibration_window(started_at: float, params: dict) -> tuple[float, float]:
    """Return (cutoff, lookback) for the mint-history layer.

    The mint layer is the PRIMARY calibration source (the token calibrating on
    its own price action); the 50-newest population is the fallback.  The
    lookback bounds how far back that own-history read may reach.

    38c07b9 (2026-09-18) capped this to 6000 s while fixing the causality
    guards.  Nothing about causality requires it — the token's own PAST can
    never leak — and the cap silently starved the layer: of the original-DB
    tapes that have >=120 candles of their own history (28.5%, the coverage
    iter86 measured when it delivered WR 63.8 -> 76.8%), only 38% survived the
    100-minute window.  Default restored to 7 days; the chain-fetch path stays
    bounded by its own max_sigs/timeout, and the RPC only retains ~2 days.
    """
    lag = float(params.get('v2_calibration_history_lag_seconds', 0.0))
    lookback = float(params.get('v2_calibration_history_seconds',
                                DEFAULT_CALIBRATION_LOOKBACK_SECONDS))
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


def strip_stale_sde_echo(params: dict) -> tuple[dict, dict]:
    """iter101 (2026-10-23): remove non-default calibration-owned SDE keys
    from live launch params before calibration.  Returns (params, stripped).

    The dashboard mirror echoes the PREVIOUS session's calibrated
    coefficients back into the launch payload (observed Sep-29: live
    tau_max=60 vs the BT's fresh 20 on the SAME recording — 8/13
    coefficients diverged, −0.21 SOL over 134 paired trades, 10× the
    measured execution drag).  `_strip_default_sde` already removes
    DEFAULT-valued echoes inside initialize_calibration; a stale
    CALIBRATED value deviates from default and used to win the
    `{**base, **explicit}` merge as false "user intent" (that merge
    semantic is pinned by test_calibration_startup).  Production
    backtests calibrate from `{}`: the live call site strips every
    non-default `_CLIP` key and journals it, so the fresh population /
    mint-history calibration wins the merge exactly like run_backtest.

    Policy knobs — delays, gates, geometry (`gain_retrace_arm_pct`, e.g.
    the g16 arm-16 policy) — pass through untouched.
    """
    from strategy_engineV2 import DEFAULT_CONFIG

    stripped: dict = {}
    for key in list(_CLIP):
        if key not in params:
            continue
        try:
            deviates = not math.isclose(
                float(params[key]), float(DEFAULT_CONFIG[key]),
                rel_tol=1e-6, abs_tol=1e-9)
        except (TypeError, ValueError):
            continue
        if deviates:
            stripped[key] = params.pop(key)
    return params, stripped


async def initialize_calibration(mint: str, started_at: float, params: dict | None = None,
                                 db_path: str = _DB_PATH,
                                 recording_id: int | None = None,
                                 allow_chain_fetch: bool = True) -> tuple[dict, dict]:
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

    # iter102c: population source selector ("pool_all" default = unseen +
    # original as one pool; "selected" = legacy single-corpus sample).
    popcal_source = str(explicit.pop('v2_popcal_source',
                                     DEFAULT_CONFIG.get('v2_popcal_source',
                                                        'pool_all')))
    audit['popcal_source'] = popcal_source

    mint_on = bool(explicit.get('v2_mintcal_enable', DEFAULT_CONFIG['v2_mintcal_enable']))
    cache_key = _initial_cache_key(
        mint, cutoff, lookback, tau_vr, tau_scale, popcal_source, mint_on)
    audit['cache_key'] = cache_key
    base = None
    if recording_id is not None:
        from data_store import load_recording_calibration
        cached = await asyncio.to_thread(
            load_recording_calibration, db_path, int(recording_id), cache_key)
        if cached is not None:
            base = dict(cached['initial_overrides'])
            audit = dict(cached['audit'])
            audit['cache_key'] = cache_key
            audit['cache_hit'] = 'live_recording'

    if base is None:
        base = {}
        if mint_on and mint:
            base = await asyncio.to_thread(calibrate_from_mint_history, mint, cutoff,
                                           db_path, lookback, tau_vr, tau_scale)
            chain_on = bool(explicit.get(
                'v2_chain_fetch_enable', DEFAULT_CONFIG['v2_chain_fetch_enable']))
            # Live may enrich an unseen mint on demand.  Ordinary sync
            # backtests must remain deterministic and offline; an explicit
            # v2_chain_fetch_enable=1 remains the research/opt-in escape hatch.
            chain_on = chain_on and (
                allow_chain_fetch or 'v2_chain_fetch_enable' in explicit)
            if not base and chain_on:
                from mint_chain_history import fetch_and_persist
                audit['chain_attempted'] = True
                try:
                    audit['chain_inserted'] = await asyncio.wait_for(
                        fetch_and_persist(mint, db_path=db_path, before_unix=cutoff,
                                          lookback_seconds=lookback), timeout=95.0)
                    base = await asyncio.to_thread(
                        calibrate_from_mint_history, mint, cutoff,
                        db_path, lookback, tau_vr, tau_scale)
                except Exception as exc:
                    audit['chain_error'] = type(exc).__name__
            if base:
                audit['source'] = 'mint_history'
        if not base:
            base = await asyncio.to_thread(calibrate_from_population, db_path, cutoff,
                                           tau_vr=tau_vr, tau_scale=tau_scale,
                                           source=popcal_source)
            audit['source'] = 'population' if any(k in base for k in _CLIP) else 'defaults'

    # Persist only this raw layer output at live.  Sourcing sentinels and the
    # caller's explicit parameters are deliberately re-derived on every load,
    # so a cached live start stays compatible with surgical backtest sweeps.
    audit['calibration_overrides'] = dict(base)
    if audit['source'] == 'mint_history':
        if not any(key in explicit for key in _CLIP):
            base['_calibration_sourced'] = 1
        if not _geom_user_explicit:
            base['_geometry_sourced'] = 1
    elif not _geom_user_explicit and any(k in base for k in GEOMETRY_KEYS):
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
                                db_path: str = _DB_PATH,
                                recording_id: int | None = None) -> tuple[dict, dict]:
    return asyncio.run(initialize_calibration(
        mint, started_at, params, db_path, recording_id=recording_id,
        allow_chain_fetch=False))
