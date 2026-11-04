"""
Strategy Engine V6 — Insider-Dump Absorption Entry (build prompt
notes/strategy_prompts/V6_dev_sell_absorption.md, re-specified per the
Step Zero finding).

    ── Thesis ──
    A verified-insider dump is a COMPLETION event, not just a warning:
    it removes the largest known overhang.  Conditional on the dump being
    ABSORBED — price holds within a band of the event price, no sell
    cascade follows, then price RECLAIMS the event level — the remaining
    holder base held through the worst single-print fear event, and the
    next-10-minute drift is positive.  Absorption is the discriminator:
    cascades are dead, absorbed dumps are clean.

    ── Step Zero re-specification (measured, ) ──
    The prompt assumed the `dev` holder_flow class has full coverage.  The
    DB audit found ZERO `tag='dev'` SELL events in the entire history
    (dev appears only on 22 buys).  V6 keys on the verified-insider SELL
    classes that actually exist — the engine's own `_DEV_TAGS` set
    (dev / sniper / bundler / rat_trader), of which `bundler` is the
    dominant sell class (5,555 events, full coverage both eras via the
    GMGN registry tags — never coverage-limited the way anonymous whale
    classification was pre-).  Anonymous `whale` sells are NOT a
    qualifying class (6.7% coverage artifact pre-08-29).

    Raw forward-drift screen on real events (pre-registration):
    A-hold entries: mean +0.42% OLD / +1.08% DEAD (WR 17.5/22.7% under a
    +25% TP / −2% scratch / 600 s proxy) vs cascade-class −9% — the
    absorption conditional carries genuine both-era edge, STRONGER in the
    dead regime.

State machine (per recording, event-driven):

    IDLE → (qualifying insider sell ≥ $min_usd) → ABSORB
        window [t0, t0+window_s]: A = price ≥ p0×(1−dd); B = post-event
        sell_volume ≤ mult × event size (SOL).  A second qualifying sell
        while ABSORB → RESET (new event).  A/B breach → DEAD.
    ABSORB (held) → first state with p ≥ p0×(1+reclaim) → BUY
    IN → exits: absorb_tp / dev_resell_exit / absorb_scratch / absorb_time_stop
        (a second qualifying insider sell while IN → immediate full exit)

Interface parity — the V1 contract every pipeline already speaks (mirror
StrategyEngineV3Adapter), PLUS the holder-flow methods with V2-compatible
semantics.  Parity-neutral on recordings with no qualifying events: zero
entries, zero state.  Event timestamps are compared against state
timestamps only (no lookahead); delivery timing mirrors the ForwardTester's
existing replay exactly (events land at their recorded on-chain timestamps
against the candle stream, exactly as V2's dev_sell_exit observes them).
"""

from __future__ import annotations

import bisect
import math
from typing import Optional

from strategy_engine import Regime as _V1Regime, \
                            Signal as _V1Signal, \
                            Direction as _V1Direction
from core.config import family_defaults
from engines.adapter_base import AdapterBase

# Every knob default lives in core.config; this dict preserves the
# exact keys, values and order the rest of the system consumes.
DEFAULT_CONFIG = family_defaults("v6")

# Verified-insider sell classes (the V2 engine's own _DEV_TAGS set).
_INSIDER_TAGS = frozenset({"dev", "sniper", "bundler", "rat_trader"})

# Machine states
S_IDLE   = 0
S_ABSORB = 1   # window open — conditions A/B being monitored
S_DEAD   = 2   # cascade: event dead, machine waits for the next event
S_READY  = 3   # window HELD — waiting for the reclaim
S_IN     = 4   # in position


def _merge_config(user: Optional[dict]) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    for k, v in (user or {}).items():
        if v is None:
            continue
        cfg[k] = v
    return cfg


def _event_fields(ev: dict) -> Optional[tuple[int, float]]:
    """(time, usd) for a qualifying insider sell; None otherwise."""
    if not isinstance(ev, dict):
        return None
    side = str(ev.get("side", "")).lower()
    if side != "sell":
        return None
    if str(ev.get("tag", "")).lower() not in _INSIDER_TAGS:
        return None
    try:
        usd = float(ev.get("amount_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if usd < 1e-12:
        return None
    return int(ev.get("time", 0)), usd


class StrategyEngineV6Adapter(AdapterBase):
    """
    V1-surface engine: insider-dump absorption, event-driven, long-only.

    All decisions key off (a) the holder_flow event stream at its recorded
    timestamps and (b) tape observables — never wall-clock, never pipeline
    context — so Backtester / ForwardTester / LiveTrader evolve identical
    state (AGENTS.md #1).  Parity-neutral with no qualifying events.
    """

    def __init__(self, **engine_kwargs):
        cfg = _merge_config(engine_kwargs)
        self.cfg = cfg

        # V1-compat echo surface.
        self._init_v1_compat(cfg, 2.0, float(cfg["v6_tp_pct"]))

        self._events: list[dict] = []
        self._event_times: list[int] = []

        self.state = S_IDLE
        self._event_t0: int = 0            # current event's timestamp
        self._event_p0: float = 0.0        # price at event delivery
        self._event_sol: float = 0.0       # event size in SOL (if usable)
        self._post_sell_vol: float = 0.0   # cumulative post-event sell vol
        self._window_low: float = 0.0      # min price inside the window
        self._trade_open_time: Optional[int] = None
        self._delivered_idx: int = 0       # events consumed so far (by time)
        self.eligibility = {
            "qualifying_events": 0, "absorb_holds": 0, "entries": 0,
        }

        # V1 surface state.
        self._init_v1_state()

        # V1 indicator attributes (ForwardTester._capture_entry_params).
        self._init_v1_indicators()

    # Holder-flow surface (pipeline parity with the V2/V3 adapters)


    def set_holder_flow_events(self, events: list[dict]):
        """Load all events for this recording (backtest path)."""
        self._events = sorted(
            (e for e in (events or []) if _event_fields(e) is not None),
            key=lambda e: int(e.get("time", 0)))
        self._event_times = [int(e.get("time", 0)) for e in self._events]
        self._delivered_idx = 0

    def append_holder_flow_events(self, events: list[dict]):
        """Live path: append newly discovered events (mirrors V2 adapter).

        Delivery is monotone in STATE time (never re-delivers consumed
        events); the insertion index only positions future delivery."""
        if not events:
            return
        for e in events:
            if _event_fields(e) is None:
                continue
            t = int(e.get("time", 0))
            if t in self._event_times:
                continue   # dedupe (live pump may re-send)
            idx = bisect.bisect_left(self._event_times, t)
            self._events.insert(idx, e)
            self._event_times.insert(idx, t)
            if idx < self._delivered_idx:
                # A late-discovered event whose timestamp already passed:
                # it was NOT delivered at its recorded time, so deliver it
                # on the next state (this is the live discovery-lag path —
                # matches how V2's dev_sell_exit sees late events).
                pass

    def _price_of_event(self) -> float:
        """State price at event delivery = the engine's current price
        (p_hat holds the current state's close — matching how V2's
        dev_sell_exit observes events)."""
        return self.p_hat if self.p_hat and self.p_hat > 0 else 0.0

    def _passes_engine_version_check(self):
        return 6

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
        # After a position closes the machine returns to watching for the
        # NEXT qualifying event (delivered events before now stay consumed).
        if self.state == S_IN:
            self.state = S_IDLE

    def _effective_stoploss_pct(self) -> float:
        return -abs(float(self.cfg["v6_scratch_tol"]))

    def _effective_takeprofit_pct(self) -> float:
        return abs(float(self.cfg["v6_tp_pct"]))

    def _compute_trail_stop_price(self):
        if not self.in_position:
            return None
        return self.entry_price * (1.0 - float(self.cfg["v6_scratch_tol"]) / 100.0)

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
        if self.ema_fast_val and self.ema_slow_val:
            self.m_hat = (self.ema_fast_val - self.ema_slow_val) / self.ema_slow_val * 100.0
        if c > 0:
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

        # Absorption quality exposed as trend_confidence (never gates
        # anything inside V6 — capture-dict surface only).
        if self.state in (S_ABSORB, S_READY, S_IN) and self._event_p0 > 0:
            hold_frac = max(0.0, min(1.0, 1.0 - self._window_low / self._event_p0
                                     / max(self.cfg["v6_absorb_dd"], 1e-9)))
            self.trend_confidence = 0.5 + 0.5 * hold_frac
        else:
            self.trend_confidence = 0.0
        self.is_trending = False
        self._pre_entry_stable = self._pre_entry_stable_up = False
        self._in_local_chop = False

    def _deliver_events(self, time: int) -> bool:
        """Consume qualifying events whose timestamp ≤ this state's time.

        Returns True when a NEW qualifying event was delivered on this
        state (the caller fires exits / lets the window logic re-arm).
        Delivery is monotone in state time; events are consumed once."""
        newly = False
        while (self._delivered_idx < len(self._event_times)
               and self._event_times[self._delivered_idx] <= time):
            ev = self._events[self._delivered_idx]
            self._delivered_idx += 1
            fields = _event_fields(ev)
            if fields is None:
                continue
            t0, usd = fields
            if usd < float(self.cfg["v6_min_event_usd"]):
                continue
            newly = True
            # Re-arm on every delivered qualifying event: while ABSORB the
            # second sell RESETS the window (spec); while IN it fires the
            # resell exit (handled by caller); while IDLE/DEAD/READY it
            # starts a fresh window at THIS event.
            self._event_t0 = t0
            self._event_p0 = self._price_of_event()
            self._event_sol = 0.0
            sol = ev.get("amount_sol")
            try:
                if sol is not None and float(sol) > 0:
                    self._event_sol = float(sol)
            except (TypeError, ValueError):
                pass
            self._post_sell_vol = 0.0
            self._window_low = self._event_p0
            if self.state != S_IN:
                self.state = S_ABSORB
                self.eligibility["qualifying_events"] += 1
        return newly

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

        _prev_close = self._prev_close
        self._maintain_v1_indicators(o, h, l, c)

        # Post-event sell volume accrues on volume-carrying ticks (state 4)
        # after the window opened — condition B's cascade measure.
        if sell_volume > 0.0 and self.state == S_ABSORB and self._event_t0:
            self._post_sell_vol += float(sell_volume)

        # Event delivery at its recorded timestamp (no lookahead: only
        # events with time ≤ this state's time are visible).
        new_event = self._deliver_events(int(time))

        self.prev_direction = self.direction
        if _prev_close and c > _prev_close:
            self.direction = _V1Direction.UP
        elif _prev_close and c < _prev_close:
            self.direction = _V1Direction.DOWN
        else:
            self.direction = _V1Direction.NONE

        v1_signal: _V1Signal = _V1Signal.NONE

        if self.in_position:
            if new_event:
                v1_signal = _V1Signal.EXIT
                self.exit_signal_reason = "dev_resell_exit"
                # The delivered event armed S_ABSORB; the exit overrides —
                # the next state continues its window (fresh event).
                if self.state == S_ABSORB:
                    self._trade_open_time = None
            if v1_signal != _V1Signal.EXIT:
                # Mechanical exits, priority order

                self.regime = _V1Regime.CONTINUATION
                entry = self.entry_price
                trade_age = int(time) - (self._trade_open_time or int(time))
                if entry > 0 and c > 0:
                    tp = float(cfg["v6_tp_pct"])
                    if tp > 0 and c >= entry * (1.0 + tp / 100.0):
                        v1_signal = _V1Signal.EXIT
                        self.exit_signal_reason = "absorb_tp"
                    elif (trade_age >= int(cfg["v6_scratch_grace_s"])
                            and c <= entry * (1.0 - float(cfg["v6_scratch_tol"]) / 100.0)):
                        v1_signal = _V1Signal.EXIT
                        self.exit_signal_reason = "absorb_scratch"
                    elif trade_age > int(cfg["v6_max_hold_s"]):
                        v1_signal = _V1Signal.EXIT
                        self.exit_signal_reason = "absorb_time_stop"
                if v1_signal != _V1Signal.EXIT:
                    self._update_peak_price(h, l)

        elif not self.in_position:
            if self.state == S_ABSORB and self._event_t0:
                self.regime = _V1Regime.EXHAUSTION
                if c > 0 and (self._window_low <= 0 or c < self._window_low):
                    self._window_low = c
                # Condition A: no follow-through breakdown.
                if (self._event_p0 > 0
                        and self._window_low < self._event_p0
                        * (1.0 - float(cfg["v6_absorb_dd"]))):
                    self.state = S_DEAD      # cascade — event dead
                else:
                    # Condition B (only when the event carries usable size):
                    # post-event sell volume ≤ mult × event size in SOL.
                    b_ok = True
                    if self._event_sol > 0:
                        b_ok = (self._post_sell_vol
                                <= float(cfg["v6_post_sell_mult"]) * self._event_sol)
                    # Window elapsed?
                    if int(time) - self._event_t0 >= int(cfg["v6_absorb_window_s"]):
                        if b_ok:
                            self.state = S_READY    # held — wait for reclaim
                            self.eligibility["absorb_holds"] += 1
                        else:
                            self.state = S_DEAD

            elif self.state == S_READY:
                self.regime = _V1Regime.TREND
                # Entry: first state at/after the held window where price
                # reclaims the event level.
                if (self._event_p0 > 0
                        and c >= self._event_p0
                        * (1.0 + float(cfg["v6_reclaim_pct"]) / 100.0)):
                    v1_signal = _V1Signal.BUY
                    self.exit_signal_reason = ""
                    self.eligibility["entries"] += 1
                    self.state = S_IN
                # A new event delivered while READY re-arms via delivery
                # (S_ABSORB) — covered in _deliver_events.

            elif self.state in (S_IDLE, S_DEAD):
                self.regime = _V1Regime.IDLE

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
        full["v6_state"] = int(self.state)
        full["v6_event_t0"] = self._event_t0
        full["v6_event_p0"] = self._event_p0
        full["volume_profiles"] = []
        full["in_position"] = self.in_position
        full["entry_price"] = self.entry_price
        full["peak_price"] = self._peak_price
        full["trail_stop_price"] = self._compute_trail_stop_price()
        full["exhaustion_bars"] = self.exhaustion_bar_count
        full["in_chop"] = False
        full["trend_bars"] = self.trend_bar_count
        return full
