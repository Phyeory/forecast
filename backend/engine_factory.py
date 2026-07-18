"""
Engine factory — picks the right strategy engine class based on `engine_version`.

The three execution pipelines (Backtester, ForwardTester, LiveTrader) plus
the FastAPI layer in `main.py` all build a strategy engine via
`create_engine(engine_version, **engine_kwargs)`.  This is the single
indirection point that decides V1 vs V2.

Contract:
    eng = create_engine(engine_version=1, **kwargs)   # → V1 StrategyEngine
    eng = create_engine(engine_version=2, **kwargs)   # → V2 StrategyEngineV2Adapter

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


def create_engine(engine_version: int = 1, **engine_kwargs: Any):
    """
    Return a fresh strategy engine instance for the requested version.

    Args
    ----
    engine_version : int
        1 → V1 `StrategyEngine` (physics / Langevin / Kalman regime detector)
        2 → V2 `StrategyEngineV2Adapter`
            (RBPF + UKF + KDE + Kramers escape, wrapped to V1 surface)
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
        return StrategyEngine(**engine_kwargs)

    if engine_version == 2:
        Adapter = _load_v2_adapter()
        return Adapter(**engine_kwargs)

    raise ValueError(
        f"Unknown engine_version={engine_version!r} (expected 1 or 2)"
    )


# Convenience alias documented in strategy_engineV2.spec and referenced
# from call sites that prefer the `build_engine(...)` spelling.
build_engine = create_engine
