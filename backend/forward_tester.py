"""
Forward Tester — Realistic LONG-ONLY trade simulation for the strategy engine.

Settings:
  - Starting balance: 1 SOL
  - Buy size: 0.1 SOL (10% of portfolio)
  - Priority fee: 0.0001 SOL
  - Bribe fee: 0.00001 SOL
  - Slippage: 10%
  - Trailing stop: +5% profit → lock stop at entry +5%

Execution model:
  - Signals from candle N are executed at the OPEN of candle N+1 (1-bar delay).
  - entry_price / exit_price stored in Trade are the RAW candle open prices
    (no slippage baked in).  This makes them directly comparable to what you
    see on the chart.
  - Slippage cost is deducted from the SOL proceeds as an explicit fee so
    that PnL in SOL is still realistic.
  - pnl_pct reflects the raw price move: (exit_price - entry_price) / entry_price
    so the percentage shown on the chart exactly matches visual price movement.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
from strategy_engine import StrategyEngine, Signal, Direction, Regime


@dataclass
class Trade:
    entry_time: int
    entry_price: float          # RAW candle open — what you see on the chart
    size_sol: float             # SOL committed to the trade
    size_tokens: float          # tokens bought (accounting for slippage)
    exit_time: Optional[int] = None
    exit_price: Optional[float] = None   # RAW candle open at exit
    pnl_sol: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    fees_paid: float = 0.0      # priority + bribe fees (not slippage)
    slippage_cost_sol: float = 0.0  # total SOL lost to slippage both ways

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

    def __init__(
        self,
        starting_balance: float = 1.0,
        buy_size_sol: float = 0.1,
        priority_fee: float = 0.0001,
        bribe_fee: float = 0.00001,
        slippage_pct: float = 1.0,
    ):
        self.engine = StrategyEngine()
        self.balance = starting_balance
        self.buy_size_sol = buy_size_sol
        self.priority_fee = priority_fee
        self.bribe_fee = bribe_fee
        self.slippage_pct = slippage_pct

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
        self._pending_exit: bool = False
        self._pending_exit_reason: str = ""

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

    def _open_long(self, raw_price: float, time: int) -> Optional[Trade]:
        """
        Open a long position.
        raw_price = candle open (what shows on the chart).
        Slippage is deducted as a cost but does NOT inflate the stored entry_price.
        """
        if self.current_trade is not None:
            return None
        if raw_price <= 0:
            return None

        fees = self.total_fees_per_trade
        trade_size = min(self.buy_size_sol, self.balance - fees)
        if trade_size <= 0:
            return None

        slip = self.slippage_pct / 100.0
        exec_price = raw_price * (1.0 + slip)   # actual fill price (internal)

        # Tokens received at slipped price
        tokens = trade_size / exec_price

        # Slippage cost in SOL = difference between ideal tokens and actual tokens, valued at raw price
        ideal_tokens = trade_size / raw_price
        slippage_cost = (ideal_tokens - tokens) * raw_price  # ≈ trade_size * slip/(1+slip)

        # Deduct trade size + fees from balance
        self.balance -= (trade_size + fees)

        trade = Trade(
            entry_time=time,
            entry_price=raw_price,          # CHART price — no slippage baked in
            size_sol=trade_size,
            size_tokens=tokens,
            fees_paid=fees,
            slippage_cost_sol=slippage_cost,
        )
        self.current_trade = trade
        self.engine.notify_trade_opened(raw_price, Direction.UP)
        return trade

    def _close_long(self, raw_price: float, time: int, reason: str = "") -> Optional[Trade]:
        """
        Close long position.
        raw_price = candle open at exit (what shows on the chart).
        """
        if self.current_trade is None:
            return None

        trade = self.current_trade
        assert trade is not None
        fees = self.total_fees_per_trade
        slip = self.slippage_pct / 100.0
        exec_price = raw_price * (1.0 - slip)   # actual fill price (internal)

        # Proceeds at slipped price
        proceeds = trade.size_tokens * exec_price

        # Slippage cost on exit
        ideal_proceeds = trade.size_tokens * raw_price
        exit_slippage_cost = ideal_proceeds - proceeds

        # Deduct exit fees
        proceeds -= fees

        # PnL in SOL: actual proceeds vs SOL we put in (includes slippage & fees)
        pnl = proceeds - trade.size_sol

        # PnL % exactly matches the raw chart move, bypassing slippage/fees
        # so chart labels accurately reflect the visual price change.
        pnl_pct = (raw_price - trade.entry_price) / trade.entry_price * 100.0 if trade.entry_price > 0 else 0.0

        trade.exit_time = time
        trade.exit_price = raw_price            # CHART price — no slippage baked in
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
        else:
            self.stats.losing_trades += 1

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
    ) -> dict:
        """
        Process one candle through strategy engine + forward tester.
        LONG-ONLY: only BUY to enter, EXIT to close.

        Execution model:
          1. Any pending signal from the PREVIOUS bar is executed at THIS
             candle's open price.  entry_price / exit_price will equal the
             open visible on the chart.
          2. The strategy engine evaluates THIS full candle and may queue
             a new pending signal for the NEXT candle.
        """
        trade_action = None
        closed_trade = None
        opened_trade = None

        # ── Step 1: Execute pending signal from the previous bar ──────────
        if self._pending_buy and self.current_trade is None:
            opened_trade = self._open_long(o, time)
            if opened_trade:
                trade_action = "buy"
            self._pending_buy = False

        elif self._pending_exit and self.current_trade is not None:
            closed_trade = self._close_long(o, time, self._pending_exit_reason)
            trade_action = "exit"
            self._pending_exit = False
            self._pending_exit_reason = ""

        # Guard: a just-opened trade shouldn't also be pending exit
        if opened_trade and self._pending_exit:
            self._pending_exit = False
            self._pending_exit_reason = ""

        # ── Step 2: Run strategy engine on this full candle ───────────────
        result = self.engine.update(time, o, h, l, c, volume)
        signal = result["signal"]
        regime = result["regime"]

        # ── Step 3: Queue signal for the NEXT candle's open ───────────────
        if signal == Signal.BUY.value and self.current_trade is None and not self._pending_buy:
            self._pending_buy = True
            self._pending_exit = False

        elif signal == Signal.EXIT.value and self.current_trade is not None:
            reason = "exit_signal"
            if regime == Regime.REVERSAL.value:
                reason = "reversal_exit"
            elif regime == Regime.EXHAUSTION.value:
                reason = "exhaustion_exit"
            self._pending_exit = True
            self._pending_exit_reason = reason
            self._pending_buy = False

        # ── Step 4: Unrealized PnL using raw prices (matches chart) ───────
        unrealized_pnl = 0.0
        unrealized_pnl_pct = 0.0
        if self.current_trade is not None:
            # Raw price move vs what we'd net after exit slippage + fees
            slip = self.slippage_pct / 100.0
            hypothetical_proceeds = self.current_trade.size_tokens * c * (1.0 - slip)
            hypothetical_proceeds -= self.total_fees_per_trade
            unrealized_pnl = hypothetical_proceeds - self.current_trade.size_sol
            if self.current_trade.entry_price > 0:
                # Show % as: how much has price moved since entry (raw)
                unrealized_pnl_pct = (
                    (c - self.current_trade.entry_price)
                    / self.current_trade.entry_price
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
        output = {
            **result,
            "forward_test": {
                "balance": round(self.balance, 6),
                "trade_action": trade_action,
                "opened_trade": opened_trade.to_dict() if opened_trade else None,
                "closed_trade": closed_trade.to_dict() if closed_trade else None,
                "current_trade": self.current_trade.to_dict() if self.current_trade else None,
                "unrealized_pnl": round(unrealized_pnl, 6),
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                "stats": self.stats.to_dict(),
            },
        }

        return output
