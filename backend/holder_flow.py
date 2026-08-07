"""
HolderFlowMonitor — tracks dev/insider wallet sells for watched tokens.

Polls GMGN's `track smartmoney` endpoint for real-time trade records and
cross-references sellers against a per-token registry of dev/sniper/bundler
wallets (fetched via `token holders --tag ...`).

Emits HolderFlowEvent into an asyncio.Queue when a tracked wallet sells a
watched token.  The sniper engine consumes these events as an entry gate
(block entry on recent dev sell) and as an exit trigger (dev sell while in
position).

Also persists events to the recording DB so future backtests can replay them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import data_store

logger = logging.getLogger(__name__)

# GMGN CLI command templates
_GMGN_TRACK_CMD = "npx -y gmgn-cli@1.5.2 track smartmoney --chain sol --limit 200 --raw"
_GMGN_HOLDERS_CMD = (
    "npx -y gmgn-cli@1.5.2 token holders --chain sol --address {mint} "
    "--tag {tag} --limit 20 --raw"
)

# Wallet tags we care about (dev/insider cohorts)
_TRACKED_TAGS = ("dev", "sniper", "bundler", "rat_trader")

# Poll interval for the smartmoney feed (seconds)
_POLL_INTERVAL = 5.0

# How often to refresh the per-token wallet registry (seconds)
_REGISTRY_REFRESH_INTERVAL = 60.0

# Minimum sell amount (USD) to consider significant
_MIN_SELL_USD = 100.0


@dataclass
class HolderFlowEvent:
    """A detected dev/insider wallet trade on a watched token."""
    mint: str
    wallet: str
    tag: str
    side: str           # "buy" or "sell"
    amount_usd: float
    amount_sol: float
    timestamp: int
    tx_hash: str = ""


@dataclass
class _TokenWatchState:
    """Per-token state for the holder-flow monitor."""
    mint: str
    recording_id: Optional[int] = None
    # wallet → tag mapping (dev/sniper/bundler wallets for this token)
    wallet_registry: dict[str, str] = field(default_factory=dict)
    last_registry_refresh: float = 0.0
    # Recent events for quick lookup (mint → list of events)
    recent_events: list[HolderFlowEvent] = field(default_factory=list)
    # Max age for "recent" events (seconds)
    recent_window: float = 60.0


class HolderFlowMonitor:
    """
    Monitors dev/insider wallet activity for a set of watched tokens.

    Usage:
        monitor = HolderFlowMonitor()
        await monitor.start()
        monitor.watch_token(mint, recording_id=123)
        # ... later ...
        event = monitor.get_recent_sell(mint, window_seconds=30)
        # ... later ...
        monitor.unwatch_token(mint)
        await monitor.stop()
    """

    def __init__(self):
        self._watched: dict[str, _TokenWatchState] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._event_queue: asyncio.Queue[HolderFlowEvent] = asyncio.Queue(maxsize=500)
        # Track last-seen tx_hash to avoid duplicates from polling
        self._seen_tx: set[str] = set()
        self._seen_tx_max = 10_000  # LRU cap

    # ── Public API ────────────────────────────────────────────────────────

    async def start(self):
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("[HolderFlow] Monitor started")

    async def stop(self):
        """Stop the background polling loop."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        logger.info("[HolderFlow] Monitor stopped")

    def watch_token(self, mint: str, recording_id: Optional[int] = None):
        """Start watching a token for dev/insider sells."""
        if mint in self._watched:
            # Update recording_id if provided
            if recording_id is not None:
                self._watched[mint].recording_id = recording_id
            return
        self._watched[mint] = _TokenWatchState(mint=mint, recording_id=recording_id)
        logger.info(f"[HolderFlow] Watching {mint[:8]} (recording_id={recording_id})")
        # Fetch wallet registry in background (only if we're in an async context)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._refresh_wallet_registry(mint))
        except RuntimeError:
            # No running event loop — registry will be fetched on first poll
            pass

    def unwatch_token(self, mint: str):
        """Stop watching a token."""
        if mint in self._watched:
            del self._watched[mint]
            logger.info(f"[HolderFlow] Unwatched {mint[:8]}")

    def get_recent_sell(self, mint: str, window_seconds: float = 30.0) -> Optional[HolderFlowEvent]:
        """Get the most recent dev/insider sell for a token within the window."""
        state = self._watched.get(mint)
        if not state:
            return None
        now = int(time.time())
        cutoff = now - int(window_seconds)
        for event in reversed(state.recent_events):
            if event.timestamp < cutoff:
                break
            if event.side == "sell":
                return event
        return None

    def get_recent_sells(self, mint: str, window_seconds: float = 30.0) -> list[HolderFlowEvent]:
        """Get all dev/insider sells for a token within the window."""
        state = self._watched.get(mint)
        if not state:
            return []
        now = int(time.time())
        cutoff = now - int(window_seconds)
        return [e for e in state.recent_events if e.side == "sell" and e.timestamp >= cutoff]

    def has_recent_dev_sell(self, mint: str, window_seconds: float = 30.0, min_usd: float = 100.0) -> bool:
        """Check if a significant dev/insider sell occurred within the window."""
        event = self.get_recent_sell(mint, window_seconds)
        return event is not None and event.amount_usd >= min_usd

    @property
    def event_queue(self) -> asyncio.Queue[HolderFlowEvent]:
        """Queue of all detected events (for consumers that want to process every event)."""
        return self._event_queue

    # ── Internal: polling loop ────────────────────────────────────────────

    async def _poll_loop(self):
        """Main polling loop: fetch smartmoney trades and match against watched tokens."""
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[HolderFlow] Poll error: {e}")
            await asyncio.sleep(_POLL_INTERVAL)

    async def _poll_once(self):
        """Single poll of the smartmoney feed."""
        if not self._watched:
            return

        # Refresh stale wallet registries
        now = time.time()
        for mint, state in self._watched.items():
            if now - state.last_registry_refresh > _REGISTRY_REFRESH_INTERVAL:
                await self._refresh_wallet_registry(mint)

        # Fetch smartmoney trades
        trades = await self._fetch_smartmoney_trades()
        if not trades:
            return

        # Filter for watched tokens and check against wallet registry
        watched_mints = set(self._watched.keys())
        for trade in trades:
            base_addr = trade.get("base_address", "")
            if base_addr not in watched_mints:
                continue

            tx_hash = trade.get("transaction_hash", "")
            if tx_hash in self._seen_tx:
                continue
            self._seen_tx.add(tx_hash)
            # LRU eviction
            if len(self._seen_tx) > self._seen_tx_max:
                self._seen_tx = set(list(self._seen_tx)[-self._seen_tx_max // 2:])

            maker = trade.get("maker", "")
            side = trade.get("side", "")
            amount_usd = trade.get("amount_usd", 0.0)
            amount_sol = trade.get("quote_amount", 0.0)
            timestamp = trade.get("timestamp", int(time.time()))

            # Check if maker is a tracked wallet for this token
            state = self._watched[base_addr]
            tag = state.wallet_registry.get(maker, "")

            # If not in registry, check maker_info tags from the feed
            if not tag:
                maker_tags = trade.get("maker_info", {}).get("tags", [])
                for t in _TRACKED_TAGS:
                    if t in maker_tags:
                        tag = t
                        break

            # Only emit if it's a tracked tag or a significant sell
            if not tag and side == "sell" and amount_usd < _MIN_SELL_USD:
                continue

            event = HolderFlowEvent(
                mint=base_addr,
                wallet=maker,
                tag=tag,
                side=side,
                amount_usd=amount_usd,
                amount_sol=amount_sol,
                timestamp=timestamp,
                tx_hash=tx_hash,
            )

            # Add to recent events
            state.recent_events.append(event)
            # Trim old events
            cutoff = int(time.time()) - int(state.recent_window)
            state.recent_events = [e for e in state.recent_events if e.timestamp >= cutoff]

            # Persist to DB if recording
            if state.recording_id is not None:
                try:
                    data_store.insert_holder_flow(
                        recording_id=state.recording_id,
                        t=timestamp,
                        wallet=maker,
                        tag=tag,
                        side=side,
                        amount_usd=amount_usd,
                        amount_sol=amount_sol,
                        tx_hash=tx_hash,
                    )
                except Exception as e:
                    logger.warning(f"[HolderFlow] DB insert failed: {e}")

            # Emit to queue
            try:
                self._event_queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("[HolderFlow] Event queue full, dropping event")

            if side == "sell" and amount_usd >= _MIN_SELL_USD:
                logger.info(
                    f"[HolderFlow] DEV SELL {base_addr[:8]} "
                    f"wallet={maker[:8]} tag={tag or 'unknown'} "
                    f"amount=${amount_usd:.2f}"
                )

    async def _fetch_smartmoney_trades(self) -> list[dict]:
        """Fetch the latest smartmoney trades from GMGN."""
        try:
            proc = await asyncio.create_subprocess_shell(
                _GMGN_TRACK_CMD,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
            if proc.returncode != 0:
                logger.warning(f"[HolderFlow] GMGN CLI error: {stderr.decode()[:200]}")
                return []
            import json
            data = json.loads(stdout.decode())
            return data.get("list", [])
        except asyncio.TimeoutError:
            logger.warning("[HolderFlow] GMGN CLI timeout")
            return []
        except Exception as e:
            logger.warning(f"[HolderFlow] GMGN CLI error: {e}")
            return []

    async def _refresh_wallet_registry(self, mint: str):
        """Fetch the dev/sniper/bundler wallet list for a token."""
        state = self._watched.get(mint)
        if not state:
            return

        for tag in _TRACKED_TAGS:
            try:
                cmd = _GMGN_HOLDERS_CMD.format(mint=mint, tag=tag)
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15.0)
                if proc.returncode != 0:
                    continue
                import json
                data = json.loads(stdout.decode())
                for holder in data.get("list", []):
                    wallet = holder.get("address", "")
                    if wallet:
                        state.wallet_registry[wallet] = tag
            except Exception as e:
                logger.debug(f"[HolderFlow] Registry refresh failed for {mint[:8]} tag={tag}: {e}")

        state.last_registry_refresh = time.time()
        logger.debug(
            f"[HolderFlow] Registry refreshed for {mint[:8]}: "
            f"{len(state.wallet_registry)} wallets"
        )
