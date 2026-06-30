"""
EntrySignal — Evaluates all 6 entry conditions on each 1-second candle close.

Conditions (ALL must be true simultaneously):
  1. Dip has occurred (>= 35% from spike high)
  2. Floor has formed (stable lows, < 5% std dev — loosened from 3%)
  3. Buy pressure ratio turning positive (buy_ratio_trend requirement removed)
  4. Momentum turning positive (ROC + Kalman: m_hat > 0, not p_hat > 0)
  5. Volume expansion on buy side (>= 1.5x)
  6. Price above floor (>= 4% above floor level)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Literal
import math

from candle_aggregator import Candle
from sniper.launch_detector import LaunchDetector
from sniper.pressure_analyzer import PressureSnapshot


@dataclass
class EntrySignal:
    triggered: bool
    timestamp: float
    mint: str
    current_mc: float
    entry_price_sol: float   # price per token at signal time
    pressure: Optional[PressureSnapshot]
    conditions_met: dict     # each condition's pass/fail for logging
    conviction: Literal["high", "medium", "low"]

    def conviction_to_size(self) -> float:
        """Map conviction to position size in SOL."""
        sizes = {"high": 0.25, "medium": 0.15, "low": 0.10}
        return sizes.get(self.conviction, 0.10)


def _std_dev(values: list[float]) -> float:
    """Simple population standard deviation."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def evaluate_entry(
    mint: str,
    launch_detector: LaunchDetector,
    pressure: Optional[PressureSnapshot],
    strategy_engine,   # StrategyEngine instance
    candles: list[Candle],
    timestamp: float = 0.0,
) -> EntrySignal:
    """
    Evaluate all 6 entry conditions. Returns EntrySignal with triggered=True
    when ALL conditions pass simultaneously.
    """
    conditions = {
        "c1_dip_occurred": False,
        "c2_floor_formed": False,
        "c3_buy_pressure": False,
        "c4_momentum": False,
        "c5_volume_expansion": False,
        "c6_price_above_floor": False,
    }

    # Default no-trigger response
    no_signal = EntrySignal(
        triggered=False,
        timestamp=timestamp,
        mint=mint,
        current_mc=launch_detector.current_mc,
        entry_price_sol=candles[-1].close if candles else 0,
        pressure=pressure,
        conditions_met=conditions,
        conviction="low",
    )

    if not candles or len(candles) < 10 or pressure is None:
        return no_signal

    # ── CONDITION 1 — Dip Has Occurred ────────────────────────────────────
    dip_depth = launch_detector.dip_depth
    conditions["c1_dip_occurred"] = dip_depth >= 0.35

    # ── CONDITION 2 — Floor Has Formed ────────────────────────────────────
    # Std-dev threshold loosened from 3% → 5%: 1s bonding-curve candles are
    # naturally noisy; 3% was too tight and blocked almost every valid floor.
    last_10_lows = [c.low for c in candles[-10:]]
    floor_low = min(last_10_lows)
    lows_std = _std_dev(last_10_lows)

    conditions["c2_floor_formed"] = (
        candles[-1].low > floor_low * 0.97  # not making new lows
        and lows_std < floor_low * 0.05     # lows stable (< 5% deviation)
    ) if floor_low > 0 else False

    # ── CONDITION 3 — Buy Pressure Ratio Turning Positive ─────────────────
    # buy_ratio_trend (5s_avg - 10s_avg) removed: the value is typically
    # a tiny fraction (~0.01) and silently blocked valid entries.
    # The 5s average staying >= 0.50 is sufficient to confirm net buying.
    conditions["c3_buy_pressure"] = (
        pressure.buy_ratio_1s >= 0.60
        and pressure.buy_ratio_5s >= 0.50
    )

    # ── CONDITION 4 — Momentum Turning Positive ───────────────────────────
    # Called on closed candles only (sniper_engine guarantees this).
    # m_hat is the Kalman momentum updated on each 1-second candle close.
    # m_hat > 0 means price is accelerating upwards.
    # If strategy_engine is None (cleaned up), default to False.
    if strategy_engine is None:
        conditions["c4_momentum"] = False
    else:
        roc = getattr(strategy_engine, 'm_hat', 0)

        # Two consecutive rising candles confirm visual momentum.
        # Prefer close > open, but fall back to close-to-close comparison for
        # doji candles (open == close) which occur when only one trade per second.
        if len(candles) >= 3:
            c1, c2, c3 = candles[-1], candles[-2], candles[-3]
            c1_up = c1.close > c1.open or (c1.close == c1.open and c1.close > c2.close)
            c2_up = c2.close > c2.open or (c2.close == c2.open and c2.close > c3.close)
            two_rising = c1_up and c2_up
        elif len(candles) >= 2:
            c1, c2 = candles[-1], candles[-2]
            two_rising = c1.close >= c1.open and c2.close >= c2.open
        else:
            two_rising = False

        conditions["c4_momentum"] = roc > 0 and two_rising


    # ── CONDITION 5 — Volume Expansion ────────────────────────────────────
    conditions["c5_volume_expansion"] = pressure.volume_expansion >= 1.5

    # ── CONDITION 6 — Price Above Floor ───────────────────────────────────
    floor_price = min(c.low for c in candles[-10:])
    current_price = candles[-1].close
    conditions["c6_price_above_floor"] = (
        current_price >= floor_price * 1.04
    ) if floor_price > 0 else False

    # ── Conviction Mapping ────────────────────────────────────────────────
    if pressure.buy_ratio_5s > 0.70 and pressure.volume_expansion > 2.0:
        conviction = "high"
    elif pressure.buy_ratio_5s > 0.60 and pressure.volume_expansion > 1.5:
        conviction = "medium"
    else:
        conviction = "low"

    # ── All conditions check ──────────────────────────────────────────────
    all_met = all(conditions.values())

    return EntrySignal(
        triggered=all_met,
        timestamp=timestamp or (candles[-1].time if candles else 0),
        mint=mint,
        current_mc=launch_detector.current_mc,
        entry_price_sol=candles[-1].close,
        pressure=pressure,
        conditions_met=conditions,
        conviction=conviction,
    )
