"""
Strategy Engine V4 — Post-Nuke Reversion (iter77, build prompt
notes/strategy_prompts/V4_post_nuke_reversion.md).

ENTRY FAMILY never tested in this program: buy the CONFIRMED BOUNCE after
a 40–50% flush from a fresh session high — liquidity-cascade exhaustion
harvest, structurally disjoint from V2's momentum/Bayesian-escape entry.

    ── Video-evidence thesis ──
    The crowd psychology on memecoins is a reversion machine: at mid-range
    market caps a 10–30% dip attracts no buyers, but the 40–50% retrace
    from a fresh ATH is where "every single person is waiting" — and on
    clean tokens it gets bought back ~45%.  The bounce confirmation is the
    load-bearing condition: entering on the falling knife is the
    documented failure mode AND the repo's own iter07/iter37 lesson.

State machine (per recording, 4-state intra-candle ticks):

    WARMUP → (arm: dd_min reached, peak ≥ mcap floor, age ≥ floor)
          → WAIT_BOUNCE  (Mode A) : watch local low L, trigger on
                                     D ≤ dd_min − bounce_depth AND
                                     trailing flow ratio ≥ floor
          → WAIT_FLAT    (Mode B)  : consolidation that mysteriously holds
                                     (range box + no new lows)
          → IN → mechanical exits: nuke_tp / jeet_scratch / nuke_time_stop

Graveyard constraints (binding, from the build prompt):
    iter07  hard SL caps truncate drawdown-and-rebound winners → V4's only
            loss cut is the SHALLOW jeet scratch, never a deep hard SL.
    iter10  entry cooldowns block profitable re-entries → NO cooldown; cap
            entries per recording instead (v4_max_entries).
    iter37  exit-on-submersion + re-entry churn → V4 is entry-side; exits
            are short and mechanical.
    iter55  long-horizon stagnation timeouts → V4's time stop is a short
            trade-lifecycle cap (minutes), part of the entry family's
            definition, swept not assumed.

Interface parity — the V1 contract every pipeline already speaks (mirror
StrategyEngineV3Adapter):
    update(time, o, h, l, c, volume, buy_volume, sell_volume, pool_sol,
           market_cap_usd, _build_full_result) → dict
    notify_trade_opened(entry_price, direction) / notify_trade_closed()
    set_holder_flow_events / append_holder_flow_events (no-op surface)
plus every indicator attribute ForwardTester._capture_entry_params reads.
The 4-state expansion feeds update() identically in all three pipelines;
V4 evaluates its mechanical exits on EVERY tick so intra-candle extremes
are honoured exactly like V2's tp_v2 semantics.
"""

from __future__ import annotations

import math
from typing import Optional

from strategy_engine import Regime as _V1Regime, \
                            Signal as _V1Signal, \
                            Direction as _V1Direction

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    # ── Warmup / arming ────────────────────────────────────────────────
    "v4_warmup_s":        120,    # let the session peak form before arming
    "v4_min_age_s":       300,    # session age floor at arm time
    "v4_min_peak_mcap":   20000,  # the flush must come off a real peak ($)

    # ── The drawdown band ───────────────────────────────────────────────
    "v4_dd_min":          0.40,   # primary cell: 40% drawdown from peak
    "v4_dd_max":          1.00,   # disarm beyond this (dead-coin floor; the
                                   # floor-bid cell sets 0.70–0.90 instead)

    # ── Mode A — nuke-dip bounce (default) ─────────────────────────────
    "v4_bounce_depth":    0.03,   # bounce ≥ 3% off the local low, i.e.
                                   # trigger when D ≤ dd_min − depth
    "v4_flow_window_states": 10,  # trailing 4-state ticks for the flow gate
    "v4_flow_ratio":      0.90,   # Σbuy/Σsell ≥ 0.9 over the window

    # ── Mode B — consolidation survival ────────────────────────────────
    "v4_flat_window_s":   240,    # trailing window for the range box
    "v4_flat_range_pct":  0.15,   # (max−min)/min of the box
    "v4_flat_no_new_low_s": 120,  # no new low below post-flush low for this

    # ── Mode select ────────────────────────────────────────────────────
    "v4_mode":            "bounce",

    # ── Exits (mechanical; jeet rule) ──────────────────────────────────
    "v4_tp_pct":          45.0,   # nuke_tp at mcap ≥ peak-mcap basis
    "v4_jeet_tol":        2.0,    # scratch when ≤ entry×(1−2/100)
    "v4_jeet_grace_s":    20,     # ... only after this trade age
    "v4_max_hold_s":      900,    # nuke_time_stop

    # ── Entry cap (no cooldown — iter10) ───────────────────────────────
    "v4_max_entries":     3,
    "v4_two_stage":       0.0,    # default off

    # ── V1-compat echo knobs (pipeline capture reads them) ─────────────
    "confidence_high":         0.79,
    "confidence_low":          0.19,
    "entry_confidence_high":   0.79,
    "entry_confidence_low":    0.19,
    "confidence_very_high":    0.86,
    "warmup_bars":             120,
}

_V4_MODES = ("bounce", "flat")


def _merge_config(user: Optional[dict]) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    for k, v in (user or {}).items():
        if v is None:
            continue
        cfg[k] = v
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Machine states
# ─────────────────────────────────────────────────────────────────────────────

S_WARMUP      = 0   # peak forming, nothing armed yet
S_WAIT_BOUNCE = 1   # armed; watching the local low + flow confirmation
S_WAIT_FLAT   = 2   # armed; watching the consolidation box
S_IN          = 3   # in position; mechanical exits only
S_DONE        = 4   # drawdown escaped the band — machine retired
# NOTE: after a full exit the machine RE-ARMS (no cooldown — iter10); only
# the drawdown-escape (dd > dd_max) permanently ends it.  Re-arm goes to
# the mode's wait state with the peak/low memory retained.


class StrategyEngineV4Adapter:
    """
    V1-surface engine: post-nuke reversion, long-only, mechanical exits.

    All decisions key off tape observables ONLY (session mcap peak,
    drawdown band, local low, trailing flow windows, trade age) — never
    wall-clock, never pipeline context — so Backtester / ForwardTester /
    LiveTrader evolve identical state (AGENTS.md #1).
    """

    # ── Constructor ───────────────────────────────────────────────────

    def __init__(self, **engine_kwargs):
        cfg = _merge_config(engine_kwargs)
        if str(cfg["v4_mode"]) not in _V4_MODES:
            raise ValueError(f"v4_mode must be one of {_V4_MODES}")
        self.cfg = cfg

        # V1-compat echo surface (capture dicts read these; values are
        # cosmetic for V4 — the machine's gates are all v4_* above).
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
        self.stoploss_pct = 2.0
        self.takeprofit_pct = float(cfg["v4_tp_pct"])
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

        # ── Machine state ──────────────────────────────────────────────
        self.state = S_WARMUP
        self._start_time: Optional[int] = None
        self._peak_mcap: float = 0.0
        self._local_low: float = 0.0          # since arming (Mode A)
        self._flush_low: float = 0.0          # post-flush low (Mode B)
        self._flatbox: list[tuple[int, float]] = []   # (time, mcap) window
        self._flow_window: list[tuple[float, float]] = []  # (buy, sell)/tick
        self._entries_used: int = 0
        self._trade_open_time: Optional[int] = None
        self.eligibility = {                  # surfaced for the analysis
            "armed": False, "mode_fired": False,
        }

        # V1 surface state.
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

        # V1 indicator attributes (ForwardTester._capture_entry_params).
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

        # Market state (state-4 ticks carry the real values).
        self._market_cap_usd: float = 0.0
        self._pool_sol: float = 0.0

    # ── Holder-flow surface (parity no-op — V4 consumes only the tape) ─

    def set_holder_flow_events(self, events: list[dict]):
        pass

    def append_holder_flow_events(self, events: list[dict]):
        pass

    # ── V1 surface helpers ────────────────────────────────────────────

    def _passes_engine_version_check(self):
        return 4

    def notify_trade_opened(self, entry_price: float, direction: _V1Direction):
        self.in_position = True
        self.entry_price = float(entry_price)
        self.position_direction = direction
        self._peak_price = float(entry_price)
        self.entry_bar_count = self.bar_count
        self._trade_open_time = self._current_time
        self.exit_signal_reason = ""

    def notify_trade_closed(self):
        self.in_position = False
        self.entry_price = 0.0
        self.position_direction = _V1Direction.NONE
        self._peak_price = 0.0
        self._trade_open_time = None
        # Re-arm to the mode's wait state (no cooldown — iter10); the
        # machine ends permanently only via drawdown escape.
        if self.state == S_IN:
            self.state = (S_WAIT_BOUNCE if self.cfg["v4_mode"] == "bounce"
                          else S_WAIT_FLAT)
            self._local_low = 0.0

    def _update_peak_price(self, h: float, l: float):
        if self.in_position and h > self._peak_price:
            self._peak_price = h

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

    def _effective_stoploss_pct(self) -> float:
        return -abs(float(self.cfg["v4_jeet_tol"]))

    def _effective_takeprofit_pct(self) -> float:
        return abs(float(self.cfg["v4_tp_pct"]))

    def _global_stoploss_pct(self) -> float:
        return self._effective_stoploss_pct()

    def _compute_trail_stop_price(self):
        if not self.in_position:
            return None
        return self.entry_price * (1.0 - abs(float(self.cfg["v4_jeet_tol"])) / 100.0)

    # ── Indicator maintenance (V1 attrs — the same projection the V2/V3
    #    adapters keep so chart + capture dicts stay meaningful) ────────

    def _maintain_v1_indicators(self, o: float, h: float, l: float, c: float):
        if self._prev_close is None:
            self.ema_fast_val = c
            self.ema_slow_val = c
            self.ema_macro_val = c
            self.atr_val = max(h - l, 1e-12)
        else:
            a_fast = 2.0 / (self.ema_fast_p + 1)
            a_slow = 2.0 / (self.ema_slow_p + 1)
            self.ema_fast_val += a_fast * (c - self.ema_fast_val)
            self.ema_slow_val += a_slow * (c - self.ema_slow_val)
            self.ema_macro_val += a_slow * (c - self.ema_macro_val)
            prev_c = self._prev_close
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            atr_alpha = 2.0 / (self.atr_period + 1)
            self.atr_val += atr_alpha * (tr - self.atr_val)

        self._prev_close = c
        self.prev_ema_spread = self.ema_spread
        self.ema_spread = self.ema_fast_val - self.ema_slow_val
        self.spread_expanding = abs(self.ema_spread) > abs(self.prev_ema_spread)

        self.prev_m_hat = self.m_hat
        if c > 0:
            if self.ema_fast_val and self.ema_slow_val:
                self.m_hat = (self.ema_fast_val - self.ema_slow_val) / self.ema_slow_val * 100.0
            self.p_hat = c
        self.momentum_acceleration = self.m_hat - self.prev_m_hat
        if abs(self.m_hat) < abs(self.prev_m_hat):
            self._momentum_peak_declining_count += 1
        else:
            self._momentum_peak_declining_count = 0
        self._momentum_past_peak_flag = (
            self._momentum_peak_declining_count >= self.momentum_peak_bars
        )
        self._price_overextended_flag = self._price_overextended(c)

        if self.atr_val and self.atr_val > 0 and c > 0:
            m_hat_pct = (self.m_hat / c) * 100 * self.roc_period
            atr_pct = (self.atr_val / c) * 100
            self.signal_strength = (abs(m_hat_pct) / atr_pct) if atr_pct > 0 else 0.0
        else:
            self.signal_strength = 0.0
        self.s_effective = self.signal_strength

        # V4's own gate quality — how "resolved" the bounce geometry is —
        # exposed to the capture dict as trend_confidence (0..1, never
        # gates anything inside V4; the machine's gates are all v4_*).
        dd = self._drawdown()
        if dd is not None:
            depth_into_band = max(0.0, min(1.0, (dd - self.cfg["v4_dd_min"]) / 0.4))
            bounce_frac = max(0.0, min(1.0, self._bounce_frac()))
            self.trend_confidence = 0.5 * (1.0 - depth_into_band) + 0.5 * bounce_frac
        self.is_trending = False
        self._pre_entry_stable = self._pre_entry_stable_up = self.is_trending
        self._in_local_chop = False

    # ── Tape observables ──────────────────────────────────────────────

    def _mcap(self) -> float:
        """Session market cap — market_cap_usd when the tape carries it,
        else the price proxy (c × 1e9).  Both are proportional to price
        at constant supply, so drawdown math is equivalent."""
        if self._market_cap_usd > 0:
            return self._market_cap_usd
        if self.p_hat and self.p_hat > 0:
            return self.p_hat * 1e9
        return 0.0

    def _drawdown(self) -> Optional[float]:
        m = self._mcap()
        if m <= 0 or self._peak_mcap <= 0:
            return None
        return 1.0 - m / self._peak_mcap

    def _bounce_frac(self) -> float:
        """Bounce off the local low as a fraction (0..1 of the flush)."""
        if self._local_low <= 0:
            return 0.0
        m = self._mcap()
        if m <= self._local_low:
            return 0.0
        return (m - self._local_low) / self._local_low

    def _trailing_flow_ratio(self) -> Optional[float]:
        """Σbuy/Σsell over the trailing v4_flow_window_states ticks with
        volume (state-4 ticks; volume-less states contribute nothing)."""
        n = int(self.cfg["v4_flow_window_states"])
        w = self._flow_window[-n:] if n > 0 else []
        if not w:
            return None
        b = sum(x[0] for x in w)
        s = sum(x[1] for x in w)
        if s <= 0:
            # one-sided buy window: ratio infinite → gated by ≥ floor pass
            return math.inf if b > 0 else None
        return b / s

    # ── Main entry point (V1 surface) ──────────────────────────────────

    def update(
        self,
        time: int,
        o: float, h: float, l: float, c: float,
        volume: float = 0.0,
        buy_volume: float = 0.0,
        sell_volume: float = 0.0,
        pool_sol: float = 0.0,
        market_cap_usd: float = 0.0,
        _build_full_result: bool = True,
    ) -> dict:
        cfg = self.cfg
        self.bar_count += 1
        self._current_time = int(time)
        if self._start_time is None:
            self._start_time = int(time)
        if market_cap_usd > 0.0:
            self._market_cap_usd = market_cap_usd
        if pool_sol > 0.0:
            self._pool_sol = pool_sol

        _prev_close = self._prev_close
        self._maintain_v1_indicators(o, h, l, c)

        # Flow window: only volume-carrying ticks count (state 4), so the
        # window sees each candle exactly once — identical in all pipelines.
        if buy_volume + sell_volume > 0.0:
            self._flow_window.append((float(buy_volume), float(sell_volume)))
            cap = 4 * max(int(cfg["v4_flow_window_states"]),
                          int(cfg["v4_flat_window_s"]))
            if len(self._flow_window) > cap:
                self._flow_window = self._flow_window[-cap:]

        # Consolidation box (Mode B): (time, mcap) close series.
        self._flatbox.append((int(time), self._mcap()))
        cutoff = int(time) - int(cfg["v4_flat_window_s"])
        while self._flatbox and self._flatbox[0][0] < cutoff:
            self._flatbox.pop(0)

        self.prev_direction = self.direction
        if _prev_close and c > _prev_close:
            self.direction = _V1Direction.UP
        elif _prev_close and c < _prev_close:
            self.direction = _V1Direction.DOWN
        else:
            self.direction = _V1Direction.NONE

        v1_signal: _V1Signal = _V1Signal.NONE
        age_s = int(time) - self._start_time

        # ── Peak formation (WARMUP) ─────────────────────────────────────
        if age_s < int(cfg["v4_warmup_s"]):
            self._peak_mcap = max(self._peak_mcap, self._mcap())
            self.regime = _V1Regime.IDLE
        elif self.state != S_DONE and not self.in_position:
            # Session peak keeps tracking until the machine retires — the
            # drawdown is always measured against the HIGH-WATER mcap.
            self._peak_mcap = max(self._peak_mcap, self._mcap())

        # ── Machine advance (only outside positions; exits below) ──────
        if not self.in_position and self.state != S_DONE:

            if self.state == S_WARMUP:
                dd = self._drawdown()
                # Arming: D reached ≥ dd_min once, peak ≥ mcap floor, age OK.
                if (dd is not None
                        and self._peak_mcap >= float(cfg["v4_min_peak_mcap"])
                        and age_s >= int(cfg["v4_min_age_s"])
                        and dd >= float(cfg["v4_dd_min"])
                        and dd <= float(cfg["v4_dd_max"])):
                    self._local_low = self._mcap()
                    self._flush_low = self._mcap()
                    self.eligibility["armed"] = True
                    self.state = (S_WAIT_BOUNCE if cfg["v4_mode"] == "bounce"
                                  else S_WAIT_FLAT)
                    self.regime = _V1Regime.EXHAUSTION
                elif dd is not None and dd > float(cfg["v4_dd_max"]):
                    # Disarm beyond the band: pure dead coin (the floor-bid
                    # CELL narrows dd_max to 0.70–0.90 for its variance).
                    self.state = S_DONE
                    self.regime = _V1Regime.REVERSAL
                else:
                    self.regime = _V1Regime.IDLE

            elif self.state == S_WAIT_BOUNCE:
                self.regime = _V1Regime.EXHAUSTION
                dd = self._drawdown()
                if dd is None:
                    pass
                elif dd > float(cfg["v4_dd_max"]):
                    self.state = S_DONE            # dead-coin disarm
                    self.regime = _V1Regime.REVERSAL
                else:
                    m = self._mcap()
                    if m > 0 and (self._local_low <= 0 or m < self._local_low):
                        self._local_low = m
                    # Trigger: bounced ≥ depth off L (D ≤ dd_min − depth)
                    # AND trailing flow confirms absorption buying.
                    if (self._local_low > 0
                            and dd <= float(cfg["v4_dd_min"]) - float(cfg["v4_bounce_depth"])):
                        fr = self._trailing_flow_ratio()
                        if fr is not None and fr >= float(cfg["v4_flow_ratio"]):
                            if self._entries_used < int(cfg["v4_max_entries"]):
                                v1_signal = _V1Signal.BUY
                                self.exit_signal_reason = ""
                                self.eligibility["mode_fired"] = True

            elif self.state == S_WAIT_FLAT:
                self.regime = _V1Regime.EXHAUSTION
                dd = self._drawdown()
                if dd is None:
                    pass
                elif dd > float(cfg["v4_dd_max"]):
                    self.state = S_DONE
                    self.regime = _V1Regime.REVERSAL
                else:
                    m = self._mcap()
                    if m > 0 and (self._flush_low <= 0 or m < self._flush_low):
                        self._flush_low = m
                    # Trigger: trailing box holds (range ≤ pct) AND no new
                    # low below the post-flush low within the no-new-low s.
                    if len(self._flatbox) >= 2:
                        vals = [v for _, v in self._flatbox if v > 0]
                        if vals:
                            vmin, vmax = min(vals), max(vals)
                            range_ok = (vmax - vmin) / vmin <= float(cfg["v4_flat_range_pct"])
                            nnl_s = int(cfg["v4_flat_no_new_low_s"])
                            recent = [v for t, v in self._flatbox if t >= int(time) - nnl_s]
                            no_new_low = (not recent) or min(recent) >= self._flush_low * 0.999
                            window_full = (int(time) - self._flatbox[0][0]
                                          >= int(cfg["v4_flat_window_s"]) * 0.75)
                            if (range_ok and no_new_low and window_full
                                    and self._entries_used < int(cfg["v4_max_entries"])):
                                v1_signal = _V1Signal.BUY
                                self.exit_signal_reason = ""
                                self.eligibility["mode_fired"] = True

        # ── In position: mechanical exits on EVERY tick ─────────────────
        if self.in_position:
            self.regime = _V1Regime.CONTINUATION
            entry = self.entry_price
            trade_age = int(time) - (self._trade_open_time or int(time))
            # Exit thresholds compare the CURRENT CLOSE against the entry
            # FILL PRICE — one consistent scale/basis (the pipeline's fill),
            # exactly like V2's tp_v2 semantics.  (The mcap feed is a
            # different NUMERIC scale from the raw price — comparing them
            # directly would fire nuke_tp on every tick.)
            if entry > 0 and c > 0:
                tp = float(cfg["v4_tp_pct"])
                if tp > 0 and c >= entry * (1.0 + tp / 100.0):
                    v1_signal = _V1Signal.EXIT
                    self.exit_signal_reason = "nuke_tp"
                elif (trade_age >= int(cfg["v4_jeet_grace_s"])
                        and c <= entry * (1.0 - float(cfg["v4_jeet_tol"]) / 100.0)):
                    v1_signal = _V1Signal.EXIT
                    self.exit_signal_reason = "jeet_scratch"
                elif trade_age > int(cfg["v4_max_hold_s"]):
                    v1_signal = _V1Signal.EXIT
                    self.exit_signal_reason = "nuke_time_stop"
            if v1_signal != _V1Signal.EXIT:
                self._update_peak_price(h, l)

        # Entry bookkeeping: the pipelines notify via notify_trade_opened
        # when the fill lands (instant mode: this state).
        if v1_signal == _V1Signal.BUY:
            self.state = S_IN
            self._entries_used += 1

        # ── Result dict (V1 contract) ─────────────────────────────────
        minimal = {
            "time": time,
            "regime": self.regime.value,
            "direction": self.direction.value,
            "signal": v1_signal.value,
            "exit_reason": self.exit_signal_reason,
        }
        if not _build_full_result:
            return minimal

        full = dict(minimal)
        full["indicators"] = {
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
            "price_overextended": self._price_overextended(c),
            "momentum_past_peak": self._momentum_past_peak(),
        }
        full["v4_state"] = int(self.state)
        full["v4_peak_mcap"] = self._peak_mcap
        full["v4_drawdown"] = self._drawdown()
        full["v4_local_low"] = self._local_low
        full["v4_entries_used"] = self._entries_used
        full["volume_profiles"] = []
        full["in_position"] = self.in_position
        full["entry_price"] = self.entry_price
        full["peak_price"] = self._peak_price
        full["trail_stop_price"] = self._compute_trail_stop_price()
        full["exhaustion_bars"] = self.exhaustion_bar_count
        full["in_chop"] = False
        full["trend_bars"] = self.trend_bar_count
        return full
