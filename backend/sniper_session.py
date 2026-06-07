"""
Sniper Session — Per-Coin New-Pair Trading Session

Each SniperSession handles ONE coin (mint) that was identified as snipeable.

Two modes:
  - PAPER:  Simulates buys/sells with realistic slippage & fees (like ForwardTester)
  - LIVE:   Executes real on-chain swaps via Jupiter (like LiveTrader)

The session:
  1. Subscribes to the PumpPortal/PumpSwap trade stream for the mint
  2. Accumulates trades into 1-second candles (via CandleAggregator)
  3. Feeds candles into StrategyEngineV2
  4. On BUY signal: enters position (paper sim or real Jupiter swap)
  5. On EXIT signal: closes position
  6. Broadcasts state to the dashboard WebSocket

Architecture mirrors ForwardTester + LiveTrader exactly:
  - 1-bar-delay execution model (same as ForwardTester)
  - Realistic slippage + fee model for paper mode
  - Identical engine interface for both modes
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, asdict
from typing import Optional, Callable, Awaitable

from candle_aggregator import CandleAggregator
from pumpfun_client import PumpFunWSClient, DexScreenerPollClient, resolve_input
from strategy_engine_v2 import StrategyEngineV2, Signal, Direction, EntryMode

logger = logging.getLogger("sniper-session")

# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SniperTrade:
    mint:          str
    entry_time:    float
    entry_price:   float
    size_sol:      float
    size_tokens:   float
    entry_reason:  str    = ""
    exit_time:     Optional[float] = None
    exit_price:    Optional[float] = None
    pnl_sol:       float = 0.0
    pnl_pct:       float = 0.0
    exit_reason:   str   = ""
    mode:          str   = "paper"   # paper | live
    tx_hash_buy:   str   = ""
    tx_hash_sell:  str   = ""
    status:        str   = "open"    # open | closed | failed

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SniperStats:
    mode:            str   = "paper"
    total_trades:    int   = 0
    winning_trades:  int   = 0
    losing_trades:   int   = 0
    total_pnl_sol:   float = 0.0
    total_pnl_pct:   float = 0.0
    win_rate:        float = 0.0
    best_trade_pct:  float = 0.0
    worst_trade_pct: float = 0.0
    avg_hold_time_s: float = 0.0
    starting_balance: float = 0.0
    current_balance:  float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Paper trading sim helpers (mirrors ForwardTester logic)
# ─────────────────────────────────────────────────────────────────────────────

_REFERENCE_FEE:  float = 0.0005
_REFERENCE_SIZE: float = 0.1


def _fill_fraction(buy_size_sol: float, priority_fee: float,
                   bribe_fee: float, slippage_pct: float) -> float:
    """Compute intra-bar fill fraction (0→1). Same as ForwardTester."""
    import math
    total_fee = max(priority_fee + bribe_fee, 1e-12)
    base_delay = _REFERENCE_FEE / (_REFERENCE_FEE + total_fee)
    size_penalty = 1.0 + math.log10(max(1.0, buy_size_sol / _REFERENCE_SIZE))
    slippage_factor = 1.0 + slippage_pct / 100.0
    frac = base_delay * size_penalty * slippage_factor
    return max(0.02, min(0.98, frac))


def _intrabar_price(o: float, h: float, l: float, c: float, frac: float) -> float:
    """Interpolate price along realistic intra-bar path. Same as ForwardTester."""
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


# ─────────────────────────────────────────────────────────────────────────────
# SniperSession
# ─────────────────────────────────────────────────────────────────────────────

class SniperSession:
    """
    Manages a single coin's sniper trading session.

    Lifecycle:
      sniper_main → qualifies pair → creates SniperSession → run()
      run() streams trades, builds candles, feeds StrategyEngineV2
      On BUY signal → open_position() (paper sim or live swap)
      On EXIT signal → close_position() (paper sim or live swap)
      Pushes structured state updates to broadcast_fn

    Parameters
    ----------
    mint : token mint address
    token_info : metadata dict from resolve_input
    engine_kwargs : passed through to StrategyEngineV2
    mode : "paper" or "live"
    buy_size_sol : SOL per trade
    slippage_pct : slippage to model (paper) or pass to Jupiter (live)
    priority_fee : SOL priority fee
    bribe_fee : SOL bribe fee
    max_duration_s : auto-stop this many seconds after session starts (0 = forever)
    timeframe : candle timeframe for the candle aggregator
    broadcast_fn : async callable that receives JSON-serialisable status dicts
    keypair : solders.Keypair for live mode (None in paper mode)
    slippage_bps : Jupiter slippage in basis points (live mode)
    priority_fee_lamports : priority fee in lamports (live mode)
    """

    def __init__(
        self,
        mint:               str,
        token_info:         Optional[dict],
        engine_kwargs:      Optional[dict]       = None,
        mode:               str                  = "paper",
        buy_size_sol:       float                = 0.05,
        slippage_pct:       float                = 1.0,
        priority_fee:       float                = 0.0001,
        bribe_fee:          float                = 0.00001,
        starting_balance:   float                = 1.0,
        max_duration_s:     float                = 300.0,
        timeframe:          str                  = "1s",
        broadcast_fn:       Optional[Callable]   = None,
        # Live mode only
        keypair                                  = None,
        slippage_bps:       int                  = 200,
        priority_fee_lamports: int               = 100_000,
    ):
        self.mint        = mint
        self.token_info  = token_info or {}
        self.mode        = mode
        self.timeframe   = timeframe
        self.broadcast_fn = broadcast_fn
        self.max_duration_s = max_duration_s

        # Paper trade finances
        self.buy_size_sol    = buy_size_sol
        self.slippage_pct    = slippage_pct
        self.priority_fee    = priority_fee
        self.bribe_fee       = bribe_fee
        self.balance         = starting_balance

        # Live trade params
        self.keypair                   = keypair
        self.slippage_bps              = slippage_bps
        self.priority_fee_lamports     = priority_fee_lamports

        # Engine
        engine_kwargs = engine_kwargs or {}
        self.engine = StrategyEngineV2(**engine_kwargs)

        # State
        self.current_trade:  Optional[SniperTrade] = None
        self.trade_history:  list[SniperTrade]     = []
        self.stats = SniperStats(
            mode=mode,
            starting_balance=starting_balance,
            current_balance=starting_balance,
        )
        self.cancelled = asyncio.Event()
        self._start_time = time.time()

        # Pending signal queue (1-bar-delay execution model, same as ForwardTester)
        self._pending_buy:    bool = False
        self._pending_buy_reason: str = ""
        self._pending_exit:   bool = False
        self._pending_exit_reason: str = ""

        # Live swap state
        self._swap_in_flight: bool = False
        self._live_trader_ref = None  # set in live mode

        # Token metadata
        self._token_name   = self.token_info.get("name", "")
        self._token_symbol = self.token_info.get("symbol", "")

        logger.info(
            f"[Sniper:{mint[:8]}] Session created  mode={mode}  "
            f"buy={buy_size_sol} SOL  tf={timeframe}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Internal paper-trade position management
    # ─────────────────────────────────────────────────────────────────────────

    def _open_paper_position(
        self, o: float, h: float, l: float, c: float,
        ts: int, reason: str
    ) -> Optional[SniperTrade]:
        if self.current_trade is not None:
            return None
        if o <= 0:
            return None

        frac       = _fill_fraction(self.buy_size_sol, self.priority_fee,
                                    self.bribe_fee, self.slippage_pct)
        raw_price  = _intrabar_price(o, h, l, c, frac)
        slip       = self.slippage_pct / 100.0
        exec_price = raw_price * (1.0 + slip)

        fees = self.priority_fee + self.bribe_fee
        trade_size = min(self.buy_size_sol, self.balance - fees)
        if trade_size <= 0:
            logger.warning(f"[Sniper:{self.mint[:8]}] Insufficient paper balance")
            return None

        tokens = trade_size / exec_price
        self.balance -= (trade_size + fees)

        trade = SniperTrade(
            mint=self.mint,
            entry_time=float(ts),
            entry_price=exec_price,
            size_sol=trade_size,
            size_tokens=tokens,
            entry_reason=reason,
            mode="paper",
        )
        self.current_trade = trade
        self.engine.notify_trade_opened(exec_price, Direction.UP)

        logger.info(
            f"[Sniper:{self.mint[:8]}] PAPER BUY  price={exec_price:.8f}  "
            f"size={trade_size:.4f} SOL  tokens={tokens:.2f}  reason={reason}"
        )
        return trade

    def _close_paper_position(
        self, o: float, h: float, l: float, c: float,
        ts: int, reason: str
    ) -> Optional[SniperTrade]:
        if self.current_trade is None:
            return None

        trade = self.current_trade
        frac       = _fill_fraction(self.buy_size_sol, self.priority_fee,
                                    self.bribe_fee, self.slippage_pct)
        raw_price  = _intrabar_price(o, h, l, c, frac)
        slip       = self.slippage_pct / 100.0
        exec_price = raw_price * (1.0 - slip)
        fees       = self.priority_fee + self.bribe_fee

        proceeds   = trade.size_tokens * exec_price - fees
        pnl        = proceeds - trade.size_sol
        pnl_pct    = (exec_price / trade.entry_price - 1.0) * 100.0 if trade.entry_price > 0 else 0.0

        trade.exit_time   = float(ts)
        trade.exit_price  = exec_price
        trade.pnl_sol     = pnl
        trade.pnl_pct     = pnl_pct
        trade.exit_reason = reason
        trade.status      = "closed"

        self.balance += proceeds

        # Stats
        self.stats.total_trades    += 1
        self.stats.total_pnl_sol   += pnl
        self.stats.total_pnl_pct   += pnl_pct
        self.stats.current_balance  = self.balance
        if pnl > 0:
            self.stats.winning_trades += 1
        else:
            self.stats.losing_trades += 1
        if self.stats.total_trades > 0:
            self.stats.win_rate = self.stats.winning_trades / self.stats.total_trades * 100
        self.stats.best_trade_pct  = max(self.stats.best_trade_pct, pnl_pct)
        self.stats.worst_trade_pct = min(self.stats.worst_trade_pct, pnl_pct)
        hold_s = (trade.exit_time - trade.entry_time) if trade.exit_time else 0
        n = self.stats.total_trades
        self.stats.avg_hold_time_s = (
            (self.stats.avg_hold_time_s * (n - 1) + hold_s) / n if n > 1
            else hold_s
        )

        self.trade_history.append(trade)
        self.current_trade = None
        self.engine.notify_trade_closed()

        logger.info(
            f"[Sniper:{self.mint[:8]}] PAPER SELL  price={exec_price:.8f}  "
            f"pnl={pnl:+.4f} SOL ({pnl_pct:+.1f}%)  reason={reason}"
        )
        return trade

    # ─────────────────────────────────────────────────────────────────────────
    # Live trade helpers (thin wrappers around LiveTrader)
    # ─────────────────────────────────────────────────────────────────────────

    async def _open_live_position(self, reason: str) -> bool:
        """Fire a live buy via LiveTrader. Returns True if swap succeeded."""
        if self._live_trader_ref is None:
            logger.error(f"[Sniper:{self.mint[:8]}] No live trader ref set")
            return False
        sig = await self._live_trader_ref.execute_buy(reason=reason)
        if sig:
            # Sync engine state with what live trader executed
            ct = self._live_trader_ref.current_trade
            if ct:
                trade = SniperTrade(
                    mint=self.mint,
                    entry_time=ct.entry_time,
                    entry_price=ct.entry_price,
                    size_sol=ct.size_sol,
                    size_tokens=ct.size_tokens,
                    entry_reason=reason,
                    mode="live",
                    tx_hash_buy=sig,
                    status="open",
                )
                self.current_trade = trade
            return True
        return False

    async def _close_live_position(self, reason: str) -> bool:
        """Fire a live sell via LiveTrader. Returns True if swap succeeded."""
        if self._live_trader_ref is None:
            return False
        sig = await self._live_trader_ref.execute_sell(reason=reason)
        if sig and self.current_trade:
            ct = self._live_trader_ref.current_trade  # will be None after close
            trade = self.current_trade
            trade.exit_time    = time.time()
            trade.exit_price   = self._live_trader_ref._last_price or trade.entry_price
            trade.exit_reason  = reason
            trade.status       = "closed"
            trade.tx_hash_sell = sig
            # Real PnL comes from live trader stats
            lt_hist = self._live_trader_ref.trade_history
            if lt_hist:
                lt = lt_hist[-1]
                trade.pnl_sol = lt.pnl_sol
                trade.pnl_pct = lt.pnl_pct
            self._update_stats_from_trade(trade)
            self.trade_history.append(trade)
            self.current_trade = None
            return True
        return False

    def _update_stats_from_trade(self, trade: SniperTrade):
        self.stats.total_trades   += 1
        self.stats.total_pnl_sol  += trade.pnl_sol
        self.stats.total_pnl_pct  += trade.pnl_pct
        if trade.pnl_sol > 0:
            self.stats.winning_trades += 1
        else:
            self.stats.losing_trades += 1
        if self.stats.total_trades > 0:
            self.stats.win_rate = self.stats.winning_trades / self.stats.total_trades * 100
        self.stats.best_trade_pct  = max(self.stats.best_trade_pct, trade.pnl_pct)
        self.stats.worst_trade_pct = min(self.stats.worst_trade_pct, trade.pnl_pct)

    # ─────────────────────────────────────────────────────────────────────────
    # Broadcast helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _broadcast(self, msg: dict):
        if self.broadcast_fn:
            try:
                await self.broadcast_fn(msg)
            except Exception:
                pass

    async def _broadcast_state(
        self,
        event: str,
        candle_dict: Optional[dict] = None,
        strategy_result: Optional[dict] = None,
        trade: Optional[SniperTrade] = None,
        market_cap_usd: float = 0.0,
        market_cap_sol: float = 0.0,
    ):
        await self._broadcast({
            "type":          "sniper_update",
            "event":         event,
            "mint":          self.mint,
            "token_name":    self._token_name,
            "token_symbol":  self._token_symbol,
            "mode":          self.mode,
            "timestamp":     time.time(),
            "candle":        candle_dict,
            "strategy":      strategy_result,
            "current_trade": trade.to_dict() if trade else (
                             self.current_trade.to_dict() if self.current_trade else None),
            "trade_history": [t.to_dict() for t in self.trade_history[-20:]],
            "stats":         self.stats.to_dict(),
            "balance":       round(self.balance, 6),
            "market_cap_usd": market_cap_usd,
            "market_cap_sol": market_cap_sol,
        })

    # ─────────────────────────────────────────────────────────────────────────
    # Main run loop
    # ─────────────────────────────────────────────────────────────────────────

    async def run(self):
        """
        Main entry point.  Streams trades → candles → strategy engine.
        Mirrors the ForwardTester 'update()' 1-bar-delay model exactly.
        """
        logger.info(f"[Sniper:{self.mint[:8]}] Starting session  mode={self.mode}")

        # Resolve token info / live source
        live_source = self.token_info.get("_live_source", "pumpportal")
        live_query  = self.token_info.get("pair_address") or self.mint
        pair_address = self.token_info.get("pair_address", "")

        # Choose data client (same priority order as main.py WS handler)
        if hasattr(self, "_mock_ws"):
            ws_client = getattr(self, "_mock_ws")
        elif live_source == "solana_rpc" and pair_address:
            from pumpfun_client import PumpSwapRPCClient
            ws_client = PumpSwapRPCClient(pair_address)
        elif live_source == "pumpfun_rpc":
            from pumpfun_client import PumpFunRPCClient
            ws_client = PumpFunRPCClient(self.mint)
        elif live_source == "dexscreener":
            ws_client = DexScreenerPollClient(live_query, poll_seconds=0.25)
        elif live_source == "pumpfun_poll":
            from pumpfun_client import PumpFunPollClient
            ws_client = PumpFunPollClient(self.mint, poll_seconds=0.25)
        else:
            ws_client = PumpFunWSClient(self.mint)

        aggregator = CandleAggregator(self.timeframe)
        start_ts   = time.time()

        # In live mode, create and attach a LiveTrader
        live_trader = None
        if self.mode == "live" and self.keypair is not None:
            from live_trader import LiveTrader
            from strategy_engine_v2 import StrategyEngineV2  # avoid circular
            live_trader = LiveTrader(
                token_mint=self.mint,
                keypair=self.keypair,
                buy_size_sol=self.buy_size_sol,
                slippage_bps=self.slippage_bps,
                priority_fee_lamports=self.priority_fee_lamports,
                engine_kwargs={},  # engine is managed here, not in LiveTrader
                skip_simulation=True,
                engine_version=2,
            )
            # Override the live trader's engine with our shared engine so signals
            # are synchronised
            live_trader.engine = self.engine
            self._live_trader_ref = live_trader
            await live_trader._get_session()  # warm up the HTTP session

        await self._broadcast_state("session_started")

        try:
            async for trade_tick in ws_client.stream():
                if self.cancelled.is_set():
                    break

                # Time stop
                if self.max_duration_s > 0 and (time.time() - start_ts) > self.max_duration_s:
                    logger.info(f"[Sniper:{self.mint[:8]}] Max duration reached — stopping")
                    break

                is_synthetic = bool(trade_tick.get("synthetic"))
                price        = trade_tick.get("price", 0.0)
                sol_amount   = trade_tick.get("sol_amount", 0.0)
                ts_raw       = trade_tick.get("timestamp", time.time())
                mcap_usd     = trade_tick.get("market_cap_usd", 0.0)
                mcap_sol     = trade_tick.get("market_cap_sol", 0.0)

                if price <= 0:
                    continue

                candle, is_new = aggregator.process_trade(
                    price, sol_amount, ts_raw, synthetic=is_synthetic
                )
                candle_dict = candle.to_dict()

                # Inject mcap into engine
                if mcap_usd > 0:
                    self.engine.set_market_cap(mcap_usd)

                o = candle_dict["open"]
                h = candle_dict["high"]
                l = candle_dict["low"]
                c = candle_dict["close"]
                v = candle_dict.get("volume", 0.0)
                t = int(candle_dict["time"])

                # ── Step 1: Execute pending signal (1-bar-delay model) ──────
                opened_trade  = None
                closed_trade  = None
                trade_action  = None

                if self._pending_buy and self.current_trade is None:
                    if self.mode == "paper":
                        opened_trade = self._open_paper_position(
                            o, h, l, c, t, self._pending_buy_reason)
                        if opened_trade:
                            trade_action = "buy"
                    else:
                        # Live: fire and forget, confirmation handled by LiveTrader
                        if not self._swap_in_flight:
                            asyncio.ensure_future(
                                self._open_live_position(self._pending_buy_reason)
                            )
                            trade_action = "buy_initiated"
                    self._pending_buy = False

                elif self._pending_exit and self.current_trade is not None:
                    if self.mode == "paper":
                        closed_trade = self._close_paper_position(
                            o, h, l, c, t, self._pending_exit_reason)
                        if closed_trade:
                            trade_action = "sell"
                    else:
                        if not self._swap_in_flight:
                            asyncio.ensure_future(
                                self._close_live_position(self._pending_exit_reason)
                            )
                            trade_action = "sell_initiated"
                    self._pending_exit = False
                    self._pending_exit_reason = ""

                # ── Step 2: Run strategy engine on completed/updated candle ─
                # Determine buy_volume for buy pressure tracking.
                # PumpPortal gives us sol_amount per trade; we approximate
                # buy vs sell by tx_type if available.
                tx_type   = trade_tick.get("tx_type", "buy")
                buy_vol   = sol_amount if tx_type == "buy" else 0.0
                sell_vol  = sol_amount if tx_type == "sell" else 0.0

                strategy_result = self.engine.update(
                    time=t,
                    o=o, h=h, l=l, c=c,
                    volume=v,
                    buy_volume=buy_vol,
                    sell_volume=sell_vol,
                    market_cap_usd=mcap_usd,
                )
                signal = strategy_result.get("signal", "none")
                regime = strategy_result.get("regime", "idle")

                # ── Step 3: Queue signal for NEXT candle ───────────────────
                if signal == Signal.BUY.value and self.current_trade is None and not self._pending_buy:
                    entry_mode = strategy_result.get("indicators", {}).get(
                        "entry_mode", "initial_pump")
                    self._pending_buy = True
                    self._pending_buy_reason = f"snipe_{entry_mode}"
                    self._pending_exit = False

                elif signal == Signal.EXIT.value and self.current_trade is not None:
                    exit_reason = strategy_result.get("indicators", {}).get(
                        "exit_reason", "exit_signal") or "exit_signal"
                    self._pending_exit = True
                    self._pending_exit_reason = exit_reason
                    self._pending_buy = False

                # ── Step 4: Broadcast state ────────────────────────────────
                event = "tick"
                if trade_action in ("buy", "buy_initiated"):
                    event = "bought"
                elif trade_action in ("sell", "sell_initiated"):
                    event = "sold"
                elif signal == Signal.BUY.value:
                    event = "buy_signal"
                elif signal == Signal.EXIT.value:
                    event = "exit_signal"

                await self._broadcast_state(
                    event=event,
                    candle_dict=candle_dict,
                    strategy_result=strategy_result,
                    trade=opened_trade or closed_trade,
                    market_cap_usd=mcap_usd,
                    market_cap_sol=mcap_sol,
                )

        except asyncio.CancelledError:
            logger.info(f"[Sniper:{self.mint[:8]}] Session cancelled")
        except Exception as e:
            logger.error(f"[Sniper:{self.mint[:8]}] Session error: {e}", exc_info=True)
        finally:
            ws_client.stop()
            if live_trader:
                await live_trader.close()

            # Emergency close if still in position at session end
            if self.current_trade and self.mode == "paper":
                self._pending_exit = True
                self._pending_exit_reason = "session_ended"

            await self._broadcast_state("session_ended")
            self.cancelled.set()
            logger.info(
                f"[Sniper:{self.mint[:8]}] Session ended  "
                f"trades={self.stats.total_trades}  "
                f"pnl={self.stats.total_pnl_sol:+.4f} SOL"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Control
    # ─────────────────────────────────────────────────────────────────────────

    def stop(self):
        """Signal the session to stop cleanly."""
        self.cancelled.set()

    def to_summary(self) -> dict:
        """Light summary dict for the sniper dashboard listing."""
        return {
            "mint":          self.mint,
            "token_name":    self._token_name,
            "token_symbol":  self._token_symbol,
            "mode":          self.mode,
            "stats":         self.stats.to_dict(),
            "current_trade": self.current_trade.to_dict() if self.current_trade else None,
            "running":       not self.cancelled.is_set(),
            "uptime_s":      round(time.time() - self._start_time, 1),
        }
