"""
Engine factory: builds the strategy engine for a requested `engine_version`.

The single indirection point between the execution pipelines and the engines:

    create_engine(engine_version=1, **kwargs)   # -> V1 StrategyEngine
    create_engine(engine_version=2, **kwargs)   # -> V2 StrategyEngineV2Adapter
    create_engine(engine_version=3, **kwargs)   # -> V3 (dump-bottom on the V2 core)
    create_engine(engine_version=4, **kwargs)   # -> V4 (post-nuke reversion)
    create_engine(engine_version=6, **kwargs)   # -> V6 (insider-dump absorption)

Every engine exposes the same call surface used by the pipelines:
update(), notify_trade_opened(), notify_trade_closed(), plus the V1
indicator attributes.  See `engines/adapter_base.py` for the shared surface.
"""

from __future__ import annotations
import inspect
from typing import Any

# V1 is always importable (pure Python).
from strategy_engine import StrategyEngine

# Adapter modules import heavy optional deps (numba, scipy), so they load
# lazily on first use; the resolved class refs are cached module-side.
_ADAPTER_PATHS = {
    2: ("strategy_engineV2", "StrategyEngineV2Adapter"),
    3: ("strategy_engineV3", "StrategyEngineV3Adapter"),
    4: ("strategy_engineV4", "StrategyEngineV4Adapter"),
    6: ("strategy_engineV6", "StrategyEngineV6Adapter"),
}
_ADAPTER_CLASSES: dict[int, type] = {}


def _load_adapter(engine_version: int) -> type:
    """Import and cache the adapter class for a non-V1 engine version."""
    cls = _ADAPTER_CLASSES.get(engine_version)
    if cls is None:
        module_name, class_name = _ADAPTER_PATHS[engine_version]
        module = __import__(module_name, fromlist=[class_name])
        cls = getattr(module, class_name)
        _ADAPTER_CLASSES[engine_version] = cls
    return cls


def create_engine(engine_version: int = 1, **engine_kwargs: Any):
    """
    Return a fresh strategy engine instance for the requested version.

    1 → V1 `StrategyEngine` (physics / Langevin / Kalman regime detector);
    2 → V2 `StrategyEngineV2Adapter` (RBPF + UKF + KDE + Kramers escape,
    wrapped to the V1 surface); 3/4/6 → the specialised adapters.

    Free parameters pass straight through to the chosen engine constructor.
    V1 ignores unknown V2 keys (and vice versa); the V2 adapter silently
    filters its kwargs against `DEFAULT_CONFIG` so a mixed bag is safe.
    """
    if engine_version is None:
        engine_version = 1
    engine_version = int(engine_version)

    if engine_version == 1:
        sig = inspect.signature(StrategyEngine.__init__)
        v1_valid = set(sig.parameters.keys()) - {"self"}
        filtered = {k: v for k, v in engine_kwargs.items() if k in v1_valid}
        return StrategyEngine(**filtered)

    if engine_version in _ADAPTER_PATHS:
        Adapter = _load_adapter(engine_version)
        return Adapter(**engine_kwargs)

    raise ValueError(
        f"Unknown engine_version={engine_version!r} (expected 1, 2, 3, 4 or 6)"
    )


# Convenience alias referenced from call sites that prefer the
# `build_engine(...)` spelling.
build_engine = create_engine
