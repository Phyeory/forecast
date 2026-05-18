"""
Strategy Engine — Physics-based (Langevin dynamics) regime detection.

Regimes:  IDLE → TREND → EXHAUSTION → REVERSAL → CONTINUATION → TREND …

Observable proxies:
  m   (momentum)       → Kalman-filtered momentum (m_hat)
  m   (trend dir)      → EMA(3) vs EMA(7)
  γ   (damping)        → EMA spread contraction rate
  σ   (noise)          → ATR
  U(p)(potential)      → Volume profile nodes
  external force       → Delta volume

Core signal:  S = |m_hat| / ATR

═══════════════════════════════════════════════════════════════════════════════
PATCH NOTES  (3 targeted fixes — all labelled FIX-A / FIX-B / FIX-C)
═══════════════════════════════════════════════════════════════════════════════

FIX-A  Top-blast prevention
  Problem: spike filter used avg body of only ~3 prior bars.  During a rally
           every bar is large, so the blow-off top candle looks "normal" and
           passes the gate.
  Changes:
    1. body_baseline_bars (default 25) — a separate, longer window for body
       average that is always taken from bars BEFORE the recent spike_lookback
       window.  This anchors the comparison to calm-market bodies, not
       recent-pump bodies.
    2. overextension_k (default 0.012) — if close > p_hat * (1 + k), price
       is overextended vs Kalman estimate; hard-block BUY entry.
    3. momentum_peak_bars (default 4) — if |m_hat| has been declining for
       this many consecutive bars, we are already past the momentum peak;
       block BUY entry regardless of S.

FIX-B  Consolidation / false DB prevention
  Problem: in the confidence ambiguous zone (confidence_low < conf < confidence_high)
           the code called `pass` for in-position but then fell through to
           _detect_regime() which could still return a BUY signal.  Also,
           the position_in_range gate (< 0.4) did not widen when the range
           itself was tiny (genuine consolidation).
  Changes:
    1. Ambiguous-zone path now explicitly sets signal = None and returns
       immediately after handling exits.  No state-machine code runs.
    2. Tight-range gate: if the N-bar range is smaller than
       consolidation_range_pct % of price AND we are between 35–65% of
       that range, block the entry.  This is the "inside a box" detector.
    3. confidence_high raised from 0.60 → 0.62 (minor tightening to reduce
       DB noise without cutting off real trends).

FIX-C  Stop missing real uptrends
  Problem: _pre_entry_stable required stability_bars (2) consecutive
           monotonically increasing m_hat bars.  Kalman lag means m_hat
           often dips for 1–2 bars right after a breakout starts, blocking
           every valid first-leg entry.
  Changes:
    1. When trend_confidence > confidence_very_high (default 0.72), reduce
       the effective stability requirement to 1 bar.
    2. The EXHAUSTION → CONTINUATION cold-start path no longer requires
       exhaustion_persist_bars wait when S > S_strong AND m_hat > 0 AND
       the overextension check passes.  The persist gate was originally
       meant to filter noise; with S > S_strong the signal is already strong.
    3. Added _momentum_peak_declining() helper used by FIX-A and FIX-C.
"""

from __future__ import annotations
import math
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional


# ── Enums ──────────────────────────────────────────────────────────────────────

class Regime(Enum):
    IDLE        = "idle"
    TREND       = "trend"
    EXHAUSTION  = "exhaustion"
    REVERSAL    = "reversal"
    CONTINUATION = "continuation"

class Direction(Enum):
    UP   = "up"
    DOWN = "down"
    NONE = "none"

class Signal(Enum):
    NONE = "none"
    BUY  = "buy"
    SELL = "sell"
    EXIT = "exit"


# ── Volume Profile ────────────────────────────────────────────────────────────

@dataclass
class VolumeBin:
    price_low: float
    price_high: float
    buy_volume: float = 0.0
    sell_volume: float = 0.0

    @property
    def total_volume(self) -> float:
        return self.buy_volume + self.sell_volume

    @property
    def delta(self) -> float:
        return self.buy_volume - self.sell_volume


class VolumeProfile:
    """Fixed-range volume profile for a single trend."""

    def __init__(self, start_price: float, num_bins: int = 30):
        self.start_price = start_price
        self.num_bins = num_bins
        self.price_min = start_price
        self.price_max = start_price
        self.bins: list[VolumeBin] = []
        self.start_time: Optional[int] = None
        self.end_time: Optional[int] = None
        self._rebuild_bins()

    def _rebuild_bins(self):
        old_bins = self.bins[:]
        low = self.price_min
        high = self.price_max
        if high <= low:
            high = low * 1.01 if low > 0 else low + 0.0001

        bin_size = (high - low) / self.num_bins
        self.bins = []
        for i in range(self.num_bins):
            bl = low + i * bin_size
            bh = low + (i + 1) * bin_size
            self.bins.append(VolumeBin(price_low=bl, price_high=bh))

        for ob in old_bins:
            mid = (ob.price_low + ob.price_high) / 2
            idx = self._bin_index(mid)
            if idx is not None:
                self.bins[idx].buy_volume += ob.buy_volume
                self.bins[idx].sell_volume += ob.sell_volume

    def _bin_index(self, price: float) -> Optional[int]:
        if not self.bins or self.price_max <= self.price_min:
            return None
        low, high = self.price_min, self.price_max
        if high <= low:
            high = low * 1.01 if low > 0 else low + 0.0001
        idx = int((price - low) / (high - low) * self.num_bins)
        return max(0, min(self.num_bins - 1, idx))

    def add_trade(self, price: float, volume: float, is_buy: bool, time: int):
        if self.start_time is None:
            self.start_time = time
        self.end_time = time

        expanded = False
        if price < self.price_min:
            self.price_min = price
            expanded = True
        if price > self.price_max:
            self.price_max = price
            expanded = True
        if expanded:
            self._rebuild_bins()

        idx = self._bin_index(price)
        if idx is not None:
            if is_buy:
                self.bins[idx].buy_volume += volume
            else:
                self.bins[idx].sell_volume += volume

    def get_hvn_bins(self, top_n: int = 5) -> list[VolumeBin]:
        sorted_bins = sorted(self.bins, key=lambda b: b.total_volume, reverse=True)
        return sorted_bins[:top_n]

    def get_lvn_bins(self) -> list[VolumeBin]:
        if not self.bins:
            return []
        sorted_bins = sorted(self.bins, key=lambda b: b.total_volume)
        cutoff = max(1, int(len(sorted_bins) * 0.3))
        return sorted_bins[:cutoff]

    def is_price_in_hvn(self, price: float, direction: Optional[Direction] = None, top_n: int = 5) -> bool:
        hvns = self.get_hvn_bins(top_n)
        for b in hvns:
            if b.price_low <= price <= b.price_high:
                if direction is None:
                    return True
                if direction == Direction.UP and b.delta > 0:
                    return True
                if direction == Direction.DOWN and b.delta < 0:
                    return True
        return False

    def is_price_in_lvn(self, price: float) -> bool:
        lvns = self.get_lvn_bins()
        for b in lvns:
            if b.price_low <= price <= b.price_high:
                return True
        return False

    @property
    def cumulative_delta(self) -> float:
        return sum(b.delta for b in self.bins)

    def to_dict(self) -> dict:
        return {
            "start_price": self.start_price,
            "price_min": self.price_min,
            "price_max": self.price_max,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "bins": [
                {
                    "price_low": b.price_low,
                    "price_high": b.price_high,
                    "buy_volume": b.buy_volume,
                    "sell_volume": b.sell_volume,
                    "total_volume": b.total_volume,
                    "delta": b.delta,
                }
                for b in self.bins
            ],
        }


# ── Helper: EMA calculation ──────────────────────────────────────────────────

def ema_step(prev: float, value: float, period: int) -> float:
    k = 2.0 / (period + 1)
    return value * k + prev * (1 - k)


# ── Kalman Filter for Momentum Estimation ────────────────────────────────────

class KalmanFilterMomentum:
    """
    2-state Kalman filter for price + momentum estimation.
    State vector: x = [p, m]^T  (price, momentum)
    """

    def __init__(self, gamma: float = 0.1, q_price: float = 0.01,
                 q_momentum: float = 0.05, r_measure: float = 1.0):
        self.gamma = gamma
        self.decay = 1.0 - gamma

        self.q_price = q_price
        self.q_momentum = q_momentum
        self.r_measure = r_measure

        self.p: float = 0.0
        self.m: float = 0.0

        self.P00: float = 1.0
        self.P01: float = 0.0
        self.P11: float = 1.0

        self._price_buf: list[float] = []
        self._var_window = 10

        self.initialised = False

    def _auto_r(self, price: float):
        buf = self._price_buf
        buf.append(price)
        if len(buf) > self._var_window:
            buf.pop(0)
        n = len(buf)
        if n >= 3:
            mean = sum(buf) / n
            var = sum((x - mean) * (x - mean) for x in buf) / n
            if var > 0:
                self.r_measure = var

    def update(self, price: float) -> tuple[float, float]:
        self._auto_r(price)

        if not self.initialised:
            self.p = price
            self.m = 0.0
            self.P00 = 1.0
            self.P01 = 0.0
            self.P11 = 1.0
            self.initialised = True
            return self.p, self.m

        decay = self.decay

        p_pred = self.p + self.m
        m_pred = decay * self.m

        P00 = self.P00
        P01 = self.P01
        P11 = self.P11

        fp00 = P00 + P01
        fp01 = P01 + P11
        fp10 = decay * P01
        fp11 = decay * P11

        pp00 = fp00 + fp01 + self.q_price
        pp01 = fp01 * decay
        pp11 = fp11 * decay + self.q_momentum

        y = price - p_pred
        S = pp00 + self.r_measure
        S_inv = 1.0 / S

        pp10 = fp10 + fp11
        K0 = pp00 * S_inv
        K1 = pp10 * S_inv

        self.p = p_pred + K0 * y
        self.m = m_pred + K1 * y

        self.P00 = (1.0 - K0) * pp00
        self.P01 = (1.0 - K0) * pp01
        self.P11 = -K1 * pp01 + pp11

        return self.p, self.m

    def reset(self):
        self.p = 0.0
        self.m = 0.0
        self.P00 = 1.0
        self.P01 = 0.0
        self.P11 = 1.0
        self.initialised = False
        self._price_buf.clear()


# ── Strategy Engine ──────────────────────────────────────────────────────────

class StrategyEngine:
    """
    Full physics-based regime detection engine.

    Feed it OHLCV candles sequentially via `update()`.
    Returns a dict with regime, direction, signal, indicators, and volume profiles.
    """

    def __init__(
        self,
        ema_fast: int = 3,
        ema_slow: int = 7,
        atr_period: int = 7,
        roc_period: int = 3,
        warmup: int = 10,
        signal_strong: float = 3.5,
        signal_weak: float = 0.8,
        signal_noise: float = 1.0535714285714286,
        exhaustion_bars_limit: int = 1,
        delta_threshold: float = 0.3,
        kalman_gamma: float = 0.23,
        min_trend_bars: int = 2,
        reversal_confirm_bars: int = 1,
        chop_atr_pct: float = 0.3,
        chop_spread_pct: float = 0.15,
        reversal_exit_confirm_bars: int = 1,
        s_effective_threshold: float = 0.35,
        exhaustion_persist_bars: int = 5,
        regime_lookback: int = 5,
        persistence_threshold: int = 2,
        momentum_mean_threshold: float = 0.0,
        ema_min_spread_pct: float = 0.02,
        # FIX-B: raised from 0.60 → 0.62 to tighten consolidation gate
        confidence_high: float = 0.785,
        confidence_low: float = 0.45571428571428574,
        confidence_w1: float = 0.3,
        confidence_w2: float = 0.25,
        confidence_w3: float = 0.25,
        confidence_w4: float = 0.2,
        atr_floor_k: float = 0.6,
        ema_cross_persist_bars: int = 3, #might consider changing to 3
        exhaustion_s_decay_bars: int = 1,
        exhaustion_stall_bars: int = 3,
        exhaustion_stall_atr_pct: float = 0.35,
        local_range_bars: int = 13,
        local_range_threshold_pct: float = 0.7,
        sign_flip_threshold: int = 2,
        stability_bars: int = 5,
        spike_atr_multiplier: float = 1.2,
        spike_lookback_bars: int = 5,
        # ── FIX-A: new top-blast parameters ──────────────────────────
        body_baseline_bars: int = 20,
        # ^ Long window for body average — anchors comparison to calm bars,
        #   not the recent pump bars.  Must be >> spike_lookback_bars.
        overextension_k: float = 0.17,
        # ^ If close > p_hat * (1 + k) AND S > S_strong, price is a blow-off
        #   (both overextended AND signal already peaked).  0.04 = 4% above
        #   Kalman estimate.  Smaller values block valid mid-trend entries due
        #   to normal Kalman lag.
        momentum_peak_bars: int = 2,  # tuned
        # ^ If |m_hat| has been declining for this many consecutive bars,
        #   we are past the momentum peak → block BUY regardless of S.
        # ── FIX-B: consolidation range gate parameter ─────────────────
        consolidation_range_pct: float = 1.7,
        # ^ If N-bar range < this % of price AND price is in mid 35–65%
        #   of that range, it's a box / consolidation → block entry.
        # ── FIX-C: high-confidence stability relaxation ────────────────
        confidence_very_high: float = 0.8448571428571429,
        # ^ When confidence exceeds this, reduce effective stability_bars to 1.
        # ── Macro trend gate ─────────────────────────────────────────────
        ema_macro_period: int = 5,
        # ^ Slow EMA lookback used to define the macro trend.  Only BUY when
        #   close >= ema_macro.  Set to 0 to disable.
    ):
        self.ema_fast_p = ema_fast
        self.ema_slow_p = ema_slow
        self.atr_period = atr_period
        self.roc_period = roc_period
        self.warmup = warmup
        self.S_strong = signal_strong
        self.S_weak = signal_weak
        self.S_noise = signal_noise
        self.exhaustion_bars_limit = exhaustion_bars_limit
        self.delta_threshold = delta_threshold
        self.min_trend_bars = min_trend_bars
        self.reversal_confirm_bars = reversal_confirm_bars
        self.chop_atr_pct = chop_atr_pct
        self.chop_spread_pct = chop_spread_pct
        self.reversal_exit_confirm_bars = reversal_exit_confirm_bars
        self.s_effective_threshold = s_effective_threshold
        self.exhaustion_persist_bars = exhaustion_persist_bars

        self.regime_lookback = regime_lookback
        self.persistence_threshold = persistence_threshold
        self.momentum_mean_threshold = momentum_mean_threshold
        self.ema_min_spread_pct = ema_min_spread_pct
        self.confidence_high = confidence_high
        self.confidence_low = confidence_low
        self.confidence_w1 = confidence_w1
        self.confidence_w2 = confidence_w2
        self.confidence_w3 = confidence_w3
        self.confidence_w4 = confidence_w4
        self.atr_floor_k = atr_floor_k
        self.ema_cross_persist_bars = ema_cross_persist_bars
        self.exhaustion_s_decay_bars = exhaustion_s_decay_bars
        self.local_range_bars = local_range_bars
        self.local_range_threshold_pct = local_range_threshold_pct
        self.sign_flip_threshold = sign_flip_threshold
        self.stability_bars = stability_bars
        self.exhaustion_stall_bars = exhaustion_stall_bars
        self.exhaustion_stall_atr_pct = exhaustion_stall_atr_pct
        self.spike_atr_multiplier = spike_atr_multiplier
        self.spike_lookback_bars = spike_lookback_bars

        # FIX-A parameters
        self.body_baseline_bars = body_baseline_bars
        self.overextension_k = overextension_k
        self.momentum_peak_bars = momentum_peak_bars

        # FIX-B parameters
        self.consolidation_range_pct = consolidation_range_pct

        # FIX-C parameters
        self.confidence_very_high = confidence_very_high

        # Macro trend gate parameter
        self.ema_macro_period = ema_macro_period

        # State
        self.bar_count = 0
        self.regime = Regime.EXHAUSTION
        self.direction = Direction.NONE
        self.prev_direction = Direction.NONE
        self.trend_before_exhaustion = Direction.NONE

        # Indicator state
        self.ema_fast_val: Optional[float] = None
        self.ema_slow_val: Optional[float] = None
        self.ema_macro_val: Optional[float] = None  # macro trend EMA
        self.atr_val: Optional[float] = None
        self.m_hat: float = 0.0
        self.prev_m_hat: float = 0.0
        self.p_hat: float = 0.0
        self.momentum_acceleration: float = 0.0

        # Kalman filter
        self.kalman = KalmanFilterMomentum(gamma=kalman_gamma)
        self.signal_strength: float = 0.0
        self.s_effective: float = 0.0

        # Price / OHLC history
        self.close_history: list[float] = []
        self.high_history: list[float] = []
        self.low_history: list[float] = []
        self.open_history: list[float] = []

        # EMA spread tracking
        self.prev_ema_spread: float = 0.0
        self.ema_spread: float = 0.0
        self.spread_expanding: bool = False

        # Trend anchors
        self.trend_start_price: float = 0.0
        self.trend_start_atr: float = 0.0
        self.trend_start_delta: float = 0.0
        self.trend_start_bar: int = 0
        self.exhaustion_bar_count: int = 0
        self.trend_bar_count: int = 0

        self.exhaustion_persist_count: int = 0
        self.reversal_confirm_count: int = 0
        self.trend_reversal_confirm_count: int = 0
        self.reversal_bar_count: int = 0

        # Volume profiles
        self.volume_profiles: list[VolumeProfile] = []
        self.current_profile: Optional[VolumeProfile] = None

        # Trade tracking
        self.in_position = False
        self.entry_price: float = 0.0
        self.position_direction: Direction = Direction.NONE

        # Rolling buffers
        self._m_hat_history: list[float] = []
        self._abs_m_hat_history: list[float] = []
        self._atr_history: list[float] = []
        self._signal_strength_history: list[float] = []
        self._ema_spread_history: list[float] = []

        # Regime filter results
        self.is_trending: bool = False
        self.trend_confidence: float = 0.0
        self.atr_floor: float = 0.0

        # Cross / stability state
        self._ema_cross_persist_count: int = 0
        self._ema_cross_valid: bool = False
        self._exhaustion_s_decay_count: int = 0
        self._in_local_chop: bool = False
        self._pre_entry_stable_up: bool = False
        self._pre_entry_stable_down: bool = False
        self._pre_entry_stable: bool = False

        # Fix 3a: track highest close seen during the current EXHAUSTION phase
        self._exhaustion_phase_high: float = 0.0

        # FIX-A: momentum-peak tracking
        self._momentum_peak_declining_count: int = 0
        # ^ counts consecutive bars where |m_hat| decreased

    # ── Indicators ────────────────────────────────────────────────────────────

    def _update_indicators(self, o: float, h: float, l: float, c: float, vol: float):
        self.close_history.append(c)
        self.high_history.append(h)
        self.low_history.append(l)
        self.open_history.append(o)

        # EMA
        if self.ema_fast_val is None:
            self.ema_fast_val = c
            self.ema_slow_val = c
        else:
            self.ema_fast_val = ema_step(self.ema_fast_val, c, self.ema_fast_p)
            self.ema_slow_val = ema_step(self.ema_slow_val, c, self.ema_slow_p)

        # Macro trend EMA (slow; used as a buy-side gate)
        if self.ema_macro_period > 0:
            if self.ema_macro_val is None:
                self.ema_macro_val = c
            else:
                self.ema_macro_val = ema_step(self.ema_macro_val, c, self.ema_macro_period)

        # EMA spread
        self.prev_ema_spread = self.ema_spread
        self.ema_spread = self.ema_fast_val - self.ema_slow_val
        self.spread_expanding = abs(self.ema_spread) > abs(self.prev_ema_spread)

        # ATR
        if len(self.close_history) >= 2:
            prev_c = self.close_history[-2]
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        else:
            tr = h - l

        if self.atr_val is None:
            self.atr_val = tr
        else:
            self.atr_val = ema_step(self.atr_val, tr, self.atr_period)

        # Kalman momentum
        self.prev_m_hat = self.m_hat
        self.p_hat, self.m_hat = self.kalman.update(c)
        self.momentum_acceleration = self.m_hat - self.prev_m_hat

        # FIX-A: update momentum-peak decline counter
        if abs(self.m_hat) < abs(self.prev_m_hat):
            self._momentum_peak_declining_count += 1
        else:
            self._momentum_peak_declining_count = 0

        # ATR floor
        self._atr_history.append(self.atr_val or 0.0)
        if len(self._atr_history) > self.regime_lookback * 4:
            self._atr_history = self._atr_history[-(self.regime_lookback * 4):]
        if len(self._atr_history) >= 3:
            sorted_atr = sorted(self._atr_history)
            rolling_median_atr = sorted_atr[len(sorted_atr) // 2]
        else:
            rolling_median_atr = self.atr_val or 0.0
        self.atr_floor = max(self.atr_val or 0.0, self.atr_floor_k * rolling_median_atr)

        # Signal strength S
        if self.atr_floor > 0 and c > 0:
            m_hat_pct = (self.m_hat / c) * 100 * self.roc_period
            atr_floor_pct = (self.atr_floor / c) * 100
            if atr_floor_pct > 0:
                self.signal_strength = abs(m_hat_pct) / atr_floor_pct
            else:
                self.signal_strength = 0.0
        else:
            self.signal_strength = 0.0

        # Rolling buffers
        N = self.regime_lookback
        self._m_hat_history.append(self.m_hat)
        if len(self._m_hat_history) > N * 2:
            self._m_hat_history = self._m_hat_history[-(N * 2):]

        self._abs_m_hat_history.append(abs(self.m_hat))
        if len(self._abs_m_hat_history) > N * 2:
            self._abs_m_hat_history = self._abs_m_hat_history[-(N * 2):]

        self._signal_strength_history.append(self.signal_strength)
        if len(self._signal_strength_history) > N * 2:
            self._signal_strength_history = self._signal_strength_history[-(N * 2):]

        spread_mag = abs(self.ema_spread) if self.ema_spread else 0.0
        self._ema_spread_history.append(spread_mag)
        if len(self._ema_spread_history) > N * 2:
            self._ema_spread_history = self._ema_spread_history[-(N * 2):]

        # Barrier-adjusted signal
        self.s_effective = self._compute_s_effective(c)

        # Regime filter, confidence, EMA cross, chop, stability
        self._update_regime_filter(c)
        self._update_ema_cross_validation(c)
        self._update_local_chop(c)
        self._update_pre_entry_stability()

    # ── FIX-A: Momentum-peak helper ───────────────────────────────────────────

    def _momentum_past_peak(self) -> bool:
        """Returns True when |m_hat| has been declining for momentum_peak_bars
        consecutive bars.  Indicates we are past the momentum peak — BUY
        entries at this point almost always end up buying a top.
        """
        return self._momentum_peak_declining_count >= self.momentum_peak_bars

    def _price_overextended(self, c: float) -> bool:
        """Returns True when close is above Kalman p_hat by > overextension_k.
        Price has run too far ahead of the model estimate — high reversal risk.
        """
        if self.p_hat <= 0 or c <= 0:
            return False
        return c > self.p_hat * (1.0 + self.overextension_k)

    def _spike_on_long_baseline(self, c: float) -> bool:
        """FIX-A: Compare last candle body against a LONG baseline window
        (body_baseline_bars) that is anchored BEFORE the recent pump bars.

        Old code used only spike_lookback_bars (~4) — during a rally those
        4 bars are all large, so the blow-off candle looks normal.

        New logic:
          1. Collect bodies from the last (body_baseline_bars + spike_lookback_bars) bars.
          2. Use the EARLIER body_baseline_bars as the baseline (calm period).
          3. Compare the most recent candle body against that calm baseline.
        """
        total_needed = self.body_baseline_bars + self.spike_lookback_bars
        if len(self.open_history) < total_needed:
            # Not enough history yet — fall back to original short-window check
            if len(self.open_history) >= 2:
                lookback = min(self.spike_lookback_bars, len(self.open_history))
                recent_bodies = [
                    abs(self.close_history[i] - self.open_history[i])
                    for i in range(-lookback, 0)
                ]
                if len(recent_bodies) >= 2:
                    avg_prior = sum(recent_bodies[:-1]) / len(recent_bodies[:-1])
                    if avg_prior > 0 and recent_bodies[-1] > self.spike_atr_multiplier * avg_prior:
                        return True
            return False

        # Calm-period bodies (older portion of the window)
        calm_start = -(self.body_baseline_bars + self.spike_lookback_bars)
        calm_end   = -self.spike_lookback_bars
        calm_bodies = [
            abs(self.close_history[i] - self.open_history[i])
            for i in range(calm_start, calm_end)
        ]
        avg_calm = sum(calm_bodies) / len(calm_bodies) if calm_bodies else 0.0

        # Last candle body
        last_body = abs(self.close_history[-1] - self.open_history[-1])

        if avg_calm > 0 and last_body > self.spike_atr_multiplier * avg_calm:
            return True

        return False

    # ── §1–§3: Global Regime Filter + Trend Confidence ───────────────────────

    def _update_regime_filter(self, c: float):
        N = self.regime_lookback

        if len(self._m_hat_history) < N:
            self.is_trending = False
            self.trend_confidence = 0.0
            return

        recent_m = self._m_hat_history[-N:]
        recent_abs_m = self._abs_m_hat_history[-N:]

        # A) Persistence
        current_sign = 1 if recent_m[-1] >= 0 else -1
        persistence_count = 0
        for v in reversed(recent_m):
            if (v >= 0 and current_sign == 1) or (v < 0 and current_sign == -1):
                persistence_count += 1
            else:
                break
        persistence_ok = persistence_count >= self.persistence_threshold

        # B) Momentum strength
        rolling_mean_abs_m = sum(recent_abs_m) / len(recent_abs_m)
        wide_abs_m = self._abs_m_hat_history
        long_mean_abs_m = sum(wide_abs_m) / len(wide_abs_m) if wide_abs_m else 0.0
        momentum_ok = (
            abs(self.m_hat) > long_mean_abs_m * 1.1
            and rolling_mean_abs_m > 0
        )

        # C) Volatility expansion
        atr_hist = self._atr_history
        if len(atr_hist) >= 5:
            sorted_atr = sorted(atr_hist)
            rolling_median_atr = sorted_atr[len(sorted_atr) // 2]
        else:
            rolling_median_atr = self.atr_val or 0.0
        volatility_ok = (self.atr_val or 0.0) > rolling_median_atr * 1.1

        # D) EMA separation
        if c > 0 and self.ema_fast_val is not None and self.ema_slow_val is not None:
            ema_sep_pct = abs(self.ema_fast_val - self.ema_slow_val) / c * 100
        else:
            ema_sep_pct = 0.0
        ema_sep_ok = ema_sep_pct > self.ema_min_spread_pct

        self.is_trending = all([persistence_ok, momentum_ok, volatility_ok, ema_sep_ok])

        # Confidence components
        persistence_frac = min(persistence_count / N, 1.0)

        if long_mean_abs_m > 0:
            norm_momentum = min(abs(self.m_hat) / long_mean_abs_m, 2.0) / 2.0
        else:
            norm_momentum = 0.0

        if rolling_median_atr > 0:
            vol_expansion = min((self.atr_val or 0.0) / rolling_median_atr, 2.0) / 2.0
        else:
            vol_expansion = 0.0

        ema_sep_norm = min(ema_sep_pct / max(self.ema_min_spread_pct * 3, 0.01), 1.0)

        self.trend_confidence = (
            self.confidence_w1 * persistence_frac
            + self.confidence_w2 * norm_momentum
            + self.confidence_w3 * vol_expansion
            + self.confidence_w4 * ema_sep_norm
        )

        # Momentum age penalty
        if len(self._m_hat_history) >= 2:
            run_sign = 1 if self._m_hat_history[-1] >= 0 else -1
            run_length = 0
            for v in reversed(self._m_hat_history):
                if (v >= 0 and run_sign == 1) or (v < 0 and run_sign == -1):
                    run_length += 1
                else:
                    break
            age_ratio = run_length / max(self.regime_lookback, 1)
            if age_ratio > 2.0:
                age_penalty = min((age_ratio - 2.0) * 0.12, 0.25)
                self.trend_confidence -= age_penalty

        self.trend_confidence = max(0.0, min(1.0, self.trend_confidence))

    # ── §5: EMA Cross Validation ──────────────────────────────────────────────

    def _update_ema_cross_validation(self, c: float):
        if c <= 0:
            self._ema_cross_valid = False
            return

        spread_mag_pct = abs(self.ema_spread) / c * 100 if c > 0 else 0.0
        spread_derivative = abs(self.ema_spread) - abs(self.prev_ema_spread)

        cond_increasing = spread_derivative > 0
        cond_magnitude = spread_mag_pct > self.ema_min_spread_pct
        cond_all = cond_increasing and cond_magnitude

        if cond_all:
            self._ema_cross_persist_count += 1
        else:
            self._ema_cross_persist_count = 0

        self._ema_cross_valid = (
            self._ema_cross_persist_count >= self.ema_cross_persist_bars
        )

    # ── §8: Local Range Anti-Chop Filter ─────────────────────────────────────

    def _update_local_chop(self, c: float):
        N = self.local_range_bars
        if len(self.close_history) < N or c <= 0:
            self._in_local_chop = False
            return

        prices = self.close_history[-N:]
        local_range = max(prices) - min(prices)
        range_pct = (local_range / c) * 100
        range_small = range_pct < self.local_range_threshold_pct

        m_recent = self._m_hat_history[-N:] if len(self._m_hat_history) >= N else self._m_hat_history
        sign_flips = 0
        for i in range(1, len(m_recent)):
            if m_recent[i] * m_recent[i - 1] < 0:
                sign_flips += 1

        self._in_local_chop = range_small and sign_flips >= self.sign_flip_threshold

    # ── §9: Pre-Entry Stability Check ────────────────────────────────────────

    def _update_pre_entry_stability(self):
        """FIX-C: When confidence is very high (> confidence_very_high), reduce
        the effective stability requirement from stability_bars → 1.  Kalman lag
        means m_hat is often still recovering for 1–2 bars after a breakout;
        requiring monotonic increase blocks too many valid first-leg entries.
        """
        # FIX-C: effective N depends on confidence level
        if self.trend_confidence >= self.confidence_very_high:
            N = 1  # only need 1 bar when confidence is very high
        else:
            N = self.stability_bars

        if len(self._m_hat_history) < N + 1 or len(self._signal_strength_history) < N + 1:
            self._pre_entry_stable_up = False
            self._pre_entry_stable_down = False
            self._pre_entry_stable = False
            return

        m_recent = self._m_hat_history[-(N + 1):]
        s_recent = self._signal_strength_history[-(N + 1):]

        m_increasing_up   = all(m_recent[i + 1] > m_recent[i] for i in range(N))
        m_increasing_down = all(m_recent[i + 1] < m_recent[i] for i in range(N))
        s_increasing = all(s_recent[i + 1] > s_recent[i] for i in range(N))

        accel_up   = self.momentum_acceleration > 0
        accel_down = self.momentum_acceleration < 0

        m_hat_positive = self.m_hat > 0
        m_hat_negative = self.m_hat < 0

        self._pre_entry_stable_up = (
            m_hat_positive and (
                m_increasing_up
                or (s_increasing and accel_up)
            )
        )
        self._pre_entry_stable_down = (
            m_hat_negative and (
                m_increasing_down
                or (s_increasing and accel_down)
            )
        )

        self._pre_entry_stable = self._pre_entry_stable_up or self._pre_entry_stable_down

    # ── §7 + §10: Unified Entry Gate ─────────────────────────────────────────

    def _passes_entry_gate(self, c: float, direction: Direction) -> bool:
        """FIX-A + FIX-B + macro-trend gate applied here in addition to existing checks."""

        # ── Macro trend gate: block BUY when price is below the slow EMA ──
        if direction == Direction.UP:
            if self.ema_macro_val is not None and c < self.ema_macro_val:
                return False

        # §3: Confidence must be above HIGH_THRESHOLD
        if self.trend_confidence < self.confidence_high:
            return False

        # §8: No trading in local chop
        if self._in_local_chop:
            return False

        # §9: Pre-entry stability
        if direction == Direction.UP and not self._pre_entry_stable_up:
            return False
        if direction == Direction.DOWN and not self._pre_entry_stable_down:
            return False

        # ── FIX-A: Top-blast guards ───────────────────────────────────
        if direction == Direction.UP:
            # 1. Price overextended above Kalman estimate — only block when
            #    signal is ALSO strong (i.e. blow-off: fast price + strong S).
            #    With only overextension_k (originally 0.012), the Kalman lag
            #    alone causes close > p_hat * 1.012 after a few trend bars,
            #    blocking every valid mid-trend entry.
            #    Gate: overextended AND S > S_strong (blow-off territory).
            if self._price_overextended(c) and self.signal_strength > self.S_strong:
                return False

            # 2. Momentum already declining from its peak
            if self._momentum_past_peak():
                return False

            # 3. Spike check using long calm-period baseline.
            #    Only applies when we have trend context (trend_bar_count > body_baseline_bars / 2).
            #    On a fresh breakout from a base, the first 1–2 candles will naturally
            #    look like "spikes" vs the calm baseline — that's what breakouts look like.
            #    Applying this too early would block virtually every valid first-leg entry.
            spike_context_min = max(self.body_baseline_bars // 2, 5)
            if self.trend_bar_count >= spike_context_min and self._spike_on_long_baseline(c):
                return False

        # ── FIX-B: Consolidation box detection ───────────────────────
        # If the recent N-bar range is tiny (box) AND price is in the
        # middle 35–65% of that box, we are inside dead range action.
        if len(self.close_history) >= self.local_range_bars:
            recent_prices = self.close_history[-self.local_range_bars:]
            recent_high = max(recent_prices)
            recent_low  = min(recent_prices)
            range_size  = recent_high - recent_low
            if range_size > 0 and c > 0:
                range_pct = (range_size / c) * 100
                position_in_range = (c - recent_low) / range_size

                # Original gate: block lower 40%
                if position_in_range < 0.4:
                    return False

                # FIX-B: additional gate for tight consolidation box —
                # block mid-range (35–65%) when range itself is small.
                if range_pct < self.consolidation_range_pct:
                    if 0.35 <= position_in_range <= 0.65:
                        return False

        # ── Original spike filter (short window, kept as secondary) ──
        # FIX-A replaces this as primary, but we keep it as a backstop.
        if self.atr_val and self.atr_val > 0 and len(self.open_history) >= 2:
            lookback = min(self.spike_lookback_bars, len(self.open_history))
            recent_bodies = [
                abs(self.close_history[i] - self.open_history[i])
                for i in range(-lookback, 0)
            ]
            if len(recent_bodies) >= 2:
                avg_prior_body = sum(recent_bodies[:-1]) / len(recent_bodies[:-1])
                if avg_prior_body > 0 and recent_bodies[-1] > self.spike_atr_multiplier * avg_prior_body:
                    return False

        # §10: Reject if mid-range / in value area
        if self.current_profile:
            if self.current_profile.is_price_in_hvn(c):
                if self.signal_strength < self.S_strong * 1.2:
                    return False

        # §7: Structure-aware resistance check
        if direction == Direction.UP and self.current_profile:
            hvn_bins = self.current_profile.get_hvn_bins(top_n=3)
            for b in hvn_bins:
                mid = (b.price_low + b.price_high) / 2
                if mid > c:
                    dist_pct = (mid - c) / c
                    if dist_pct < 0.005 and b.delta < 0:
                        return False

        return True

    def _detect_direction(self) -> Direction:
        if self.ema_fast_val is not None and self.ema_slow_val is not None:
            if self.ema_fast_val > self.ema_slow_val:
                return Direction.UP
            elif self.ema_fast_val < self.ema_slow_val:
                return Direction.DOWN
        return Direction.NONE

    def _is_chop_zone(self, c: float) -> bool:
        if not self.atr_val or c <= 0:
            return False
        atr_pct = ((self.atr_val or 0.0) / c) * 100
        spread_pct = (abs(self.ema_spread) / c) * 100 if self.ema_spread else 0.0
        return atr_pct < self.chop_atr_pct and spread_pct < self.chop_spread_pct

    def _compute_s_effective(self, c: float) -> float:
        S = self.signal_strength
        if not self.current_profile or not self.current_profile.bins:
            return S
        if c <= 0:
            return S

        direction = self._detect_direction()
        hvn_bins = self.current_profile.get_hvn_bins(top_n=5)

        min_dist = float('inf')
        for b in hvn_bins:
            mid = (b.price_low + b.price_high) / 2
            if direction == Direction.UP and mid > c:
                dist = (mid - c) / c
                min_dist = min(min_dist, dist)
            elif direction == Direction.DOWN and mid < c:
                dist = (c - mid) / c
                min_dist = min(min_dist, dist)

        if min_dist == float('inf') or min_dist <= 0:
            return S

        delta_U = max(min_dist, 0.001)
        return S / delta_U

    def _is_leaving_hvn(self, c: float, direction: Direction = Direction.NONE) -> bool:
        if not self.current_profile:
            return True
        if direction == Direction.NONE:
            return True
        opp_dir = Direction.DOWN if direction == Direction.UP else Direction.UP
        return not self.current_profile.is_price_in_hvn(c, opp_dir)

    def _detect_regime(self, c: float) -> tuple[Regime, Optional[Signal]]:
        """State machine for regime detection.

        FIX-B applied in the ambiguous-zone guard:
          - Ambiguous zone now returns immediately with signal=None, no fall-through.
        FIX-A + FIX-C applied via _passes_entry_gate().
        """
        direction = self._detect_direction()
        signal = None

        S = self.signal_strength
        roc_decreasing = abs(self.m_hat) < abs(self.prev_m_hat)
        roc_zero_cross = (self.m_hat * self.prev_m_hat) < 0 if self.prev_m_hat != 0 else False
        momentum_decay = (abs(self.m_hat) - abs(self.prev_m_hat)) < 0
        atr_expanding = False
        if self.trend_start_atr > 0 and self.atr_val:
            atr_expanding = self.atr_val > self.trend_start_atr * 1.1

        in_chop = self._is_chop_zone(c) or self._in_local_chop

        s_decreasing = False
        if len(self._signal_strength_history) >= 2:
            s_decreasing = self._signal_strength_history[-1] < self._signal_strength_history[-2]

        price_stalling = False
        atr_now: float = self.atr_val if self.atr_val is not None else 0.0
        n_stall = self.exhaustion_stall_bars
        if atr_now > 0 and len(self.close_history) >= n_stall:
            stall_window = self.close_history[-n_stall:]
            close_range = max(stall_window) - min(stall_window)
            price_stalling = close_range < self.exhaustion_stall_atr_pct * atr_now

        in_hvn_current = False
        in_hvn_opposite = False
        in_lvn = False
        if self.current_profile:
            opp_dir = Direction.DOWN if direction == Direction.UP else Direction.UP
            in_hvn_current = self.current_profile.is_price_in_hvn(c, direction)
            in_hvn_opposite = self.current_profile.is_price_in_hvn(c, opp_dir)
            in_lvn = self.current_profile.is_price_in_lvn(c)

        strong_opposite_delta = False
        if self.current_profile:
            cum_delta = self.current_profile.cumulative_delta
            if direction == Direction.UP and cum_delta < -self.delta_threshold:
                strong_opposite_delta = True
            elif direction == Direction.DOWN and cum_delta > self.delta_threshold:
                strong_opposite_delta = True

        delta_aligned = False
        if self.current_profile:
            cum_delta = self.current_profile.cumulative_delta
            if direction == Direction.UP and cum_delta > 0:
                delta_aligned = True
            elif direction == Direction.DOWN and cum_delta < 0:
                delta_aligned = True

        # ── §1/§3: GLOBAL REGIME FILTER ──────────────────────────────────────
        if self.trend_confidence < self.confidence_low:
            if self.regime not in (Regime.EXHAUSTION,):
                if self.in_position:
                    pass
                else:
                    self.regime = Regime.EXHAUSTION
                    self.trend_before_exhaustion = self.direction
                    self.exhaustion_bar_count = 0
                    self.reversal_confirm_count = 0
                    self.exhaustion_persist_count = 0
                    self._exhaustion_phase_high = 0.0
                    return self.regime, None
            return self.regime, None

        if self.trend_confidence < self.confidence_high:
            # ── FIX-B: Ambiguous zone — hard return with no signal ────────────
            # Old code used `pass` for in_position then fell through to the
            # state machine below, allowing BUY/DB signals to fire in consolidation.
            # New: always return here.  Exit signals are still allowed via
            # _check_exit() which runs after this function.
            return self.regime, None

        # ─── A. TREND → EXHAUSTION ────────────────────────────────────────────
        if self.regime == Regime.TREND:
            self.trend_bar_count += 1

            if self.trend_bar_count < self.min_trend_bars:
                return self.regime, None

            spread_shrinking = not self.spread_expanding
            exhaust_conds = [
                spread_shrinking,
                roc_decreasing,
                momentum_decay,
                S < self.S_weak,
                self.momentum_acceleration < 0,
                s_decreasing,
                price_stalling,
                price_stalling,  # counts double
            ]
            met = sum(1 for x in exhaust_conds if x)
            exhaust_threshold = 6 if in_chop else 3

            if met >= exhaust_threshold:
                decay_confirmed = momentum_decay and s_decreasing
                if decay_confirmed:
                    self.exhaustion_persist_count += 1
                else:
                    self.exhaustion_persist_count = 0

                persist_needed = 1 if price_stalling else self.exhaustion_persist_bars
                if self.exhaustion_persist_count >= persist_needed:
                    self.regime = Regime.EXHAUSTION
                    self.trend_before_exhaustion = self.direction
                    self.exhaustion_bar_count = 0
                    self.reversal_confirm_count = 0
                    self.exhaustion_persist_count = 0
                    self._exhaustion_s_decay_count = 0
                    return self.regime, None
            else:
                self.exhaustion_persist_count = 0

            if direction != self.direction and direction != Direction.NONE:
                if self._ema_cross_valid and S > self.S_strong:
                    self.prev_direction = self.direction
                    self.regime = Regime.REVERSAL
                    self.direction = direction
                    self.reversal_bar_count = 0
                    self.trend_reversal_confirm_count = 0
                    self.exhaustion_persist_count = 0
                    return self.regime, None

                if roc_zero_cross and self._ema_cross_valid:
                    self.trend_reversal_confirm_count += 1
                    if self.trend_reversal_confirm_count >= self.reversal_confirm_bars:
                        self.prev_direction = self.direction
                        self.regime = Regime.REVERSAL
                        self.direction = direction
                        self.reversal_bar_count = 0
                        self.trend_reversal_confirm_count = 0
                        return self.regime, None
                else:
                    self.trend_reversal_confirm_count = 0

                self.exhaustion_persist_count += 1
                if self.exhaustion_persist_count >= self.exhaustion_persist_bars:
                    self.regime = Regime.EXHAUSTION
                    self.trend_before_exhaustion = self.direction
                    self.exhaustion_bar_count = 0
                    self.reversal_confirm_count = 0
                    self.exhaustion_persist_count = 0
                    return self.regime, None
            else:
                self.trend_reversal_confirm_count = 0

        # ─── B/C. EXHAUSTION → CONTINUATION / REVERSAL ───────────────────────
        elif self.regime == Regime.EXHAUSTION:
            self.exhaustion_bar_count += 1

            if len(self.close_history) >= 1:
                self._exhaustion_phase_high = max(self._exhaustion_phase_high, self.close_history[-1])

            cold_start = (self.trend_before_exhaustion == Direction.NONE)
            # FIX-C: Tiered S threshold for EXHAUSTION → CONTINUATION transition.
            #   cold_start (first breakout ever): use S_noise — ATR floor is
            #     calibrated to the baseline, so S barely rises above 1.0 on the
            #     first leg.  We should not miss the first breakout.
            #   normal post-trend exhaustion: use S_weak — we want meaningful
            #     signal before re-entering but not the full S_strong gate.
            #   S_strong is reserved for BUY entry decisions only.
            s_transition_threshold = self.S_noise if cold_start else self.S_weak
            if self.spread_expanding and S > s_transition_threshold and not momentum_decay:
                if direction == self.trend_before_exhaustion or cold_start:
                    self.regime = Regime.CONTINUATION
                    self.direction = direction
                    signal = None
                    if cold_start:
                        self.trend_before_exhaustion = Direction.DOWN if direction == Direction.UP else Direction.UP

                    # FIX-C: Emit BUY on cold-start UP breakout.
                    # Original code never emitted BUY here. For the very first breakout
                    # from idle, this is the only chance — symmetric with the
                    # opposite-direction and REVERSAL→CONTINUATION BUY paths.
                    if cold_start and direction == Direction.UP:
                        if not self.in_position and self.m_hat > 0:
                            if self._passes_entry_gate(c, direction):
                                entry_conds = [
                                    S > self.S_weak,                            # S_weak sufficient at cold start
                                    delta_aligned,
                                    self._is_leaving_hvn(c, direction),
                                    self.momentum_acceleration > 0,
                                    self._ema_cross_valid,
                                    self.s_effective > self.s_effective_threshold,
                                ]
                                if sum(1 for x in entry_conds if x) >= 2:
                                    signal = Signal.BUY

                    if direction == Direction.DOWN and self.in_position:
                        signal = Signal.EXIT
                    self.trend_start_price = c
                    self.trend_start_atr = self.atr_val or 0
                    self.trend_start_bar = self.bar_count
                    self._start_new_profile(c)
                    self.exhaustion_bar_count = 0
                    self._exhaustion_phase_high = 0.0
                    return self.regime, signal
                else:
                    self.regime = Regime.CONTINUATION
                    self.direction = direction
                    signal = None

                    if direction == Direction.UP and self.trend_before_exhaustion == Direction.DOWN:
                        # FIX-C: When S is already strong, skip the exhaustion_persist_bars wait.
                        # The persist gate filters noise but with S > S_strong the signal is decisive.
                        persist_ok = (
                            S > self.S_strong  # FIX-C: bypass persist wait when signal is strong
                            or self.exhaustion_bar_count >= self.exhaustion_persist_bars
                        )
                        if persist_ok:
                            if not self.in_position and S > self.S_weak and self.m_hat > 0:
                                price_retrace = (self._exhaustion_phase_high <= 0 or
                                                 c < self._exhaustion_phase_high * 0.998)
                                if not price_retrace:
                                    signal = None
                                else:
                                    if self._passes_entry_gate(c, direction):
                                        entry_conds = [
                                            S > self.S_strong,
                                            delta_aligned,
                                            self._is_leaving_hvn(c, direction),
                                            self.momentum_acceleration > 0,
                                            self._ema_cross_valid,
                                            self.s_effective > self.s_effective_threshold,
                                        ]
                                        if sum(1 for x in entry_conds if x) >= 2:
                                            signal = Signal.BUY
                    elif direction == Direction.DOWN and self.in_position:
                        signal = Signal.EXIT

                    self.trend_start_price = c
                    self.trend_start_atr = self.atr_val or 0
                    self.trend_start_bar = self.bar_count
                    self._start_new_profile(c)
                    self.exhaustion_bar_count = 0
                    self._exhaustion_phase_high = 0.0
                    return self.regime, signal

            direction_flipped = (direction != Direction.NONE and
                                 direction != self.trend_before_exhaustion
                                 and self._ema_cross_valid)

            rev_conds = [
                roc_zero_cross,
                atr_expanding,
                strong_opposite_delta,
                in_hvn_opposite or in_lvn,
                direction_flipped,
            ]
            rev_met = sum(1 for x in rev_conds if x)
            rev_threshold = 4 if in_chop else 3

            if rev_met >= rev_threshold:
                self.reversal_confirm_count += 1
                if self.reversal_confirm_count >= self.reversal_confirm_bars:
                    rev_dir = direction if direction != Direction.NONE else self.direction
                    self.prev_direction = rev_dir
                    self.direction = rev_dir
                    self.regime = Regime.REVERSAL
                    self.reversal_bar_count = 0
                    self.reversal_confirm_count = 0
                    self._exhaustion_phase_high = 0.0
                    return self.regime, None
            else:
                self.reversal_confirm_count = 0

            if self.exhaustion_bar_count >= self.exhaustion_bars_limit:
                if self.in_position:
                    signal = Signal.EXIT

        # ─── D. REVERSAL → CONTINUATION / back to EXHAUSTION ─────────────────
        elif self.regime == Regime.REVERSAL:
            self.reversal_bar_count += 1
            new_dir = self._detect_direction()

            ema_cross = (new_dir != Direction.NONE and new_dir != self.prev_direction
                         and self._ema_cross_valid)
            cont_conds = [
                ema_cross,
                S > self.S_strong,
                delta_aligned,
                in_hvn_current,
                self.spread_expanding,
            ]
            cont_met = sum(1 for x in cont_conds if x)

            if cont_met >= 3 and ema_cross:
                self.regime = Regime.CONTINUATION
                self.direction = new_dir
                signal = None

                if new_dir == Direction.UP and self.prev_direction == Direction.DOWN:
                    if not self.in_position and S > self.S_weak and self.m_hat > 0:
                        if self._passes_entry_gate(c, new_dir):
                            entry_conds = [
                                S > self.S_strong,
                                delta_aligned,
                                self._is_leaving_hvn(c, new_dir),
                                self.momentum_acceleration > 0,
                                self._ema_cross_valid,
                                self.s_effective > self.s_effective_threshold,
                            ]
                            if sum(1 for x in entry_conds if x) >= 2:
                                signal = Signal.BUY
                elif new_dir == Direction.DOWN and self.prev_direction == Direction.UP:
                    if self.in_position:
                        signal = Signal.EXIT

                self.trend_start_price = c
                self.trend_start_atr = self.atr_val or 0
                self.trend_start_bar = self.bar_count
                self._start_new_profile(c)
                self.exhaustion_bar_count = 0
                self.trend_bar_count = 0
                return self.regime, signal

            if S < self.S_noise and not roc_zero_cross and not momentum_decay:
                self.regime = Regime.EXHAUSTION
                self.trend_before_exhaustion = self.prev_direction
                self.exhaustion_bar_count = 0
                self.reversal_confirm_count = 0
                self._exhaustion_phase_high = 0.0
                return self.regime, None

        # ─── E. CONTINUATION → TREND ──────────────────────────────────────────
        elif self.regime == Regime.CONTINUATION:
            self.regime = Regime.TREND
            self.trend_bar_count = 0
            return self.regime, None

        if direction != Direction.NONE:
            if self.regime in (Regime.EXHAUSTION, Regime.REVERSAL):
                self.direction = direction

        return self.regime, signal

    def _check_exit(self, c: float) -> Optional[Signal]:
        if not self.in_position:
            return None

        if self.regime == Regime.EXHAUSTION and self.exhaustion_bar_count >= self.exhaustion_bars_limit:
            return Signal.EXIT

        if self.regime == Regime.REVERSAL:
            if (self.reversal_bar_count >= self.reversal_exit_confirm_bars
                    and self.signal_strength > self.S_noise):
                return Signal.EXIT

        return None

    def _update_profile(self, c: float, vol: float, is_buy: bool, time: int):
        if self.current_profile:
            self.current_profile.add_trade(c, vol, is_buy, time)

    def _start_new_profile(self, price: float):
        if self.current_profile:
            self.volume_profiles.append(self.current_profile)
        self.current_profile = VolumeProfile(price)

    def notify_trade_opened(self, entry_price: float, direction: Direction):
        self.in_position = True
        self.entry_price = entry_price
        self.position_direction = direction

    def notify_trade_closed(self):
        self.in_position = False
        self.entry_price = 0.0
        self.position_direction = Direction.NONE

    def update(
        self,
        time: int,
        o: float,
        h: float,
        l: float,
        c: float,
        volume: float = 0.0,
        buy_volume: float = 0.0,
        sell_volume: float = 0.0,
        _build_full_result: bool = True,
    ) -> dict:
        self.bar_count += 1
        self._update_indicators(o, h, l, c, volume)

        is_buy = c >= o
        self._update_profile(c, volume, is_buy, time)

        if self.current_profile is None:
            self._start_new_profile(c)

        regime, signal = self._detect_regime(c)

        if signal is None:
            exit_signal = self._check_exit(c)
            if exit_signal:
                signal = exit_signal

        # Belt-and-suspenders: never emit a signal during the warmup window
        if self.bar_count <= self.warmup:
            signal = None

        if _build_full_result:
            return self._build_result(time, c, signal)
        return self._build_result_minimal(time, c, signal)

    def _build_result_minimal(self, time: int, price: float, signal: Optional[Signal]) -> dict:
        return {
            "time": time,
            "regime": self.regime.value,
            "direction": self.direction.value,
            "signal": signal.value if signal else Signal.NONE.value,
        }

    def _build_result(self, time: int, price: float, signal: Optional[Signal]) -> dict:
        all_profiles = []
        for vp in self.volume_profiles:
            all_profiles.append(vp.to_dict())
        if self.current_profile:
            all_profiles.append(self.current_profile.to_dict())

        return {
            "time": time,
            "regime": self.regime.value,
            "direction": self.direction.value,
            "signal": signal.value if signal else Signal.NONE.value,
            "indicators": {
                "ema_fast": self.ema_fast_val,
                "ema_slow": self.ema_slow_val,
                "ema_macro": self.ema_macro_val,
                "atr": self.atr_val,
                "atr_floor": self.atr_floor,
                "roc": self.m_hat,
                "m_hat": self.m_hat,
                "p_hat": self.p_hat,
                "signal_strength": self.signal_strength,
                "momentum_acceleration": self.momentum_acceleration,
                "s_effective": self.s_effective,
                "ema_spread": self.ema_spread,
                "spread_expanding": self.spread_expanding,
                "trend_confidence": self.trend_confidence,
                "is_trending": self.is_trending,
                "ema_cross_valid": self._ema_cross_valid,
                "pre_entry_stable": self._pre_entry_stable,
                "in_local_chop": self._in_local_chop,
                # FIX-A debug indicators
                "price_overextended": self._price_overextended(price),
                "momentum_past_peak": self._momentum_past_peak(),
            },
            "volume_profiles": all_profiles,
            "in_position": self.in_position,
            "entry_price": self.entry_price,
            "exhaustion_bars": self.exhaustion_bar_count,
            "in_chop": self._is_chop_zone(price) or self._in_local_chop,
            "trend_bars": self.trend_bar_count,
        }