"""
HolderFlowMonitor — tracks dev/insider wallet sells for watched tokens.

Polls GMGN's `track smartmoney` endpoint for real-time trade records and
cross-references sellers against a per-token registry of dev/sniper/bundler
wallets (fetched via `token holders --tag ...`).

Emits HolderFlowEvent into an asyncio.Queue when a tracked wallet sells a
watched token.  The strategy engine consumes these events as an entry gate
(block entry on recent dev sell) and as an exit trigger (dev sell while in
position).

Also persists events to the recording DB so future backtests can replay them.

Implementation notes (iter36 rate-limit fix):
  * Uses a *shared* process-wide singleton (`get_shared_monitor()`) so that
    N concurrent live sessions / recorders share ONE poller instead of each
    hammering the GMGN API independently.
  * Calls the GMGN OpenAPI REST endpoint directly over async HTTP
    (`https://openapi.gmgn.ai`, exist-auth: X-APIKEY + timestamp + client_id)
    instead of shelling out to the `npx gmgn-cli` subprocess on every poll —
    subprocess startup was the main driver of the request rate.
  * Rate-limit aware: on HTTP 429 the poller backs off until the
    server-provided reset time, and registry refreshes are skipped while
    banned.  Errors are logged once per ban window, not per poll.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

import data_store
import rate_limiter as rl

logger = logging.getLogger(__name__)

# GMGN OpenAPI host (exist-auth: X-APIKEY header + timestamp/client_id query)
_GMGN_HOST = "https://openapi.gmgn.ai"

# Wallet tags we care about (dev/insider cohorts)
_TRACKED_TAGS = ("dev", "sniper", "bundler", "rat_trader")

# iter38: map GMGN's assorted maker-tag spellings onto our tracked set, and
# recognise additional insider-ish labels.  Anything not matched stays "".
_TAG_SYNONYMS = {
    "dev": "dev",
    "token_deployer": "dev",
    "deployer": "dev",
    "creator": "dev",
    "sniper": "sniper",
    "bundler": "bundler",
    "bundle": "bundler",
    "rat_trader": "rat_trader",
    "rat": "rat_trader",
    "insider": "rat_trader",
    "smart_money": "rat_trader",
    "smartmoney": "rat_trader",
}

# Tag assigned to a LARGE sell (>= _MIN_SELL_USD) whose wallet is NOT in the
# tracked registry and carries no recognised maker tag.  This keeps the
# "big-seller circuit breaker" signal (which was net-positive in iter38) but
# makes it explicitly distinguishable from a verified dev/insider sell, so the
# engine's `v2_holder_flow_require_tag` gate can tell them apart.
LARGE_SELLER_TAG = "whale"


def _normalise_tag(maker_tags_lower: set[str]) -> str:
    """Map a lowercased set of GMGN maker tags onto our canonical tracked tag.

    Returns the first tracked tag found, else "".
    """
    for raw in maker_tags_lower:
        canon = _TAG_SYNONYMS.get(raw)
        if canon:
            return canon
    # Substring fallback (e.g. "top_sniper_1" -> sniper)
    for raw in maker_tags_lower:
        for syn, canon in _TAG_SYNONYMS.items():
            if syn in raw:
                return canon
    return ""


def _extract_holder_list(data) -> Optional[list]:
    """Tolerantly extract a list of holder dicts from a GMGN response.

    Returns the list (possibly empty) on success, or None when `data` is not a
    recognisable shape (so the caller can log the actual payload).  Handles:
      * bare list   → [holder, ...]
      * {"list":[...]}
      * {"data":{"list":[...]}}
      * {"data":[...]}
      * {"holders":[...]} / {"top_holders":[...]}
    """
    if data is None:
        return None
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        payload = data.get("data", data)
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("list", "holders", "top_holders"):
                lst = payload.get(key)
                if isinstance(lst, list):
                    return lst
        # Some endpoints nest one level deeper.
        if isinstance(data.get("data"), dict):
            for key in ("list", "holders", "top_holders"):
                lst = data["data"].get(key)
                if isinstance(lst, list):
                    return lst
    return None

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
        self._poll_task: Optional[asyncio.Task] = None
        self._event_queue: asyncio.Queue[HolderFlowEvent] = asyncio.Queue(maxsize=500)
        # Track last-seen tx_hash to avoid duplicates from polling
        self._seen_tx: set[str] = set()
        self._seen_tx_max = 10_000  # LRU cap
        # Registry refresh queue (mints needing a wallet-registry fetch)
        self._registry_queue: asyncio.Queue[str] = asyncio.Queue()
        self._registry_task: Optional[asyncio.Task] = None
        # HTTP session + auth
        self._session: Optional[aiohttp.ClientSession] = None
        self._api_key: str = os.environ.get("GMGN_API_KEY", "")
        # Refcount of active users (live sessions + recorders)
        self._refcount: int = 0
        # iter38: rate-limited error log timestamps (key -> last emit time)
        self._err_log_ts: dict[str, float] = {}

    # ── Public API ────────────────────────────────────────────────────────

    async def start(self):
        """Start the background polling loop (idempotent, refcounted)."""
        self._refcount += 1
        if self._running:
            return
        if not self._api_key:
            logger.warning("[HolderFlow] GMGN_API_KEY not set — monitor disabled")
            self._refcount -= 1
            return
        self._running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12.0),
            headers={"User-Agent": "pump-chart/holder-flow"},
        )
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._registry_task = asyncio.create_task(self._registry_loop())
        logger.info("[HolderFlow] Monitor started")

    async def stop(self):
        """Stop the background polling loop when the last user disconnects."""
        self._refcount = max(0, self._refcount - 1)
        if self._refcount > 0 or not self._running:
            return
        self._running = False
        for task in (self._poll_task, self._registry_task):
            if task:
                task.cancel()
        for task in (self._poll_task, self._registry_task):
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._poll_task = None
        self._registry_task = None
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
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
        # Queue a wallet-registry fetch (processed serially by the registry loop)
        try:
            self._registry_queue.put_nowait(mint)
        except asyncio.QueueFull:
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

    def get_events_as_dicts(self, mint: str) -> list[dict]:
        """Return all retained events for a token as plain dicts (engine-facing)."""
        state = self._watched.get(mint)
        if not state:
            return []
        return [
            {
                "time": e.timestamp,
                "wallet": e.wallet,
                "tag": e.tag,
                "side": e.side,
                "amount_usd": e.amount_usd,
                "amount_sol": e.amount_sol,
                "tx_hash": e.tx_hash,
            }
            for e in state.recent_events
        ]

    @property
    def event_queue(self) -> asyncio.Queue[HolderFlowEvent]:
        """Queue of all detected events (for consumers that want to process every event)."""
        return self._event_queue

    # ── Internal: HTTP plumbing ───────────────────────────────────────────

    def _auth_query(self, extra: dict) -> dict:
        """Exist-auth query params: timestamp + client_id (no signature needed)."""
        return {**extra, "timestamp": int(time.time()), "client_id": str(uuid.uuid4())}

    async def _get(self, path: str, extra: dict) -> Optional[dict]:
        """Authenticated GET against the GMGN OpenAPI.  Returns parsed JSON or None.

        iter38: failures are now logged at INFO/WARNING (rate-limited) instead of
        DEBUG so the root cause of an empty wallet registry is observable in the
        server log.  Returns None on any non-200.
        """
        if not self._session:
            return None
        params = self._auth_query(extra)
        headers = {"X-APIKEY": self._api_key, "Content-Type": "application/json"}
        try:
            async with self._session.get(
                f"{_GMGN_HOST}{path}", params=params, headers=headers
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                text = await resp.text()
                if resp.status == 429:
                    reset = rl.parse_gmgn_reset_time(text)
                    if reset is None:
                        rem = rl.parse_gmgn_remaining_seconds(text)
                        if rem is not None:
                            reset = time.time() + rem
                    rl.note_gmgn_429(reset, source="holder_flow")
                else:
                    # Surface non-429 failures — these are why the registry was
                    # silently empty in iter36/38 (e.g. 404 wrong endpoint, 401
                    # bad auth, Cloudflare challenge).
                    self._log_rl(
                        f"http_{resp.status}",
                        f"[HolderFlow] GMGN {path} HTTP {resp.status}: {text[:200]}",
                    )
                return None
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_rl(f"err_{type(e).__name__}", f"[HolderFlow] GMGN {path} error: {e}")
            return None

    def _log_rl(self, key: str, msg: str):
        """Rate-limited logger: emit each distinct `key` at most once per 60s so
        a persistent failure is visible without spamming the log every poll."""
        now = time.time()
        last = self._err_log_ts.get(key, 0.0)
        if now - last >= 60.0:
            logger.warning(msg)
            self._err_log_ts[key] = now
        else:
            logger.debug(msg)

    def _ban_active(self) -> bool:
        return rl.gmgn_banned()

    # ── Internal: polling loops ───────────────────────────────────────────

    async def _poll_loop(self):
        """Main polling loop: fetch smartmoney trades and match against watched tokens."""
        while self._running:
            try:
                if self._ban_active():
                    await asyncio.sleep(1.0)
                    continue
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[HolderFlow] Poll error: {e}")
            await asyncio.sleep(_POLL_INTERVAL)

    async def _poll_once(self):
        """Single poll of the smartmoney feed."""
        if not self._watched:
            return

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
            if tx_hash and tx_hash in self._seen_tx:
                continue
            if tx_hash:
                self._seen_tx.add(tx_hash)
            # LRU eviction
            if len(self._seen_tx) > self._seen_tx_max:
                self._seen_tx = set(list(self._seen_tx)[-self._seen_tx_max // 2:])

            maker = trade.get("maker", "")
            side = trade.get("side", "")
            amount_usd = float(trade.get("amount_usd", 0.0) or 0.0)
            amount_sol = float(trade.get("quote_amount", 0.0) or 0.0)
            timestamp = int(trade.get("timestamp", int(time.time())))

            # Check if maker is a tracked wallet for this token
            state = self._watched[base_addr]
            tag = state.wallet_registry.get(maker, "")

            # If not in registry, check maker_info tags from the feed.
            # iter38: accept several tag spellings GMGN uses (e.g. "dev",
            # "token_deployer", "sniper", "bundler", "rat_trader", "insider")
            # and normalise them onto our tracked set so provenance is captured
            # even when the registry is still populating.
            if not tag:
                maker_info = trade.get("maker_info", {}) or {}
                maker_tags = maker_info.get("tags", []) or []
                if isinstance(maker_tags, str):
                    maker_tags = [maker_tags]
                norm = {str(t).lower() for t in maker_tags}
                tag = _normalise_tag(norm)

            # Only emit if it's a tracked/known tag or a significant sell.
            # Untagged dust sells are dropped; everything else is persisted so
            # the recorded dataset retains the full order-flow picture.
            if not tag and side == "sell" and amount_usd < _MIN_SELL_USD:
                continue

            # iter38: a large sell with no recognised provenance is tagged
            # "whale" so it stays distinguishable from a verified dev/insider
            # sell in the recorded dataset (and so the engine's require_tag
            # gate can treat them differently).
            if not tag and side == "sell" and amount_usd >= _MIN_SELL_USD:
                tag = LARGE_SELLER_TAG

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
                    logger.debug(f"[HolderFlow] DB insert failed: {e}")

            # Emit to queue
            try:
                self._event_queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

            if side == "sell" and amount_usd >= _MIN_SELL_USD:
                logger.info(
                    f"[HolderFlow] DEV SELL {base_addr[:8]} "
                    f"wallet={maker[:8]} tag={tag or 'unknown'} "
                    f"amount=${amount_usd:.2f}"
                )

    async def _fetch_smartmoney_trades(self) -> list[dict]:
        """Fetch the latest smartmoney trades from GMGN (exist auth, no signature)."""
        data = await self._get("/v1/user/smartmoney", {"chain": "sol", "limit": 200})
        if not data or not isinstance(data, dict):
            return []
        # Response shape: {"code":0, "data":{"list":[...]}, "message":...}
        payload = data.get("data", data)
        if isinstance(payload, dict):
            return payload.get("list", []) or []
        return []

    async def _registry_loop(self):
        """Serially fetch per-token wallet registries (rate-limit friendly)."""
        while self._running:
            try:
                mint = await asyncio.wait_for(self._registry_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # Periodic re-refresh of stale registries
                now = time.time()
                if not self._ban_active():
                    for m, state in list(self._watched.items()):
                        if now - state.last_registry_refresh > _REGISTRY_REFRESH_INTERVAL:
                            await self._refresh_wallet_registry(m)
                continue
            except asyncio.CancelledError:
                break
            if self._ban_active():
                # Re-queue after the ban lifts
                await asyncio.sleep(2.0)
                try:
                    self._registry_queue.put_nowait(mint)
                except asyncio.QueueFull:
                    pass
                continue
            await self._refresh_wallet_registry(mint)
            # Pace registry fetches — 4 tag requests per token
            await asyncio.sleep(1.0)

    async def _refresh_wallet_registry(self, mint: str):
        """Fetch the dev/sniper/bundler wallet list for a token.

        iter38: made robust + observable.  Previously a non-200 / empty payload
        silently left the registry empty (the root cause of the all-untagged
        iter38 finding).  Now each tag fetch is logged, the response shape is
        validated, and a token that still has an empty registry after a full
        pass is re-queued for a retry (with backoff) instead of being marked
        "refreshed" and forgotten.
        """
        state = self._watched.get(mint)
        if not state:
            return
        fetched_any = False
        for tag in _TRACKED_TAGS:
            if self._ban_active():
                return
            data = await self._get(
                "/v1/market/token_top_holders",
                {"chain": "sol", "address": mint, "tag": tag, "limit": 50},
            )
            holders = _extract_holder_list(data)
            if holders is None:
                # Log the actual response shape so the failure mode is visible.
                shape = type(data).__name__
                if isinstance(data, dict):
                    keys = list(data.keys())[:5]
                    shape += f" keys={keys}"
                elif data is not None:
                    shape += f" val={str(data)[:120]}"
                logger.info(
                    f"[HolderFlow] Registry fetch tag={tag} {mint[:8]}: "
                    f"no holders ({shape})"
                )
                continue
            added = 0
            for holder in holders:
                # Tolerate both "address" and "wallet"/"maker" key spellings.
                wallet = (holder.get("address")
                          or holder.get("wallet")
                          or holder.get("maker") or "")
                if wallet:
                    state.wallet_registry[wallet] = tag
                    added += 1
            fetched_any = fetched_any or added > 0
            logger.info(
                f"[HolderFlow] Registry tag={tag} {mint[:8]}: +{added} wallets "
                f"(total {len(state.wallet_registry)})"
            )
            await asyncio.sleep(0.3)  # gentle pacing between tag fetches
        state.last_registry_refresh = time.time()
        n = len(state.wallet_registry)
        if n == 0:
            # Empty registry — schedule a retry rather than accepting it.  The
            # periodic re-refresh in _registry_loop will pick this up again on
            # the next cycle; log at WARNING so it's visible.
            state.registry_retry_count = getattr(state, "registry_retry_count", 0) + 1
            logger.warning(
                f"[HolderFlow] Registry EMPTY for {mint[:8]} after full pass "
                f"(attempt {state.registry_retry_count}) — will retry; "
                f"dev/insider sells will be UNtagged until registry populates"
            )
        else:
            state.registry_retry_count = 0
            logger.info(f"[HolderFlow] Registry ready for {mint[:8]}: {n} wallets")


# ── Process-wide shared singleton ─────────────────────────────────────────
# All live sessions and recorders share ONE monitor so the GMGN API is polled
# once per interval regardless of how many tokens are being watched.
_shared_monitor: Optional[HolderFlowMonitor] = None


def get_shared_monitor() -> HolderFlowMonitor:
    """Return the process-wide shared HolderFlowMonitor (created on first use)."""
    global _shared_monitor
    if _shared_monitor is None:
        _shared_monitor = HolderFlowMonitor()
    return _shared_monitor
