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

# Poll interval for the smartmoney feed (seconds) while at least one token is
# watched.  /v1/user/smartmoney is a GLOBAL rolling window of the newest N
# Solana smartmoney trades, so the poll interval bounds BOTH delivery latency
# AND coverage: a trade that scrolls out of the window between two polls is
# lost permanently, not merely delayed.  A 30 s interval left the engine's
# 30 s entry-block / 15 s exit windows racing the poll phase, while the
# backtester replays every row at its exact on-chain timestamp — so live
# entered trades the backtest had blocked and exited dev sells seconds late.
# Rate-limit safety comes from the 429 ban backoff in `_ban_active()`, not
# from a slow nominal interval, and the monitor is a process-wide singleton
# so this is one request per interval regardless of session count.
_POLL_INTERVAL = 5.0

# Poll interval while nothing is watched — no session can consume the events,
# so idle polling would only burn API quota.
_POLL_INTERVAL_IDLE = 30.0

# How often to refresh the per-token wallet registry (seconds)
_REGISTRY_REFRESH_INTERVAL = 60.0

# Minimum sell amount (USD) to consider significant
_MIN_SELL_USD = 100.0

# ── iter66: realtime on-chain holder-flow watcher ─────────────────────────
# A second event source alongside the GMGN poller, built entirely on
# infrastructure this process already runs — no third-party indexer, no API
# key, no rate limits, no indexing lag:
#   • dev wallet     resolved from the token's own PumpSwap pool account
#                    ('coin_creator', byte 211) with ONE HTTP RPC call;
#   • dev sells/buys detected by accountSubscribe'ing the dev wallet's SPL
#                    token account and diffing its balance (shared Solana WS
#                    hub) — an event lands within ~1 s of the swap;
#   • whale sells    classified straight off the session's trade stream via
#                    observe_trade() (size + direction need no identity).
# Coverage mirrors the iter43 production gate semantics (require_tag=0):
#   tag='dev'    creator-wallet trades            (verified provenance)
#   tag='whale'  any other wallet selling ≥ _MIN_SELL_USD
# GMGN stays as the enrichment source for sniper/bundler/rat_trader tags,
# for bonding-curve tokens with no pool to decode, and as a late fallback.
# Live probe 2026-08-26: PumpPortal subscribeTokenTrade does NOT deliver
# trades for graduated PumpSwap tokens (active tokens returned 0 events),
# so the PumpPortal hub is NOT used here.
_ONCHAIN_IDLE_SLEEP = 1.0        # idle wait on the trade queue (seconds)
_ONCHAIN_RESOLVE_RETRY = 30.0    # dev-wallet resolve retry cadence
_SOL_USD_TTL = 60.0              # cached SOL/USD spot refresh cadence
# Rough estimate used only when no live SOL/USD quote is reachable at all.
# Keeps the detector alive (a missed dev sell is worse than a slightly-off
# $ threshold); refreshed the instant any quote succeeds.
_SOL_USD_FALLBACK = 200.0

# Cache for the cached SOL/USD spot getter below.
_sol_usd_cache: dict = {"ts": 0.0, "px": 0.0}


async def _get_sol_usd() -> float:
    """Cached SOL/USD spot price (60s TTL, CoinGecko simple-price).

    Falls back to the last known value, then to _SOL_USD_FALLBACK so the
    on-chain watcher's USD notional (SOL spent × SOL/USD) never goes blind.
    """
    now = time.time()
    if _sol_usd_cache["px"] > 0 and now - _sol_usd_cache["ts"] < _SOL_USD_TTL:
        return _sol_usd_cache["px"]
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=6)
        ) as s:
            async with s.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
            ) as r:
                if r.status == 200:
                    px = float((await r.json(content_type=None))["solana"]["usd"])
                    if px > 0:
                        _sol_usd_cache.update(ts=now, px=px)
                        return px
    except Exception:
        pass
    return _sol_usd_cache["px"] if _sol_usd_cache["px"] > 0 else _SOL_USD_FALLBACK


async def _fetch_pumpswap_pool_creator(pool_address: str) -> str:
    """Resolve the token creator (dev) wallet from its PumpSwap pool account.

    One HTTP RPC getAccountInfo per watched token (the caller caches the
    result for the monitor's lifetime).  Uses the same publicnode endpoint
    and account layout as pumpfun_client.PumpSwapRPCClient._load_pool —
    'coin_creator' sits at byte offset 211 of the pool data.
    Returns "" when the account is missing / not a PumpSwap pool layout.
    """
    import base64
    from pumpfun_client import SOLANA_RPC_HTTP, _decode_pool_account

    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getAccountInfo",
        "params": [pool_address, {"encoding": "base64", "commitment": "processed"}],
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as s:
            async with s.post(SOLANA_RPC_HTTP, json=payload) as r:
                if r.status != 200:
                    return ""
                data = await r.json(content_type=None)
    except Exception:
        return ""
    value = (data.get("result") or {}).get("value")
    if not value:
        return ""
    try:
        raw = base64.b64decode(value["data"][0])
        return str(_decode_pool_account(raw).get("coin_creator") or "")
    except Exception:
        return ""


async def _fetch_dev_token_account(owner: str, mint: str) -> tuple[str, Optional[int]]:
    """Locate *owner*'s SPL token account for *mint* (one HTTP RPC call).

    Returns (account_address, current_raw_balance) or ("", None) when the
    wallet holds no account for this mint / the lookup fails.  Uses
    getTokenAccountsByOwner so no PDA derivation is needed and non-ATA
    token accounts are found too.
    """
    from pumpfun_client import SOLANA_RPC_HTTP, _decode_spl_token_amount
    import base64 as _b64

    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccountsByOwner",
        "params": [
            owner,
            {"mint": mint},
            {"encoding": "base64", "commitment": "processed"},
        ],
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as s:
            async with s.post(SOLANA_RPC_HTTP, json=payload) as r:
                if r.status != 200:
                    return "", None
                data = await r.json(content_type=None)
    except Exception:
        return "", None
    result = (data.get("result") or {}).get("value") or []
    if not result:
        return "", None
    addr = str(result[0].get("pubkey") or "")
    if not addr:
        return "", None
    initial_raw: Optional[int] = None
    try:
        raw = _b64.b64decode(result[0]["account"]["data"][0])
        initial_raw = _decode_spl_token_amount(raw)
    except Exception:
        pass
    return addr, initial_raw


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
    # PumpSwap pool address (when known) — used by the iter66 on-chain
    # watcher to resolve the token creator (dev) wallet.
    pool_address: Optional[str] = None
    # iter66: last token price seen in SOL (fed by observe_trade) so the
    # dev-wallet ATA balance deltas can be converted into SOL/USD notionals.
    last_price_sol: float = 0.0
    # iter66: dev wallet's SPL token account for this mint (resolved once),
    # plus the previous raw balance for delta extraction.
    dev_ata: Optional[str] = None
    prev_ata_raw: Optional[int] = None
    # wallet → tag mapping (dev/sniper/bundler wallets for this token)
    wallet_registry: dict[str, str] = field(default_factory=dict)
    last_registry_refresh: float = 0.0
    # Round-robin cursor: which tag in _TRACKED_TAGS to fetch next (iter38
    # rate-limit fix — one tag per _refresh_wallet_registry call).
    _tag_cursor: int = 0
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
        # iter66: per-mint on-chain dev-sell watcher tasks, resolved dev
        # wallets and rate-limited warn-log timestamps.
        self._onchain_tasks: dict[str, asyncio.Task] = {}
        self._dev_wallets: dict[str, str] = {}
        self._warn_log_ts: dict[str, float] = {}
        # Realtime delivery wakeup: set on every dispatched event so consumer
        # pumps (main.py's _holder_flow_pump) can pull DB rows immediately
        # instead of waiting out their 1 s poll sleep — events must reach the
        # live engine at tick time for entry-gate parity with the backtester
        # (which always sees events at their exact on-chain timestamp).
        self._new_event_signal: asyncio.Event = asyncio.Event()
        # Set by watch_token() so the poll loop breaks its sleep and polls a
        # freshly watched token immediately instead of waiting out a full
        # interval (a session opening just after a poll tick would otherwise
        # start blind to the feed for the whole interval).
        self._watch_signal: asyncio.Event = asyncio.Event()
        # Newest feed timestamp observed in the previous poll window, and the
        # oldest one in the current window — used to detect that the global
        # rolling window turned over between polls (⇒ events lost, not late).
        self._feed_newest_ts: int = 0

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
        # iter66: stop all per-mint on-chain watchers too
        for task in list(self._onchain_tasks.values()):
            task.cancel()
        for mint, task in list(self._onchain_tasks.items()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._onchain_tasks.clear()
        if self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        logger.info("[HolderFlow] Monitor stopped")

    def watch_token(self, mint: str, recording_id: Optional[int] = None,
                    pool_address: Optional[str] = None):
        """Start watching a token for dev/insider sells.

        `pool_address` (optional, iter66): the token's PumpSwap pool — lets
        the realtime on-chain watcher resolve the creator (dev) wallet from
        the pool account itself instead of relying on the GMGN registry.
        """
        if mint in self._watched:
            # Update recording_id if provided
            if recording_id is not None:
                self._watched[mint].recording_id = recording_id
            if pool_address and not self._watched[mint].pool_address:
                self._watched[mint].pool_address = pool_address
            return
        self._watched[mint] = _TokenWatchState(
            mint=mint, recording_id=recording_id, pool_address=pool_address,
        )
        logger.info(f"[HolderFlow] Watching {mint[:8]} (recording_id={recording_id})")
        # Break the poll loop's sleep so this token is covered by the very next
        # feed fetch instead of after up to a full interval of blindness.
        try:
            self._watch_signal.set()
        except Exception:
            pass
        # Queue a wallet-registry fetch (processed serially by the registry loop)
        try:
            self._registry_queue.put_nowait(mint)
        except asyncio.QueueFull:
            pass
        # iter66: spawn the realtime on-chain dev-sell watcher.  It runs
        # independently of (and regardless of) the GMGN poller — no API key,
        # no rate limits.  Delivery to the engine stays exclusively via the
        # DB id-cursor pump in main.py so backtest replay stays exactly-once.
        if mint not in self._onchain_tasks:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass  # no running loop (sync test context) — GMGN-only fallback
            else:
                self._onchain_tasks[mint] = asyncio.create_task(
                    self._onchain_devsell_loop(mint)
                )

    def unwatch_token(self, mint: str):
        """Stop watching a token."""
        if mint in self._watched:
            del self._watched[mint]
            logger.info(f"[HolderFlow] Unwatched {mint[:8]}")
        task = self._onchain_tasks.pop(mint, None)
        if task:
            task.cancel()
        self._dev_wallets.pop(mint, None)

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

    def new_event_signal(self) -> asyncio.Event:
        """Asyncio Event set on every dispatched event (realtime wakeup)."""
        return self._new_event_signal

    # ── iter66: realtime on-chain dev-sell detection ──────────────────────

    def _warn_once_min(self, key: str, msg: str, min_interval: float = 60.0):
        """Rate-limited warning log (one line per key per min_interval)."""
        now = time.time()
        if now - self._warn_log_ts.get(key, 0.0) < min_interval:
            return
        self._warn_log_ts[key] = now
        logger.warning(msg)

    def _claim_tx(self, tx_hash: str) -> bool:
        """True if this tx hash was never seen before (and records it).

        Empty hashes always claim True (nothing to dedupe on).  Shared by
        both event sources so whichever sees an on-chain trade first wins —
        the other source's late duplicate is dropped instead of double-
        persisting to the holder_flow table.
        """
        if not tx_hash:
            return True
        if tx_hash in self._seen_tx:
            return False
        self._seen_tx.add(tx_hash)
        # LRU eviction
        if len(self._seen_tx) > self._seen_tx_max:
            self._seen_tx = set(list(self._seen_tx)[-self._seen_tx_max // 2:])
        return True

    def _is_near_duplicate(self, state: _TokenWatchState, wallet: str,
                           side: str, ts: int) -> bool:
        """Fallback dedupe for events without tx hashes.

        Matches any same-side event within ±5 s regardless of wallet when
        either wallet is unknown — the vault-diff trade stream (and the ATA
        watcher) carry no trader identity/tx hash, and a cross-source
        duplicate of the same on-chain sale must not double-persist.  The
        cost is that two genuinely simultaneous same-side dumps may collapse
        into one event, which is immaterial to the engine's binary gates.
        """
        for e in reversed(state.recent_events):
            if abs(e.timestamp - ts) > 5:
                break
            if e.side != side:
                continue
            if not wallet or not e.wallet or e.wallet == wallet:
                return True
        return False

    def _dispatch_event(self, state: _TokenWatchState, event: HolderFlowEvent):
        """Common tail for both sources (GMGN poll + on-chain stream):
        recent-window bookkeeping, DB persistence, queue emission, logging."""
        state.recent_events.append(event)
        cutoff = int(time.time()) - int(state.recent_window)
        state.recent_events = [e for e in state.recent_events if e.timestamp >= cutoff]

        # Persist to DB if recording
        if state.recording_id is not None:
            try:
                data_store.insert_holder_flow(
                    recording_id=state.recording_id,
                    t=event.timestamp,
                    wallet=event.wallet,
                    tag=event.tag,
                    side=event.side,
                    amount_usd=event.amount_usd,
                    amount_sol=event.amount_sol,
                    tx_hash=event.tx_hash,
                )
            except Exception as e:
                logger.debug(f"[HolderFlow] DB insert failed: {e}")

        # Emit to queue
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

        # Wake any consumer pump waiting on new events (see __init__).
        try:
            self._new_event_signal.set()
        except Exception:
            pass

        if event.side == "sell" and event.amount_usd >= _MIN_SELL_USD:
            logger.info(
                f"[HolderFlow] SELL {event.mint[:8]} "
                f"wallet={event.wallet[:8]} tag={event.tag or 'unknown'} "
                f"amount=${event.amount_usd:.2f}"
            )

    def _handle_onchain_trade(self, state: _TokenWatchState, dev_wallet: str,
                              trade: dict, sol_usd: float):
        """Match one normalised trade-stream trade against dev provenance
        and dispatch a HolderFlowEvent when it qualifies.

        Matches:
          • ``dev``     — trader pubkey equals the pool's coin_creator
                          (verified provenance, works under require_tag=0/1).
                          Non-dev trades from the raw stream are ignored here
                          to avoid flooding holder_flow with anonymous whale
                          events that cause entry starvation under require_tag=0.
                          GMGN polling provides curated smartmoney/whale events.
        """
        trader = str(trade.get("trader") or "")
        side = str(trade.get("tx_type") or "")
        if side not in ("buy", "sell"):
            return
        is_dev = bool(trader) and trader == dev_wallet
        amount_sol = float(trade.get("sol_amount") or 0.0)
        amount_usd = amount_sol * max(sol_usd, 0.0)

        if is_dev:
            tag = "dev"
        elif trader and trader in state.wallet_registry:
            tag = state.wallet_registry[trader]
        else:
            return

        tx_hash = str(trade.get("tx_hash") or "")
        ts = int(float(trade.get("timestamp") or time.time()))
        if tx_hash and not self._claim_tx(tx_hash):
            return  # already delivered via the other source
        if self._is_near_duplicate(state, trader, side, ts):
            return

        self._dispatch_event(state, HolderFlowEvent(
            mint=state.mint,
            wallet=trader,
            tag=tag,
            side=side,
            amount_usd=amount_usd,
            amount_sol=amount_sol,
            timestamp=ts,
            tx_hash=tx_hash,
        ))

    async def _onchain_devsell_loop(self, mint: str):
        """Realtime per-mint dev-wallet watcher (iter66).

        1. Resolve the dev wallet from the token's PumpSwap pool account
           (`coin_creator`) — one RPC call, retried every 30 s until the
           pool is known/decoded.  On failure the token degrades to the
           GMGN-only path (logged once per minute, never fatal).
        2. Locate the dev wallet's SPL token account (ATA) for this mint —
           one HTTP RPC call.
        3. ``accountSubscribe`` that ATA on the shared Solana WS hub (the
           same connection PumpSwapRPCClient already uses) and diff the raw
           balance on every notification: a decrease is a dev sell, an
           increase a dev buy.  No third-party indexer anywhere in the path,
           so there is nothing to rate-limit and no indexing lag — the event
           lands within ~1 s of the on-chain swap (WS push + pump tick).

        Whale sells from any other wallet are classified separately by
        :meth:`observe_trade` directly on the session's trade stream.
        Delivery for both sources stays exclusively via the holder_flow
        table (the same rows the backtester replays); main.py's id-cursor
        pump forwards them into the engine and fires the immediate-exit
        check.
        """
        q: Optional[asyncio.Queue] = None
        account = ""
        hub = None
        dev = ""
        try:
            while True:
                state = self._watched.get(mint)
                if state is None:
                    return  # unwatched — task exits
                if not dev:
                    pool = state.pool_address
                    if pool:
                        dev = await _fetch_pumpswap_pool_creator(pool)
                        if dev:
                            self._dev_wallets[mint] = dev
                            # Seed the GMGN registry too so late-arriving GMGN
                            # trades from this wallet carry the verified 'dev'
                            # tag even when the registry fetch lagged/failed
                            # (the iter38 root cause).
                            state.wallet_registry[dev] = "dev"
                            logger.info(
                                f"[HolderFlow:onchain] {mint[:8]}… dev wallet "
                                f"{dev[:8]}… resolved from pool"
                            )
                        else:
                            self._warn_once_min(
                                mint,
                                f"[HolderFlow:onchain] {mint[:8]}… no coin_creator "
                                f"on pool {pool[:8]}… — GMGN-only fallback",
                            )
                    else:
                        self._warn_once_min(
                            mint,
                            f"[HolderFlow:onchain] {mint[:8]}… no pool address "
                            f"— GMGN-only fallback",
                        )
                    if not dev:
                        await asyncio.sleep(_ONCHAIN_RESOLVE_RETRY)
                        continue
                if q is None or not account:
                    ata, initial_raw = await _fetch_dev_token_account(dev, mint)
                    if not ata:
                        # Dev holds no token account (or lookup failed) — retry
                        # a few times, then stay GMGN-only for this session.
                        self._warn_once_min(
                            f"{mint}:ata",
                            f"[HolderFlow:onchain] {mint[:8]}… no dev token "
                            f"account found — retrying / GMGN-only fallback",
                        )
                        await asyncio.sleep(_ONCHAIN_RESOLVE_RETRY)
                        continue
                    state = self._watched.get(mint)
                    if state is None:
                        return
                    state.dev_ata = ata
                    state.prev_ata_raw = initial_raw
                    account = ata
                    from pumpfun_client import _solana_hub
                    hub = _solana_hub
                    q = asyncio.Queue(maxsize=256)
                    await hub.subscribe(account, q)
                    logger.info(
                        f"[HolderFlow:onchain] {mint[:8]}… watching dev ATA "
                        f"{ata[:8]}… (initial={initial_raw})"
                    )
                try:
                    note = await asyncio.wait_for(q.get(), timeout=_ONCHAIN_IDLE_SLEEP)
                except asyncio.TimeoutError:
                    continue
                cur = self._watched.get(mint)
                if cur is None:
                    return
                if not isinstance(note, tuple) or len(note) != 2:
                    continue
                try:
                    _, amount_raw = note
                    prev = cur.prev_ata_raw
                    cur.prev_ata_raw = int(amount_raw)
                    if prev is None or prev == amount_raw:
                        continue
                    delta_raw = int(amount_raw) - prev
                    tokens = abs(delta_raw) / 1e6          # pump.fun mints: 6 dp
                    price = cur.last_price_sol
                    sol_amt = tokens * price if price > 0 else 0.0
                    side = "buy" if delta_raw > 0 else "sell"
                    pseudo_trade = {
                        "trader": dev,
                        "tx_type": side,
                        "sol_amount": sol_amt,
                        "price": price,
                        "timestamp": float(int(time.time())),
                        "tx_hash": "",     # balance notifications carry no signature
                    }
                    self._handle_onchain_trade(cur, dev, pseudo_trade,
                                               self._cached_sol_usd())
                except Exception as e:
                    # One malformed notification must never kill the watcher
                    logger.debug(f"[HolderFlow:onchain] {mint[:8]}… note error: {e}")
        except asyncio.CancelledError:
            pass
        finally:
            if q is not None and hub is not None and account:
                try:
                    await hub.unsubscribe(account, q)
                except Exception:
                    pass

    def observe_trade(self, mint: str, trade: dict):
        """Feed one live trade into the monitor (called from main.py's stream
        loops for every non-synthetic tick — never blocks).

        Updates the last-seen SOL price (used to size ATA-balance deltas),
        and matches trades whose trader pubkey matches the resolved dev wallet.
        """
        state = self._watched.get(mint)
        if state is None or not isinstance(trade, dict):
            return
        price = float(trade.get("price") or 0.0)
        if price > 0:
            state.last_price_sol = price
        side = str(trade.get("tx_type") or "")
        if side not in ("buy", "sell"):
            return
        self._handle_onchain_trade(state, self._dev_wallets.get(mint, ""),
                                   trade, self._cached_sol_usd())

    def _cached_sol_usd(self) -> float:
        """Non-blocking read of the cached SOL/USD spot for hot paths."""
        px = _sol_usd_cache["px"]
        if px > 0 and time.time() - _sol_usd_cache["ts"] < _SOL_USD_TTL:
            return px
        # Stale/empty: kick a background refresh (once per 5 s max) and use
        # last-known / fallback meanwhile — never block the trade loop.
        try:
            asyncio.get_running_loop()
            if time.time() - self._warn_log_ts.get("_solusd_refresh", 0.0) > 5.0:
                self._warn_log_ts["_solusd_refresh"] = time.time()
                asyncio.create_task(_get_sol_usd())
        except RuntimeError:
            pass
        return px if px > 0 else _SOL_USD_FALLBACK

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
        """Main polling loop: fetch smartmoney trades and match against watched tokens.

        Sleeps `_POLL_INTERVAL` while any token is watched (the interval bounds
        both delivery latency and feed coverage — see the constant's comment)
        and `_POLL_INTERVAL_IDLE` otherwise.  The sleep is interruptible by
        `watch_token()` so a session that opens mid-interval is polled at once.
        """
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
            interval = _POLL_INTERVAL if self._watched else _POLL_INTERVAL_IDLE
            try:
                await asyncio.wait_for(self._watch_signal.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            else:
                self._watch_signal.clear()

    async def _poll_once(self):
        """Single poll of the smartmoney feed."""
        if not self._watched:
            return

        # Fetch smartmoney trades
        trades = await self._fetch_smartmoney_trades()
        if not trades:
            return

        self._note_feed_coverage(trades)

        # Filter for watched tokens and check against wallet registry
        watched_mints = set(self._watched.keys())
        for trade in trades:
            base_addr = trade.get("base_address", "")
            if base_addr not in watched_mints:
                continue

            tx_hash = trade.get("transaction_hash", "")
            if not self._claim_tx(tx_hash):
                continue

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

            self._dispatch_event(state, event)

    def _note_feed_coverage(self, trades: list[dict]):
        """Detect that the global smartmoney window turned over between polls.

        `/v1/user/smartmoney` returns only the newest N Solana smartmoney
        trades.  When the OLDEST row of this poll is newer than the NEWEST row
        of the previous poll, every trade in between scrolled out unseen — those
        events are lost permanently, so the live engine can never reach parity
        with a backtest replay of the same recording no matter how fast the
        pump delivers.  Log it (rate-limited) so the residual gap is visible
        instead of silently degrading the entry gate.
        """
        stamps = []
        for t in trades:
            try:
                ts = int(t.get("timestamp") or 0)
            except (TypeError, ValueError):
                continue
            if ts > 0:
                stamps.append(ts)
        if not stamps:
            return
        oldest, newest = min(stamps), max(stamps)
        prev_newest = self._feed_newest_ts
        if prev_newest and oldest > prev_newest:
            self._log_rl(
                "feed_window_gap",
                f"[HolderFlow] smartmoney window turned over between polls — "
                f"{oldest - prev_newest}s of feed history was never seen "
                f"(window span {newest - oldest}s over {len(stamps)} rows). "
                f"Lower _POLL_INTERVAL (now {_POLL_INTERVAL:.0f}s) to close it.",
            )
        self._feed_newest_ts = max(prev_newest, newest)

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
        """Serially fetch per-token wallet registries with strict rate-limit pacing.

        iter38/iter67: Paced at >= 3.0s between requests (<= 0.33 req/s),
        ensuring GMGN's rate limits are never triggered.

        Prioritizes:
          1. Tokens newly queued via watch_token().
          2. Tokens actively completing their initial or refresh tag rotation (cursor > 0).
          3. Tokens whose last complete refresh is older than _REGISTRY_REFRESH_INTERVAL (60s).
        """
        while self._running:
            try:
                if self._ban_active():
                    await asyncio.sleep(2.0)
                    continue

                target_mint = None
                # 1. Check for newly queued tokens
                try:
                    target_mint = self._registry_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass

                # 2. Check for tokens mid-rotation (cursor > 0)
                if not target_mint and self._watched:
                    for m, state in list(self._watched.items()):
                        if state._tag_cursor > 0:
                            target_mint = m
                            break

                # 3. Check for stalest token exceeding _REGISTRY_REFRESH_INTERVAL
                if not target_mint and self._watched:
                    now = time.time()
                    stalest_age = -1.0
                    for m, state in list(self._watched.items()):
                        age = now - state.last_registry_refresh
                        if age >= _REGISTRY_REFRESH_INTERVAL and age > stalest_age:
                            target_mint = m
                            stalest_age = age

                if target_mint and target_mint in self._watched:
                    await self._refresh_wallet_registry(target_mint)
                    await asyncio.sleep(3.5)
                else:
                    await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[HolderFlow] Registry loop error: {e}")
                await asyncio.sleep(1.0)

    async def _refresh_wallet_registry(self, mint: str):
        """Fetch ONE tracked-wallet tag for a token (round-robin per call).

        iter38 rate-limit fix: GMGN allows roughly one request per 30s before
        429-ing.  The old code fetched all 4 tags (dev/sniper/bundler/
        rat_trader) back-to-back with 0.3s pacing — dev succeeded, then
        sniper got 429'd and `last_registry_refresh` was set anyway, marking
        the token "done" with only 25% tag coverage.  Now each call fetches
        exactly ONE tag, rotating through `_TRACKED_TAGS` via a per-token
        cursor (`_tag_cursor`).  A 429 only delays one tag; the cursor stays
        put so the next cycle retries the same tag instead of skipping it.
        """
        state = self._watched.get(mint)
        if not state:
            return
        if self._ban_active():
            return

        tag = _TRACKED_TAGS[state._tag_cursor]
        data = await self._get(
            "/v1/market/token_top_holders",
            {"chain": "sol", "address": mint, "tag": tag, "limit": 50},
        )
        holders = _extract_holder_list(data)
        if holders is None:
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
            # Don't advance the cursor — retry the same tag next cycle.
            return

        added = 0
        for holder in holders:
            wallet = (holder.get("address")
                       or holder.get("wallet")
                       or holder.get("maker") or "")
            if wallet:
                state.wallet_registry[wallet] = tag
                added += 1
        logger.info(
            f"[HolderFlow] Registry tag={tag} {mint[:8]}: +{added} wallets "
            f"(total {len(state.wallet_registry)})"
        )
        # Advance to the next tag for next cycle (wraps around).
        state._tag_cursor = (state._tag_cursor + 1) % len(_TRACKED_TAGS)
        # Mark as refreshed only when all tags have been fetched at least once.
        tags_fetched = sum(1 for t in _TRACKED_TAGS
                           if any(w_tag == t for w_tag in state.wallet_registry.values()))
        if tags_fetched >= len(_TRACKED_TAGS) or state._tag_cursor == 0:
            state.last_registry_refresh = time.time()
        n = len(state.wallet_registry)
        if n == 0 and state._tag_cursor == 0:
            # Full rotation with zero wallets — log and let the cursor loop
            # again (the periodic re-refresh in _registry_loop will retry).
            state.registry_retry_count = getattr(state, "registry_retry_count", 0) + 1
            logger.warning(
                f"[HolderFlow] Registry EMPTY for {mint[:8]} after full rotation "
                f"(attempt {state.registry_retry_count}) — will retry; "
                f"dev/insider sells will be UNtagged until registry populates"
            )
        elif n > 0:
            state.registry_retry_count = 0


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
