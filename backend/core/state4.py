"""The 4-state intra-candle expansion, the single implementation.

One completed candle feeds the engine as exactly four accumulated states,
in this order, in every pipeline (replay, backtest, live):

    State 1: (o, o, o, o)                  open tick, no volume
    State 2: (o, ext1, other, ext1)        first extreme
    State 3: (o, h, l, ext2)               both extremes
    State 4: (o, h, l, c)                  close tick, full volume

Bull bars (close >= open) visit the high before the low; bear bars visit
the low before the high.  A different order changes which intra-candle
state produces a signal, which changes every trade.
"""
from __future__ import annotations

import bisect
from typing import Tuple

State = Tuple[float, float, float, float]


def candle_states(o: float, h: float, l: float,
                  c: float) -> Tuple[State, State, State, State]:
    """Return the four accumulated (o, h, l, c) states for one candle."""
    if c >= o:
        mid_first, mid_second = h, l
    else:
        mid_first, mid_second = l, h
    return (
        (o, o, o, o),
        (o, max(o, mid_first), min(o, mid_first), mid_first),
        (o, h, l, mid_second),
        (o, h, l, c),
    )


def intrabar_price(o: float, h: float, l: float, c: float, frac: float) -> float:
    """Price along the intra-candle path at frac (0 = open, 1 = close).

    Three equal segments: open to first extreme, first to second extreme,
    second extreme to close.  This is the fill-pricing contract shared by
    replay and live; do not re-derive it at call sites.
    """
    if c >= o:
        p0, p1, p2, p3 = o, h, l, c
    else:
        p0, p1, p2, p3 = o, l, h, c

    if frac <= 1 / 3:
        t = frac * 3
        return p0 + (p1 - p0) * t
    elif frac <= 2 / 3:
        t = (frac - 1 / 3) * 3
        return p1 + (p2 - p1) * t
    else:
        t = (frac - 2 / 3) * 3
        return p2 + (p3 - p2) * t


def record_path_candle(path_candles: dict, path_times: list, time: int,
                       o: float, h: float, l: float, c: float) -> None:
    """Record one progressive state tuple into a fill-path buffer.

    States 1 to 3 share the candle time and progressively complete h/l/c;
    the final write per time is the full candle.
    """
    t = int(time)
    if t not in path_candles:
        path_times.append(t)
    path_candles[t] = (o, h, l, c)


def path_price_at(path_candles: dict, path_times: list,
                  target_ts: float) -> float:
    """Price on a recorded intra-candle path at absolute time *target_ts*.

    Locates the candle containing the timestamp (greatest recorded candle
    time <= target) and interpolates along its open-extreme-extreme-close
    path, using the next recorded candle time as the bar span.  Before the
    first candle this returns its open; beyond the recording end the span
    is 1 and the price clamps to the last close.
    """
    if not path_times:
        return 0.0
    t0 = int(target_ts)
    idx = bisect.bisect_right(path_times, t0) - 1
    if idx < 0:
        o, _h, _l, _c = path_candles[path_times[0]]
        return o
    t = path_times[idx]
    o, h, l, c = path_candles[t]
    if idx + 1 < len(path_times):
        span = path_times[idx + 1] - t
    else:
        span = 1
    frac = max(0.0, min(1.0, (target_ts - t) / span))
    return intrabar_price(o, h, l, c, frac)
