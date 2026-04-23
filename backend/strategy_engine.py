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
        """Rebuild bins when price range expands."""
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

        # Re-distribute old volume into new bins
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
        """Top N highest volume bins."""
        sorted_bins = sorted(self.bins, key=lambda b: b.total_volume, reverse=True)
        return sorted_bins[:top_n]

    def get_lvn_bins(self) -> list[VolumeBin]:
        """Bottom 30% bins by volume."""
        if not self.bins:
            return []
        sorted_bins = sorted(self.bins, key=lambda b: b.total_volume)
        cutoff = max(1, int(len(sorted_bins) * 0.3))
        return sorted_bins[:cutoff]

    def is_price_in_hvn(self, price: float, direction: Optional[Direction] = None, top_n: int = 5) -> bool:
        """Check if price is in a high volume node (optionally filtered by direction delta)."""
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
    Pure-scalar implementation (no numpy) for maximum throughput.

    State vector:  x = [p, m]^T
      p = price
      m = momentum (dp/dt)

    State transition (dt = 1):
      p_t = p_{t-1} + m_{t-1}
      m_t = m_{t-1} * (1 - gamma)

    Observation:
      z_t = p_t  (we observe price only)
    """

    def __init__(self, gamma: float = 0.1, q_price: float = 0.01,
                 q_momentum: float = 0.05, r_measure: float = 1.0):
        self.gamma = gamma
        self.decay = 1.0 - gamma  # F[1,1]

        # Process noise Q (diagonal)
        self.q_price = q_price
        self.q_momentum = q_momentum

        # Measurement noise R (scalar)
        self.r_measure = r_measure

        # State estimate: [p, m]
        self.p: float = 0.0
        self.m: float = 0.0

        # Covariance P (2×2 symmetric, stored as 3 scalars)
        self.P00: float = 1.0
        self.P01: float = 0.0
        self.P11: float = 1.0

        # Short-term variance tracker for auto-R calibration
        self._price_buf: list[float] = []
        self._var_window = 10

        self.initialised = False

    def _auto_r(self, price: float):
        """Update measurement noise R from recent price variance."""
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
        """
        Feed a new price observation. Returns (p_hat, m_hat).
        Pure scalar arithmetic — ~10× faster than numpy for 2×2.
        """
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

        # ── Predict ──────────────────────────────────────────────────
        # x_pred = F @ x
        p_pred = self.p + self.m        # F[0,0]*p + F[0,1]*m  (F[0,1]=1)
        m_pred = decay * self.m         # F[1,0]*p + F[1,1]*m  (F[1,0]=0)

        # P_pred = F @ P @ F^T + Q
        # F = [[1, 1], [0, d]]  where d = decay
        P00 = self.P00
        P01 = self.P01
        P11 = self.P11

        # FP = F @ P
        fp00 = P00 + P01         # 1*P00 + 1*P01  (row0 of F dot col0 of P)
        fp01 = P01 + P11         # 1*P01 + 1*P11
        fp10 = decay * P01       # 0*P00 + d*P01  (but P is symmetric: P10=P01)
        fp11 = decay * P11       # 0*P01 + d*P11

        # P_pred = FP @ F^T + Q
        pp00 = fp00 * 1.0 + fp01 * 0.0 + self.q_price    # row0·col0 of F^T
        # Actually: F^T = [[1,0],[1,d]]
        # pp[0,0] = fp00*1 + fp01*1   (wait, F^T col0 = [1, 1]^T → no)
        # Let me redo properly:
        # F^T = [[1, 0], [1, d]]
        # (FP) @ F^T:
        # pp[0,0] = fp00 * F^T[0,0] + fp01 * F^T[1,0]  = fp00*1 + fp01*1
        # pp[0,1] = fp00 * F^T[0,1] + fp01 * F^T[1,1]  = fp00*0 + fp01*d
        # pp[1,0] = fp10 * F^T[0,0] + fp11 * F^T[1,0]  = fp10*1 + fp11*1
        # pp[1,1] = fp10 * F^T[0,1] + fp11 * F^T[1,1]  = fp10*0 + fp11*d
        pp00 = fp00 + fp01 + self.q_price
        pp01 = fp01 * decay
        # pp10 = fp10 + fp11  (symmetric with pp01 by construction)
        pp11 = fp11 * decay + self.q_momentum

        # ── Update ───────────────────────────────────────────────────
        # H = [1, 0], so H @ x_pred = p_pred, H @ P_pred @ H^T = pp00
        y = price - p_pred                       # innovation
        S = pp00 + self.r_measure                 # innovation covariance (scalar)
        S_inv = 1.0 / S

        # K = P_pred @ H^T / S  →  K = [pp00, pp01(=pp10)]^T / S
        # But pp10 = fp10 + fp11, let me use the symmetric property:
        # Actually for H = [1,0]: P_pred @ H^T = [pp00, pp10]^T
        pp10 = fp10 + fp11  # pp[1,0]
        K0 = pp00 * S_inv
        K1 = pp10 * S_inv

        # x = x_pred + K * y
        self.p = p_pred + K0 * y
        self.m = m_pred + K1 * y

        # P = (I - K @ H) @ P_pred
        # I - K@H = [[1-K0, 0], [-K1, 1]]
        self.P00 = (1.0 - K0) * pp00
        self.P01 = (1.0 - K0) * pp01
        self.P11 = -K1 * pp01 + pp11

        return self.p, self.m

    def reset(self):
        """Reset filter state."""
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
        warmup: int = 5,
        signal_strong: float = 2.0,
        signal_weak: float = 1.5,
        signal_noise: float = 1.0,
        exhaustion_bars_limit: int = 3, #changed from 7
        delta_threshold: float = 0.3,
        kalman_gamma: float = 0.15,
        min_trend_bars: int = 3, # changed from 3
        reversal_confirm_bars: int = 2,
        chop_atr_pct: float = 0.5,
        chop_spread_pct: float = 0.15,
        reversal_exit_confirm_bars: int = 1,
        s_effective_threshold: float = 0.5,
        exhaustion_persist_bars: int = 4,
        # ── NEW: Regime Filter & Confidence params ────────────────────
        regime_lookback: int = 5,             # N bars for persistence / rolling calcs
        persistence_threshold: int = 2,       # min same-sign m_hat bars (§1A)
        momentum_mean_threshold: float = 0.0, # auto-calibrated; fallback floor
        ema_min_spread_pct: float = 0.08,     # min |EMA3-EMA7|/price * 100 (§1D)
        confidence_high: float = 0.6,        # above → allow trading changed from 0.6
        confidence_low: float = 0.42,         # below → force IDLE $ changed form 0.35
        confidence_w1: float = 0.30,          # persistence weight  (§2)
        confidence_w2: float = 0.25,          # normalised momentum weight
        confidence_w3: float = 0.25,          # volatility expansion weight
        confidence_w4: float = 0.20,          # EMA separation weight
        atr_floor_k: float = 0.6,             # ATR floor multiplier (§4)
        ema_cross_persist_bars: int = 2,      # min bars EMA spread increasing (§5)
        exhaustion_s_decay_bars: int = 2,      # bars S must decay for exhaustion (§6)
        exhaustion_stall_bars: int = 5,        # §6b: bars to check for price stall
        exhaustion_stall_atr_pct: float = 0.4, # §6b: close range < N×ATR → stalling
        local_range_bars: int = 10,            # lookback for local range (§8)
        local_range_threshold_pct: float = 0.4,# min range % of price (§8)
        sign_flip_threshold: int = 4,          # max sign flips before chop (§8)
        stability_bars: int = 2,              # required consecutive stability bars (§9) [was 2]
        spike_atr_multiplier: float = 2,    # §11: reject entry if last candle body > N×ATR
        spike_lookback_bars: int = 4,          # §11: how many recent bars to scan for spikes
    ):
        self.ema_fast_p = ema_fast
        self.ema_slow_p = ema_slow
        self.atr_period = atr_period
        self.roc_period = roc_period      # Kept for backward compat; unused
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

        # ── NEW parameters ────────────────────────────────────────────
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

        # State
        self.bar_count = 0
        self.regime = Regime.EXHAUSTION
        self.direction = Direction.NONE
        self.prev_direction = Direction.NONE
        self.trend_before_exhaustion = Direction.NONE  # Immutable trend dir at exhaustion entry

        # Indicator state
        self.ema_fast_val: Optional[float] = None
        self.ema_slow_val: Optional[float] = None
        self.atr_val: Optional[float] = None
        self.m_hat: float = 0.0           # Kalman-filtered momentum (replaces ROC)
        self.prev_m_hat: float = 0.0      # Previous m_hat (replaces prev_roc)
        self.p_hat: float = 0.0           # Kalman-filtered price estimate
        self.momentum_acceleration: float = 0.0  # m_hat(t) - m_hat(t-1)

        # Kalman filter
        self.kalman = KalmanFilterMomentum(gamma=kalman_gamma)
        self.signal_strength: float = 0.0
        self.s_effective: float = 0.0     # S / delta_U (barrier-adjusted signal)

        # Price history for ATR / ROC
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
        self.trend_start_bar: int = 0     # Bar index when current trend/continuation began
        self.exhaustion_bar_count: int = 0
        self.trend_bar_count: int = 0     # Bars spent in current TREND regime

        # Exhaustion persistence counter — how many consecutive bars
        # the exhaustion conditions have been met before committing.
        self.exhaustion_persist_count: int = 0

        # Reversal confirmation counter — how many consecutive bars
        # the reversal conditions have been met before committing.
        self.reversal_confirm_count: int = 0

        # In-trend reversal confirmation counter — for direction change
        # while still in TREND regime (requires AND + persistence).
        self.trend_reversal_confirm_count: int = 0

        # Reversal regime bar counter — how many bars we've been in REVERSAL.
        self.reversal_bar_count: int = 0

        # Volume profiles (list of completed profiles + current)
        self.volume_profiles: list[VolumeProfile] = []
        self.current_profile: Optional[VolumeProfile] = None

        # Trade tracking
        self.in_position = False
        self.entry_price: float = 0.0
        self.position_direction: Direction = Direction.NONE

        # ── NEW: Rolling buffers for regime filter & confidence ────────
        self._m_hat_history: list[float] = []        # rolling m_hat values
        self._abs_m_hat_history: list[float] = []    # rolling |m_hat|
        self._atr_history: list[float] = []          # rolling ATR values
        self._signal_strength_history: list[float] = []  # rolling S values
        self._ema_spread_history: list[float] = []   # rolling EMA spread magnitudes

        # §1: Global regime filter result
        self.is_trending: bool = False

        # §2: Trend confidence score [0, 1]
        self.trend_confidence: float = 0.0

        # §4: ATR floor for S normalisation
        self.atr_floor: float = 0.0

        # §5: EMA cross persistence counter
        self._ema_cross_persist_count: int = 0
        self._ema_cross_valid: bool = False   # True when cross passes all gates
        # §6: Exhaustion S-decay counter
        self._exhaustion_s_decay_count: int = 0

        # §8: Local range chop detection
        self._in_local_chop: bool = False

        # §9: Pre-entry stability results
        self._pre_entry_stable_up: bool = False
        self._pre_entry_stable_down: bool = False
        self._pre_entry_stable: bool = False

        # Fix 3a: track highest close seen during the current EXHAUSTION phase
        self._exhaustion_phase_high: float = 0.0

    def _update_indicators(self, o: float, h: float, l: float, c: float, vol: float):
        """Update EMA, ATR, ROC from new OHLC bar."""
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

        # EMA spread
        self.prev_ema_spread = self.ema_spread
        self.ema_spread = self.ema_fast_val - self.ema_slow_val
        self.spread_expanding = abs(self.ema_spread) > abs(self.prev_ema_spread)

        # ATR (Wilder's smoothed)
        if len(self.close_history) >= 2:
            prev_c = self.close_history[-2]
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        else:
            tr = h - l

        if self.atr_val is None:
            self.atr_val = tr
        else:
            self.atr_val = ema_step(self.atr_val, tr, self.atr_period)

        # Kalman-filtered momentum (replaces ROC)
        self.prev_m_hat = self.m_hat
        self.p_hat, self.m_hat = self.kalman.update(c)

        # Momentum acceleration: rate of change of momentum
        self.momentum_acceleration = self.m_hat - self.prev_m_hat

        # ── §4: ATR Floor — prevent S inflation in low-volatility ─────
        # rolling_median_ATR from recent history
        self._atr_history.append(self.atr_val or 0.0)
        if len(self._atr_history) > self.regime_lookback * 4:
            self._atr_history = self._atr_history[-(self.regime_lookback * 4):]
        if len(self._atr_history) >= 3:
            sorted_atr = sorted(self._atr_history)
            rolling_median_atr = sorted_atr[len(sorted_atr) // 2]
        else:
            rolling_median_atr = self.atr_val or 0.0
        self.atr_floor = max(self.atr_val or 0.0, self.atr_floor_k * rolling_median_atr)

        # Signal strength S = |m_hat_pct * roc_period| / ATR_floor_pct
        # (§4: uses ATR_floor instead of raw ATR to prevent inflation)
        if self.atr_floor > 0 and c > 0:
            m_hat_pct = (self.m_hat / c) * 100 * self.roc_period
            atr_floor_pct = (self.atr_floor / c) * 100
            if atr_floor_pct > 0:
                self.signal_strength = abs(m_hat_pct) / atr_floor_pct
            else:
                self.signal_strength = 0.0
        else:
            self.signal_strength = 0.0

        # ── Update rolling buffers for regime filter ──────────────────
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

        # Barrier proximity: S_effective = S / delta_U
        # delta_U = normalised distance from price to nearest HVN in trade direction
        self.s_effective = self._compute_s_effective(c)

        # ── §1–§3: Regime filter, confidence, and ambiguous zone ──────
        self._update_regime_filter(c)

        # ── §5: EMA cross validation ──────────────────────────────────
        self._update_ema_cross_validation(c)

        # ── §8: Local range chop detection ────────────────────────────
        self._update_local_chop(c)

        # ── §9: Pre-entry stability check ─────────────────────────────
        self._update_pre_entry_stability()

    # ── §1–§3: Global Regime Filter + Trend Confidence + Ambiguous Zone ───

    def _update_regime_filter(self, c: float):
        """Compute trend confidence and apply global regime filter.

        Sets self.is_trending and self.trend_confidence.
        Confidence is calibrated so noisy random-walk coins score ~0.30–0.45
        and clean trending coins score ~0.65–0.85+, with a gap in between.
        """
        N = self.regime_lookback

        if len(self._m_hat_history) < N:
            self.is_trending = False
            self.trend_confidence = 0.0
            return

        recent_m = self._m_hat_history[-N:]
        recent_abs_m = self._abs_m_hat_history[-N:]

        # ── A) Directional Persistence (consecutive run) ───────────────
        # Count consecutive same-sign m_hat from most recent bar backward.
        # Consecutive runs are hard for random walks to sustain.
        current_sign = 1 if recent_m[-1] >= 0 else -1
        persistence_count = 0
        for v in reversed(recent_m):
            if (v >= 0 and current_sign == 1) or (v < 0 and current_sign == -1):
                persistence_count += 1
            else:
                break
        persistence_ok = persistence_count >= self.persistence_threshold

        # ── B) Momentum Strength (FIXED) ─────────────────────────────
        # Check: current |m_hat| must be ABOVE the long-run rolling mean.
        # This asks "is momentum currently elevated vs its own history?"
        # A decaying or noisy momentum will oscillate around the mean → fails.
        # The previous version checked rolling_mean > rolling_mean*0.3 which
        # is mathematically always True and filtered nothing.
        rolling_mean_abs_m = sum(recent_abs_m) / len(recent_abs_m)
        wide_abs_m = self._abs_m_hat_history  # full buffer (up to 4×N bars)
        long_mean_abs_m = sum(wide_abs_m) / len(wide_abs_m) if wide_abs_m else 0.0
        # Current momentum must be elevated above the long-run baseline
        momentum_ok = (
            abs(self.m_hat) > long_mean_abs_m * 1.1
            and rolling_mean_abs_m > 0
        )

        # ── C) Volatility Expansion (wider window) ────────────────────
        # Use the full ATR buffer so the median is a genuine long-run baseline,
        # not oscillating around median 50% by definition with a short window.
        atr_hist = self._atr_history
        if len(atr_hist) >= 5:
            sorted_atr = sorted(atr_hist)
            rolling_median_atr = sorted_atr[len(sorted_atr) // 2]
        else:
            rolling_median_atr = self.atr_val or 0.0
        volatility_ok = (self.atr_val or 0.0) > rolling_median_atr * 1.1

        # ── D) EMA Separation ─────────────────────────────────────────
        if c > 0 and self.ema_fast_val is not None and self.ema_slow_val is not None:
            ema_sep_pct = abs(self.ema_fast_val - self.ema_slow_val) / c * 100
        else:
            ema_sep_pct = 0.0
        ema_sep_ok = ema_sep_pct > self.ema_min_spread_pct

        # ── Boolean filter (§1) ───────────────────────────────────────
        self.is_trending = all([persistence_ok, momentum_ok, volatility_ok, ema_sep_ok])

        # ── §2: Continuous Trend Confidence ───────────────────────────
        # Component 1: consecutive persistence ratio [0,1]
        # Random walks sustain ~1-2 bar runs; real trends sustain N bars.
        persistence_frac = min(persistence_count / N, 1.0)

        # Component 2: elevated momentum vs long-run baseline [0, 1]
        # Noisy coins: current bar spikes randomly above/below long mean → ~0.50
        # Trending coins: current bar consistently above long mean → ~0.70–1.0
        if long_mean_abs_m > 0:
            norm_momentum = min(abs(self.m_hat) / long_mean_abs_m, 2.0) / 2.0
        else:
            norm_momentum = 0.0

        # Component 3: ATR expansion vs long-run median [0, 1]
        if rolling_median_atr > 0:
            vol_expansion = min((self.atr_val or 0.0) / rolling_median_atr, 2.0) / 2.0
        else:
            vol_expansion = 0.0

        # Component 4: EMA separation relative to minimum threshold [0, 1]
        ema_sep_norm = min(ema_sep_pct / max(self.ema_min_spread_pct * 3, 0.01), 1.0)

        self.trend_confidence = (
            self.confidence_w1 * persistence_frac
            + self.confidence_w2 * norm_momentum
            + self.confidence_w3 * vol_expansion
            + self.confidence_w4 * ema_sep_norm
        )

        # Fix 4: Momentum age penalty — long unbroken runs signal exhaustion, not strength
        if len(self._m_hat_history) >= 2:
            run_sign = 1 if self._m_hat_history[-1] >= 0 else -1
            run_length = 0
            for v in reversed(self._m_hat_history):
                if (v >= 0 and run_sign == 1) or (v < 0 and run_sign == -1):
                    run_length += 1
                else:
                    break
            age_ratio = run_length / max(self.regime_lookback, 1)
            if age_ratio > 2.0:  # run is more than 2× the lookback window
                age_penalty = min((age_ratio - 2.0) * 0.12, 0.25)
                self.trend_confidence -= age_penalty

        self.trend_confidence = max(0.0, min(1.0, self.trend_confidence))

    # ── §5: EMA Cross Sensitivity Reduction ───────────────────────────

    def _update_ema_cross_validation(self, c: float):
        """EMA cross is only valid if spread is increasing, magnitude is
        above threshold, and it persists for >= ema_cross_persist_bars.
        """
        if c <= 0:
            self._ema_cross_valid = False
            return

        spread_mag_pct = abs(self.ema_spread) / c * 100 if c > 0 else 0.0
        spread_derivative = abs(self.ema_spread) - abs(self.prev_ema_spread)

        # All three conditions must hold
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

    # ── §8: Local Range Anti-Chop Filter ──────────────────────────────

    def _update_local_chop(self, c: float):
        """Detect choppy/range-bound conditions from price range and
        momentum sign flips over the local window.
        """
        N = self.local_range_bars
        if len(self.close_history) < N or c <= 0:
            self._in_local_chop = False
            return

        prices = self.close_history[-N:]
        local_range = max(prices) - min(prices)
        range_pct = (local_range / c) * 100
        range_small = range_pct < self.local_range_threshold_pct

        # Count momentum sign flips
        m_recent = self._m_hat_history[-N:] if len(self._m_hat_history) >= N else self._m_hat_history
        sign_flips = 0
        for i in range(1, len(m_recent)):
            if m_recent[i] * m_recent[i - 1] < 0:
                sign_flips += 1

        self._in_local_chop = range_small and sign_flips >= self.sign_flip_threshold

    # ── §9: Pre-Entry Stability Check ─────────────────────────────────

    def _update_pre_entry_stability(self):
        """Require raw momentum OR signal strength to be accelerating
        in the direction of the trend for >= stability_bars.
        """
        N = self.stability_bars
        if len(self._m_hat_history) < N + 1 or len(self._signal_strength_history) < N + 1:
            self._pre_entry_stable_up = False
            self._pre_entry_stable_down = False
            self._pre_entry_stable = False
            return

        # raw m_hat values
        m_recent = self._m_hat_history[-(N + 1):]
        s_recent = self._signal_strength_history[-(N + 1):]

        # Upward trajectory: net positive over the window AND current bar still rising.
        # Strict all-N monotonic increase is too brittle with a Kalman-smoothed m_hat
        # which oscillates slightly even in strong trends.
        m_increasing_up   = m_recent[-1] > m_recent[0] and m_recent[-1] > m_recent[-2]
        # Downward trajectory: net negative over the window AND current bar still falling.
        m_increasing_down = m_recent[-1] < m_recent[0] and m_recent[-1] < m_recent[-2]
        
        s_increasing = all(s_recent[i + 1] > s_recent[i] for i in range(N))

        accel_up = self.momentum_acceleration > 0
        accel_down = self.momentum_acceleration < 0

        # Flaw-4 fix: require m_hat sign agreement with direction.
        # s_increasing is direction-agnostic (|m_hat|/ATR growing), so on a
        # downtrend with growing |m_hat|, s_increasing=True would wrongly
        # satisfy _pre_entry_stable_up.  The sign check prevents that.
        m_hat_positive = self.m_hat > 0
        m_hat_negative = self.m_hat < 0
        self._pre_entry_stable_up = m_hat_positive and (m_increasing_up or s_increasing) and accel_up
        self._pre_entry_stable_down = m_hat_negative and (m_increasing_down or s_increasing) and accel_down
        
        # For general dashboard display, true if stable in any direction
        self._pre_entry_stable = self._pre_entry_stable_up or self._pre_entry_stable_down

    # ── §7 + §10: Unified Entry Gate ──────────────────────────────────

    def _passes_entry_gate(self, c: float, direction: Direction) -> bool:
        """Combined entry gate: regime filter, confidence, stability,
        local chop, and structure-aware location.

        Returns True only if all regime / stability conditions allow entry.
        This does NOT replace the existing entry scoring — it's a hard gate
        that must pass BEFORE the scoring is evaluated.
        """
        # §3: Ambiguous zone — confidence must be above HIGH_THRESHOLD
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

        # §11: Spike filter — reject entry if any candle in the recent lookback
        # window has a body > spike_atr_multiplier × current ATR.
        # Uses ATR (not avg-body) so it scales with regime volatility and doesn't
        # reject the legitimate large breakout candle leaving flat chop.
        if self.atr_val and self.atr_val > 0 and len(self.open_history) >= 2:
            spike_threshold = self.spike_atr_multiplier * self.atr_val
            lookback = min(self.spike_lookback_bars, len(self.open_history))
            for i in range(-lookback, 0):
                if abs(self.close_history[i] - self.open_history[i]) > spike_threshold:
                    return False

        # §10: Entry location filter — reject if mid-range / in value area
        # "value area" = inside an HVN cluster (already tracked)
        if self.current_profile:
            # Reject if price is inside a high-volume cluster (value area)
            if self.current_profile.is_price_in_hvn(c):
                # Exception: allow if price is actively breaking OUT of the cluster
                # (momentum strong enough to overcome)
                if self.signal_strength < self.S_strong * 1.2:
                    return False

        # §7: Structure-aware rejection — directly below strong resistance
        if direction == Direction.UP and self.current_profile:
            hvn_bins = self.current_profile.get_hvn_bins(top_n=3)
            for b in hvn_bins:
                mid = (b.price_low + b.price_high) / 2
                if mid > c:
                    dist_pct = (mid - c) / c
                    # Very close to overhead resistance with sell pressure
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
        """Detect consolidation / chop — ATR tight + EMA spread tight.

        When price is chopping sideways, both ATR as a percentage of price
        is small AND the EMA spread relative to price is tiny.  In this
        zone we raise all thresholds to avoid false regime transitions.
        """
        if not self.atr_val or c <= 0:
            return False
        atr_pct = ((self.atr_val or 0.0) / c) * 100
        spread_pct = (abs(self.ema_spread) / c) * 100 if self.ema_spread else 0.0
        return atr_pct < self.chop_atr_pct and spread_pct < self.chop_spread_pct

    def _compute_s_effective(self, c: float) -> float:
        """Compute barrier-adjusted signal strength: S / delta_U.

        delta_U = normalised distance from current price to nearest HVN
        in the direction of the trade.  If no profile or no HVN found,
        returns raw S (assume no barrier).
        """
        S = self.signal_strength
        if not self.current_profile or not self.current_profile.bins:
            return S
        if c <= 0:
            return S

        direction = self._detect_direction()
        hvn_bins = self.current_profile.get_hvn_bins(top_n=5)

        # Find nearest HVN in the trade direction
        min_dist = float('inf')
        for b in hvn_bins:
            mid = (b.price_low + b.price_high) / 2
            if direction == Direction.UP and mid > c:
                dist = (mid - c) / c  # normalised distance
                min_dist = min(min_dist, dist)
            elif direction == Direction.DOWN and mid < c:
                dist = (c - mid) / c
                min_dist = min(min_dist, dist)

        if min_dist == float('inf') or min_dist <= 0:
            # No barrier ahead → S_effective = S (unimpeded)
            return S

        # delta_U is the normalised distance; S_effective = S / delta_U
        # We cap delta_U at a minimum to avoid division by tiny numbers
        delta_U = max(min_dist, 0.001)
        return S / delta_U

    def _is_leaving_hvn(self, c: float, direction: Direction = Direction.NONE) -> bool:
        """Check if price is NOT trapped inside an opposing-direction HVN.

        For a long entry (Direction.UP), we check that we're NOT sitting
        inside a sell-dominated HVN (resistance).  Being in a buy-dominated
        HVN is fine — that's support, not resistance.

        If direction is NONE, defaults to True (no block).
        """
        if not self.current_profile:
            return True  # No profile → no barrier
        if direction == Direction.NONE:
            return True
        # Check opposing direction — are we in a resistance zone?
        opp_dir = Direction.DOWN if direction == Direction.UP else Direction.UP
        return not self.current_profile.is_price_in_hvn(c, opp_dir)

    def _detect_regime(self, c: float) -> tuple[Regime, Optional[Signal]]:
        """State machine for regime detection.

        Integrations:
          §1/§3: Global regime filter — force IDLE when confidence < LOW_THRESHOLD,
                 freeze state in ambiguous zone.
          §5:    EMA cross only valid when _ema_cross_valid is True.
          §6:    Strengthened exhaustion — require BOTH momentum_decay AND S decay.
          §7/§9/§10: All BUY signals gated by _passes_entry_gate().
        """
        direction = self._detect_direction()
        signal = None

        S = self.signal_strength
        roc_decreasing = abs(self.m_hat) < abs(self.prev_m_hat)
        roc_zero_cross = (self.m_hat * self.prev_m_hat) < 0 if self.prev_m_hat != 0 else False
        momentum_decay = (abs(self.m_hat) - abs(self.prev_m_hat)) < 0  # D < 0
        atr_expanding = False
        if self.trend_start_atr > 0 and self.atr_val:
            atr_expanding = self.atr_val > self.trend_start_atr * 1.1

        # Consolidation / chop detection (original + new §8)
        in_chop = self._is_chop_zone(c) or self._in_local_chop

        # §6: Signal strength decay tracking for exhaustion
        s_decreasing = False
        if len(self._signal_strength_history) >= 2:
            s_decreasing = self._signal_strength_history[-1] < self._signal_strength_history[-2]

        # §6b: Price stall detector — if close range over last N bars is tiny
        # relative to ATR, price is going nowhere → strong exhaustion signal.
        price_stalling = False
        atr_now: float = self.atr_val if self.atr_val is not None else 0.0
        n_stall = self.exhaustion_stall_bars
        if atr_now > 0 and len(self.close_history) >= n_stall:
            stall_window = self.close_history[-n_stall:]
            close_range = max(stall_window) - min(stall_window)
            price_stalling = close_range < self.exhaustion_stall_atr_pct * atr_now

        # Profile checks
        in_hvn_current = False
        in_hvn_opposite = False
        in_lvn = False
        if self.current_profile:
            opp_dir = Direction.DOWN if direction == Direction.UP else Direction.UP
            in_hvn_current = self.current_profile.is_price_in_hvn(c, direction)
            in_hvn_opposite = self.current_profile.is_price_in_hvn(c, opp_dir)
            in_lvn = self.current_profile.is_price_in_lvn(c)

        # Strong opposite delta check
        strong_opposite_delta = False
        if self.current_profile:
            cum_delta = self.current_profile.cumulative_delta
            if direction == Direction.UP and cum_delta < -self.delta_threshold:
                strong_opposite_delta = True
            elif direction == Direction.DOWN and cum_delta > self.delta_threshold:
                strong_opposite_delta = True

        # Delta aligned with new direction
        delta_aligned = False
        if self.current_profile:
            cum_delta = self.current_profile.cumulative_delta
            if direction == Direction.UP and cum_delta > 0:
                delta_aligned = True
            elif direction == Direction.DOWN and cum_delta < 0:
                delta_aligned = True

        # ── §1/§3: GLOBAL REGIME FILTER — runs BEFORE state machine ──────
        # If confidence is below LOW_THRESHOLD → collapse to EXHAUSTION so the
        # engine remains primed to catch the breakout, rather than going fully
        # dark in IDLE.  Exhaustion's bar-count timeout will still exit positions.
        # If in ambiguous zone (between LOW and HIGH) → freeze state, no transitions.
        if self.trend_confidence < self.confidence_low:
            if self.regime not in (Regime.EXHAUSTION,):
                if self.in_position:
                    # Keep current regime so _check_exit can fire
                    pass
                else:
                    self.regime = Regime.EXHAUSTION
                    self.trend_before_exhaustion = self.direction  # remember last direction
                    self.exhaustion_bar_count = 0
                    self.reversal_confirm_count = 0
                    self.exhaustion_persist_count = 0
                    self._exhaustion_phase_high = 0.0  # Fix 3c: reset on collapsing to EXHAUSTION
                    return self.regime, None
            return self.regime, None

        if self.trend_confidence < self.confidence_high:
            # Ambiguous zone: no entries, no state transitions
            # But still process exits if in position
            if self.in_position:
                pass
            return self.regime, None

        # ─── A. TREND → EXHAUSTION ────────────────────────────────────────
        if self.regime == Regime.TREND:
            self.trend_bar_count += 1

            # Guard: don't allow exhaustion transition until trend has
            # persisted for a minimum number of bars. This prevents
            # rapid TREND→EXHAUSTION→REVERSAL cycling from EMA noise.
            if self.trend_bar_count < self.min_trend_bars:
                return self.regime, None

            # §6: Strengthened exhaustion — require BOTH momentum decay AND S decay
            # §6b: price_stalling counts as 3 conditions by itself (hard stall evidence)
            spread_shrinking = not self.spread_expanding
            exhaust_conds = [
                spread_shrinking,
                roc_decreasing,
                momentum_decay,
                S < self.S_weak,
                self.momentum_acceleration < 0,  # require decelerating momentum
                s_decreasing,                    # §6: S must also be decreasing
                price_stalling,                  # §6b: price going nowhere
                price_stalling,                  # counts double — stall is decisive
            ]
            met = sum(1 for x in exhaust_conds if x)

            # In chop zones, require all 6 standard conditions (stricter).
            # Normal trend: 3/8 suffices (stall alone = 2pts, stall+decay = 3pts).
            exhaust_threshold = 6 if in_chop else 3

            if met >= exhaust_threshold:
                # §6: Exhaustion persistence — require either price stall OR both decay signals
                decay_confirmed = decay_confirmed = momentum_decay and s_decreasing
                if decay_confirmed:
                    self.exhaustion_persist_count += 1
                else:
                    self.exhaustion_persist_count = 0

                # Price stall is so decisive it only needs 1 persistence bar
                persist_needed = 1 if price_stalling else self.exhaustion_persist_bars
                if self.exhaustion_persist_count >= persist_needed:
                    self.regime = Regime.EXHAUSTION
                    self.trend_before_exhaustion = self.direction  # Lock original trend
                    self.exhaustion_bar_count = 0
                    self.reversal_confirm_count = 0
                    self.exhaustion_persist_count = 0
                    self._exhaustion_s_decay_count = 0
                    return self.regime, None
            else:
                # Conditions not met this bar — reset persistence streak
                self.exhaustion_persist_count = 0

            # Direction change while in trend → could be rapid reversal
            # §5: require EMA cross to be validated + momentum zero-cross + persistence
            if direction != self.direction and direction != Direction.NONE:
                # Fast-path: if EMA cross valid AND S already strong in new direction,
                # this is a real reversal — don't waste bars in EXHAUSTION.
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

                # EMA cross happened but momentum not confirmed yet
                # → go to exhaustion (with persistence requirement)
                self.exhaustion_persist_count += 1
                if self.exhaustion_persist_count >= self.exhaustion_persist_bars:
                    self.regime = Regime.EXHAUSTION
                    self.trend_before_exhaustion = self.direction  # Lock original trend
                    self.exhaustion_bar_count = 0
                    self.reversal_confirm_count = 0
                    self.exhaustion_persist_count = 0
                    return self.regime, None
            else:
                self.trend_reversal_confirm_count = 0

        # ─── B/C. EXHAUSTION → CONTINUATION / REVERSAL ─────────────────
        # This now handles BOTH cold-start (trend_before_exhaustion == NONE)
        # and normal post-trend exhaustion.  When trend_before_exhaustion is
        # NONE any confirmed breakout direction is treated as a fresh start.
        # ─────────────────────────────────────────────────────────────────
        elif self.regime == Regime.EXHAUSTION:
            self.exhaustion_bar_count += 1

            # Fix 3b: track the highest close seen during this exhaustion phase
            if len(self.close_history) >= 1:
                self._exhaustion_phase_high = max(self._exhaustion_phase_high, self.close_history[-1])

            # Continuation after exhaustion: original trend resumes.
            # Cold-start case: if trend_before_exhaustion is NONE (fresh engine
            # or just collapsed from low-confidence), treat any breakout as a
            # continuation so we don't miss the first leg.
            # State transition gate: only checks core regime conditions.
            # Entry-specific gates (momentum_acceleration, s_effective,
            # delta_aligned, leaving_hvn) are applied to the BUY decision only.
            cold_start = (self.trend_before_exhaustion == Direction.NONE)
            # Use S_weak (not S_strong) as the state-transition exit threshold.
            # S_strong is still scored as +1 condition inside entry_conds, so
            # strong setups still get rewarded — but moderate trends can also
            # leave EXHAUSTION without being blocked at the door.
            if self.spread_expanding and S > self.S_weak and not momentum_decay:
                if direction == self.trend_before_exhaustion or cold_start:
                    # Same direction as original trend (or cold-start) → CONTINUATION
                    self.regime = Regime.CONTINUATION
                    self.direction = direction
                    signal = None
                    if cold_start:
                        # Fresh breakout from cold start: evaluate BUY immediately.
                        # Set trend_before_exhaustion to opposite so the subsequent
                        # direction-flip BUY logic recognises this as a reversal-up scenario.
                        self.trend_before_exhaustion = Direction.DOWN if direction == Direction.UP else Direction.UP
                        if direction == Direction.UP and not self.in_position and S > self.S_weak and self.m_hat > 0:
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
                    # EXIT on downward move if in position
                    if direction == Direction.DOWN and self.in_position:
                        signal = Signal.EXIT
                    self.trend_start_price = c
                    self.trend_start_atr = self.atr_val or 0
                    self.trend_start_bar = self.bar_count
                    self._start_new_profile(c)
                    self.exhaustion_bar_count = 0
                    self._exhaustion_phase_high = 0.0  # Fix 3c: reset on leaving EXHAUSTION
                    return self.regime, signal
                else:
                    # Direction flipped during exhaustion (e.g. DOWN -> UP)!
                    # This is a strong momentum shift that bypasses the REVERSAL regime.
                    # We treat it as CONTINUATION of the new momentum.
                    self.regime = Regime.CONTINUATION
                    self.direction = direction
                    signal = None

                    if direction == Direction.UP and self.trend_before_exhaustion == Direction.DOWN:
                        # it shouldnt be just 1 bar, it should be the exhaustion threshold
                        if self.exhaustion_bar_count >= self.exhaustion_persist_bars:
                            if not self.in_position and S > self.S_weak and self.m_hat > 0:
                                # Fix 3d: retracement guard — require price to have pulled
                                # back below the exhaustion-phase high before allowing BUY.
                                price_retrace = (self._exhaustion_phase_high <= 0 or
                                                 c < self._exhaustion_phase_high * 0.998)
                                if not price_retrace:
                                    signal = None  # still enter CONTINUATION but no BUY
                                else:
                                    # §7/§9/§10: Entry gate must pass
                                    if self._passes_entry_gate(c, direction):
                                        entry_conds = [
                                            S > self.S_strong,                          # signal strength
                                            delta_aligned,                              # volume delta confirms
                                            self._is_leaving_hvn(c, direction),         # not trapped in resistance
                                            self.momentum_acceleration > 0,             # momentum building
                                            self._ema_cross_valid,
                                            self.s_effective > self.s_effective_threshold  # confirmed EMA direction
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
                    self._exhaustion_phase_high = 0.0  # Fix 3c: reset on leaving EXHAUSTION
                    return self.regime, signal

            # Check for direction flip (EMA cross) relative to original trend
            # §5: only consider validated EMA crosses
            direction_flipped = (direction != Direction.NONE and
                                 direction != self.trend_before_exhaustion
                                 and self._ema_cross_valid)

            # Check reversal conditions
            rev_conds = [
                roc_zero_cross,
                atr_expanding,
                strong_opposite_delta,
                in_hvn_opposite or in_lvn,
                direction_flipped,
            ]
            rev_met = sum(1 for x in rev_conds if x)

            # In chop zones, require 4/5 conditions (much stricter)
            rev_threshold = 4 if in_chop else 3

            if rev_met >= rev_threshold:
                # Reversal must be confirmed for N consecutive bars
                # to avoid single-bar noise flips during consolidation.
                self.reversal_confirm_count += 1
                if self.reversal_confirm_count >= self.reversal_confirm_bars:
                    # prev_direction = the direction WE ARE REVERSING INTO (current reversal dir).
                    # This ensures REVERSAL→CONTINUATION fires ema_cross when the price
                    # crosses BACK to the opposite (original trend) direction.
                    # e.g. TREND UP → EXHAUSTION → REVERSAL DOWN:
                    #   prev_direction = DOWN (reversal direction)
                    #   Later when new_dir=UP → ema_cross = True → BUY fires
                    rev_dir = direction if direction != Direction.NONE else self.direction
                    self.prev_direction = rev_dir  # direction of this reversal move
                    self.direction = rev_dir
                    self.regime = Regime.REVERSAL
                    self.reversal_bar_count = 0
                    self.reversal_confirm_count = 0
                    self._exhaustion_phase_high = 0.0  # Fix 3c: reset on leaving EXHAUSTION
                    return self.regime, None
            else:
                # Conditions not met this bar — reset the confirmation streak
                self.reversal_confirm_count = 0

            # Prolonged exhaustion → signal exit if in position
            if self.exhaustion_bar_count >= self.exhaustion_bars_limit:
                if self.in_position:
                    signal = Signal.EXIT

        # ─── D. REVERSAL → CONTINUATION / back to EXHAUSTION ─────────────
        elif self.regime == Regime.REVERSAL:
            self.reversal_bar_count += 1
            new_dir = self._detect_direction()

            # Check continuation conditions — core state transition only.
            # Entry-specific gates are applied to BUY decision below.
            # §5: EMA cross must be validated
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
                # Dynamic entry: BUY when core conditions met + enough
                # confirming signals.  S > S_weak and m_hat > 0 (Flaw-2 fix:
                # sign check instead of acceleration) are hard requirements;
                # the rest are scored (2/5 needed).
                # Flaw-2 fix: replaced momentum_acceleration > 0 with m_hat > 0.
                # Coming out of EXHAUSTION→REVERSAL, the Kalman filter is still
                # smoothing out the momentum decay, so acceleration is often negative
                # for 1-2 bars even though m_hat has already flipped positive.
                # Checking the sign of m_hat catches the new move immediately.
                if new_dir == Direction.UP and self.prev_direction == Direction.DOWN:
                    if not self.in_position and S > self.S_weak and self.m_hat > 0:
                        # §7/§9/§10: Entry gate must pass
                        if self._passes_entry_gate(c, new_dir):
                            # Flaw-1 fix: replaced s_effective (degenerates to S on fresh
                            # profile) and spread_expanding (already a hard gate above)
                            # with independent signals.
                            entry_conds = [
                                S > self.S_strong,                          # signal strength
                                delta_aligned,                              # volume delta confirms
                                self._is_leaving_hvn(c, new_dir),           # not trapped in resistance
                                self.momentum_acceleration > 0,             # momentum building
                                self._ema_cross_valid,    
                                self.s_effective > self.s_effective_threshold,                  # confirmed EMA direction
                            ]
                            if sum(1 for x in entry_conds if x) >= 2:
                                signal = Signal.BUY
                elif new_dir == Direction.DOWN and self.prev_direction == Direction.UP:
                    if self.in_position:
                        signal = Signal.EXIT
                # Transition to TREND with new direction
                self.trend_start_price = c
                self.trend_start_atr = self.atr_val or 0
                self.trend_start_bar = self.bar_count
                self._start_new_profile(c)
                self.exhaustion_bar_count = 0
                self.trend_bar_count = 0
                # Move to TREND on next bar
                return self.regime, signal

            # If momentum truly died, go back to exhaustion
            if S < self.S_noise and not roc_zero_cross and not momentum_decay:
                self.regime = Regime.EXHAUSTION
                self.trend_before_exhaustion = self.prev_direction  # Original trend before reversal
                self.exhaustion_bar_count = 0
                self.reversal_confirm_count = 0
                self._exhaustion_phase_high = 0.0  # Fix 3c: reset when re-entering EXHAUSTION
                return self.regime, None

        # ─── E. CONTINUATION → TREND ─────────────────────────────────────
        elif self.regime == Regime.CONTINUATION:
            self.regime = Regime.TREND
            self.trend_bar_count = 0
            return self.regime, None

        # Update direction tracking
        if direction != Direction.NONE:
            if self.regime in (Regime.EXHAUSTION, Regime.REVERSAL):
                self.direction = direction

        return self.regime, signal

    def _check_exit(self, c: float) -> Optional[Signal]:
        """Check exit conditions for an open position.

        Exit rules:
          1. Trailing stop loss (profit >= 5% → lock stop at +5%)
          2. Prolonged exhaustion (consolidation for N bars)
          3. Confirmed reversal: reversal must persist for N bars with
             meaningful momentum in the opposite direction.  A single-
             bar spike into REVERSAL during consolidation is NOT enough.
        """
        if not self.in_position:
            return None

        # 2. Prolonged exhaustion (consolidation)
        if self.regime == Regime.EXHAUSTION and self.exhaustion_bar_count >= self.exhaustion_bars_limit:
            return Signal.EXIT

        # 3. Confirmed reversal exit — requires the reversal regime to
        #    have lasted at least `reversal_exit_confirm_bars` AND for
        #    momentum (signal strength) to be above the noise floor.
        #    This prevents premature jeets right before a pump.
        if self.regime == Regime.REVERSAL:
            if (self.reversal_bar_count >= self.reversal_exit_confirm_bars
                    and self.signal_strength > self.S_noise):
                return Signal.EXIT

        return None

    def _update_profile(self, c: float, vol: float, is_buy: bool, time: int):
        """Feed trade into current volume profile."""
        if self.current_profile:
            self.current_profile.add_trade(c, vol, is_buy, time)

    def _start_new_profile(self, price: float):
        """Archive current profile and start a new one."""
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
        """
        Process a single OHLCV bar.

        Returns dict with:
          regime, direction, signal, indicators, volume_profiles, etc.

        When _build_full_result=False (backtester fast path), returns only
        the signal and regime — skips volume profile serialization and
        most indicator dict construction.
        """
        self.bar_count += 1
        self._update_indicators(o, h, l, c, volume)

        # Feed volume into profile
        is_buy = c >= o  # Simple heuristic: green candle = buy pressure
        self._update_profile(c, volume, is_buy, time)

        # During warmup just collect data
        if self.bar_count < self.warmup:
            if _build_full_result:
                return self._build_result(time, c, None)
            return self._build_result_minimal(time, c, None)

        # If no profile yet, start one
        if self.current_profile is None:
            self._start_new_profile(c)

        # Detect regime
        regime, signal = self._detect_regime(c)

        # Check exit conditions
        if signal is None:
            exit_signal = self._check_exit(c)
            if exit_signal:
                signal = exit_signal

        if _build_full_result:
            return self._build_result(time, c, signal)
        return self._build_result_minimal(time, c, signal)

    def _build_result_minimal(self, time: int, price: float, signal: Optional[Signal]) -> dict:
        """Minimal result dict for backtester fast path — no profile serialization."""
        return {
            "time": time,
            "regime": self.regime.value,
            "direction": self.direction.value,
            "signal": signal.value if signal else Signal.NONE.value,
        }

    def _build_result(self, time: int, price: float, signal: Optional[Signal]) -> dict:
        """Build the full result dict to return from update()."""
        # Collect all profiles for frontend rendering
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
                "atr": self.atr_val,
                "atr_floor": self.atr_floor,
                "roc": self.m_hat,              # Kalman momentum (drop-in for ROC)
                "m_hat": self.m_hat,             # Explicit alias
                "p_hat": self.p_hat,             # Kalman filtered price
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
            },
            "volume_profiles": all_profiles,
            "in_position": self.in_position,
            "entry_price": self.entry_price,
            "exhaustion_bars": self.exhaustion_bar_count,
            "in_chop": self._is_chop_zone(price) or self._in_local_chop,
            "trend_bars": self.trend_bar_count,
        }
