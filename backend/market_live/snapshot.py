"""snapshot.py — engine-state snapshots for fast, exact resume.

The live stack's warm (replaying the whole tape through the engine) costs
~5-6 min for the 67-symbol universe.  That is fine on a workstation but
fatal on a slow/free host with a short task limit — and it is pure waste on
every restart.

The engine state is picklable: `MarketEngineHost` (engine + ForwardTester
book) round-trips through `pickle` and, when restored, produces
DECISION-IDENTICAL results to a fresh warm on the same tape (verified in
`market_live/test_snapshot.py`).

So the runner can:
  1. load the snapshot (67 symbols ≈ 13 MB raw, ~4 MB gzipped, unpickle
     in milliseconds),
  2. process only the bars that closed since the snapshot's cursor,
  3. write a fresh snapshot.

A snapshot is only valid for the code that produced it — the fingerprint of
the engine files is stored with it, and any mismatch falls back to a full
warm (correctness over speed).
"""
from __future__ import annotations

import gzip
import hashlib
import json
import pickle
from pathlib import Path
from typing import Optional

SNAPSHOT_VERSION = 2

# Files whose contents define the engine's decisions.  A change to any of
# them invalidates every snapshot.
_CODE_FILES = (
    "market_backtest/strategy_engineV2M.py",
    "forward_tester.py",
    "market_backtest/market_calibration.py",
    "market_live/host.py",
)


def code_fingerprint(root: Optional[Path] = None) -> str:
    """sha256 over the engine-defining files (order-stable)."""
    root = Path(root or Path(__file__).resolve().parent.parent)
    h = hashlib.sha256()
    for rel in _CODE_FILES:
        p = root / rel
        h.update(rel.encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:16]


def params_fingerprint(params: Optional[dict]) -> str:
    """sha256 over a symbol's frozen engine params."""
    if not params:
        return "none"
    try:
        blob = json.dumps(params, sort_keys=True, default=str).encode()
    except Exception:  # noqa: BLE001
        return "unhashable"
    return hashlib.sha256(blob).hexdigest()[:16]


def save_snapshot(trader, path: str | Path) -> bool:
    """Persist the per-symbol engine state + cursor.  Returns success."""
    p = Path(path)
    try:
        payload = {
            "version": SNAPSHOT_VERSION,
            "code_fp": code_fingerprint(),
            "symbols": {
                sym: {
                    "host": host,                 # pickled MarketEngineHost
                    "last_ts": int(trader.last_ts.get(sym, 0)),
                    "price": float(trader.prices.get(sym, 0.0)),
                    "params_fp": params_fingerprint(
                        (getattr(trader, "_frozen", {}) or {}).get(sym)),
                }
                for sym, host in trader.hosts.items()
            },
        }
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with gzip.open(tmp, "wb", compresslevel=6) as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(p)                          # atomic swap
        return True
    except Exception as exc:  # noqa: BLE001 — never break the live loop
        if getattr(trader, "verbose", False):
            trader._log(f"snapshot save failed: {type(exc).__name__}: {exc}")
        return False


def load_snapshot(trader, path: str | Path) -> set[str]:
    """Restore engine state into `trader`.  Returns the symbols restored."""
    p = Path(path)
    if not p.exists():
        return set()
    try:
        with gzip.open(p, "rb") as fh:
            payload = pickle.load(fh)
    except Exception as exc:  # noqa: BLE001
        if getattr(trader, "verbose", False):
            trader._log(f"snapshot unreadable ({type(exc).__name__}) — warming")
        return set()
    if not isinstance(payload, dict):
        return set()
    if int(payload.get("version") or 0) != SNAPSHOT_VERSION:
        return set()
    if payload.get("code_fp") != code_fingerprint():
        if getattr(trader, "verbose", False):
            trader._log("snapshot code fingerprint changed — warming instead")
        return set()

    restored: set[str] = set()
    frozen = getattr(trader, "_frozen", {}) or {}
    for sym, rec in (payload.get("symbols") or {}).items():
        if sym not in trader.cfg.symbols:
            continue
        try:
            # the frozen params must match, or the snapshot is stale
            want = params_fingerprint(frozen.get(sym))
            if rec.get("params_fp") and rec.get("params_fp") != want:
                continue
            host = rec["host"]
            trader.hosts[sym] = host
            trader.last_ts[sym] = int(rec.get("last_ts") or 0)
            trader.prices[sym] = float(rec.get("price") or 0.0)
            trader.desired[sym] = int(host.desired_position)
            restored.add(sym)
        except Exception:  # noqa: BLE001
            continue
    return restored
