"""
Forward Tester — Realistic LONG-ONLY trade simulation for the strategy engine.

Settings:
  - Starting balance: 1 SOL
  - Buy size: 0.1 SOL (10% of portfolio)
  - Priority fee: 0.0001 SOL
  - Bribe fee: 0.00001 SOL
  - Slippage: 10%

Execution model:
  - Signals from candle N are executed at the OPEN of candle N+1 (1-bar delay).
    (1-bar delay), but the fill price realistically interpolates between 
    Open and Close based on Priority Fees (simulating execution delay).
  - Then, Slippage is applied to this delayed price.
  - The final hyper-realistic price is stored as entry_price / exit_price,
    so the chart reflects what you actually got filled at.
  - pnl_pct reflects the performance based on these final real prices.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
from strategy_engine import StrategyEngine, Signal, Direction, Regime


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
        engine_kwargs: Optional[dict] = None,
    ):
        if engine_kwargs is None:
            engine_kwargs = {}
        self.engine = StrategyEngine(**engine_kwargs)
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
        self._pending_buy_reason: str = ""
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

    def _open_long(self, o: float, h: float, l: float, c: float, time: int, reason: str = "") -> Optional[Trade]:
        """
        Open a long position.
        Calculates realistic execution price factoring in priority-fee based delay
        and slippage, storing the final real price.
        """
        if self.current_trade is not None:
            return None
        if o <= 0:
            return None

        # 1. Simulate delay: penalty moves price towards the TOP of the execution candle
        total_fee = self.priority_fee + self.bribe_fee
        delay_fraction = 0.000005 / max(total_fee, 1e-9)
        delay_fraction = max(0.01, min(1.0, delay_fraction))
        
        # The penalty moves price from Open to the High (worst realistic price for a buy)
        delayed_price = o + (h - o) * delay_fraction

        # 2. Add slippage
        slip = self.slippage_pct / 100.0
        exec_price = delayed_price * (1.0 + slip)   # hyper-realistic fill price

        fees = self.total_fees_per_trade
        trade_size = min(self.buy_size_sol, self.balance - fees)
        if trade_size <= 0:
            return None

        # Tokens received at exact filled price
        tokens = trade_size / exec_price

        # Slippage/delay cost vs ideal Raw Open
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
        )
        self.current_trade = trade
        self.engine.notify_trade_opened(exec_price, Direction.UP)
        return trade

    def _close_long(self, o: float, h: float, l: float, c: float, time: int, reason: str = "") -> Optional[Trade]:
        """
        Close long position.
        Calculates realistic execution price factoring in priority-fee based delay
        and slippage, storing the final real price.
        """
        if self.current_trade is None:
            return None

        trade = self.current_trade
        assert trade is not None
        fees = self.total_fees_per_trade
        
        # 1. Simulate delay: penalty moves price towards the BOTTOM of the execution candle
        total_fee = self.priority_fee + self.bribe_fee
        delay_fraction = 0.000005 / max(total_fee, 1e-9)
        delay_fraction = max(0.01, min(1.0, delay_fraction))
        
        # The penalty moves price from Open to the Low (worst realistic price for a sell)
        delayed_price = o - (o - l) * delay_fraction

        # 2. Add slippage
        slip = self.slippage_pct / 100.0
        exec_price = delayed_price * (1.0 - slip)   # actual fill price

        # Proceeds at realistic slipped price
        proceeds = trade.size_tokens * exec_price

        # Slippage cost vs ideal
        ideal_proceeds = trade.size_tokens * o
        exit_slippage_cost = max(0.0, ideal_proceeds - proceeds)

        # Deduct exit fees
        proceeds -= fees

        # PnL in SOL: actual proceeds vs SOL we put in (includes slippage & fees)
        pnl = proceeds - trade.size_sol

        # PnL % using the realistic slipped entry/exit prices
        pnl_pct = (exec_price - trade.entry_price) / trade.entry_price * 100.0 if trade.entry_price > 0 else 0.0

        trade.exit_time = time
        trade.exit_price = exec_price            # CHART price — realistic, includes delay + slippage
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
        _build_full_result: bool = True,
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

        When _build_full_result=False (backtester fast path), the strategy
        engine still processes the bar fully, but the expensive result dict
        construction (volume profiles, unrealised PnL, signal log) is
        skipped.  Trade execution is unaffected.
        """
        trade_action = None
        closed_trade = None
        opened_trade = None

        # ── Step 1: Execute pending signal from the previous bar ──────────
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
                                    _build_full_result=_build_full_result)
        signal = result.get("signal", "none")
        regime = result.get("regime", "idle")

        # ── Step 3: Queue signal for the NEXT candle's open ───────────────
        if signal == Signal.BUY.value and self.current_trade is None and not self._pending_buy:
            self._pending_buy = True
            self._pending_buy_reason = f"buy_{regime}"
            self._pending_exit = False

        elif signal == Signal.EXIT.value and self.current_trade is not None:
            reason = "exit_signal"
            if regime == Regime.REVERSAL.value:
                reason = "reversal_exit"
            elif regime == Regime.EXHAUSTION.value:
                reason = "exhaustion_exit"
            elif regime == Regime.CONTINUATION.value:
                reason = "continuation_exit"
            elif regime == Regime.TREND.value:
                reason = "trend_exit"
                
            self._pending_exit = True
            self._pending_exit_reason = reason
            self._pending_buy = False

        # ── Fast path: skip expensive output construction ─────────────────
        if not _build_full_result:
            # Return minimal dict — only trade_action is needed by backtester
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
