"""
PressureAnalyzer — Computes buy/sell pressure metrics from extended OHLCV candles.

Returns a PressureSnapshot used by entry_signal.py and exit_signal.py.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import math

from candle_aggregator import Candle


@dataclass
class PressureSnapshot:
    buy_ratio_1s: float       # buyVol / totalVol for most recent 1s candle
    buy_ratio_5s: float       # rolling 5-candle average of buy_ratio_1s
    buy_ratio_10s: float      # rolling 10-candle average
    buy_ratio_trend: float    # buy_ratio_5s - buy_ratio_10s (positive = improving)
    current_buy_vol: float    # buyVolume in most recent 1s candle (SOL)
    avg_buy_vol: float        # average buyVolume over last 10 candles (SOL)
    volume_expansion: float   # current_buy_vol / avg_buy_vol (>1 means above avg)
    net_pressure: float       # buyVol - sellVol over last 3 candles (SOL)
    large_sell_spike: bool    # True if current candle sellVol > avg_total_vol * 3.0
    upper_wick_count: int     # count of last 3 candles where upper_wick > body


def _buy_ratio(candle: Candle) -> float:
    """Buy volume fraction for a single candle."""
    total = candle.buy_volume + candle.sell_volume
    if total <= 0:
        return 0.5  # neutral if no volume
    return candle.buy_volume / total


def _upper_wick_gt_body(candle: Candle) -> bool:
    """True if the upper wick exceeds the candle body."""
    upper_wick = candle.high - max(candle.open, candle.close)
    body = abs(candle.close - candle.open)
    return upper_wick > body and body > 0


def compute_pressure(candles: list[Candle]) -> Optional[PressureSnapshot]:
    """
    Compute pressure metrics from the last 10+ one-second candles (most recent last).
    Returns None if fewer than 3 candles available (insufficient data).
    """
    if len(candles) < 3:
        return None

    # Use up to last 10 candles
    window = candles[-10:] if len(candles) >= 10 else candles

    # Per-candle buy ratios
    ratios = [_buy_ratio(c) for c in window]

    # Current (last) candle
    current = window[-1]
    buy_ratio_1s = ratios[-1]

    # Rolling averages
    buy_ratio_5s = sum(ratios[-5:]) / min(5, len(ratios)) if len(ratios) >= 1 else 0.5
    buy_ratio_10s = sum(ratios) / len(ratios) if ratios else 0.5
    buy_ratio_trend = buy_ratio_5s - buy_ratio_10s

    # Volume metrics
    current_buy_vol = current.buy_volume
    buy_vols = [c.buy_volume for c in window]
    avg_buy_vol = sum(buy_vols) / len(buy_vols) if buy_vols else 0.001
    volume_expansion = current_buy_vol / max(avg_buy_vol, 1e-12)

    # Net pressure over last 3 candles
    last3 = window[-3:]
    net_pressure = sum(c.buy_volume - c.sell_volume for c in last3)

    # Large sell spike detection
    total_vols = [(c.buy_volume + c.sell_volume) for c in window]
    avg_total_vol = sum(total_vols) / len(total_vols) if total_vols else 0.001
    large_sell_spike = current.sell_volume > avg_total_vol * 3.0

    # Upper wick exhaustion count (last 3 candles)
    wick_candles = window[-3:] if len(window) >= 3 else window
    upper_wick_count = sum(1 for c in wick_candles if _upper_wick_gt_body(c))

    return PressureSnapshot(
        buy_ratio_1s=buy_ratio_1s,
        buy_ratio_5s=buy_ratio_5s,
        buy_ratio_10s=buy_ratio_10s,
        buy_ratio_trend=buy_ratio_trend,
        current_buy_vol=current_buy_vol,
        avg_buy_vol=avg_buy_vol,
        volume_expansion=volume_expansion,
        net_pressure=net_pressure,
        large_sell_spike=large_sell_spike,
        upper_wick_count=upper_wick_count,
    )
