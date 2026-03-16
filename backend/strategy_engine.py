"""
Strategy Engine — Regime-detection state machine for PumpFun memecoin trading.

Detects: TREND → EXHAUSTION → REVERSAL → CONTINUATION
Uses:    EMA3/7, ROC, ATR, Signal S, Volume Profile with delta
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


# ── Regime enum ──────────────────────────────────────────────────────────────

class Regime(str, Enum):
    IDLE         = "IDLE"
    TREND        = "TREND"
    EXHAUSTION   = "EXHAUSTION"
    REVERSAL     = "REVERSAL"
    CONTINUATION = "CONTINUATION"


class Direction(str, Enum):
    NONE = "NONE"
    UP   = "UP"
    DOWN = "DOWN"


class Signal(str, Enum):
    NONE = "NONE"
    BUY  = "BUY"
    SELL = "SELL"


# ── Volume Profile ──────────────────────────────────────────────────────────

@dataclass
class VolumeBin:
    price_low: float = 0.0
    price_high: float = 0.0
    buy_vol: float = 0.0
    sell_vol: float = 0.0

    @property
    def total(self) -> float:
        return self.buy_vol + self.sell_vol

    @property
    def delta(self) -> float:
        return self.buy_vol - self.sell_vol

    @property
    def mid(self) -> float:
        return (self.price_low + self.price_high) / 2.0

    def to_dict(self) -> dict:
        return {
            "price_low": self.price_low,
            "price_high": self.price_high,
            "price": self.mid,
            "buy_vol": round(self.buy_vol, 6),
            "sell_vol": round(self.sell_vol, 6),
            "total": round(self.total, 6),
            "delta": round(self.delta, 6),
        }


class VolumeProfile:
    """Fixed-range volume profile built from trend-start to current price."""

    def __init__(self, num_bins: int = 20):
        self.num_bins = num_bins
        self.bins: list[VolumeBin] = []
        self.range_low: float = 0.0
        self.range_high: float = 0.0
        self._total_volume: float = 0.0

    def reset(self, price: float):
        """Start a new profile anchored around the current price."""
        spread = price * 0.3  # ±30% initial range
        self.range_low = price - spread
        self.range_high = price + spread
        if self.range_low <= 0:
            self.range_low = price * 0.01
        self._rebuild_bins()

    def _rebuild_bins(self):
        """Rebuild bin boundaries, preserving accumulated volume."""
        old_bins = self.bins[:]
        step = (self.range_high - self.range_low) / self.num_bins
        self.bins = []
        for i in range(self.num_bins):
            lo = self.range_low + i * step
            hi = self.range_low + (i + 1) * step
            b = VolumeBin(price_low=lo, price_high=hi)
            # Re-accumulate from old bins that overlap
            for ob in old_bins:
                if ob.price_high > lo and ob.price_low < hi:
                    overlap = (min(ob.price_high, hi) - max(ob.price_low, lo))
                    ob_width = ob.price_high - ob.price_low
                    if ob_width > 0:
                        frac = overlap / ob_width
                        b.buy_vol += ob.buy_vol * frac
                        b.sell_vol += ob.sell_vol * frac
            self.bins.append(b)
        self._total_volume = sum(b.total for b in self.bins)

    def _expand_range(self, price: float):
        """Expand range if price moves outside."""
        changed = False
        if price < self.range_low:
            self.range_low = price * 0.95
            changed = True
        if price > self.range_high:
            self.range_high = price * 1.05
            changed = True
        if changed:
            self._rebuild_bins()

    def add_volume(self, price: float, buy_vol: float, sell_vol: float):
        """Add trade volume at a given price."""
        if not self.bins:
            return
        self._expand_range(price)
        for b in self.bins:
            if b.price_low <= price < b.price_high:
                b.buy_vol += buy_vol
                b.sell_vol += sell_vol
                self._total_volume += buy_vol + sell_vol
                return
        # Price at or above range_high → last bin
        if price >= self.range_high and self.bins:
            self.bins[-1].buy_vol += buy_vol
            self.bins[-1].sell_vol += sell_vol
            self._total_volume += buy_vol + sell_vol

    @property
    def poc(self) -> float:
        """Point of Control — price of highest-volume bin."""
        if not self.bins:
            return 0.0
        best = max(self.bins, key=lambda b: b.total)
        return best.mid

    def hvn_prices(self, top_n: int = 3) -> list[float]:
        """High Volume Nodes — top N bins by total volume."""
        sorted_bins = sorted(self.bins, key=lambda b: b.total, reverse=True)
        return [b.mid for b in sorted_bins[:top_n] if b.total > 0]

    def value_area(self, pct: float = 0.70) -> tuple[float, float]:
        """Value area containing pct of total volume around POC."""
        if not self.bins or self._total_volume <= 0:
            return (0.0, 0.0)
        # Find POC index
        poc_idx = max(range(len(self.bins)), key=lambda i: self.bins[i].total)
        target = self._total_volume * pct
        accumulated = self.bins[poc_idx].total
        lo_idx, hi_idx = poc_idx, poc_idx

        while accumulated < target and (lo_idx > 0 or hi_idx < len(self.bins) - 1):
            lo_vol = self.bins[lo_idx - 1].total if lo_idx > 0 else -1
            hi_vol = self.bins[hi_idx + 1].total if hi_idx < len(self.bins) - 1 else -1
            if lo_vol >= hi_vol and lo_idx > 0:
                lo_idx -= 1
                accumulated += self.bins[lo_idx].total
            elif hi_idx < len(self.bins) - 1:
                hi_idx += 1
                accumulated += self.bins[hi_idx].total
            else:
                break

        return (self.bins[lo_idx].price_low, self.bins[hi_idx].price_high)

    def is_price_in_hvn(self, price: float, direction: Direction, top_n: int = 3) -> bool:
        """Check if price is in a high volume node of a given direction."""
        sorted_bins = sorted(self.bins, key=lambda b: b.total, reverse=True)
        for b in sorted_bins[:top_n]:
            if b.price_low <= price < b.price_high:
                if direction == Direction.UP and b.delta > 0:
                    return True
                if direction == Direction.DOWN and b.delta < 0:
                    return True
                # For opposite direction check
                if direction == Direction.NONE:
                    return True
        return False

    def is_price_in_low_volume_node(self, price: float) -> bool:
        """Check if price is in a low volume area (bottom 30% of bins by volume)."""
        if not self.bins or self._total_volume <= 0:
            return False
        sorted_bins = sorted(self.bins, key=lambda b: b.total)
        low_count = max(1, len(sorted_bins) // 3)
        low_bins = sorted_bins[:low_count]
        return any(b.price_low <= price < b.price_high for b in low_bins)

    def has_opposite_hvn_ahead(self, price: float, direction: Direction, top_n: int = 3) -> bool:
        """Check if there's a major opposite HVN in the direction of travel."""
        sorted_bins = sorted(self.bins, key=lambda b: b.total, reverse=True)
        hvn_bins = sorted_bins[:top_n]
        for b in hvn_bins:
            if direction == Direction.UP and b.mid > price and b.delta < 0:
                return True
            if direction == Direction.DOWN and b.mid < price and b.delta > 0:
                return True
        return False

    def cumulative_delta(self) -> float:
        """Net buy-sell delta across all bins."""
        return sum(b.delta for b in self.bins)

    def to_dict(self) -> dict:
        va_lo, va_hi = self.value_area()
        return {
            "bins": [b.to_dict() for b in self.bins if b.total > 0],
            "poc": round(self.poc, 10),
            "value_area_low": round(va_lo, 10),
            "value_area_high": round(va_hi, 10),
            "hvn_prices": [round(p, 10) for p in self.hvn_prices()],
            "cumulative_delta": round(self.cumulative_delta(), 6),
        }


# ── Strategy Engine ─────────────────────────────────────────────────────────

class StrategyEngine:
    """
    Stateful strategy engine. Call `update()` with each new candle close.
    Returns a dict snapshot of all indicators, regime, and signals.
    """

    # Thresholds (from user's strategy spec)
    S_TREND_ENTER     = 1.0   # S > 1 for trend
    S_TREND_EXIT      = 1.0   # S < 1 for exhaustion
    S_CONTINUATION    = 1.5   # S > 1.5 for continuation
    ATR_REVERSAL_MULT = 1.2   # ATR > 1.2 × ATR_trend_start
    DELTA_THRESHOLD   = 0.5   # minimum delta volume for reversal (SOL)

    def __init__(
        self,
        ema_fast: int = 3,
        ema_slow: int = 7,
        roc_period: int = 5,
        atr_period: int = 14,
        vp_bins: int = 20,
    ):
        self.ema_fast_n = ema_fast
        self.ema_slow_n = ema_slow
        self.roc_period = roc_period
        self.atr_period = atr_period

        # EMA state
        self._ema_fast: Optional[float] = None
        self._ema_slow: Optional[float] = None
        self._ema_fast_k = 2.0 / (ema_fast + 1)
        self._ema_slow_k = 2.0 / (ema_slow + 1)

        # Price history for ROC
        self._closes: deque[float] = deque(maxlen=max(roc_period + 2, 50))

        # ATR state
        self._atr: Optional[float] = None
        self._atr_k = 2.0 / (atr_period + 1)
        self._prev_close: Optional[float] = None

        # Spread tracking
        self._prev_spread: Optional[float] = None
        self._prev_roc: Optional[float] = None

        # Volume profile
        self.volume_profile = VolumeProfile(num_bins=vp_bins)
        self._vp_initialized = False

        # Regime state
        self.regime = Regime.IDLE
        self.direction = Direction.NONE
        self.trend_start_price: Optional[float] = None
        self.trend_start_atr: Optional[float] = None
        self.trend_start_delta: Optional[float] = None

        # Signal output
        self.entry_signal = Signal.NONE
        self.exit_signal = Signal.NONE
        self._in_position = False

        # Count bars
        self._bar_count = 0

        # Reversal direction (the NEW direction after reversal)
        self._reversal_direction = Direction.NONE

    def _update_ema(self, close: float):
        if self._ema_fast is None:
            self._ema_fast = close
            self._ema_slow = close
        else:
            self._ema_fast = close * self._ema_fast_k + self._ema_fast * (1 - self._ema_fast_k)
            self._ema_slow = close * self._ema_slow_k + self._ema_slow * (1 - self._ema_slow_k)

    def _update_atr(self, high: float, low: float, close: float):
        tr = high - low
        if self._prev_close is not None:
            tr = max(tr, abs(high - self._prev_close), abs(low - self._prev_close))
        if self._atr is None:
            self._atr = tr
        else:
            self._atr = tr * self._atr_k + self._atr * (1 - self._atr_k)

    def _calc_roc(self) -> float:
        if len(self._closes) <= self.roc_period:
            return 0.0
        prev = self._closes[-(self.roc_period + 1)]
        curr = self._closes[-1]
        if prev <= 0:
            return 0.0
        return (curr - prev) / prev

    def _calc_signal_s(self, roc: float) -> float:
        if self._atr is None or self._atr <= 0:
            return 0.0
        # Normalize: ROC is a ratio, ATR is absolute → use relative ATR
        last_close = self._closes[-1] if self._closes else 1.0
        if last_close <= 0:
            return 0.0
        relative_atr = self._atr / last_close
        if relative_atr <= 0:
            return 0.0
        return abs(roc) / relative_atr

    def update(
        self,
        close: float,
        high: float,
        low: float,
        volume: float = 0.0,
        buy_volume: float = 0.0,
        sell_volume: float = 0.0,
        timestamp: float = 0.0,
    ) -> dict:
        """
        Feed one candle. Returns a state snapshot dict.
        """
        self._bar_count += 1

        # ── Indicators ──────────────────────────────────────────────────
        self._update_ema(close)
        self._update_atr(high, low, close)
        self._closes.append(close)

        roc = self._calc_roc()
        signal_s = self._calc_signal_s(roc)

        ema_spread = (self._ema_fast or 0) - (self._ema_slow or 0)
        spread_expanding = False
        spread_shrinking = False
        if self._prev_spread is not None:
            spread_delta = abs(ema_spread) - abs(self._prev_spread)
            spread_expanding = spread_delta > 0
            spread_shrinking = spread_delta < 0

        roc_decreasing = False
        if self._prev_roc is not None:
            roc_decreasing = abs(roc) < abs(self._prev_roc)

        # ── Volume Profile ──────────────────────────────────────────────
        if not self._vp_initialized and close > 0:
            self.volume_profile.reset(close)
            self._vp_initialized = True

        if self._vp_initialized:
            self.volume_profile.add_volume(close, buy_volume, sell_volume)

        # ── Regime Detection ────────────────────────────────────────────
        self.entry_signal = Signal.NONE
        self.exit_signal = Signal.NONE

        warming_up = self._bar_count < max(self.ema_slow_n, self.roc_period) + 2

        if not warming_up:
            self._detect_regime(close, roc, signal_s, ema_spread,
                                spread_expanding, spread_shrinking, roc_decreasing)

        # ── Exit check if in position ───────────────────────────────────
        if self._in_position and not warming_up:
            self._check_exit(close, signal_s, ema_spread, spread_shrinking)

        # ── Save state for next bar ─────────────────────────────────────
        self._prev_close = close
        self._prev_spread = ema_spread
        self._prev_roc = roc

        return self._snapshot(close, roc, signal_s, ema_spread, spread_expanding)

    def _detect_regime(
        self,
        close: float,
        roc: float,
        signal_s: float,
        ema_spread: float,
        spread_expanding: bool,
        spread_shrinking: bool,
        roc_decreasing: bool,
    ):
        ema_up = (self._ema_fast or 0) > (self._ema_slow or 0)
        ema_down = (self._ema_slow or 0) > (self._ema_fast or 0)

        if self.regime == Regime.IDLE:
            # A. Trend Detection
            if (ema_up or ema_down) and spread_expanding and signal_s > self.S_TREND_ENTER:
                self.regime = Regime.TREND
                self.direction = Direction.UP if ema_up else Direction.DOWN
                self.trend_start_price = close
                self.trend_start_atr = self._atr
                self.trend_start_delta = self.volume_profile.cumulative_delta()
                # Start new volume profile from trend start
                self.volume_profile.reset(close)
                self._vp_initialized = True

        elif self.regime == Regime.TREND:
            # B. Trend Exhaustion Detection
            if spread_shrinking and roc_decreasing and signal_s < self.S_TREND_EXIT:
                in_low_vol = self.volume_profile.is_price_in_low_volume_node(close)
                if in_low_vol or signal_s < 0.5:  # relaxed if S is very low
                    self.regime = Regime.EXHAUSTION

            # Also reset if direction flips without exhaustion
            new_dir = Direction.UP if ema_up else Direction.DOWN
            if new_dir != self.direction and new_dir != Direction.NONE:
                # Quick direction flip → go to exhaustion
                self.regime = Regime.EXHAUSTION

        elif self.regime == Regime.EXHAUSTION:
            # C. Reversal Confirmation
            roc_crossed_zero = False
            if self._prev_roc is not None:
                if self.direction == Direction.UP and roc < 0:
                    roc_crossed_zero = True
                elif self.direction == Direction.DOWN and roc > 0:
                    roc_crossed_zero = True

            atr_spike = False
            if self.trend_start_atr and self.trend_start_atr > 0 and self._atr:
                atr_spike = self._atr > self.ATR_REVERSAL_MULT * self.trend_start_atr

            # Check opposite delta
            cum_delta = self.volume_profile.cumulative_delta()
            strong_opposite_delta = False
            if self.direction == Direction.UP and cum_delta < -self.DELTA_THRESHOLD:
                strong_opposite_delta = True
            elif self.direction == Direction.DOWN and cum_delta > self.DELTA_THRESHOLD:
                strong_opposite_delta = True

            opposite_dir = Direction.DOWN if self.direction == Direction.UP else Direction.UP
            in_opposite_hvn = self.volume_profile.is_price_in_hvn(close, opposite_dir)

            # Need at least ROC cross + one other condition
            if roc_crossed_zero and (atr_spike or strong_opposite_delta or in_opposite_hvn):
                self.regime = Regime.REVERSAL
                self._reversal_direction = opposite_dir

            # Timeout — if exhaustion lasts too long without reversal, go back to IDLE
            elif signal_s > self.S_TREND_ENTER and spread_expanding:
                # Trend resumed
                self.regime = Regime.TREND

        elif self.regime == Regime.REVERSAL:
            # D. Continuation Confirmation
            new_ema_up = (self._ema_fast or 0) > (self._ema_slow or 0)
            new_dir = Direction.UP if new_ema_up else Direction.DOWN

            ema_confirms = new_dir == self._reversal_direction
            strong_s = signal_s > self.S_CONTINUATION

            # Delta aligned with new direction
            cum_delta = self.volume_profile.cumulative_delta()
            delta_aligned = False
            if self._reversal_direction == Direction.UP and cum_delta > 0:
                delta_aligned = True
            elif self._reversal_direction == Direction.DOWN and cum_delta < 0:
                delta_aligned = True

            # Price outside prior value area
            va_lo, va_hi = self.volume_profile.value_area()
            price_outside_va = close < va_lo or close > va_hi

            # No major opposite HVN ahead
            no_opposite_hvn = not self.volume_profile.has_opposite_hvn_ahead(
                close, self._reversal_direction
            )

            if ema_confirms and strong_s:
                # Relaxed: need at least 2 of the other 3 conditions
                other_conditions = sum([delta_aligned, price_outside_va, no_opposite_hvn])
                if other_conditions >= 2:
                    self.regime = Regime.CONTINUATION
                    self.direction = self._reversal_direction
                    # Fire entry signal
                    if not self._in_position:
                        self.entry_signal = Signal.BUY if self.direction == Direction.UP else Signal.SELL
                        self._in_position = True
                    # Reset for next cycle
                    self.trend_start_price = close
                    self.trend_start_atr = self._atr
                    self.trend_start_delta = self.volume_profile.cumulative_delta()
                    self.volume_profile.reset(close)
                    self._vp_initialized = True
                    # Move to TREND for the new direction
                    self.regime = Regime.TREND

            # Reversal failed → back to IDLE
            elif signal_s < 0.3:
                self.regime = Regime.IDLE
                self.direction = Direction.NONE
                self._reversal_direction = Direction.NONE

    def _check_exit(
        self,
        close: float,
        signal_s: float,
        ema_spread: float,
        spread_shrinking: bool,
    ):
        reasons = []

        # 1. Signal strength drops
        if signal_s < self.S_TREND_EXIT:
            reasons.append("S < 1")

        # 2. Delta reverses
        cum_delta = self.volume_profile.cumulative_delta()
        if self.direction == Direction.UP and cum_delta < -self.DELTA_THRESHOLD:
            reasons.append("delta_reversal")
        elif self.direction == Direction.DOWN and cum_delta > self.DELTA_THRESHOLD:
            reasons.append("delta_reversal")

        # 3. Price enters major volume node
        if self.volume_profile.is_price_in_hvn(close, Direction.NONE):
            reasons.append("major_volume_node")

        # 4. EMA spread collapses
        if spread_shrinking and abs(ema_spread) < abs(self._prev_spread or 1e-18) * 0.3:
            reasons.append("ema_collapse")

        # Need at least 2 exit conditions to avoid premature exits
        if len(reasons) >= 2:
            self.exit_signal = Signal.SELL if self.direction == Direction.UP else Signal.BUY
            self._in_position = False
            self.regime = Regime.IDLE
            self.direction = Direction.NONE

    def notify_trade_closed(self):
        """Called by trade simulator when position is closed."""
        self._in_position = False

    def _snapshot(
        self,
        close: float,
        roc: float,
        signal_s: float,
        ema_spread: float,
        spread_expanding: bool,
    ) -> dict:
        return {
            "ema3": round(self._ema_fast, 10) if self._ema_fast else 0,
            "ema7": round(self._ema_slow, 10) if self._ema_slow else 0,
            "ema_spread": ema_spread,
            "ema_spread_expanding": spread_expanding,
            "roc": round(roc, 6),
            "atr": round(self._atr, 10) if self._atr else 0,
            "signal_s": round(signal_s, 4),
            "regime": self.regime.value,
            "regime_direction": self.direction.value,
            "trend_start_price": self.trend_start_price,
            "entry_signal": self.entry_signal.value if self.entry_signal != Signal.NONE else None,
            "exit_signal": self.exit_signal.value if self.exit_signal != Signal.NONE else None,
            "in_position": self._in_position,
            "volume_profile": self.volume_profile.to_dict(),
            "bar_count": self._bar_count,
            "warming_up": self._bar_count < max(self.ema_slow_n, self.roc_period) + 2,
        }
