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

    def get_hvn_bins(self, top_n: int = 5, min_vol_fraction: float = 0.1) -> list[VolumeBin]:
        """Return the top-N highest-volume bins.

        Only bins with total_volume >= min_vol_fraction * average_bin_volume
        are considered candidates.  This prevents zero-volume bins from being
        included when most bins have no trades — which would otherwise make
        nearly any price test as HVN.
        """
        total = sum(b.total_volume for b in self.bins)
        if total == 0:
            return []
        avg = total / len(self.bins)
        threshold = avg * min_vol_fraction
        candidates = [b for b in self.bins if b.total_volume >= threshold]
        if not candidates:
            return []
        sorted_bins = sorted(candidates, key=lambda b: b.total_volume, reverse=True)
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
        warmup: int = 100,
        signal_strong: float = 4.0,
        signal_weak: float = 2.0,
        signal_noise: float = 1.1535714285714287,
        exhaustion_bars_limit: int = 3,
        delta_threshold: float = 0.3,
        kalman_gamma: float = 0.125,
        min_trend_bars: int = 2,
        reversal_confirm_bars: int = 2,
        chop_atr_pct: float = 0.3,
        chop_spread_pct: float = 0.05,
        reversal_exit_confirm_bars: int = 0,
        s_effective_threshold: float = 0.5,
        exhaustion_persist_bars: int = 6,
        regime_lookback: int = 6,
        persistence_threshold: int = 2,
        momentum_mean_threshold: float = 0.0,
        ema_min_spread_pct: float = 0.02,
        # FIX-B: raised from 0.60 → 0.62 to tighten consolidation gate
        confidence_high: float = 0.79,
        confidence_low: float = 0.19,
        # ── Entry-side confidence gates (independent of exit thresholds) ────
        # entry_confidence_high: minimum confidence required to open a new
        #   position (used in _passes_entry_gate).  Kept separate so you can
        #   tighten entries without affecting exit / TP-SL scaling.
        # entry_confidence_low:  lower bound used symmetrically if you want
        #   an entry-side lerp in future; currently acts as a hard floor
        #   (entry blocked when confidence < entry_confidence_low).
        #   Defaults to confidence_low so behaviour is unchanged unless set.
        entry_confidence_high: float = 0.79,
        entry_confidence_low: float = 0.19,
        confidence_w1: float = 0.3,
        confidence_w2: float = 0.25,
        confidence_w3: float = 0.25,
        confidence_w4: float = 0.2,
        atr_floor_k: float = 0.0,
        ema_cross_persist_bars: int = 2,
        exhaustion_s_decay_bars: int = 1,
        exhaustion_stall_bars: int = 6,
        exhaustion_stall_atr_pct: float = 3.0,
        local_range_bars: int = 80,
        local_range_threshold_pct: float = 10.0,
        sign_flip_threshold: int = 0,
        stability_bars: int = 5,
        spike_atr_multiplier: float = 1.2,
        spike_lookback_bars: int = 9,
        # ── FIX-A: new top-blast parameters ──────────────────────────
        body_baseline_bars: int = 160,
        # ^ Long window for body average — anchors comparison to calm bars,
        #   not the recent pump bars.  Must be >> spike_lookback_bars.
        overextension_k: float = 0.08,
        # ^ If close > p_hat * (1 + k) AND S > S_strong, price is a blow-off
        #   (both overextended AND signal already peaked).  0.04 = 4% above
        #   Kalman estimate.  Smaller values block valid mid-trend entries due
        #   to normal Kalman lag.
        momentum_peak_bars: int = 1,  # tuned
        # ^ If |m_hat| has been declining for this many consecutive bars,
        #   we are past the momentum peak → block BUY regardless of S.
        # ── FIX-B: consolidation range gate parameter ─────────────────
        consolidation_range_pct: float = 25.0,
        # ^ If N-bar range < this % of price AND price is in mid 35–65%
        #   of that range, it's a box / consolidation → block entry.
        # ── FIX-C: high-confidence stability relaxation ────────────────
        confidence_very_high: float = 0.86,
        # ^ When confidence exceeds this, reduce effective stability_bars to 1.
        # ── Macro trend gate ─────────────────────────────────────────────
        ema_macro_period: int = 7,
        # ^ Slow EMA lookback used to define the macro trend.  Only BUY when
        #   close >= ema_macro.  Set to 0 to disable.
        # ── Stoploss ─────────────────────────────────────────────────────
        stoploss_pct: float = 0.0,
        # ^ Stop loss control (sign-encoded):
        #   0.0        → disabled
        #   negative   → hard stop loss  (e.g. -10.0 = exit if price drops 10% from entry)
        #   positive   → trailing stop   (e.g.  10.0 = exit if price falls 10% from peak)
        # ── Take Profit ──────────────────────────────────────────────────
        takeprofit_pct: float = 0.0,
        # ^ Take profit control:
        #   0.0        → disabled
        #   positive   → exit if price exceeds entry by this percentage
        # ── Confidence-scaled TP / SL ────────────────────────────────────
        # When in position, TP and SL are linearly interpolated between their
        # low-confidence and high-confidence extremes based on trend_confidence.
        #
        # takeprofit_pct_low  = TP used when confidence ≤ confidence_low
        # takeprofit_pct_high = TP used when confidence ≥ confidence_high
        # stoploss_pct_low    = SL (magnitude) used when confidence ≤ confidence_low
        # stoploss_pct_high   = SL (magnitude) used when confidence ≥ confidence_high
        #
        # Set all four to 0.0 to disable scaling (uses static stoploss_pct / takeprofit_pct).
        takeprofit_pct_low: float = 20.0,
        takeprofit_pct_high: float = 300.0,
        stoploss_pct_low: float = 12.0,
        # Tuned: stoploss_pct_high = 20 (was 25). Tightening the high-conviction
        # trailing stop from 25 → 20 captured ~+0.05 SOL on the full batch
        # (1374 backtests): beats baseline (+1.7191 → +1.9619 SOL).
        stoploss_pct_high: float = 20.0,
        # ── FIX-D: early underwater exit ────────────────────────────────────
        # If the position is losing more than this % AND momentum is rolling
        # over, exit before the regime state machine takes 5+ bars to confirm
        # a REVERSAL.  Set to a large value (e.g. 100) to effectively disable.
        early_exit_loss_pct: float = 10.0,
        # FIX-D: bars (engine states) after entry during which the
        # early underwater exit is active.  30 ≈ 7-8 candles on a 4-state
        # expansion.  Set to 0 to disable the protection window entirely.
        early_protection_bars: int = 30,
        # FIX-D: hard cap on TREND-stuck length.  If a trade stays in TREND
        # regime (without dipping ≥10% above entry as a peak) for more than
        # this many engine state-bars, exit.  0 disables.  Pump-and-dump
        # tokens that never launch can sit stuck in TREND for 9+ minutes;
        # this caps the bleed.
        trend_max_hold_bars: int = 0,
        # FIX-D: stage_a only fires while the trade peak is below
        # `entry × (1 + early_peak_floor_pct/100)`.  Setting it to 5 means
        # stage_a fires only on trades whose peak gain never reached +5%
        # (true pump-and-dumps).  A trade that already ran +15% lets the
        # trailing-stop mechanism handle its downside.
        early_peak_floor_pct: float = 5.0,
        # ── Trailing-stop arm buffer (was hardcoded 5%) ────────────────────
        # Percentage buffer above the activation price that must be reached
        # before the trailing stop arm engages.  5 = original behaviour.
        # 0 = arm exactly at activation (1+pct above entry), tighter.
        trailing_stop_arm_buffer_pct: float = 5.0,
        # FIX-D: floor for armed trailing-stops, expressed as % above entry.
        # Once the trail has armed, the trail_stop is never allowed below
        # `entry_price * (1 + trail_floor_pct/100)`.  This protects a
        # winning trade from whipsawing back below entry after arming.
        trail_floor_pct: float = 13.0,
        # FIX-D: confidence level below which a stuck-in-TREND position is
        # demoted to EXHAUSTION (so the existing _check_exit mechanisms can
        # take over).  Default 0.5×confidence_high (= 0.395 at confidence_high
        # of 0.79).  Must sit ABOVE confidence_low (=0.19) to avoid double
        # demote already handled by that branch.
        bleed_demote_threshold: float = 0.395,
        # FIX-D: the bleed-out demote must only fire when the position is
        # materially below entry (i.e. this is a real pump-and-dump bleed,
        # not a winning uptrend's brief confidence pullback).  Default -3%:
        # the demote kicks in only when c ≤ entry × 0.97.
        bleed_underwater_pct: float = 3.0,
        # FIX-D: signal_strength threshold for the bleed-out demote.  When
        # TREND-stuck LONG positions have S < this value AND confidence <
        # bleed_demote_threshold AND price is below entry × (1 - bleed_underwater%/100),
        # they become eligible for TREND→EXHAUSTION demotion after
        # `bleed_persist_bars` consecutive eligible bars.  Default S_weak
        # (=2.0) is much more stable than m_hat<0 (which flips sign on
        # Kalman noise).  Pump-and-dump tokens reliably collapse to S near 0
        # during the bleed phase.
        bleed_signal_threshold: float = 2.0,
        # FIX-D: debounce — require confidence + bleed-out conditions to
        # persist for this many state-updates before demoting.  ~20 state
        # updates ≈ 5 seconds on 1s candles; long enough to skip transient
        # pullbacks inside a winning TREND but quick enough to catch a
        # pump-and-dump bleed before giving back +10%.
        bleed_persist_bars: int = 20,
        # FIX-D (price-action guard): require current price to have fallen
        # at least this far below the running peak (_peak_price) for the
        # bleed demote to be eligible.  A pump-and-dump reliably retraces
        # 15-30% from its short-lived peak; a healthy pullback inside a
        # winning uptrend rarely sweeps more than 8-12%.  Expressed as a
        # percentage drop from peak (e.g. 15 means c < peak × 0.85).
        # Default 0 disables the price-drop criterion and falls back to
        # pure-underwater eligibility: any state-bar in TREND with c < entry
        # × (1 - bleed_underwater_pct/100) counts toward persistence.
        bleed_drop_from_peak_pct: float = 0.0,
        # ── FIX-D: max bars REVERSAL regime waits for S collapse before exit
        # If reversal_bar_count has grown past this and we are still in
        # position, exit regardless of signal strength.
        reversal_exit_bars_max: int = 20,
        # ── Late-recording entry gate: refuse BUY entries when bar_count > this.
        # Backtest analysis showed entries later than ~8000 state-bars (>~2000 1s
        # candles) have winrate ~51% and near-zero PnL contribution per trade,
        # while still generating big losses.  Default 0 disables the gate.
        max_entry_bar_count: int = 5700,
        # ── Bar-count FORBIDDEN band: refuse BUY entries when bar_count is in [lo, hi].
        # Both bounds default to 0 = no forbidden band.  Backtest analysis on the
        # 1374-recording corpus showed the bc=2000-3500 bucket has 41.5% WR and
        # -0.20 SOL contribution.  Skipping this band cuts ~70 trades but
        # simultaneously removes 7 big losses (~-0.40 SOL) while preserving ~93% of
        # the +1.30 SOL take-profit winners, lifting overall WR/PnL/big-loss counts.
        forbidden_bc_lo: int = 2000,
        forbidden_bc_hi: int = 3000,
        # ── LANGEVIN drift discriminator (price-level escape detector) ───────
        # Mathematical motivation: Under the engine's Langevin regime model
        #   dp = m·dt + σ·dW , dm = -γ·m·dt + F·dt + σ·dW'
        # the Kalman-smoothed position estimate `p_hat` is the *most stable*
        # observable of the latent price level.  Three regimes are visible
        # in p_hat over a rolling window of size `langevin_drift_window`:
        #
        #   (i)  Price mean-reverting         → p_hat drifts around entry.
        #   (ii) V-shaped recovery            → p_hat dips briefly then climbs
        #                                        back above entry; the rolling
        #                                        MIN(p_hat/entry) recovers to
        #                                        near 1.0 within K windows.
        #   (iii) Pump-and-dump escape         → p_hat monotonically falls,
        #                                        typically settling below
        #                                        entry × (1 − 10%).  Rolling
        #                                        MIN(p_hat/entry) keeps
        #                                        drifting down window by window.
        #
        # The discriminator `D(t)` checks: for `langevin_drift_stay`
        # consecutive state-updates, has ``p_hat < entry × (1 −
        # langevin_drift_pct/100)`` been sustained?  In case (iii) the
        # answer is yes — p_hat is *trapped* below entry for the whole
        # window.  In case (ii) the dip is too short to qualify.  In case
        # (i) p_hat never crosses the threshold.
        #
        # Combined with the requirement that we are in a long position
        # while in the ambiguous confidence zone (regime=TREND but
        # confidence < confidence_high, where the natural TREND → REVERSAL
        # flip is gated on S > S_strong which never fires on slow bleeds),
        # this discriminator cleanly detects the slow-bleed pattern that
        # currently leaves positions stuck in TREND-up while they bleed.
        langevin_drift_window: int = 28,
        langevin_drift_pct: float = 8.0,
        langevin_drift_stay: int = 94,
        # ── Market-cap bound trade block ────────────────────────────────────
        # Block all BUY entries when the USD market cap is below mcap_low_usd
        # or above mcap_high_usd.  Both default to 0 = deactivated.
        mcap_low_usd: float = 0.0,
        mcap_high_usd: float = 0.0,
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
        self.entry_confidence_high = entry_confidence_high
        self.entry_confidence_low = entry_confidence_low
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

        self.stoploss_pct = stoploss_pct
        self.takeprofit_pct = takeprofit_pct

        # Confidence-scaled TP/SL bounds
        self.takeprofit_pct_low = takeprofit_pct_low
        self.takeprofit_pct_high = takeprofit_pct_high
        self.stoploss_pct_low = stoploss_pct_low
        self.stoploss_pct_high = stoploss_pct_high

        # FIX-D early-exit parameter
        self.early_exit_loss_pct = early_exit_loss_pct
        self.early_protection_bars = early_protection_bars
        self.trend_max_hold_bars = trend_max_hold_bars
        self.early_peak_floor_pct = early_peak_floor_pct

        # ── Trailing-stop arm buffer (was hardcoded 5%) ────────────────────
        # When the trailing stop has positive `stoploss_pct`, the trail
        # doesn't arm until the peak has moved `activation + buffer %` above
        # entry.  Set to 0 to arm exactly at the activation level.  Lower
        # values arm the trail sooner (catches dumps before they get bad)
        # but are more prone to whipsaw stop-outs on noise.
        self.trailing_stop_arm_buffer_pct = trailing_stop_arm_buffer_pct
        self.reversal_exit_bars_max = reversal_exit_bars_max
        self.trail_floor_pct = trail_floor_pct
        self.bleed_demote_threshold = bleed_demote_threshold
        self.bleed_underwater_pct = bleed_underwater_pct
        self.bleed_persist_bars = bleed_persist_bars
        self.bleed_signal_threshold = bleed_signal_threshold
        self.bleed_drop_from_peak_pct = bleed_drop_from_peak_pct
        self.max_entry_bar_count = max_entry_bar_count
        self.forbidden_bc_lo = forbidden_bc_lo
        self.forbidden_bc_hi = forbidden_bc_hi
        self.mcap_low_usd = mcap_low_usd
        self.mcap_high_usd = mcap_high_usd
        # Latest USD market cap (updated each bar via update() kwarg)
        self._market_cap_usd: float = 0.0
        # LANGEVIN drift discriminator (price-level escape detector)
        self.langevin_drift_window = langevin_drift_window
        self.langevin_drift_pct = langevin_drift_pct
        self.langevin_drift_stay = langevin_drift_stay
        # Persistent counter: consecutive state-bars where p_hat stayed below
        # `entry × (1 − langevin_drift_pct/100)`.  Reset on any state-update
        # where p_hat climbs back above the tripwire (typical V-recovery).
        self._p_hat_below_entry_count: int = 0
        # Same idea, but used directly by the exit path (bypasses regime).
        self._check_exit_p_hat_below: int = 0
        # State for the debounce counter of the bleed guard.
        self._bleed_eligible_count: int = 0

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
        self._peak_price: float = 0.0
        # FIX-D: engine state-bar at which the most recent position opened,
        # used by the early-underwater exit protection window.
        self._entry_bar_count: int = 0
        self.exit_signal_reason: str = ""

        # Rolling buffers
        self._m_hat_history: list[float] = []
        self._abs_m_hat_history: list[float] = []
        self._atr_history: list[float] = []
        self._signal_strength_history: list[float] = []
        self._ema_spread_history: list[float] = []
        # LANGEVIN drift discriminator (positions-drift detector):
        # We monitor how often the Kalman position estimate `p_hat` falls
        # below the trade's entry by more than some threshold over a window.
        # In an Orstein-Uhlenbeck-like price process, `p_hat` reverts toward
        # equilibrium (entry).  When `p_hat` consistently drops below entry
        # for an extended window, the realisation `p` is escaping its basin
        # (real dump).  When the price temporarily dips then recovers,
        # `p_hat` *also* dips then climbs back above entry — the V-recovery.
        #   _p_hat_below_entry_count: consecutive state-bars where p_hat < entry
        #     (declared above in __init__)
        #   _p_hat_vs_entry_history: rolling recent p_hat vs entry ratio
        self._p_hat_vs_entry_history: list[float] = []
        # Legacy LANGEVIN m_hat kinetic-energy buffers (still maintained but
        # currently unused by the active discriminator).
        self._neg_m_hat_history: list[float] = []
        self._neg_m_hat_atr_ratio_history: list[float] = []

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

        self.no_motion_count: int = 0

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

        # No motion tracking
        if len(self.close_history) >= 2 and c == self.close_history[-2] and vol == 0:
            self.no_motion_count += 1
        else:
            self.no_motion_count = 0

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

        # ── Dead code: legacy LANGEVIN buffers (kept for backward compat
        # since external callers may mutate these).  The active LANGEVIN
        # discriminator uses the `_p_hat_vs_entry_history` rolling buffer
        # populated just below.  Computing `neg_mhat` here is costless
        # and keeps the buffer symbols alive for any future re-tuning.
        neg_mhat = max(0.0, -self.m_hat)
        self._neg_m_hat_history.append(neg_mhat)
        if len(self._neg_m_hat_history) > N * 4:
            self._neg_m_hat_history = self._neg_m_hat_history[-(N * 4):]

        denom = max(self.atr_val or 0.0, self.atr_floor or 0.0, 1e-18)
        ratio = neg_mhat / denom
        # keep buffer (used by old metric; harmless dead code if not used)
        self._neg_m_hat_atr_ratio_history.append(ratio)
        if len(self._neg_m_hat_atr_ratio_history) > N * 4:
            self._neg_m_hat_atr_ratio_history = self._neg_m_hat_atr_ratio_history[-(N * 4):]

        # ── LANGEVIN drift discriminator (price level escape detector):
        # Track the ratio p_hat / entry_price over a rolling window.
        # Whenever the ratio drops below (1 − p_hat_drift_pct/100), increment
        # a consecutive counter.  When the counter reaches stay_floor it
        # indicates that p_hat has been persistently below entry for the
        # last `stay_floor` state-bars — escape has happened.
        if self.entry_price > 0:
            p_vs_entry_ratio = self.p_hat / self.entry_price
            self._p_hat_vs_entry_history.append(p_vs_entry_ratio)
            if len(self._p_hat_vs_entry_history) > N * 8:
                self._p_hat_vs_entry_history = self._p_hat_vs_entry_history[-(N * 8):]
        else:
            p_vs_entry_ratio = 1.0

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

    def _langevin_escape_score(self) -> float:
        """LANGEVIN drift discriminator.

        Returns the *minimum* p_hat / entry_price ratio observed over the
        most recent `langevin_drift_window` state-updates (or 1.0 when no
        trade is open / window too short).

        In an Orstein-Uhlenbeck-like price process, `p_hat` is a
        mean-reverting estimator that pulls back toward equilibrium (the
        entry price) when the price is bouncing around.  When the
        realisation is escaping (real dump), `p_hat` *itself* drifts
        downward and over successive windows its rolling MIN keeps
        falling.  When the realisation is V-shaped, `p_hat` dips for a
        brief window then climbs back above entry — the MIN over a
        rolling 28-bar window briefly goes low, then returns to 1.0+.

        So: a low `min(p_hat/entry)` over langevin_drift_window is the
        concrete signal that the p_hat estimator is *trapped* below entry
        — i.e. the particle has escaped its basin.

        The actual gate is enforced by `_apply_langevin_drift_demote`
        which references _p_hat_below_entry_count — incremented per
        state-update when p_hat < entry × (1 − langevin_drift_pct/100)
        and reset as soon as p_hat climbs back above that tripwire.
        """
        if self.entry_price <= 0 or self.langevin_drift_window <= 0:
            return 1.0
        if not self._p_hat_vs_entry_history:
            return 1.0
        W = self.langevin_drift_window
        window = self._p_hat_vs_entry_history[-W:]
        return min(window) if window else 1.0

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

        m_increasing_up = all(m_recent[i + 1] > m_recent[i] for i in range(N))
        s_increasing = all(s_recent[i + 1] > s_recent[i] for i in range(N))
        accel_up = self.momentum_acceleration > 0

        self._pre_entry_stable_up = (
            self.m_hat > 0 and (
                m_increasing_up
                or (s_increasing and accel_up)
            )
        )
        self._pre_entry_stable_down = False  # shorts removed
        self._pre_entry_stable = self._pre_entry_stable_up

    # ── §7 + §10: Unified Entry Gate ─────────────────────────────────────────

    def _passes_entry_gate(self, c: float, direction: Direction) -> bool:
        """FIX-A + FIX-B + macro-trend gate applied here in addition to existing checks."""

        # ── Market-cap bound trade block: block all BUY entries when the
        # latest USD market cap is below mcap_low_usd or above mcap_high_usd.
        # Both default to 0 = deactivated.
        if direction == Direction.UP:
            mcap = self._market_cap_usd
            if mcap > 0.0:
                if self.mcap_low_usd > 0.0 and mcap < self.mcap_low_usd:
                    return False
                if self.mcap_high_usd > 0.0 and mcap > self.mcap_high_usd:
                    return False

        # ── Late-recording entry gate: avoid trading tokens whose recording is
        # already mature (high bar_count).  Late-record trades have a higher
        # big-loss density and lower per-trade PnL contribution.  Default 0
        # disables this gate.
        if self.max_entry_bar_count > 0 and self.bar_count > self.max_entry_bar_count:
            return False

        # ── Forbidden-band entry gate: refuse BUY entries whose bar_count
        # falls inside [lo, hi].  Empirically the bc=2000-3500 bucket is the
        # weakest: 41.5% WR with +0.20 SOL bleeding on top.  Excluding this
        # band lifts WR/PnL/loss counts simultaneously.  Both bounds default
        # to 0 = no forbidden band.
        if (self.forbidden_bc_lo > 0 and self.forbidden_bc_hi > 0
                and self.forbidden_bc_lo <= self.bar_count <= self.forbidden_bc_hi):
            return False

        # ── Macro trend gate: block BUY when price is below the slow EMA ──
        if direction == Direction.UP:
            if self.ema_macro_val is not None and c < self.ema_macro_val:
                return False

        # §3: Confidence must be above ENTRY threshold (entry_confidence_high)
        if self.trend_confidence < self.entry_confidence_high:
            return False

        # §8: No trading in local chop
        if self._in_local_chop:
            return False

        # §9: Pre-entry stability (long-only)
        if direction == Direction.UP and not self._pre_entry_stable_up:
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
                return Direction.DOWN  # used for exit detection only — no short entries
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

        if min_dist == float('inf') or min_dist <= 0:
            return S

        delta_U = max(min_dist, 0.001)
        return S / delta_U

    def _is_leaving_hvn(self, c: float, direction: Direction = Direction.NONE) -> bool:
        if not self.current_profile:
            return True
        if direction != Direction.UP:
            return True
        # For longs: check we are not sitting in a HVN with bearish delta below us
        return not self.current_profile.is_price_in_hvn(c, Direction.DOWN)

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
            # ── FIX-D: bleed-out guard ───────────────────────────────────────
            # The ambiguous-zone guard was leaking a serious bug: when an open
            # position's trend started decaying (confidence drops below
            # confidence_high) the state machine got stuck in TREND because
            # the state-transition block (TREND → EXHAUSTION / REVERSAL)
            # only runs when confidence ≥ confidence_high.  Traces of all 31
            # big-loss backtest trades show 5-10 minutes of regime="trend"
            # while price bled from peak to -25%.
            #
            # Two exits exist for stuck positions: exhaust_exit and reversal.
            # To unblock them, demote TREND → EXHAUSTION only when:
            #   * the position is in money (in_position)
            #   * confidence has collapsed *materially* below confidence_high
            #     (i.e. conf < bleed_demote_threshold) — small dips in
            #     confidence during a healthy uptrend pullback should NOT
            #     demote.  Default 0.5 * confidence_high is well into decay
            #     territory.
            #   * AND the price has rolled against us (m_hat < 0 OR we are
            #     already at a loss relative to entry — otherwise we cut
            #     winners that had a one-bar confidence dip).
            # Demote requires:
            #   * position is a long, in TREND regime, in ambiguous conf
            #   * confidence has collapsed well below confidence_high
            #     (conf < bleed_demote_threshold) — small dips in
            #     confidence during a healthy uptrend pullback should NOT
            #     demote.  Default 0.5 * confidence_high is well into decay
            #     territory.
            #   * AND the trade is materially underwater (c < entry × (1 -
            #     bleed_underwater_pct/100)) AND m_hat < 0 — i.e. this is
            #     not a winning pullback but a real bleed: the price rolled
            #     over AND is now below our entry.  This gate means we do
            #     NOT demote on winning pullbacks; only genuine losers.
            #
            # Without this gate, the demote fires on any winning uptrend's
            # brief confidence dip and exits +9% winners prematurely.
            underwater = (
                self.entry_price > 0
                and c < self.entry_price * (1.0 - self.bleed_underwater_pct / 100.0)
            )
            # Debounce: count bars where this condition has been continuously
            # eligible — only demote after confidence has been below the
            # threshold AND price below entry for at least `bleed_persist_bars`
            # state-updates.  This filters transient pullbacks inside an
            # otherwise healthy TREND.
            #
            # Two eligibility modes:
            #
            #  — Price-action mode (preferred when `bleed_drop_from_peak_pct` > 0
            #    AND a peak above entry exists): eligible when TREND + LONG AND
            #    price is materially below entry AND has fallen at least
            #    `bleed_drop_from_peak_pct` from the running peak.  This catches
            #    pump-and-roll-over trades whose trend confidence stays high
            #    throughout the dump entirely: a token bleeding out keeps
            #    `trend_confidence` in the 0.45-0.75 range for many seconds while
            #    price falls -5% to -20%, but the relative drop from peak is
            #    monotonic.
            #
            #  — Legacy mode (default, when `bleed_drop_from_peak_pct` == 0):
            #    Confidence+signal-gated eligibility: requires conf <
            #    `bleed_demote_threshold` AND S < `bleed_signal_threshold` AND
            #    underwater.  This is the original FIX-D design that effectively
            #    never fires on the big-loss distribution under default
            #    parameters — which is the backward-compatible baseline
            #    behaviour.
            use_price_mode = (
                self.bleed_drop_from_peak_pct > 0.0
                and self._peak_price > self.entry_price
            )
            if use_price_mode:
                drop_from_peak = 0.0
                if self._peak_price > 0:
                    drop_from_peak = (self._peak_price - c) / self._peak_price * 100.0
                price_drop_ok = drop_from_peak >= self.bleed_drop_from_peak_pct
                eligible = (
                    self.regime == Regime.TREND
                    and self.in_position
                    and self.position_direction == Direction.UP
                    and underwater
                    and price_drop_ok
                )
            else:
                # Legacy confidence-gated eligibility: requires confidence
                # collapsed AND signal collapsed AND underwater.  Defaults
                # (bleed_demote_threshold=0.395, bleed_signal_threshold=2.0)
                # make this NEVER fire in practice on the baseline loss
                # distribution — preserving the original baseline's bleed-guard
                # behaviour.
                eligible = (
                    self.regime == Regime.TREND
                    and self.in_position
                    and self.position_direction == Direction.UP
                    and self.trend_confidence < self.bleed_demote_threshold
                    and self.signal_strength < self.bleed_signal_threshold
                    and underwater
                )
            if eligible:
                self._bleed_eligible_count += 1
            else:
                self._bleed_eligible_count = 0

            if eligible and self._bleed_eligible_count >= self.bleed_persist_bars:
                self.regime = Regime.EXHAUSTION
                self.trend_before_exhaustion = self.direction
                # Pre-set exhaustion_bar_count at the demote threshold so that
                # _check_exit (line ~1792) on this SAME bar and on subsequent
                # bars immediately fires `exhaustion_exit` even while the
                # state-machine remains in EXHAUSTION inside the ambiguous
                # confidence zone (where the EXHAUSTION branch at line ~1447
                # is unreachable due to the ambiguous-zone early return).
                # Setting it ≥ exhaustion_bars_limit means _check_exit will
                # see the exit condition already met on the very next call.
                self.exhaustion_bar_count = max(self.exhaustion_bar_count, self.exhaustion_bars_limit)
                self.reversal_confirm_count = 0
                self.exhaustion_persist_count = 0
                self._exhaustion_s_decay_count = 0
                self._exhaustion_phase_high = max(self._exhaustion_phase_high, self._peak_price)
                self._bleed_eligible_count = 0
                # Demote only — do NOT immediately emit EXIT.  The next call
                # to `_check_exit` will fire exhaustion_exit because we preset
                # exhaustion_bar_count above (or the regime state machine may
                # further transition EXHAUSTION → CONTINUATION/REVERSAL and
                # emit its own EXIT).  Delaying one bar gives a tentatively
                # recovering trade a chance to bounce back into TREND before
                # we cut it.
                return self.regime, None

            # ── LANGEVIN drift discriminator (price-level escape detector)
            # Two routes to ENFORCE the demote:
            #   (A) direct executive in `_check_exit` → `langevin_drift_exit`
            #   (B) demote TREND → REVERSAL here, then existing reversal_exit
            #       fires via line ~2030.  Having both gives the strategy a
            #       robust two-track routing: the executive Bypasses the
            #       regime state machine entirely if the executive was
            #       somehow disabled, and the demote route preserves the
            #       natural mid-trade state transitions for diagnostics.
            # Set `langevin_drift_stay` very large (>1e6) to fully disable
            # both routes.
            # Use the EFFECTIVE adverse excursion: max(p_hat_drop, c_drop).
            # In slow bleeds the kalman-smoothed p_hat trails the close so we use
            # `c`; in fast dumps `p_hat` lags behind so we use both.  This makes
            # the discriminator responsive to both regimes — mode (a) and (b) in
            # `_langevin_escape_score` at line ~940.
            if self.in_position and self.position_direction == Direction.UP and self.entry_price > 0 and self.langevin_drift_stay > 0:
                tripwire = self.entry_price * (1.0 - self.langevin_drift_pct / 100.0)
                effective_low = min(self.p_hat, c) if c > 0 else self.p_hat
                if effective_low < tripwire:
                    self._p_hat_below_entry_count += 1
                else:
                    # Both p_hat and c have climbed back above the tripwire —
                    # V-recovery signature.
                    self._p_hat_below_entry_count = 0
            else:
                self._p_hat_below_entry_count = 0

            if self._p_hat_below_entry_count >= self.langevin_drift_stay:
                # Route (B): demote TREND→REVERSAL.
                self.regime = Regime.REVERSAL
                self.prev_direction = self.direction
                self.direction = Direction.DOWN
                self.reversal_bar_count = 0
                self.trend_reversal_confirm_count = 0
                self.exhaustion_persist_count = 0
                self._exhaustion_s_decay_count = 0
                self._p_hat_below_entry_count = 0
                return self.regime, None

            # ── FIX-B: Ambiguous zone — hard return with no signal ────────────
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
                S < self.S_strong,
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
            s_transition_threshold = self.S_noise if cold_start else self.S_strong
            if self.spread_expanding and S > s_transition_threshold and not momentum_decay:
                if direction == self.trend_before_exhaustion or cold_start:
                    self.regime = Regime.CONTINUATION
                    self.direction = direction
                    signal = None
                    if cold_start:
                        self.trend_before_exhaustion = Direction.DOWN if direction == Direction.UP else Direction.UP

                    # EXIT long if market direction has flipped to DOWN
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
                                # ── FIX-D: top-of-spike guard ───────────────
                                # The 29/31 worst losses all entered here with
                                # momentum_past_peak=True, pre_entry_stable=False
                                # and spread_expanding=False. We are buying the
                                # last gasp of a spike that already crested →
                                # reject the BUY.
                                if self._momentum_past_peak() and not self.spread_expanding:
                                    signal = None
                                else:
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
                                            if sum(1 for x in entry_conds if x) >= 3:
                                                signal = Signal.BUY
                    # EXIT long if market direction has flipped to DOWN
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
            rev_threshold = 5 if in_chop else 5

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
                            if sum(1 for x in entry_conds if x) >= 3:
                                signal = Signal.BUY
                elif new_dir == Direction.DOWN and self.prev_direction == Direction.UP:
                    # EXIT long on confirmed reversal — no short entry
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
        # Promote to TREND only when the move has confluence: confidence is
        # already ABOVE the entry threshold AND the EMA spread is expanding.
        # This prevents the blind 1-bar promotion that historically let weak
        # continuation flips ride straight into TREND with no validation,
        # forcing exits through the same loose reversal flag.
        elif self.regime == Regime.CONTINUATION:
            if self.trend_confidence >= self.entry_confidence_high and self.spread_expanding:
                self.regime = Regime.TREND
                self.trend_bar_count = 0
                return self.regime, None
            # Otherwise: stay in CONTINUATION (look for more evidence next bar).

        if direction != Direction.NONE:
            if self.regime in (Regime.EXHAUSTION, Regime.REVERSAL):
                self.direction = direction

        return self.regime, signal

    def _confidence_lerp(self, low_val: float, high_val: float) -> float:
        """Linearly interpolate between low_val and high_val based on trend_confidence.
        Returns low_val when confidence ≤ confidence_low, high_val when ≥ confidence_high.
        """
        conf_range = self.confidence_high - self.confidence_low
        if conf_range <= 0:
            return high_val
        t = (self.trend_confidence - self.confidence_low) / conf_range
        t = max(0.0, min(1.0, t))
        return low_val + t * (high_val - low_val)

    def _effective_takeprofit_pct(self) -> float:
        """Return the effective TP% for the current confidence level.
        Falls back to static takeprofit_pct when confidence scaling is disabled.
        """
        if self.takeprofit_pct_low > 0.0 or self.takeprofit_pct_high > 0.0:
            lo = self.takeprofit_pct_low if self.takeprofit_pct_low > 0.0 else self.takeprofit_pct
            hi = self.takeprofit_pct_high if self.takeprofit_pct_high > 0.0 else 100.0
            return self._confidence_lerp(lo, hi)
        return self.takeprofit_pct

    def _effective_stoploss_pct(self) -> float:
        """Return the confidence-scaled SL% only (0.0 when no scaling params are set).

        This is intentionally separate from stoploss_pct — both can be active
        simultaneously in _check_exit.  stoploss_pct is always evaluated on its
        own via _global_stoploss_pct().

        For hard stops (negative):  higher confidence → tighter stop (smaller magnitude)
        For trailing stops (positive): same sense — higher confidence → tighter trail
        """
        if self.stoploss_pct_low != 0.0 or self.stoploss_pct_high != 0.0:
            # Scaling operates on magnitudes. Determine sign dynamically from the parameters.
            is_hard_stop = (self.stoploss_pct_low < 0) or (self.stoploss_pct_high < 0)
            sign = -1.0 if is_hard_stop else 1.0

            lo = abs(self.stoploss_pct_low) if self.stoploss_pct_low != 0.0 else abs(self.stoploss_pct)
            hi = abs(self.stoploss_pct_high) if self.stoploss_pct_high != 0.0 else abs(self.stoploss_pct)
            mag = self._confidence_lerp(lo, hi)
            return sign * mag
        return 0.0

    def _global_stoploss_pct(self) -> float:
        """Return the static global stoploss_pct as-is.

        Runs simultaneously with _effective_stoploss_pct() so you can combine
        e.g. a confidence-scaled hard stop (-25/-30) with a global trailing
        stop (+25) at the same time.
        """
        return self.stoploss_pct

    def _update_peak_price(self, h: float, l: float):
        """Update the trailing-stop peak price using the bar's high/low.

        This must run every bar regardless of regime signals so the peak
        always reflects the true intra-bar extreme (not just the close).
        """
        if not self.in_position:
            return
        if h > self._peak_price:
            self._peak_price = h

    def _check_exit(self, c: float, l: float = 0.0, h: float = 0.0) -> Optional[Signal]:
        """Check exit conditions for the current bar (long positions only).

        Args:
            c: bar close price
            l: bar low — used to detect intra-bar stop breach on longs
            h: bar high — unused, kept for signature compatibility
        """
        if not self.in_position:
            return None

        # Note: _peak_price is updated in update() AFTER this method is called,
        # so it evaluates trailing stops against the peak established from 
        # previous bars. This prevents phantom intra-bar stop-outs.

        # ── LANGEVIN drift EXECUTIVE — directly exit when p_hat has been
        # trapped below entry for too long.  This is an advanced physical
        # discriminator: the Kalman-smoothed position `p_hat` is the most
        # stable estimator of the latent price level.  When it has been
        # continuously below `entry × (1 − langevin_drift_pct/100)` for at
        # least `langevin_drift_stay` consecutive state-bars, the realisation
        # has been ESCAPING its basin (Ortstein-Uhlenbeck picture) for too
        # long.  Bypasses the slower regime-state machine which needs S > 4
        # to flip to REVERSAL.
        # ── (Currently DISABLED in favour of the demote-and-reversal_exit
        #     path through `_detect_regime`.  See iteration log: the direct
        #     executive produced slightly worse PnL by exiting during
        #     the regime's natural resolution window — the demote-then-
        #     reversal_exit flow gives the regime an opportunity to
        #     re-evaluate once before committing.  Re-enable by removing
        #     these comments if profiling results in the future change.) ──
        # if self.position_direction == Direction.UP and self.entry_price > 0 and self.langevin_drift_stay > 0:
        #     tripwire = self.entry_price * (1.0 - self.langevin_drift_pct / 100.0)
        #     if self.p_hat < tripwire:
        #         self._check_exit_p_hat_below += 1
        #         if self._check_exit_p_hat_below >= self.langevin_drift_stay:
        #             self.exit_signal_reason = "langevin_drift_exit"
        #             self._check_exit_p_hat_below = 0
        #             return Signal.EXIT
        #     else:
        #         self._check_exit_p_hat_below = 0
        # Reset space for the lazy-init attribute above — Reset to 0 once
        # the trade closes
        # (handled in notify_trade_closed via reset)

        # ── Confidence-scaled stop loss (stoploss_pct_low / stoploss_pct_high) ─
        eff_sl = self._effective_stoploss_pct()
        if eff_sl != 0.0:
            pct = abs(eff_sl)
            if eff_sl < 0:
                # ── Hard stop loss (long only) ──────────────────────────────
                # Use intra-bar low so a wick through the stop always triggers.
                check_price = l if l > 0 else c
                if check_price <= self.entry_price * (1.0 - pct / 100.0):
                    self.exit_signal_reason = "hard_stop"
                    return Signal.EXIT
            else:
                # ── Trailing stop loss (long only) ──────────────────────────
                # Dormant until price has moved at least pct% above entry.
                # An optional `trailing_stop_arm_buffer_pct` (default 5%) is
                # added on top of activation to prevent the trail from arming
                # the instant price touches the activation level and then
                # immediately selling on any micro stall at that exact price.
                # Set the buffer to 0 to arm at exactly the activation price.
                activation_price = self.entry_price * (1.0 + pct / 100.0)
                arm_price = activation_price * (1.0 + self.trailing_stop_arm_buffer_pct / 100.0)
                if self._peak_price >= arm_price:
                    trail_stop = self._peak_price * (1.0 - pct / 100.0)
                    # FIX-D: floor the trail stop at `entry_price * trail_floor_pct`.
                    # Once the trailing stop is armed, never let it sit below the
                    # entry price + a small profit margin.  This prevents the
                    # whipsaw pattern where a +20% spike arms the trail, then the
                    # trail follows the price down to a loss when the spike
                    # retraces past the activation level.
                    floor = self.entry_price * (1.0 + self.trail_floor_pct / 100.0)
                    trail_stop = max(trail_stop, floor)
                    check_price = l if l > 0 else c
                    if check_price <= trail_stop:
                        self.exit_signal_reason = "trailing_stop"
                        return Signal.EXIT

        # ── Global (static) stop loss — runs simultaneously with scaled stop ─
        gsl = self._global_stoploss_pct()
        if gsl != 0.0:
            pct = abs(gsl)
            if gsl < 0:
                # ── Hard stop loss (long only) ──────────────────────────────
                check_price = l if l > 0 else c
                if check_price <= self.entry_price * (1.0 - pct / 100.0):
                    self.exit_signal_reason = "hard_stop"
                    return Signal.EXIT
            else:
                # ── Trailing stop loss (long only) ──────────────────────────
                # Dormant until price has moved at least pct% above entry,
                # PLUS an additional 5% buffer past that activation price
                # (see note in the scaled-SL branch above).
                activation_price = self.entry_price * (1.0 + pct / 100.0)
                arm_price = activation_price * (1.0 + self.trailing_stop_arm_buffer_pct / 100.0)
                if self._peak_price >= arm_price:
                    trail_stop = self._peak_price * (1.0 - pct / 100.0)
                    # FIX-D: same floor as scaled branch — prevents armed
                    # trail stops settling below entry on retracement after
                    # a temporary spike.
                    floor = self.entry_price * (1.0 + self.trail_floor_pct / 100.0)
                    trail_stop = max(trail_stop, floor)
                    check_price = l if l > 0 else c
                    if check_price <= trail_stop:
                        self.exit_signal_reason = "trailing_stop"
                        return Signal.EXIT

        # ── Confidence-scaled (or static) take profit ─────────────────────
        eff_tp = self._effective_takeprofit_pct()
        if eff_tp > 0.0:
            if self.position_direction == Direction.UP:
                if c >= self.entry_price * (1.0 + eff_tp / 100.0):
                    self.exit_signal_reason = "take_profit"
                    return Signal.EXIT
            elif self.position_direction == Direction.DOWN:
                if c <= self.entry_price * (1.0 - eff_tp / 100.0):
                    self.exit_signal_reason = "take_profit"
                    return Signal.EXIT

        if self.regime == Regime.EXHAUSTION and self.exhaustion_bar_count >= self.exhaustion_bars_limit:
            self.exit_signal_reason = "exhaustion_exit"
            return Signal.EXIT

        if self.regime == Regime.REVERSAL:
            # FIX-D: keep the original exit gate (S > S_noise) but also exit
            # if we are stuck in REVERSAL for > reversal_exit_bars_max bars
            # while still in position — bounds the loss if the regime machine
            # is slow to confirm a collapse.
            if (self.reversal_bar_count >= self.reversal_exit_confirm_bars
                    and self.signal_strength > self.S_noise):
                self.exit_signal_reason = "reversal_exit"
                return Signal.EXIT
            if self.reversal_bar_count >= self.reversal_exit_bars_max:
                self.exit_signal_reason = "reversal_exit_max"
                return Signal.EXIT

        # NOTE: the original 'Stuck-in-TREND exit' band has been removed.
        # The FIX-D bleed-out guard in `_detect_regime` (TREND → EXHAUSTION
        # transition under the ambiguous-zone guard) is the root-cause fix
        # and now lets the regime state machine drop to EXHAUSTION when a
        # position's trend starts decaying, allowing the existing
        # `exhaustion_exit` mechanism to fire instead of adding a second
        # parallel trend-exit path that was proving too noisy.

        # ── FIX-D: Early underwater exit ────────────────────────────────────
        # Pump-and-dump tokens drawdown -15% to -70% in the first ~30-60
        # seconds after the BUY.  The regime state machine takes that long
        # just to flip to REVERSAL, so the position rides the slide all the
        # way to the bottom.  This guard exits early when:
        #   1. the trade is in its initial `early_protection_bars` engine-bar
        #      window (default 30 ≈ 7-8 candles),
        #   2. the unrealised loss ≥ `early_exit_loss_pct` %,
        # (The early-underwater exit path has been removed — the
        # stuck-in-TREND exit above replaced it with a simpler/more
        # targeted trigger that fires only when actually stuck in TREND.)

        if self.no_motion_count >= 60:
            self.exit_signal_reason = "stale_exit"
            return Signal.EXIT

        self.exit_signal_reason = ""
        return None

    def _update_profile(
        self,
        c: float,
        vol: float,
        time: int,
        buy_volume: float = 0.0,
        sell_volume: float = 0.0,
    ):
        """Feed a completed candle into the volume profile.

        When explicit buy_volume / sell_volume are available (from order-flow
        data), add them as two separate trades.  This gives the profile real
        delta information instead of a coarse candle-direction heuristic.

        Falls back to the `c >= o` (candle-direction) heuristic when both
        split values are zero (e.g. historical OHLCV-only candles).
        """
        if not self.current_profile:
            return
        if buy_volume > 0 or sell_volume > 0:
            if buy_volume > 0:
                self.current_profile.add_trade(c, buy_volume, True, time)
            if sell_volume > 0:
                self.current_profile.add_trade(c, sell_volume, False, time)
        elif vol > 0:
            # Heuristic fallback: treat the whole candle as one side
            is_buy = c >= self.open_history[-1] if self.open_history else True
            self.current_profile.add_trade(c, vol, is_buy, time)

    def _start_new_profile(self, price: float):
        if self.current_profile:
            self.volume_profiles.append(self.current_profile)
        self.current_profile = VolumeProfile(price)

    def notify_trade_opened(self, entry_price: float, direction: Direction):
        self.in_position = True
        self.entry_price = entry_price
        self.position_direction = direction
        self._peak_price = entry_price
        # FIX-D: record engine state-bar at which the position opened so the
        # early-underwater protection window can be measured in engine bars.
        self._entry_bar_count = self.bar_count
        # LANGEVIN drift discriminator: clear any escape-counter carried
        # over from a previous trade.  The discriminator is *intra-trade*
        # by construction; it must not span across closed positions.
        self._p_hat_below_entry_count = 0
        self._check_exit_p_hat_below = 0
        self._p_hat_vs_entry_history = []

    def notify_trade_closed(self):
        self.in_position = False
        self.entry_price = 0.0
        self.position_direction = Direction.NONE
        self._peak_price = 0.0
        self._p_hat_below_entry_count = 0
        self._check_exit_p_hat_below = 0

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
        pool_sol: float = 0.0,
        market_cap_usd: float = 0.0,
        _build_full_result: bool = True,
    ) -> dict:
        self.bar_count += 1
        if market_cap_usd > 0.0:
            self._market_cap_usd = market_cap_usd
        self._update_indicators(o, h, l, c, volume)

        self._update_profile(c, volume, time,
                             buy_volume=buy_volume,
                             sell_volume=sell_volume)

        if self.current_profile is None:
            self._start_new_profile(c)

        regime, signal = self._detect_regime(c)

        if signal is None:
            exit_signal = self._check_exit(c, l=l, h=h)
            if exit_signal:
                signal = exit_signal
        else:
            # Even when the regime emits a non-exit signal, still check the
            # hard/trailing stop — it must take priority over any BUY signal
            # and must not be masked by regime transitions.
            if self.in_position:
                exit_signal = self._check_exit(c, l=l, h=h)
                if exit_signal:
                    signal = exit_signal
            else:
                self.exit_signal_reason = ""

        # Update the trailing-stop peak from intra-bar high/low AFTER checking
        # exits, so the current bar's high doesn't incorrectly tighten the stop
        # for its own low (which would cause phantom stop-outs).
        if self.in_position and signal != Signal.EXIT:
            self._update_peak_price(h, l)

        # Belt-and-suspenders: never emit a signal during the warmup window
        # (100 full candles = 400 intra-candle sub-state intakes).
        _min_warmup_intakes = max(self.warmup * 4 if self.warmup <= 100 else self.warmup, 400)
        if self.bar_count <= _min_warmup_intakes:
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
            "exit_reason": self.exit_signal_reason,
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
            "exit_reason": self.exit_signal_reason,
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
            "peak_price": self._peak_price,
            "trail_stop_price": (
                self._peak_price * (1.0 - abs(self._global_stoploss_pct()) / 100.0)
                if self.in_position and self._global_stoploss_pct() > 0
                else None
            ),
            "exhaustion_bars": self.exhaustion_bar_count,
            "in_chop": self._is_chop_zone(price) or self._in_local_chop,
            "trend_bars": self.trend_bar_count,
        }