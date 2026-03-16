"""
Trade Simulator — realistic forward-testing execution engine.

Simulates PumpFun Solana memecoin trading with:
  - 10% slippage per transaction
  - 0.00001 SOL bribe per tx
  - 0.0001 SOL priority fee per tx
  - Transaction delay simulation
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Position:
    direction: str = ""        # "LONG" or "SHORT" (for memecoins, mostly LONG)
    entry_price: float = 0.0   # actual fill price after slippage
    market_entry_price: float = 0.0  # market price at signal time
    size_sol: float = 0.0      # SOL committed
    size_tokens: float = 0.0   # tokens received
    entry_time: float = 0.0
    entry_bar: int = 0

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "entry_price": self.entry_price,
            "market_entry_price": self.market_entry_price,
            "size_sol": round(self.size_sol, 6),
            "size_tokens": round(self.size_tokens, 4),
            "entry_time": self.entry_time,
        }


@dataclass
class TradeRecord:
    direction: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    entry_market_price: float = 0.0
    exit_market_price: float = 0.0
    size_sol: float = 0.0
    pnl_sol: float = 0.0
    fees_sol: float = 0.0
    entry_time: float = 0.0
    exit_time: float = 0.0
    slippage_cost: float = 0.0

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "size_sol": round(self.size_sol, 6),
            "pnl_sol": round(self.pnl_sol, 6),
            "fees_sol": round(self.fees_sol, 6),
            "slippage_cost": round(self.slippage_cost, 6),
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
        }


class TradeSimulator:
    """
    Simulates trade execution with realistic Solana memecoin costs.
    """

    SLIPPAGE_PCT   = 0.10      # 10% slippage
    BRIBE_SOL      = 0.00001   # per tx
    PRIORITY_SOL   = 0.0001    # per tx
    TX_DELAY_MIN   = 0.3       # seconds
    TX_DELAY_MAX   = 2.0       # seconds
    TRADE_PCT      = 0.50      # use 50% of balance per trade

    def __init__(self, starting_balance: float = 1.0):
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.position: Optional[Position] = None
        self.trades: list[TradeRecord] = []
        self.total_fees: float = 0.0
        self.total_slippage_cost: float = 0.0
        self._pending_action: Optional[dict] = None
        self._pending_until: float = 0.0

    @property
    def fees_per_tx(self) -> float:
        return self.BRIBE_SOL + self.PRIORITY_SOL

    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl_sol for t in self.trades)

    @property
    def unrealized_pnl(self) -> float:
        if not self.position:
            return 0.0
        return 0.0  # Can't compute without current price

    def unrealized_pnl_at(self, current_price: float) -> float:
        if not self.position or current_price <= 0:
            return 0.0
        if self.position.direction == "LONG":
            # tokens * current_price gives value in SOL
            current_value_sol = self.position.size_tokens * current_price
            return current_value_sol - self.position.size_sol
        return 0.0

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl_sol > 0)
        return wins / len(self.trades)

    def process_signal(
        self,
        signal: str,           # "BUY" or "SELL"
        market_price: float,   # current market price (SOL per token)
        timestamp: float,
        bar_count: int = 0,
    ) -> Optional[dict]:
        """
        Process an entry/exit signal. Returns trade execution details or None.
        """
        if signal == "BUY" and not self.position:
            return self._execute_buy(market_price, timestamp, bar_count)
        elif signal == "SELL" and self.position:
            return self._execute_sell(market_price, timestamp, bar_count)
        elif signal == "BUY" and self.position:
            # Already in position — no double entry
            return None
        elif signal == "SELL" and not self.position:
            # No position to exit — ignore
            return None
        return None

    def _execute_buy(self, market_price: float, timestamp: float, bar_count: int) -> dict:
        """Execute a BUY with slippage and fees."""
        fees = self.fees_per_tx
        if self.balance <= fees:
            return {"error": "insufficient_balance", "balance": self.balance}

        # How much SOL to spend
        available = self.balance - fees
        trade_sol = available * self.TRADE_PCT

        # Slippage: buy at a WORSE (higher) price
        fill_price = market_price * (1 + self.SLIPPAGE_PCT)
        slippage_cost = trade_sol * self.SLIPPAGE_PCT  # approximate cost

        # Tokens received
        tokens = trade_sol / fill_price

        # Simulated transaction delay
        tx_delay = random.uniform(self.TX_DELAY_MIN, self.TX_DELAY_MAX)

        # Create position
        self.position = Position(
            direction="LONG",
            entry_price=fill_price,
            market_entry_price=market_price,
            size_sol=trade_sol,
            size_tokens=tokens,
            entry_time=timestamp,
            entry_bar=bar_count,
        )

        # Deduct from balance
        self.balance -= (trade_sol + fees)
        self.total_fees += fees
        self.total_slippage_cost += slippage_cost

        return {
            "action": "BUY",
            "market_price": market_price,
            "fill_price": fill_price,
            "amount_sol": round(trade_sol, 6),
            "tokens_received": round(tokens, 4),
            "fees": round(fees, 6),
            "slippage_cost": round(slippage_cost, 6),
            "tx_delay": round(tx_delay, 2),
            "position": self.position.to_dict(),
            "balance": round(self.balance, 6),
            "total_pnl": round(self.realized_pnl, 6),
            "trade_count": self.trade_count,
        }

    def _execute_sell(self, market_price: float, timestamp: float, bar_count: int) -> dict:
        """Execute a SELL (close position) with slippage and fees."""
        if not self.position:
            return {"error": "no_position"}

        fees = self.fees_per_tx

        # Slippage: sell at a WORSE (lower) price
        fill_price = market_price * (1 - self.SLIPPAGE_PCT)

        # Proceeds
        proceeds_sol = self.position.size_tokens * fill_price
        slippage_cost = self.position.size_tokens * market_price * self.SLIPPAGE_PCT

        # PnL
        pnl = proceeds_sol - self.position.size_sol - fees

        # Transaction delay
        tx_delay = random.uniform(self.TX_DELAY_MIN, self.TX_DELAY_MAX)

        # Record trade
        record = TradeRecord(
            direction=self.position.direction,
            entry_price=self.position.entry_price,
            exit_price=fill_price,
            entry_market_price=self.position.market_entry_price,
            exit_market_price=market_price,
            size_sol=self.position.size_sol,
            pnl_sol=pnl,
            fees_sol=fees + self.fees_per_tx,  # entry + exit fees
            entry_time=self.position.entry_time,
            exit_time=timestamp,
            slippage_cost=slippage_cost,
        )
        self.trades.append(record)

        # Update balance
        self.balance += proceeds_sol - fees
        self.total_fees += fees
        self.total_slippage_cost += slippage_cost

        result = {
            "action": "SELL",
            "market_price": market_price,
            "fill_price": fill_price,
            "proceeds_sol": round(proceeds_sol, 6),
            "fees": round(fees, 6),
            "slippage_cost": round(slippage_cost, 6),
            "pnl": round(pnl, 6),
            "tx_delay": round(tx_delay, 2),
            "position": None,
            "balance": round(self.balance, 6),
            "total_pnl": round(self.realized_pnl, 6),
            "trade_count": self.trade_count,
            "win_rate": round(self.win_rate, 4),
            "trade": record.to_dict(),
        }

        self.position = None
        return result

    def snapshot(self, current_price: float = 0.0) -> dict:
        """Current simulator state."""
        return {
            "balance": round(self.balance, 6),
            "starting_balance": self.starting_balance,
            "in_position": self.position is not None,
            "position": self.position.to_dict() if self.position else None,
            "realized_pnl": round(self.realized_pnl, 6),
            "unrealized_pnl": round(self.unrealized_pnl_at(current_price), 6),
            "total_pnl": round(self.realized_pnl + self.unrealized_pnl_at(current_price), 6),
            "total_fees": round(self.total_fees, 6),
            "total_slippage_cost": round(self.total_slippage_cost, 6),
            "trade_count": self.trade_count,
            "win_rate": round(self.win_rate, 4),
        }
