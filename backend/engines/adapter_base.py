"""Shared V1-compat surface for the strategy engine adapters.

One copy of the fake V1 constants, the V1 surface state, and the trivial
helpers the adapters carry identically; each engine overrides only what is
genuinely different.  Helper bodies are byte-identical to the copies they
replaced: engine.update() results must not move by a single float.
"""

from __future__ import annotations

from typing import Optional

from strategy_engine import Direction as _V1Direction, \
                            Regime as _V1Regime


class AdapterBase:
    """Mixin holding the V1-compat surface shared by the engine adapters."""

    def _init_v1_compat(self, cfg: dict, stoploss_pct: float,
                        takeprofit_pct: float):
        """Assign the fake V1-compat constants the pipeline capture dicts read.

        ``stoploss_pct``/``takeprofit_pct`` are the only engine-specific
        values; every other constant is shared verbatim by V3, V4 and V6.
        """
        self.confidence_high = float(cfg["confidence_high"])
        self.confidence_low = float(cfg["confidence_low"])
        self.entry_confidence_high = float(cfg["entry_confidence_high"])
        self.entry_confidence_low = float(cfg["entry_confidence_low"])
        self.confidence_very_high = float(cfg["confidence_very_high"])
        self.confidence_w1 = 0.3
        self.confidence_w2 = 0.25
        self.confidence_w3 = 0.25
        self.confidence_w4 = 0.2
        self.ema_fast_p = 3
        self.ema_slow_p = 7
        self.atr_period = 7
        self.roc_period = 3
        self.warmup = int(cfg["warmup_bars"]) // 4
        self.stoploss_pct = stoploss_pct
        self.takeprofit_pct = takeprofit_pct
        self.takeprofit_pct_low = self.takeprofit_pct
        self.takeprofit_pct_high = self.takeprofit_pct
        self.stoploss_pct_low = self.stoploss_pct
        self.stoploss_pct_high = self.stoploss_pct
        self.max_entry_bar_count = 5700
        self.forbidden_bc_lo = 0
        self.forbidden_bc_hi = 0
        self.trail_floor_pct = 0.0
        self.reversal_exit_bars_max = 0
        self.overextension_k = 0.08
        self.momentum_peak_bars = 1
        self.S_strong = 4.0
        self.S_weak = 2.0
        self.S_noise = 1.15
        self.delta_threshold = 0.3
        self.min_trend_bars = 2
        self.ema_min_spread_pct = 0.02
        self.ema_macro_period = 7
        self.exhaustion_persist_bars = 6
        self.regime_lookback = 6
        self.persistence_threshold = 2
        self.momentum_mean_threshold = 0.0
        self.chop_atr_pct = 0.3
        self.chop_spread_pct = 0.05
        self.local_range_bars = 80
        self.local_range_threshold_pct = 10.0
        self.sign_flip_threshold = 0
        self.stability_bars = 5
        self.atr_floor_k = 0.0
        self.ema_cross_persist_bars = 2
        self.exhaustion_bars_limit = 3
        self.reversal_confirm_bars = 2
        self.reversal_exit_confirm_bars = 0
        self.s_effective_threshold = 0.5
        self.exhaustion_s_decay_bars = 1
        self.exhaustion_stall_bars = 6
        self.exhaustion_stall_atr_pct = 3.0
        self.spike_atr_multiplier = 1.2
        self.spike_lookback_bars = 9
        self.body_baseline_bars = 160
        self.consolidation_range_pct = 25.0

    def _init_v1_state(self):
        """Reset the V1 surface state the pipelines and capture dicts read."""
        self.regime: _V1Regime = _V1Regime.IDLE
        self.direction: _V1Direction = _V1Direction.NONE
        self.prev_direction: _V1Direction = _V1Direction.NONE
        self.trend_before_exhaustion: _V1Direction = _V1Direction.NONE
        self.position_direction: _V1Direction = _V1Direction.NONE
        self.in_position = False
        self.entry_price = 0.0
        self._peak_price = 0.0
        self.entry_bar_count = 0
        self.bar_count = 0
        self._current_time = 0
        self.exit_signal_reason = ""
        self.trend_bar_count = 0
        self.exhaustion_bar_count = 0
        self.exhaustion_persist_count = 0
        self.reversal_confirm_count = 0
        self.trend_reversal_confirm_count = 0
        self.reversal_bar_count = 0
        self.no_motion_count = 0
        self.trend_start_bar = 0
        self.trend_start_price = 0.0
        self.trend_start_atr = 0.0
        self._exhaustion_phase_high = 0.0
        self._exhaustion_s_decay_count = 0
        self._momentum_peak_declining_count = 0
        self._momentum_past_peak_flag = False
        self._pre_entry_stable = False
        self._pre_entry_stable_up = False
        self._pre_entry_stable_down = False
        self._in_local_chop = False
        self._ema_cross_valid = False
        self._ema_cross_persist_count = 0
        self.iter05_s_effective_min = 0.0

    def _init_v1_indicators(self):
        """Zero the V1 indicator attributes ForwardTester._capture_entry_params reads."""
        self.m_hat = 0.0
        self.prev_m_hat = 0.0
        self.p_hat = 0.0
        self.momentum_acceleration = 0.0
        self.signal_strength = 0.0
        self.s_effective = 0.0
        self.ema_fast_val: Optional[float] = None
        self.ema_slow_val: Optional[float] = None
        self.ema_macro_val: Optional[float] = None
        self.ema_spread = 0.0
        self.prev_ema_spread = 0.0
        self.spread_expanding = False
        self.atr_val: Optional[float] = None
        self.atr_floor = 0.0
        self.trend_confidence = 0.0
        self.is_trending = False
        self._price_overextended_flag = False
        self._prev_close: Optional[float] = None

    def _update_peak_price(self, h: float, l: float):
        if self.in_position and h > self._peak_price:
            self._peak_price = h

    # Scalar types a snapshot entry may hold; anything else (enums, dicts,
    # windows, the core object) is added explicitly or left to the reader.
    _SNAPSHOT_SCALARS = (bool, int, float, str)

    def snapshot(self) -> dict:
        """Full decision state as a flat dict of scalar attribute values.

        The signal journal reads this instead of re-deriving gate values;
        keys are the engine's own attribute names, values are live and
        unrounded.  Computed quantities (warmup floor, take-profit rate)
        and non-scalar state are appended by this method or by overrides.
        """
        snap: dict = {}
        for name in dir(self):
            if name.startswith("__"):
                continue
            try:
                v = getattr(self, name)
            except Exception:
                continue
            if v is None or isinstance(v, self._SNAPSHOT_SCALARS):
                snap[name] = v
        snap["regime"] = self.regime
        w = int(getattr(self, "warmup", 100) or 100)
        snap["warmup_floor"] = w * 4 if w <= 100 else w
        snap["effective_takeprofit_pct"] = self._effective_takeprofit_pct()
        return snap

    def _price_overextended(self, c: float) -> bool:
        if not self.p_hat:
            return False
        return c > self.p_hat * (1.0 + self.overextension_k)

    def _momentum_past_peak(self, c=None) -> bool:
        return self._momentum_past_peak_flag

    def _is_chop_zone(self, c: float) -> bool:
        return False

    def _confidence_lerp(self, low_val: float, high_val: float) -> float:
        lo = low_val if low_val != 0.0 else high_val
        hi = high_val if high_val != 0.0 else low_val
        if lo == hi:
            return lo
        t = min(max(self.trend_confidence, 0.0), 1.0)
        if self.confidence_high > self.confidence_low:
            frac = (t - self.confidence_low) / (self.confidence_high - self.confidence_low)
        else:
            frac = 0.0
        frac = max(0.0, min(1.0, frac))
        return low_val + (high_val - low_val) * frac

    def _global_stoploss_pct(self) -> float:
        return self._effective_stoploss_pct()
