"""
Strategy Engine — Physics-inspired regime-detection state machine.

Models price dynamics as a Langevin particle:
  dp = m·dt
  dm = U'(p)·dt − γ·m·dt + σ·dW_t

Detects four regimes:
  TREND → EXHAUSTION → REVERSAL → CONTINUATION
then enters at the earliest stable continuation phase.

Observable proxies:
  m  (momentum)       → ROC
  m  (trend direction) → EMA(3–7) cross
  γ  (damping)        → EMA spread contraction rate
  σ  (noise)          → ATR
  U(p) (potential)    → volume profile nodes
  external force      → delta volume

Core metric — Signal-to-Noise Ratio:
  S = |ROC| / (ATR / price)
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
    """
    Fixed-range volume profile built from trend start to current price.

    Range spans from the local low to local high seen during the current trend.
    A new profile is started each time a new trend begins.
    """

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
        if step <= 0:
            return
        self.bins = []
        for i in range(self.num_bins):
            lo = self.range_low + i * step
            hi = self.range_low + (i + 1) * step
            b = VolumeBin(price_low=lo, price_high=hi)
            # Re-accumulate from old bins that overlap
            for ob in old_bins:
                if ob.price_high > lo and ob.price_low < hi:
                    overlap = min(ob.price_high, hi) - max(ob.price_low, lo)
                    ob_width = ob.price_high - ob.price_low
                    if ob_width > 0:
                        frac = overlap / ob_width
                        b.buy_vol += ob.buy_vol * frac
                        b.sell_vol += ob.sell_vol * frac
            self.bins.append(b)
        self._total_volume = sum(b.total for b in self.bins)

    def _expand_range(self, price: float):
        """Expand range if price moves outside current bounds."""
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
        """
        Check if price is in a high volume node of a given direction.
        direction=UP  → HVN with positive delta (buy dominated)
        direction=DOWN → HVN with negative delta (sell dominated)
        direction=NONE → any HVN regardless of delta
        """
        sorted_bins = sorted(self.bins, key=lambda b: b.total, reverse=True)
        for b in sorted_bins[:top_n]:
            if b.price_low <= price < b.price_high:
                if direction == Direction.UP and b.delta > 0:
                    return True
                if direction == Direction.DOWN and b.delta < 0:
                    return True
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

    def hvn_at_top(self, direction: Direction, top_n: int = 3) -> bool:
        """
        Check if the highest HVN of the trend's direction is at the
        leading edge (top for UP, bottom for DOWN).

        This confirms the new trend has volume structure supporting it
        at the frontier — the 'particle' has momentum into new territory.
        """
        if not self.bins:
            return False

        # Get top-N bins by total volume that have delta aligned with direction
        sorted_bins = sorted(self.bins, key=lambda b: b.total, reverse=True)
        aligned_hvns = []
        for b in sorted_bins[:top_n]:
            if direction == Direction.UP and b.delta > 0:
                aligned_hvns.append(b)
            elif direction == Direction.DOWN and b.delta < 0:
                aligned_hvns.append(b)

        if not aligned_hvns:
            return False

        # The highest-volume aligned bin should be in the leading half
        best = aligned_hvns[0]
        mid_price = (self.range_low + self.range_high) / 2.0
        if direction == Direction.UP:
            return best.mid >= mid_price
        else:  # DOWN
            return best.mid <= mid_price

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
    Stateful strategy engine implementing physics-inspired regime detection.

    Models price as a Langevin particle:
      dp = m·dt                              (price follows momentum)
      dm = U'(p)·dt − γ·m·dt + σ·dW_t       (momentum evolves)

    Continuation when:  m² ≫ γ·ΔU  (kinetic energy dominates potential)
    Reversal when:      γ·m dominates (damping kills momentum — Kramers escape)

    Call `update()` with each new trade/candle. Returns a dict snapshot.
    """

    # ── Tuning constants (from spec) ────────────────────────────────────
    #
    # Signal strength S = |ROC| / relative_ATR
    #   S < 0.8   → noise dominated
    #   0.8–1.5   → unstable
    #   S > 1.5   → strong directional momentum

    S_NOISE_THRESHOLD      = 0.8   # below → noise dominated
    S_TREND_ENTER          = 1.0   # S > 1.0 to enter TREND
    S_EXHAUSTION           = 1.0   # S < 1.0 → exhaustion (γm > m_drive)
    S_CONTINUATION         = 1.5   # S > 1.5 for continuation entry
    S_EXIT                 = 1.0   # S < 1.0 → exit position
    S_REVERSAL_FAIL        = 0.3   # reversal aborts if S collapses

    ATR_REVERSAL_MULT      = 1.2   # ATR > 1.2 × ATR_trend_start
    DELTA_THRESHOLD        = 0.5   # minimum delta volume for reversal

    TRAIL_TRIGGER_PCT      = 0.05  # arm trailing stop at 5% profit
    TRAIL_LOCK_PCT         = 0.05  # lock stop at entry + 5%

    EXHAUSTION_EXIT_BARS   = 10    # exit after N bars in EXHAUSTION

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

        # ── EMA state ────────────────────────────────────────────────
        self._ema_fast: Optional[float] = None
        self._ema_slow: Optional[float] = None
        self._ema_fast_k = 2.0 / (ema_fast + 1)
        self._ema_slow_k = 2.0 / (ema_slow + 1)

        # ── Price history for ROC ────────────────────────────────────
        self._closes: deque[float] = deque(maxlen=max(roc_period + 2, 50))

        # ── ATR state ────────────────────────────────────────────────
        self._atr: Optional[float] = None
        self._atr_k = 2.0 / (atr_period + 1)
        self._prev_close: Optional[float] = None

        # ── ROC tracking ─────────────────────────────────────────────
        self._prev_roc: Optional[float] = None

        # ── EMA spread tracking (γ damping proxy) ────────────────────
        self._prev_ema_spread: Optional[float] = None

        # ── Volume profile (one per trend) ───────────────────────────
        self.volume_profile = VolumeProfile(num_bins=vp_bins)
        self._vp_initialized = False

        # ── Regime state ─────────────────────────────────────────────
        self.regime = Regime.IDLE
        self.direction = Direction.NONE
        self.trend_start_price: Optional[float] = None
        self.trend_start_atr: Optional[float] = None
        self.trend_start_delta: Optional[float] = None

        # ── Saved value area from the OLD trend (for continuation check) ──
        self._prev_value_area: tuple[float, float] = (0.0, 0.0)

        # ── Signal output ────────────────────────────────────────────
        self.entry_signal = Signal.NONE
        self.exit_signal = Signal.NONE
        self._in_position = False

        # ── Position tracking for exit rules ─────────────────────────
        self._entry_price: Optional[float] = None
        self._trailing_stop: Optional[float] = None
        self._exhaustion_bars: int = 0

        # ── Bar counter ──────────────────────────────────────────────
        self._bar_count = 0

        # ── Reversal direction (the NEW direction after reversal) ────
        self._reversal_direction = Direction.NONE

    # ── Indicator updates ────────────────────────────────────────────────

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
        """Rate of Change over roc_period bars."""
        if len(self._closes) <= self.roc_period:
            return 0.0
        prev = self._closes[-(self.roc_period + 1)]
        curr = self._closes[-1]
        if prev <= 0:
            return 0.0
        return (curr - prev) / prev

    def _calc_signal_s(self, roc: float) -> float:
        """
        Signal strength: S = |ROC| / relative_ATR

        This is the trend signal-to-noise ratio from the spec.
        S < 0.8  → noise dominated
        0.8–1.5  → unstable
        S > 1.5  → strong directional momentum
        """
        if self._atr is None or self._atr <= 0:
            return 0.0
        last_close = self._closes[-1] if self._closes else 1.0
        if last_close <= 0:
            return 0.0
        relative_atr = self._atr / last_close
        if relative_atr <= 0:
            return 0.0
        return abs(roc) / relative_atr

    # ── Main update ──────────────────────────────────────────────────────

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
        Feed one candle / trade tick. Returns a state snapshot dict.
        """
        self._bar_count += 1

        # ── 1. Update indicators ─────────────────────────────────────
        self._update_ema(close)
        self._update_atr(high, low, close)
        self._closes.append(close)

        roc = self._calc_roc()
        signal_s = self._calc_signal_s(roc)

        # EMA spread — proxy for momentum damping γ
        ema_spread = (self._ema_fast or 0.0) - (self._ema_slow or 0.0)
        spread_expanding = False
        spread_shrinking = False
        if self._prev_ema_spread is not None:
            spread_delta = abs(ema_spread) - abs(self._prev_ema_spread)
            spread_expanding = spread_delta > 0
            spread_shrinking = spread_delta < 0

        roc_decreasing = False
        if self._prev_roc is not None:
            roc_decreasing = abs(roc) < abs(self._prev_roc)

        # ── 2. Volume Profile — accumulate within current trend ──────
        if not self._vp_initialized and close > 0:
            self.volume_profile.reset(close)
            self._vp_initialized = True

        if self._vp_initialized:
            self.volume_profile.add_volume(close, buy_volume, sell_volume)

        # ── 3. Regime detection ──────────────────────────────────────
        self.entry_signal = Signal.NONE
        self.exit_signal = Signal.NONE

        warming_up = self._bar_count < max(self.ema_slow_n, self.roc_period) + 2

        if not warming_up:
            self._detect_regime(
                close, roc, signal_s, ema_spread,
                spread_expanding, spread_shrinking, roc_decreasing,
            )

        # ── 4. Record entry price on fresh entry signal ──────────────
        if self.entry_signal != Signal.NONE:
            self._entry_price = close
            self._trailing_stop = None
            self._exhaustion_bars = 0

        # ── 5. Update trailing stop ──────────────────────────────────
        if self._in_position and self._entry_price and self._entry_price > 0:
            profit_pct = (
                (close - self._entry_price) / self._entry_price
                if self.direction == Direction.UP
                else (self._entry_price - close) / self._entry_price
            )
            if profit_pct >= self.TRAIL_TRIGGER_PCT:
                if self.direction == Direction.UP:
                    lock_level = self._entry_price * (1 + self.TRAIL_LOCK_PCT)
                    if self._trailing_stop is None or lock_level > self._trailing_stop:
                        self._trailing_stop = lock_level
                else:
                    lock_level = self._entry_price * (1 - self.TRAIL_LOCK_PCT)
                    if self._trailing_stop is None or lock_level < self._trailing_stop:
                        self._trailing_stop = lock_level

        # ── 6. Track exhaustion bars while in position ───────────────
        if self._in_position and self.regime == Regime.EXHAUSTION:
            self._exhaustion_bars += 1
        elif self.regime != Regime.EXHAUSTION:
            self._exhaustion_bars = 0

        # ── 7. Check exit conditions if in position ──────────────────
        if self._in_position and not warming_up:
            self._check_exit(close, signal_s)

        # ── 8. Save state for next bar ───────────────────────────────
        self._prev_close = close
        self._prev_roc = roc
        self._prev_ema_spread = ema_spread

        return self._snapshot(close, roc, signal_s, ema_spread, spread_expanding)

    # ── Regime state machine ─────────────────────────────────────────────

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
        """
        Regime state machine — faithful to the physics-based spec.

        Observable proxies:
          m  (momentum / velocity)  → ROC
          m  (trend direction)      → EMA3 vs EMA7 cross
          γ  (damping)              → EMA spread contraction/expansion rate
          σ  (noise)                → ATR
          U(p) (potential)          → volume profile nodes
          external force            → delta volume

        Continuation: m² ≫ γ·ΔU  →  S > 1.5, spread expanding
        Reversal:     γ·m dominates →  S < 1, spread shrinking, ROC flips
        """
        ema_up   = (self._ema_fast or 0.0) > (self._ema_slow or 0.0)
        ema_down = (self._ema_slow or 0.0) > (self._ema_fast or 0.0)

        # ════════════════════════════════════════════════════════════════
        # A. IDLE → TREND
        #
        # Trend begins when ALL THREE conditions are met:
        #   1. EMA3 > EMA7 (uptrend) or EMA7 > EMA3 (downtrend)
        #   2. EMA spread expanding: (EMA3 − EMA7)' > 0
        #   3. Signal strength S > 1
        #
        # Record: starting ATR, starting cumulative delta, trend start price.
        # Start a fresh volume profile anchored at trend start.
        # ════════════════════════════════════════════════════════════════
        if self.regime == Regime.IDLE:
            if (ema_up or ema_down) and spread_expanding and signal_s > self.S_TREND_ENTER:
                self.regime = Regime.TREND
                self.direction = Direction.UP if ema_up else Direction.DOWN
                self.trend_start_price = close
                self.trend_start_atr   = self._atr
                self.trend_start_delta = self.volume_profile.cumulative_delta()
                # Save old value area before resetting (for continuation checks)
                self._prev_value_area = self.volume_profile.value_area()
                # Fresh volume profile for this trend
                self.volume_profile.reset(close)
                self._vp_initialized = True

        # ════════════════════════════════════════════════════════════════
        # B. TREND → EXHAUSTION
        #
        # Exhaustion occurs when momentum decays (γ·m > m_drive).
        #
        # Conditions (ALL required):
        #   1. EMA spread shrinking
        #   2. ROC decreasing
        #   3. S < 1
        #   4. Price entering low volume node
        #
        # Fast-reversal path: if EMA has already flipped to the opposite
        # direction with structural confirmation → skip EXHAUSTION, go
        # directly to REVERSAL.
        # ════════════════════════════════════════════════════════════════
        elif self.regime == Regime.TREND:
            new_dir      = Direction.UP if ema_up else Direction.DOWN
            opposite_dir = Direction.DOWN if self.direction == Direction.UP else Direction.UP

            # Pre-compute reversal evidence for fast-reversal path
            atr_spike = (
                bool(self.trend_start_atr)
                and self.trend_start_atr > 0
                and bool(self._atr)
                and self._atr > self.ATR_REVERSAL_MULT * self.trend_start_atr
            )
            cum_delta = self.volume_profile.cumulative_delta()
            strong_opposite_delta = (
                (self.direction == Direction.UP   and cum_delta < -self.DELTA_THRESHOLD)
                or (self.direction == Direction.DOWN and cum_delta >  self.DELTA_THRESHOLD)
            )
            in_opposite_hvn = self.volume_profile.is_price_in_hvn(close, opposite_dir)

            # ── Fast-reversal: EMA flipped + strong evidence ─────────
            if (new_dir == opposite_dir
                    and signal_s > self.S_TREND_ENTER
                    and (atr_spike or strong_opposite_delta or in_opposite_hvn)):
                self.regime              = Regime.REVERSAL
                self._reversal_direction = opposite_dir

            # ── EMA flipped but weak — treat as exhaustion ───────────
            elif new_dir == opposite_dir:
                self.regime = Regime.EXHAUSTION

            # ── Slow exhaustion: all 4 conditions ────────────────────
            elif (spread_shrinking
                  and roc_decreasing
                  and signal_s < self.S_EXHAUSTION
                  and self.volume_profile.is_price_in_low_volume_node(close)):
                self.regime = Regime.EXHAUSTION

        # ════════════════════════════════════════════════════════════════
        # C. EXHAUSTION → REVERSAL (Kramers escape event)
        #
        # Reversal occurs when momentum flips. ALL conditions:
        #   1. ROC crosses zero
        #   2. ATR spike: ATR > 1.2 × ATR_trend_start
        #   3. Strong opposite delta: ΔV > threshold
        #   4. Price enters HVN of opposite trend direction
        #
        # Can return to TREND if momentum genuinely resumes:
        #   spread expanding + S > 1
        # ════════════════════════════════════════════════════════════════
        elif self.regime == Regime.EXHAUSTION:
            # ── Check for trend resumption ───────────────────────────
            if spread_expanding and signal_s > self.S_TREND_ENTER:
                self.regime = Regime.TREND
            else:
                opposite_dir = Direction.DOWN if self.direction == Direction.UP else Direction.UP

                # 1. ROC crosses zero
                roc_crossed_zero = (
                    (self.direction == Direction.UP   and roc < 0)
                    or (self.direction == Direction.DOWN and roc > 0)
                )
                # Also check transition this bar
                if self._prev_roc is not None and not roc_crossed_zero:
                    roc_crossed_zero = (
                        (self.direction == Direction.UP   and self._prev_roc >= 0 and roc < 0)
                        or (self.direction == Direction.DOWN and self._prev_roc <= 0 and roc > 0)
                    )

                # 2. ATR spike
                atr_spike = (
                    bool(self.trend_start_atr)
                    and self.trend_start_atr > 0
                    and bool(self._atr)
                    and self._atr > self.ATR_REVERSAL_MULT * self.trend_start_atr
                )

                # 3. Strong opposite delta
                cum_delta = self.volume_profile.cumulative_delta()
                strong_opposite_delta = (
                    (self.direction == Direction.UP   and cum_delta < -self.DELTA_THRESHOLD)
                    or (self.direction == Direction.DOWN and cum_delta >  self.DELTA_THRESHOLD)
                )

                # 4. Price in HVN of opposite direction
                in_opposite_hvn = self.volume_profile.is_price_in_hvn(close, opposite_dir)

                # ALL conditions required for Kramers escape
                if roc_crossed_zero and atr_spike and strong_opposite_delta and in_opposite_hvn:
                    self.regime              = Regime.REVERSAL
                    self._reversal_direction = opposite_dir

        # ════════════════════════════════════════════════════════════════
        # D. REVERSAL → CONTINUATION
        #
        # Continuation = particle has escaped the previous potential well.
        #
        # ALL conditions required:
        #   1. (Reversal already confirmed — implicit)
        #   2. EMA cross occurs in new direction
        #   3. S > 1.5
        #   4. Delta volume aligned with new direction, HVN at top
        #   5. Price outside previous value area
        #   6. No major opposite HVN immediately ahead
        #
        # On confirmation: fire entry signal, reset anchors, → TREND.
        # If S collapses → reversal failed → IDLE.
        # ════════════════════════════════════════════════════════════════
        elif self.regime == Regime.REVERSAL:
            new_ema_up = (self._ema_fast or 0.0) > (self._ema_slow or 0.0)
            new_dir    = Direction.UP if new_ema_up else Direction.DOWN

            # Condition 2: EMA cross confirms new direction
            ema_confirms = new_dir == self._reversal_direction

            # Condition 3: S > 1.5
            strong_s = signal_s > self.S_CONTINUATION

            # Condition 4: delta aligned + HVN of trend at top
            cum_delta = self.volume_profile.cumulative_delta()
            delta_aligned = (
                (self._reversal_direction == Direction.UP   and cum_delta > 0)
                or (self._reversal_direction == Direction.DOWN and cum_delta < 0)
            )
            hvn_at_top = self.volume_profile.hvn_at_top(self._reversal_direction)

            # Condition 5: price outside previous value area
            va_lo, va_hi = self._prev_value_area
            price_outside_va = (va_lo == 0.0 and va_hi == 0.0) or close < va_lo or close > va_hi

            # Condition 6: no major opposite HVN immediately ahead
            no_opposite_hvn = not self.volume_profile.has_opposite_hvn_ahead(
                close, self._reversal_direction
            )

            # ALL conditions must be satisfied
            if (ema_confirms and strong_s
                    and delta_aligned and hvn_at_top
                    and price_outside_va and no_opposite_hvn):
                # ── Continuation confirmed ───────────────────────────
                self.regime    = Regime.CONTINUATION
                self.direction = self._reversal_direction

                # Fire entry signal
                if not self._in_position:
                    self.entry_signal = (
                        Signal.BUY if self.direction == Direction.UP else Signal.SELL
                    )
                    self._in_position = True

                # Save old value area, reset anchors for the new trend leg
                self._prev_value_area  = self.volume_profile.value_area()
                self.trend_start_price = close
                self.trend_start_atr   = self._atr
                self.trend_start_delta = self.volume_profile.cumulative_delta()
                self.volume_profile.reset(close)
                self._vp_initialized = True

                # Transition into TREND for the new direction
                self.regime = Regime.TREND

            # Reversal failed — S collapsed
            elif signal_s < self.S_REVERSAL_FAIL:
                self.regime              = Regime.IDLE
                self.direction           = Direction.NONE
                self._reversal_direction = Direction.NONE

    # ── Exit rules ───────────────────────────────────────────────────────

    def _check_exit(self, close: float, signal_s: float):
        """
        Exit Rules — any single condition triggers immediate exit:

        1. Signal strength S < 1  (momentum lost)
        2. Price enters low volume node of current trend (thin structure)
        3. Price enters major HVN of opposite trend direction (resistance)
        4. Trailing stop: profit ≥ 5% → lock stop at entry +5%
        5. Prolonged exhaustion (≥ N bars in EXHAUSTION while in position)
        6. Regime enters REVERSAL → immediate exit
        """
        exit_reason: Optional[str] = None

        # ── Rule 6: regime entered REVERSAL (highest priority) ───────
        if self.regime == Regime.REVERSAL:
            exit_reason = "reversal"

        # ── Rule 4: trailing stop hit ────────────────────────────────
        if exit_reason is None and self._trailing_stop is not None:
            if self.direction == Direction.UP and close <= self._trailing_stop:
                exit_reason = "trailing_stop"
            elif self.direction == Direction.DOWN and close >= self._trailing_stop:
                exit_reason = "trailing_stop"

        # ── Rule 1: signal strength drops S < 1 ─────────────────────
        if exit_reason is None and signal_s < self.S_EXIT:
            exit_reason = "signal_weak"

        # ── Rule 2: price in LVN of current trend ───────────────────
        if exit_reason is None:
            if self.volume_profile.is_price_in_low_volume_node(close):
                exit_reason = "lvn_current_trend"

        # ── Rule 3: price in HVN of opposite trend ──────────────────
        if exit_reason is None:
            opposite_dir = Direction.DOWN if self.direction == Direction.UP else Direction.UP
            if self.volume_profile.is_price_in_hvn(close, opposite_dir):
                exit_reason = "hvn_opposite_trend"

        # ── Rule 5: prolonged exhaustion / consolidation ─────────────
        if exit_reason is None and self._exhaustion_bars >= self.EXHAUSTION_EXIT_BARS:
            exit_reason = "exhaustion_consolidation"

        # ── Fire exit ────────────────────────────────────────────────
        if exit_reason is not None:
            self.exit_signal = Signal.SELL if self.direction == Direction.UP else Signal.BUY
            self._in_position = False
            self._entry_price = None
            self._trailing_stop = None
            self._exhaustion_bars = 0
            # Only reset regime to IDLE when exit isn't already a recognised
            # regime transition (REVERSAL manages its own state)
            if self.regime not in (Regime.REVERSAL, Regime.EXHAUSTION):
                self.regime = Regime.IDLE
                self.direction = Direction.NONE

    def notify_trade_closed(self):
        """Called by trade simulator when position is closed externally."""
        self._in_position = False
        self._entry_price = None
        self._trailing_stop = None
        self._exhaustion_bars = 0

    # ── Snapshot ─────────────────────────────────────────────────────────

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
            "ema_spread": round(ema_spread, 10),
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
            "entry_price": self._entry_price,
            "trailing_stop": round(self._trailing_stop, 10) if self._trailing_stop else None,
            "exhaustion_bars": self._exhaustion_bars,
            "volume_profile": self.volume_profile.to_dict(),
            "bar_count": self._bar_count,
            "warming_up": self._bar_count < max(self.ema_slow_n, self.roc_period) + 2,
        }