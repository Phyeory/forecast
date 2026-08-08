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
  - Optional Jito MEV bribe for ultra-fast block inclusion
"""

from __future__ import annotations
import asyncio
import base64
import random
import time
import logging
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

import aiohttp
import base58
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from solders.system_program import transfer, TransferParams
from solders.pubkey import Pubkey
from solders.message import MessageV0
from solders.hash import Hash

from strategy_engine import Signal, Direction, Regime
from engine_factory import create_engine
from live_session_logger import SessionJournal

# SPL Token / ATA program ids (used for deterministic ATA derivation and
# Token-2022 detection without pulling in the heavyweight `spl-token` package).
SPL_TOKEN_PROGRAM_ID  = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM_ID        = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

logger = logging.getLogger("live-trader")


class _SessionTagFilter(logging.Filter):
    """Stamp every live-trader LogRecord with the emitting LiveTrader's
    SessionJournal so each session's FileHandler only writes its own lines
    (see live_session_logger.SessionJournal)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "_sid"):
            record._sid = LiveTrader.current_journal
        return True


logger.addFilter(_SessionTagFilter())

# ── Jupiter & Solana constants ────────────────────────────────────────────────
# NOTE: Using the newer Swap API v1 (lite-api.jup.ag) instead of the V6 API
# (public.jupiterapi.com). The V6 on-chain program (JUP6L...) does NOT handle
# Token-2022 mints correctly in its Route instruction, causing error 6014
# (IncorrectTokenProgramID). The newer API generates transactions for an
# updated program that supports Token-2022 natively.
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL  = "https://lite-api.jup.ag/swap/v1/swap"
WSOL_MINT         = "So11111111111111111111111111111111111111112"

# ── Multi-RPC fanout ──────────────────────────────────────────────────────────
# Broadcast every signed TX to ALL of these endpoints simultaneously.
# If ANY one forwards it to a slot leader, the TX lands on-chain.
# All are free, no API key required.  Order matters: first = primary
# (used for reads like balance queries and confirmation polling).
#
# IMPORTANT: api.mainnet-beta.solana.com is the MOST rate-limited free RPC
# (public, no API key, aggressive per-IP caps).  Using it as the primary read
# endpoint causes 429 storms that also starve pumpfun_client.py's gated reads
# (getAccountInfo/getMultipleAccounts on every PumpSwapRPCClient connect).
# publicnode has much higher per-IP limits; ankr is a reliable secondary.
# mainnet-beta is kept LAST for broadcast redundancy but NOT for reads.
SOLANA_RPCS = [
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
    "https://api.mainnet-beta.solana.com",
]
SOLANA_RPC_PRIMARY = SOLANA_RPCS[0]

# Confirmation polling and retry tuning
# Hot path: confirmation polled at ~standby-tick cadence; rebroadcast fires
# every 1.0s so a slow slot leader gets retried within the 12s window.
CONFIRM_TIMEOUT_S:  float = 12.0   # per-attempt confirm window (short — we retry w/ fresh blockhash)
CONFIRM_POLL_MS:    float = 0.5    # poll cadence while waiting for confirmation
CONFIRM_REBROADCAST_S: float = 1.5 # re-broadcast same signed TX every N s while confirming

# ── BUY FINALITY INVARIANT ────────────────────────────────────────────────────
# A buy may ONLY be declared "failed" once the transaction is *cryptographically
# dead* — i.e. its blockhash has expired (~150 slots ≈ 60 s) — AND a direct
# on-chain wallet-balance probe confirms zero tokens arrived.  The 12 s
# per-attempt confirm window above is NOT a failure boundary for buys: a TX
# that shows no status at t=12 s can still land at t=20–60 s (RPC forwarding
# queues keep it alive until the blockhash expires).  Declaring failure at
# t=12 s previously produced unmonitored on-chain positions that the algorithm
# believed were closed.
#
# While the outcome is unknown the trade stays in status="pending", which
# blocks new entries everywhere (update(), force_buy(), watchdog, no-motion).
BUY_CONFIRM_TIMEOUT_S:   float = 75.0  # absolute cap: blockhash (~60 s) + grace
BUY_SETTLE_GRACE_S:      float = 8.0   # post-expiry grace for processed-commitment lag
BUY_BALANCE_PROBE_EVERY_S: float = 1.5 # wallet-balance probe cadence while settling
BUY_SETTLE_POLL_S:       float = 0.4   # settle-loop iteration cadence (fast)

# ── RETRY / ESCALATION POLICY ────────────────────────────────────────────────
# ASYMMETRIC FAILURE POLICY (per requirements):
#   • BUY  — single attempt, NO retry.  If the broadcast is rejected or the TX
#            never confirms, the trade is logged as failed and the algorithm is
#            returned to FLAT (current_trade=None, engine notified closed) so
#            it can re-enter on the next signal.
#   • SELL — retry IMMEDIATELY and WITHOUT LIMIT until the on-chain balance is
#            zero.  A missed sell on a memecoin can mean riding it to zero, so
#            the sell loop re-quotes/re-broadcasts back-to-back (escalating
#            slippage/fee) and the watchdog remains as the final backstop.
#
# SINGLE-CLOSE INVARIANT: a position is closed (``confirm_sell``) EXACTLY ONCE,
# and only AFTER the sell TX is confirmed on-chain.  Broadcast does NOT close
# the trade — so N failed attempts followed by 1 success yields exactly ONE
# closed trade, never N phantom closes.
#
# AMOUNT ACCURACY: sells ALWAYS read the authoritative on-chain token balance
# before building the swap.  This eliminates `Custom: 6024` (insufficient token
# account balance) caused by a stale/overstated cached figure (e.g. the buy's
# Jupiter outAmount inflated by slippage or a partial fill).
QUOTE_RETRIES_PER_GROUP = 3          # fresh quotes fetched per sell attempt group
NONSIMULATION_ABORT_CODES = frozenset({6024, 1, 0x1771})  # swallowed — handled by re-quote
PRIORITY_FEE_ESCALATION  = [100_000, 100_000, 100_000, 100_000]  # micro-lamports — fixed 0.0001 SOL
MAX_PRIORITY_FEE         = 100_000  # micro-lamports — 0.0001 SOL, never exceeded

# Watchdog: if the on-chain position hasn't reached zero after a confirmed sell
# signal within this many seconds, force another sell pass.
WATCHDOG_INTERVAL_S: float = 6.0
WATCHDOG_TIMEOUT_S:  float = 45.0

# Market-cap floor stop: maximum time (seconds) the session will wait for the
# emergency sell to confirm on-chain before terminating anyway.  Generous on
# purpose — a panicked market needs the slippage/priority-fee escalation
# ladder in execute_sell to run its course.  The background watchdog
# (_monitor_trade) keeps trying to settle even after this fires.
MCAP_STOP_SELL_TIMEOUT_SECONDS: float = 300.0

# ── Hot-path cache tuning ─────────────────────────────────────────────────────
# How often the background tasks refresh the cached balances / blockhash.
# These run OFF the critical path: the swap reads the cache instantly instead
# of blocking on an RPC round-trip before it can even build the transaction.
# Slowed down from 0.9s→2.0s and 4.0s→8.0s to reduce RPC pressure (the
# previous cadences generated ~1.6 req/s of background traffic per session,
# which caused 429 storms on mainnet-beta when combined with pumpfun_client).
BALANCE_CACHE_TTL_S:   float = 8.0   # SOL + token balance refresh cadence
BLOCKHASH_REFRESH_S:   float = 2.0   # blockhash refresh cadence (~2 slots)
BLOCKHASH_MAX_AGE_S:   float = 25.0  # blockhash is valid ~150 slots (~60s); use well under that


def _derive_ata(owner: "Pubkey", mint: "Pubkey", token_program: "Pubkey") -> "Pubkey":
    """
    Deterministic ATA derivation — avoids importing the `spl-token`
    Python package (which is a heavy dependency).
    """
    pda, _ = Pubkey.find_program_address(
        [bytes(owner), bytes(token_program), bytes(mint)],
        ATA_PROGRAM_ID,
    )
    return pda


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

    # Class-level pointer to the currently-active session journal.  The
    # dashboard runs one live-trader session at a time per mint; the
    # _SessionTagFilter on the "live-trader" logger stamps each record with
    # this journal so every session's console.log only contains its own lines.
    current_journal: Optional[SessionJournal] = None

    def __init__(
        self,
        token_mint: str,
        keypair: Keypair,
        buy_size_sol: float = 0.01,
        slippage_bps: int = 1500,
        priority_fee_lamports: int = 100_000,
        min_market_cap_usd: float = 6_000.0,
        engine_kwargs: Optional[dict] = None,

        # Skip on-chain simulation on the hot path (saves ~300 ms per swap).
        # Simulation is still run on explicit test buys if desired.
        skip_simulation: bool = True,
        engine_version: int = 1,

        # ── No-motion stop ───────────────────────────────────────────────
        # If the price has not moved for this many wall-clock seconds while
        # a position is open, force-close it.  Set to 0 to disable.
        no_motion_stop_seconds: float = 300.0,
    ):
        if engine_kwargs is None:
            engine_kwargs = {}

        self.engine = create_engine(engine_version, **engine_kwargs)
        self.token_mint = token_mint
        self.keypair = keypair
        self.wallet_pubkey = str(keypair.pubkey())

        # ── Per-session persistent logging ─────────────────────────────────
        # Physical files under backend/data/live_logs/<session>/:
        #   console.log  — every console log line emitted by this session
        #   trades.jsonl — structured ledger of every trade transaction
        #   (entry/exit timestamps, broadcast/confirm/fail, sizes, tx hashes)
        self.journal = SessionJournal(
            token_mint,
            self.wallet_pubkey,
            meta={
                "buy_size_sol": buy_size_sol,
                "slippage_bps": slippage_bps,
                "engine_version": engine_version,
                "engine_kwargs": engine_kwargs,
                "min_market_cap_usd": min_market_cap_usd,
                "skip_simulation": skip_simulation,
            },
        )
        LiveTrader.current_journal = self.journal
        logger.info(
            f"[SESSION] Logging to {self.journal.dir} "
            f"(console.log + trades.jsonl)"
        )
        self.buy_size_sol = buy_size_sol
        self.slippage_bps = slippage_bps
        self.priority_fee_lamports = 100_000  # fixed: 0.0001 SOL per transaction
        self.skip_simulation = skip_simulation

        # ── Market-cap safety floor ───────────────────────────────────────
        # If the live market cap (USD) drops below this value while a
        # position is open, an emergency sell is triggered and the session
        # is flagged for shutdown by main.py.
        self.min_market_cap_usd: float = min_market_cap_usd
        self._last_market_cap_usd: float = 0.0
        self.mcap_stop_triggered: bool = False   # set True once triggered
        self.no_motion_stop_triggered: bool = False  # set True once no-motion exit fires

        # ── No-motion stop tracking ──────────────────────────────────────
        self.no_motion_stop_seconds: float = no_motion_stop_seconds
        self._last_motion_ts: float = 0.0       # wall-clock timestamp of last price change
        self._last_motion_price: float = 0.0    # close price at that moment

        self.stats = LiveTraderStats()
        self.current_trade: Optional[LiveTrade] = None
        self.trade_history: list[LiveTrade] = []
        self.signals_log: list[dict] = []

        self._last_price: float = 0.0
        self._token_decimals: int = 6  # pump.fun default
        self._token_balance: int = 0   # raw token units held

        # Async swap task tracker (so we don't overlap swaps)
        self._swap_in_flight: bool = False

        # Persistent aiohttp session — reusing TCP connections eliminates
        # per-request handshake overhead (~50-150 ms per call).
        self._session: Optional[aiohttp.ClientSession] = None

        # Websocket broadcast callback (set by main.py)
        self.broadcast_fn = None

        # ── Candle buffering (match backtester's 4-state expansion) ───────
        # We buffer the current accumulating candle.  When main.py signals
        # is_new=True we know the previous candle is now final.  We expand
        # it into 4 intra-candle sub-states and run engine.update() on each
        # — exactly as the backtester does via ft.update().
        self._current_accumulating: Optional[dict] = None     # live-updating candle
        self._last_engine_result: dict = {}                   # latest engine output

        # Pending signal model — mirrors ForwardTester._pending_buy/exit.
        # A signal detected during candle N's 4-state expansion is NOT
        # acted on immediately.  Instead, engine.notify_trade_opened/closed()
        # is called at sub-state 1 (the open) of candle N+1's expansion,
        # BEFORE engine.update() runs on that open tick.  This is exactly
        # what ForwardTester.update() does (Step 1 before Step 2).
        # For the LIVE TRADER, the actual swap fires immediately (no N+1
        # bar wait) — only the engine's in_position state is deferred to
        # match the indicator evolution of the backtester.
        self._pending_buy: bool = False
        self._pending_buy_reason: str = ""
        self._pending_exit: bool = False
        self._pending_exit_reason: str = ""

        # ── Lifecycle / watchdog state ─────────────────────────────────────
        # Never give up on a sell: if the on-chain balance still shows tokens
        # WATCHDOG_TIMEOUT_S after the exit signal fired, the watchdog
        # re-triggers execute_sell so positions can't ride to zero.
        self._alive: bool = True
        self._last_exit_signal_ts: float = 0.0
        self._watchdog_task: Optional[asyncio.Task] = None

        # ── Hot-path caches (balance + blockhash) ──────────────────────────
        # The pre-optimization hot path blocked on an RPC read BEFORE building
        # the swap TX (~0.3–1.5s each).  We keep a warm cache of both balances
        # and a fresh blockhash so a signal can go straight to Jupiter with
        # zero pre-RPC on the critical path.  Background tasks refresh the
        # cache continuously; the swap path only touches the cache.
        self._cached_sol_balance: float = 0.0
        self._cached_token_balance: int = 0
        self._cached_balance_ts: float = 0.0
        self._cached_blockhash: Optional[str] = None
        self._cached_blockhash_lvh: Optional[int] = None
        self._cached_blockhash_ts: float = 0.0
        self._balance_cache_task: Optional[asyncio.Task] = None
        self._blockhash_task: Optional[asyncio.Task] = None

        # ── Hot-path RPC affinity ──────────────────────────────────────────
        # Free public RPCs vary wildly in latency. Track the last RPC that
        # successfully served a balance/confirm read and try it FIRST next
        # time — empirically cuts "first-wins" wait time in half. Falls back
        # to the full fanout if that RPC ever fails or stalls.
        self._fast_rpc_idx: int = 0

        # ── Buy-finality state ─────────────────────────────────────────────
        # While a buy TX is broadcast-but-unconfirmed the trade stays
        # "pending" and ALL entry paths are blocked (see _is_buy_pending).
        # If the wallet ever holds tokens that no tracked trade accounts for
        # (e.g. a buy landed after we gave up on it — should no longer be
        # possible, but belt-and-braces), the watchdog adopts the bag into a
        # monitored trade instead of leaving it invisible.
        self._adopted_bag: bool = False

    # ── Structured session ledger helpers ─────────────────────────────────────

    def set_session_meta(self, **meta):
        """Record extra session metadata (e.g. recording_id) into the ledger."""
        if meta:
            self.journal.event("session_meta", meta)

    def _journal_event(self, kind: str, trade: Optional["LiveTrade"] = None, **data):
        """Append a structured trade-transaction record to trades.jsonl.

        Always includes the SOL/tx-hash detail passed in plus a full snapshot
        of the affected trade (entry/exit timestamps, sizes, PnL, status)."""
        payload = dict(data)
        if trade is not None:
            payload["trade"] = trade.to_dict()
        elif self.current_trade is not None:
            payload["trade"] = self.current_trade.to_dict()
        self.journal.event(kind, payload)

    def _is_buy_pending(self) -> bool:
        """True while a buy TX's on-chain fate is still unknown.

        A pending buy must block every entry path (signal buys, manual buys,
        watchdog) — entering a second position while the first may still land
        is exactly the unmonitored-position bug this invariant fixes.
        """
        ct = self.current_trade
        return (
            ct is not None
            and ct.status == "pending"
            and not ct.tx_hash_sell
            and ct.exit_time is None
        )

    def start_watchdog(self):
        """Spawn `_monitor_trade` exactly once per session (called by main.py).

        Also spins up the two hot-path cache tasks (balance + blockhash).
        These keep the swap path free of any pre-RPC round-trips so a signal
        can go from "detected" to "broadcast" in a single Jupiter round-trip.
        """
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.ensure_future(self._monitor_trade())
            logger.info("[WATCHDOG] Task started")
        self._start_caches()

    def _start_caches(self):
        """Lazily start the balance + blockhash background refresh tasks."""
        if self._balance_cache_task is None or self._balance_cache_task.done():
            self._balance_cache_task = asyncio.ensure_future(self._balance_cache_loop())
            logger.info("[CACHE] Balance cache task started")
        if self._blockhash_task is None or self._blockhash_task.done():
            self._blockhash_task = asyncio.ensure_future(self._blockhash_loop())
            logger.info("[CACHE] Blockhash task started")

    async def close(self):
        """Stop the watchdog, cache tasks and close the HTTP session."""
        self._alive = False
        for task_attr in ("_watchdog_task", "_balance_cache_task", "_blockhash_task"):
            task = getattr(self, task_attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                setattr(self, task_attr, None)
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        # Flush + close this session's physical log files (console.log /
        # trades.jsonl) with a final summary record.
        summary = {
            "stats": self.stats.to_dict(),
            "open_trade": self.current_trade.to_dict() if self.current_trade else None,
            "trade_count": len(self.trade_history),
            "log_dir": str(self.journal.dir),
        }
        self.journal.close(summary)
        if LiveTrader.current_journal is self.journal:
            LiveTrader.current_journal = None

    # ── Session lifecycle ─────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        """Return (or lazily create) the persistent aiohttp session."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=20,                # max parallel connections
                ttl_dns_cache=300,       # 5-min DNS cache
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers={"Content-Type": "application/json"},
            )
        return self._session

    # ── Hot-path cache background loops ───────────────────────────────────────

    async def _balance_cache_loop(self):
        """
        Background loop that keeps SOL + token balances warm.

        The swap hot path reads ``_cached_sol_balance`` / ``_cached_token_balance``
        instead of awaiting an RPC read before it can build the transaction.
        This shaves ~0.3–1.5s off every entry/exit.  The first iteration runs
        immediately so the cache is populated before the first signal can fire.
        """
        while self._alive:
            try:
                sol, tok = await asyncio.gather(
                    self._get_sol_balance(),
                    self._get_token_balance(),
                )
                self._cached_sol_balance = sol
                if tok > 0:
                    self._cached_token_balance = tok
                    self._token_balance = tok
                self._cached_balance_ts = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[CACHE] balance refresh error: {e}")
            await asyncio.sleep(BALANCE_CACHE_TTL_S)

    async def _blockhash_loop(self):
        """
        Background loop that keeps a fresh blockhash warm.

        The Jupiter quote response carries a blockhash, but by the time the
        quote → swap-build → sign sequence finishes the hash can be a few
        seconds old.  A fresher hash means more slots for the TX to land,
        which directly improves first-attempt landing rate.  When we rebuild
        the TX locally with this hash we remove blockhash freshness from the
        quote path entirely.
        """
        while self._alive:
            try:
                res = await self._fetch_latest_blockhash()
                if res:
                    bh, lvh = res
                    self._cached_blockhash = bh
                    self._cached_blockhash_lvh = lvh
                    self._cached_blockhash_ts = time.time()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[CACHE] blockhash refresh error: {e}")
            await asyncio.sleep(BLOCKHASH_REFRESH_S)

    async def _fetch_latest_blockhash(self) -> Optional[tuple]:
        """Fetch a fresh (processed) blockhash from the fastest known RPC.

        Returns ``(blockhash, last_valid_block_height)`` — the height is the
        cryptographic expiry of the hash (~150 slots out).  A transaction is
        guaranteed-dead once the chain passes that height, which is the ONLY
        moment a buy may be declared failed.
        """
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getLatestBlockhash",
            "params": [{"commitment": "processed"}],
        }

        def _extract(data: dict) -> Optional[tuple]:
            try:
                value = data.get("result", {}).get("value", {})
                bh = value.get("blockhash")
                lvh = value.get("lastValidBlockHeight")
                if isinstance(bh, str) and isinstance(lvh, int):
                    return (bh, lvh)
                return (bh, None) if isinstance(bh, str) else None
            except Exception:
                return None

        return await self._rpc_fanout_first_wins(payload, timeout_s=1.2, result_fn=_extract)

    async def _get_block_height(self) -> Optional[int]:
        """Current chain block height (processed commitment)."""
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getBlockHeight",
            "params": [{"commitment": "processed"}],
        }

        def _extract(data: dict) -> Optional[int]:
            try:
                val = data.get("result")
                return int(val) if val is not None else None
            except Exception:
                return None

        result = await self._rpc_fanout_first_wins(payload, timeout_s=1.2, result_fn=_extract)
        return result if isinstance(result, int) else None

    def _get_cached_sol_balance(self) -> float:
        return self._cached_sol_balance

    def _get_cached_token_balance(self) -> int:
        return self._cached_token_balance

    def _get_fresh_blockhash(self) -> Optional[tuple]:
        """Return ``(blockhash, last_valid_block_height)`` if still fresh."""
        if self._cached_blockhash is None:
            return None
        if (time.time() - self._cached_blockhash_ts) > BLOCKHASH_MAX_AGE_S:
            return None
        return (self._cached_blockhash, getattr(self, "_cached_blockhash_lvh", None))

    async def _rpc_fanout_first_wins(self, payload: dict, timeout_s: float,
                                       result_fn) -> Optional[object]:
        """
        Faster replacement for the `asyncio.gather(*all-RPCs)` pattern used
        on the hot read path (balance, signature-status fetches).

        Behaviour:
          1. Fire the cached-fast RPC first with a very short timeout.
             If it answers within ``timeout_s``, return its result and
             update the fast-RPC cache in-place.
          2. Otherwise, fan out to ALL remaining RPCs concurrently with
             the same short timeout and return the first non-None answer.
          3. If a fanout RPC succeeds, promote it to ``_fast_rpc_idx``.

        ``result_fn`` extracts the parsed payload-specific value from a
        parsed JSON dict; it should return ``None`` to indicate a soft
        failure (e.g. empty account list) so other RPCs are still tried.

        Free-RPC reality: public mainnet-beta is heavily rate-limited so
        landing latency ≈ 1.5–4 s per failed read kills the trade hot path.
        Bypassing the slow ones after a single short timeout dramatically
        cuts median round-trip while keeping robustness.
        """
        s = await self._get_session()
        order = list(range(len(SOLANA_RPCS)))
        # Move cached-fast RPC to front of search order.
        order.pop(order.index(self._fast_rpc_idx))
        order.insert(0, self._fast_rpc_idx)

        async def _call(rpc_url: str) -> Optional[object]:
            try:
                async with s.post(
                    rpc_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout_s),
                ) as r:
                    if r.status != 200:
                        return None
                    data = await r.json()
                    if "error" in data:
                        return None
                    return result_fn(data)
            except Exception:
                return None
            finally:
                pass

        # Phase 1: try the best-known RPC with a short timeout.
        try:
            best_url = SOLANA_RPCS[self._fast_rpc_idx]
            first = await _call(best_url)
            if first is not None:
                return first
        except Exception:
            pass

        # Phase 2: fan out to the other RPCs concurrently.
        others = [SOLANA_RPCS[i] for i in order[1:]]
        results = await asyncio.gather(
            *[_call(u) for u in others], return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception) or r is None:
                continue
            # Promote this RPC to fast cache for next call.
            self._fast_rpc_idx = order[1 + i]
            return r
        return None

    async def cleanup(self):
        """
        Emergency cleanup before WebSocket disconnect.

        If a swap is already in flight, wait briefly for it to land — killing the
        WS while a sell TX is mid-flight would abandon a position mid-execution.
        Then, if a position is STILL open, run the emergency sell path (which
        now retries indefinitely until tokens leave the wallet).
        """
        if self._swap_in_flight:
            logger.info("[CLEANUP] Swap in flight — waiting up to 20s for it to finish")
            deadline = time.time() + 20.0
            while self._swap_in_flight and time.time() < deadline:
                await asyncio.sleep(0.5)

        if self.current_trade is not None:
            logger.warning(
                f"[CLEANUP] Position still open on disconnect for {self.token_mint[:8]}… "
                f"— launching emergency sell"
            )
            self.current_trade.status = "closing"
            self.current_trade.exit_reason = "connection_closed"
            self._journal_event(
                "emergency_cleanup_sell", trade=self.current_trade,
                reason="connection_closed",
            )
            try:
                sig = await asyncio.wait_for(
                    self.execute_sell("connection_closed"),
                    timeout=45.0,
                )
                if sig:
                    logger.info(f"[CLEANUP] Emergency sell completed: {sig}")
                else:
                    logger.error("[CLEANUP] Emergency sell gave up — watchdog will keep trying in background")
            except asyncio.TimeoutError:
                logger.error("[CLEANUP] Emergency sell timed out after 45s — watchdog continues in background")

        await self.close()

    # ── Jupiter helpers ───────────────────────────────────────────────────────

    async def _get_quote(self, input_mint: str, output_mint: str, amount: int) -> Optional[dict]:
        """Fetch a Jupiter swap quote via the Swap API v1."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(self.slippage_bps),
            # Exclude Meteora DLMM — sells routed through it consistently
            # timeout while Pump.fun Amm routes confirm in ~1s.  Forcing
            # Jupiter to skip Meteora keeps buys and sells on the same
            # protocol (Pump.fun Amm) for pump.fun tokens.
            "excludeDexes": "Meteora DLMM",
            # NOTE: onlyDirectRoutes removed — restricting to direct routes
            # can force a Pump.fun AMM route that fails on Token-2022 via
            # the V6 program. Letting Jupiter find the best route (possibly
            # multi-hop) also avoids problematic single-hop Token-2022
            # interactions.
            #
            # platformFeeBps intentionally omitted — including it (even as
            # "0") triggers fee-collection code paths that can cause
            # IncorrectTokenProgramID (error 6014) on Token-2022 mints.
        }
        logger.info(f"[QUOTE] Fetching quote: {input_mint[:8]}… → {output_mint[:8]}… amount={amount}")
        try:
            s = await self._get_session()
            async with s.get(
                JUPITER_QUOTE_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=3.0),
            ) as r:
                body = await r.text()
                if r.status != 200:
                    logger.error(f"[QUOTE FAILED] HTTP {r.status}: {body}")
                    return None
                quote = json.loads(body)
                out_amt = quote.get('outAmount', '?')
                route_plan = quote.get('routePlan', [])
                swaps = [rp.get('swapInfo', {}).get('label', '?') for rp in route_plan]
                logger.info(f"[QUOTE OK] outAmount={out_amt} route={'→'.join(swaps)} priceImpact={quote.get('priceImpactPct', '?')}%")
                return quote
        except Exception as e:
            logger.error(f"[QUOTE ERROR] {e}")
            return None

    async def _get_swap_tx(self, quote: dict, priority_fee_override: Optional[int] = None) -> Optional[str]:
        """
        Build a versioned swap transaction via Jupiter.

        NOTE: asLegacyTransaction is intentionally NOT set (defaults to
        False / versioned transaction).  Legacy transactions do not support
        the address-lookup-tables that Token-2022 routes require, which is
        the root cause of IncorrectTokenProgramID (error 6014).

        `computeUnitPriceMicroLamports` is the Jupiter-API priority-fee knob
        (micro-lamports per compute unit).  Pass `priority_fee_override` to
        escalate fees on retry groups — a stuck TX at a low fee often simply
        needs more juice on the next fresh blockhash.
        """
        cu_price = int(priority_fee_override) if priority_fee_override is not None else int(self.priority_fee_lamports)
        body = {
            "quoteResponse": quote,
            "userPublicKey": self.wallet_pubkey,
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": cu_price,
            "dynamicComputeUnitLimit": True,
            # asLegacyTransaction intentionally omitted — versioned TXs
            # handle Token-2022 correctly.
        }
        logger.info(f"[SWAP TX] Building swap transaction…")
        try:
            s = await self._get_session()
            async with s.post(
                JUPITER_SWAP_URL,
                json=body,
                timeout=aiohttp.ClientTimeout(total=3.0),
            ) as r:
                resp_body = await r.text()
                if r.status != 200:
                    logger.error(f"[SWAP TX FAILED] HTTP {r.status}: {resp_body}")
                    return None
                data = json.loads(resp_body)
                swap_tx = data.get("swapTransaction")
                if swap_tx:
                    logger.info(f"[SWAP TX OK] Transaction built ({len(swap_tx)} chars b64)")
                else:
                    logger.error(f"[SWAP TX FAILED] No swapTransaction in response: {resp_body[:200]}")
                return swap_tx
        except Exception as e:
            logger.error(f"[SWAP TX ERROR] {e}")
            return None

    async def _simulate_tx(self, signed_b64: str) -> dict:
        """
        Simulate a signed transaction on-chain BEFORE broadcasting.
        Returns {"ok": True/False, "error": str|None, "logs": list}.
        Only called explicitly; skipped on the hot path when skip_simulation=True.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "simulateTransaction",
            "params": [
                signed_b64,
                {
                    "encoding": "base64",
                    "commitment": "processed",
                    "replaceRecentBlockhash": True,
                },
            ],
        }
        try:
            s = await self._get_session()
            async with s.post(
                SOLANA_RPC_PRIMARY, json=payload,
                timeout=aiohttp.ClientTimeout(total=7),
            ) as r:
                data = await r.json()
                result = data.get("result", {}).get("value", {})
                err = result.get("err")
                logs = result.get("logs", [])
                if err:
                    logger.error(f"[SIMULATE FAILED] err={err}")
                    for i, log_line in enumerate(logs):
                        logger.error(f"  sim_log[{i}]: {log_line}")
                    return {"ok": False, "error": str(err), "logs": logs}
                logger.info(f"[SIMULATE OK] {len(logs)} log lines, units={result.get('unitsConsumed', '?')}")
                return {"ok": True, "error": None, "logs": logs}
        except Exception as e:
            logger.warning(f"[SIMULATE WARN] Simulation call failed ({e}), proceeding anyway…")
            return {"ok": True, "error": None, "logs": []}  # Don't block on sim failure

    async def _confirm_tx(
        self,
        sig: str,
        timeout_s: float = CONFIRM_TIMEOUT_S,
        signed_b64: Optional[str] = None,
    ) -> dict:
        """
        Poll multiple RPCs for transaction confirmation.  While polling, the
        SAME signed transaction is re-broadcast to every RPC every
        CONFIRM_REBROADCAST_S seconds — a Solana blockhash is valid for ~150
        slots (~60 s), so within one CONFIRM_TIMEOUT_S window a rebroadcast is
        safe (TX is idempotent by signature) and dramatically increases the
        chance that a slow forwarder actually lands it before expiry.

        Rebroadcast is HARD-CAPPED at the first CONFIRM_TIMEOUT_S window even
        when the caller passes a longer ``timeout_s`` (the buy-settle path
        polls for the full blockhash-validity window).  Rebroadcasting past
        that window would keep the TX alive in RPC forwarding queues and
        undermine the "blockhash expired ⇒ TX is dead" guarantee the buy
        path relies on.

        Returns:
            {"confirmed": bool, "error": str | None, "slot": int | None}
        """
        logger.info(f"[CONFIRM] Waiting for confirmation of {sig[:16]}… (max {timeout_s}s)")
        start = time.time()
        s = await self._get_session()
        last_broadcast = 0.0

        while time.time() - start < timeout_s:
            # ── Aggressive rebroadcast of the same signed TX while confirming ──
            # Capped at the first CONFIRM_TIMEOUT_S window (see docstring).
            if (
                signed_b64
                and (time.time() - last_broadcast) >= CONFIRM_REBROADCAST_S
                and (time.time() - start) < CONFIRM_TIMEOUT_S
            ):
                last_broadcast = time.time()
                asyncio.ensure_future(self._broadcast_multi(signed_b64))

            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [[sig], {"searchTransactionHistory": False}],
            }

            async def _poll_one(rpc_url: str) -> Optional[dict]:
                try:
                    async with s.post(
                        rpc_url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=1.5),
                    ) as r:
                        data = await r.json()
                        statuses = data.get("result", {}).get("value", [None])
                        return statuses[0] if statuses else None
                except Exception:
                    return None

            results = await asyncio.gather(
                *[_poll_one(url) for url in SOLANA_RPCS],
                return_exceptions=True,
            )

            for status in results:
                if isinstance(status, Exception) or status is None:
                    continue
                err = status.get("err")
                conf = status.get("confirmationStatus")
                slot = status.get("slot")
                if err:
                    logger.error(f"[CONFIRM FAILED] sig={sig[:16]}… err={err} slot={slot}")
                    await self._fetch_tx_logs(sig)
                    return {"confirmed": False, "error": str(err), "slot": slot}
                if conf in ("confirmed", "finalized"):
                    logger.info(f"[CONFIRM OK] sig={sig[:16]}… status={conf} slot={slot}")
                    return {"confirmed": True, "error": None, "slot": slot}

            await asyncio.sleep(CONFIRM_POLL_MS)

        logger.warning(f"[CONFIRM TIMEOUT] sig={sig[:16]}… not confirmed within {timeout_s}s")
        return {"confirmed": False, "error": "timeout", "slot": None}

    async def _get_signature_status(self, sig: str) -> Optional[dict]:
        """Single non-blocking signature-status probe across the RPC fanout.

        Returns the raw status dict (``{"err", "confirmationStatus", "slot"}``)
        from the first RPC that has one, or ``None`` if no RPC knows the sig
        yet.  Unlike ``_confirm_tx`` this performs ONE round of probes with no
        internal wait loop, so the buy-settle loop can interleave status probes
        with balance probes and blockheight checks at a fast cadence.
        """
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getSignatureStatuses",
            "params": [[sig], {"searchTransactionHistory": False}],
        }

        def _extract(data: dict) -> Optional[dict]:
            statuses = data.get("result", {}).get("value", [None])
            st = statuses[0] if statuses else None
            return st if isinstance(st, dict) else None

        result = await self._rpc_fanout_first_wins(payload, timeout_s=1.2, result_fn=_extract)
        return result if isinstance(result, dict) else None

    async def _fetch_tx_logs(self, sig: str):
        """Fetch and log the full transaction logs for a given signature."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                sig,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
            ],
        }
        try:
            s = await self._get_session()
            async with s.post(
                SOLANA_RPC_PRIMARY, json=payload,
                timeout=aiohttp.ClientTimeout(total=7),
            ) as r:
                data = await r.json()
                result = data.get("result")
                if not result:
                    logger.warning(f"[TX LOGS] No result for {sig[:16]}… (may not be on-chain yet)")
                    return
                meta = result.get("meta", {})
                err = meta.get("err")
                logs = meta.get("logMessages", [])
                logger.info(f"[TX LOGS] sig={sig[:16]}… err={err}")
                for i, log_line in enumerate(logs):
                    logger.info(f"  tx_log[{i}]: {log_line}")
        except Exception as e:
            logger.warning(f"[TX LOGS] Failed to fetch logs: {e}")


    async def _broadcast_multi(self, signed_b64: str) -> Optional[str]:
        """
        Broadcast a signed transaction to ALL RPCs simultaneously.

        Key differences from the old single-RPC approach:
          - Fan-out to every endpoint in SOLANA_RPCS concurrently.
          - maxRetries=0 — we control retries ourselves with fresh
            blockhashes (Jupiter gives us a new one on each quote).
          - If ANY endpoint accepts it, the TX will be forwarded to
            slot leaders.  This dramatically improves landing rates.
        """
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
                    "maxRetries": 0,
                },
            ],
        }

        async def _send_one(rpc_url: str) -> Optional[str]:
            try:
                s = await self._get_session()
                async with s.post(
                    rpc_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=2.0),
                ) as r:
                    data = await r.json()
                    if "error" in data:
                        logger.debug(f"[BROADCAST] {rpc_url[:30]}… rejected: {data['error']}")
                        return None
                    return data.get("result")
            except Exception as e:
                logger.debug(f"[BROADCAST] {rpc_url[:30]}… error: {e}")
                return None

        results = await asyncio.gather(
            *[_send_one(url) for url in SOLANA_RPCS],
            return_exceptions=True,
        )

        # Take the first non-None signature
        sig = None
        accepted = 0
        for r in results:
            if isinstance(r, str) and r:
                if sig is None:
                    sig = r
                accepted += 1

        if sig:
            logger.info(f"[BROADCAST OK] sig={sig} ({accepted}/{len(SOLANA_RPCS)} RPCs accepted)")
            logger.info(f"[SOLSCAN] https://solscan.io/tx/{sig}")
        else:
            logger.error(f"[BROADCAST FAILED] All {len(SOLANA_RPCS)} RPCs rejected the transaction")

        return sig

    def _rebuild_tx_with_blockhash(self, tx: "VersionedTransaction", blockhash: str) -> "VersionedTransaction":
        """
        Rebuild a versioned transaction with a fresh recent-blockhash.

        Jupiter's quote → swap round-trip takes ~0.5–1s, during which the
        blockhash embedded in the returned TX ages.  Swapping in a blockhash
        that is <1s old maximises the number of slots the TX has to land,
        which materially improves first-attempt landing rate on congested
        free RPCs.

        `solders.message.MessageV0.recent_blockhash` is read-only, so we
        reconstruct the message field-by-field with the new hash.  All other
        fields (header, account keys, instructions, address lookup tables) are
        copied verbatim — only the blockhash changes, so the instruction set
        and account references are untouched.  On any failure we fall back to
        the original TX (behaviour identical to the pre-optimization code).
        """
        try:
            msg = tx.message
            new_msg = MessageV0(
                header=msg.header,
                account_keys=list(msg.account_keys),
                recent_blockhash=Hash.from_string(blockhash),
                instructions=list(msg.instructions),
                address_table_lookups=list(msg.address_table_lookups),
            )
            return VersionedTransaction(new_msg, [self.keypair])
        except Exception as e:
            logger.debug(f"[BLOCKHASH] rebuild failed ({e}) — using Jupiter blockhash")
            return tx

    async def _sign_and_send(self, swap_tx_b64: str, wait_for_confirmation: bool = False) -> Optional[dict]:
        """
        Sign the base64-encoded versioned transaction and broadcast it
        to ALL RPCs simultaneously.

        Hot path (wait_for_confirmation=False):
          - Rebuild TX with a fresh cached blockhash (maximises landing window)
          - skip_simulation=True  → no simulate call (saves ~300 ms)
          - Multi-RPC fanout for maximum landing probability
          - Returns the INSTANT the TX is on the wire.
            Confirmation + rebroadcast run as a background task.

        Returns ``{"sig": str, "last_valid_block_height": int | None}`` on
        success, ``None`` if every RPC rejected the broadcast.  The height is
        the cryptographic expiry of the TX's blockhash — the only moment a
        buy may be declared failed.
        """
        try:
            raw_tx = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(raw_tx)

            # ── Rebuild with the freshest cached blockhash if available ────
            last_valid_height: Optional[int] = None
            fresh = self._get_fresh_blockhash()
            if fresh:
                fresh_bh, fresh_lvh = fresh
                if fresh_bh:
                    tx = self._rebuild_tx_with_blockhash(tx, fresh_bh)
                last_valid_height = fresh_lvh

            # Sign with our keypair
            signed_tx = VersionedTransaction(tx.message, [self.keypair])
            signed_bytes = bytes(signed_tx)
            signed_b64 = base64.b64encode(signed_bytes).decode()

            logger.info(f"[SIGN] Transaction signed ({len(signed_bytes)} bytes, fresh_bh={bool(fresh_bh)})")

            # ── Optional simulation (disabled on hot path) ─────────────────
            if not self.skip_simulation:
                sim = await self._simulate_tx(signed_b64)
                if not sim["ok"]:
                    error_detail = sim['error']
                    logger.error(f"[SIGN_AND_SEND] Aborting — simulation failed: {error_detail}")
                    await self._broadcast_status(
                        "tx_simulation_failed",
                        f"Simulation failed: {error_detail}",
                    )
                    return None

            # ── Broadcast to ALL RPCs simultaneously ──────────────────────
            sig = await self._broadcast_multi(signed_b64)

            if sig is None:
                return None

            if wait_for_confirmation:
                # ── Wait for confirmation (with in-flight rebroadcast) ───────
                confirm_result = await self._confirm_tx(sig, signed_b64=signed_b64)
                if not confirm_result["confirmed"]:
                    logger.error(f"[TX FAILED ON-CHAIN] sig={sig[:16]}… error={confirm_result['error']}")
                    return None
            else:
                # ── Fire-and-forget confirmation (also rebroadcasts) ─────────
                # NOTE: rebroadcast is capped at the first CONFIRM_TIMEOUT_S
                # window inside _confirm_tx — we must NOT keep the TX alive in
                # RPC forwarding queues past the point where the settle task
                # is deciding whether the buy is dead.
                asyncio.ensure_future(self._background_confirm(sig, signed_b64))

            return {"sig": sig, "last_valid_block_height": last_valid_height}

        except Exception as e:
            logger.error(f"[SIGN_AND_SEND ERROR] {e}", exc_info=True)
            return None

    async def _background_confirm(self, sig: str, signed_b64: Optional[str] = None):
        """Background task: poll for confirmation and log the result.
        Passes signed_b64 through so rebroadcast happens here too."""
        confirm_result = await self._confirm_tx(sig, signed_b64=signed_b64)
        if not confirm_result["confirmed"]:
            logger.error(
                f"[TX FAILED ON-CHAIN] sig={sig[:16]}… error={confirm_result['error']}"
            )

    async def _get_sol_balance(self) -> float:
        """Fetch wallet SOL balance — fast-path cached RPC, fanout otherwise.

        Returns 0.0 if no RPC answered; the caller treats 0 as 'stale RPC'
        and falls back to cached figures rather than failing the swap.
        """
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getBalance",
            "params": [self.wallet_pubkey, {"commitment": "processed"}],
        }

        def _extract(data: dict) -> Optional[float]:
            try:
                val = data.get("result", {}).get("value")
                if val is None:
                    return None
                return float(val) / 1e9
            except Exception:
                return None

        result = await self._rpc_fanout_first_wins(payload, timeout_s=1.2, result_fn=_extract)
        return result if isinstance(result, float) else 0.0

    async def _get_token_balance(self, commitment: str = "processed") -> int:
        """
        Authoritative on-chain token balance (raw smallest units).

        Hot-path rewrite: tries the cached-fast RPC first with a short
        1.2 s timeout, then falls back to a parallel fanout across all
        remaining RPCs (each capped at 1.2 s). First non-zero account
        amount wins. Auto-detects Token vs Token-2022 program ownership
        from the parsed account info.

        ``commitment`` defaults to ``processed`` (fastest, used on the swap hot
        path).  Post-trade VERIFICATION reads (post-buy reconcile, post-sell
        settle, group-boundary empty-check) should pass ``confirmed`` — a
        processed read can lag a just-confirmed swap and report a stale
        pre-trade balance, which caused phantom "SELL PARTIAL" resurrections.
        """
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                self.wallet_pubkey,
                {"mint": self.token_mint},
                {"encoding": "jsonParsed", "commitment": commitment},
            ],
        }

        def _extract(data: dict) -> Optional[int]:
            accounts = data.get("result", {}).get("value", [])
            if not accounts:
                return 0  # definitive empty — don't try other RPCs
            try:
                parsed = accounts[0]["account"]["data"]["parsed"]
                amount = int(parsed["info"]["tokenAmount"]["amount"])
                decimals = int(parsed["info"]["tokenAmount"]["decimals"])
                if decimals != self._token_decimals:
                    logger.info(f"[BAL] Token decimals detected: {decimals} (was {self._token_decimals})")
                    self._token_decimals = decimals
                return amount
            except Exception:
                return None

        result = await self._rpc_fanout_first_wins(payload, timeout_s=1.2, result_fn=_extract)
        return result if isinstance(result, int) else 0

    # ── Swap execution ────────────────────────────────────────────────────────

    async def execute_buy(self, reason: str = "signal") -> Optional[str]:
        """
        Single-attempt, fire-and-forget buy.

        Policy (per requirements):
          - Build and broadcast the swap ONCE, as fast as possible (cached SOL
            balance + cached blockhash keep the hot path free of pre-RPC reads).
          - The TX is broadcast immediately and the signature is returned
            without waiting for on-chain confirmation.
          - NO RETRY on failure.  If the broadcast is rejected — or the
            background confirmation task later finds the TX never landed — the
            trade is logged as failed and the engine/position state is returned
            to FLAT (``current_trade=None``, ``engine.notify_trade_closed()``)
            so the algorithm sees "position closed" and may re-enter on the
            next signal.

        Reliability comes from the fast-RPC fanout + fresh blockhash (high
        first-attempt landing rate) rather than from hammering retries.
        """
        if self._swap_in_flight:
            logger.warning("[BUY] Swap already in flight — skipping")
            return None
        self._swap_in_flight = True
        buy_start = time.time()
        try:
            amount_lam = int(self.buy_size_sol * 1e9)
            mint_str = str(self.token_mint)

            # ── Cached SOL balance (no RPC round-trip on the hot path) ──────
            # The background balance cache keeps this figure fresh (~4s).  If
            # the cache has never been populated (session just started) we do
            # one blocking read; otherwise the swap proceeds immediately.
            sol_bal = self._get_cached_sol_balance()
            if self._cached_balance_ts == 0.0:
                sol_bal = await self._get_sol_balance()
                self._cached_sol_balance = sol_bal
                self._cached_balance_ts = time.time()
            logger.info(
                f"[BUY] Starting buy: mint={mint_str[:8]}… size={self.buy_size_sol} SOL "
                f"balance={sol_bal:.4f} SOL (cached) reason={reason}"
            )
            self._journal_event(
                "buy_attempt", reason=reason, size_sol=self.buy_size_sol,
                amount_lamports=amount_lam, sol_balance=sol_bal,
            )
            if sol_bal * 1e9 < amount_lam + 50_000:  # buy size + gas buffer
                logger.error(f"[BUY FAILED] Insufficient balance: {sol_bal:.4f} SOL but need ~{self.buy_size_sol} SOL")
                self._journal_event(
                    "buy_rejected", reason=reason, error="insufficient_sol",
                    sol_balance=sol_bal, required_sol=self.buy_size_sol,
                )
                await self._fail_buy_flat(f"Insufficient SOL ({sol_bal:.4f} available)", reason)
                return None

            # ── Single quote → swap → broadcast (NO retry) ───────────────────
            fee = PRIORITY_FEE_ESCALATION[0]

            quote = await self._get_quote(WSOL_MINT, self.token_mint, amount_lam)
            if not quote:
                logger.error("[BUY FAILED] Jupiter quote failed")
                self._journal_event("buy_rejected", reason=reason, error="jupiter_quote_failed")
                await self._fail_buy_flat("Jupiter quote failed", reason)
                return None

            swap_tx = await self._get_swap_tx(quote, priority_fee_override=fee)
            if not swap_tx:
                logger.error("[BUY FAILED] Swap TX build failed")
                self._journal_event(
                    "buy_rejected", reason=reason, error="swap_tx_build_failed",
                    quote_out_amount=quote.get("outAmount"),
                )
                await self._fail_buy_flat("Swap TX build failed", reason)
                return None

            # ── Fire-and-forget broadcast ─────────────────────────────────────
            send_res = await self._sign_and_send(swap_tx, wait_for_confirmation=False)
            if not send_res:
                logger.error("[BUY FAILED] Broadcast rejected by all RPCs — not retrying")
                self._journal_event(
                    "buy_rejected", reason=reason, error="broadcast_rejected_all_rpcs",
                    quote_out_amount=quote.get("outAmount"),
                )
                await self._fail_buy_flat("Broadcast rejected by all RPCs", reason)
                return None
            sig = send_res["sig"]
            last_valid_height = send_res.get("last_valid_block_height")

            # ── PENDING until on-chain confirmation ───────────────────────────
            # DO NOT mark the trade open here — the TX may still fail or never
            # land.  The trade stays "pending" (blocking new entries) until
            # _background_buy_settle either confirms it on-chain (→ open) or
            # proves it dead (→ failed).  We DO seed a PROVISIONAL balance from
            # the Jupiter outAmount so a sell signal that fires before the
            # 4s background balance refresh can still build a swap — the sell
            # path re-reads the authoritative on-chain balance and corrects it.
            elapsed = time.time() - buy_start
            out_amount = int(quote.get("outAmount", 0))
            tokens = out_amount / (10 ** self._token_decimals)
            ct = self.current_trade
            if ct:
                ct.tx_hash_buy = sig
                ct.size_tokens = tokens   # provisional — corrected on confirm
                ct.status = "pending"
            self._token_balance = out_amount          # provisional seed
            self._cached_token_balance = out_amount   # keep cache warm pre-refresh
            self._last_exit_signal_ts = 0.0  # reset watchdog
            # Background task confirms the TX across the FULL blockhash-validity
            # window; only after the hash is cryptographically dead AND the
            # wallet provably holds no tokens is the buy declared failed.
            asyncio.ensure_future(
                self._background_buy_settle(sig, out_amount, last_valid_height)
            )
            await self._broadcast_status("buy_pending", sig, reason, tokens=tokens)
            logger.info(
                f"[BUY BROADCAST] sig={sig} tokens={tokens:.4f} "
                f"elapsed={elapsed:.2f}s (pending on-chain confirmation)"
            )
            logger.info(f"[BUY] https://solscan.io/tx/{sig}")
            self._journal_event(
                "buy_broadcast", reason=reason, status="pending",
                tx_hash=sig, solscan=f"https://solscan.io/tx/{sig}",
                size_sol=self.buy_size_sol, tokens_expected=tokens,
                out_amount_raw=out_amount, priority_fee_microlamports=fee,
                last_valid_block_height=last_valid_height,
                elapsed_s=round(elapsed, 3),
            )
            return sig

        except Exception as e:
            logger.error(f"[BUY ERROR] Unexpected error: {e}", exc_info=True)
            self._journal_event("buy_error", reason=reason, error=str(e))
            await self._fail_buy_flat(f"Unexpected: {e}", reason)
            return None
        finally:
            self._swap_in_flight = False

    async def _fail_buy_flat(self, detail: str, reason: str):
        """
        Return the algorithm to a FLAT / position-closed state after a failed
        buy (no retry).  Clears the open trade and notifies the engine that no
        position is held so it can generate a fresh entry on the next signal.

        CRITICAL SAFETY: before failing, do a final on-chain balance probe.
        If tokens ARE present the buy actually landed — adopt them into the
        open trade instead of declaring failure (an unmonitored bag is the
        exact bug this fixes).
        """
        if self.current_trade is not None:
            failed = self.current_trade
            # Defensive final probe: never fail a buy whose tokens are
            # actually sitting in the wallet (belt-and-braces — the settle
            # loop already probed, but RPC lag is real).
            try:
                on_chain = await self._get_token_balance()
            except Exception:
                on_chain = 0
            if on_chain > 0:
                logger.warning(
                    f"[BUY FAIL ABORTED] Wallet holds {on_chain} tokens despite "
                    f"'{detail}' — treating buy as CONFIRMED instead of failed."
                )
                self._token_balance = on_chain
                self._cached_token_balance = on_chain
                failed.status = "open"
                failed.size_tokens = on_chain / (10 ** self._token_decimals)
                self._journal_event(
                    "buy_confirmed", trade=failed, reason=reason,
                    via="fail_aborted_balance_probe", original_error=detail,
                    on_chain_balance=on_chain, tokens=failed.size_tokens,
                )
                await self._broadcast_status(
                    "buy_confirmed", failed.tx_hash_buy or "", reason,
                    tokens=failed.size_tokens,
                )
                return
            failed.status = "failed"
            failed.exit_reason = f"buy_failed: {detail}"
            failed.exit_time = time.time()
            self.trade_history.append(failed)
            self._journal_event(
                "buy_failed", trade=failed, reason=reason, error=detail,
                tx_hash=failed.tx_hash_buy or None,
                sol_spent=failed.size_sol, tokens=failed.size_tokens,
            )
            self.current_trade = None
        self._token_balance = 0
        self._cached_token_balance = 0
        self._pending_buy = False
        self._pending_buy_reason = ""
        self.engine.notify_trade_closed()
        await self._broadcast_status("buy_failed", detail, reason)

    async def _verify_buy_settled(self, sig: str, expected_amount: int):
        """Background post-buy balance reconcile.  The buy is CONFIRMED on-chain,
        so the tokens MUST exist — a ``0`` read is just processed-commitment /
        ATA-propagation lag, never a real empty wallet.  Poll briefly until we
        get a definitive non-zero read and adopt it, so ``_token_balance`` /
        ``_cached_token_balance`` reflect reality before any sell can fire.
        Never leaves the cache stale by silently returning on a lagged 0."""
        for attempt in range(8):  # ~0.4s initial + up to 7 × 0.5s retries
            await asyncio.sleep(0.4 if attempt == 0 else 0.5)
            try:
                settled = await self._get_token_balance()
            except Exception:
                settled = 0
            if settled > 0:
                if settled != expected_amount:
                    logger.info(
                        f"[BUY VERIFY] On-chain balance {settled} differs from Jupiter "
                        f"outAmount {expected_amount} — adopting on-chain figure."
                    )
                self._token_balance = settled
                self._cached_token_balance = settled
                ct = self.current_trade
                if ct and ct.tx_hash_buy == sig:
                    ct.size_tokens = settled / (10 ** self._token_decimals)
                return
            # settled == 0 → read lag; keep polling
        # All probes lagged.  Keep the provisional Jupiter outAmount (seeded in
        # execute_buy) rather than zeroing — the sell path re-reads on-chain and
        # corrects, and zeroing here is what caused "No token balance to sell".
        logger.warning(
            f"[BUY VERIFY] Balance still reading 0 after retries (sig={sig[:8]}…) — "
            f"keeping provisional {expected_amount}; sell will re-read on-chain."
        )

    async def _background_buy_settle(
        self,
        sig: str,
        expected_amount: int,
        last_valid_height: Optional[int] = None,
    ):
        """
        Background confirmation + settle for a fire-and-forget buy.

        THE BUY-FINALITY INVARIANT lives here.  A buy is resolved in exactly
        one of two ways:

          CONFIRMED — the TX reached confirmed/finalized status, OR the wallet
                      provably holds the tokens (balance probe).  The trade is
                      promoted to ``open`` and monitored normally.

          FAILED    — ONLY once the TX is *cryptographically dead* (its
                      blockhash's ``lastValidBlockHeight`` has been passed by
                      the chain, ~150 slots ≈ 60 s) AND the wallet provably
                      holds zero tokens.  Until BOTH hold, the trade stays
                      ``pending`` and no new entry is possible.

        This removes the old 12 s timeout failure boundary, which declared a
        buy "failed" while the TX was still landable — producing unmonitored
        on-chain positions the algorithm believed were closed.
        """
        start = time.time()
        deadline = start + BUY_CONFIRM_TIMEOUT_S
        last_probe = 0.0
        last_height_check = 0.0

        async def _confirm_open(balance: int, via: str):
            """Promote the pending trade to open and reconcile the balance."""
            self._token_balance = balance
            self._cached_token_balance = balance
            ct = self.current_trade
            if ct is not None and ct.tx_hash_buy == sig:
                ct.status = "open"
                ct.size_tokens = balance / (10 ** self._token_decimals)
            await self._refresh_sol_balance_async()
            await self._broadcast_status(
                "buy_confirmed", sig, "signal",
                tokens=(self.current_trade.size_tokens if self.current_trade else 0),
            )
            logger.info(f"[BUY CONFIRMED BG] sig={sig[:16]}… via={via} balance={balance}")
            self._journal_event(
                "buy_confirmed", trade=self.current_trade, via=via,
                tx_hash=sig, on_chain_balance=balance,
                tokens=(balance / (10 ** self._token_decimals)),
                elapsed_s=round(time.time() - start, 3),
                solscan=f"https://solscan.io/tx/{sig}",
            )

        # Single tight poll loop — one status probe per iteration (NOT a nested
        # 12 s _confirm_tx per outer loop, which made deadness/balance checks
        # run only once per 12 s and duplicated the _background_confirm waiter).
        while time.time() < deadline:
            # ── 1. Single signature-status probe ─────────────────────────────
            status = await self._get_signature_status(sig)
            if status is not None:
                if status.get("err"):
                    # Definitive on-chain rejection — TX can never land.  Probe
                    # balance once (a prior rebroadcast may have landed) then fail.
                    logger.warning(f"[BUY BG] sig={sig[:16]}… on-chain error: {status['err']}")
                    self._journal_event(
                        "buy_onchain_error", tx_hash=sig,
                        error=str(status["err"]),
                        elapsed_s=round(time.time() - start, 3),
                    )
                    break
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    # Confirmed — reconcile the real balance and open.
                    await self._verify_buy_settled(sig, expected_amount)
                    bal = self._token_balance or expected_amount
                    await _confirm_open(bal, "signature_status")
                    return

            # ── 2. Wallet-balance probe (catches landed-but-unseen TXs) ───────
            if time.time() - last_probe >= BUY_BALANCE_PROBE_EVERY_S:
                last_probe = time.time()
                try:
                    on_chain = await self._get_token_balance()
                except Exception:
                    on_chain = 0
                if on_chain > 0:
                    await _confirm_open(on_chain, "balance_probe")
                    return

            # ── 3. Cryptographic deadness check (blockhash expiry) ────────────
            if last_valid_height is not None and (time.time() - last_height_check) >= 2.0:
                last_height_check = time.time()
                height = await self._get_block_height()
                if height is not None and height > last_valid_height:
                    # TX can NEVER land now.  Grace window for processed-commitment
                    # lag, then the balance probe decides confirmed-vs-failed.
                    logger.info(
                        f"[BUY BG] sig={sig[:16]}… blockhash expired "
                        f"(height {height} > last_valid {last_valid_height}) — final settle grace"
                    )
                    await asyncio.sleep(BUY_SETTLE_GRACE_S)
                    try:
                        final_bal = await self._get_token_balance()
                    except Exception:
                        final_bal = 0
                    if final_bal > 0:
                        await _confirm_open(final_bal, "post_expiry_balance")
                        return
                    logger.error(
                        f"[BUY BG FAILED] sig={sig[:16]}… blockhash expired and "
                        f"wallet empty — buy is cryptographically dead"
                    )
                    self._journal_event(
                        "buy_dead", tx_hash=sig, cause="blockhash_expired_wallet_empty",
                        elapsed_s=round(time.time() - start, 3),
                    )
                    ct = self.current_trade
                    if ct is not None and ct.tx_hash_buy == sig:
                        await self._fail_buy_flat(
                            "Buy TX expired without landing (blockhash dead)", "signal"
                        )
                    return

            await asyncio.sleep(BUY_SETTLE_POLL_S)

        # ── Absolute deadline hit (or definitive on-chain error) ──────────────
        # Final balance probe is the tie-breaker: tokens present ⇒ confirmed.
        try:
            final_bal = await self._get_token_balance()
        except Exception:
            final_bal = 0
        ct = self.current_trade
        if final_bal > 0:
            await _confirm_open(final_bal, "deadline_balance")
            return

        logger.error(
            f"[BUY BG FAILED] sig={sig[:16]}… never confirmed within "
            f"{BUY_CONFIRM_TIMEOUT_S:.0f}s and wallet empty — returning to FLAT (no retry)"
        )
        self._journal_event(
            "buy_dead", tx_hash=sig, cause="confirm_deadline_wallet_empty",
            elapsed_s=round(time.time() - start, 3),
        )
        if ct is not None and ct.tx_hash_buy == sig:
            await self._fail_buy_flat(
                "Buy TX failed on-chain (never landed, wallet empty)", "signal"
            )

    async def _refresh_sol_balance_async(self):
        """Non-blocking stats-balance refresh used after a confirmed buy."""
        bal = await self._get_sol_balance()
        if bal > 0:
            self.stats.starting_balance = bal

    async def _verify_sell_settled(self, sig: str, closed_trade):
        """
        Background post-sell verification. If on-chain tokens survived the
        sell (partial fill / program error) we reopen the trade's metadata
        and arm the watchdog to attempt another sell pass. Non-blocking on
        the hot path so the user sees the SELL OK message immediately.

        ``closed_trade`` is the LiveTrade returned by confirm_sell; if it's
        not None we may need to "un-close" it and re-arm the watchdog.

        CRITICAL: this read MUST use ``confirmed`` commitment and retry.  A
        ``processed`` read taken moments after the sell confirmed can lag and
        report the stale PRE-SELL balance — which produced phantom "SELL
        PARTIAL" resurrections that re-sold an already-empty wallet (6024
        loop).  We only resurrect on a STABLE non-zero confirmed balance.
        """
        post_balance = 0
        for attempt in range(5):
            await asyncio.sleep(0.8 if attempt == 0 else 0.7)
            try:
                post_balance = await self._get_token_balance(commitment="confirmed")
            except Exception:
                post_balance = 0
            if post_balance == 0:
                return  # genuinely empty — trade is closed, nothing to do
            # Non-zero: confirm it's stable (not a lagging pre-sell snapshot)
            # by reading once more before deciding to resurrect.
            await asyncio.sleep(0.5)
            try:
                confirm_read = await self._get_token_balance(commitment="confirmed")
            except Exception:
                confirm_read = 0
            if confirm_read == 0:
                return  # first read was lag — wallet is actually empty
            post_balance = confirm_read
            break  # stable non-zero across two confirmed reads → genuine partial
        if post_balance > 0:
            logger.warning(
                f"[SELL PARTIAL] {post_balance} tokens still on-chain after "
                f"confirmed sell sig={sig[:8]}… — re-arming watchdog to retry."
            )
            self._journal_event(
                "sell_partial", tx_hash=sig,
                remaining_token_balance=post_balance,
                closed_trade=(closed_trade.to_dict() if closed_trade else None),
            )
            self._token_balance = post_balance
            # If confirm_sell already nulled current_trade, resurrect it so
            # the watchdog's "position open" check passes.
            if self.current_trade is None and closed_trade is not None:
                from dataclasses import replace
                reopened = replace(
                    closed_trade,
                    status="open",
                    exit_time=None,
                    exit_price=None,
                    exit_reason="watchdog_retry",
                    size_tokens=post_balance / (10 ** self._token_decimals),
                    pnl_sol=0.0,
                    pnl_pct=0.0,
                    tx_hash_sell="",
                )
                self.current_trade = reopened
                # Pop the prematurely-closed trade entry so we don't double-
                # count PnL when the retry actually sells.
                if self.trade_history and self.trade_history[-1] is closed_trade:
                    self.trade_history.pop()
                self.stats.total_trades -= 1
                if closed_trade.pnl_sol > 0:
                    self.stats.winning_trades -= 1
                else:
                    self.stats.losing_trades -= 1
                self.stats.total_pnl_sol -= closed_trade.pnl_sol
                self.stats.current_balance -= closed_trade.pnl_sol
                self._last_exit_signal_ts = time.time()  # arm watchdog
                self.engine.notify_trade_opened(self._last_price, Direction.UP)
                self._journal_event(
                    "trade_resurrected", trade=self.current_trade,
                    tx_hash_sell_reverted=sig,
                    reason="partial_fill_resurrection",
                )
        # Success path: balance is 0 — trade is genuinely closed. Nothing to do.

    async def execute_sell(self, reason: str = "signal") -> Optional[str]:
        """
        Full sell cycle with grouped-retry escalation.

        THIS IS THE CRITICAL PATH.  A confirmed sell signal that never settles
        lets a position ride to zero — this rewrite makes that impossible:

          1.  Unlimited grouped retries — NO 5-attempt bail.
          2.  Authoritative on-chain balance fetch at the start of EVERY group.
              Selling more tokens than the wallet actually holds (usually from
              cached buy-quote outAmount inflated by slippage / partial fill)
              is the root cause of `Custom: 6024` on Pump.fun.
          3.  Priority-fee escalation per group.
          4.  Slippage escalation ladder: 1x → 1.5x → 2x → 2.5x of the
              configured `slippage_bps` (capped at 9000 bps) — panicked markets
              need wider slippage bands rather than a failed TX.
          5.  After success, the sold amount used for PnL is the actual amount
              requested (verified by re-querying the balance).
        """
        if self._swap_in_flight:
            logger.warning("[SELL] Swap already in flight — skipping")
            return None
        self._swap_in_flight = True
        sell_start = time.time()
        attempt_group = 0
        original_slippage = self.slippage_bps  # restore after call
        try:
            mint_str = str(self.token_mint)
            # Watchdog stamp — regardless of success/failure, the monitor task
            # knows we SAW an exit signal at this moment. Stamped BEFORE the
            # balance fetch so any latency in the read doesn't push us
            # past the watchdog timeout without a clear "we tried" mark.
            self._last_exit_signal_ts = time.time()

            # ── If a buy is still pending, wait for it to settle first ────────
            # Selling into a pending buy races the landing TX.  Wait (bounded)
            # for the settle loop to resolve it; then read the real balance.
            if self._is_buy_pending():
                logger.info("[SELL] Buy still pending — waiting for it to settle before selling")
                wait_deadline = time.time() + 30.0
                while self._is_buy_pending() and time.time() < wait_deadline:
                    await asyncio.sleep(0.25)

            logger.info(
                f"[SELL] Starting sell: mint={mint_str[:8]}… reason={reason} "
                f"cached_balance={self._token_balance} units"
            )
            self._journal_event(
                "sell_attempt", reason=reason,
                cached_token_balance=self._token_balance,
            )
            # ── Authoritative on-chain token balance (accuracy > speed) ──────
            # `Custom: 6024` (insufficient token account balance) is the #1
            # cause of failed sells, and it happens when we try to sell MORE
            # tokens than the wallet actually holds — typically because the
            # cached figure came from the buy's Jupiter outAmount (inflated by
            # slippage / a partial fill).  We therefore ALWAYS read the live
            # on-chain balance before building the swap.  This is the single
            # most important step for "correct amount every time".  The read
            # uses the fast-RPC fanout (~0.2–0.5s), far cheaper than a failed
            # 6024 round-trip (~1s + a wasted signature).
            #
            # A 0 read is usually transient (processed-commitment / ATA
            # propagation lag right after a buy) — NEVER bail on it.  Retry the
            # read briefly, then fall back to the last-known cached figure and
            # let the grouped-retry loop + watchdog keep selling until the
            # wallet is actually empty.
            fresh_bal = 0
            for probe in range(4):
                try:
                    fresh_bal = await self._get_token_balance()
                except Exception:
                    fresh_bal = 0
                if fresh_bal > 0:
                    break
                if probe < 3:
                    await asyncio.sleep(0.5)
            if fresh_bal > 0:
                if fresh_bal != self._token_balance:
                    logger.info(
                        f"[SELL] Correcting stale cached balance: "
                        f"{self._token_balance} → {fresh_bal}"
                    )
                token_balance = fresh_bal
                self._token_balance = fresh_bal
                self._cached_token_balance = fresh_bal
            else:
                # Live read stayed 0 — transient RPC/ATA lag or genuinely empty.
                # Fall back to the last-known cached balance so we still attempt
                # the sell; the 6024 retry loop + watchdog correct it.
                token_balance = self._token_balance

            if token_balance <= 0:
                # No on-chain balance AND no cached balance.  Do NOT give up —
                # re-arm the watchdog so it keeps probing until tokens appear
                # (delayed buy landing) or the session is torn down.  This
                # removes the old "No token balance" dead-end that orphaned
                # positions.
                logger.warning(
                    f"[SELL] No token balance readable yet for {mint_str[:8]}… — "
                    f"arming watchdog to retry until tokens appear"
                )
                self._last_exit_signal_ts = time.time()
                self._journal_event(
                    "sell_pending", reason=reason,
                    detail="no_token_balance_readable_watchdog_armed",
                )
                await self._broadcast_status(
                    "sell_pending", "Waiting for token balance to appear", reason
                )
                return None

            logger.info(
                f"[SELL] Authoritative balance: {token_balance} units (on-chain={fresh_bal > 0})"
            )

            while True:  # grouped-retry — never give up while wallet still holds tokens
                attempt_group += 1
                fee = PRIORITY_FEE_ESCALATION[
                    min(attempt_group - 1, len(PRIORITY_FEE_ESCALATION) - 1)
                ]
                logger.info(
                    f"[SELL] Attempt group {attempt_group} "
                    f"(fee={fee} micro-lamports, slippage={self.slippage_bps} bps)"
                )
                self._journal_event(
                    "sell_attempt_group", reason=reason, group=attempt_group,
                    priority_fee_microlamports=fee, slippage_bps=self.slippage_bps,
                    token_balance=token_balance,
                )

                for inner in range(1, QUOTE_RETRIES_PER_GROUP + 1):
                    label = f"G{attempt_group}/Q{inner}"

                    # Authoritative on-chain balance re-fetch at the START of
                    # every group EXCEPT the first (we already have a fresh
                    # figure from the entry fetch above). On retries we MUST
                    # refresh because a partial fill may have changed things.
                    if attempt_group > 1:
                        live_bal = await self._get_token_balance()
                    else:
                        live_bal = token_balance
                    if live_bal > 0:
                        if live_bal != token_balance:
                            logger.info(
                                f"[SELL] Balance refreshed on {label}: "
                                f"{token_balance} → {live_bal}"
                            )
                        token_balance = live_bal
                        self._token_balance = token_balance
                    elif attempt_group > 1 and live_bal == 0 and token_balance > 0:
                        # Transient RPC gap — stick with the last-known balance.
                        pass

                    quote = await self._get_quote(self.token_mint, WSOL_MINT, token_balance)
                    if not quote:
                        logger.error(f"[SELL FAILED {label}] Jupiter quote failed — retrying immediately")
                        await asyncio.sleep(0.05)
                        continue

                    swap_tx = await self._get_swap_tx(quote, priority_fee_override=fee)
                    if not swap_tx:
                        logger.error(f"[SELL FAILED {label}] Swap TX build failed — retrying immediately")
                        await asyncio.sleep(0.05)
                        continue

                    # ── Confirm-first broadcast (single unified path) ────────
                    # CRITICAL INVARIANT: the trade is closed EXACTLY ONCE, and
                    # only AFTER the sell TX is confirmed on-chain.  We never
                    # call confirm_sell() on broadcast — doing so produced one
                    # phantom "closed trade" per failed attempt.  If the TX
                    # fails, the position stays open and the loop retries
                    # immediately with a fresh quote + fresh balance.
                    send_res = await self._sign_and_send(swap_tx, wait_for_confirmation=True)
                    sig = send_res["sig"] if send_res else None
                    if not sig:
                        logger.warning(f"[SELL FAILED {label}] TX not confirmed — retrying immediately with fresh quote")
                        await asyncio.sleep(0.05)
                        continue

                    # ── Success: TX confirmed on-chain — close ONCE ──────────
                    elapsed = time.time() - sell_start
                    sol_received = int(quote.get("outAmount", 0)) / 1e9

                    self._token_balance = 0
                    self._cached_token_balance = 0
                    self._last_exit_signal_ts = 0.0  # clear watchdog on the happy path
                    closed_trade = None
                    if self.current_trade:
                        closed_trade = self.confirm_sell(sig, sol_received, self._last_price)

                    # Post-confirm balance verification in the background: if
                    # tokens survived (partial fill / program error) the
                    # watchdog schedules another sell pass.
                    asyncio.ensure_future(
                        self._verify_sell_settled(sig, closed_trade)
                    )

                    await self._broadcast_status(
                        "sell_confirmed", sig, reason,
                        sol_received=sol_received, closed_trade=closed_trade,
                    )
                    logger.info(
                        f"[SELL OK] sig={sig} received={sol_received:.6f} SOL "
                        f"group={attempt_group} elapsed={elapsed:.1f}s"
                    )
                    logger.info(f"[SELL OK] https://solscan.io/tx/{sig}")
                    self._journal_event(
                        "sell_confirmed", trade=closed_trade, reason=reason,
                        tx_hash=sig, solscan=f"https://solscan.io/tx/{sig}",
                        sol_received=sol_received, attempt_group=attempt_group,
                        elapsed_s=round(elapsed, 3),
                        pnl_sol=(closed_trade.pnl_sol if closed_trade else None),
                        pnl_pct=(closed_trade.pnl_pct if closed_trade else None),
                    )
                    return sig

                # ── Group exhausted ──────────────────────────────────────────
                # Use confirmed commitment + retry: a lagging processed read
                # here can report the stale pre-sell balance (or vice-versa).
                bal = 0
                for _chk in range(3):
                    try:
                        bal = await self._get_token_balance(commitment="confirmed")
                    except Exception:
                        bal = 0
                    if bal > 0:
                        break
                    if _chk < 2:
                        await asyncio.sleep(0.5)
                if bal == 0:
                    # Wallet empty — the sell DID go through on-chain (otherwise
                    # we'd still hold tokens). Estimate SOL received from the
                    # last known token price rather than using 0.0 (which would
                    # produce a bogus -100% PnL).
                    est_sol_received = 0.0
                    if self.current_trade:
                        est_price = self._last_price or self.current_trade.entry_price
                        if est_price > 0 and self.current_trade.size_tokens > 0:
                            est_sol_received = self.current_trade.size_tokens * est_price
                    logger.info(
                        "[SELL VERIFIED] Wallet is now empty — exiting sell loop cleanly."
                    )
                    self._token_balance = 0
                    self._last_exit_signal_ts = 0.0
                    closed_trade = None
                    if self.current_trade:
                        closed_trade = self.confirm_sell("", est_sol_received, self._last_price)
                    self._journal_event(
                        "sell_confirmed", trade=closed_trade, reason=reason,
                        tx_hash=None, via="verified_empty_wallet",
                        sol_received=est_sol_received, estimated=True,
                        attempt_group=attempt_group,
                        pnl_sol=(closed_trade.pnl_sol if closed_trade else None),
                        pnl_pct=(closed_trade.pnl_pct if closed_trade else None),
                    )
                    return "verified_empty"
                token_balance = bal

                # Slippage escalation ladder.
                if attempt_group >= 2:
                    new_slip = min(
                        int(original_slippage * (1.5 ** (attempt_group - 1))),
                        9000,
                    )
                    if new_slip != self.slippage_bps:
                        logger.warning(
                            f"[SELL SLIPPAGE ↑] {self.slippage_bps} → {new_slip} bps"
                        )
                        self.slippage_bps = new_slip

                # Retry immediately — only a tiny yield so we don't spin the
                # event loop while still re-quoting right away.
                logger.info(
                    f"[SELL] Group {attempt_group} exhausted; retrying immediately "
                    f"(still holding {bal} raw tokens)"
                )
                await asyncio.sleep(0.05)

        except Exception as e:
            logger.error(f"[SELL ERROR] Unexpected error: {e}", exc_info=True)
            self._journal_event("sell_error", reason=reason, error=str(e))
            await self._broadcast_status("sell_failed", f"Unexpected: {e}", reason)
            return None
        finally:
            self.slippage_bps = original_slippage
            self._swap_in_flight = False

    async def _broadcast_status(self, event: str, detail: str, reason: str = "",
                                 tokens: float = 0, sol_received: float = 0, closed_trade=None):
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
                "closed_trade": closed_trade.to_dict() if closed_trade else None,
                "stats": self.stats.to_dict(),
            }
            try:
                await self.broadcast_fn(json.dumps(msg))
            except Exception:
                pass

    # ── Emergency sell watchdog ───────────────────────────────────────────────

    async def _monitor_trade(self):
        """
        Background watchdog — guarantees "if we stop attempting, this coin is
        doomed to go to 0" can never happen silently.

        Every WATCHDOG_INTERVAL_S it checks:
          • Do we have a current_trade AND on-chain balance > 0?
          • Has it been > WATCHDOG_TIMEOUT_S since the last sell signal fired?
          • Is no swap currently in flight?

        If all three are true it force-fires execute_sell("watchdog_retry"),
        regardless of why the prior trade action was skipped or failed.
        """
        logger.info(
            f"[WATCHDOG] Armed — interval={WATCHDOG_INTERVAL_S}s "
            f"timeout={WATCHDOG_TIMEOUT_S}s  mint={self.token_mint[:8]}…"
        )
        while self._alive:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL_S)

                # ── Orphaned-bag adoption (backstop) ──────────────────────────
                # If the wallet holds tokens but NO trade is tracked and no buy
                # is pending, a position exists on-chain that the algorithm
                # cannot see.  Adopt it into a monitored trade so it can be
                # sold — never leave an invisible bag.  (With the buy-finality
                # invariant this should be unreachable, but belt-and-braces.)
                if (self.current_trade is None
                        and not self._swap_in_flight
                        and not self._is_buy_pending()
                        and not self._adopted_bag):
                    try:
                        orphan = await self._get_token_balance(commitment="confirmed")
                    except Exception:
                        orphan = 0
                    if orphan > 0:
                        logger.warning(
                            f"[WATCHDOG] ⚠  Wallet holds {orphan} tokens with no "
                            f"tracked trade — adopting orphaned bag into a monitored "
                            f"position so it can be sold."
                        )
                        self._adopted_bag = True
                        self._token_balance = orphan
                        self._cached_token_balance = orphan
                        price = self._last_price or 0.0
                        adopted = LiveTrade(
                            token_mint=self.token_mint,
                            entry_time=int(time.time()),
                            entry_price=price,
                            size_sol=0.0,
                            size_tokens=orphan / (10 ** self._token_decimals),
                            entry_reason="watchdog_adopted_orphan",
                            status="open",
                        )
                        self.current_trade = adopted
                        self.engine.notify_trade_opened(price, Direction.UP)
                        self._last_exit_signal_ts = time.time()  # arm sell path
                        self._journal_event(
                            "bag_adopted", trade=adopted,
                            on_chain_balance=orphan, price=price,
                            reason="orphaned_bag_watchdog_adoption",
                        )
                        asyncio.ensure_future(self.execute_sell("watchdog_adopted_orphan"))
                        continue

                if self.current_trade is None or self._swap_in_flight:
                    continue

                # A pending buy is mid-flight — never interfere with it.
                if self._is_buy_pending():
                    continue

                if self._last_exit_signal_ts <= 0:
                    continue  # no exit signal seen yet — nothing to do

                time_since_exit = time.time() - self._last_exit_signal_ts
                if time_since_exit < WATCHDOG_TIMEOUT_S:
                    continue  # normal sell retry may still be running

                try:
                    on_chain = await self._get_token_balance(commitment="confirmed")
                except Exception:
                    on_chain = 0
                if on_chain > 0:
                    logger.warning(
                        f"[WATCHDOG] ⚠  {time_since_exit:.0f}s elapsed since sell "
                        f"signal, {on_chain} tokens still on-chain — "
                        f"forcing sell retry."
                    )
                    self.current_trade.status = "closing"
                    self.current_trade.exit_reason = "watchdog_retry"
                    asyncio.ensure_future(self.execute_sell("watchdog_retry"))
                else:
                    # Swap already completed on-chain but local state was stale.
                    # Estimate SOL received from the last known price rather than
                    # using 0.0 (which would produce a bogus -100% PnL).
                    est_sol_received = 0.0
                    if self.current_trade:
                        est_price = self._last_price or self.current_trade.entry_price
                        if est_price > 0 and self.current_trade.size_tokens > 0:
                            est_sol_received = self.current_trade.size_tokens * est_price
                    logger.info(
                        f"[WATCHDOG] On-chain balance = 0 but current_trade still "
                        f"open — finalising trade locally (est_sol={est_sol_received:.6f})"
                    )
                    self.confirm_sell("watchdog_finalise", est_sol_received, self._last_price)
                    self._last_exit_signal_ts = 0.0

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[WATCHDOG] Error: {e}", exc_info=True)
                await asyncio.sleep(2.0)
        logger.info("[WATCHDOG] Stopped")

    # ── Market-cap safety floor ───────────────────────────────────────────────

    async def update_market_cap(self, market_cap_usd: float) -> bool:
        """
        Called every tick with the latest USD market cap.

        Returns True only after the mcap floor breach has been FULLY handled:
          - If mcap ≥ floor  → nothing happens (returns False).
          - If mcap < floor and NO open position → block new entries by
            setting mcap_stop_triggered; broadcast warning; returns True
            immediately (nothing to sell).
          - If mcap < floor and position IS open → await the emergency sell
            to completion (execute_sell loops until the wallet is empty or
            the sale is confirmed on-chain), THEN set mcap_stop_triggered
            and return True.  The caller (main.py) can therefore cancel the
            session immediately on True without racing an in-flight swap.

        A generous timeout backstop (module-level
        MCAP_STOP_SELL_TIMEOUT_SECONDS) prevents a pathologically stuck RPC
        from hanging the session forever; on timeout we return True anyway
        so the session can shut down and the background watchdog
        (_monitor_trade) keeps trying to settle.
        """
        if market_cap_usd <= 0:
            return False  # No data yet — don't act on 0

        self._last_market_cap_usd = market_cap_usd

        if self.mcap_stop_triggered:
            return True  # Already handled

        if market_cap_usd >= self.min_market_cap_usd:
            return False  # All good

        # ── Threshold breached ────────────────────────────────────────────
        logger.warning(
            f"[MCAP STOP] Market cap ${market_cap_usd:,.0f} dropped below "
            f"floor ${self.min_market_cap_usd:,.0f} for {self.token_mint[:8]}…"
        )
        self.mcap_stop_triggered = True

        if self.current_trade is not None:
            if self._swap_in_flight:
                # A swap (buy or sell) is already in flight.  Poll until it
                # clears, then run the emergency sell if a position remains.
                # This avoids execute_sell's "swap already in flight" early
                # return, which would otherwise skip the emergency sell
                # entirely and terminate the session with tokens still held.
                logger.warning(
                    "[MCAP STOP] Swap already in flight — waiting for it to "
                    "clear before emergency sell"
                )
                wait_start = time.time()
                while self._swap_in_flight:
                    if time.time() - wait_start > MCAP_STOP_SELL_TIMEOUT_SECONDS:
                        logger.error(
                            f"[MCAP STOP] In-flight swap did not clear within "
                            f"{MCAP_STOP_SELL_TIMEOUT_SECONDS:.0f}s — "
                            f"terminating session; watchdog will continue "
                            f"attempting to settle"
                        )
                        break
                    await asyncio.sleep(0.1)

            if self.current_trade is not None and not self._swap_in_flight:
                logger.warning(
                    "[MCAP STOP] Position open — awaiting emergency sell "
                    "confirmation before session termination"
                )
                self.current_trade.status = "closing"
                self.current_trade.exit_reason = "mcap_floor_stop"
                try:
                    # Await the sell to completion.  execute_sell only returns
                    # once the swap is confirmed on-chain (sig), the wallet is
                    # verified empty ("verified_empty"), or an unrecoverable
                    # error occurred (None).  It never returns while a retry
                    # is still pending.
                    sig = await asyncio.wait_for(
                        self.execute_sell("mcap_floor_stop"),
                        timeout=MCAP_STOP_SELL_TIMEOUT_SECONDS,
                    )
                    if sig:
                        logger.info(
                            f"[MCAP STOP] Emergency sell settled (sig={sig}) — "
                            f"safe to terminate session"
                        )
                    else:
                        logger.error(
                            "[MCAP STOP] Emergency sell returned without a "
                            "signature — terminating session anyway; watchdog "
                            "will continue attempting to settle"
                        )
                except asyncio.TimeoutError:
                    logger.error(
                        f"[MCAP STOP] Emergency sell exceeded "
                        f"{MCAP_STOP_SELL_TIMEOUT_SECONDS:.0f}s timeout — "
                        f"terminating session; watchdog will continue attempting "
                        f"to settle"
                    )

        await self._broadcast_status(
            "mcap_stop",
            f"Market cap ${market_cap_usd:,.0f} below ${self.min_market_cap_usd:,.0f} floor — session stopped",
        )
        return True

    # ── Intra-candle 4-state expansion (matches ForwardTester exactly) ──────

    def _process_completed_candle(self, t: int, o: float, h: float,
                                   l: float, c: float, vol: float,
                                   buy_vol: float = 0.0,
                                   sell_vol: float = 0.0) -> dict:
        """
        Mirror ForwardTester.update() exactly — called once per completed candle.

        Step 1 (before any engine.update() call): apply any pending signal from
                the PREVIOUS candle by calling engine.notify_trade_opened/closed().
                This is what ForwardTester does when executing a pending signal
                at the *open* of the current candle before running the engine.

        Step 2: expand this candle into 4 intra-candle sub-states and call
                engine.update() on each — identical to the backtester loop.

        Step 3: detect any new signal from the 4-state expansion and store it
                as a pending signal for the NEXT candle (same as ForwardTester
                setting _pending_buy / _pending_exit).

        The LIVE SWAP fires immediately in step 1 (the actual asyncio task was
        already scheduled by update() when the signal was detected last candle).
        Only the engine's in_position state is deferred — this is what aligns
        the indicator evolution with the backtester.

        Returns the engine result from the final sub-state (state 4).
        """
        # ── Step 1: apply pending signal to engine BEFORE first engine.update() ──
        # This mirrors: ForwardTester._open_long / _close_long at candle open.
        # Only notify the engine once the buy is CONFIRMED open — while the TX
        # is still pending (broadcast but not on-chain) the engine must NOT
        # believe a position is held, or it would evolve state for a trade that
        # may never exist.
        if (self._pending_buy and self.current_trade is not None
                and self.current_trade.status == "open"):
            # Notify engine that a trade is now open at the open of this candle.
            # (The actual LiveTrade object was created and swap fired last candle.)
            self.engine.notify_trade_opened(o, Direction.UP)
            self._pending_buy = False
            self._pending_buy_reason = ""

        elif self._pending_exit and self.current_trade is None:
            # Trade was closed — notify engine.
            self.engine.notify_trade_closed()
            self._pending_exit = False
            self._pending_exit_reason = ""

        # Guard: clear stale pending flags
        if self._pending_buy and self.current_trade is None:
            self._pending_buy = False
        if self._pending_exit and self.current_trade is not None:
            self._pending_exit = False

        # ── Step 2: 4-state expansion ─────────────────────────────────────────
        bullish = c >= o
        if bullish:
            mid_first, mid_second = h, l
        else:
            mid_first, mid_second = l, h

        final_signal = None
        final_regime = None

        # State 1: open tick
        result = self.engine.update(t, o, o, o, o, 0.0)
        sig = result.get("signal", "none")
        if sig not in (Signal.NONE.value, "none"):
            final_signal = sig
            final_regime = result.get("regime")

        # State 2: first extreme
        h2 = max(o, mid_first)
        l2 = min(o, mid_first)
        result = self.engine.update(t, o, h2, l2, mid_first, 0.0)
        sig = result.get("signal", "none")
        if sig not in (Signal.NONE.value, "none") and final_signal is None:
            final_signal = sig
            final_regime = result.get("regime")

        # State 3: both extremes
        result = self.engine.update(t, o, h, l, mid_second, 0.0)
        sig = result.get("signal", "none")
        if sig not in (Signal.NONE.value, "none") and final_signal is None:
            final_signal = sig
            final_regime = result.get("regime")

        # State 4: close tick — buy/sell split lands here
        result = self.engine.update(t, o, h, l, c, vol,
                                    buy_volume=buy_vol, sell_volume=sell_vol)
        sig = result.get("signal", "none")
        if sig not in (Signal.NONE.value, "none") and final_signal is None:
            final_signal = sig
            final_regime = result.get("regime")

        # Propagate earliest signal into the final result dict
        if final_signal is not None:
            result["signal"] = final_signal
            if final_regime is not None:
                result["regime"] = final_regime

        # ── Step 3: queue signal for next candle (pending model) ──────────────
        # (The backtester queues then executes at the next candle's open sub-state.
        #  We queue here; the live swap is launched immediately below in update().)
        detected_signal = result.get("signal", "none")
        detected_regime = result.get("regime", "")

        if detected_signal == Signal.BUY.value and self.current_trade is None and not self._pending_buy:
            self._pending_buy = True
            self._pending_buy_reason = f"buy_{detected_regime}"
            self._pending_exit = False

        elif detected_signal == Signal.EXIT.value and self.current_trade is not None:
            reason = "exit_signal"
            if detected_regime == Regime.REVERSAL.value:
                reason = "reversal_exit"
            elif detected_regime == Regime.EXHAUSTION.value:
                reason = "exhaustion_exit"
            elif detected_regime == Regime.CONTINUATION.value:
                reason = "continuation_exit"
            elif detected_regime == Regime.TREND.value:
                reason = "trend_exit"
            self._pending_exit = True
            self._pending_exit_reason = reason
            self._pending_buy = False

        # ── No-motion stop ───────────────────────────────────────────────
        if self.no_motion_stop_seconds > 0:
            if self._last_motion_price == 0.0 or c != self._last_motion_price:
                self._last_motion_price = c
                self._last_motion_ts = time.time()

            # Only terminate the session when idle — never force-exit a
            # live position.  main.py sees the flag and shuts down.
            if (self.current_trade is None
                    and not self._pending_buy
                    and not self._pending_exit
                    and not self._swap_in_flight
                    and (time.time() - self._last_motion_ts) > self.no_motion_stop_seconds):
                self.no_motion_stop_triggered = True

        return result

    # ── Strategy update loop ──────────────────────────────────────────────────

    def update_historical_candle(
        self,
        time_val: int,
        o: float, h: float, l: float, c: float,
        volume: float = 0.0,
    ) -> dict:
        """
        Warm up the engine with a historical candle using the same 4-state
        expansion + pending model as the backtester.  No real swaps are executed.
        Returns the strategy result dict for the candle.
        """
        self._last_price = c
        result = self._process_completed_candle(time_val, o, h, l, c, volume)
        self._last_engine_result = result
        
        # Clear pending signals during historical warmup to prevent stale
        # signals from triggering an immediate buy when live trades commence.
        self._pending_buy = False
        self._pending_buy_reason = ""
        self._pending_exit = False
        self._pending_exit_reason = ""

        unrealized_pnl = 0.0
        unrealized_pnl_pct = 0.0
        if self.current_trade and self.current_trade.entry_price > 0:
            unrealized_pnl_pct = (c - self.current_trade.entry_price) / self.current_trade.entry_price * 100
            unrealized_pnl = self.current_trade.size_sol * (unrealized_pnl_pct / 100)

        return {
            **result,
            "live_trade": {
                "balance": round(self.stats.current_balance, 6),
                "trade_action": None,
                "trade_label": "",
                "opened_trade": None,
                "current_trade": self.current_trade.to_dict() if self.current_trade else None,
                "unrealized_pnl": round(unrealized_pnl, 6),
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                "stats": self.stats.to_dict(),
                "swap_request": None,
            },
        }

    def update(
        self,
        time_val: int,
        o: float, h: float, l: float, c: float,
        volume: float = 0.0,
        buy_volume: float = 0.0,
        sell_volume: float = 0.0,
        is_new: bool = False,
    ) -> dict:
        """
        Process one live tick through the candle-buffering + pending-signal pipeline.

        Indicator evolution is IDENTICAL to the backtester:
          - Completed candles are expanded into 4 sub-states.
          - engine.notify_trade_opened/closed() is applied at the START of the
            next candle's expansion (sub-state 1), not immediately on signal
            detection — matching ForwardTester.update() Step 1 exactly.

        Live swaps fire IMMEDIATELY (no N+1 bar wait):
          - When a BUY/EXIT signal is detected, the asyncio swap task is fired
            at once.  Only the engine's in_position flag is deferred to keep
            indicator math in sync with the backtester.
        """
        self._last_price = c
        trade_action = None
        opened_trade = None
        swap_request = None

        # ── Candle boundary: process the just-completed candle ────────────────
        if is_new and self._current_accumulating is not None:
            prev = self._current_accumulating

            # _process_completed_candle mirrors ForwardTester.update() fully:
            #   Step 1 — apply pending signal to engine (notify at open)
            #   Step 2 — run 4 sub-states through engine.update()
            #   Step 3 — queue newly detected signal as pending
            result = self._process_completed_candle(
                prev["t"], prev["o"], prev["h"], prev["l"], prev["c"], prev["vol"],
                buy_vol=prev.get("buy_vol", 0.0),
                sell_vol=prev.get("sell_vol", 0.0),
            )
            self._last_engine_result = result

            # ── Act on the signal detected in the 4-state expansion ───────────
            # The pending flags were just SET by _process_completed_candle.
            # We launch the live swap immediately (no N+1 bar wait), but the
            # engine.notify_trade_opened/closed() will be called at the START
            # of the NEXT candle's _process_completed_candle call.

            if (self._pending_buy and self.current_trade is None
                    and not self._swap_in_flight and not self._is_buy_pending()
                    and not self.mcap_stop_triggered and not self.no_motion_stop_triggered):
                buy_reason = self._pending_buy_reason
                trade = LiveTrade(
                    token_mint=self.token_mint,
                    entry_time=prev["t"],
                    entry_price=prev["c"],
                    size_sol=self.buy_size_sol,
                    size_tokens=0,
                    entry_reason=buy_reason,
                    status="pending",
                )
                self.current_trade = trade
                opened_trade = trade
                trade_action = "buy"
                self._last_motion_ts = time.time()  # reset no-motion clock at position open
                # DO NOT call engine.notify_trade_opened() here.
                # It will be called at sub-state 1 of the NEXT candle in
                # _process_completed_candle — matching the backtester exactly.

                asyncio.ensure_future(self.execute_buy(buy_reason))

                swap_request = {
                    "action": "buy",
                    "token": self.token_mint,
                    "amount_sol": self.buy_size_sol,
                    "reason": buy_reason,
                    "price": prev["c"],
                }

            elif self._pending_exit and self.current_trade is not None and not self._swap_in_flight:
                exit_reason = self._pending_exit_reason
                self.current_trade.status = "closing"
                self.current_trade.exit_reason = exit_reason
                trade_action = "exit"
                # DO NOT call engine.notify_trade_closed() here.
                # It will be called at sub-state 1 of the NEXT candle.

                asyncio.ensure_future(self.execute_sell(exit_reason))

                swap_request = {
                    "action": "sell",
                    "token": self.token_mint,
                    "reason": exit_reason,
                    "price": prev["c"],
                }

        # ── Always buffer the current tick ────────────────────────────────────
        self._current_accumulating = {
            "t": time_val, "o": o, "h": h, "l": l, "c": c, "vol": volume,
            "buy_vol": buy_volume, "sell_vol": sell_volume,
        }

        # ── Build output ──────────────────────────────────────────────────────
        result = self._last_engine_result or {}

        unrealized_pnl = 0.0
        unrealized_pnl_pct = 0.0
        if self.current_trade and self.current_trade.entry_price > 0:
            unrealized_pnl_pct = (c - self.current_trade.entry_price) / self.current_trade.entry_price * 100
            unrealized_pnl = self.current_trade.size_sol * (unrealized_pnl_pct / 100)

        if trade_action:
            self.signals_log.append({"time": time_val, "action": trade_action, "price": c, "regime": result.get("regime", "")})

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
        if self.current_trade is not None or self._is_buy_pending():
            logger.warning("[MANUAL BUY] Already in a trade (or buy pending) — ignoring")
            return None
        c = self._last_price or 1.0
        logger.info(f"[MANUAL BUY] Initiating manual buy at price={c}")
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
        self._last_motion_ts = time.time()  # reset no-motion clock at position open
        self._journal_event("manual_buy_triggered", trade=trade, price=c)
        # Do NOT notify the engine yet — the trade is only "pending" until the
        # buy confirms on-chain.  The engine is notified on confirmed-open via
        # the normal pending-signal path (or on settle).
        sig = await self.execute_buy("manual_test_buy")
        if sig is None:
            # Buy failed — clean up so future trades aren't blocked.
            # Engine was never notified of an open, so no notify_trade_closed.
            logger.warning("[MANUAL BUY] Buy failed — resetting trade state")
            self._journal_event("manual_buy_failed", trade=trade)
            self.current_trade = None
        return sig

    async def force_sell(self) -> Optional[str]:
        """Manually trigger a test sell from the dashboard."""
        if self.current_trade is None:
            logger.warning("[MANUAL SELL] No position open — ignoring")
            return None
        logger.info("[MANUAL SELL] Initiating manual sell")
        self.current_trade.status = "closing"
        self.current_trade.exit_reason = "manual_test_sell"
        self._journal_event("manual_sell_triggered", trade=self.current_trade)
        sig = await self.execute_sell("manual_test_sell")
        if sig is None and self.current_trade is not None:
            # Sell failed — revert status so position isn't stuck
            logger.warning("[MANUAL SELL] Sell failed — reverting trade status to open")
            self._journal_event("manual_sell_failed", trade=self.current_trade)
            self.current_trade.status = "open"
        return sig

    # ── Trade confirmation (called internally after TX confirmed) ─────────────

    def confirm_sell(self, tx_hash: str, sol_received: float, actual_price: float):
        if self.current_trade is None:
            return
        trade = self.current_trade
        trade.tx_hash_sell = tx_hash
        trade.exit_price = actual_price
        trade.exit_time = time.time()
        # Adopted-orphan / zero-cost-basis trades carry size_sol=0.0 — guard
        # the division and treat the entire proceeds as the PnL (there is no
        # known entry cost to subtract).
        if trade.size_sol > 0:
            trade.pnl_sol = sol_received - trade.size_sol
            if trade.entry_price > 0:
                trade.pnl_pct = (trade.pnl_sol / trade.size_sol) * 100
        else:
            trade.pnl_sol = sol_received
            trade.pnl_pct = 0.0
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
        self._journal_event(
            "trade_closed", trade=trade,
            tx_hash_buy=trade.tx_hash_buy or None,
            tx_hash_sell=trade.tx_hash_sell or None,
            sol_received=sol_received, exit_price=actual_price,
            pnl_sol=trade.pnl_sol, pnl_pct=trade.pnl_pct,
            hold_time_s=(trade.exit_time - trade.entry_time) if trade.exit_time else None,
            stats=self.stats.to_dict(),
        )
        return trade

    def confirm_failed(self, action: str, error: str):
        logger.error(f"Swap FAILED ({action}): {error}")
        self._journal_event(
            "swap_failed", action=action, error=error,
            trade=(self.current_trade.to_dict() if self.current_trade else None),
        )
        if action == "buy" and self.current_trade is not None:
            self.current_trade = None
            self.engine.notify_trade_closed()
        elif action == "sell" and self.current_trade is not None:
            self.current_trade.status = "open"
