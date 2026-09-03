"""
Forward Tester — Realistic LONG-ONLY trade simulation for the strategy engine.

Settings:
  - Starting balance: 1 SOL
  - Buy size: 0.1 SOL (10% of portfolio)
  - Priority fee: 0.0001 SOL (fixed, no bribe fee)
  - Slippage: 1% (run_backtest default)

Execution models (``exec_model``):

  "instant" (DEFAULT — mirrors LiveTrader after the 2026-08-30 signal-instant
  execution change):
    - Signals from candle state k are executed IMMEDIATELY at that state's
      close price (the price at the instant the signal was generated).
    - Fill price = signal-state close ± slippage_pct (× exec_offset if set).
    - No pending queue, no n+1-bar wait.  The engine is notified of the
      open/close at the same intra-candle state that produced the signal,
      exactly like the live trader.
    - Optional measured-latency overlay: ``entry_latency_seconds`` /
      ``exit_latency_seconds`` (> 0) defer the FILL to a target time
      t_signal + latency and fill at the recorded intra-candle price path
      interpolated at that target (walks the realistic open→extreme→extreme→
      close path of the containing candle).  The engine is notified when the
      fill lands.  Measured live signal→confirm latencies (626-session
      journal audit, 2026-08-30): buys median 10.0 s (p90 15.7 s — includes
      RPC confirm lag), sells median 2.3 s (p90 4.5 s); on-chain LANDING is
      earlier than confirm, so treat those as upper bounds.

  "legacy" (pre-2026-08-30 n+1 model — byte-identical to every historical
  baseline batch):
    - Signals from candle N are executed during candle N+1 (1-bar delay).
    - The fill time within candle N+1 is determined by a *fill_fraction*
      (0 → 1) that represents how far into the bar the transaction completes:

        fill_fraction = clamp(base_delay * size_penalty * slippage_penalty, 0.02, 0.98)

      where:
        base_delay     = reference_fee / (reference_fee + total_fee)
                         smooth bounded decay: 0 (very high fee, instant) → 1 (zero fee)
                         e.g. low fee 0.00011 → ~0.82 | mid 0.0006 → ~0.45 | high 0.0025 → ~0.17
        size_penalty   = 1 + log10(max(1, buy_size_sol / ref_size))
                         (larger order → more queue depth → slower fill)
        slippage_penalty = 1 + slippage_pct / 100
                         (higher slippage tolerance adds modest extra latency)
        reference_fee  = 0.0001 SOL  (fixed per-transaction fee)
        ref_size       = 0.1  SOL    (typical retail buy size)

    - The intra-bar price at fill_fraction is interpolated along the realistic
      candle path:  open → first_extreme → second_extreme → close
      (bull bar: open→high→low→close; bear bar: open→low→high→close).

    - Slippage is then applied on top of the interpolated price:
        buy  → price * (1 + slip)
        sell → price * (1 - slip)

    - The final hyper-realistic price is stored as entry_price / exit_price,
      so the chart reflects what you actually got filled at.
    - pnl_pct reflects the performance based on these final real prices.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
from strategy_engine import Signal, Direction, Regime
from engine_factory import create_engine


def _v2_entry_snapshot(eng) -> dict:
    """
    iter17: extract the V2 Kramers/Kelly decision snapshot for per-trade
    entry-feature logging (counterfactual entry-gate analysis).

    Parity-safe: read-only on the engine, write-only into the trade record.
    V1 engines have no `_v2_last_decision` attribute → returns {}.
    The stash is refreshed on every adapter update(), so at capture time
    (state 1 of the fill bar) it reflects the fill-bar decision — matching
    the rest of the entry_params snapshot semantics.
    """
    dec = getattr(eng, "_v2_last_decision", None)
    if not dec:
        return {}
    st = dec.get("state", {}) or {}
    def f(d, k):
        v = d.get(k)
        try:
            return round(float(v), 8)
        except (TypeError, ValueError):
            return None
    return {
        "v2_k_up":        f(dec, "k_up"),
        "v2_k_down":      f(dec, "k_down"),
        "v2_P_up":        f(dec, "P_up"),
        "v2_P_down":      f(dec, "P_down"),
        "v2_P_zero":      f(dec, "P_zero"),
        "v2_du_up":       f(dec, "du_up"),
        "v2_du_down":     f(dec, "du_down"),
        "v2_mu_hat_tau":  f(dec, "mu_hat_tau"),
        "v2_sigma2_tau":  f(dec, "sigma2_tau"),
        "v2_E_star":      f(dec, "E_star"),
        "v2_n_star":      f(dec, "n_star"),
        "v2_direction":   dec.get("direction"),
        "v2_tau":         f(dec, "tau"),
        "v2_mu":          f(st, "mu"),
        "v2_phi":         f(st, "phi"),
        "v2_h":           f(st, "h"),
        "v2_var_phi":     f(st, "var_phi"),
        "v2_sigma_t":     round(float(getattr(getattr(eng, "core", None), "_last_sigma_t", 0.0) or 0.0), 8),
        "v2_regime_code": st.get("regime"),
    }


@dataclass
class Trade:
    entry_time: int
    entry_price: float          # Hyper-realistic price (delay + slippage)
    size_sol: float             # SOL committed to the trade
    size_tokens: float          # tokens bought (accounting for slippage)
    exit_time: Optional[int] = None
    exit_price: Optional[float] = None   # Hyper-realistic price at exit
    pnl_sol: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    entry_reason: str = ""
    fees_paid: float = 0.0      # priority + bribe fees (not slippage)
    slippage_cost_sol: float = 0.0  # total SOL lost to slippage both ways
    outcome: str = ""           # "W" or "L" — set when trade is closed
    entry_params: dict = field(default_factory=dict)  # engine snapshot at entry
    exit_params: dict = field(default_factory=dict)   # engine snapshot at exit

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ForwardTestStats:
    starting_balance: float = 1.0
    current_balance: float = 1.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_sol: float = 0.0
    total_fees_paid: float = 0.0
    max_drawdown_pct: float = 0.0
    peak_balance: float = 1.0
    win_rate: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class ForwardTester:
    """
    Realistic forward tester wrapping StrategyEngine.
    LONG-ONLY: buys tokens and sells them back to SOL.
    """

    # iter80: armed/harvest exit classes eligible for deferred fills under
    # `exit_latency_armed_only` (the loss book stays instant — its forward
    # drift after fire is NEGATIVE, E[Δp30] = −0.25%, so deferring it is
    # poison; see analysis/iter80_PREREGISTRATION.md §2).
    _ARMED_EXIT_REASONS = frozenset({
        "gain_retrace", "rate_split_flip:armed", "tp_v2",
        "breakeven_scratch", "reversal_exit",
    })

    def __init__(
        self,
        starting_balance: float = 1.0,
        buy_size_sol: float = 0.1,
        priority_fee: float = 0.0001,
        bribe_fee: float = 0.0,
        slippage_pct: float = 10.0,
        engine_kwargs: Optional[dict] = None,
        engine_version: int = 1,
        holder_flow_events: Optional[list[dict]] = None,
        holder_flow_latency_seconds: float = 0.0,
        # ── iter66: execution-level calibration (additive, default OFF) ────
        # Live Jupiter-routed fills clear the recorded print stream at a
        # structurally higher level (measured median +27% on entries, +23%
        # on exits across the 2026-08-26 divergence audit).  These knobs
        # shift simulated fill prices to that executable level so backtests
        # can be made representative of live execution.  0.0 = exact legacy
        # behaviour (×(1+0/100) is an IEEE-exact identity — bit-for-bit).
        exec_offset_pct_buy: float = 0.0,
        exec_offset_pct_sell: float = 0.0,
        # ── Execution model (2026-08-30 signal-instant execution) ──────────
        # "instant": fill at the signal-instant price (state close ± slippage),
        #            no n+1-bar wait — the exact mirror of the live trader.
        # "legacy":  pre-change n+1 mid-bar model, byte-identical to every
        #            historical baseline batch.
        # entry/exit_latency_seconds: fill deferred to t_signal + latency and
        #            priced on the recorded intra-candle path (latency mode).
        exec_model: str = "instant",
        entry_latency_seconds: float = 0.0,
        exit_latency_seconds: float = 0.0,
    ):
        if engine_kwargs is None:
            engine_kwargs = {}

        self.engine = create_engine(engine_version, **engine_kwargs)
        self.balance = starting_balance
        self.buy_size_sol = buy_size_sol
        self.priority_fee = 0.0001   # fixed: 0.0001 SOL per transaction
        self.bribe_fee = 0.0          # fixed: no bribe fee
        self.slippage_pct = slippage_pct
        # iter66: execution-level calibration offsets (default 0.0 = OFF)
        self.exec_offset_pct_buy = float(exec_offset_pct_buy)
        self.exec_offset_pct_sell = float(exec_offset_pct_sell)
        # Execution model selection: "instant" (default, mirrors live),
        # "latency" (instant + measured-latency path-priced fill), or
        # "legacy" (pre-2026-08-30 n+1 mid-bar model — byte-identical to
        # every historical baseline batch).
        self.exec_model = str(exec_model)
        self.entry_latency_seconds = max(0.0, float(entry_latency_seconds))
        self.exit_latency_seconds = max(0.0, float(exit_latency_seconds))
        # iter80: when True, only armed/harvest exit reasons defer; the loss
        # book (kelly_flat / evr_triage / kramers_down_exit / dev_sell_exit /
        # recording_ended) fills at the signal instant.  Set by
        # enable_exit_latency() (engine-keyed injection) — see DEFAULT_CONFIG
        # `v2_exit_delay_armed_only`.
        self.exit_latency_armed_only = False
        if self.exec_model == "legacy":
            self._exec_mode = "legacy"
        elif self.entry_latency_seconds > 0.0 or self.exit_latency_seconds > 0.0:
            self._exec_mode = "latency"
        else:
            self._exec_mode = "instant"
        # Recorded intra-candle path per candle time: t → (o, h, l, c).
        # Populated by update(); consumed by _path_price_at() so latency
        # fills price against the same recorded price stream the live
        # trader acted on.
        self._path_candles: dict[int, tuple] = {}
        self._path_times: list[int] = []
        # Latency-mode deferred fill: {"side": "buy"|"exit", "target_ts": float,
        # "reason": str, "entry": Trade|None}.  At most one in flight — the
        # engine cannot emit a second signal while a fill is pending because
        # in_position gates entries and exits respectively.
        self._latency_pending: Optional[dict] = None

        # Holder-flow events for this recording (dev/insider wallet trades).
        # Passed into the engine so the V2 adapter can check them as an
        # entry gate / exit trigger (iter36).  The pipeline layer (backtester
        # or live trader) is responsible for loading them from the DB.
        #
        # holder_flow_latency_seconds: simulates the GMGN poll delay so the
        # backtester doesn't see events before the live engine could have.
        # Each event's time is shifted forward by this many seconds before
        # indexing, matching the live delivery latency.
        if holder_flow_events:
            if holder_flow_latency_seconds > 0:
                shifted = []
                for ev in holder_flow_events:
                    ev = dict(ev)
                    ev["time"] = int(ev.get("time", 0)) + int(holder_flow_latency_seconds)
                    shifted.append(ev)
                holder_flow_events = shifted
            self.engine.set_holder_flow_events(holder_flow_events)

        self.stats = ForwardTestStats(
            starting_balance=starting_balance,
            current_balance=starting_balance,
            peak_balance=starting_balance,
        )

        self.current_trade: Optional[Trade] = None
        self.trade_history: list[Trade] = []
        self.signals_log: list[dict] = []

        # Pending signals: executed on the next candle's open (1-bar delay)
        self._pending_buy: bool = False
        self._pending_buy_reason: str = ""
        self._pending_exit: bool = False
        self._pending_exit_reason: str = ""

        # Signal-candle param snapshots: captured immediately when the signal is
        # queued (Step 3, after engine.update on candle N) using the signal
        # candle's close as the price reference.  Assigned to the trade when
        # it fills on candle N+1.
        self._stashed_entry_params: dict = {}
        self._stashed_exit_params: dict = {}

    def enable_entry_latency(self, seconds: float) -> None:
        """iter78 adoption hook: switch an instant-mode tester into latency
        mode with a deferred-entry fill of `seconds` (the adopted 5 s cell).

        Called by backtester.run_backtest AFTER construction, keyed on the
        ENGINE's `v2_entry_delay_seconds` knob (default 5.0 since the
        2026-09-02 adoption) so bare {} runs the adopted execution model.
        Never called for the legacy exec model; an explicit positive
        `entry_latency_seconds` constructor argument keeps precedence
        (run_backtest only calls this when its own explicit argument is 0).
        """
        seconds = max(0.0, float(seconds))
        if seconds <= 0.0 or self._exec_mode == "legacy":
            return
        self.entry_latency_seconds = seconds
        self.exec_model = "latency"
        self._exec_mode = "latency"

    def enable_exit_latency(self, seconds: float, armed_only: bool = False) -> None:
        """iter80 hook: deferred EXIT fills (`seconds` after the exit signal
        fires, priced on the recorded path), optionally restricted to the
        armed/harvest exit classes (`_ARMED_EXIT_REASONS`) — the sell-side
        mirror of `enable_entry_latency`.  Keyed on the ENGINE's
        `v2_exit_delay_seconds` / `v2_exit_delay_armed_only` knobs by
        backtester.run_backtest; an explicit positive `exit_latency_seconds`
        constructor argument keeps precedence (run_backtest only calls this
        when its own explicit argument is 0).  0.0 = no-op (byte parity).
        """
        seconds = max(0.0, float(seconds))
        if seconds <= 0.0 or self._exec_mode == "legacy":
            return
        self.exit_latency_seconds = seconds
        self.exit_latency_armed_only = bool(armed_only)
        self.exec_model = "latency"
        self._exec_mode = "latency"

    @property
    def total_fees_per_trade(self) -> float:
        return self.priority_fee + self.bribe_fee

    def _slippage_cost_buy(self, raw_price: float, trade_size_sol: float) -> float:
        """
        SOL lost to slippage on a buy.
        We pay raw_price * (1 + slip) effective price instead of raw_price.
        Extra cost = trade_size_sol * slip / (1 + slip)  ≈ trade_size_sol * slip
        (exact: we get fewer tokens than ideal, so effective cost = trade_size_sol * slip/(1+slip))
        """
        slip = self.slippage_pct / 100.0
        # tokens we'd get at perfect price vs slipped price
        # cost is expressed as SOL we didn't get back
        return trade_size_sol * slip / (1.0 + slip)

    def _slippage_cost_sell(self, raw_price: float, size_tokens: float) -> float:
        """
        SOL lost to slippage on a sell.
        We receive raw_price * (1 - slip) instead of raw_price per token.
        """
        slip = self.slippage_pct / 100.0
        return raw_price * size_tokens * slip

    # ── Timed-delay fill parameters ────────────────────────────────────────
    _REFERENCE_FEE: float = 0.0001   # SOL — fixed per-transaction fee
    _REFERENCE_SIZE: float = 0.1      # SOL — typical retail order size

    def _fill_fraction(self) -> float:
        """
        Compute how far into the execution candle (0 → 1) the transaction
        completes, driven by fee competitiveness, order size, and slippage.

        Formula:  fill_fraction = clamp(base_delay * size_penalty * slippage_factor, 0.02, 0.98)

          base_delay   = ref_fee / (ref_fee + total_fee)
                         Smooth bounded decay (unlike ref/fee which blows up for low fees):
                           total_fee ~0.00011 → base ~0.82  (fills late — low priority)
                           total_fee ~0.0006  → base ~0.45  (fills mid-bar)
                           total_fee ~0.0025  → base ~0.17  (fills early — aggressive)
          size_penalty = 1 + log10(max(1, buy_size / ref_size))
                         Larger orders sit deeper in the queue → slower fill.
          slippage_factor = 1 + slippage_pct / 100
                         Higher slippage tolerance adds modest extra latency.
        """
        import math
        total_fee = max(self.priority_fee + self.bribe_fee, 1e-12)
        # Bounded smooth decay: high fee → small base_delay → fills early in the bar
        base_delay = self._REFERENCE_FEE / (self._REFERENCE_FEE + total_fee)
        size_penalty = 1.0 + math.log10(max(1.0, self.buy_size_sol / self._REFERENCE_SIZE))
        slippage_factor = 1.0 + self.slippage_pct / 100.0
        frac = base_delay * size_penalty * slippage_factor
        return max(0.02, min(0.98, frac))

    @staticmethod
    def _intrabar_price(o: float, h: float, l: float, c: float, frac: float) -> float:
        """
        Interpolate a price along the realistic intra-bar path at position
        *frac* (0 = open, 1 = close).

        The path is split into three equal segments:
          [0, 1/3)  →  open  →  first_extreme  (high for bull, low for bear)
          [1/3,2/3) →  first_extreme → second_extreme
          [2/3, 1]  →  second_extreme → close
        """
        bullish = c >= o
        if bullish:
            p0, p1, p2, p3 = o, h, l, c
        else:
            p0, p1, p2, p3 = o, l, h, c

        if frac <= 1 / 3:
            t = frac * 3
            return p0 + (p1 - p0) * t
        elif frac <= 2 / 3:
            t = (frac - 1 / 3) * 3
            return p1 + (p2 - p1) * t
        else:
            t = (frac - 2 / 3) * 3
            return p2 + (p3 - p2) * t

    # ── Latency-mode price-path resolution ──────────────────────────────────

    def _record_path_candle(self, time: int, o: float, h: float, l: float,
                            c: float) -> None:
        """Buffer the candle's OHLC so latency fills can price against the
        recorded path.  States 1-3 share the candle time and progressively
        complete h/l/c; the final write per time is the full candle."""
        if self._exec_mode != "latency":
            return
        t = int(time)
        if t not in self._path_candles:
            self._path_times.append(t)
        self._path_candles[t] = (o, h, l, c)

    def _path_price_at(self, target_ts: float) -> float:
        """Price on the recorded intra-candle path at absolute time *target_ts*.

        Locates the candle containing the timestamp (greatest buffered candle
        time ≤ target) and interpolates along its open→extreme→extreme→close
        path using the NEXT buffered candle time as the bar span.  Beyond the
        recording end this clamps to the last close (the same convention as
        the recording_ended force-close)."""
        import bisect
        if not self._path_times:
            return 0.0
        t0 = int(target_ts)
        idx = bisect.bisect_right(self._path_times, t0) - 1
        if idx < 0:
            o, h, l, c = self._path_candles[self._path_times[0]]
            return o
        t = self._path_times[idx]
        o, h, l, c = self._path_candles[t]
        if idx + 1 < len(self._path_times):
            span = self._path_times[idx + 1] - t
        else:
            span = 1  # recording end — assume the recording's bar length
        frac = max(0.0, min(1.0, (target_ts - t) / span))
        return self._intrabar_price(o, h, l, c, frac)

    def _resolve_latency_fill(self, time: int, o: float, h: float, l: float,
                              c: float) -> tuple[Optional[str], Optional[Trade]]:
        """Execute the deferred fill once the candle containing its target
        timestamp is complete (time > floor(target_ts) — no intra-candle
        future leak).  Returns (trade_action, trade)."""
        pend = self._latency_pending
        if pend is None:
            return None, None
        target_ts = pend["target_ts"]
        # The containing candle's full path is only known once its state 4
        # has been fed — i.e. on the first update whose time exceeds it.
        if float(time) <= target_ts:
            return None, None
        self._latency_pending = None
        fill_price = self._path_price_at(target_ts)
        fill_time = int(target_ts)
        if pend["side"] == "buy":
            trade = self._open_long(o, h, l, c, fill_time,
                                    reason=pend["reason"], fill_price=fill_price)
            return ("buy", trade) if trade else (None, None)
        trade = self._close_long(o, h, l, c, fill_time,
                                 reason=pend["reason"], fill_price=fill_price)
        return ("exit", trade) if trade else (None, None)

    def _open_long(self, o: float, h: float, l: float, c: float, time: int, reason: str = "",
                   fill_price: Optional[float] = None) -> Optional[Trade]:
        """
        Open a long position.

        Fill price resolution:
          - fill_price given (latency mode) → use it directly.
          - instant mode                    → the signal state's close (the
            price at the instant the signal was generated).
          - legacy mode                     → interpolated along the execution
            candle's intra-bar path at _fill_fraction(), which accounts for
            fee competitiveness, order size, and slippage tolerance.
        Slippage is applied on top of the resolved price.
        """
        if self.current_trade is not None:
            return None
        if o <= 0:
            return None

        # 1. Resolve the raw fill price
        if fill_price is not None:
            raw_price = float(fill_price)
        elif self._exec_mode == "instant":
            raw_price = c
        else:
            frac = self._fill_fraction()
            raw_price = self._intrabar_price(o, h, l, c, frac)

        # 2. Add slippage (buy → price kicks up)
        slip = self.slippage_pct / 100.0
        exec_price = raw_price * (1.0 + slip)

        # iter66: optional execution-level calibration.  0.0 → ×1.0 exact identity.
        if self.exec_offset_pct_buy:
            exec_price *= (1.0 + self.exec_offset_pct_buy / 100.0)

        fees = self.total_fees_per_trade
        trade_size = min(self.buy_size_sol, self.balance - fees)
        if trade_size <= 0:
            return None

        # Tokens received at fill price
        tokens = trade_size / exec_price

        # Slippage/delay cost vs ideal open
        ideal_tokens = trade_size / o
        slippage_cost = max(0.0, (ideal_tokens - tokens) * o)

        # Deduct trade size + fees from balance
        self.balance -= (trade_size + fees)

        trade = Trade(
            entry_time=time,
            entry_price=exec_price,          # CHART price — realistic, includes delay + slippage
            size_sol=trade_size,
            size_tokens=tokens,
            fees_paid=fees,
            slippage_cost_sol=slippage_cost,
            entry_reason=reason,
            entry_params=self._stashed_entry_params,  # signal-candle snapshot
        )
        self._stashed_entry_params = {}  # clear stash
        self.current_trade = trade
        self.engine.notify_trade_opened(exec_price, Direction.UP)
        return trade

    def _capture_entry_params(self, exec_price: float) -> dict:
        """
        Build the full engine snapshot for entry_params.

        MUST be called AFTER engine.update() has run for the entry candle,
        so that bar_count, trend_bar_count, regime, EMAs, ATR, confidence,
        etc. reflect the bar on which the trade was actually filled — not
        the prior bar that merely produced the signal.
        """
        eng = self.engine
        return {
            # Regime / direction
            "regime":                   eng.regime.value,
            "direction":                eng.direction.value,
            "prev_direction":           eng.prev_direction.value,
            "trend_before_exhaustion":  eng.trend_before_exhaustion.value,

            # Kalman / momentum
            "m_hat":                    round(eng.m_hat, 8),
            "prev_m_hat":               round(eng.prev_m_hat, 8),
            "p_hat":                    round(eng.p_hat, 8),
            "momentum_acceleration":    round(eng.momentum_acceleration, 8),
            "_momentum_peak_declining_count": eng._momentum_peak_declining_count,
            "momentum_past_peak":       eng._momentum_past_peak(),

            # Signal
            "signal_strength":          round(eng.signal_strength, 6),
            "s_effective":              round(eng.s_effective, 6),

            # EMA
            "ema_fast":                 round(eng.ema_fast_val or 0.0, 8),
            "ema_slow":                 round(eng.ema_slow_val or 0.0, 8),
            "ema_macro":                round(eng.ema_macro_val or 0.0, 8),
            "ema_spread":               round(eng.ema_spread, 8),
            "prev_ema_spread":          round(eng.prev_ema_spread, 8),
            "spread_expanding":         eng.spread_expanding,

            # ATR / volatility
            "atr":                      round(eng.atr_val or 0.0, 8),
            "atr_floor":                round(eng.atr_floor, 8),

            # Confidence / regime filter
            "trend_confidence":         round(eng.trend_confidence, 6),
            "is_trending":              eng.is_trending,

            # Cross / stability / chop
            "ema_cross_valid":          eng._ema_cross_valid,
            "_ema_cross_persist_count": eng._ema_cross_persist_count,
            "pre_entry_stable":         eng._pre_entry_stable,
            "pre_entry_stable_up":      eng._pre_entry_stable_up,
            "pre_entry_stable_down":    eng._pre_entry_stable_down,
            "in_local_chop":            eng._in_local_chop,

            # Overextension
            "price_overextended":       eng._price_overextended(exec_price),
            "overextension_ratio":      round(exec_price / eng.p_hat, 6) if eng.p_hat > 0 else 0.0,

            # Trend anchors / bar counters
            "bar_count":                eng.bar_count,
            "trend_bar_count":          eng.trend_bar_count,
            "exhaustion_bar_count":     eng.exhaustion_bar_count,
            "exhaustion_persist_count": eng.exhaustion_persist_count,
            "reversal_confirm_count":   eng.reversal_confirm_count,
            "trend_reversal_confirm_count": eng.trend_reversal_confirm_count,
            "reversal_bar_count":       eng.reversal_bar_count,
            "no_motion_count":          eng.no_motion_count,
            "_exhaustion_s_decay_count": eng._exhaustion_s_decay_count,
            "trend_start_bar":          eng.trend_start_bar,
            "trend_start_price":        round(eng.trend_start_price, 8),
            "trend_start_atr":          round(eng.trend_start_atr, 8),
            "_exhaustion_phase_high":   round(eng._exhaustion_phase_high, 8),

            # Computed helpers
            "effective_stoploss_pct":   round(eng._effective_stoploss_pct(), 4),
            "effective_takeprofit_pct": round(eng._effective_takeprofit_pct(), 4),
            "global_stoploss_pct":      round(eng._global_stoploss_pct(), 4),

            # ── V2 decision snapshot (iter17; getattr-guarded, V1-safe) ──
            **_v2_entry_snapshot(eng),

            # ── Config / hyperparameters (all of them) ───────────────────
            "cfg_ema_fast_p":                    eng.ema_fast_p,
            "cfg_ema_slow_p":                    eng.ema_slow_p,
            "cfg_atr_period":                    eng.atr_period,
            "cfg_roc_period":                    eng.roc_period,
            "cfg_warmup":                        eng.warmup,
            "cfg_S_strong":                      eng.S_strong,
            "cfg_S_weak":                        eng.S_weak,
            "cfg_S_noise":                       eng.S_noise,
            "cfg_exhaustion_bars_limit":         eng.exhaustion_bars_limit,
            "cfg_delta_threshold":               eng.delta_threshold,
            "cfg_min_trend_bars":                eng.min_trend_bars,
            "cfg_reversal_confirm_bars":         eng.reversal_confirm_bars,
            "cfg_chop_atr_pct":                  eng.chop_atr_pct,
            "cfg_chop_spread_pct":               eng.chop_spread_pct,
            "cfg_reversal_exit_confirm_bars":    eng.reversal_exit_confirm_bars,
            "cfg_s_effective_threshold":         eng.s_effective_threshold,
            "cfg_exhaustion_persist_bars":       eng.exhaustion_persist_bars,
            "cfg_regime_lookback":               eng.regime_lookback,
            "cfg_persistence_threshold":         eng.persistence_threshold,
            "cfg_momentum_mean_threshold":       eng.momentum_mean_threshold,
            "cfg_ema_min_spread_pct":            eng.ema_min_spread_pct,
            "cfg_confidence_high":               eng.confidence_high,
            "cfg_confidence_low":                eng.confidence_low,
            "cfg_entry_confidence_high":         eng.entry_confidence_high,
            "cfg_entry_confidence_low":          eng.entry_confidence_low,
            "cfg_confidence_very_high":          eng.confidence_very_high,
            "cfg_confidence_w1":                 eng.confidence_w1,
            "cfg_confidence_w2":                 eng.confidence_w2,
            "cfg_confidence_w3":                 eng.confidence_w3,
            "cfg_confidence_w4":                 eng.confidence_w4,
            "cfg_atr_floor_k":                   eng.atr_floor_k,
            "cfg_ema_cross_persist_bars":        eng.ema_cross_persist_bars,
            "cfg_exhaustion_s_decay_bars":       eng.exhaustion_s_decay_bars,
            "cfg_exhaustion_stall_bars":         eng.exhaustion_stall_bars,
            "cfg_exhaustion_stall_atr_pct":      eng.exhaustion_stall_atr_pct,
            "cfg_local_range_bars":              eng.local_range_bars,
            "cfg_local_range_threshold_pct":     eng.local_range_threshold_pct,
            "cfg_sign_flip_threshold":           eng.sign_flip_threshold,
            "cfg_stability_bars":                eng.stability_bars,
            "cfg_spike_atr_multiplier":          eng.spike_atr_multiplier,
            "cfg_spike_lookback_bars":           eng.spike_lookback_bars,
            "cfg_body_baseline_bars":            eng.body_baseline_bars,
            "cfg_overextension_k":               eng.overextension_k,
            "cfg_momentum_peak_bars":            eng.momentum_peak_bars,
            "cfg_consolidation_range_pct":       eng.consolidation_range_pct,
            "cfg_ema_macro_period":              eng.ema_macro_period,
            "cfg_stoploss_pct":                  eng.stoploss_pct,
            "cfg_takeprofit_pct":                eng.takeprofit_pct,
            "cfg_stoploss_pct_low":              eng.stoploss_pct_low,
            "cfg_stoploss_pct_high":              eng.stoploss_pct_high,
            "cfg_takeprofit_pct_low":            eng.takeprofit_pct_low,
            "cfg_takeprofit_pct_high":           eng.takeprofit_pct_high,
            "cfg_max_entry_bar_count":           eng.max_entry_bar_count,
            "cfg_forbidden_bc_lo":               getattr(eng, "forbidden_bc_lo", 0),
            "cfg_forbidden_bc_hi":               getattr(eng, "forbidden_bc_hi", 0),
        }

    def _capture_exit_params(self, exec_price: float) -> dict:
        """
        Build the engine snapshot for exit_params.

        Mirrors _capture_entry_params() but is called for the EXIT bar.
        Must be called AFTER engine.update() has run for the exit candle so
        that regime, EMAs, ATR, bar counts, etc. reflect the bar on which the
        sell was actually filled.
        """
        eng = self.engine
        return {
            # Regime / direction
            "regime":                   eng.regime.value,
            "direction":                eng.direction.value,
            "prev_direction":           eng.prev_direction.value,
            "trend_before_exhaustion":  eng.trend_before_exhaustion.value,

            # Kalman / momentum
            "m_hat":                    round(eng.m_hat, 8),
            "prev_m_hat":               round(eng.prev_m_hat, 8),
            "p_hat":                    round(eng.p_hat, 8),
            "momentum_acceleration":    round(eng.momentum_acceleration, 8),
            "_momentum_peak_declining_count": eng._momentum_peak_declining_count,
            "momentum_past_peak":       eng._momentum_past_peak(),

            # Signal
            "signal_strength":          round(eng.signal_strength, 6),
            "s_effective":              round(eng.s_effective, 6),

            # EMA
            "ema_fast":                 round(eng.ema_fast_val or 0.0, 8),
            "ema_slow":                 round(eng.ema_slow_val or 0.0, 8),
            "ema_macro":                round(eng.ema_macro_val or 0.0, 8),
            "ema_spread":               round(eng.ema_spread, 8),
            "prev_ema_spread":          round(eng.prev_ema_spread, 8),
            "spread_expanding":         eng.spread_expanding,

            # ATR / volatility
            "atr":                      round(eng.atr_val or 0.0, 8),
            "atr_floor":                round(eng.atr_floor, 8),

            # Confidence / regime filter
            "trend_confidence":         round(eng.trend_confidence, 6),
            "is_trending":              eng.is_trending,

            # Cross / stability / chop
            "ema_cross_valid":          eng._ema_cross_valid,
            "_ema_cross_persist_count": eng._ema_cross_persist_count,
            "pre_entry_stable":         eng._pre_entry_stable,
            "pre_entry_stable_up":      eng._pre_entry_stable_up,
            "pre_entry_stable_down":    eng._pre_entry_stable_down,
            "in_local_chop":            eng._in_local_chop,

            # Overextension relative to Kalman estimate
            "price_overextended":       eng._price_overextended(exec_price),
            "overextension_ratio":      round(exec_price / eng.p_hat, 6) if eng.p_hat > 0 else 0.0,

            # Trend anchors / bar counters
            "bar_count":                eng.bar_count,
            "trend_bar_count":          eng.trend_bar_count,
            "exhaustion_bar_count":     eng.exhaustion_bar_count,
            "exhaustion_persist_count": eng.exhaustion_persist_count,
            "reversal_confirm_count":   eng.reversal_confirm_count,
            "trend_reversal_confirm_count": eng.trend_reversal_confirm_count,
            "reversal_bar_count":       eng.reversal_bar_count,
            "no_motion_count":          eng.no_motion_count,
            "_exhaustion_s_decay_count": eng._exhaustion_s_decay_count,
            "trend_start_bar":          eng.trend_start_bar,
            "trend_start_price":        round(eng.trend_start_price, 8),
            "trend_start_atr":          round(eng.trend_start_atr, 8),
            "_exhaustion_phase_high":   round(eng._exhaustion_phase_high, 8),

            # Computed helpers
            "effective_stoploss_pct":   round(eng._effective_stoploss_pct(), 4),
            "effective_takeprofit_pct": round(eng._effective_takeprofit_pct(), 4),
            "global_stoploss_pct":      round(eng._global_stoploss_pct(), 4),

            # ── V2 decision snapshot at the exit bar (iter20 diagnostic) ──
            # Same getattr-guarded extractor used in _capture_entry_params.
            # Write-only; consumed by iter20 diagnostic & paired-diff analytics.
            **_v2_entry_snapshot(eng),
        }

    def _close_long(self, o: float, h: float, l: float, c: float, time: int, reason: str = "",
                    fill_price: Optional[float] = None) -> Optional[Trade]:
        """
        Close long position.

        Fill price resolution mirrors _open_long:
          - fill_price given (latency mode) → use it directly.
          - instant mode                    → the signal state's close.
          - legacy mode                     → intra-bar path at _fill_fraction();
            for an exit (sell) the fill price can only realistically be at or
            below the open.  Slippage is applied downward on top.
        """
        if self.current_trade is None:
            return None

        trade = self.current_trade
        assert trade is not None
        fees = self.total_fees_per_trade

        # 1. Resolve the raw fill price
        if fill_price is not None:
            raw_price = float(fill_price)
        elif self._exec_mode == "instant":
            raw_price = c
        else:
            frac = self._fill_fraction()
            #    For an exit (sell) we want the *bear* perspective of the intra-bar
            #    path: open → low → high → close, so adverse moves come first.
            raw_price = self._intrabar_price(o, h, l, c, frac)

        # 2. Add slippage (sell → price kicks down)
        slip = self.slippage_pct / 100.0
        exec_price = raw_price * (1.0 - slip)

        # iter66: optional execution-level calibration.  0.0 → ×1.0.
        if self.exec_offset_pct_sell:
            exec_price *= (1.0 - self.exec_offset_pct_sell / 100.0)

        # Proceeds at realistic slipped price
        proceeds = trade.size_tokens * exec_price

        # Slippage cost vs ideal open
        ideal_proceeds = trade.size_tokens * o
        exit_slippage_cost = max(0.0, ideal_proceeds - proceeds)

        # Deduct exit fees
        proceeds -= fees

        # PnL in SOL: actual proceeds vs SOL we put in
        pnl = proceeds - trade.size_sol

        # PnL % as net return on the SOL committed (includes fees & slippage),
        # so its sign always agrees with pnl_sol.
        pnl_pct = (pnl / trade.size_sol * 100.0) if trade.size_sol > 0 else 0.0

        trade.exit_time = time
        trade.exit_price = exec_price
        trade.pnl_sol = pnl
        trade.pnl_pct = pnl_pct
        trade.exit_reason = reason
        trade.fees_paid += fees
        trade.slippage_cost_sol += exit_slippage_cost

        # Update balance
        self.balance += proceeds

        # Update stats
        self.stats.current_balance = self.balance
        self.stats.total_trades += 1
        self.stats.total_pnl_sol += pnl
        self.stats.total_fees_paid += trade.fees_paid

        if pnl > 0:
            self.stats.winning_trades += 1
            trade.outcome = "W"
        else:
            self.stats.losing_trades += 1
            trade.outcome = "L"

        if self.stats.total_trades > 0:
            self.stats.win_rate = self.stats.winning_trades / self.stats.total_trades * 100

        if self.balance > self.stats.peak_balance:
            self.stats.peak_balance = self.balance

        drawdown = (
            (self.stats.peak_balance - self.balance) / self.stats.peak_balance * 100
            if self.stats.peak_balance > 0
            else 0
        )
        if drawdown > self.stats.max_drawdown_pct:
            self.stats.max_drawdown_pct = drawdown

        trade.exit_params = self._stashed_exit_params  # signal-candle snapshot
        self._stashed_exit_params = {}  # clear stash
        self.trade_history.append(trade)
        self.current_trade = None
        self.engine.notify_trade_closed()

        return trade

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
        """
        Process one candle state through strategy engine + forward tester.
        LONG-ONLY: only BUY to enter, EXIT to close.

        Execution models (see module docstring):
          instant (default): a signal produced by THIS state executes at THIS
            state's close price immediately — no pending queue, no n+1-bar
            wait.  Engine notified at the same intra-candle state, exactly
            like the live trader.
          latency: like instant, but the fill is deferred to
            t_signal + entry/exit_latency_seconds and priced on the recorded
            intra-candle path at that target (measured-latency overlay).
          legacy: the pre-2026-08-30 n+1 model — signals queue and execute at
            the next state with a fill_fraction mid-bar price.  Byte-identical
            to all historical baseline batches.

        When _build_full_result=False (backtester fast path), the strategy
        engine still processes the bar fully, but the expensive result dict
        construction (volume profiles, unrealised PnL, signal log) is
        skipped.  Trade execution is unaffected.
        """
        trade_action = None
        closed_trade = None
        opened_trade = None

        # ── Step 0: record the price path / resolve deferred fills ────────
        self._record_path_candle(time, o, h, l, c)
        if self._latency_pending is not None:
            action, trade = self._resolve_latency_fill(time, o, h, l, c)
            if action == "buy" and trade is not None:
                opened_trade = trade
                trade_action = "buy"
            elif action == "exit" and trade is not None:
                closed_trade = trade
                trade_action = "exit"

        # ── Step 1 (legacy only): Execute pending signal from the previous bar ──
        if self._exec_mode == "legacy":
            if self._pending_buy and self.current_trade is None:
                opened_trade = self._open_long(o, h, l, c, time, getattr(self, '_pending_buy_reason', 'buy'))
                if opened_trade:
                    trade_action = "buy"
                self._pending_buy = False

            elif self._pending_exit and self.current_trade is not None:
                closed_trade = self._close_long(o, h, l, c, time, self._pending_exit_reason)
                trade_action = "exit"
                self._pending_exit = False
                self._pending_exit_reason = ""

            # Guard: a just-opened trade shouldn't also be pending exit
            if opened_trade and self._pending_exit:
                self._pending_exit = False
                self._pending_exit_reason = ""

        # ── Step 2: Run strategy engine on this full candle ───────────────
        result = self.engine.update(time, o, h, l, c, volume,
                                    buy_volume=buy_volume,
                                    sell_volume=sell_volume,
                                    pool_sol=pool_sol,
                                    market_cap_usd=market_cap_usd,
                                    _build_full_result=_build_full_result)
        signal = result.get("signal", "none")
        regime = result.get("regime", "idle")

        # ── Step 3: Execute (instant) or queue (legacy / latency) ─────────
        # Snapshots are captured after engine.update so they reflect the
        # signal state's engine state.  In instant mode they are consumed by
        # the same-state fill; in latency/legacy mode they are stashed until
        # the fill resolves.
        if signal == Signal.BUY.value and self.current_trade is None \
                and not self._pending_buy and self._latency_pending is None:
            reason = f"buy_{regime}"
            if self._exec_mode == "instant":
                self._pending_exit = False
                self._pending_exit_reason = ""
                self._stashed_entry_params = self._capture_entry_params(c)
                opened_trade = self._open_long(o, h, l, c, time, reason)
                if opened_trade:
                    trade_action = "buy"
            elif self._exec_mode == "latency":
                self._stashed_entry_params = self._capture_entry_params(c)
                self._latency_pending = {
                    "side": "buy",
                    "target_ts": float(time) + self.entry_latency_seconds,
                    "reason": reason,
                }
            else:  # legacy: queue for the next state's open
                self._pending_buy = True
                self._pending_buy_reason = reason
                self._pending_exit = False
                self._stashed_entry_params = self._capture_entry_params(c)

        elif signal == Signal.EXIT.value and self.current_trade is not None \
                and self._latency_pending is None:
            reason = result.get("exit_reason")
            if not reason:
                reason = "exit_signal"
                if regime == Regime.REVERSAL.value:
                    reason = "reversal_exit"
                elif regime == Regime.EXHAUSTION.value:
                    reason = "exhaustion_exit"
                elif regime == Regime.CONTINUATION.value:
                    reason = "continuation_exit"
                elif regime == Regime.TREND.value:
                    reason = "trend_exit"

            if self._exec_mode == "instant":
                self._pending_buy = False
                self._pending_buy_reason = ""
                self._stashed_exit_params = self._capture_exit_params(c)
                closed_trade = self._close_long(o, h, l, c, time, reason)
                trade_action = "exit"
            elif self._exec_mode == "latency":
                # iter80: under armed_only, loss-book exits fill at the
                # signal instant exactly as the pre-iter80 model did.
                if self.exit_latency_armed_only and reason not in self._ARMED_EXIT_REASONS:
                    self._pending_buy = False
                    self._pending_buy_reason = ""
                    self._stashed_exit_params = self._capture_exit_params(c)
                    closed_trade = self._close_long(o, h, l, c, time, reason)
                    trade_action = "exit"
                else:
                    self._stashed_exit_params = self._capture_exit_params(c)
                    self._latency_pending = {
                        "side": "exit",
                        "target_ts": float(time) + self.exit_latency_seconds,
                        "reason": reason,
                    }
            else:  # legacy: queue for the next state's open
                self._pending_exit = True
                self._pending_exit_reason = reason
                self._pending_buy = False
                self._stashed_exit_params = self._capture_exit_params(c)

        # ── Fast path: skip expensive output construction ─────────────────
        if not _build_full_result:
            if trade_action:
                trade_label = ""
                if trade_action == "buy" and opened_trade:
                    trade_label = opened_trade.entry_reason
                elif trade_action == "exit" and closed_trade:
                    trade_label = closed_trade.exit_reason
                return {
                    "forward_test": {
                        "trade_action": trade_action,
                        "trade_label": trade_label,
                    }
                }
            return {}

        # ── Step 4: Unrealized PnL using realistic delayed/slipped prices ───────
        unrealized_pnl = 0.0
        unrealized_pnl_pct = 0.0
        current_trade = self.current_trade
        if current_trade is not None:
            # Simulate delay and slippage for hypothetical exit right now
            slip = self.slippage_pct / 100.0
            hypothetical_exit_price = c * (1.0 - slip)

            hypothetical_proceeds = current_trade.size_tokens * hypothetical_exit_price
            hypothetical_proceeds -= self.total_fees_per_trade
            unrealized_pnl = hypothetical_proceeds - current_trade.size_sol

            if current_trade.entry_price > 0:
                unrealized_pnl_pct = (
                    (hypothetical_exit_price - current_trade.entry_price)
                    / current_trade.entry_price
                    * 100
                )

        # ── Log executed action ───────────────────────────────────────────
        if trade_action:
            display_price = (
                opened_trade.entry_price if opened_trade else
                (closed_trade.exit_price if closed_trade else o)
            )
            self.signals_log.append({
                "time": time,
                "action": trade_action,
                "price": display_price,
                "regime": regime,
            })

        # ── Build output ──────────────────────────────────────────────────
        trade_label = ""
        if trade_action == "buy" and opened_trade:
            trade_label = opened_trade.entry_reason
        elif trade_action == "exit" and closed_trade:
            trade_label = closed_trade.exit_reason

        output = {
            **result,
            "forward_test": {
                "balance": round(self.balance, 6),
                "trade_action": trade_action,
                "trade_label": trade_label,
                "opened_trade": opened_trade.to_dict() if opened_trade else None,
                "closed_trade": closed_trade.to_dict() if closed_trade else None,
                "current_trade": self.current_trade.to_dict() if self.current_trade else None,
                "unrealized_pnl": round(unrealized_pnl, 6),
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                "stats": self.stats.to_dict(),
            },
        }

        return output
