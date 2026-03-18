"""
Forward Tester — Realistic LONG-ONLY trade simulation for the strategy engine.

Settings:
  - Starting balance: 1 SOL
  - Buy size: 0.1 SOL (10% of portfolio)
  - Priority fee: 0.0001 SOL
  - Bribe fee: 0.00001 SOL
  - Slippage: 10%
  - Trailing stop: +5% profit → lock stop at entry +5%
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
from strategy_engine import StrategyEngine, Signal, Direction, Regime


@dataclass
class Trade:
    entry_time: int
    entry_price: float
    size_sol: float
    size_tokens: float
    exit_time: Optional[int] = None
    exit_price: Optional[float] = None
    pnl_sol: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    fees_paid: float = 0.0

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
        slippage_pct: float = 10.0,
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

    @property
    def total_fees_per_trade(self) -> float:
        return self.priority_fee + self.bribe_fee

    def _apply_slippage(self, price: float, is_buy: bool) -> float:
        """Apply slippage to execution price."""
        slip = self.slippage_pct / 100.0
        if is_buy:
            return price * (1 + slip)  # Pay more when buying
        else:
            return price * (1 - slip)  # Get less when selling

    def _open_long(self, price: float, time: int) -> Optional[Trade]:
        """Open a long position (buy tokens with SOL)."""
        if self.current_trade is not None:
            return None  # Already in a trade

        fees = self.total_fees_per_trade
        trade_size = min(self.buy_size_sol, self.balance - fees)
        if trade_size <= 0:
            return None  # Not enough balance

        exec_price = self._apply_slippage(price, is_buy=True)

        # Deduct trade size + fees from balance
        self.balance -= (trade_size + fees)

        # Calculate tokens bought
        tokens = trade_size / exec_price if exec_price > 0 else 0

        trade = Trade(
            entry_time=time,
            entry_price=exec_price,
            size_sol=trade_size,
            size_tokens=tokens,
            fees_paid=fees,
        )
        self.current_trade = trade
        self.engine.notify_trade_opened(exec_price, Direction.UP)
        return trade

    def _close_long(self, price: float, time: int, reason: str = "") -> Optional[Trade]:
        """Close long position (sell tokens back to SOL)."""
        if self.current_trade is None:
            return None

        trade = self.current_trade
        fees = self.total_fees_per_trade
        exec_price = self._apply_slippage(price, is_buy=False)

        # Proceeds = tokens * sell price
        proceeds = trade.size_tokens * exec_price

        # Deduct exit fees
        proceeds -= fees

        # PnL
        pnl = proceeds - trade.size_sol
        pnl_pct = (pnl / trade.size_sol * 100) if trade.size_sol > 0 else 0

        trade.exit_time = time
        trade.exit_price = exec_price
        trade.pnl_sol = pnl
        trade.pnl_pct = pnl_pct
        trade.exit_reason = reason
        trade.fees_paid += fees

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
        """
        # Run strategy engine
        result = self.engine.update(time, o, h, l, c, volume)
        signal = result["signal"]
        regime = result["regime"]

        trade_action = None
        closed_trade = None
        opened_trade = None

        # Handle signals — LONG ONLY
        if signal == Signal.BUY.value:
            if self.current_trade is None:
                opened_trade = self._open_long(c, time)
                if opened_trade:
                    trade_action = "buy"

        elif signal == Signal.EXIT.value:
            if self.current_trade is not None:
                reason = "exit_signal"
                if regime == Regime.REVERSAL.value:
                    reason = "reversal_exit"
                elif regime == Regime.EXHAUSTION.value:
                    reason = "exhaustion_exit"
                closed_trade = self._close_long(c, time, reason)
                trade_action = "exit"

        # Calculate unrealized PnL (long only)
        unrealized_pnl = 0.0
        unrealized_pnl_pct = 0.0
        if self.current_trade is not None:
            unrealized_pnl = (
                (c - self.current_trade.entry_price) * self.current_trade.size_tokens
            )
            if self.current_trade.entry_price > 0:
                unrealized_pnl_pct = (
                    (c - self.current_trade.entry_price)
                    / self.current_trade.entry_price
                    * 100
                )

        # Log signal
        if trade_action:
            self.signals_log.append({
                "time": time,
                "action": trade_action,
                "price": c,
                "regime": regime,
            })

        # Build output
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
