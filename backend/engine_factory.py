"""
Engine factory — picks the right strategy engine class based on `engine_version`.

The three execution pipelines (Backtester, ForwardTester, LiveTrader) plus
the FastAPI layer in `main.py` all build a strategy engine via
`create_engine(engine_version, **engine_kwargs)`.  This is the single
indirection point that decides V1 vs V2.

    Contract:
        eng = create_engine(engine_version=1, **kwargs)   # → V1 StrategyEngine
        eng = create_engine(engine_version=2, **kwargs)   # → V2 StrategyEngineV2Adapter
        eng = create_engine(engine_version=3, **kwargs)   # → V3 StrategyEngineV3Adapter
            (V3 = newborn-coin dump-bottom engine on the V2 mathematical core)
        eng = create_engine(engine_version=4, **kwargs)   # → V4 StrategyEngineV4Adapter
            (V4 = post-nuke reversion: bounce-confirmed flush harvest, iter77)
        eng = create_engine(engine_version=6, **kwargs)   # → V6 StrategyEngineV6Adapter
            (V6 = insider-dump absorption entry, iter77)

Both V1 and V2 expose the EXACT same call surface used by the pipelines:
    eng.update(time, o, h, l, c, volume, buy_volume, sell_volume, ...)
    eng.notify_trade_opened(entry_price, direction)
    eng.notify_trade_closed()
All V1 indicator attributes (`m_hat`, `ema_fast_val`, `trend_confidence`, ...)
are also present on the V2 adapter.  See `StrategyEngineV2Adapter` docstring
in `strategy_engineV2.py` for the full mapping table.
"""

from __future__ import annotations
from typing import Any

# V1 — always importable (pure-Python).
from strategy_engine import StrategyEngine


# Lazy-import V2 — the module imports `numba` and `scipy` which are heavy and
# optional at runtime; bringing them in unconditionally on every `import
# engine_factory` would slow startup for V1-only runs and would crash the
# process if the wheels are not installed yet (e.g. cold boot, CI).  We use a
# module-level cache to keep the second call path-fast.
_V2Adapter = None
_V2_IMPORT_LOCK = False

# V3 (newborn dump-bottom engine) re-imports the same V2 core and is cached
# the same way.
_V3Adapter = None

# V4 (post-nuke reversion) / V6 (insider-dump absorption) — iter77 archetype
# engines; pure-Python standalone machines, cached the same way.
_V4Adapter = None
_V6Adapter = None


def _load_v2_adapter():
    """Import `StrategyEngineV2Adapter` lazily and cache the class ref."""
    global _V2Adapter, _V2_IMPORT_LOCK
    if _V2Adapter is not None:
        return _V2Adapter
    if _V2_IMPORT_LOCK:
        # Re-entrant guard — would only happen if a misbehaving caller
        # constructed a circular import.  Defensive hard-stop.
        raise RuntimeError("Recursive V2 adapter import detected")
    _V2_IMPORT_LOCK = True
    try:
        from strategy_engineV2 import StrategyEngineV2Adapter  # noqa: WPS433
        _V2Adapter = StrategyEngineV2Adapter
        return _V2Adapter
    finally:
        _V2_IMPORT_LOCK = False


def _load_v3_adapter():
    """Import `StrategyEngineV3Adapter` lazily and cache the class ref."""
    global _V3Adapter
    if _V3Adapter is not None:
        return _V3Adapter
    from strategy_engineV3 import StrategyEngineV3Adapter  # noqa: WPS433
    _V3Adapter = StrategyEngineV3Adapter
    return _V3Adapter


def _load_v4_adapter():
    """Import `StrategyEngineV4Adapter` lazily and cache the class ref."""
    global _V4Adapter
    if _V4Adapter is not None:
        return _V4Adapter
    from strategy_engineV4 import StrategyEngineV4Adapter  # noqa: WPS433
    _V4Adapter = StrategyEngineV4Adapter
    return _V4Adapter


def _load_v6_adapter():
    """Import `StrategyEngineV6Adapter` lazily and cache the class ref."""
    global _V6Adapter
    if _V6Adapter is not None:
        return _V6Adapter
    from strategy_engineV6 import StrategyEngineV6Adapter  # noqa: WPS433
    _V6Adapter = StrategyEngineV6Adapter
    return _V6Adapter


def create_engine(engine_version: int = 1, **engine_kwargs: Any):
    """
    Return a fresh strategy engine instance for the requested version.

    Args
    ----
    engine_version : int
        1 → V1 `StrategyEngine` (physics / Langevin / Kalman regime detector)
        2 → V2 `StrategyEngineV2Adapter`
            (RBPF + UKF + KDE + Kramers escape, wrapped to V1 surface)
        3 → V3 `StrategyEngineV3Adapter`
            (newborn-coin dump-bottom recovery on the V2 core: launch →
            dump → bottom → organic-buyer entry, strict TP/SL + mcap band)
    **engine_kwargs : Any
        Free parameters passed straight through to the chosen engine
        constructor.  V1 ignores unknown V2 keys (and vice-versa); the V2
        adapter silently filters its kwargs against `DEFAULT_CONFIG` so
        passing a mixed bag is safe.

    Returns
    -------
    StrategyEngine | StrategyEngineV2Adapter   (both expose the V1 contract)
    """
    if engine_version is None:
        engine_version = 1
    engine_version = int(engine_version)

    if engine_version == 1:
        import inspect
        sig = inspect.signature(StrategyEngine.__init__)
        v1_valid = set(sig.parameters.keys()) - {"self"}
        filtered = {k: v for k, v in engine_kwargs.items() if k in v1_valid}
        return StrategyEngine(**filtered)

    if engine_version == 2:
        Adapter = _load_v2_adapter()
        return Adapter(**engine_kwargs)

    if engine_version == 3:
        Adapter = _load_v3_adapter()
        return Adapter(**engine_kwargs)

    if engine_version == 4:
        Adapter = _load_v4_adapter()
        return Adapter(**engine_kwargs)

    if engine_version == 6:
        Adapter = _load_v6_adapter()
        return Adapter(**engine_kwargs)

    raise ValueError(
        f"Unknown engine_version={engine_version!r} (expected 1, 2, 3, 4 or 6)"
    )


# Convenience alias documented in strategy_engineV2.spec and referenced
# from call sites that prefer the `build_engine(...)` spelling.
build_engine = create_engine
