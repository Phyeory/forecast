"""
SniperEngine — Main orchestrator for the dip-recovery sniper strategy.

One SniperEngine instance exists globally and manages all per-token
LaunchDetector instances. Subscribes to the PumpPortal new-token + trade
streams. Coordinates:
  filters → detection → signal → forward test OR live execution.
"""

from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Literal

from candle_aggregator import CandleAggregator
from strategy_engine import StrategyEngine
from sniper.launch_detector import LaunchDetector
from sniper.pressure_analyzer import compute_pressure
from sniper.chart_validator import classify_chart
from sniper.entry_signal import evaluate_entry, EntrySignal
from sniper.exit_signal import ExitEvaluator, ExitSignal, OpenPosition
from sniper.fee_filter import passes_fee_filter
from sniper.forward_tester import SniperForwardTester
from pumpfun_client import NewPairsStream, PumpFunWSClient
from holder_flow import HolderFlowMonitor

logger = logging.getLogger(__name__)


@dataclass
class SniperConfig:
    mode: Literal["forward_test", "live"] = "forward_test"
    fee_threshold_sol: float = 0.1      # lowered from 0.5 — 50 SOL buy vol was too restrictive
    max_concurrent_positions: int = 3
    max_trades_per_hour: int = 10
    daily_loss_limit_sol: float = 2.0
    weekly_loss_limit_sol: float = 8.0
    min_forward_trades_for_live: int = 200
    stale_timeout_seconds: float = 300.0  # 5 minutes


class SniperEngine:
    """Main sniper bot orchestrator."""

    def __init__(self, config: Optional[SniperConfig] = None):
        self.config = config or SniperConfig()
        self.forward_tester = SniperForwardTester()
        self.holder_flow = HolderFlowMonitor()

        # Per-token state
        self.detectors: dict[str, LaunchDetector] = {}
        self.aggregators: dict[str, CandleAggregator] = {}
        self.strategy_engines: dict[str, StrategyEngine] = {}
        self.exit_evaluators: dict[str, ExitEvaluator] = {}
        self.open_positions: dict[str, OpenPosition] = {}
        self.watching: set[str] = set()    # tokens past fee threshold awaiting entry
        self.stale: set[str] = set()       # tokens timed out
        self.rejected: set[str] = set()    # tokens rejected by chart validator
        self.blacklist: set[str] = set()   # blacklisted creators
        # Last closed-candle time fed to each token's StrategyEngine
        self._last_se_candle_time: dict[str, int] = {}

        # Rate limiting
        self._trades_this_hour: int = 0
        self._hour_start: float = time.time()
        self._daily_pnl: float = 0.0
        self._daily_start: float = time.time()

        # WebSocket broadcast queue for frontend
        self.event_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

        # Running state
        self._running = False
        self._new_pairs_stream: Optional[NewPairsStream] = None
        self._trade_clients: dict[str, PumpFunWSClient] = {}
        self._tasks: list[asyncio.Task] = []

        # Stats
        self.tokens_seen: int = 0
        self.tokens_rejected: int = 0
        self.tokens_watching: int = 0

    @property
    def mode(self) -> str:
        return self.config.mode

    @property
    def tracking_count(self) -> int:
        return len(self.detectors) - len(self.stale) - len(self.rejected)

    # ── Public API ────────────────────────────────────────────────────────

    async def start(self):
        """Start the sniper engine — subscribe to new token events."""
        if self._running:
            return
        self._running = True
        logger.info(f"[Sniper] Starting in {self.config.mode} mode")

        # Start holder-flow monitor
        await self.holder_flow.start()

        # Start new pairs stream
        self._new_pairs_stream = NewPairsStream()
        self._tasks.append(
            asyncio.create_task(self._process_new_tokens())
        )
        # Start stale checker
        self._tasks.append(
            asyncio.create_task(self._stale_checker())
        )
        # Start rate limit reset
        self._tasks.append(
            asyncio.create_task(self._rate_limit_reset())
        )

    async def stop(self):
        """Stop the sniper engine."""
        self._running = False
        await self.holder_flow.stop()
        if self._new_pairs_stream:
            self._new_pairs_stream.stop()
        for client in self._trade_clients.values():
            client.stop()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        logger.info("[Sniper] Stopped")

    def set_mode(self, mode: str) -> dict:
        """Switch between forward_test and live mode."""
        if mode == "live":
            trade_count = self.forward_tester.get_total_trade_count()
            if trade_count < self.config.min_forward_trades_for_live:
                return {
                    "error": f"Need {self.config.min_forward_trades_for_live} forward test trades "
                             f"before going live (currently have {trade_count})"
                }
        self.config.mode = mode
        logger.info(f"[Sniper] Mode switched to {mode}")
        return {"mode": mode}

    def get_status(self) -> dict:
        """Return current engine state for the REST API."""
        return {
            "mode": self.config.mode,
            "running": self._running,
            "tracking_count": self.tracking_count,
            "watching_count": len(self.watching),
            "open_positions": [
                {
                    "mint": pos.mint,
                    "entry_price": pos.entry_price,
                    "entry_mc": pos.entry_mc,
                    "entry_time": pos.entry_time,
                    "size_sol": pos.size_sol,
                }
                for pos in self.open_positions.values()
            ],
            "tokens_seen": self.tokens_seen,
            "tokens_rejected": self.tokens_rejected,
            "stale_count": len(self.stale),
            "today_trade_count": self._trades_this_hour,
            "today_pnl_sol": self._daily_pnl,
        }

    def get_watching_list(self) -> list[dict]:
        """Return list of tokens being actively watched."""
        result = []
        for mint in self.watching:
            detector = self.detectors.get(mint)
            if detector:
                result.append({
                    "mint": mint,
                    "token_name": detector.token_name,
                    "token_symbol": detector.token_symbol,
                    "current_act": detector.current_act,
                    "fees_paid_sol": detector.fees_paid_sol,
                    "dip_depth": detector.dip_depth,
                    "spike_high_mc": detector.spike_high_mc,
                    "floor_mc": detector.lowest_mc_since_spike,
                    "current_mc": detector.current_mc,
                })
        return result

    # ── Internal: New Token Processing ────────────────────────────────────

    async def _process_new_tokens(self):
        """Process new token events from PumpPortal."""
        try:
            async for token_event in self._new_pairs_stream.stream():
                if not self._running:
                    break

                mint = token_event.get("mint", "")
                if not mint:
                    continue

                creator = token_event.get("creator", "")
                if creator in self.blacklist:
                    continue

                # Skip if already tracking
                if mint in self.detectors or mint in self.stale:
                    continue

                self.tokens_seen += 1

                # Basic metadata filter (Filter 3: no-metadata spam)
                name = token_event.get("name", "")
                symbol = token_event.get("symbol", "")
                has_metadata = bool(name) or bool(symbol)

                # Create detector
                genesis_mc = token_event.get("marketCapSol", 0)
                ts = token_event.get("timestamp", time.time())

                detector = LaunchDetector(
                    mint=mint,
                    genesis_mc=genesis_mc,
                    genesis_time=ts,
                    event_queue=self.event_queue,
                )
                detector.token_name = name
                detector.token_symbol = symbol
                detector.creator = creator
                detector.bonding_curve_key = token_event.get("bondingCurveKey", "")

                self.detectors[mint] = detector

                # Create per-token candle aggregator (1s timeframe for sniper)
                agg = CandleAggregator(timeframe="1s", history_size=120)
                self.aggregators[mint] = agg

                # Create per-token strategy engine for Kalman/ROC
                # Use minimal warmup — we only need m_hat, not the full signal
                se = StrategyEngine(warmup=3, ema_fast=3, ema_slow=7, roc_period=3)
                self.strategy_engines[mint] = se
                # Track last closed candle time so we only feed closed bars
                self._last_se_candle_time[mint] = -1

                # Subscribe to trade stream for this token
                await self._subscribe_to_trades(mint)

                # Broadcast to frontend
                await self._broadcast({
                    "type": "token_detected",
                    "mint": mint,
                    "name": name,
                    "symbol": symbol,
                    "genesis_mc": genesis_mc,
                })

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Sniper] New token processing error: {e}", exc_info=True)

    async def _subscribe_to_trades(self, mint: str):
        """Subscribe to PumpPortal trade stream for a token via the shared hub."""
        client = PumpFunWSClient(mint)
        self._trade_clients[mint] = client
        task = asyncio.create_task(self._process_trades(mint, client))
        self._tasks.append(task)

    async def _process_trades(self, mint: str, client: PumpFunWSClient):
        """Process trade stream for a single token."""
        try:
            async for trade in client.stream():
                if not self._running:
                    break
                if mint in self.stale or mint in self.rejected:
                    break
                await self._on_trade(mint, trade)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"[Sniper] Trade stream error for {mint[:8]}: {e}")
        finally:
            client.stop()
            self._trade_clients.pop(mint, None)

    async def _on_trade(self, mint: str, trade: dict):
        """Process a single trade event for a token."""
        detector = self.detectors.get(mint)
        if not detector:
            return

        # Update detector state
        await detector.on_trade(trade)

        # Update candle aggregator
        agg = self.aggregators.get(mint)
        is_new_candle = False
        if agg:
            is_buy = trade.get("tx_type", "buy") == "buy"
            candle, is_new_candle = agg.process_trade(
                price=trade.get("price", 0),
                volume=trade.get("sol_amount", 0),
                timestamp=trade.get("timestamp", time.time()),
                is_buy=is_buy,
            )

        # Update strategy engine — ONLY on closed candles (new candle started).
        # Feeding the in-progress candle on every tick means the Kalman filter
        # sees near-identical prices hundreds of times per second, which drives
        # m_hat → 0 and permanently breaks C4 (momentum check).
        se = self.strategy_engines.get(mint)
        if se and agg and is_new_candle and len(agg._history) > 0:
            # Feed the just-CLOSED candle (last entry in history) to StrategyEngine
            closed = agg._history[-1]
            last_t = self._last_se_candle_time.get(mint, -1)
            if closed.time != last_t:
                se.update(closed.time, closed.open, closed.high, closed.low, closed.close, closed.volume)
                self._last_se_candle_time[mint] = closed.time

        # ── Gate 1: Fee filter ────────────────────────────────────────────
        if not passes_fee_filter(detector.fees_paid_sol, self.config.fee_threshold_sol):
            return

        # ── Gate 2: Act-based routing ─────────────────────────────────────
        if detector.current_act < 2:
            return

        if detector.current_act == 2:
            if mint not in self.watching:
                # Validate chart
                candles = agg.get_last_n(60) if agg else []
                if candles:
                    classification = classify_chart(candles, detector)
                    if not classification.is_real:
                        self.rejected.add(mint)
                        self.tokens_rejected += 1
                        await self._broadcast({
                            "type": "filter_rejected",
                            "mint": mint,
                            "reason": f"fake_chart: {', '.join(classification.flags)}"
                        })
                        self._cleanup_token(mint)
                        return
                self.watching.add(mint)
                self.holder_flow.watch_token(mint)
                await self._broadcast({
                    "type": "token_watching",
                    "mint": mint,
                    "fees_sol": detector.fees_paid_sol,
                    "dip_depth": detector.dip_depth,
                })
            return

        # ── Act 3: Evaluate entry or exit ─────────────────────────────────
        if mint in self.open_positions:
            await self._evaluate_exit(mint, trade)
            return

        # ── Watching fallback for tokens that hit fee threshold in Act 3 ───
        # If fees crossed the threshold only after the token was already in
        # Act 3, the Act 2 block never ran — it was never added to watching.
        # Recover: if dip_depth is valid, add to watching now.
        if mint not in self.watching:
            if detector.dip_depth >= 0.35:
                self.watching.add(mint)
                self.holder_flow.watch_token(mint)
                await self._broadcast({
                    "type": "token_watching",
                    "mint": mint,
                    "fees_sol": detector.fees_paid_sol,
                    "dip_depth": detector.dip_depth,
                    "note": "late_fee_threshold_recovery",
                })
                logger.info(
                    f"[Sniper] Late-recovery watch: {mint[:8]} "
                    f"dip={detector.dip_depth:.1%} fees={detector.fees_paid_sol:.3f} SOL"
                )
            else:
                return  # no valid dip yet, skip

        # Rate limit check
        if self._trades_this_hour >= self.config.max_trades_per_hour:
            return

        # Max concurrent positions check
        if len(self.open_positions) >= self.config.max_concurrent_positions:
            return

        # ── Entry evaluation: only run on candle close ────────────────────
        # Running on every raw trade tick means pressure metrics and the
        # two_green check see partial/in-progress candle data that changes
        # on every tick. Conditions never align simultaneously. Only evaluate
        # once per closed 1-second candle when we have settled OHLCV data.
        if not is_new_candle:
            return

        # Need at least 10 closed candles for meaningful signals
        if not agg or len(agg._history) < 10:
            return

        # Build candle list from CLOSED history only (exclude current in-progress)
        # This ensures two_green, floor, and pressure use fully-settled data.
        closed_candles = list(agg._history)[-15:]

        pressure = compute_pressure(closed_candles)
        if pressure is None:
            return

        # Re-fetch se in case it was cleaned up between gate checks
        se = self.strategy_engines.get(mint)

        # ── Holder-flow entry gate: block entry on recent dev/insider sell ──
        if self.holder_flow.has_recent_dev_sell(mint, window_seconds=30, min_usd=100.0):
            logger.info(
                f"[Sniper] {mint[:8]} entry BLOCKED — recent dev/insider sell detected"
            )
            await self._broadcast({
                "type": "entry_blocked_dev_sell",
                "mint": mint,
                "reason": "recent_dev_sell",
            })
            return

        entry = evaluate_entry(
            mint=mint,
            launch_detector=detector,
            pressure=pressure,
            strategy_engine=se,
            candles=closed_candles,
            timestamp=trade.get("timestamp", time.time()),
        )

        # One log line per closed candle — clear and not spammy
        failed = [k for k, v in entry.conditions_met.items() if not v]
        logger.info(
            f"[Sniper] {mint[:8]} candle eval — "
            f"{'PASS' if not failed else 'blocked:' + str(failed)} | "
            f"dip={detector.dip_depth:.1%} "
            f"m_hat={getattr(se, 'm_hat', 0):.6f} "
            f"bars={getattr(se, 'bar_count', 0)} "
            f"buy1s={pressure.buy_ratio_1s:.2f} "
            f"buy5s={pressure.buy_ratio_5s:.2f} "
            f"vol_exp={pressure.volume_expansion:.2f}"
        )

        if entry.triggered:
            await self._execute_entry(entry, detector)


    async def _execute_entry(self, entry: EntrySignal, detector: LaunchDetector):
        """Execute a paper or live entry."""
        position = OpenPosition(
            mint=entry.mint,
            entry_price=entry.entry_price_sol,
            entry_mc=entry.current_mc,
            entry_time=entry.timestamp,
            size_sol=entry.conviction_to_size(),
            peak_mc=entry.current_mc,
        )
        self.open_positions[entry.mint] = position
        self.exit_evaluators[entry.mint] = ExitEvaluator()
        self._trades_this_hour += 1

        if self.config.mode == "forward_test":
            trade_id = await self.forward_tester.record_entry(entry, detector)
            position.forward_test_id = trade_id
        else:
            # Live trading would go here
            logger.info(f"[Sniper] LIVE entry — {entry.mint[:8]} (not implemented)")

        await self._broadcast({
            "type": "entry",
            "mint": entry.mint,
            "entry_mc": entry.current_mc,
            "conviction": entry.conviction,
            "size_sol": entry.conviction_to_size(),
            "conditions": entry.conditions_met,
        })

        logger.info(
            f"[Sniper] ENTRY {entry.mint[:8]} "
            f"mc={entry.current_mc:.0f} conv={entry.conviction} "
            f"mode={self.config.mode}"
        )

    async def _evaluate_exit(self, mint: str, trade: dict):
        """Evaluate exit triggers for an open position."""
        position = self.open_positions.get(mint)
        if not position:
            return

        evaluator = self.exit_evaluators.get(mint)
        if not evaluator:
            return

        agg = self.aggregators.get(mint)
        se = self.strategy_engines.get(mint)
        candles = agg.get_last_n(15) if agg else []
        pressure = compute_pressure(candles) if len(candles) >= 3 else None

        current_price = trade.get("price", 0)
        current_mc = trade.get("market_cap_sol", 0)

        # ── Holder-flow exit trigger: dev/insider sell while in position ──
        if self.holder_flow.has_recent_dev_sell(mint, window_seconds=15, min_usd=100.0):
            sell_event = self.holder_flow.get_recent_sell(mint, window_seconds=15)
            exit_signal = ExitSignal(
                triggered=True,
                trigger_name="dev_sell_exit",
                timestamp=time.time(),
                current_mc=current_mc,
                exit_price_sol=current_price,
                urgency="immediate",
            )
            logger.info(
                f"[Sniper] DEV SELL EXIT {mint[:8]} "
                f"wallet={sell_event.wallet[:8] if sell_event else '?'} "
                f"amount=${sell_event.amount_usd:.2f if sell_event else 0}"
            )
            await self._execute_exit(mint, exit_signal)
            return

        exit_signal = evaluator.evaluate_exit(
            position=position,
            pressure=pressure,
            strategy_engine=se,
            candles=candles,
            current_tick_price=current_price,
            current_mc=current_mc,
        )

        if exit_signal and exit_signal.triggered:
            if exit_signal.urgency == "immediate":
                await self._execute_exit(mint, exit_signal)
            else:
                # Store as pending — check on candle close
                position.pending_exit = exit_signal
                await self._execute_exit(mint, exit_signal)  # For simplicity, execute immediately

    async def _execute_exit(self, mint: str, exit_signal: ExitSignal):
        """Execute a paper or live exit."""
        position = self.open_positions.get(mint)
        if not position:
            return

        if self.config.mode == "forward_test" and position.forward_test_id is not None:
            await self.forward_tester.record_exit(
                trade_id=position.forward_test_id,
                exit_signal=exit_signal,
                entry_price_sol=position.entry_price,
                position_size_sol=position.size_sol,
            )
        else:
            logger.info(f"[Sniper] LIVE exit — {mint[:8]} (not implemented)")

        # Track daily P&L
        if position.entry_price > 0 and exit_signal.exit_price_sol > 0:
            pnl = position.size_sol * (exit_signal.exit_price_sol / position.entry_price - 1)
            self._daily_pnl += pnl

            # Check daily loss limit
            if self._daily_pnl <= -self.config.daily_loss_limit_sol:
                logger.warning("[Sniper] Daily loss limit reached — pausing")

        await self._broadcast({
            "type": "exit",
            "mint": mint,
            "exit_mc": exit_signal.current_mc,
            "trigger": exit_signal.trigger_name,
            "exit_price": exit_signal.exit_price_sol,
        })

        logger.info(
            f"[Sniper] EXIT {mint[:8]} trigger={exit_signal.trigger_name} "
            f"mode={self.config.mode}"
        )

        # Cleanup position
        del self.open_positions[mint]
        self.exit_evaluators.pop(mint, None)
        self.watching.discard(mint)
        self._cleanup_token(mint)

    # ── Housekeeping ──────────────────────────────────────────────────────

    async def _stale_checker(self):
        """Periodically mark stale tokens that never spiked."""
        while self._running:
            await asyncio.sleep(30)
            now = time.time()
            to_stale = []
            for mint, detector in list(self.detectors.items()):
                if mint in self.stale or mint in self.rejected:
                    continue
                if mint in self.open_positions:
                    continue
                age = now - detector.genesis_time
                if age > self.config.stale_timeout_seconds and detector.current_act == 1:
                    to_stale.append(mint)

            for mint in to_stale:
                self.stale.add(mint)
                self._cleanup_token(mint)

            if to_stale:
                logger.debug(f"[Sniper] Marked {len(to_stale)} tokens stale")

    async def _rate_limit_reset(self):
        """Reset hourly trade counter."""
        while self._running:
            await asyncio.sleep(3600)
            self._trades_this_hour = 0

            # Daily reset check
            if time.time() - self._daily_start > 86400:
                self._daily_pnl = 0.0
                self._daily_start = time.time()

    def _cleanup_token(self, mint: str):
        """Remove all state for a token to free memory."""
        # Keep detectors for stale/rejected tracking but clean up heavy objects
        self.aggregators.pop(mint, None)
        self.strategy_engines.pop(mint, None)
        self._last_se_candle_time.pop(mint, None)
        self.holder_flow.unwatch_token(mint)
        client = self._trade_clients.pop(mint, None)
        if client:
            client.stop()

    async def _broadcast(self, event: dict):
        """Put event on the broadcast queue for WebSocket subscribers."""
        try:
            self.event_queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest
            try:
                self.event_queue.get_nowait()
                self.event_queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
