"""
ExitSignal — Evaluates all 6 exit triggers continuously while a position is open.

Exit triggers:
  1. Buy pressure collapse (2 consecutive candles with buyRatio_3s < 0.38)
  2. Momentum reversal (ROC crosses negative + Kalman down)
  3. Large sell spike (immediate, tick-level)
  4. Upper wick exhaustion (3 consecutive rejection candles)
  5. Time stop (> 5 min with < 15% gain)
  6. Hard price stop (> 30% loss, immediate)

Triggers 3 and 6 have urgency="immediate" and take absolute priority.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Literal
from collections import deque
import time

from candle_aggregator import Candle
from sniper.pressure_analyzer import PressureSnapshot


@dataclass
class OpenPosition:
    mint: str
    entry_price: float
    entry_mc: float
    entry_time: float
    size_sol: float
    peak_mc: float
    forward_test_id: Optional[int] = None
    pending_exit: Optional['ExitSignal'] = None


@dataclass
class ExitSignal:
    triggered: bool
    trigger_name: str       # which of the 6 triggers fired
    timestamp: float
    current_mc: float
    exit_price_sol: float
    urgency: Literal["immediate", "on_close"]


class ExitEvaluator:
    """
    Stateful exit evaluator that tracks rolling metrics across candle closes.
    One instance per open position.
    """

    def __init__(self):
        # Trigger 1: buy ratio collapse tracking
        self._buy_ratio_3s_history: deque[float] = deque(maxlen=5)
        self._collapse_consecutive: int = 0

    def evaluate_exit(
        self,
        position: OpenPosition,
        pressure: Optional[PressureSnapshot],
        strategy_engine,
        candles: list[Candle],
        current_tick_price: float,
        current_mc: float = 0,
        now: Optional[float] = None,
    ) -> Optional[ExitSignal]:
        """
        Evaluate all 6 exit triggers. Returns ExitSignal if any fires, None otherwise.
        Triggers 3 and 6 (urgency="immediate") take absolute priority.
        """
        if now is None:
            now = time.time()

        ts = now

        # ── TRIGGER 6 — Hard Price Stop (IMMEDIATE) ────────────────────────
        if position.entry_price > 0 and current_tick_price > 0:
            pct_loss = (current_tick_price - position.entry_price) / position.entry_price
            if pct_loss <= -0.30:
                return ExitSignal(
                    triggered=True,
                    trigger_name="hard_stop",
                    timestamp=ts,
                    current_mc=current_mc,
                    exit_price_sol=current_tick_price,
                    urgency="immediate",
                )

        # ── TRIGGER 3 — Large Sell Spike (IMMEDIATE) ───────────────────────
        if pressure is not None and pressure.large_sell_spike:
            return ExitSignal(
                triggered=True,
                trigger_name="large_sell_spike",
                timestamp=ts,
                current_mc=current_mc,
                exit_price_sol=current_tick_price,
                urgency="immediate",
            )

        # ── TRIGGER 1 — Buy Pressure Collapse (on_close) ──────────────────
        if pressure is not None and candles:
            # Compute 3-candle rolling average of buy ratios
            recent_ratios = []
            for c in candles[-3:]:
                total = c.buy_volume + c.sell_volume
                ratio = c.buy_volume / total if total > 0 else 0.5
                recent_ratios.append(ratio)
            if recent_ratios:
                buy_ratio_3s = sum(recent_ratios) / len(recent_ratios)
                self._buy_ratio_3s_history.append(buy_ratio_3s)

                if buy_ratio_3s < 0.38:
                    self._collapse_consecutive += 1
                else:
                    self._collapse_consecutive = 0

                if self._collapse_consecutive >= 2:
                    return ExitSignal(
                        triggered=True,
                        trigger_name="buy_pressure_collapse",
                        timestamp=ts,
                        current_mc=current_mc,
                        exit_price_sol=current_tick_price,
                        urgency="on_close",
                    )

        # ── TRIGGER 2 — Momentum Reversal (on_close) ──────────────────────
        roc = getattr(strategy_engine, 'm_hat', 0)
        prev_roc = getattr(strategy_engine, 'prev_m_hat', 0)
        p_hat = getattr(strategy_engine, 'p_hat', 0)

        roc_crossed_negative = roc < 0 and prev_roc >= 0

        # Approximate Kalman direction from m_hat history
        m_hat_history = getattr(strategy_engine, '_m_hat_history', [])
        kalman_turned_down = False
        if len(m_hat_history) >= 2:
            kalman_turned_down = m_hat_history[-1] < m_hat_history[-2]

        if roc_crossed_negative and kalman_turned_down:
            return ExitSignal(
                triggered=True,
                trigger_name="momentum_reversal",
                timestamp=ts,
                current_mc=current_mc,
                exit_price_sol=current_tick_price,
                urgency="on_close",
            )

        # ── TRIGGER 4 — Upper Wick Exhaustion (on_close) ───────────────────
        if pressure is not None and pressure.upper_wick_count >= 3 and len(candles) >= 3:
            last3_highs = [c.high for c in candles[-3:]]
            no_new_high = last3_highs[-1] <= max(last3_highs[:-1])
            if no_new_high:
                return ExitSignal(
                    triggered=True,
                    trigger_name="wick_exhaustion",
                    timestamp=ts,
                    current_mc=current_mc,
                    exit_price_sol=current_tick_price,
                    urgency="on_close",
                )

        # ── TRIGGER 5 — Time Stop (on_close) ──────────────────────────────
        time_in_trade = now - position.entry_time
        if position.entry_price > 0 and current_tick_price > 0:
            pct_gain = (current_tick_price - position.entry_price) / position.entry_price
            if time_in_trade > 300 and pct_gain < 0.15:
                return ExitSignal(
                    triggered=True,
                    trigger_name="time_stop",
                    timestamp=ts,
                    current_mc=current_mc,
                    exit_price_sol=current_tick_price,
                    urgency="on_close",
                )

        return None
