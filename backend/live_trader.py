"""
Live Trader — Real on-chain Solana trading using the same StrategyEngine logic
as ForwardTester.

Architecture (private-key mode):
  - NO browser wallet required — transactions are signed server-side
  - Private key is accepted as base58 string via the dashboard API
  - Full cycle: Jupiter quote → swap TX → sign (solders) → broadcast to RPC
  - IMMEDIATE execution: signals fire on the SAME candle
  - Jupiter Aggregator V6 for optimal routing
  - Configurable slippage, priority fee, buy size
"""

from __future__ import annotations
import asyncio
import base64
import time
import logging
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

import aiohttp
import base58
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from strategy_engine import StrategyEngine, Signal, Direction, Regime

logger = logging.getLogger("live-trader")

# ── Jupiter & Solana constants ────────────────────────────────────────────────
JUPITER_QUOTE_URL = "https://public.jupiterapi.com/quote"
JUPITER_SWAP_URL  = "https://public.jupiterapi.com/swap"
SOLANA_RPC        = "https://api.mainnet-beta.solana.com"
WSOL_MINT         = "So11111111111111111111111111111111111111112"


def keypair_from_private_key(pk_b58: str) -> Keypair:
    """
    Construct a solders Keypair from a base58-encoded private key string.
    Phantom exports private keys as a 64-byte base58 string (secret + public).
    """
    raw = base58.b58decode(pk_b58)
    if len(raw) == 64:
        return Keypair.from_bytes(raw)
    elif len(raw) == 32:
        return Keypair.from_seed(raw)
    raise ValueError(f"Invalid private key length: {len(raw)} bytes (expected 32 or 64)")


# ── Data classes ──────────────────────────────────────────────────────────────

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


# ── Main LiveTrader class ─────────────────────────────────────────────────────

class LiveTrader:
    """
    Real on-chain trader wrapping StrategyEngine.

    Signs and broadcasts all transactions server-side using solders.
    No browser wallet extension required.

    Flow:
      1. StrategyEngine fires BUY/EXIT signal on new candle
      2. Jupiter quote fetched asynchronously
      3. Swap transaction built by Jupiter
      4. Transaction signed with private key (solders Keypair)
      5. Signed TX broadcast directly to Solana RPC
      6. Result logged and state updated
    """

    def __init__(
        self,
        token_mint: str,
        keypair: Keypair,
        buy_size_sol: float = 0.1,
        slippage_bps: int = 1000,
        priority_fee_lamports: int = 100_000,
        engine_kwargs: Optional[dict] = None,
    ):
        if engine_kwargs is None:
            engine_kwargs = {}

        self.engine = StrategyEngine(**engine_kwargs)
        self.token_mint = token_mint
        self.keypair = keypair
        self.wallet_pubkey = str(keypair.pubkey())
        self.buy_size_sol = buy_size_sol
        self.slippage_bps = min(slippage_bps, 10000)
        self.priority_fee_lamports = priority_fee_lamports

        self.stats = LiveTraderStats()
        self.current_trade: Optional[LiveTrade] = None
        self.trade_history: list[LiveTrade] = []
        self.signals_log: list[dict] = []

        self._last_price: float = 0.0
        self._token_decimals: int = 6  # pump.fun default
        self._token_balance: int = 0   # raw token units held

        # Async swap task tracker (so we don't overlap swaps)
        self._swap_in_flight: bool = False

        # Websocket broadcast callback (set by main.py)
        self.broadcast_fn = None

    # ── Jupiter helpers ───────────────────────────────────────────────────────

    async def _get_quote(self, input_mint: str, output_mint: str, amount: int) -> Optional[dict]:
        """Fetch a Jupiter V6 quote."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(self.slippage_bps),
            "onlyDirectRoutes": "false",
            "asLegacyTransaction": "false",
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    JUPITER_QUOTE_URL,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    if r.status != 200:
                        logger.error(f"Jupiter quote HTTP {r.status}: {await r.text()}")
                        return None
                    return await r.json()
        except Exception as e:
            logger.error(f"Jupiter quote error: {e}")
            return None

    async def _get_swap_tx(self, quote: dict) -> Optional[str]:
        """Build a versioned swap transaction via Jupiter."""
        body = {
            "quoteResponse": quote,
            "userPublicKey": self.wallet_pubkey,
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": self.priority_fee_lamports,
            "dynamicComputeUnitLimit": True,
            "asLegacyTransaction": False,
            "dynamicSlippage": {"maxBps": self.slippage_bps},
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    JUPITER_SWAP_URL,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    if r.status != 200:
                        logger.error(f"Jupiter swap HTTP {r.status}: {await r.text()}")
                        return None
                    data = await r.json()
                    return data.get("swapTransaction")
        except Exception as e:
            logger.error(f"Jupiter swap error: {e}")
            return None

    async def _sign_and_send(self, swap_tx_b64: str) -> Optional[str]:
        """
        Sign the base64-encoded versioned transaction and broadcast it to Solana RPC.
        Returns the transaction signature (tx hash) on success.
        """
        try:
            raw_tx = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(raw_tx)

            # Sign with our keypair
            signed_tx = VersionedTransaction(tx.message, [self.keypair])
            signed_bytes = bytes(signed_tx)
            signed_b64 = base64.b64encode(signed_bytes).decode()

            # Broadcast to Solana RPC
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    signed_b64,
                    {
                        "encoding": "base64",
                        "skipPreflight": True,
                        "preflightCommitment": "processed",
                        "maxRetries": 5,
                    },
                ],
            }
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    SOLANA_RPC,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    data = await r.json()
                    if "error" in data:
                        logger.error(f"RPC sendTransaction error: {data['error']}")
                        return None
                    sig = data.get("result")
                    logger.info(f"TX broadcast: {sig}")
                    return sig
        except Exception as e:
            logger.error(f"Sign/send error: {e}")
            return None

    async def _get_sol_balance(self) -> float:
        """Fetch wallet SOL balance from RPC."""
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [self.wallet_pubkey, {"commitment": "processed"}],
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(SOLANA_RPC, json=payload,
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json()
                    return float(data.get("result", {}).get("value", 0)) / 1e9
        except Exception:
            pass
        return 0.0

    async def _get_token_balance(self) -> int:
        """Fetch raw token balance (in smallest units) for our wallet."""
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                self.wallet_pubkey,
                {"mint": self.token_mint},
                {"encoding": "jsonParsed"},
            ],
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(SOLANA_RPC, json=payload,
                                   timeout=aiohttp.ClientTimeout(total=5)) as r:
                    data = await r.json()
                    accounts = data.get("result", {}).get("value", [])
                    if not accounts:
                        return 0
                    parsed = accounts[0]["account"]["data"]["parsed"]
                    return int(parsed["info"]["tokenAmount"]["amount"])
        except Exception:
            pass
        return 0

    # ── Swap execution ────────────────────────────────────────────────────────

    async def execute_buy(self, reason: str = "signal") -> Optional[str]:
        """
        Full buy cycle: quote → swap TX → sign → broadcast.
        Returns tx signature on success, None on failure.
        """
        if self._swap_in_flight:
            logger.warning("Swap already in flight — skipping buy")
            return None
        self._swap_in_flight = True
        try:
            amount_lam = int(self.buy_size_sol * 1e9)
            mint_str = str(self.token_mint)

            # Sanity balance check to avoid confusing RPC simulation errors
            sol_bal = await self._get_sol_balance()
            if sol_bal * 1e9 < amount_lam + 5000:  # buy size + rough gas buffer
                logger.error(f"[BUY FAILED] Insufficient balance: {sol_bal:.4f} SOL but need ~{self.buy_size_sol} SOL")
                await self._broadcast_status("buy_failed", f"Insufficient SOL ({sol_bal:.4f} available)", reason)
                return None

            logger.info(f"[BUY] {mint_str[:8]}… {self.buy_size_sol} SOL reason={reason}")

            quote = await self._get_quote(WSOL_MINT, self.token_mint, amount_lam)
            if not quote:
                await self._broadcast_status("buy_failed", "Jupiter quote failed", reason)
                return None

            swap_tx = await self._get_swap_tx(quote)
            if not swap_tx:
                await self._broadcast_status("buy_failed", "Jupiter swap TX failed", reason)
                return None

            sig = await self._sign_and_send(swap_tx)
            if not sig:
                await self._broadcast_status("buy_failed", "TX broadcast failed", reason)
                return None

            # Record trade
            out_amount = int(quote.get("outAmount", 0))
            tokens = out_amount / (10 ** self._token_decimals)
            ct = self.current_trade
            if ct:
                ct.tx_hash_buy = sig
                ct.size_tokens = tokens
                ct.status = "open"
            self._token_balance = out_amount
            self.stats.starting_balance = await self._get_sol_balance()

            await self._broadcast_status("buy_confirmed", sig, reason, tokens=tokens)
            safe_sig = str(sig)
            logger.info(f"[BUY OK] sig={safe_sig[:16]}… tokens={tokens:.4f}")
            return sig

        finally:
            self._swap_in_flight = False

    async def execute_sell(self, reason: str = "signal") -> Optional[str]:
        """
        Full sell cycle: get token balance → quote → swap TX → sign → broadcast.
        Returns tx signature on success, None on failure.
        """
        if self._swap_in_flight:
            logger.warning("Swap already in flight — skipping sell")
            return None
        self._swap_in_flight = True
        try:
            # Fetch live token balance to sell exactly what we hold
            token_balance = await self._get_token_balance()
            if token_balance <= 0:
                logger.warning("No token balance to sell")
                await self._broadcast_status("sell_failed", "No token balance", reason)
                return None

            mint_str = str(self.token_mint)
            logger.info(f"[SELL] {mint_str[:8]}… {token_balance} units reason={reason}")

            quote = await self._get_quote(self.token_mint, WSOL_MINT, token_balance)
            if not quote:
                await self._broadcast_status("sell_failed", "Jupiter quote failed", reason)
                return None

            swap_tx = await self._get_swap_tx(quote)
            if not swap_tx:
                await self._broadcast_status("sell_failed", "Jupiter swap TX failed", reason)
                return None

            sig = await self._sign_and_send(swap_tx)
            if not sig:
                await self._broadcast_status("sell_failed", "TX broadcast failed", reason)
                return None

            # Calculate PnL
            sol_received = int(quote.get("outAmount", 0)) / 1e9
            if self.current_trade:
                self.confirm_sell(sig, sol_received, self._last_price)

            self._token_balance = 0
            await self._broadcast_status("sell_confirmed", sig, reason, sol_received=sol_received)
            safe_sig = str(sig)
            logger.info(f"[SELL OK] sig={safe_sig[:16]}… received={sol_received:.6f} SOL")
            return sig

        finally:
            self._swap_in_flight = False

    async def _broadcast_status(self, event: str, detail: str, reason: str = "",
                                 tokens: float = 0, sol_received: float = 0):
        """Push a status update to the frontend via WebSocket."""
        if self.broadcast_fn:
            ct = self.current_trade
            msg = {
                "type": "trade_update",
                "token": self.token_mint,
                "event": event,
                "detail": detail,
                "reason": reason,
                "tokens": tokens,
                "sol_received": sol_received,
                "timestamp": time.time(),
                "current_trade": ct.to_dict() if ct else None,
                "stats": self.stats.to_dict(),
            }
            try:
                await self.broadcast_fn(json.dumps(msg))
            except Exception:
                pass

    # ── Strategy update loop ──────────────────────────────────────────────────

    def update(
        self,
        time_val: int,
        o: float, h: float, l: float, c: float,
        volume: float = 0.0,
    ) -> dict:
        """
        Process one candle through StrategyEngine.
        If BUY/EXIT signal fires, schedules the swap as a background task.

        Returns strategy result dict for the candle.
        """
        self._last_price = c
        trade_action = None
        opened_trade = None
        swap_request = None

        result = self.engine.update(time_val, o, h, l, c, volume)
        signal = result["signal"]
        regime = result["regime"]

        # BUY signal
        if signal == Signal.BUY.value and self.current_trade is None and not self._swap_in_flight:
            buy_reason = f"buy_{regime}"
            trade = LiveTrade(
                token_mint=self.token_mint,
                entry_time=time_val,
                entry_price=c,
                size_sol=self.buy_size_sol,
                size_tokens=0,
                entry_reason=buy_reason,
                status="pending",
            )
            self.current_trade = trade
            opened_trade = trade
            trade_action = "buy"
            self.engine.notify_trade_opened(c, Direction.UP)

            # Fire-and-forget swap in background
            asyncio.ensure_future(self.execute_buy(buy_reason))

            swap_request = {
                "action": "buy",
                "token": self.token_mint,
                "amount_sol": self.buy_size_sol,
                "reason": buy_reason,
                "price": c,
            }

        # EXIT signal
        elif signal == Signal.EXIT.value and self.current_trade is not None and not self._swap_in_flight:
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

            self.current_trade.status = "closing"
            self.current_trade.exit_reason = exit_reason
            trade_action = "exit"

            asyncio.ensure_future(self.execute_sell(exit_reason))

            swap_request = {
                "action": "sell",
                "token": self.token_mint,
                "reason": exit_reason,
                "price": c,
            }

        # Unrealised PnL
        unrealized_pnl = 0.0
        unrealized_pnl_pct = 0.0
        if self.current_trade and self.current_trade.entry_price > 0:
            unrealized_pnl_pct = (c - self.current_trade.entry_price) / self.current_trade.entry_price * 100
            unrealized_pnl = self.current_trade.size_sol * (unrealized_pnl_pct / 100)

        if trade_action:
            self.signals_log.append({"time": time_val, "action": trade_action, "price": c, "regime": regime})

        trade_label = ""
        if trade_action == "buy" and opened_trade:
            trade_label = opened_trade.entry_reason
        elif trade_action == "exit" and self.current_trade:
            trade_label = self.current_trade.exit_reason or "exit"

        return {
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

    # ── Manual controls ───────────────────────────────────────────────────────

    async def force_buy(self) -> Optional[str]:
        """Manually trigger a test buy from the dashboard."""
        if self.current_trade is not None:
            return None
        c = self._last_price or 1.0
        trade = LiveTrade(
            token_mint=self.token_mint,
            entry_time=int(time.time()),
            entry_price=c,
            size_sol=self.buy_size_sol,
            size_tokens=0,
            entry_reason="manual_test_buy",
            status="pending",
        )
        self.current_trade = trade
        self.engine.notify_trade_opened(c, Direction.UP)
        return await self.execute_buy("manual_test_buy")

    async def force_sell(self) -> Optional[str]:
        """Manually trigger a test sell from the dashboard."""
        if self.current_trade is None:
            return None
        self.current_trade.status = "closing"
        self.current_trade.exit_reason = "manual_test_sell"
        return await self.execute_sell("manual_test_sell")

    # ── Trade confirmation (called internally after TX confirmed) ─────────────

    def confirm_sell(self, tx_hash: str, sol_received: float, actual_price: float):
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
        logger.info(f"Trade closed: PnL={trade.pnl_sol:+.6f} SOL ({trade.pnl_pct:+.2f}%)")

    def confirm_failed(self, action: str, error: str):
        logger.error(f"Swap FAILED ({action}): {error}")
        if action == "buy" and self.current_trade is not None:
            self.current_trade = None
            self.engine.notify_trade_closed()
        elif action == "sell" and self.current_trade is not None:
            self.current_trade.status = "open"
