"""Shared causal calibration initialization for live and recorded sessions.

Two callers, one ladder:

  * LIVE (``persist_result=True``): calibrate from the token's on-chain/DB
    history before the session start, then SNAPSHOT the raw layer output into
    ``calibration_init_cache`` so the session's own tape can be replayed from
    the identical starting physics.
  * REPLAY (``persist_result=False``, default): read that snapshot for the
    same (mint, anchor, lookback, estimator flags) key.  A hit is byte-equal
    to the live start.  A miss (old recordings predating the cache) falls
    through the SAME deterministic causal ladder — DB mint history →
    population → defaults — with NO chain fetch (public RPCs have pruned the
    tape by then, and batch workers must stay offline/deterministic).  An
    explicit ``v2_chain_fetch_enable`` re-enables the fetch as an escape
    hatch; replay never writes the cache.

The live anchor is the auto-recording's ``started_at`` (main.py passes the
same timestamp to both), so live and replay derive the same cache key.
"""
from __future__ import annotations

import asyncio
import hashlib
import math

from data_store import (
    load_calibration_init_cache, save_calibration_init_cache,
)
from session_calibrator import (
    _DB_PATH, _CLIP, GEOMETRY_KEYS, calibrate_from_mint_history,
    calibrate_from_population,
)

# Lookback default: 100 minutes of the token's prior tape.
DEFAULT_CALIBRATION_LOOKBACK_SECONDS = 6000.0


def calibration_window(started_at: float, params: dict) -> tuple[float, float]:
    lag = float(params.get('v2_calibration_history_lag_seconds', 0.0))
    lookback = float(params.get(
        'v2_calibration_history_seconds', DEFAULT_CALIBRATION_LOOKBACK_SECONDS))
    if not math.isfinite(started_at) or started_at <= 0:
        raise ValueError('calibration start must be a positive timestamp')
    if not math.isfinite(lag) or lag < 0 or not math.isfinite(lookback) or lookback <= 0:
        raise ValueError('calibration history requires finite nonnegative lag and positive lookback')
    return float(math.floor(started_at - lag)), lookback


def _init_cache_key(mint: str, cutoff: float, lookback_seconds: float,
                    tau_vr: bool, tau_scale: float, popcal_source: str,
                    mint_enabled: bool) -> str:
    """Stable snapshot key covering every input that can change the BASE.

    The anchor second is part of the key: a different cutoff is a different
    calibration input, not a cache hit.  Policy/merge knobs (delays, gates,
    harvest switch) are merged over the snapshot per call and are not keyed.
    """
    raw = '|'.join((
        str(mint or ''),
        f'{float(cutoff):.0f}',
        repr(float(lookback_seconds)),
        '1' if tau_vr else '0',
        repr(float(tau_scale)),
        str(popcal_source or ''),
        '1' if mint_enabled else '0',
    ))
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()


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


def _finalize_base(base: dict, explicit: dict, audit: dict,
                   harvest_on: bool, geom_user_explicit: bool) -> dict:
    """Post-process a raw layer output identically for fresh and cached runs.

    Sentinels and the harvest master switch are RE-DERIVED per call (they are
    never stored in the snapshot), so a cached start merges byte-identically
    to the live one.  Mutates ``audit``; returns the merged engine kwargs.
    """
    base = dict(base or {})
    source = audit.get('source')
    if source == 'mint_history':
        if not any(key in explicit for key in _CLIP):
            base['_calibration_sourced'] = 1
        if not geom_user_explicit:
            base['_geometry_sourced'] = 1
    elif not geom_user_explicit and any(k in base for k in GEOMETRY_KEYS):
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
    audit['geometry_user_explicit'] = geom_user_explicit
    audit['source'] = source
    return {**base, **explicit}


def _snapshot_payload(base: dict) -> dict:
    """Raw layer output only — sentinels are re-derived, never cached."""
    return {k: v for k, v in (base or {}).items()
            if k not in ('_calibration_sourced', '_geometry_sourced')}


async def initialize_calibration(mint: str, started_at: float, params: dict | None = None,
                                 db_path: str = _DB_PATH,
                                 persist_result: bool = False) -> tuple[dict, dict]:
    """Resolve the session's tick-0 calibration.

    ``persist_result=True`` marks the LIVE call: chain history is allowed by
    default and the resolved raw output is snapshotted into
    ``calibration_init_cache`` (never on failure).  The default
    (``persist_result=False``) is the REPLAY call: read the snapshot, else
    recompute deterministically offline.
    """
    from strategy_engineV2 import DEFAULT_CONFIG

    explicit = dict(params or {})
    _strip_default_sde(explicit, DEFAULT_CONFIG)
    enabled = bool(explicit.pop('use_session_calibration', explicit.get(
        'v2_popcal_enable', DEFAULT_CONFIG['v2_popcal_enable'])))
    cutoff, lookback = calibration_window(float(started_at), explicit)
    audit = {'started_at': float(started_at), 'cutoff': cutoff,
             'window_start': cutoff - lookback, 'source': 'defaults',
             'chain_attempted': False, 'chain_inserted': 0,
             'persist_result': bool(persist_result)}
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
    popcal_source = str(explicit.get('v2_popcal_source',
                                     DEFAULT_CONFIG.get('v2_popcal_source',
                                                        'pool_all')))
    audit['popcal_source'] = popcal_source

    mint_on = bool(explicit.get('v2_mintcal_enable', DEFAULT_CONFIG['v2_mintcal_enable']))
    cache_key = _init_cache_key(mint, cutoff, lookback, tau_vr, tau_scale,
                                popcal_source, mint_on)
    audit['cache_key'] = cache_key

    # ── replay: the live session's own snapshot wins ──────────────────────
    base: dict = {}
    if not persist_result:
        cached = load_calibration_init_cache(db_path, cache_key)
        if cached is not None:
            base = dict(cached.get('overrides') or {})
            audit['source'] = cached.get('source') or 'defaults'
            audit['cache_hit'] = 1
            audit['cache_created_at'] = cached.get('created_at')

    # ── fresh ladder: DB mint history → (live) chain fetch → population ───
    if not base:
        if mint_on and mint:
            base = await asyncio.to_thread(calibrate_from_mint_history, mint, cutoff,
                                          db_path, lookback, tau_vr, tau_scale)
            # Live calibrates now, while the tape still exists on chain.
            # Replay is offline by default (the RPC has pruned it anyway);
            # an explicit v2_chain_fetch_enable is the escape hatch.
            if 'v2_chain_fetch_enable' in explicit:
                chain_on = bool(explicit['v2_chain_fetch_enable'])
            else:
                chain_on = bool(persist_result
                                and DEFAULT_CONFIG['v2_chain_fetch_enable'])
            if not base and chain_on:
                from mint_chain_history import fetch_and_persist
                audit['chain_attempted'] = True
                try:
                    # Budget: a session must not wait minutes on a chain
                    # walk that the public RPC throttles anyway.  Whatever
                    # surface is fetched inside the budget is kept (the scan
                    # returns its newest gap-free prefix), and a shortfall
                    # still falls through to population below.
                    # ~700 signatures newest-first ≈ 3-4 minutes of a HOT
                    # token's tape (~40 s) and, for quiet tokens, the whole
                    # window (the signature walk stops as soon as a page
                    # predates it).  The scan keeps its newest gap-free prefix
                    # on budget exhaustion, so a throttled provider cannot
                    # stall the launch — but the fetch is still bounded: the
                    # public RPCs sustain only ~15-20 getTransaction/s, so
                    # asking for the full 1000 pushed a launch past 45 s.
                    audit['chain_inserted'] = await asyncio.wait_for(
                        fetch_and_persist(mint, max_sigs=700, db_path=db_path,
                                          before_unix=cutoff,
                                          lookback_seconds=lookback,
                                          timeout_s=40.0), timeout=55.0)
                    base = await asyncio.to_thread(calibrate_from_mint_history, mint,
                                                  cutoff, db_path, lookback,
                                                  tau_vr, tau_scale)
                except Exception as exc:
                    audit['chain_error'] = type(exc).__name__
            if base:
                audit['source'] = 'mint_history'
        if not base:
            base = await asyncio.to_thread(calibrate_from_population, db_path, cutoff,
                                           tau_vr=tau_vr, tau_scale=tau_scale,
                                           source=popcal_source)
            audit['source'] = ('population' if any(k in base for k in _CLIP)
                               else 'defaults')

    merged = _finalize_base(base, explicit, audit, harvest_on, _geom_user_explicit)

    # ── live: snapshot the raw start (never cached on replay, never fatal) ─
    if persist_result and 'cache_hit' not in audit:
        snapshot = _snapshot_payload(base)
        snapshot_audit = {k: v for k, v in audit.items() if k != 'cache_key'}
        try:
            if save_calibration_init_cache(
                    db_path, cache_key, mint, cutoff, audit['source'],
                    snapshot, snapshot_audit):
                audit['cache_written'] = 1
        except Exception as exc:  # a cache write must never break a launch
            audit['cache_error'] = type(exc).__name__
    return merged, audit


def initialize_calibration_sync(mint: str, started_at: float, params: dict | None = None,
                                db_path: str = _DB_PATH) -> tuple[dict, dict]:
    """Replay entry point: cache first, deterministic offline ladder on miss."""
    return asyncio.run(initialize_calibration(mint, started_at, params, db_path))
