"""
Strategy Engine V2 — New-Pair Memecoin Sniper (Rebuilt)

Philosophy:
  Memecoins launched on pump.fun go through a very predictable lifecycle:
    1. Launch at ~$5-15k mcap on the bonding curve
    2. Initial pump phase where early snipers buy in (3-4k is often the sweet spot)
    3. A fast dump back to a dip (weak hands selling)
    4. Re-accumulation / second wave if coin has genuine momentum

  This engine is designed for ONE purpose: identify high-quality NEW pairs
  at 3-4k mcap and extract profit targeting the 7-8k mcap region (~2x).

  The PnL profile seen in elite snipers (like the image: +$162K with mostly
  0%-200% wins and the rare 200-500% outlier, very few large losses) is
  achieved by:
    a) Filtering out rug-pull candidates before entry
    b) Entering at the right moment (not too early, not too late)
    c) Cutting losses fast and letting winners run slightly

─────────────────────────────────────────────────────────────────────────
  QUALITY FILTER MODEL
─────────────────────────────────────────────────────────────────────────

  A coin is considered "snipeable" if it passes ALL of these gates:

  1. MOMENTUM GATE — Buy pressure dominance
     - buy_volume_ratio  >  buy_vol_min_pct
     - i.e., at least 60% of recent volume is buyers vs sellers
     - Ensures we're not walking into a slow bleed

  2. VELOCITY GATE — Price must be appreciating fast
     - ROC (Rate of Change over roc_period bars) > roc_threshold
     - Memecoins that pump slowly don't pump at all

  3. VOLUME SPIKE GATE — Confirms real pump vs noise
     - vol_ratio (current_vol / avg_vol) > vol_spike_mult
     - Protects against entering on dead volume

  4. DIP RECOVERY MODE (alternative entry):
     - After the initial surge, if price dips > dip_threshold_pct from peak
       but holds above the launch support and buy pressure returns,
       enter the dip as a second-chance entry (often higher conviction)
     - The "recovery" is confirmed when:
         - ROC turns positive after being negative
         - buy_volume_ratio > recovery_buy_ratio (higher bar than initial entry)
         - Price stays above EMA(dip_ema_period)

  5. ANTI-RUG GATES — Pattern recognition for rug/dump coins
     - Max single-trade sell concentration < max_sell_concentration
       (a massive whale dump in a single tx is a dev sell)
     - Post-peak drawdown must not exceed the hard_rug_drawdown_pct
       before a signal fires (we'd have already stopped out)
     - Price must be within a reasonable range from the VWAP
       (extreme deviation = manipulation)

─────────────────────────────────────────────────────────────────────────
  EXIT MODEL
─────────────────────────────────────────────────────────────────────────

  Priority order (first trigger wins):

  1. TAKE PROFIT — Fixed % from entry (targeting 2x at ~7-8k from 3-4k entry)
     take_profit_pct = 90-100% (i.e. near double)

  2. TRAILING STOP — Activates once we're N% in profit
     trail_activate_pct: gains must exceed this before trailing kicks in
     trailing_stop_pct: trail distance from peak

  3. HARD STOP — Immediate cut if thesis fails
     hard_stop_pct: maximum loss tolerated (e.g. 25%)

  4. DIP RECOVERY STOP — In dip-recovery mode entries, use tighter stops
     because we're entering later in the timeline

  5. TIME STOP — If no movement after max_hold_bars, exit flat

  6. MOMENTUM DEATH — Exit if:
     - ROC stays negative for roc_death_bars consecutive bars AND
     - buy_volume_ratio drops below sell_dominance_threshold
     This means sellers have taken over — don't hold a dead coin

─────────────────────────────────────────────────────────────────────────
  MARKET CAP INTEGRATION
─────────────────────────────────────────────────────────────────────────

  The engine tracks market cap when provided (via feed). This allows:
  - Setting a hard target: "sell at $7,500 mcap"
  - Setting a floor: "never enter if mcap > $6k" (already past the zone)
  - Providing a mcap_take_profit alongside the pct take_profit

  Since mcap is not always available in candle data, mcap-based exits
  are optional layered on top of the price-based model.
"""

from __future__ import annotations
import math
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Optional, Deque
from collections import deque


# ── Enums (compatible with V1 interface) ──────────────────────────────────────

class Regime(Enum):
    IDLE         = "idle"
    TREND        = "trend"
    EXHAUSTION   = "exhaustion"
    REVERSAL     = "reversal"
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

class EntryMode(Enum):
    NONE            = "none"
    INITIAL_PUMP    = "initial_pump"    # Entry on early pump
    DIP_RECOVERY    = "dip_recovery"    # Entry on dip bounce


# ── Helper functions ──────────────────────────────────────────────────────────

def ema_step(prev: float, value: float, period: int) -> float:
    k = 2.0 / (period + 1)
    return value * k + prev * (1 - k)


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


# ── Strategy Engine V2 — Memecoin Sniper ──────────────────────────────────────

class StrategyEngineV2:
    """
    New-pair memecoin sniper engine.

    Targets entry at ~3-4k mcap, exit at ~7-8k mcap.
    Two entry modes:
      1. Initial pump entry (momentum breakout with quality filters)
      2. Dip recovery entry (bounce from first sell-off)

    Same external interface as StrategyEngine V1:
      - Feed OHLCV candles via update()
      - Returns dict with regime, direction, signal, indicators
      - Supports notify_trade_opened() / notify_trade_closed()
    """

    def __init__(
        self,
        # ── Warmup ──────────────────────────────────────────────────────────
        warmup: int = 2,
        # ^ Bars needed before any signal. Keep low for new pairs (they're young).

        # ── Momentum / ROC ──────────────────────────────────────────────────
        roc_period: int = 3,
        # ^ Rate of change lookback (short because we're on sub-minute candles).
        roc_threshold: float = 1.5,
        # ^ Minimum ROC % to confirm the pump is real.
        roc_death_bars: int = 5,
        # ^ Exit if ROC stays ≤ 0 for this many consecutive bars (momentum dead).

        # ── Volume ──────────────────────────────────────────────────────────
        vol_ma_period: int = 10,
        # ^ Rolling window for average volume.
        vol_spike_mult: float = 1.2,
        # ^ Volume must be > vol_spike_mult × recent average to confirm pump.

        # ── Buy/Sell Pressure ────────────────────────────────────────────────
        buy_vol_min_pct: float = 51.0,
        # ^ Minimum buy volume % of total volume to allow entry (quality filter).
        # If we only see buy_volume coming in from the engine feed (not split),
        # this falls back to volume spike alone.
        pressure_period: int = 8,
        # ^ Bars over which to calculate buy pressure ratio.

        # ── VWAP breakout ────────────────────────────────────────────────────
        vwap_period: int = 10,
        # ^ Rolling window for VWAP.
        breakout_pct: float = 0.5,
        # ^ Price must be > VWAP × (1 + breakout_pct/100) to confirm pump.

        # ── EMA trend filter ─────────────────────────────────────────────────
        ema_fast: int = 3,
        ema_slow: int = 8,
        # ^ Fast > Slow = bullish alignment. Short periods for new coins.

        # ── ATR ──────────────────────────────────────────────────────────────
        atr_period: int = 5,

        # ── Dip Recovery Mode ────────────────────────────────────────────────
        enable_dip_recovery: bool = True,
        # ^ Allow entering on the bounce after the initial dip.
        dip_threshold_pct: float = 15.0,
        # ^ How much must price fall from peak (post-entry or post-ATH) before
        #   we classify it as a "dip" that could bounce.
        dip_roc_recovery: float = 1.5,
        # ^ Minimum ROC needed to confirm the dip is bouncing.
        dip_buy_pressure: float = 60.0,
        # ^ Stricter buy pressure requirement for dip entries.
        dip_ema_period: int = 5,
        # ^ Dip entry only if price holds above this EMA (support check).
        dip_ema_mult: float = 0.98,
        # ^ Price >= EMA * dip_ema_mult to be valid support.
        max_dip_recovery_attempts: int = 1,
        # ^ Only attempt 1 dip recovery entry per coin session.

        # ── Anti-Rug Filters ─────────────────────────────────────────────────
        max_single_drop_pct: float = 40.0,
        # ^ If price drops more than this % in a single candle, treat as rug.
        # This fires an immediate exit if in position, or blocks entry.
        rug_vol_spike_mult: float = 5.0,
        # ^ A sell-side volume spike > this multiple = possible whale/dev dump.
        # If simultaneously price falling → rug detection.

        # ── Exit Rules ───────────────────────────────────────────────────────
        take_profit_pct: float = 20.0,
        # ^ Exit at this % gain (default 100% = 2x, targeting 7-8k from 3-4k).
        trailing_stop_pct: float = 4.0,
        # ^ Once we're trail_activate_pct in profit, trail this % from peak.
        trail_activate_pct: float = 8.0,
        # ^ Trailing stop only activates once we gain this % from entry.
        hard_stop_pct: float = 10.0,
        # ^ Hard stop loss. 25% is aggressive but needed for fast exits.
        dip_hard_stop_pct: float = 10.0,
        # ^ Tighter stop for dip recovery entries (we're entering later/riskier).
        max_hold_bars: int = 120,
        # ^ Time stop: exit after this many bars regardless.

        # ── Market Cap Targets (optional, 0 = disabled) ──────────────────────
        mcap_entry_max_usd: float = 0.0,
        # ^ Block entry if mcap > this value (we missed the 3-4k window).
        mcap_take_profit_usd: float = 0.0,
        # ^ Exit when mcap reaches this USD value (e.g., 7500 = $7.5k).

        # ── Cooldown ─────────────────────────────────────────────────────────
        cooldown_bars: int = 3,
        # ^ After an exit, wait this many bars before re-entering.

        # ── RSI Overbought Blocker ────────────────────────────────────────────
        rsi_period: int = 7,
        rsi_overbought: float = 85.0,
        # ^ Block new entries when RSI is this extreme (chasing the very top).
    ):
        # ── Store all params ──────────────────────────────────────────────────
        self.warmup = warmup
        self.roc_period = roc_period
        self.roc_threshold = roc_threshold
        self.roc_death_bars = roc_death_bars
        self.vol_ma_period = vol_ma_period
        self.vol_spike_mult = vol_spike_mult
        self.buy_vol_min_pct = buy_vol_min_pct
        self.pressure_period = pressure_period
        self.vwap_period = vwap_period
        self.breakout_pct = breakout_pct
        self.ema_fast_p = ema_fast
        self.ema_slow_p = ema_slow
        self.atr_period = atr_period
        self.enable_dip_recovery = enable_dip_recovery
        self.dip_threshold_pct = dip_threshold_pct
        self.dip_roc_recovery = dip_roc_recovery
        self.dip_buy_pressure = dip_buy_pressure
        self.dip_ema_period = dip_ema_period
        self.dip_ema_mult = dip_ema_mult
        self.max_dip_recovery_attempts = max_dip_recovery_attempts
        self.max_single_drop_pct = max_single_drop_pct
        self.rug_vol_spike_mult = rug_vol_spike_mult
        self.take_profit_pct = take_profit_pct
        self.trailing_stop_pct = trailing_stop_pct
        self.trail_activate_pct = trail_activate_pct
        self.hard_stop_pct = hard_stop_pct
        self.dip_hard_stop_pct = dip_hard_stop_pct
        self.max_hold_bars = max_hold_bars
        self.mcap_entry_max_usd = mcap_entry_max_usd
        self.mcap_take_profit_usd = mcap_take_profit_usd
        self.cooldown_bars = cooldown_bars
        self.rsi_period = rsi_period
        self.rsi_overbought = rsi_overbought

        # ── Rolling price / OHLCV history ─────────────────────────────────────
        self.close_history:  list[float] = []
        self.high_history:   list[float] = []
        self.low_history:    list[float] = []
        self.open_history:   list[float] = []
        self.volume_history: list[float] = []
        self.buy_vol_history: list[float] = []  # buy volume per bar (if available)

        # ── Indicators ────────────────────────────────────────────────────────
        self.ema_fast_val: Optional[float]  = None
        self.ema_slow_val: Optional[float]  = None
        self.dip_ema_val:  Optional[float]  = None
        self.atr_val:      Optional[float]  = None
        self._vwap:        float = 0.0
        self._vol_ma:      float = 0.0
        self._vol_ratio:   float = 0.0
        self._roc:         float = 0.0
        self._roc_negative_count: int = 0
        self._buy_pressure_pct: float = 50.0  # tracked buy vs sell vol %
        self._rsi:         float = 50.0
        self._rsi_avg_gain: float = 0.0
        self._rsi_avg_loss: float = 0.0

        # ── Regime / State tracking ───────────────────────────────────────────
        self.bar_count: int = 0
        self.last_update_time: int = -1
        self._baseline_state: dict = {}
        self.regime    = Regime.IDLE
        self.direction = Direction.NONE

        # ── Trade tracking ────────────────────────────────────────────────────
        self.in_position: bool = False
        self.entry_price: float = 0.0
        self.position_direction: Direction = Direction.NONE
        self.entry_mode: EntryMode = EntryMode.NONE
        self._peak_since_entry: float = 0.0
        self._hold_bar_count:  int = 0
        self._cooldown_count:  int = 0
        self._trailing_active: bool = False

        # ── Session-level state (per coin) ───────────────────────────────────
        # These track things about the coin's lifecycle since we started watching
        self._session_peak:      float = 0.0  # ATH seen since watching this coin
        self._session_launched:  bool  = False
        self._in_dip:            bool  = False  # we're in a dip after a pump
        self._dip_low:           float = 0.0   # lowest point of the dip so far
        self._dip_recovery_attempts: int = 0
        self._has_pumped:        bool  = False  # price has moved up significantly
        self._rug_detected:      bool  = False  # rug/dev-dump detected

        # Market cap (injected externally each tick if available)
        self._current_mcap_usd: float = 0.0

        # ── V1-compat fields (needed for frontend display) ────────────────────
        self.signal_strength:      float = 0.0
        self.trend_confidence:     float = 0.0
        self.is_trending:          bool  = False
        self.m_hat:                float = 0.0
        self.p_hat:                float = 0.0
        self.ema_spread:           float = 0.0
        self.spread_expanding:     bool  = False
        self._last_exit_reason:    Optional[str] = None

    # ─────────────────────────────────────────────────────────────────────────
    # Market cap injection (called by main.py for each candle update)
    # ─────────────────────────────────────────────────────────────────────────

    def set_market_cap(self, mcap_usd: float):
        """Inject the current USD market cap so the engine can use it for filters."""
        if mcap_usd and mcap_usd > 0:
            self._current_mcap_usd = mcap_usd

    # ─────────────────────────────────────────────────────────────────────────
    # Indicator calculations
    # ─────────────────────────────────────────────────────────────────────────

    def _update_indicators(
        self, o: float, h: float, l: float, c: float,
        vol: float, buy_vol: float, is_new_bar: bool = True
    ):
        if is_new_bar or len(self.close_history) == 0:
            self.close_history.append(c)
            self.high_history.append(h)
            self.low_history.append(l)
            self.open_history.append(o)
            self.volume_history.append(vol)
            self.buy_vol_history.append(buy_vol)
        else:
            self.close_history[-1] = c
            self.high_history[-1] = max(self.high_history[-1], h)
            self.low_history[-1] = min(self.low_history[-1], l)
            # Open is intact
            self.volume_history[-1] = vol
            self.buy_vol_history[-1] = buy_vol

        # Keep bounded (we don't need much history for new coins)
        max_hist = max(
            self.vwap_period, self.vol_ma_period, self.roc_period,
            self.rsi_period, self.atr_period, self.pressure_period,
            self.dip_ema_period, 50
        ) + 10
        if len(self.close_history) > max_hist:
            self.close_history   = self.close_history[-max_hist:]
            self.high_history    = self.high_history[-max_hist:]
            self.low_history     = self.low_history[-max_hist:]
            self.open_history    = self.open_history[-max_hist:]
            self.volume_history  = self.volume_history[-max_hist:]
            self.buy_vol_history = self.buy_vol_history[-max_hist:]

        # ── EMA ──────────────────────────────────────────────────────────────
        if self._baseline_state.get('ema_fast') is None and self.ema_fast_val is None:
            self.ema_fast_val = c
            self.ema_slow_val = c
            self.dip_ema_val  = c
            self._baseline_state['ema_fast'] = c
            self._baseline_state['ema_slow'] = c
            self._baseline_state['dip_ema'] = c
        else:
            base_ef = self._baseline_state.get('ema_fast', c)
            base_es = self._baseline_state.get('ema_slow', c)
            base_de = self._baseline_state.get('dip_ema', c)
            self.ema_fast_val = ema_step(base_ef, c, self.ema_fast_p)
            self.ema_slow_val = ema_step(base_es, c, self.ema_slow_p)
            self.dip_ema_val  = ema_step(base_de, c, self.dip_ema_period)

        prev_spread = self.ema_spread
        ef = self.ema_fast_val or 0.0
        es = self.ema_slow_val or 0.0
        self.ema_spread     = ef - es
        self.spread_expanding = abs(self.ema_spread) > abs(prev_spread)

        # ── ATR ──────────────────────────────────────────────────────────────
        prev_c = self.close_history[-2] if len(self.close_history) >= 2 else o
        tr_cur_h = self.high_history[-1]
        tr_cur_l = self.low_history[-1]
        tr = max(tr_cur_h - tr_cur_l, abs(tr_cur_h - prev_c), abs(tr_cur_l - prev_c))

        if self._baseline_state.get('atr') is None and self.atr_val is None:
            self.atr_val = tr
            self._baseline_state['atr'] = tr
        else:
            base_atr = self._baseline_state.get('atr', tr)
            self.atr_val = ema_step(base_atr, tr, self.atr_period)

        # ── VWAP ─────────────────────────────────────────────────────────────
        n = min(len(self.close_history), self.vwap_period)
        if n >= 2:
            pv_sum, v_sum = 0.0, 0.0
            for i in range(-n, 0):
                typical = (self.high_history[i] + self.low_history[i] + self.close_history[i]) / 3.0
                v = max(self.volume_history[i], 1e-9)
                pv_sum += typical * v
                v_sum  += v
            self._vwap = pv_sum / v_sum if v_sum > 0 else c
        else:
            self._vwap = c

        # ── Volume MA & spike ratio ───────────────────────────────────────────
        n_vol = min(len(self.volume_history), self.vol_ma_period)
        if n_vol >= 2:
            self._vol_ma    = sum(self.volume_history[-n_vol:]) / n_vol
            self._vol_ratio = vol / self._vol_ma if self._vol_ma > 0 else 0.0
        else:
            self._vol_ma    = vol
            self._vol_ratio = 1.0

        # ── Buy pressure (buy volume as % of total) ───────────────────────────
        n_p = min(len(self.volume_history), self.pressure_period)
        if n_p >= 2:
            total_vol = sum(self.volume_history[-n_p:])
            total_buy = sum(self.buy_vol_history[-n_p:])
            if total_vol > 0:
                self._buy_pressure_pct = (total_buy / total_vol) * 100.0
            else:
                self._buy_pressure_pct = 50.0
        else:
            self._buy_pressure_pct = 50.0

        # ── ROC ──────────────────────────────────────────────────────────────
        if len(self.close_history) > self.roc_period:
            prev_p = self.close_history[-(self.roc_period + 1)]
            if prev_p > 0:
                self._roc = ((c - prev_p) / prev_p) * 100.0
            else:
                self._roc = 0.0
        else:
            self._roc = 0.0

        if self._roc <= 0:
            self._roc_negative_count += 1
        else:
            self._roc_negative_count = 0

        # ── RSI ──────────────────────────────────────────────────────────────
        if len(self.close_history) >= 2:
            chg  = c - self.close_history[-2]
            gain = max(chg, 0.0)
            loss = max(-chg, 0.0)
            if self.bar_count <= self.rsi_period + 2:
                self._rsi_avg_gain = ema_step(self._rsi_avg_gain, gain, self.rsi_period)
                self._rsi_avg_loss = ema_step(self._rsi_avg_loss, loss, self.rsi_period)
            else:
                self._rsi_avg_gain = (self._rsi_avg_gain * (self.rsi_period - 1) + gain) / self.rsi_period
                self._rsi_avg_loss = (self._rsi_avg_loss * (self.rsi_period - 1) + loss) / self.rsi_period
            if self._rsi_avg_loss > 0:
                self._rsi = 100.0 - (100.0 / (1.0 + self._rsi_avg_gain / self._rsi_avg_loss))
            else:
                self._rsi = 100.0 if self._rsi_avg_gain > 0 else 50.0

        # ── Session peak tracking ─────────────────────────────────────────────
        if c > self._session_peak:
            self._session_peak = c
            # Once price is up > 5% above first seen price, mark as "pumped"
            if len(self.close_history) >= 3:
                first_price = self.close_history[0]
                if first_price > 0 and c / first_price > 1.05:
                    self._has_pumped = True

        # ── Dip detection ──────────────────────────────────────────────────────
        # A "dip" is defined as: price has fallen dip_threshold_pct from session peak
        if self._session_peak > 0 and not self.in_position:
            drop_from_peak = (self._session_peak - c) / self._session_peak * 100.0
            if drop_from_peak >= self.dip_threshold_pct and self._has_pumped:
                if not self._in_dip:
                    self._in_dip = True
                    self._dip_low = c
                elif c < self._dip_low:
                    self._dip_low = c
            elif self._in_dip and c > self._dip_low * 1.02:
                # Price is recovering from the dip low — potential bounce
                pass  # keep _in_dip=True so we can detect the bounce entry

        # ── V1 compat metrics ─────────────────────────────────────────────────
        if self.atr_val and self.atr_val > 0 and c > 0:
            roc_abs = abs(self._roc) / 100.0 * c
            self.signal_strength = _clamp(roc_abs / self.atr_val, 0.0, 5.0)
        else:
            self.signal_strength = 0.0

        self._update_trend_confidence(c)

        if len(self.close_history) >= 2:
            self.m_hat = c - self.close_history[-2]
        self.p_hat = c

    def _update_trend_confidence(self, c: float):
        components = []
        if self.ema_fast_val and self.ema_slow_val:
            components.append(1.0 if self.ema_fast_val > self.ema_slow_val else 0.0)
        vol_conf = _clamp(self._vol_ratio / max(self.vol_spike_mult, 1), 0.0, 1.0)
        components.append(vol_conf)
        roc_conf = _clamp(max(self._roc, 0) / max(self.roc_threshold * 2, 1), 0.0, 1.0)
        components.append(roc_conf)
        bp_conf = _clamp((self._buy_pressure_pct - 50.0) / 50.0, 0.0, 1.0)
        components.append(bp_conf)
        if self._vwap > 0 and c > self._vwap:
            vwap_conf = _clamp((c / self._vwap - 1.0) * 100 / max(self.breakout_pct * 2, 1), 0.0, 1.0)
        else:
            vwap_conf = 0.0
        components.append(vwap_conf)
        self.trend_confidence = sum(components) / len(components) if components else 0.0
        self.is_trending = self.trend_confidence > 0.55

    # ─────────────────────────────────────────────────────────────────────────
    # Rug detection
    # ─────────────────────────────────────────────────────────────────────────

    def _check_rug_pattern(self, o: float, c: float, vol: float) -> bool:
        """
        Detect rug/dev dump patterns:
        1. Single candle drop > max_single_drop_pct
        2. Massive sell-side volume spike while price is falling
        """
        if o > 0:
            candle_drop_pct = (o - c) / o * 100.0
            if candle_drop_pct >= self.max_single_drop_pct:
                return True

        # Sell-side volume spike: vol_ratio is very high AND price is falling
        if (self._vol_ratio >= self.rug_vol_spike_mult
                and c < o
                and len(self.close_history) >= 2
                and c < self.close_history[-2]):
            # Only flag as rug if buy pressure is very low too
            if self._buy_pressure_pct < 30.0:
                return True

        return False

    # ─────────────────────────────────────────────────────────────────────────
    # Entry conditions
    # ─────────────────────────────────────────────────────────────────────────

    def _check_initial_pump_entry(self, c: float) -> bool:
        """
        Gate 1: Initial pump entry.
        Buy when a genuine new-pair pump begins — typically within the first
        few minutes of launch at 3-4k mcap.
        """
        # Basic guards
        if self.bar_count < self.warmup:
            return False
        if self._cooldown_count > 0:
            return False
        if self.in_position:
            return False
        if self._rug_detected:
            return False

        # Don't enter if we already missed the window (mcap too high)
        if self.mcap_entry_max_usd > 0 and self._current_mcap_usd > self.mcap_entry_max_usd:
            return False

        # Don't enter if RSI is at extreme overbought (chasing the very top)
        if self.rsi_overbought > 0 and self._rsi > self.rsi_overbought:
            return False

        # 1. Momentum (ROC) must be strong — price moving fast upward
        if self._roc < self.roc_threshold:
            return False

        # 2. Volume spike — confirms real pump vs random noise
        has_real_vol_data = any(v > 0 for v in self.vol_history[-5:])
        if has_real_vol_data and self._vol_ratio < self.vol_spike_mult:
            return False

        # 3. Buy pressure dominance (if buy_vol data is available)
        # If buy_vol_history has non-zero data (real split provided), enforce it
        has_real_bvol_data = any(v > 0 for v in self.buy_vol_history[-5:])
        if has_real_bvol_data and self._buy_pressure_pct < self.buy_vol_min_pct:
            return False

        # 4. Price must be above VWAP breakout level
        vwap_target = self._vwap * (1.0 + self.breakout_pct / 100.0)
        if c < vwap_target:
            return False

        # 5. EMA bullish alignment (fast > slow)
        if self.ema_fast_val and self.ema_slow_val:
            if self.ema_fast_val <= self.ema_slow_val:
                return False

        return True

    def _check_dip_recovery_entry(self, c: float) -> bool:
        """
        Gate 2: Dip recovery entry.
        The coin pumped, dipped, and is now recovering. Enter the bounce.
        Higher conviction entries because the coin has already proven demand.
        """
        if not self.enable_dip_recovery:
            return False
        if not self._in_dip:
            return False
        if self.bar_count < self.warmup:
            return False
        if self._cooldown_count > 0:
            return False
        if self.in_position:
            return False
        if self._rug_detected:
            return False
        if self._dip_recovery_attempts >= self.max_dip_recovery_attempts:
            return False

        # Don't enter if mcap is already too high (past our target zone)
        if self.mcap_entry_max_usd > 0 and self._current_mcap_usd > self.mcap_entry_max_usd:
            return False

        # The dip is bouncing when:
        # 1. ROC has turned positive and above our recovery threshold
        if self._roc < self.dip_roc_recovery:
            return False

        # 2. Buy pressure is stronger than normal (buyers are coming back)
        has_real_bvol_data = any(v > 0 for v in self.buy_vol_history[-5:] if v > 0)
        if has_real_bvol_data and self._buy_pressure_pct < self.dip_buy_pressure:
            return False

        # 3. Price is above the dip EMA * multiplier (holding support)
        if self.dip_ema_val and c < self.dip_ema_val * self.dip_ema_mult:
            return False

        # 4. Price must be recovering from the dip low (at least slightly)
        if self._dip_low > 0 and c < self._dip_low * 1.01:
            return False

        # 5. Volume confirms the bounce
        if self._vol_ratio < self.vol_spike_mult * 0.7:  # slightly relaxed for bounces
            return False

        return True

    # ─────────────────────────────────────────────────────────────────────────
    # Exit conditions
    # ─────────────────────────────────────────────────────────────────────────

    def _check_exit_signal(self, o: float, c: float, vol: float) -> Optional[str]:
        """Check all exit conditions. Returns the exit reason or None."""
        if not self.in_position:
            return None

        # Update peak since entry
        if c > self._peak_since_entry:
            self._peak_since_entry = c
            # Activate trailing stop once we hit the activation threshold
            if (self.entry_price > 0
                    and (self._peak_since_entry / self.entry_price - 1.0) * 100.0
                    >= self.trail_activate_pct):
                self._trailing_active = True

        self._hold_bar_count += 1

        # ── 0. Rug/dump detection — emergency exit ────────────────────────────
        if self._check_rug_pattern(o, c, vol):
            self._rug_detected = True
            return "rug_detected"

        # ── 1. Hard stop loss ─────────────────────────────────────────────────
        active_hard_stop = (
            self.dip_hard_stop_pct
            if self.entry_mode == EntryMode.DIP_RECOVERY
            else self.hard_stop_pct
        )
        if active_hard_stop > 0 and self.entry_price > 0:
            stop_price = self.entry_price * (1.0 - active_hard_stop / 100.0)
            if c <= stop_price:
                return "hard_stop"

        # ── 2. Trailing stop ──────────────────────────────────────────────────
        if self._trailing_active and self.trailing_stop_pct > 0 and self._peak_since_entry > 0:
            trail_price = self._peak_since_entry * (1.0 - self.trailing_stop_pct / 100.0)
            if c <= trail_price:
                return "trailing_stop"

        # ── 3. Take profit (fixed %) ──────────────────────────────────────────
        if self.take_profit_pct > 0 and self.entry_price > 0:
            tp_price = self.entry_price * (1.0 + self.take_profit_pct / 100.0)
            if c >= tp_price:
                return "take_profit"

        # ── 4. Market cap take profit ─────────────────────────────────────────
        if (self.mcap_take_profit_usd > 0
                and self._current_mcap_usd >= self.mcap_take_profit_usd
                and self._current_mcap_usd > 0):
            return "mcap_take_profit"

        # ── 5. Time stop ──────────────────────────────────────────────────────
        if self.max_hold_bars > 0 and self._hold_bar_count >= self.max_hold_bars:
            return "max_hold_time"

        # ── 6. Momentum death — sellers have taken over ───────────────────────
        if (self.roc_death_bars > 0
                and self._roc_negative_count >= self.roc_death_bars):
            # Only exit on momentum death if we're not in profit (let winners run)
            if self.entry_price > 0 and c <= self.entry_price * 1.05:
                return "momentum_death"

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Trade notification (V1-compatible interface)
    # ─────────────────────────────────────────────────────────────────────────

    def notify_trade_opened(self, entry_price: float, direction: Direction):
        self.in_position         = True
        self.entry_price         = entry_price
        self.position_direction  = direction
        self._peak_since_entry   = entry_price
        self._hold_bar_count     = 0
        self._cooldown_count     = 0
        self._trailing_active    = False
        # entry_mode is set before calling this

    def notify_trade_closed(self):
        self.in_position         = False
        self.entry_price         = 0.0
        self.position_direction  = Direction.NONE
        self._peak_since_entry   = 0.0
        self._hold_bar_count     = 0
        self._cooldown_count     = self.cooldown_bars
        self._trailing_active    = False
        self.entry_mode          = EntryMode.NONE
        # Reset dip state after closing
        self._in_dip             = False
        self._dip_low            = 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # Main update loop
    # ─────────────────────────────────────────────────────────────────────────

    def update(
        self,
        time:        int,
        o:           float,
        h:           float,
        l:           float,
        c:           float,
        volume:      float = 0.0,
        buy_volume:  float = 0.0,
        sell_volume: float = 0.0,
        market_cap_usd: float = 0.0,
        _build_full_result: bool = True,
    ) -> dict:
        """
        Process one OHLCV candle through the sniper engine.

        buy_volume  — optional: SOL volume from buy-side trades
        sell_volume — optional: SOL volume from sell-side trades
        If buy_volume is provided, it's used directly for buy pressure.
        If only total volume is provided, buy pressure defaults to neutral.
        """
        is_new_bar = (time != self.last_update_time)

        if is_new_bar:
            self.bar_count += 1
            self.last_update_time = time
            # Capture state to baseline before iterating
            self._baseline_state['ema_fast'] = self.ema_fast_val
            self._baseline_state['ema_slow'] = self.ema_slow_val
            self._baseline_state['dip_ema'] = self.dip_ema_val
            self._baseline_state['atr'] = self.atr_val

        # Inject mcap if provided
        if market_cap_usd > 0:
            self.set_market_cap(market_cap_usd)

        # If buy_volume not provided but both given, infer
        if buy_volume <= 0 and sell_volume > 0 and volume > sell_volume:
            buy_volume = volume - sell_volume
        # If only total volume, split proxy: use neutral (0.5 * vol)
        actual_buy_vol = buy_volume if buy_volume > 0 else volume * 0.5

        # Update all indicators
        self._update_indicators(o, h, l, c, volume, actual_buy_vol, is_new_bar)

        # Decrement cooldown
        if self._cooldown_count > 0:
            self._cooldown_count -= 1

        # ── Rug detection (outside position check) ────────────────────────────
        if not self.in_position and self._check_rug_pattern(o, c, volume):
            self._rug_detected = True

        # ── Determine signal ──────────────────────────────────────────────────
        signal = None
        exit_reason = None
        detected_entry_mode = EntryMode.NONE

        # Check exit first
        if self.in_position:
            exit_reason = self._check_exit_signal(o, c, volume)
            if exit_reason:
                signal = Signal.EXIT
                self.direction = Direction.DOWN
                if exit_reason in ("hard_stop", "momentum_death", "rug_detected"):
                    self.regime = Regime.REVERSAL
                else:
                    self.regime = Regime.EXHAUSTION

        # Check entries
        elif self.bar_count > self.warmup and not self._rug_detected:
            if self._check_initial_pump_entry(c):
                signal = Signal.BUY
                detected_entry_mode = EntryMode.INITIAL_PUMP
                self.direction = Direction.UP
                self.regime    = Regime.TREND
            elif self._check_dip_recovery_entry(c):
                signal = Signal.BUY
                detected_entry_mode = EntryMode.DIP_RECOVERY
                self.direction = Direction.UP
                self.regime    = Regime.CONTINUATION

        # Update entry mode before returning (so ForwardTester can read it)
        if signal == Signal.BUY:
            self.entry_mode = detected_entry_mode

        # Background regime update (no signal case)
        if signal is None:
            if self.in_position:
                self.regime    = Regime.TREND if self._roc > 0 else Regime.EXHAUSTION
                self.direction = Direction.UP if self._roc > 0 else Direction.DOWN
            elif self._in_dip:
                self.regime    = Regime.REVERSAL
                self.direction = Direction.DOWN
            elif self._roc > self.roc_threshold * 0.5 and self._vol_ratio > 1.0:
                self.regime    = Regime.CONTINUATION
                self.direction = Direction.UP
            else:
                self.regime    = Regime.IDLE
                self.direction = Direction.NONE

        # Belt-and-suspenders: no signals during warmup
        if self.bar_count <= self.warmup:
            signal = None

        self._last_exit_reason = exit_reason

        if _build_full_result:
            return self._build_result(time, c, signal, exit_reason, detected_entry_mode)
        return self._build_result_minimal(time, c, signal)

    def _build_result_minimal(self, time: int, price: float,
                               signal: Optional[Signal]) -> dict:
        return {
            "time":      time,
            "regime":    self.regime.value,
            "direction": self.direction.value,
            "signal":    signal.value if signal else Signal.NONE.value,
        }

    def _build_result(
        self, time: int, price: float,
        signal: Optional[Signal],
        exit_reason: Optional[str] = None,
        entry_mode: EntryMode = EntryMode.NONE,
    ) -> dict:
        # Compute gain from entry for display
        gain_pct = 0.0
        if self.in_position and self.entry_price > 0:
            gain_pct = (price / self.entry_price - 1.0) * 100.0

        return {
            "time":      time,
            "regime":    self.regime.value,
            "direction": self.direction.value,
            "signal":    signal.value if signal else Signal.NONE.value,
            "indicators": {
                # ── Core V2 sniper indicators ──────────────────────────────
                "roc_pct":            self._roc,
                "vol_ratio":          self._vol_ratio,
                "buy_pressure_pct":   self._buy_pressure_pct,
                "vwap":               self._vwap,
                "rsi":                self._rsi,
                "roc_negative_bars":  self._roc_negative_count,
                "session_peak":       self._session_peak,
                "in_dip":             self._in_dip,
                "dip_low":            self._dip_low,
                "has_pumped":         self._has_pumped,
                "rug_detected":       self._rug_detected,
                "entry_mode":         entry_mode.value if entry_mode != EntryMode.NONE else (
                                          self.entry_mode.value if self.in_position else "none"),
                "trailing_active":    self._trailing_active,
                "gain_pct":           round(gain_pct, 2),
                "peak_since_entry":   self._peak_since_entry if self.in_position else 0.0,
                "hold_bars":          self._hold_bar_count if self.in_position else 0,
                "exit_reason":        exit_reason,
                "current_mcap_usd":   self._current_mcap_usd,

                # ── V1 compat fields (needed for frontend chart rendering) ─
                "ema_fast":            self.ema_fast_val,
                "ema_slow":            self.ema_slow_val,
                "ema_macro":           self.dip_ema_val,
                "atr":                 self.atr_val,
                "atr_floor":           self.atr_val,
                "roc":                 self.m_hat,
                "m_hat":               self.m_hat,
                "p_hat":               self.p_hat,
                "signal_strength":     self.signal_strength,
                "momentum_acceleration": 0.0,
                "s_effective":         self.signal_strength,
                "ema_spread":          self.ema_spread,
                "spread_expanding":    self.spread_expanding,
                "trend_confidence":    self.trend_confidence,
                "is_trending":         self.is_trending,
                "ema_cross_valid":     bool(self.ema_fast_val and self.ema_slow_val
                                           and self.ema_fast_val > self.ema_slow_val),
                "pre_entry_stable":    self._roc > 0,
                "in_local_chop":       not self.is_trending and abs(self._roc) < self.roc_threshold * 0.3,
                "price_overextended":  self._rsi > self.rsi_overbought if self.rsi_overbought > 0 else False,
                "momentum_past_peak":  self._roc_negative_count >= self.roc_death_bars // 2,
            },
            "volume_profiles": [],
            "in_position":   self.in_position,
            "entry_price":   self.entry_price,
            "exhaustion_bars": 0,
            "in_chop":       not self.is_trending,
            "trend_bars":    self._hold_bar_count if self.in_position else 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# NEW-PAIR SNIPER SCANNER
# ─────────────────────────────────────────────────────────────────────────────

class NewPairSniperScanner:
    """
    Passive scanner that monitors PumpPortal's new token stream and evaluates
    each new pair for snipe-worthiness using a lightweight quality score.

    This is separate from the StrategyEngineV2 (which runs per-coin).
    The scanner is used by main.py's sniper WebSocket endpoint to:
      1. Receive new token events from PumpPortal
      2. Score each new token
      3. Return high-quality tokens with a recommended action

    Scoring model (0-100):
      +20  Deployed with  > 1 SOL initial liquidity
      +20  Has social links (Twitter / Telegram / website)
      +15  Name/symbol is not generic/random (heuristic)
      +15  Launch time is recent (< 30 seconds old)
      +15  Initial buy volume is significant (> 0.5 SOL)
      +15  Token supply is reasonable (not too concentrated)

    Threshold for "snipeable": score >= snipe_min_score
    """

    def __init__(
        self,
        snipe_min_score: float = 55.0,
        min_initial_sol: float = 1.0,
        max_mcap_entry_usd: float = 8000.0,
        require_socials: bool = False,
        min_initial_buy_sol: float = 0.3,
    ):
        self.snipe_min_score     = snipe_min_score
        self.min_initial_sol     = min_initial_sol
        self.max_mcap_entry_usd  = max_mcap_entry_usd
        self.require_socials     = require_socials
        self.min_initial_buy_sol = min_initial_buy_sol
        self.evaluated: dict[str, dict] = {}  # mint -> evaluation result

    def score_new_pair(self, token_event: dict) -> dict:
        """
        Score a new token event from PumpPortal's subscribeNewToken feed.

        Verified real field mapping from live PumpPortal data:
          solAmount           = SOL spent on initial buy (already in SOL, e.g. 2.5)
          initialBuy          = tokens received (raw token units, NOT lamports/SOL)
          vSolInBondingCurve  = virtual SOL reserves (already in SOL, ~30 at launch)
          marketCapSol        = market cap in SOL      (already in SOL, ~30-33 at launch)
        """
        import time as _time
        score    = 0.0
        reasons  = []
        failures = []

        mint      = token_event.get("mint", "")
        name      = token_event.get("name", "") or ""
        symbol    = token_event.get("symbol", "") or ""
        twitter   = token_event.get("twitter", "") or ""
        telegram  = token_event.get("telegram", "") or ""
        website   = token_event.get("website", "") or ""

        # solAmount = SOL spent by creator on initial buy (already in SOL)
        initial_buy_sol  = float(token_event.get("solAmount", 0) or 0)
        # vSolInBondingCurve is already in SOL (NOT lamports) — real: ~30-33 at launch
        v_sol_reserves   = float(token_event.get("vSolInBondingCurve", 0) or 0)
        market_cap_sol   = float(token_event.get("marketCapSol", 0) or 0)
        created_timestamp = float(token_event.get("timestamp", 0) or 0)
        age_seconds = _time.time() - created_timestamp if created_timestamp > 0 else 999

        # ── Gate: mcap must be in snipe window ───────────────────────────────
        # (market_cap_sol * ~150 USD/SOL ≈ USD mcap at ~3-8k range)
        # Most new tokens launch at mcap < 15k SOL value.
        # We use raw market_cap_sol and allow the caller to filter.

        # ── Scoring ──────────────────────────────────────────────────────────

        # 1. Virtual SOL reserves (liquidity depth — real launch: ~30 SOL in curve)
        if v_sol_reserves >= self.min_initial_sol:
            score += 20
            reasons.append(f"Good liquidity ({v_sol_reserves:.1f} SOL in curve)")
        elif v_sol_reserves >= self.min_initial_sol * 0.3:
            score += 8
            failures.append(f"Low liquidity ({v_sol_reserves:.2f} SOL)")
        else:
            failures.append(f"No liquidity ({v_sol_reserves:.3f} SOL)")

        # 2. Social presence — bonus for multi-channel presence
        has_socials = bool(twitter or telegram or website)
        social_count = sum([bool(twitter), bool(telegram), bool(website)])
        if has_socials:
            score += 15
            if social_count >= 2:
                score += 10   # multi-channel = stronger legitimacy signal
            links = []
            if twitter: links.append("Twitter")
            if telegram: links.append("Telegram")
            if website: links.append("Website")
            reasons.append(f"Socials ({social_count}): {', '.join(links)}")
        else:
            if self.require_socials:
                failures.append("No socials (required)")
            else:
                failures.append("No socials")

        # 3. Name quality (not random/generic)
        name_score = self._score_name(name, symbol)
        score += name_score
        if name_score >= 10:
            reasons.append(f"Quality name/symbol ({name} / {symbol})")
        else:
            failures.append(f"Generic/random name ({name} / {symbol})")

        # 4. Launch recency (fresh coins are more volatile = better for sniping)
        if age_seconds < 15:
            score += 15
            reasons.append(f"Very fresh launch ({age_seconds:.0f}s old)")
        elif age_seconds < 60:
            score += 10
            reasons.append(f"Recent launch ({age_seconds:.0f}s old)")
        elif age_seconds < 300:
            score += 5
        else:
            failures.append(f"Stale launch ({age_seconds:.0f}s ago)")

        # 5. Initial buy size (solAmount = SOL the creator/sniper put in)
        #    Zero buy = creator deployed with no skin in the game → hard skip
        #    Real data: 2.5 SOL = strong conviction. Our min: 0.3 SOL.
        if initial_buy_sol == 0:
            # Hard disqualifier — no initial buy means no demand signal at all
            score -= 20
            failures.append("Zero initial buy — no demand signal")
        elif initial_buy_sol >= 1.0:
            score += 20   # strong conviction
            reasons.append(f"Big buy ({initial_buy_sol:.2f} SOL)")
        elif initial_buy_sol >= self.min_initial_buy_sol:
            score += 12
            reasons.append(f"Decent buy ({initial_buy_sol:.2f} SOL)")
        elif initial_buy_sol >= self.min_initial_buy_sol * 0.4:
            score += 5
            failures.append(f"Small buy ({initial_buy_sol:.3f} SOL)")
        else:
            failures.append(f"Micro buy ({initial_buy_sol:.4f} SOL)")

        # 6. Market cap window — target: 3k-9k USD ≈ 20-60 SOL at ~$150/SOL
        #    Real launch: marketCapSol ~30-33 SOL → perfect snipe window
        if 20 <= market_cap_sol <= 60:
            score += 15
            reasons.append(f"Ideal mcap ({market_cap_sol:.0f} SOL ≈ ${market_cap_sol*150:.0f})")
        elif market_cap_sol < 20:
            score += 8   # very early, still viable
            reasons.append(f"Early entry ({market_cap_sol:.1f} SOL)")
        elif market_cap_sol <= 120:
            score += 5   # slightly late but watchable
            failures.append(f"Late mcap ({market_cap_sol:.0f} SOL)")
        else:
            failures.append(f"Mcap too high ({market_cap_sol:.0f} SOL)")

        snipeable = score >= self.snipe_min_score

        # Hard gates — no exceptions regardless of score
        if self.require_socials and not has_socials:
            snipeable = False
        if market_cap_sol > 120:          # > ~$18k USD — too late to enter
            snipeable = False
        if initial_buy_sol == 0:          # no demand signal — skip regardless
            snipeable = False
        if v_sol_reserves < 0.5:          # ghost curve — no real liquidity
            snipeable = False

        result = {
            "mint":            mint,
            "name":            name,
            "symbol":          symbol,
            "score":           round(score, 1),
            "snipeable":       snipeable,
            "reasons":         reasons,
            "failures":        failures,
            "initial_buy_sol": round(initial_buy_sol, 4),
            "market_cap_sol":  round(market_cap_sol, 2),
            "v_sol_reserves":  round(v_sol_reserves, 2),
            "age_seconds":     round(age_seconds, 1),
            "has_socials":     has_socials,
            "twitter":         twitter,
            "telegram":        telegram,
            "website":         website,
        }
        self.evaluated[mint] = result
        return result

    def _score_name(self, name: str, symbol: str) -> float:
        """
        Heuristic name quality score (0 = random, 15 = deliberate branding).
        Totally random mints (pump.fun often has gibberish names) score 0.
        """
        if not name or not symbol:
            return 0.0

        # Very short names are often placeholders
        if len(name) < 3 or len(symbol) < 2:
            return 3.0

        # Names that are all random hex/numbers are likely bot-created
        import re
        alphanumeric_only = re.sub(r'[^a-zA-Z0-9]', '', name)
        has_vowels = bool(re.search(r'[aeiouAEIOU]', alphanumeric_only))
        if not has_vowels:
            return 3.0  # no vowels = likely random hash

        # Meme coin quality heuristics
        score = 8.0

        # Deliberate capitalization or camelCase = human-crafted
        if name != name.lower() and name != name.upper():
            score += 4.0

        # Known meme keywords (doge, pepe, moon, inu, cat, etc.) = higher virality
        meme_keywords = {
            "doge", "pepe", "moon", "inu", "cat", "dog", "frog", "nft",
            "ai", "gpt", "elon", "trump", "wojak", "chad", "based",
            "pump", "launch", "meme", "token", "coin", "gem", "bull",
            "bear", "sol", "solana", "bonk", "mog", "gigachad", "sigma",
        }
        name_lower = name.lower()
        if any(kw in name_lower for kw in meme_keywords):
            score += 3.0

        return min(score, 15.0)
