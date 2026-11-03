"""
Strategy Engine V3 — Newborn-Coin Dump-Bottom Recovery (user spec 2026-08-30).

V2's RBPF / UKF / KDE / Kramers machinery, specialised into a strict
four-phase LIFECYCLE that models the first minutes of a pump.fun newborn:

    LAUNCH PUMP  →  snipers, bundlers and copy-bots mechanically bid the
                    bonding-curve price up from birth; the tape is dominated
                    by inorganic taker flow.
    DUMP         →  the same wallets dump their supply; price bleeds out
                    while `ell` (liquidity) keeps collapsing.
    BOTTOM       →  sell flow exhausts; the KDE posterior re-localises and
                    the barrier geometry turns symmetric-to-bullish.
    ORGANIC      →  real buyers arrive (persistent positive signed delta);
                    V2's Bayesian posterior confirms upward escape.

V3 only ever enters on the BOTTOM→ORGANIC transition and only when organic
demand is measurable — the exact trade V2's trend-following design cannot
express (V2 would have bought the LAUNCH PUMP top).

Market-cap band (user spec): buy ≈ $2k–$4k mcap, sell ≈ $7k–$8k mcap.
BOTH ends are enforced by the engine itself (backtest and live receive
market_cap_usd on every state-4 update), and by STRICT stop-loss /
take-profit levels — V3 deliberately abandons V2's posterior-only exit
stack: a newborn-coin bottom entry that fails is a dead coin, and dead
coins must be cut at a fixed price, not at a posterior flip that may never
arrive before the recording dies.

Interface parity — the V1 contract every pipeline already speaks:

    eng.update(time, o, h, l, c, volume, buy_volume, sell_volume,
               pool_sol, market_cap_usd, _build_full_result)
    eng.notify_trade_opened(entry_price, direction)
    eng.notify_trade_closed()
    eng.set_holder_flow_events(events) / append_holder_flow_events(events)

plus every indicator attribute ForwardTester._capture_entry_params and the
backtester candle loop read (m_hat, ema_fast_val, trend_confidence, ...).
The 4-state intra-candle expansion feeds update() identically in all three
pipelines; V3 tracks candle boundaries via volume-carrying state-4 ticks.

The V2 core (`MemecoinStrategyEngine`) is imported, never duplicated — the
mathematics (RBPF posterior, KDE potential, Kramers escape, Kelly utility)
is consumed as-is; V3 layers only the lifecycle state machine, the organic-
flow measurement, the mcap band, and the strict TP/SL on top.

Lifecycle semantic parity (the pipeline-parity invariant, AGENTS.md #1):
the lifecycle state machine, regime mapping, and strict exits evaluate on
the SAME per-tick cadence in Backtester / ForwardTester / LiveTrader —
they read only per-tick engine state, never wall-clock or pipeline context.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

# V1 enums — the pipeline vocabulary (Regime / Signal / Direction .value).
from strategy_engine import Regime as _V1Regime, \
                            Signal as _V1Signal, \
                            Direction as _V1Direction

# The V2 mathematical core, consumed as-is (RBPF + UKF + KDE + Kramers).
from strategy_engineV2 import (
    MemecoinStrategyEngine,
    _V2_TO_V1_REGIME,
    R_IDLE,
    R_TREND,
    R_EXHAUSTION,
    R_REVERSAL,
    _REGIME_NAMES,
)
from core.config import family_defaults
from engines.adapter_base import AdapterBase

# V2 adapter-level config the lifecycle inherits.  V2's DEFAULT_CONFIG keys
# that reach the core are forwarded verbatim (SDE rates, KDE memory, tau
# horizon sweep); lifecycle-specific keys are consumed here.
_V2_CORE_KEYS = frozenset({
    "lambda_mu", "kappa_mu", "sigma_mu",
    "eta", "sigma_h",
    "alpha", "beta", "sigma_phi",
    "theta", "sigma_ell", "zeta",
    "lambda_0", "lambda_1", "kappa_J",
    "s_0", "s_1",
    "n_particles", "n_grid", "grid_sigma_extent", "tw_window_seconds",
    "tau_min", "tau_max", "tau_step", "eps_div",
    "fee_fraction", "latency_seconds", "liquidity_cap_frac",
    "warmup_bars", "sigma_floor", "logprob_floor",
    "v2_drift_work_fraction",
    "rng_seed",
})

# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle phases — integer codes mirrored onto V1 Regime vocabulary
# ─────────────────────────────────────────────────────────────────────────────

P_LAUNCH = 0   # birth pump: inorganic sniper/bundler flow, price rips up
P_DUMP    = 1   # supply dump: persistent negative flow, liquidity drains
P_BOTTOM  = 2   # flow exhausts near the lows; posterior re-localises
P_ORGANIC = 3   # organic demand arrives: persistent positive flow, P_up builds

# The V1 Regime enum members the UI + capture dicts already understand.
# LAUNCH/DUMP/BOTTOM are all "trend"/"exhaustion"-flavoured for display; the
# true phase is exposed as `lifecycle_phase` in the result dict.
_PHASE_TO_V1_REGIME = {
    P_LAUNCH: _V1Regime.TREND,        # a launch pump IS a trend — but V3 never buys it
    P_DUMP:    _V1Regime.EXHAUSTION,
    P_BOTTOM:  _V1Regime.IDLE,        # flow gone quiet; nothing to do yet
    P_ORGANIC: _V1Regime.CONTINUATION,
}

_PHASE_NAMES = {
    P_LAUNCH: "launch",
    P_DUMP:   "dump",
    P_BOTTOM: "bottom",
    P_ORGANIC: "organic",
}


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Every knob default lives in core.config; this dict preserves the
# exact keys, values and order the rest of the system consumes.
DEFAULT_CONFIG = family_defaults("v3")


def _merge_config(user: Optional[dict]) -> dict:
    """Layer user config over V3 defaults; forward V2-core keys verbatim."""
    cfg = dict(DEFAULT_CONFIG)
    for k, v in (user or {}).items():
        if v is None:
            continue
        cfg[k] = v
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# StrategyEngineV3Adapter — the V1-surface engine
# ─────────────────────────────────────────────────────────────────────────────

class StrategyEngineV3Adapter(AdapterBase):
    """
    V1-surface wrapper around the V2 core specialised for newborn coins.

    update(time, o, h, l, c, volume, buy_volume, sell_volume, pool_sol,
           market_cap_usd, _build_full_result) → dict
        Identical call signature and result contract to V1/V2: the pipelines
        (Backtester / ForwardTester / LiveTrader) are unchanged.

    Lifecycle state machine (per tick):
        LAUNCH → DUMP → BOTTOM → ORGANIC → (entry) → strict TP/SL/band exits

    The phase machine is monotone forward with a guard: DUMP can only arm
    after a launch pump is on the tape; BOTTOM/ORGANIC can only be reached
    from DUMP (or from launch-less tapes that open directly into a dump —
    the pump gate is `launch_high ≥ open·(1+g)` which a stillborn tape never
    satisfies, in which case V3 simply never trades, which is correct: no
    organic-recovery trade exists without a dump to recover from).
    """

    # ── Constructor ───────────────────────────────────────────────────

    def __init__(self, **engine_kwargs):
        cfg = _merge_config(engine_kwargs)

        # Split the merged config: V2-core keys go to the mathematical
        # core; everything else stays lifecycle-side.  Unknown keys are
        # silently retained (mirrors the V2 adapter's tolerant filter) so a
        # mixed V2/V3 param dict can never crash a run.
        v2_cfg = {k: cfg[k] for k in _V2_CORE_KEYS if k in cfg}
        self.cfg = cfg
        self.core = MemecoinStrategyEngine(v2_cfg)

        # V1-compat knobs read by the pipeline capture dicts.
        self._init_v1_compat(cfg, float(cfg["v3_stoploss_pct"]),
                             float(cfg["v3_takeprofit_pct"]))

        # ── Lifecycle state ────────────────────────────────────────────
        self.lifecycle_phase: int = P_LAUNCH
        self._launch_high: float = 0.0
        self._dump_low: float = 0.0
        self._dump_confirmed: bool = False
        self._bottom_confirmed: bool = False
        self._first_close_seen: bool = False

        # V1 surface state.
        self._init_v1_state()
        self._entry_E_star = 0.0
        self._kramers_down_streak = 0
        self._v2_last_decision: dict = {}

        # V1 indicator attributes (ForwardTester._capture_entry_params).
        self._init_v1_indicators()

        # Market state (state-4 ticks carry the real values).
        self._market_cap_usd: float = 0.0
        self._pool_sol: float = 0.0

        # Rolling candle flow history for the organic/dump ratio windows.
        self._flow_history: list[dict] = []

        # Holder-flow events (dev/insider sells), indexed by time.
        self._holder_flow_events: list[dict] = []
        self._holder_flow_index: dict[int, list[dict]] = {}
        self._holder_flow_timestamps: list[int] = []

    # ── Holder-flow surface (pipeline parity with the V2 adapter) ─────

    def set_holder_flow_events(self, events: list[dict]):
        """Load holder-flow events for this recording (called by pipeline)."""
        self._holder_flow_events = events
        self._holder_flow_index = {}
        for event in events:
            t = int(event.get("time", 0))
            self._holder_flow_index.setdefault(t, []).append(event)
        self._holder_flow_timestamps = sorted(self._holder_flow_index.keys())

    def append_holder_flow_events(self, events: list[dict]):
        """Live path: append newly discovered events (mirrors V2 adapter)."""
        if not events:
            return
        for event in events:
            self._holder_flow_events.append(event)
            t = int(event.get("time", 0))
            self._holder_flow_index.setdefault(t, []).append(event)
        self._holder_flow_timestamps = sorted(self._holder_flow_index.keys())

    def _has_recent_dev_sell(self, time: int, window_seconds: int) -> bool:
        """True when a dev/insider sell ≥ min_usd fired within the window."""
        if not self._holder_flow_timestamps:
            return False
        import bisect
        min_usd = float(self.cfg["v3_holder_flow_min_usd"])
        cutoff = time - window_seconds
        idx_lo = bisect.bisect_left(self._holder_flow_timestamps, cutoff)
        idx_hi = bisect.bisect_right(self._holder_flow_timestamps, time)
        for i in range(idx_lo, idx_hi):
            for event in self._holder_flow_index.get(self._holder_flow_timestamps[i], []):
                side = str(event.get("side", "")).lower()
                if side != "sell":
                    continue
                try:
                    amount = float(event.get("amount_usd", 0.0) or 0.0)
                except (TypeError, ValueError):
                    amount = 0.0
                if amount >= min_usd:
                    return True
        return False

    # ── V1 surface helpers ────────────────────────────────────────────

    def _passes_engine_version_check(self):
        return 3

    def notify_trade_opened(self, entry_price: float, direction: _V1Direction):
        self.in_position = True
        self.entry_price = float(entry_price)
        self.position_direction = direction
        self._peak_price = float(entry_price)
        self.entry_bar_count = self.bar_count
        self._kramers_down_streak = 0
        self.exit_signal_reason = ""

    def notify_trade_closed(self):
        self.in_position = False
        self.entry_price = 0.0
        self.position_direction = _V1Direction.NONE
        self._peak_price = 0.0
        self._kramers_down_streak = 0

    def _update_peak_price(self, h: float, l: float):
        if not self.in_position:
            return
        if self.position_direction == _V1Direction.UP and h > self._peak_price:
            self._peak_price = h

    def _effective_stoploss_pct(self) -> float:
        return -abs(float(self.cfg["v3_stoploss_pct"]))

    def _effective_takeprofit_pct(self) -> float:
        return abs(float(self.cfg["v3_takeprofit_pct"]))

    def _compute_trail_stop_price(self):
        if not self.in_position:
            return None
        sl = self._effective_stoploss_pct()
        return self.entry_price * (1.0 - abs(sl) / 100.0)

    # ── Indicator maintenance (V1-attrs from the V2 posterior) ─────────

    def _maintain_v1_indicators(self, o: float, h: float, l: float, c: float):
        """EMAs / ATR / m_hat / confidence — the same projection the V2
        adapter uses so the chart and the capture dicts stay meaningful."""
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

        st = self.core._last_state
        self.prev_m_hat = self.m_hat
        self.m_hat = float(st["mu"]) * 200.0
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

        regime_dist = st.get("regime_dist")
        if regime_dist is not None:
            ps = np.asarray(regime_dist, dtype=np.float64)
            ps = ps[ps > 0]
            if ps.size > 0:
                H = float(-(ps * np.log(ps)).sum())
                H_max = math.log(7)
                conf_entropy = 1.0 - (H / H_max if H_max > 0 else 1.0)
                sigma_t = self.core._last_sigma_t
                mu_star = sigma_t / math.sqrt(max(self.core.cfg["tau_max"], 1.0)) if sigma_t > 0 else 1e-6
                mu_conf = min(abs(float(st["mu"])) / (5.0 * mu_star), 1.0) if mu_star > 0 else 0.0
                self.trend_confidence = max(0.0, min(1.0, 0.6 * conf_entropy + 0.4 * mu_conf))
        self.is_trending = float(self.trend_confidence) >= self.confidence_high
        self._pre_entry_stable = self._pre_entry_stable_up = self.is_trending
        self._in_local_chop = False

    # ── Flow windows ───────────────────────────────────────────────────

    def _push_flow(self, time: int, buy_vol: float, sell_vol: float):
        """Record one candle's taker flow into the rolling window (state-4
        ticks carry the candle's full buy/sell split; intermediate states
        carry 0 and are skipped)."""
        if buy_vol + sell_vol <= 0:
            return
        t_sec = int(time)
        if self._flow_history and self._flow_history[-1]["time"] == t_sec:
            self._flow_history[-1]["buy"] = buy_vol
            self._flow_history[-1]["sell"] = sell_vol
        else:
            self._flow_history.append({"time": t_sec, "buy": buy_vol, "sell": sell_vol})
            if len(self._flow_history) > 600:
                self._flow_history.pop(0)

    def _window_flow(self, window_seconds: int) -> Optional[tuple[float, float]]:
        """(buy, sell) summed over the trailing window; None when empty."""
        if not self._flow_history:
            return None
        cutoff = int(self._current_time) - int(window_seconds)
        tot_buy = 0.0
        tot_sell = 0.0
        for fd in reversed(self._flow_history):
            if fd["time"] < cutoff:
                break
            tot_buy += fd["buy"]
            tot_sell += fd["sell"]
        if tot_buy + tot_sell <= 0:
            return None
        return tot_buy, tot_sell

    def _trailing_buy_ratio(self, window_seconds: int) -> Optional[float]:
        wf = self._window_flow(window_seconds)
        if wf is None:
            return None
        tot_buy, tot_sell = wf
        return tot_buy / (tot_buy + tot_sell + 1e-9)

    # ── Lifecycle state machine ────────────────────────────────────────

    def _update_lifecycle(self, h: float, l: float, c: float):
        """
        Advance the LAUNCH → DUMP → BOTTOM → ORGANIC machine.

        Monotone forward; every transition is measured only from tape
        observables (price extremes + trailing taker-flow windows), so the
        machine advances identically on backtest replay and live ticks.
        """
        cfg = self.cfg

        # LAUNCH: track the running high from the tape's birth.
        if self.lifecycle_phase == P_LAUNCH:
            self._launch_high = max(self._launch_high, h)
            launch_gain = (self._launch_high / c - 1.0) if c > 0 else 0.0
            if self._launch_high > 0 and launch_gain * 100.0 >= float(cfg["v3_launch_gain_min_pct"]):
                # Pump registered → immediately watch for its round-tr.
                self.lifecycle_phase = P_DUMP
            return

        if self.lifecycle_phase == P_DUMP:
            if l > 0 and (l <= self._launch_high * (1.0 - float(cfg["v3_dump_retrace_pct"]) / 100.0)):
                # Price round-tripped the pump — the dump is on the tape.
                self._dump_low = l
                self._dump_confirmed = True
                self.lifecycle_phase = P_BOTTOM
            return

        if self.lifecycle_phase == P_BOTTOM:
            self._dump_low = min(self._dump_low, l) if self._dump_low > 0 else l
            br = self._trailing_buy_ratio(int(cfg["v3_dump_window_seconds"]))
            # Flow must have NORMALISED: sell dominance decayed below the
            # dump threshold (or the window is quiet = exhaustion).
            if br is None or br > 1.0 - float(cfg["v3_dump_sell_ratio_min"]):
                self.lifecycle_phase = P_ORGANIC
            return

        if self.lifecycle_phase == P_ORGANIC:
            # Late-dump protection: a NEW round-trip low while waiting for
            # organic flow re-arms the bottom hunt (dumps are multi-leg).
            if self._dump_low > 0 and l < self._dump_low * 0.85:
                self._dump_low = l
                self.lifecycle_phase = P_BOTTOM
            return

    # ── Entry / exit decisions ────────────────────────────────────────

    def _passes_entry_gate(self, c: float, decision: dict) -> bool:
        """ALL four conditions required (user spec: only enter with enough
        potential AND volume from organic buyers)."""
        cfg = self.cfg

        # 1. Bayesian confirmation — same Kramers/Kelly contract as V2.
        if int(decision.get("direction", 0) or 0) != 1:
            return False
        if float(decision.get("E_star", -1.0) or -1.0) <= 0:
            return False
        if float(decision.get("P_up", 0.0) or 0.0) < float(cfg["v3_p_up_min"]):
            return False

        # 2. Organic flow — trailing buy ratio with a real-volume floor.
        br = self._trailing_buy_ratio(int(cfg["v3_organic_window_seconds"]))
        if br is None:
            return False
        wf = self._window_flow(int(cfg["v3_organic_window_seconds"]))
        if wf is None or (wf[0] + wf[1]) < float(cfg["v3_organic_volume_min_sol"]):
            return False
        if br < float(cfg["v3_organic_buy_ratio_min"]):
            return False

        # 3. Market-cap entry band (user spec: 2k–4k).
        mcap = self._market_cap_usd
        if mcap <= 0:
            return False
        if mcap < float(cfg["v3_mcap_entry_min_usd"]):
            return False
        if mcap > float(cfg["v3_mcap_entry_max_usd"]):
            return False

        # 4. Posterior volatility floor (barrier geometry resolved).
        if float(getattr(self.core, "_last_sigma_t", 0.0) or 0.0) < float(cfg["v3_sigma_t_min"]):
            return False

        # 5. Holder-flow entry block: never buy into a fresh insider dump.
        if (float(cfg["v3_holder_flow_enable"]) > 0.0
                and self._has_recent_dev_sell(
                    self._current_time, int(cfg["v3_holder_flow_window_seconds"]))):
            self.exit_signal_reason = "holder_flow_block"
            return False

        return True

    def _check_exit_v3(self, c: float, decision: dict) -> Optional[_V1Signal]:
        """
        STRICT exits, evaluated in priority order.  Unlike V2's posterior-
        only stack, the fixed SL/TP/band levels always fire — the levels
        ARE the risk management; the posterior exits are supplementary and
        offside-guarded so they can never cut a winning trade.
        """
        cfg = self.cfg
        entry = self.entry_price
        if entry <= 0:
            return None

        # 1. Strict take-profit: bank the runner at a fixed level.
        tp = float(cfg["v3_takeprofit_pct"])
        if tp > 0 and c >= entry * (1.0 + tp / 100.0):
            self.exit_signal_reason = "v3_take_profit"
            return _V1Signal.EXIT

        # 2. Market-cap band exit (user spec: sell ≈ 7k–8k mcap).  The
        #    recorded mcap is the same feed the entry band read; on live it
        #    is pushed per state-4 tick.
        mcap_exit = float(cfg["v3_mcap_exit_usd"])
        if mcap_exit > 0 and self._market_cap_usd >= mcap_exit:
            self.exit_signal_reason = "v3_mcap_band_exit"
            return _V1Signal.EXIT

        # 3. Strict stop-loss: dead coin cut at a fixed price.
        sl = float(cfg["v3_stoploss_pct"])
        if sl > 0 and c <= entry * (1.0 - sl / 100.0):
            self.exit_signal_reason = "v3_stop_loss"
            return _V1Signal.EXIT

        # 4. Sustained Kramers down-flip on an offside trade (supplementary;
        #    the strict SL dominates for deep losses — this catches the
        #    slow-bleed that never reaches −SL before the tape dies).
        offside_pct = float(cfg["v3_kramers_offside_pct"])
        if (float(cfg["v3_kramers_down_persist"]) > 0
                and c <= entry * (1.0 - offside_pct / 100.0)):
            p_up = float(decision.get("P_up", 0.0) or 0.0)
            p_down = float(decision.get("P_down", 0.0) or 0.0)
            p_zero = float(decision.get("P_zero", 0.0) or 0.0)
            if p_down > p_up and p_down > p_zero and p_down >= 0.5:
                self._kramers_down_streak += 1
                if self._kramers_down_streak >= int(cfg["v3_kramers_down_persist"]):
                    self.exit_signal_reason = "v3_kramers_down_exit"
                    return _V1Signal.EXIT
            else:
                self._kramers_down_streak = 0

        # 5. Holder-flow dev/insider sell while in position — the newborn
        #    dump-resumption event (same observable V2's iter43 gate uses).
        if (float(cfg["v3_holder_flow_enable"]) > 0.0
                and self._has_recent_dev_sell(
                    self._current_time, int(cfg["v3_holder_flow_window_seconds"]))):
            self.exit_signal_reason = "v3_dev_sell_exit"
            return _V1Signal.EXIT

        return None

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
        self.bar_count += 1
        self._current_time = int(time)
        if market_cap_usd > 0.0:
            self._market_cap_usd = market_cap_usd
        if pool_sol > 0.0:
            self._pool_sol = pool_sol

        # Candle flow lands on state-4 ticks only (the 4-state expansion
        # carries zero volume on states 1–3), so the flow windows see each
        # candle exactly once — identical in backtest and live.
        self._push_flow(time, buy_volume, sell_volume)

        _prev_close = self._prev_close
        self._maintain_v1_indicators(o, h, l, c)

        # ── V2 core: one obs bucket per tick (same mapping as the V2
        # adapter — log_return vs the previous close, taker split as the
        # signed delta, spread proxy |ln(o/c)|).
        sigma_t = self.core._last_sigma_t
        log_return = 0.0
        if _prev_close and _prev_close > 0 and c > 0:
            log_return = math.log(c / _prev_close)
        spread = abs(math.log(o / c)) if o > 0 and c > 0 else 1e-3
        obs = {
            "dt": 1.0,
            "log_return": float(log_return),
            "volume": float(volume),
            "signed_delta": float(buy_volume - sell_volume),
            "spread": float(spread),
            "bid_depth": max(float(buy_volume + 1.0), 1.0),
            "ask_depth": max(float(sell_volume + 1.0), 1.0),
        }
        state = self.core.update_state(obs)

        # Potential / barriers recompute only when new flow entered the
        # KDE (state-4) or a full result was requested (dashboard).
        if _build_full_result or buy_volume + sell_volume > 0.0:
            self.core.compute_potential_and_barriers()

        # ── Lifecycle advance + V1 regime/direction projection ────────
        self._update_lifecycle(h, l, c)
        self.prev_direction = self.direction
        mu = float(state["mu"])
        if mu > 0:
            self.direction = _V1Direction.UP
        elif mu < 0:
            self.direction = _V1Direction.DOWN
        else:
            self.direction = _V1Direction.NONE

        v2_regime = int(state["regime"])
        self.regime = _PHASE_TO_V1_REGIME.get(self.lifecycle_phase, _V1Regime.IDLE)

        # Bayesian decision at the compressed V3 horizon.
        decision = self.core.get_decision(horizon=int(self.core.cfg.get("tau_max", 10)))
        self._v2_last_decision = decision

        # ── Signal emission ───────────────────────────────────────────
        v1_signal: _V1Signal = _V1Signal.NONE
        min_warmup = max(int(self.cfg["warmup_bars"]), 16)

        if self.bar_count <= min_warmup:
            v1_signal = _V1Signal.NONE
            self.exit_signal_reason = ""
        elif self.in_position:
            exit_signal = self._check_exit_v3(c, decision)
            if exit_signal is not None:
                v1_signal = exit_signal
        else:
            # Entries live ONLY in the ORGANIC phase — the bottom-recovery
            # trade; LAUNCH/DUMP/BOTTOM phases never emit BUY.
            if self.lifecycle_phase == P_ORGANIC and self._passes_entry_gate(c, decision):
                v1_signal = _V1Signal.BUY
                self._entry_E_star = float(decision.get("E_star", 0.0))
                self.exit_signal_reason = ""

        # Peak tracking for the capture dicts / chart trail display.
        if self.in_position and v1_signal != _V1Signal.EXIT:
            self._update_peak_price(h, l)

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
            "v2_mu": float(state["mu"]),
            "v2_phi": float(state["phi"]),
            "v2_h": float(state["h"]),
            "v2_sigma_t": self.core._last_sigma_t,
            "v2_ell": float(state["ell"]),
            "v2_k_up": float(decision.get("k_up", 0.0)),
            "v2_k_down": float(decision.get("k_down", 0.0)),
            "v2_P_up": float(decision.get("P_up", 0.0)),
            "v2_P_down": float(decision.get("P_down", 0.0)),
            "v2_E_star": float(decision.get("E_star", 0.0)),
            "v2_n_star": float(decision.get("n_star", 0.0)),
            "v2_direction": int(decision.get("direction", 0)),
            "price_overextended": self._price_overextended(c),
            "momentum_past_peak": self._momentum_past_peak(),
        }
        full["lifecycle_phase"] = _PHASE_NAMES.get(self.lifecycle_phase, "launch")
        full["launch_high"] = self._launch_high
        full["dump_low"] = self._dump_low
        full["volume_profiles"] = []
        full["in_position"] = self.in_position
        full["entry_price"] = self.entry_price
        full["peak_price"] = self._peak_price
        full["trail_stop_price"] = self._compute_trail_stop_price()
        full["exhaustion_bars"] = self.exhaustion_bar_count
        full["in_chop"] = False
        full["trend_bars"] = self.trend_bar_count
        return full
