"""
Live Trader — Real on-chain Solana trading using the same StrategyEngine logic
as ForwardTester.

Architecture:
  - IMMEDIATE execution: signals execute on the SAME candle they fire
  - No 1-bar delay — speed is the priority for live trading
  - Instead of simulating fills, it emits swap requests to the frontend
  - Frontend signs transactions via Phantom wallet
  - Jupiter Aggregator V6 API used for optimal routing & minimum slippage
  - Execution speed is maximised: pre-built transactions, WebSocket comms

Settings (configurable via dashboard):
  - buy_size_sol: SOL per trade
  - slippage_bps: slippage tolerance in basis points (for Jupiter)
  - priority_fee_lamports: priority fee in lamports for faster inclusion
"""

from __future__ import annotations
import asyncio
import time
import logging
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

import aiohttp

from strategy_engine import StrategyEngine, Signal, Direction, Regime

logger = logging.getLogger("live-trader")

JUPITER_QUOTE_URL = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_URL = "https://quote-api.jup.ag/v6/swap"
WSOL_MINT = "So11111111111111111111111111111111111111112"


@dataclass
class LiveTrade:
    token_mint: str
    entry_time: float
    entry_price: float
    size_sol: float
    size_tokens: float
    tx_hash_buy: str = ""
    exit_time: Optional[float] = None
    exit_price: Optional[float] = None
    pnl_sol: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ""
    entry_reason: str = ""
    tx_hash_sell: str = ""
    status: str = "open"  # open, closing, closed, failed

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LiveTraderStats:
    starting_balance: float = 0.0
    current_balance: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_sol: float = 0.0
    total_fees_paid: float = 0.0
    max_drawdown_pct: float = 0.0
    peak_balance: float = 0.0
    win_rate: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class LiveTrader:
    """
    Real on-chain trader wrapping StrategyEngine.
    IMMEDIATE execution — signals fire and swap requests are emitted
    on the SAME candle, with zero delay.

    Instead of simulating fills, it:
      1. Runs StrategyEngine on current candle
      2. If BUY/EXIT signal fires → immediately creates swap request
      3. Sends swap request to frontend via WebSocket
      4. Frontend signs with Phantom and broadcasts

    LONG-ONLY: buys tokens with SOL, sells tokens back to SOL.
    """

    def __init__(
        self,
        token_mint: str,
        wallet_pubkey: str,
        buy_size_sol: float = 0.1,
        slippage_bps: int = 1000,  # 10% default
        priority_fee_lamports: int = 100_000,  # 0.0001 SOL
        engine_kwargs: Optional[dict] = None,
    ):
        if engine_kwargs is None:
            engine_kwargs = {}
        self.engine = StrategyEngine(**engine_kwargs)
        self.token_mint = token_mint
        self.wallet_pubkey = wallet_pubkey
        self.buy_size_sol = buy_size_sol
        self.slippage_bps = slippage_bps
        self.priority_fee_lamports = priority_fee_lamports

        self.stats = LiveTraderStats()
        self.current_trade: Optional[LiveTrade] = None
        self.trade_history: list[LiveTrade] = []
        self.signals_log: list[dict] = []

        # Swap action queue — frontend picks these up
        self._pending_swap: Optional[dict] = None

        # Track the last known price for unrealised PnL
        self._last_price: float = 0.0

        # Token decimals cache
        self._token_decimals: int = 6  # default for pump.fun tokens

    async def get_jupiter_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
    ) -> Optional[dict]:
        """Fetch a swap quote from Jupiter V6 API."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_lamports),
            "slippageBps": str(self.slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    JUPITER_QUOTE_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"Jupiter quote failed: {resp.status} {text}")
                        return None
                    return await resp.json()
        except Exception as e:
            logger.error(f"Jupiter quote error: {e}")
            return None

    async def get_jupiter_swap_tx(
        self,
        quote: dict,
    ) -> Optional[str]:
        """Get serialised swap transaction from Jupiter."""
        body = {
            "quoteResponse": quote,
            "userPublicKey": self.wallet_pubkey,
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": self.priority_fee_lamports,
            "dynamicComputeUnitLimit": True,
            "asLegacyTransaction": False,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    JUPITER_SWAP_URL,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error(f"Jupiter swap failed: {resp.status} {text}")
                        return None
                    data = await resp.json()
                    return data.get("swapTransaction")
        except Exception as e:
            logger.error(f"Jupiter swap error: {e}")
            return None

    def update(
        self,
        time_val: int,
        o: float,
        h: float,
        l: float,
        c: float,
        volume: float = 0.0,
    ) -> dict:
        """
        Process one candle through strategy engine + live trader.
        IMMEDIATE execution — no 1-bar delay.

        Returns dict with strategy results + live_trade state.
        The 'swap_request' field (if present) tells frontend to execute a swap.
        """
        self._last_price = c
        trade_action = None
        opened_trade = None
        swap_request = None

        # ── Step 1: Run strategy engine on this candle ────────────────────
        result = self.engine.update(time_val, o, h, l, c, volume)
        signal = result["signal"]
        regime = result["regime"]

        # ── Step 2: IMMEDIATELY execute signal on this candle ─────────────
        if signal == Signal.BUY.value and self.current_trade is None:
            # BUY signal → create swap request RIGHT NOW at current price
            buy_reason = f"buy_{regime}"
            amount_lamports = int(self.buy_size_sol * 1e9)
            swap_request = {
                "action": "buy",
                "input_mint": WSOL_MINT,
                "output_mint": self.token_mint,
                "amount_lamports": amount_lamports,
                "slippage_bps": self.slippage_bps,
                "priority_fee": self.priority_fee_lamports,
                "reason": buy_reason,
                "price_at_signal": c,
                "time": time_val,
            }
            self._pending_swap = swap_request
            # Create placeholder trade (will be updated when tx confirms)
            trade = LiveTrade(
                token_mint=self.token_mint,
                entry_time=time_val,
                entry_price=c,
                size_sol=self.buy_size_sol,
                size_tokens=0,  # updated on confirmation
                entry_reason=buy_reason,
            )
            self.current_trade = trade
            self.engine.notify_trade_opened(c, Direction.UP)
            opened_trade = trade
            trade_action = "buy"

        elif signal == Signal.EXIT.value and self.current_trade is not None:
            # EXIT signal → create sell swap request RIGHT NOW
            exit_reason = "exit_signal"
            if regime == Regime.REVERSAL.value:
                exit_reason = "reversal_exit"
            elif regime == Regime.EXHAUSTION.value:
                exit_reason = "exhaustion_exit"
            elif regime == Regime.CONTINUATION.value:
                exit_reason = "continuation_exit"
            elif regime == Regime.TREND.value:
                exit_reason = "trend_exit"
            elif self.engine.trailing_stop is not None and c <= self.engine.trailing_stop:
                exit_reason = "trailing_stop"

            swap_request = {
                "action": "sell",
                "input_mint": self.token_mint,
                "output_mint": WSOL_MINT,
                "amount_lamports": 0,  # sell ALL tokens (handled by frontend)
                "slippage_bps": self.slippage_bps,
                "priority_fee": self.priority_fee_lamports,
                "reason": exit_reason,
                "price_at_signal": c,
                "time": time_val,
                "sell_all": True,
            }
            self._pending_swap = swap_request
            self.current_trade.status = "closing"
            self.current_trade.exit_reason = exit_reason
            trade_action = "exit"

        # ── Step 3: Unrealized PnL ─────────────────────────────────────────
        unrealized_pnl = 0.0
        unrealized_pnl_pct = 0.0
        if self.current_trade is not None and self.current_trade.entry_price > 0:
            unrealized_pnl_pct = (c - self.current_trade.entry_price) / self.current_trade.entry_price * 100
            unrealized_pnl = self.current_trade.size_sol * (unrealized_pnl_pct / 100)

        # ── Log executed action ───────────────────────────────────────────
        if trade_action:
            self.signals_log.append({
                "time": time_val,
                "action": trade_action,
                "price": c,
                "regime": regime,
            })

        # ── Build output ──────────────────────────────────────────────────
        trade_label = ""
        if trade_action == "buy" and opened_trade:
            trade_label = opened_trade.entry_reason
        elif trade_action == "exit" and self.current_trade:
            trade_label = self.current_trade.exit_reason or "exit"

        output = {
            **result,
            "live_trade": {
                "balance": round(self.stats.current_balance, 6),
                "trade_action": trade_action,
                "trade_label": trade_label,
                "opened_trade": opened_trade.to_dict() if opened_trade else None,
                "current_trade": self.current_trade.to_dict() if self.current_trade else None,
                "unrealized_pnl": round(unrealized_pnl, 6),
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                "stats": self.stats.to_dict(),
                "swap_request": swap_request,
            },
        }

        return output

    def confirm_buy(self, tx_hash: str, tokens_received: float, actual_price: float):
        """Called when frontend confirms a buy transaction."""
        if self.current_trade is None:
            return
        self.current_trade.tx_hash_buy = tx_hash
        self.current_trade.size_tokens = tokens_received
        self.current_trade.entry_price = actual_price
        self.current_trade.status = "open"
        logger.info(f"BUY confirmed: {tx_hash[:16]}... got {tokens_received:.4f} tokens @ {actual_price}")

    def confirm_sell(self, tx_hash: str, sol_received: float, actual_price: float):
        """Called when frontend confirms a sell transaction."""
        if self.current_trade is None:
            return
        trade = self.current_trade
        trade.tx_hash_sell = tx_hash
        trade.exit_price = actual_price
        trade.exit_time = time.time()
        trade.pnl_sol = sol_received - trade.size_sol
        if trade.entry_price > 0:
            trade.pnl_pct = (actual_price - trade.entry_price) / trade.entry_price * 100
        trade.status = "closed"

        # Update stats
        self.stats.total_trades += 1
        self.stats.total_pnl_sol += trade.pnl_sol
        if trade.pnl_sol > 0:
            self.stats.winning_trades += 1
        else:
            self.stats.losing_trades += 1
        if self.stats.total_trades > 0:
            self.stats.win_rate = self.stats.winning_trades / self.stats.total_trades * 100

        self.stats.current_balance += trade.pnl_sol
        if self.stats.current_balance > self.stats.peak_balance:
            self.stats.peak_balance = self.stats.current_balance
        drawdown = (
            (self.stats.peak_balance - self.stats.current_balance) / self.stats.peak_balance * 100
            if self.stats.peak_balance > 0 else 0
        )
        if drawdown > self.stats.max_drawdown_pct:
            self.stats.max_drawdown_pct = drawdown

        self.trade_history.append(trade)
        self.current_trade = None
        self.engine.notify_trade_closed()
        logger.info(f"SELL confirmed: {tx_hash[:16]}... PnL: {trade.pnl_sol:+.6f} SOL ({trade.pnl_pct:+.2f}%)")

    def confirm_failed(self, action: str, error: str):
        """Called when a swap transaction fails."""
        logger.error(f"Swap FAILED ({action}): {error}")
        if action == "buy" and self.current_trade is not None:
            # Revert — no trade was opened
            self.current_trade = None
            self.engine.notify_trade_closed()
        elif action == "sell" and self.current_trade is not None:
            self.current_trade.status = "open"  # revert to open
