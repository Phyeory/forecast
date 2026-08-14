#!/usr/bin/env python3
"""
Bit-identity parity harness for strategy_engineV2 optimisation work.

AGENTS.md requires that Backtester / ForwardTester / LiveTrader evolve engine
state identically, and that both engines stay strictly deterministic.  When
optimising the V2 hot path we therefore need to PROVE that a change is purely
a performance change and not a behaviour change.

Usage
-----
    # 1. BEFORE touching the engine, record a baseline fingerprint:
    python backend/tools/parity_check.py --save baseline --limit 12

    # 2. After optimising, verify nothing moved:
    python backend/tools/parity_check.py --check baseline --limit 12

Determinism note
----------------
`RaoBlackwellisedParticleFilter._systematic_resample` draws its systematic
offset from the UNSEEDED global numpy RNG, so two runs of the same recording
are not naturally reproducible.  This harness seeds `np.random` immediately
before every recording so the random stream is pinned and any difference in
the fingerprint is attributable to the code change under test, not RNG drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

RNG_SEED = 20240611

# Keys whose values are wall-clock / environment dependent and therefore must
# not participate in the behavioural fingerprint.
_VOLATILE_KEYS = {
    "id", "backtest_id", "created_at", "finished_at", "started_at",
    "duration_seconds", "elapsed", "runtime_seconds", "batch_id", "label",
    "wall_time", "timestamp_utc",
}


def _canonical(obj):
    """Recursively normalise a result payload into a stable, hashable form.

    Floats are serialised with `repr` so the comparison is exact to the last
    bit — a 1-ULP change in the Kelly fraction WILL be caught.
    """
    if isinstance(obj, dict):
        return {k: _canonical(v) for k, v in sorted(obj.items())
                if k not in _VOLATILE_KEYS}
    if isinstance(obj, (list, tuple)):
        return [_canonical(v) for v in obj]
    if isinstance(obj, float):
        if obj != obj:          # NaN
            return "nan"
        return repr(obj)
    if isinstance(obj, (np.floating,)):
        return repr(float(obj))
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return [_canonical(v) for v in obj.tolist()]
    return obj


_STREAM_FIELDS = ("x", "mu", "h", "phi", "ell", "var_phi", "regime")


class _StreamRecorder:
    """Hooks the V2 core so every tick's latent state + decision is captured.

    Trade-level output alone is a weak parity signal: on quiet recordings the
    engine may open zero positions, leaving the RBPF / potential / Kramers math
    completely unverified.  Hashing the per-tick latent vector and decision
    payload exercises the entire hot path on every single tick instead.
    """

    def __init__(self):
        self.ticks: list = []
        self._orig_update = None
        self._orig_decision = None

    def __enter__(self):
        import strategy_engineV2 as v2
        eng = v2.MemecoinStrategyEngine
        self._orig_update = eng.update_state
        self._orig_decision = getattr(eng, "get_decision", None)
        rec = self.ticks

        def update_state(inner, obs):
            out = self._orig_update(inner, obs)
            rec.append(("s", [_canonical(out.get(k)) for k in _STREAM_FIELDS]))
            return out
        eng.update_state = update_state

        if self._orig_decision is not None:
            def get_decision(inner, *a, **k):
                out = self._orig_decision(inner, *a, **k)
                rec.append(("d", _canonical(out)))
                return out
            eng.get_decision = get_decision
        return self

    def __exit__(self, *exc):
        import strategy_engineV2 as v2
        eng = v2.MemecoinStrategyEngine
        eng.update_state = self._orig_update
        if self._orig_decision is not None:
            eng.get_decision = self._orig_decision
        return False


def fingerprint_recording(recording_id: int, engine_version: int = 2) -> dict:
    """Run one backtest under a pinned RNG and return its behavioural digest."""
    from backtester import run_backtest

    np.random.seed(RNG_SEED)
    with _StreamRecorder() as rc:
        res = run_backtest(
            recording_id=recording_id,
            engine_version=engine_version,
            batch_id="__parity__",
        )
        stream = rc.ticks

    canon = _canonical(res)
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    sblob = json.dumps(stream, sort_keys=True, separators=(",", ":"))
    return {
        "recording_id": recording_id,
        "sha256": hashlib.sha256(blob.encode()).hexdigest(),
        "stream_sha256": hashlib.sha256(sblob.encode()).hexdigest(),
        "stream_len": len(stream),
        "candle_count": res.get("candle_count"),
        "trade_count": len(res.get("trades") or []),
        "total_pnl": repr(float(res.get("total_pnl_sol") or 0.0)),
        "canon": canon,
        # Retained so a mismatch can be localised to an exact tick index.
        "stream": stream,
    }


def build(limit: int, engine_version: int) -> dict:
    import data_store as ds

    recs = [r for r in ds.list_recordings() if r["status"] == "completed"]
    recs.sort(key=lambda r: r["id"])
    if limit:
        recs = recs[:limit]
    out = {"engine_version": engine_version, "seed": RNG_SEED, "records": {}}
    for i, r in enumerate(recs, 1):
        fp = fingerprint_recording(r["id"], engine_version)
        out["records"][str(r["id"])] = fp
        print(f"  [{i}/{len(recs)}] rec={r['id']:<5} "
              f"candles={fp['candle_count']:<6} ticks={fp['stream_len']:<6} "
              f"trades={fp['trade_count']:<4} "
              f"res={fp['sha256'][:12]} stream={fp['stream_sha256'][:12]}")
    return out


def _path(name: str) -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parity")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{name}.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", metavar="NAME")
    ap.add_argument("--check", metavar="NAME")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--engine-version", type=int, default=2)
    a = ap.parse_args()

    if not a.save and not a.check:
        ap.error("one of --save / --check is required")

    print(f"Building fingerprints (engine v{a.engine_version}, "
          f"limit={a.limit}, seed={RNG_SEED}) ...")
    cur = build(a.limit, a.engine_version)

    if a.save:
        with open(_path(a.save), "w") as f:
            json.dump(cur, f, indent=1, sort_keys=True)
        print(f"\nSaved baseline -> {_path(a.save)}")
        return 0

    with open(_path(a.check)) as f:
        base = json.load(f)

    bad, missing = [], []
    for rid, fp in cur["records"].items():
        ref = base["records"].get(rid)
        if ref is None:
            missing.append(rid)
            continue
        if (ref["sha256"] != fp["sha256"]
                or ref.get("stream_sha256") != fp.get("stream_sha256")):
            bad.append((rid, ref, fp))

    print("\n" + "=" * 74)
    if not bad and not missing:
        print(f"PARITY OK — {len(cur['records'])} recordings bit-identical "
              f"to baseline '{a.check}'")
        print("=" * 74)
        return 0

    print(f"PARITY FAILED — {len(bad)} mismatched, {len(missing)} missing")
    for rid, ref, fp in bad[:10]:
        print(f"\n  recording {rid}:")
        print(f"    trades  {ref['trade_count']:>6} -> {fp['trade_count']}")
        print(f"    pnl     {ref['total_pnl']} -> {fp['total_pnl']}")
        if ref.get("stream_sha256") != fp.get("stream_sha256"):
            a, b = ref.get("stream") or [], fp.get("stream") or []
            print(f"    decision stream DIVERGED "
                  f"(len {len(a)} -> {len(b)})")
            for i in range(min(len(a), len(b))):
                if a[i] != b[i]:
                    kind = "latent" if a[i][0] == "s" else "decision"
                    print(f"    first divergence at tick {i} ({kind}):")
                    print(f"      baseline : {a[i][1]}")
                    print(f"      candidate: {b[i][1]}")
                    break
        if ref["sha256"] != fp["sha256"]:
            _diff(ref["canon"], fp["canon"])
    print("=" * 74)
    return 1


def _diff(a, b, path="", depth=0, shown=None):
    """Print the first few leaf-level divergences between two canon trees."""
    if shown is None:
        shown = [0]
    if shown[0] >= 6 or depth > 8:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                print(f"      + {path}.{k} = {b[k]!r}"); shown[0] += 1
            elif k not in b:
                print(f"      - {path}.{k} = {a[k]!r}"); shown[0] += 1
            else:
                _diff(a[k], b[k], f"{path}.{k}", depth + 1, shown)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            print(f"      ~ {path} length {len(a)} -> {len(b)}"); shown[0] += 1
        for i in range(min(len(a), len(b))):
            _diff(a[i], b[i], f"{path}[{i}]", depth + 1, shown)
    elif a != b:
        print(f"      ~ {path}: {a!r} -> {b!r}"); shown[0] += 1


if __name__ == "__main__":
    raise SystemExit(main())
