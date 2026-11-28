"""
Tape-integrity rules + live-tripwire contract tests (2026-11-27).

The iter103 rules were extracted from `analysis/iter103_tape_integrity.py`
into `backend/tape_integrity.py` so the live autofeed tripwire can score a
recording's own candles.  These tests pin the rug fingerprint and the
kill-reason gate (a clean tape must never be killed).

Run:  cd backend && python -m pytest test_tape_integrity_live.py -q
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import tape_integrity as ti


def _candle(t, price, *, high=None, low=None, vol=0.05, pool=30.0):
    return {"time": t, "open": price, "high": high or price,
            "low": low or price, "close": price, "volume": vol,
            "buy_volume": 0.0, "sell_volume": 0.0, "pool_sol": pool}


def _clean_tape(n=240):
    out = []
    p = 1.0
    for i in range(n):
        # deterministic zig-zag with varied sizes (no identical steps)
        p *= 1.0 + (0.004 if i % 3 else -0.0035) * (1 + (i % 5) * 0.07)
        out.append(_candle(1000 + i, p, high=p * 1.002, low=p * 0.998,
                           vol=0.05 + (i % 7) * 0.011))
    return out


def _rug_tape(n=150):
    out = [_candle(1000 + i, 1.0, vol=0.05 + (i % 7) * 0.011) for i in range(n)]
    # dev dumps + pulls the pool: price collapses 82%, pool falls 73%
    out.append(_candle(1000 + n, 0.18, high=1.0, low=0.18, vol=20.0, pool=8.0))
    out += [_candle(1000 + n + 1 + i, 0.18, vol=0.05 + (i % 5) * 0.013,
                    pool=8.0) for i in range(60)]
    return out


def test_clean_tape_is_not_flagged():
    f = ti.features_from_dicts(_clean_tape())
    assert ti.classify(f) == []
    assert f["n"] == 240
    assert f["real_vol_sol"] > 2.0


def test_rug_fingerprint_kills():
    f = ti.features_from_dicts(_rug_tape())
    reasons = ti.classify(f)
    assert "rug_pull" in reasons, (reasons, f.get("dd60"), f.get("dd60_pool_drop"))
    assert f["dd60"] < 0.35
    assert "rug_pull" in ti.LIVE_KILL_REASONS


def test_thin_tape_is_stillborn_not_a_kill():
    f = ti.features_from_dicts(_clean_tape(n=40))
    assert ti.classify(f) == ["stillborn_lt100"]
    assert "stillborn_lt100" not in ti.LIVE_KILL_REASONS


def test_quiet_market_is_not_a_live_kill():
    # no_tradable_market is a corpus judgement; a live token can simply be
    # quiet (the engine has its own no-motion stop).  Volumes vary so the
    # fixed-size-bot / fixture rules do not fire.
    out = [_candle(1000 + i, 1.0, vol=0.0001 + (i % 7) * 0.00001)
           for i in range(150)]
    reasons = ti.classify(ti.features_from_dicts(out))
    assert "no_tradable_market" in reasons
    assert not any(r in ti.LIVE_KILL_REASONS for r in reasons)


def test_activity_padding_kill():
    # 150 identical sub-fee prints (< 10 000 lamports) = a volume bot.
    out = [_candle(1000 + i, 1.0, vol=0.000001) for i in range(150)]
    reasons = ti.classify(ti.features_from_dicts(out))
    assert "activity_padding" in reasons
    assert "activity_padding" in ti.LIVE_KILL_REASONS
