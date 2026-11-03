"""Golden-capture harness for the behaviour-preserving refactor.

Runs one backtest cell (recording x engine version x exec model) through the
unmodified production pipeline with only its output sinks patched, and
returns a canonical JSON-safe dict of every observable result.  Shared by
golden_capture.py (baseline writing, candidate scanning) and golden_verify.py
(re-run and diff against the committed baseline).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys
from types import SimpleNamespace
from typing import Any, Optional

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

GOLDEN_DIR = os.path.join(BACKEND_DIR, "tools", "golden")
BASELINE_DIR = os.path.join(GOLDEN_DIR, "baseline")
SCRATCH_DIR = os.path.join(GOLDEN_DIR, "scratch")
RECORDINGS_JSON = os.path.join(GOLDEN_DIR, "recordings.json")

# Belt and braces: no patched sink ever writes, but nothing downstream may
# land in the research result dirs either.
os.environ.setdefault("BACKTEST_RESULTS_DIR", SCRATCH_DIR)

ENGINES = (1, 2, 3, 4, 6)
EXEC_MODELS = ("instant", "legacy", "latency")
# The latency cell pins explicit deferrals so the path-priced fill overlay is
# exercised independently of engine-keyed delay knobs.
LATENCY_KWARGS = {"entry_latency_seconds": 5.0, "exit_latency_seconds": 10.0}

# Golden cells must never touch the network or mint_history_candles: the
# production default enables a bounded chain fetch whenever a mint lacks
# local history, which would mutate the shared DB mid-capture.  Applied to
# V2 cells only (calibration is V2-only in run_backtest).
GOLDEN_ENGINE_PARAMS = {"v2_chain_fetch_enable": 0.0}


# ---------------------------------------------------------------------------
# Canonical serialisation

def _stringify_keys(obj: Any) -> Any:
    """Recursively convert dict keys to str and sets to sorted lists."""
    if isinstance(obj, dict):
        return {str(k): _stringify_keys(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_stringify_keys(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((repr(v) for v in obj))
    return obj


def canonical_json(obj: Any) -> str:
    """Deterministic JSON text: sorted keys, full-precision floats, repr
    fallback for exotic values.  The float round-trip through repr is exact,
    so equal doubles always produce equal text."""
    return json.dumps(_stringify_keys(obj), sort_keys=True, default=repr)


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def first_diff(baseline: Any, candidate: Any, path: str = "$") -> Optional[str]:
    """Return the first differing JSON path, or None when identical.
    Floats compare bit-exactly (repr round-trip preserves the double)."""
    if type(baseline) is not type(candidate):
        return (f"{path}: type {type(baseline).__name__} != "
                f"{type(candidate).__name__}")
    if isinstance(baseline, dict):
        for key in sorted(set(baseline) | set(candidate)):
            if key not in baseline:
                return f"{path}.{key}: missing in baseline"
            if key not in candidate:
                return f"{path}.{key}: missing in candidate"
            diff = first_diff(baseline[key], candidate[key], f"{path}.{key}")
            if diff:
                return diff
        return None
    if isinstance(baseline, list):
        if len(baseline) != len(candidate):
            return f"{path}: len {len(baseline)} != {len(candidate)}"
        for i, (b, c) in enumerate(zip(baseline, candidate)):
            diff = first_diff(b, c, f"{path}[{i}]")
            if diff:
                return diff
        return None
    if baseline != candidate:
        return f"{path}: {baseline!r} != {candidate!r}"
    return None


# ---------------------------------------------------------------------------
# Output-sink patches (installed at import in every capture/verify process)

_HARNESS: dict[str, Any] = {"proxy": None}


class _UpdateHashProxy:
    """Transparent engine wrapper that hashes every update() result dict."""

    __slots__ = ("_engine", "_digest")

    def __init__(self, engine: Any):
        self._engine = engine
        self._digest = hashlib.sha256()

    def update(self, *args: Any, **kwargs: Any) -> Any:
        result = self._engine.update(*args, **kwargs)
        self._digest.update(canonical_json(result).encode("utf-8"))
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._engine, name)


import backtester as _backtester  # noqa: E402
import forward_tester as _forward_tester  # noqa: E402

_original_create_engine = _forward_tester.create_engine
_original_create_backtest = _backtester.create_backtest
_original_write_trade_log = _backtester._write_trade_log


def _hashing_create_engine(engine_version: int = 1, **engine_kwargs: Any) -> Any:
    engine = _original_create_engine(engine_version, **engine_kwargs)
    proxy = _UpdateHashProxy(engine)
    _HARNESS["proxy"] = proxy
    return proxy


def _capture_create_backtest(**kwargs: Any) -> None:
    _HARNESS["candle_series_sha256"] = sha256_of(kwargs.get("candle_results") or [])
    _HARNESS["candle_series_count"] = len(kwargs.get("candle_results") or [])
    _HARNESS["engine_params"] = kwargs.get("engine_params")
    return None


def _capture_write_trade_log(**kwargs: Any) -> str:
    _HARNESS["trades"] = kwargs.get("trades") or []
    return ""


_forward_tester.create_engine = _hashing_create_engine
_backtester.create_backtest = _capture_create_backtest
_backtester._write_trade_log = _capture_write_trade_log


# ---------------------------------------------------------------------------
# Backtest cell runner

def run_cell(task: tuple) -> dict:
    """Run one (recording_id, engine_version, exec_model) cell and return
    every observable output as a canonical JSON-safe dict."""
    recording_id, engine_version, exec_model = task
    _HARNESS["proxy"] = None
    _HARNESS.pop("candle_series_sha256", None)
    _HARNESS.pop("candle_series_count", None)
    _HARNESS.pop("engine_params", None)
    _HARNESS["trades"] = []

    exec_kwargs = dict(LATENCY_KWARGS) if exec_model == "latency" else {}
    engine_params = dict(GOLDEN_ENGINE_PARAMS) if engine_version == 2 else {}
    summary = _backtester.run_backtest(
        recording_id=recording_id,
        engine_version=engine_version,
        engine_params=engine_params,
        exec_model=exec_model,
        persist_results=True,
        persist_candles=True,
        **exec_kwargs,
    )
    proxy = _HARNESS.get("proxy")
    return {
        "cell": {
            "recording_id": recording_id,
            "engine_version": engine_version,
            "exec_model": exec_model,
            "entry_latency_seconds": exec_kwargs.get("entry_latency_seconds", 0.0),
            "exit_latency_seconds": exec_kwargs.get("exit_latency_seconds", 0.0),
        },
        "engine_params": _HARNESS.get("engine_params") or {},
        "calibration_audit": summary.get("calibration") or {},
        "calibration_updates": summary.get("calibration_updates") or [],
        "stats": summary.get("stats") or {},
        "trade_count": summary.get("trade_count", 0),
        "trades": _HARNESS.get("trades") or [],
        "candle_series_sha256": _HARNESS.get("candle_series_sha256"),
        "candle_series_count": _HARNESS.get("candle_series_count"),
        "engine_update_sha256": proxy._digest.hexdigest() if proxy else None,
    }


def cell_path(recording_id: int, engine_version: int, exec_model: str) -> str:
    return os.path.join(
        BASELINE_DIR, str(recording_id), f"v{engine_version}", f"{exec_model}.json"
    )


def collect_reasons(results: list[dict]) -> dict:
    """Sorted exit/entry reason sets across a list of cell results."""
    exits: set[str] = set()
    entries: set[str] = set()
    for result in results:
        for trade in result.get("trades", []):
            if trade.get("exit_reason"):
                exits.add(str(trade["exit_reason"]))
            if trade.get("entry_reason"):
                entries.add(str(trade["entry_reason"]))
    return {"exit_reasons": sorted(exits), "entry_reasons": sorted(entries)}


# ---------------------------------------------------------------------------
# Pure-function fixtures (no DB, no network)

def _pure_fill_functions() -> dict:
    from forward_tester import ForwardTester
    import live_trader as lt

    candles = [
        (1.0, 2.0, 0.5, 1.5),      # bull
        (1.0, 1.6, 0.4, 0.7),      # bear
        (1.0, 1.0, 1.0, 1.0),      # doji
        (2.0, 2.0, 1.0, 2.0),      # open == high == close
        (2.0, 3.0, 2.0, 2.0),      # open == low == close
        (1.0, 1.5, 0.5, 1.0),      # close == open, both extremes real
        (0.001, 0.0099, 0.0002, 0.0091),
    ]
    fracs = [i / 20 for i in range(21)] + [1 / 3, 2 / 3]
    rows = []
    for o, h, l, c in candles:
        for frac in fracs:
            ft_value = ForwardTester._intrabar_price(o, h, l, c, frac)
            lt_value = lt.LiveTrader._intrabar_price(o, h, l, c, frac)
            if ft_value != lt_value:
                raise AssertionError(
                    f"intrabar parity broken: ft={ft_value!r} lt={lt_value!r}"
                )
            rows.append([o, h, l, c, frac, ft_value])
    section = {"intrabar_price": rows}

    ft = ForwardTester(slippage_pct=1.0, engine_version=1)
    fill_rows = []
    for lamports in (100000, 205000, 1000000):
        for buy_size in (0.1, 0.3, 1.0):
            for slip in (1.0, 10.0, 25.0):
                ft.priority_fee = lamports / 1e9
                ft.bribe_fee = 0.0
                ft.buy_size_sol = buy_size
                ft.slippage_pct = slip
                ft_value = ft._fill_fraction()
                lt_self = SimpleNamespace(
                    priority_fee_lamports=lamports,
                    buy_size_sol=buy_size,
                    engine_fill_slippage_pct=slip,
                )
                lt_value = lt.LiveTrader._fill_fraction(lt_self)
                if ft_value != lt_value:
                    raise AssertionError(
                        f"fill_fraction parity broken: ft={ft_value!r} "
                        f"lt={lt_value!r}"
                    )
                fill_rows.append([lamports, buy_size, slip, ft_value])
    section["fill_fraction"] = fill_rows

    path = [(1000 + i, 1.0 + i * 0.01, 1.5 + i * 0.01, 0.8, 1.2 + i * 0.01)
            for i in range(4)]
    ft._exec_mode = "latency"
    ft._path_candles = {}
    ft._path_times = []
    for t, o, h, l, c in path:
        ft._record_path_candle(t, o, h, l, c)
    lt_self = SimpleNamespace(
        _fill_path_candles=ft._path_candles,
        _fill_path_times=ft._path_times,
        _intrabar_price=lt.LiveTrader._intrabar_price,
    )
    path_rows = []
    for i in range(24):
        target = 1000.0 + i * 0.25
        ft_value = ft._path_price_at(target)
        lt_value = lt.LiveTrader._path_price_at(lt_self, target)
        if ft_value != lt_value:
            raise AssertionError(
                f"path_price parity broken: ft={ft_value!r} lt={lt_value!r}"
            )
        path_rows.append([target, ft_value])
    section["path_price_at"] = path_rows
    return section


def _pure_candle_aggregator() -> dict:
    from candle_aggregator import CandleAggregator

    section: dict[str, Any] = {}
    rng = random.Random(1234)
    for tf in ("1s", "5s", "15s"):
        aggregator = CandleAggregator(timeframe=tf, history_size=4096)
        closed: list[dict] = []
        aggregator.on_candle_close = lambda candle: closed.append(candle.to_dict())
        price = 0.00042
        pool_sol = 40.0
        timestamp = 1790000000.0
        for _ in range(500):
            price *= math.exp(rng.gauss(0.0, 0.02))
            pool_sol = max(1.0, pool_sol + rng.gauss(0.0, 0.5))
            volume = round(rng.uniform(1.0, 100.0), 6)
            aggregator.process_trade(
                price=price,
                volume=volume,
                timestamp=timestamp,
                synthetic=False,
                is_buy=rng.random() < 0.5,
                pool_sol=pool_sol,
                market_cap_usd=price * 1e9,
            )
            timestamp += 0.37
        section[tf] = {
            "closed": closed,
            "history_len": len(aggregator._history),
            "final_current": (aggregator.current_candle.to_dict()
                              if aggregator.current_candle else None),
        }
    return section


def _pure_effective_basis() -> dict:
    import copy
    import sqlite3

    from pool_virtual_reserves import (
        PUMPSWAP_VR_FIX_EPOCH,
        _graduation_boundary,
        correct_candles_to_effective_basis,
        wedge_factor,
    )
    import pool_virtual_reserves as pvr

    db_path = os.path.join(BACKEND_DIR, "data", "price_data.db")
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT mint, v_sol, pair_created_at FROM pool_virtual_resolves "
        "WHERE v_sol > 0 AND pair_created_at IS NOT NULL "
        "AND pair_created_at < 1790395000.0 ORDER BY mint LIMIT 1"
    ).fetchone()
    con.close()
    if row is None:
        raise RuntimeError("no sidecar-resolved mint available for the fixture")
    mint, v_sol, pair_created_at = row[0], float(row[1]), float(row[2])
    boundary = _graduation_boundary(pair_created_at)
    section: dict[str, Any] = {
        "mint": mint, "v_sol": v_sol,
        "pair_created_at": pair_created_at, "boundary": boundary,
    }

    def synth(start: float, count: int, vault_base: float) -> list[dict]:
        out = []
        for i in range(count):
            t = int(start) + i
            base = 0.0004 + 0.00001 * i
            out.append({
                "time": t,
                "open": base,
                "high": base * 1.05,
                "low": base * 0.96,
                "close": base * 1.01,
                "volume": 10.0 + i,
                "buy_volume": 6.0 + i,
                "sell_volume": 4.0,
                "pool_sol": vault_base + i * 0.5,
                "market_cap_usd": base * 1e9,
            })
        return out

    candles = synth(boundary - 10, 10, 40.0) + synth(boundary, 10, 300.0)
    corrected = correct_candles_to_effective_basis(
        copy.deepcopy(candles), mint, candles[0]["time"])
    section["corrected"] = corrected
    section["corrected_count"] = sum(
        1 for c in corrected if c.get("price_basis") == "effective")
    section["value_changed_count"] = sum(
        1 for a, b in zip(candles, corrected)
        if any(a[k] != b[k] for k in ("open", "high", "low", "close")))

    post_fix = correct_candles_to_effective_basis(
        copy.deepcopy(candles), mint, float(PUMPSWAP_VR_FIX_EPOCH) + 10.0)
    section["post_fix_is_input"] = post_fix is not None
    section["post_fix_unchanged"] = (post_fix == candles)

    saved_disable = pvr._DISABLE
    try:
        pvr._DISABLE = True
        disabled = correct_candles_to_effective_basis(
            copy.deepcopy(candles), mint, candles[0]["time"])
    finally:
        pvr._DISABLE = saved_disable
    section["disabled_unchanged"] = (disabled == candles)

    section["wedge_factors"] = [
        [vault, wedge_factor(vault, v_sol)]
        for vault in (1.0, 5.0, 17.5845, 40.0, 85.0, 300.0, 1000.0)
    ]
    return section


def capture_pure() -> dict:
    """All cheap no-DB fixtures in one dict."""
    return {
        "fill_functions": _pure_fill_functions(),
        "candle_aggregator": _pure_candle_aggregator(),
        "effective_basis": _pure_effective_basis(),
    }
