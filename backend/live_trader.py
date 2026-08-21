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
import re
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
CONFIRM_POLL_MS:    float = 0.25   # poll cadence while waiting for confirmation
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

# ── BUY-FAILURE RE-ENTRY BLOCK ────────────────────────────────────────────────
# After a buy fails (broadcast rejected, TX dead, never landed) NO further
# automatic buy may fire for this many seconds.  The engine is notified FLAT
# on failure and would otherwise re-emit a BUY on the very next candle with
# the same broken conditions — an implicit retry the user explicitly forbids.
# Signals arriving inside the block window are dropped (logged once); a
# genuinely new setup after the window can enter normally.
BUY_FAIL_REENTRY_BLOCK_S: float = 120.0

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


def _custom_error_code(err_str: object) -> Optional[int]:
    """
    Extract a custom instruction error code from a Solana RPC error value,
    e.g. ``{'InstructionError': [3, {'Custom': 6024}]}`` → ``6024``.

    Returns None when the error carries no custom code (timeouts, broadcast
    rejections, generic InstructionError with a builtin index, …).
    """
    try:
        m = re.search(r"Custom['\"]?\s*[:=]?\s*(\d+)", str(err_str))
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


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
    cost_sol: float = 0.0  # ACTUAL SOL spent on the buy incl. fees (0 = fall back to size_sol)

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
        # No default: the buy size MUST come from the dashboard input field
        # (via /ws/live query param or the /api/live/buy_size store).  A
        # hard-coded size must never be traded.
        buy_size_sol: float,
        slippage_bps: int = 1500,
        priority_fee_lamports: int = 100_000,
        min_market_cap_usd: float = 6_000.0,
        engine_kwargs: Optional[dict] = None,

        # Skip on-chain simulation on the hot path (saves ~300 ms per swap).
        # Simulation is still run on explicit test buys if desired.
        skip_simulation: bool = True,
        engine_version: int = 2,

        # ── Entry reference price (engine parity) ──────────────────────────
        # The backtester notifies the engine of an entry at
        #   exec_price = open_of_execution_candle × (1 + slippage_pct/100)
        # (ForwardTester._open_long runs at State 1 of candle N+1 where the
        # intra-bar interpolation collapses to the open; default slip 1%).
        # Every V2 exit threshold (EVR offside, gain-retrace confirm,
        # kelly_flat offside, breakeven scratch, stop floors) references
        # engine.entry_price, so a different live reference shifts each
        # threshold crossing by seconds-to-minutes and desynchronises exits.
        # Live now notifies with the SAME formula: the first tick price of
        # the new candle (== its recorded open) × (1 + this slip).
        engine_entry_slippage_pct: float = 1.0,

        # ── No-motion stop ───────────────────────────────────────────────
        # If the price has not moved for this many wall-clock seconds while
        # a position is open, force-close it.  Set to 0 to disable.
        # Parity: backtester has no no-motion stop; live must replicate the
        # backtester exactly, so default to 0 (disabled).  The 300s idle
        # stop was forking recordings that the backtester later replayed as
        # having 2-3 extra trades after the live session had already shut
        # down.  Re-enable via an explicit dashboard param if needed.
        no_motion_stop_seconds: float = 0.0,
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
        # Engine-parity entry slip (see __init__ docstring): applied to the
        # entry reference price notified to the strategy engine only — it has
        # NO effect on on-chain sizing or PnL accounting.
        self.engine_entry_slippage_pct = float(engine_entry_slippage_pct)

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
        # acted on until the next candle boundary (is_new=True).  At that
        # point the live swap fires AND engine.notify_trade_opened/closed()
        # is called synchronously — matching the backtester where
        # _open_long/_close_long + notify happen at Step 1 of candle N+1.
        # If a buy fails on-chain, _fail_buy_flat() rolls back with
        # notify_trade_closed() so the engine sees "flat" again.
        self._pending_buy: bool = False
        self._pending_buy_reason: str = ""
        self._pending_exit: bool = False
        self._pending_exit_reason: str = ""

        # ── Entry-reference reconstruction state ───────────────────────────
        # Set when a BUY is armed during the 4-state expansion: the exact
        # price the backtester would have filled this signal at (slip
        # included), or None for state-4 signals whose fill point is the
        # next candle's open (resolved at dispatch from boundary_open_price).
        self._pending_buy_ref: Optional[float] = None
        # Signal candle timestamp — used as LiveTrade.entry_time so live
        # trade records align with backtester records (±1 candle).
        self._pending_buy_ts: int = 0

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

        # ── Explicit buy-in-flight flag ────────────────────────────────────
        # True from the moment a buy TX is broadcast until it is either
        # confirmed on-chain (→ open) or proven dead (→ failed).  This is the
        # authoritative buy-pending signal used by _is_buy_pending() — it is
        # INDEPENDENT of trade.status, because the exit path sets status to
        # "closing" BEFORE execute_sell runs, which used to disarm the old
        # status-based check and let a sell race an unconfirmed buy (the
        # 2026-08-11 EGPtQP race: sell_attempt fired 4 s before buy_confirmed,
        # closing the trade via verified_empty_wallet with no sell hash).
        self._buy_tx_in_flight: bool = False

        # ── Buy-failure re-entry block ──────────────────────────────────────
        # Wall-clock timestamp until which NO automatic buy may fire after a
        # failed buy (see BUY_FAIL_REENTRY_BLOCK_S).  A failed buy is NOT
        # retried — and the engine re-emitting a BUY on the next candle with
        # the same broken conditions must not become an implicit retry.
        self._buy_failed_until: float = 0.0
        self._buy_fail_block_announced: bool = False

        # ── Actual-SOL-received accounting ─────────────────────────────────
        # SOL balance captured when the buy confirmed (post-buy baseline).  A
        # completed sell's proceeds are MEASURED on-chain (exact per-TX
        # pre/post balances, wallet-delta fallback) instead of estimated from
        # the last price or the Jupiter quote outAmount — the number the
        # dashboard presents must be the SOL the wallet actually received.
        self._post_buy_sol_balance: float = 0.0
        self._pre_sell_sol_balance: float = 0.0
        self._pre_buy_sol_balance: float = 0.0   # SOL balance just before the buy broadcast

        # Last sell signature broadcast (confirmed or not).  Used to give
        # verified_empty_wallet / watchdog_finalise closes a linkable hash —
        # previously those paths closed with tx_hash_sell="" so the UI showed
        # a SELL row with no transaction.
        self._last_sell_sig: str = ""

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

        Driven by the explicit ``_buy_tx_in_flight`` flag, NOT by
        ``trade.status``: the exit path sets ``status="closing"`` *before*
        calling ``execute_sell``, so a status-based check sees
        ``"closing" != "pending"`` and incorrectly lets a sell race the
        still-unconfirmed buy.
        """
        ct = self.current_trade
        return (
            self._buy_tx_in_flight
            and ct is not None
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
                if sol is not None:
                    self._cached_sol_balance = sol
                    self._cached_balance_ts = time.time()
                if tok > 0:
                    self._cached_token_balance = tok
                    self._token_balance = tok
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

    async def cleanup(self, reason: str = "connection_closed"):
        """
        Emergency cleanup at live-session teardown.

        If a swap is already in flight, wait briefly for it to land — tearing
        the session down while a sell TX is mid-flight would abandon a position
        mid-execution.  Then, if a position is STILL open, run the emergency
        sell path (which retries indefinitely until tokens leave the wallet).
        """
        if self._swap_in_flight:
            logger.info("[CLEANUP] Swap in flight — waiting up to 20s for it to finish")
            deadline = time.time() + 20.0
            while self._swap_in_flight and time.time() < deadline:
                await asyncio.sleep(0.5)

        if self._is_buy_pending():
            logger.info("[CLEANUP] Buy pending — waiting up to 20s for it to settle before emergency sell")
            deadline = time.time() + 20.0
            while self._is_buy_pending() and time.time() < deadline:
                await asyncio.sleep(0.25)

        if self.current_trade is not None:
            logger.warning(
                f"[CLEANUP] Position still open at session teardown "
                f"({reason}) for {self.token_mint[:8]}… — launching emergency sell"
            )
            self.current_trade.status = "closing"
            self.current_trade.exit_reason = reason
            self._journal_event(
                "emergency_cleanup_sell", trade=self.current_trade,
                reason=reason,
            )
            try:
                sig = await asyncio.wait_for(
                    self.execute_sell("connection_closed"),
                    timeout=90.0,
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

            # ── Single fast status probe (first non-null answer wins) ─────────
            # The old implementation gathered a status poll from ALL RPCs every
            # round, paying the slowest endpoint's latency each time (~1.5s+).
            # _get_signature_status returns the FIRST RPC that knows the sig
            # (fast-RPC-first + concurrent fanout), so a confirmed TX is
            # detected in ~0.3–0.5s.
            status = await self._get_signature_status(sig)
            if status is not None:
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

        Speed: returns the INSTANT the first RPC accepts (FIRST_COMPLETED),
        cancelling the remaining in-flight send attempts.  Waiting for every
        RPC (the old ``asyncio.gather``) added the slowest endpoint's latency
        — typically the heavily rate-limited mainnet-beta public node — to
        every broadcast, which is the dominant cost on the swap hot path.
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

        try:
            tasks = [
                asyncio.create_task(_send_one(url)) for url in SOLANA_RPCS
            ]
            sig = None
            accepted = 0
            pending = set(tasks)

            start_time = time.time()
            timeout_s = 2.5

            while pending and (time.time() - start_time < timeout_s):
                rem_time = max(0.1, timeout_s - (time.time() - start_time))
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED, timeout=rem_time
                )
                for t in done:
                    try:
                        r = t.result() if not t.cancelled() else None
                        if isinstance(r, str) and r:
                            accepted += 1
                            if sig is None:
                                sig = r
                    except Exception:
                        pass
                if sig is not None:
                    for t in pending:
                        t.cancel()
                    break

            for t in pending:
                t.cancel()

            if sig:
                logger.info(
                    f"[BROADCAST OK] sig={sig} ({accepted} RPCs accepted fast path)"
                )
                logger.info(f"[SOLSCAN] https://solscan.io/tx/{sig}")
            else:
                logger.warning(f"[BROADCAST REJECTED] All {len(SOLANA_RPCS)} RPCs rejected or timed out")
            return sig
        except Exception as e:
            logger.warning(f"[BROADCAST ERROR] {e}")
            return None

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

    async def _sign_and_send(self, swap_tx_b64: str, wait_for_confirmation: bool = False,
                             force_fresh_blockhash: bool = False) -> Optional[dict]:
        """
        Sign the base64-encoded versioned transaction and broadcast it
        to ALL RPCs simultaneously.

        Hot path (wait_for_confirmation=False):
          - Rebuild TX with a fresh cached blockhash (maximises landing window)
          - skip_simulation=True  → no simulate call (saves ~300 ms)
          - Multi-RPC fanout for maximum landing probability
          - Returns the INSTANT the TX is on the wire.
            Confirmation + rebroadcast run as a background task.

        ``force_fresh_blockhash=True`` (sell retries): fetch a brand-new
        blockhash from the RPCs instead of trusting the cache, so a retry
        transaction is never byte-identical to the failed attempt (same quote
        amount + same cached blockhash ⇒ same signature ⇒ same failure).

        Returns ``{"sig": str, "last_valid_block_height": int | None}`` on
        success, ``None`` if every RPC rejected the broadcast.  When
        ``wait_for_confirmation=True`` and the TX is confirmed-as-failed (or
        times out), returns ``{"sig": None, "error": str}`` so the caller can
        act on the on-chain error (e.g. clamp the sell amount on 6024).
        """
        try:
            raw_tx = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(raw_tx)

            # ── Rebuild with the freshest cached blockhash if available ────
            last_valid_height: Optional[int] = None
            fresh = self._get_fresh_blockhash()
            if force_fresh_blockhash:
                fetched = await self._fetch_latest_blockhash()
                if fetched:
                    fresh = fetched
                    self._cached_blockhash = fetched[0]
                    self._cached_blockhash_lvh = fetched[1]
                    self._cached_blockhash_ts = time.time()
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
                    return {
                        "sig": None,
                        "broadcast_sig": sig,
                        "last_valid_block_height": last_valid_height,
                        "error": confirm_result["error"],
                    }
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

    async def _get_sol_balance(self, timeout_s: float = 1.2) -> Optional[float]:
        """Fetch wallet SOL balance — fast-path cached RPC, fanout otherwise.

        Returns None if no RPC answered; callers can fall back to cached figures
        or retry rather than failing the swap or overwriting cache with zero.
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

        result = await self._rpc_fanout_first_wins(payload, timeout_s=timeout_s, result_fn=_extract)
        return result if isinstance(result, float) else None

    async def _get_token_balance(self, commitment: str = "processed") -> int:
        """
        Authoritative on-chain token balance (raw smallest units).

        Robustness rewrite: a single RPC returning an EMPTY account list is
        NOT trusted.  Every RPC's reply is collected; a positive amount wins
        immediately.  "0" is only returned once it is corroborated by a
        direct ``getTokenAccountBalance`` read of the wallet's derived ATAs
        (SPL and Token-2022 variants) — a completely different code path that
        reads the account state directly instead of scanning by owner+mint.

        This kills the failure mode that produced phantom empty wallets in
        live sessions: one lagging/stale node's empty reply short-circuited
        the whole read (``_rpc_fanout_first_wins`` returns the first non-None
        result, and an empty account list parsed to ``0``), which then caused
        inflated provisional buy amounts to be quoted for sells → ``Custom:
        6024`` failures, and phantom "-100% PnL" closes on trades whose
        tokens were demonstrably in the wallet.

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
            try:
                accounts = data.get("result", {}).get("value", [])
                if not accounts:
                    return 0  # this RPC claims empty — corroborate below
                parsed = accounts[0]["account"]["data"]["parsed"]
                amount = int(parsed["info"]["tokenAmount"]["amount"])
                decimals = int(parsed["info"]["tokenAmount"]["decimals"])
                if decimals != self._token_decimals:
                    logger.info(f"[BAL] Token decimals detected: {decimals} (was {self._token_decimals})")
                    self._token_decimals = decimals
                return amount
            except Exception:
                return None

        try:
            s = await self._get_session()

            async def _probe(rpc_url: str) -> Optional[int]:
                try:
                    async with s.post(
                        rpc_url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=1.5),
                    ) as r:
                        if r.status != 200:
                            return None
                        data = await r.json()
                        if "error" in data:
                            return None
                        return _extract(data)
                except Exception:
                    return None

            # Fire all RPC probes concurrently; return the INSTANT any of them
            # reports a positive balance (fast-RPC typical case ~0.3s).  Only
            # if no probe reports positive do we wait for the stragglers and
            # then corroborate "0" via the direct ATA read.
            tasks = [
                asyncio.ensure_future(_probe(url)) for url in SOLANA_RPCS
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED, timeout=1.5,
            )
            positives = [
                t.result() for t in done
                if isinstance(t.result(), int) and t.result() > 0
            ]
            if positives:
                for t in pending:
                    t.cancel()
                return max(positives)
            if pending:
                done2, pending2 = await asyncio.wait(
                    pending, return_when=asyncio.ALL_COMPLETED, timeout=0.8,
                )
                positives += [
                    t.result() for t in done2
                    if isinstance(t.result(), int) and t.result() > 0
                ]
                for t in pending2:
                    t.cancel()
            if positives:
                return max(positives)

            # All responding RPCs claim 0 (or some errored).  Corroborate via
            # a direct ATA balance read before trusting "empty".
            ata_bal = await self._get_ata_balance(commitment)
            if ata_bal is not None:
                return ata_bal
            return 0
        except Exception as e:
            logger.warning(f"[BAL] token balance read failed: {e}")
            return 0

    async def _get_ata_balance(self, commitment: str = "processed") -> Optional[int]:
        """
        Direct ``getTokenAccountBalance`` read of the wallet's derived ATAs
        (SPL and Token-2022 program variants), fanning out across all RPCs.

        A valid integer reply is authoritative (the account exists and holds
        that many raw units — including 0).  Returns None only if every probe
        errored (account missing / RPC failure) so callers can keep a fallback
        instead of trusting an unverifiable 0.
        """
        try:
            mint_pk = Pubkey.from_string(self.token_mint)
            owner_pk = self.keypair.pubkey()
            candidates = [
                str(_derive_ata(owner_pk, mint_pk, SPL_TOKEN_PROGRAM_ID)),
                str(_derive_ata(owner_pk, mint_pk, TOKEN_2022_PROGRAM_ID)),
            ]
            payloads = [
                {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTokenAccountBalance",
                    "params": [ata, {"commitment": commitment}],
                }
                for ata in candidates
            ]
            s = await self._get_session()

            async def _probe(rpc_url: str, payload: dict) -> Optional[int]:
                try:
                    async with s.post(
                        rpc_url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=1.5),
                    ) as r:
                        if r.status != 200:
                            return None
                        data = await r.json()
                        if "error" in data:
                            return None
                        value = data.get("result", {}).get("value") or {}
                        amount = value.get("amount")
                        if amount is None:
                            return None
                        try:
                            amt = int(amount)
                        except (TypeError, ValueError):
                            return None
                        if isinstance(value.get("decimals"), int):
                            dec = int(value["decimals"])
                            if dec != self._token_decimals:
                                self._token_decimals = dec
                        return amt
                except Exception:
                    return None

            results = await asyncio.gather(
                *[_probe(url, p) for url in SOLANA_RPCS for p in payloads],
                return_exceptions=True,
            )
            amounts = [r for r in results if isinstance(r, int)]
            if not amounts:
                return None
            return max(amounts)
        except Exception as e:
            logger.warning(f"[BAL] ATA balance read failed: {e}")
            return None

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
            # The background balance cache keeps this figure fresh (~8s).  If
            # the cache has never been populated (session just started) or is
            # zero/stale, we perform an inline RPC read.
            sol_bal = self._get_cached_sol_balance()
            if self._cached_balance_ts == 0.0 or sol_bal <= 0.0 or (time.time() - self._cached_balance_ts) > 30.0:
                fresh_sol = await self._get_sol_balance(timeout_s=1.5)
                if fresh_sol is None and (sol_bal <= 0.0 or self._cached_balance_ts == 0.0):
                    fresh_sol = await self._get_sol_balance(timeout_s=2.5)
                if fresh_sol is not None:
                    sol_bal = fresh_sol
                    self._cached_sol_balance = fresh_sol
                    self._cached_balance_ts = time.time()
                elif self._cached_sol_balance > 0.0 and self._cached_balance_ts > 0.0:
                    sol_bal = self._cached_sol_balance
                else:
                    sol_bal = None

            sol_bal_val = sol_bal if sol_bal is not None else 0.0
            self._pre_buy_sol_balance = sol_bal_val
            logger.info(
                f"[BUY] Starting buy: mint={mint_str[:8]}… size={self.buy_size_sol} SOL "
                f"balance={sol_bal_val:.4f} SOL (cached) reason={reason}"
            )
            self._journal_event(
                "buy_attempt", reason=reason, size_sol=self.buy_size_sol,
                amount_lamports=amount_lam, sol_balance=sol_bal_val,
            )

            if sol_bal is None:
                logger.error("[BUY FAILED] Balance check failed: RPC timed out/failed and no cache available")
                self._journal_event(
                    "buy_rejected", reason=reason, error="balance_check_failed",
                    sol_balance=0.0, required_sol=self.buy_size_sol,
                )
                await self._fail_buy_flat("RPC balance check failed", reason)
                return None

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
            self._buy_tx_in_flight = True
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

        RE-ENTRY BLOCK: after any failed buy, no further automatic buy may
        fire for BUY_FAIL_REENTRY_BLOCK_S (see _process_completed_candle /
        update).  A failed buy is never retried; the engine re-emitting a BUY
        on subsequent candles is NOT allowed to become an implicit retry.
        """
        if self.current_trade is not None:
            failed = self.current_trade
            # Defensive final probe: never fail a buy whose tokens are
            # actually sitting in the wallet (belt-and-braces — the settle
            # loop already probed, but RPC lag is real).  Use confirmed
            # commitment so we don't act on a lagging processed snapshot.
            try:
                on_chain = await self._get_token_balance(commitment="confirmed")
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
        self._buy_tx_in_flight = False
        self._pending_buy = False
        self._pending_buy_reason = ""
        # No automatic buy for the re-entry block window — a failed buy must
        # never be immediately re-attempted by a lingering engine signal.
        self._buy_failed_until = time.time() + BUY_FAIL_REENTRY_BLOCK_S
        self._buy_fail_block_announced = False
        self.engine.notify_trade_closed()
        await self._broadcast_status("buy_failed", detail, reason)
        logger.warning(
            f"[BUY FAIL BLOCK] No automatic buys for the next "
            f"{BUY_FAIL_REENTRY_BLOCK_S:.0f}s (failed buy: {detail})"
        )

    async def _verify_buy_settled(self, sig: str, expected_amount: int):
        """Background post-buy balance reconcile.  The buy is CONFIRMED on-chain,
        so the tokens MUST exist — a ``0`` read is just processed-commitment /
        ATA-propagation lag, never a real empty wallet.  Poll briefly until we
        get a definitive non-zero read and adopt it, so ``_token_balance`` /
        ``_cached_token_balance`` reflect reality before any sell can fire.
        Never leaves the cache stale by silently returning on a lagged 0."""
        for attempt in range(6):  # ~0.25s initial + up to 5 × 0.3s retries
            await asyncio.sleep(0.25 if attempt == 0 else 0.3)
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
            self._buy_tx_in_flight = False
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
        """Non-blocking stats-balance refresh used after a confirmed buy.

        Also captures ``_post_buy_sol_balance`` — the SOL baseline against
        which the next sell's proceeds are measured (see
        ``_measure_sell_proceeds``).  If the read looks stale (≥ the pre-buy
        cached figure, i.e. the buy clearly hasn't been debited yet), retry
        once after a short lag so the baseline is never the pre-buy balance.
        """
        bal = await self._get_sol_balance()
        if bal is not None and bal > 0:
            self.stats.starting_balance = bal
            self._post_buy_sol_balance = bal
            if bal >= self._cached_sol_balance and self._cached_sol_balance > 0:
                await asyncio.sleep(0.4)
                bal2 = await self._get_sol_balance()
                if bal2 is not None and bal2 > 0:
                    self.stats.starting_balance = bal2
                    self._post_buy_sol_balance = bal2
            # Actual SOL spent on the buy (incl. priority + base fees) —
            # measured as the wallet delta, stored on the trade as its real
            # cost basis so PnL is fully on-chain measured (not nominal
            # buy_size_sol).
            if self._pre_buy_sol_balance > 0 and self._post_buy_sol_balance > 0:
                spent = max(0.0, self._pre_buy_sol_balance - self._post_buy_sol_balance)
                ct = self.current_trade
                if spent > 0 and ct is not None and ct.tx_hash_buy:
                    ct.cost_sol = spent

    async def _get_tx_sol_proceeds(self, sig: str) -> Optional[float]:
        """EXACT on-chain SOL received by a swap, read from the transaction's
        own balance-change fields.

        ``getTransaction`` reports every account's balance before and after
        the TX plus the total fee it paid (base + priority).  For the wallet
        the net change is ``−fee + proceeds``, so the exact proceeds are
        ``post − pre + fee`` — no quotes, no slippage bands, no price
        estimates, and immune to fee burns from unrelated failed attempts.

        Returns None if the TX can't be read yet (not finalized / RPC miss);
        callers fall back to the wallet-delta measurement.
        """
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTransaction",
            "params": [
                sig,
                {"encoding": "jsonParsed",
                 "maxSupportedTransactionVersion": 0,
                 "commitment": "confirmed"},
            ],
        }
        result = None
        try:
            s = await self._get_session()
            for _attempt in range(3):
                try:
                    async with s.post(
                        SOLANA_RPC_PRIMARY, json=payload,
                        timeout=aiohttp.ClientTimeout(total=4.0),
                    ) as r:
                        data = await r.json()
                    result = data.get("result") if isinstance(data, dict) else None
                    if result:
                        break
                except Exception:
                    result = None
                await asyncio.sleep(0.4)
        except Exception as e:
            logger.warning(f"[PROCEEDS] getTransaction failed for {sig[:8]}…: {e}")
            return None
        if not result:
            logger.warning(f"[PROCEEDS] No getTransaction result yet for {sig[:8]}…")
            return None
        try:
            meta = result.get("meta") or {}
            if meta.get("err"):
                return None
            pre = meta.get("preBalances") or []
            post = meta.get("postBalances") or []
            fee = meta.get("fee") or 0
            if not pre or not post:
                return None
            msg = result.get("transaction", {}).get("message", {})
            keys = msg.get("accountKeys") or []
            wallet_idx = None
            for i, k in enumerate(keys):
                pk = k if isinstance(k, str) else (k.get("pubkey") if isinstance(k, dict) else None)
                if pk == self.wallet_pubkey:
                    wallet_idx = i
                    break
            if wallet_idx is None:
                wallet_idx = 0  # fee payer is always the first account
            if wallet_idx >= len(pre) or wallet_idx >= len(post):
                return None
            pre_w, post_w = pre[wallet_idx], post[wallet_idx]
            if pre_w is None or post_w is None:
                return None
            received = (post_w - pre_w + fee) / 1e9
            logger.info(f"[PROCEEDS] tx={sig[:8]}… wallet {post_w - pre_w:+d} lamports, fee={fee} → received={received:.8f} SOL")
            return max(0.0, received)
        except Exception as e:
            logger.warning(f"[PROCEEDS] parse failed for {sig[:8]}…: {e}")
            return None

    async def _measure_sell_proceeds(self) -> Optional[float]:
        """Actual SOL received from a completed sell, measured on-chain as the
        SOL-balance delta (post − pre) plus the known priority fee.

        This is the REAL amount the wallet gained — the figure the dashboard
        should present as the trade's proceeds — rather than the Jupiter
        quote's outAmount estimate (which can differ by up to the slippage
        band) or a price×tokens estimate (which is worse).  Returns None when
        it cannot be measured; callers fall back to an estimate.
        """
        pre = self._pre_sell_sol_balance
        if pre <= 0:
            return None
        try:
            post = await self._get_sol_balance()
        except Exception:
            return None
        if post is None or post <= 0:
            return None
        fee_sol = self.priority_fee_lamports / 1e9
        received = post - pre + fee_sol
        return max(0.0, received)

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

            # ── If a buy is still pending, WAIT for it to settle ──────────────
            # Selling into a pending buy races the landing TX (the sell would
            # quote against a provisional balance and could close a position
            # that never actually opened on-chain).  Wait for the settle loop
            # to resolve the buy (bounded by its own finality deadline + grace)
            # and sell the INSTANT it confirms — the fastest correct option,
            # with no deferral hop to the watchdog.  If the buy dies instead,
            # the settle loop clears current_trade and there is nothing to sell.
            if self._is_buy_pending():
                logger.info("[SELL] Buy still pending — waiting for it to settle before selling")
                wait_deadline = time.time() + BUY_CONFIRM_TIMEOUT_S + 5.0
                while self._is_buy_pending() and time.time() < wait_deadline:
                    await asyncio.sleep(0.25)
                if self.current_trade is None:
                    # The buy died while we waited — nothing to sell.
                    logger.info("[SELL] Buy failed while waiting — no position to sell")
                    return None
                if self._is_buy_pending():
                    # Still unresolved beyond the buy-finality deadline (the
                    # settle loop is about to declare it dead).  Defend against
                    # a race with that declaration by deferring to the watchdog.
                    logger.warning(
                        "[SELL] Buy still pending past finality deadline — deferring to watchdog"
                    )
                    self._journal_event(
                        "sell_deferred", reason=reason,
                        detail="buy_pending_past_finality_deadline",
                        trade=self.current_trade,
                    )
                    await self._broadcast_status(
                        "sell_pending",
                        "Buy still confirming on-chain — sell fires once it lands",
                        reason,
                    )
                    return None

            # ── Pre-sell SOL baseline for proceeds measurement ────────────────
            # The proceeds of the sell are MEASURED as the on-chain SOL-balance
            # delta from this baseline (see _measure_sell_proceeds).  Prefer
            # the balance captured when the buy confirmed (zero extra reads on
            # the hot path); fall back to one fresh bounded read.
            if self._post_buy_sol_balance > 0:
                self._pre_sell_sol_balance = self._post_buy_sol_balance
            else:
                try:
                    self._pre_sell_sol_balance = (
                        await asyncio.wait_for(self._get_sol_balance(), timeout=1.5)
                    ) or 0.0
                except Exception:
                    self._pre_sell_sol_balance = 0.0
            self._last_sell_sig = ""

            logger.info(
                f"[SELL] Starting sell: mint={mint_str[:8]}… reason={reason} "
                f"cached_balance={self._token_balance} units"
            )
            self._journal_event(
                "sell_attempt", reason=reason,
                cached_token_balance=self._token_balance,
            )
            # ── Authoritative on-chain token balance (accuracy + SPEED) ──────
            # `Custom: 6024` (insufficient token account balance) is the #1
            # cause of failed sells, and it happens when we try to sell MORE
            # tokens than the wallet actually holds — typically because the
            # cached figure came from the buy's Jupiter outAmount (inflated by
            # slippage / a partial fill).  We therefore ALWAYS read the live
            # on-chain balance before building the swap.
            #
            # Speed: ONE bounded read (fast-RPC-first fanout, ~0.2–0.5s).  The
            # old 4-probe loop (each probe up to ~2.4s + 0.5s sleeps) burned
            # ~9s BEFORE the first attempt — the dominant cost of the 10s+
            # sells seen in production.  If the single read returns 0 we use
            # the last-known cached figure for the FIRST attempt; the retry
            # loop re-reads and clamps the amount before every subsequent
            # quote, so a wrong first amount costs one 6024 round-trip at
            # most — not nine seconds.
            fresh_bal = 0
            try:
                fresh_bal = await self._get_token_balance()
            except Exception:
                fresh_bal = 0
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
                    # EVERY attempt after the first (cheap single read).  A
                    # partial fill — or a 6024 clamp from the previous attempt
                    # — must be reflected in the next quote.
                    if attempt_group > 1 or inner > 1:
                        live_bal = await self._get_token_balance()
                        if live_bal > 0:
                            if live_bal != token_balance:
                                logger.info(
                                    f"[SELL] Balance refreshed on {label}: "
                                    f"{token_balance} → {live_bal}"
                                )
                            token_balance = live_bal
                            self._token_balance = token_balance
                        elif token_balance > 0:
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
                    #
                    # Retries force a FRESH blockhash so the retry TX is never
                    # byte-identical to the failed attempt (same cached
                    # blockhash + same amount ⇒ same signature ⇒ same failure).
                    send_res = await self._sign_and_send(
                        swap_tx, wait_for_confirmation=True,
                        force_fresh_blockhash=(attempt_group > 1 or inner > 1),
                    )
                    sig = send_res.get("sig") if isinstance(send_res, dict) else None
                    broadcast_sig = send_res.get("broadcast_sig") if isinstance(send_res, dict) else None
                    tx_error = (send_res.get("error") if isinstance(send_res, dict) else "broadcast_rejected")
                    if sig:
                        self._last_sell_sig = sig
                    elif broadcast_sig:
                        self._last_sell_sig = broadcast_sig
                    if not sig:
                        # On-chain amount-mismatch errors (6024 etc.): the
                        # quoted amount exceeds the real balance.  Re-read the
                        # authoritative balance and CLAMP before the next
                        # quote — otherwise every retry re-fails with the
                        # same inflated amount (the observed 6024×N chains).
                        if _custom_error_code(tx_error) in NONSIMULATION_ABORT_CODES:
                            try:
                                live_bal = await self._get_token_balance()
                            except Exception:
                                live_bal = 0
                            if live_bal > 0 and live_bal < token_balance:
                                logger.warning(
                                    f"[SELL AMOUNT CLAMP] {label} error={tx_error}: "
                                    f"{token_balance} → {live_bal} units (real on-chain balance)"
                                )
                                token_balance = live_bal
                                self._token_balance = live_bal
                                self._cached_token_balance = live_bal
                        logger.warning(
                            f"[SELL FAILED {label}] TX not confirmed (err={tx_error}) — "
                            f"retrying immediately with fresh quote"
                        )
                        await asyncio.sleep(0.05)
                        continue

                    # ── Success: TX confirmed on-chain — close ONCE ──────────
                    elapsed = time.time() - sell_start
                    # EXACT proceeds: read the sell TX's own on-chain pre/post
                    # balance change.  Fall back to the wallet-delta measurement,
                    # then to the quote outAmount.  PnL is presented from the
                    # SOL the wallet actually received, never from an estimate.
                    sol_received = int(quote.get("outAmount", 0)) / 1e9
                    measured = await self._get_tx_sol_proceeds(sig)
                    if measured is None:
                        measured = await self._measure_sell_proceeds()
                    if measured is not None:
                        sol_received = measured

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
                        await asyncio.sleep(0.35)
                if bal == 0:
                    # The wallet READS empty — but is that a completed sale?
                    # NOT necessarily: if every sell TX failed on-chain (e.g.
                    # a 6024 chain) while balance reads lagged/errored, the
                    # tokens may never have existed (failed buy) or the read
                    # is still wrong.  Closing here with 0 proceeds produced
                    # the phantom "-100% PnL" trades seen in production.
                    # We only declare "verified empty" when a sell is PROVEN:
                    #   1. the last broadcast sig's status shows confirmed, or
                    #   2. the SOL-balance delta measures proceeds > 0.
                    # Otherwise the buy never delivered tokens → reconcile as
                    # a FAILED BUY (never a -100% close).
                    confirmed_sell = False
                    proceeds = None
                    if self._last_sell_sig:
                        st = await self._get_signature_status(self._last_sell_sig)
                        if (st is not None and not st.get("err")
                                and st.get("confirmationStatus") in ("confirmed", "finalized")):
                            confirmed_sell = True
                            proceeds = await self._get_tx_sol_proceeds(self._last_sell_sig)
                    if proceeds is None:
                        proceeds = await self._measure_sell_proceeds()

                    if confirmed_sell or (proceeds is not None and proceeds > 0):
                        # A sell DID land on-chain (even though the confirm
                        # poll in this loop missed it).  Measure the EXACT
                        # proceeds from the last broadcast sig's own balance
                        # change; fall back to the wallet-delta, then a
                        # price×tokens estimate.
                        estimated = proceeds is None
                        if proceeds is None:
                            proceeds = 0.0
                            if self.current_trade:
                                est_price = self._last_price or self.current_trade.entry_price
                                if est_price > 0 and self.current_trade.size_tokens > 0:
                                    proceeds = self.current_trade.size_tokens * est_price
                        logger.info(
                            "[SELL VERIFIED] Wallet is now empty and a sell is "
                            "confirmed on-chain — exiting sell loop cleanly "
                            f"(sol_received={proceeds:.6f}"
                            f"{' estimated' if estimated else ' measured via balance delta'})."
                        )
                        self._token_balance = 0
                        self._last_exit_signal_ts = 0.0
                        closed_trade = None
                        if self.current_trade:
                            closed_trade = self.confirm_sell(
                                self._last_sell_sig or "", proceeds, self._last_price
                            )
                        self._journal_event(
                            "sell_confirmed", trade=closed_trade, reason=reason,
                            tx_hash=self._last_sell_sig or None,
                            solscan=(f"https://solscan.io/tx/{self._last_sell_sig}"
                                     if self._last_sell_sig else None),
                            via="verified_empty_wallet",
                            sol_received=proceeds, estimated=estimated,
                            attempt_group=attempt_group,
                            pnl_sol=(closed_trade.pnl_sol if closed_trade else None),
                            pnl_pct=(closed_trade.pnl_pct if closed_trade else None),
                        )
                        # CRITICAL UI FIX: this close previously never broadcast
                        # sell_confirmed — the frontend only renders SELL rows
                        # from that event, so these trades were permanently
                        # missing from the trade history table.
                        await self._broadcast_status(
                            "sell_confirmed", self._last_sell_sig or "", reason,
                            sol_received=proceeds, closed_trade=closed_trade,
                        )
                        return "verified_empty"

                    # ── NO confirmed sell, NO proceeds: buy never landed ──────
                    # Every attempt in this loop failed on-chain (or never
                    # broadcast) and the wallet reads empty.  The 0.01 SOL for
                    # this position never left the wallet — closing it with
                    # sol_received=0 would invent a -100% loss that never
                    # happened.  Reconcile as a FAILED BUY (flat, no PnL
                    # bookkeeping) and let the engine see "flat" again.
                    logger.error(
                        "[SELL ABORT] Wallet reads empty but NO sell confirmed "
                        "on-chain and no SOL proceeds measured — the buy never "
                        "delivered tokens.  Reconciling as a FAILED BUY instead "
                        "of a phantom -100% close."
                    )
                    self._journal_event(
                        "sell_abort_buy_never_landed", reason=reason,
                        last_sell_sig=self._last_sell_sig or None,
                        attempt_group=attempt_group,
                        trade=self.current_trade,
                    )
                    await self._fail_buy_flat(
                        "Sell loop: wallet empty with no confirmed sell — buy never landed on-chain",
                        reason,
                    )
                    if self.current_trade is not None:
                        # _fail_buy_flat's defensive probe found the tokens —
                        # the buy DID land after all; keep selling with the
                        # now-correct on-chain balance.
                        token_balance = self._token_balance
                        if token_balance > 0:
                            continue
                    await self._broadcast_status(
                        "sell_failed",
                        "No confirmed sell and wallet empty — buy never landed "
                        "(reconciled as failed buy, no PnL recorded)",
                        reason,
                    )
                    return None
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
                    # Wallet reads empty with a trade still marked open.  Only
                    # finalise locally if a sell is PROVEN to have landed (last
                    # sig confirmed, or SOL proceeds measured) — otherwise the
                    # buy never delivered tokens and closing would invent a
                    # phantom -100% PnL (the production bug this guards).
                    confirmed_sell = False
                    proceeds = None
                    if self._last_sell_sig:
                        st = await self._get_signature_status(self._last_sell_sig)
                        if (st is not None and not st.get("err")
                                and st.get("confirmationStatus") in ("confirmed", "finalized")):
                            confirmed_sell = True
                            proceeds = await self._get_tx_sol_proceeds(self._last_sell_sig)
                    if proceeds is None:
                        proceeds = await self._measure_sell_proceeds()

                    if not confirmed_sell and (proceeds is None or proceeds <= 0):
                        logger.error(
                            "[WATCHDOG] Wallet reads empty but no sell confirmed "
                            "and no proceeds measured — buy never landed. "
                            "Reconciling as a FAILED BUY instead of a phantom -100% close."
                        )
                        self._journal_event(
                            "watchdog_buy_never_landed",
                            last_sell_sig=self._last_sell_sig or None,
                            trade=self.current_trade,
                        )
                        await self._fail_buy_flat(
                            "Watchdog: wallet empty with no confirmed sell — buy never landed on-chain",
                            "watchdog_finalise",
                        )
                        if self.current_trade is not None:
                            # _fail_buy_flat's defensive probe found tokens —
                            # keep the watchdog armed so the sell is retried.
                            self._last_exit_signal_ts = time.time()
                            continue
                        self._last_exit_signal_ts = 0.0
                        continue

                    # Swap already completed on-chain but local state was stale.
                    # Measure the EXACT proceeds from the last broadcast sig's
                    # own balance change; fall back to the wallet-delta, then a
                    # price×tokens estimate.
                    estimated = proceeds is None
                    if proceeds is None:
                        proceeds = 0.0
                        if self.current_trade:
                            est_price = self._last_price or self.current_trade.entry_price
                            if est_price > 0 and self.current_trade.size_tokens > 0:
                                proceeds = self.current_trade.size_tokens * est_price
                    logger.info(
                        f"[WATCHDOG] On-chain balance = 0 and sell confirmed — "
                        f"finalising trade locally "
                        f"(sol_received={proceeds:.6f}"
                        f"{' estimated' if estimated else ' measured via balance delta'})"
                    )
                    if self.current_trade is not None:
                        self.current_trade.exit_reason = "watchdog_finalise"
                        closed_trade = self.confirm_sell(
                            self._last_sell_sig or "", proceeds, self._last_price
                        )
                        self._journal_event(
                            "sell_confirmed", trade=closed_trade,
                            reason="watchdog_finalise",
                            tx_hash=self._last_sell_sig or None,
                            solscan=(f"https://solscan.io/tx/{self._last_sell_sig}"
                                     if self._last_sell_sig else None),
                            via="watchdog_finalise",
                            sol_received=proceeds, estimated=estimated,
                            pnl_sol=(closed_trade.pnl_sol if closed_trade else None),
                            pnl_pct=(closed_trade.pnl_pct if closed_trade else None),
                        )
                        # Previously this close never broadcast sell_confirmed,
                        # so the UI never showed the SELL row for it.
                        await self._broadcast_status(
                            "sell_confirmed", self._last_sell_sig or "",
                            "watchdog_finalise",
                            sol_received=proceeds, closed_trade=closed_trade,
                        )
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
                                   sell_vol: float = 0.0,
                                   market_cap_usd: float = 0.0,
                                   pool_sol: float = 0.0,
                                   engine_exec: bool = True) -> dict:
        """
        Mirror ForwardTester.update() exactly — called once per completed candle.

        Step 1: expand this candle into 4 intra-candle sub-states and call
                engine.update() on each — identical to the backtester loop.
                Uses _build_full_result=False for all 4 states, matching the
                backtester's fast path.

        Step 2: act on signals EXACTLY where the backtester acts.
                ForwardTester executes a queued signal at Step 1 of its NEXT
                ft.update() call — i.e. for a signal emitted at intra-candle
                state k < 4, at state k+1 of the SAME candle (mid-candle
                fill), and for k == 4 at State 1 of the next candle.  The
                fill flips the engine's in_position flag BETWEEN sub-states,
                which changes what the engine can emit for the remainder of
                the candle (a mid-candle close lets it re-arm a BUY on states
                k+1..4; a mid-candle open lets it emit further EXITs).
                We replicate this by notifying the engine synchronously at
                the same point in the expansion (``engine_exec=True``).
                Swap DISPATCH is decoupled: signals are armed as pending
                flags that update()/_try_execute_pending() fire immediately
                and retry on later boundaries if blocked — the ForwardTester
                never discards a queued signal, and neither must live
                trading.

                ``engine_exec=False`` (historical warmup) only collects the
                earliest signal into the result dict; the caller clears the
                pending flags so warmup never triggers real swaps.

        Returns the engine result from the final sub-state (state 4).
        """
        # ── Step 1: 4-state expansion ────────────────────────────────────────
        bullish = c >= o
        if bullish:
            mid_first, mid_second = h, l
        else:
            mid_first, mid_second = l, h

        # Deterministic OHLC of each sub-state (open, first extreme, second
        # extreme, close) — the fill point of a signal emitted at state k is
        # state k+1's row (or the next candle's open for k == 4).
        states = [
            (o, o, o, o),
            (o, max(o, mid_first), min(o, mid_first), mid_first),
            (o, h, l, mid_second),
            (o, h, l, c),
        ]

        final_signal = None
        final_regime = None

        # Engine's position view at expansion start; mirrors what the
        # backtester's engine sees as ft.update walks the same states.
        pos = bool(self.engine.in_position)

        result = {}
        for k, (so, sh, sl, sc) in enumerate(states, start=1):
            if k == 4:
                result = self.engine.update(t, so, sh, sl, sc, vol,
                                            buy_volume=buy_vol,
                                            sell_volume=sell_vol,
                                            pool_sol=pool_sol,
                                            market_cap_usd=market_cap_usd,
                                            _build_full_result=False)
            else:
                result = self.engine.update(t, so, sh, sl, sc, 0.0,
                                            _build_full_result=False)
            sig = (result or {}).get("signal", "none")
            if sig in (Signal.NONE.value, "none"):
                continue

            regime_k = (result or {}).get("regime", "")
            if final_signal is None:
                final_signal = sig
                final_regime = regime_k

            if not engine_exec:
                continue

            # ── Interleaved execution (ForwardTester Step-1 analogue) ────
            if sig == Signal.EXIT.value and pos and not self._pending_exit:
                reason = (result or {}).get("exit_reason")
                if not reason:
                    reason = "exit_signal"
                    if regime_k == Regime.REVERSAL.value:
                        reason = "reversal_exit"
                    elif regime_k == Regime.EXHAUSTION.value:
                        reason = "exhaustion_exit"
                    elif regime_k == Regime.CONTINUATION.value:
                        reason = "continuation_exit"
                    elif regime_k == Regime.TREND.value:
                        reason = "trend_exit"
                self._pending_exit = True
                self._pending_exit_reason = reason
                self._pending_buy = False
                self._pending_buy_ref = None
                self._pending_buy_ts = 0
                # Backtest closes the position at state k+1: notify the
                # engine here so the remaining sub-states run FLAT, exactly
                # as they do inside the backtester's expansion.
                self.engine.notify_trade_closed()
                pos = False

            elif sig == Signal.BUY.value and not pos and not self._pending_buy:
                if time.time() < self._buy_failed_until:
                    # A previous buy failed — NO further automatic buys
                    # until the re-entry block window elapses.  A failed buy
                    # is never retried; the engine re-emitting BUY with the
                    # same broken conditions must not become an implicit
                    # retry (user requirement).
                    if not self._buy_fail_block_announced:
                        self._buy_fail_block_announced = True
                        remaining = max(0.0, self._buy_failed_until - time.time())
                        logger.info(
                            f"[BUY BLOCK] BUY signal suppressed — re-entry "
                            f"block active for another {remaining:.0f}s "
                            f"(after failed buy)"
                        )
                    continue
                self._pending_buy = True
                self._pending_buy_reason = f"buy_{regime_k}"
                self._pending_buy_ts = t
                self._pending_exit = False
                if k < 4:
                    # Fill happens at state k+1 of THIS candle — reconstruct
                    # the exact price the backtester fills at (raw path
                    # interpolation × slippage) and notify the engine NOW so
                    # states k+2..4 run IN POSITION, as in the backtest.
                    _, fh, fl, fc = states[k]   # state k+1 row
                    from forward_tester import ForwardTester as _FT
                    raw = _FT._intrabar_price(o, fh, fl, fc,
                                              self._backtest_fill_fraction())
                    self._pending_buy_ref = float(raw) * (
                        1.0 + self.engine_entry_slippage_pct / 100.0)
                    if self.current_trade is None:
                        self.engine.notify_trade_opened(
                            self._pending_buy_ref, Direction.UP)
                        pos = True
                    # else: a previous sell is still unsettled — defer the
                    # notify to dispatch time (accounting safety over the
                    # rare-mid-candle edge).
                else:
                    # State-4 signal: the backtest fills at State 1 of the
                    # NEXT candle (its open).  Notify at dispatch time when
                    # boundary_open_price is known — still before the next
                    # expansion, i.e. the identical engine-visible point.
                    self._pending_buy_ref = None

        # Propagate earliest signal into the final result dict
        if final_signal is not None:
            result["signal"] = final_signal
            if final_regime is not None:
                result["regime"] = final_regime

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

    def _backtest_fill_fraction(self) -> float:
        """Replicate ForwardTester._fill_fraction() with live-run parameters.

        The backtester interpolates its fill along the intra-bar path at this
        fraction (fee competitiveness × order size × slippage tolerance).
        Live uses the same fixed priority fee (0.0001 SOL) and the same
        formula so the reconstructed entry reference matches exactly.
        """
        import math
        ref_fee = 0.0001          # ForwardTester._REFERENCE_FEE
        ref_size = 0.1            # ForwardTester._REFERENCE_SIZE
        total_fee = max(0.0001 + 0.0, 1e-12)   # fixed priority fee, no bribe
        base_delay = ref_fee / (ref_fee + total_fee)
        size_penalty = 1.0 + math.log10(max(1.0, self.buy_size_sol / ref_size))
        slippage_factor = 1.0 + self.engine_entry_slippage_pct / 100.0
        frac = base_delay * size_penalty * slippage_factor
        return max(0.02, min(0.98, frac))

    def _try_execute_pending(self, ref_price: float) -> dict:
        """Attempt to execute armed pending signals (ForwardTester Step-1 analogue).

        Called (a) immediately after a signal is detected and (b) at the start
        of every subsequent candle boundary.  On success the flags are cleared;
        when execution is transiently blocked the signal stays ARMED and is
        retried on the next boundary — the ForwardTester never discards a
        queued signal, and neither must live trading.

        ENGINE NOTIFICATION happens in _process_completed_candle (interleaved
        at the exact backtest fill point); this method only does app-level
        bookkeeping (LiveTrade creation, status changes) and swap dispatch.

        Returns {"action": "buy"|"exit"|None, "opened_trade": ..., "swap_request": ...}.

        ``ref_price`` is the first tick price of the newly-started candle —
        identical to the open of the execution candle the backtester fills at.
        For state-4 signals it is the engine's entry reference basis:
        ``ref_price × (1 + engine_entry_slippage_pct/100)``.
        """
        out = {"action": None, "opened_trade": None, "swap_request": None}

        # ── EXIT first (risk side wins; matches ForwardTester ordering) ────
        if self._pending_exit:
            ct = self.current_trade
            if ct is None:
                # Position already gone (failed-buy rollback / manual close /
                # watchdog finalise) — nothing left to exit.  The engine was
                # already notified closed during the expansion.
                self._pending_exit = False
                self._pending_exit_reason = ""
            elif getattr(ct, "status", "") == "closing" or ct.tx_hash_sell:
                # A sell for this trade was already dispatched elsewhere
                # (e.g. the immediate holder-flow exit in main.py).  Dedupe —
                # never double-sell.
                self._pending_exit = False
                self._pending_exit_reason = ""
            elif self._is_buy_pending():
                # Buy TX not yet resolved: hold the exit armed until the
                # settle loop confirms it open (→ sell next boundary) or
                # proves it dead (→ current_trade=None → dropped above).
                pass
            elif self._swap_in_flight:
                # Another swap is mid-build/broadcast.  Stay armed; retry on
                # the next boundary instead of dropping the exit.
                pass
            else:
                exit_reason = self._pending_exit_reason or "exit_signal"
                ct.status = "closing"
                ct.exit_reason = exit_reason
                out["action"] = "exit"
                asyncio.ensure_future(self.execute_sell(exit_reason))
                out["swap_request"] = {
                    "action": "sell",
                    "token": self.token_mint,
                    "reason": exit_reason,
                    "price": self._last_price,
                }
                self._pending_exit = False
                self._pending_exit_reason = ""

        if out["action"] or self.current_trade is not None:
            return out  # cannot buy while a position exists / just exited

        # ── BUY ───────────────────────────────────────────────────────────────
        if self._pending_buy:
            if self.mcap_stop_triggered or self.no_motion_stop_triggered:
                # Session is shutting down — entries are permanently blocked.
                self._pending_buy = False
                self._pending_buy_reason = ""
                self._pending_buy_ref = None
                self._pending_buy_ts = 0
            elif self._swap_in_flight:
                pass  # stay armed; retry next boundary
            elif time.time() < self._buy_failed_until:
                # A previous buy failed — NO further automatic buys until the
                # re-entry block window elapses.  A failed buy is never
                # retried; the engine re-emitting BUY with the same broken
                # conditions must not become an implicit retry.  Stay armed
                # until the window elapses (logged once below).
                if not self._buy_fail_block_announced:
                    self._buy_fail_block_announced = True
                    remaining = max(0.0, self._buy_failed_until - time.time())
                    logger.info(
                        f"[BUY BLOCK] BUY signal suppressed — re-entry block "
                        f"active for another {remaining:.0f}s (after failed buy)"
                    )
            else:
                buy_reason = self._pending_buy_reason
                slip_mult = 1.0 + self.engine_entry_slippage_pct / 100.0
                if not self.engine.in_position:
                    # Engine not yet notified: state-4 signal (fill point =
                    # next candle's open) or a mid-candle signal deferred
                    # while a previous sell was unsettled.  Notify NOW —
                    # still before the next expansion, i.e. the identical
                    # engine-visible point as the backtester's fill.
                    if self._pending_buy_ref is not None:
                        exec_ref = float(self._pending_buy_ref)
                    else:
                        base = float(ref_price) if ref_price > 0 else float(self._last_price or 0.0)
                        exec_ref = base * slip_mult
                    self.engine.notify_trade_opened(exec_ref, Direction.UP)
                else:
                    # Already notified mid-expansion with the reconstructed
                    # backtest fill reference.
                    exec_ref = float(self.engine.entry_price or 0.0)
                trade = LiveTrade(
                    token_mint=self.token_mint,
                    entry_time=int(self._pending_buy_ts or time.time()),
                    entry_price=exec_ref,
                    size_sol=self.buy_size_sol,
                    size_tokens=0,
                    entry_reason=buy_reason,
                    status="pending",
                )
                self.current_trade = trade
                out["opened_trade"] = trade
                out["action"] = "buy"
                self._last_motion_ts = time.time()  # reset no-motion clock at position open
                # If the buy later fails, _fail_buy_flat() rolls back with
                # notify_trade_closed().
                asyncio.ensure_future(self.execute_buy(buy_reason))
                out["swap_request"] = {
                    "action": "buy",
                    "token": self.token_mint,
                    "amount_sol": self.buy_size_sol,
                    "reason": buy_reason,
                    "price": exec_ref,
                }
                self._pending_buy = False
                self._pending_buy_reason = ""
                self._pending_buy_ref = None
                self._pending_buy_ts = 0

        return out

    def check_immediate_holder_flow_exit(self) -> Optional[str]:
        """Check if any newly appended holder-flow events trigger an immediate exit
        under V2 exit rules (e.g. dev_sell_exit) while we are in a position,
        even if no trade tick has arrived yet."""
        if (not self.current_trade or self._swap_in_flight or self._pending_exit
                or self._is_buy_pending()):
            return None

        # Only V2 adapter has this exit logic
        if not hasattr(self.engine, "_v2_holder_flow_exit_enable") or not hasattr(self.engine, "_has_recent_dev_sell"):
            return None

        if self.engine._v2_holder_flow_exit_enable <= 0.0:
            return None

        now_ts = int(time.time())
        last_candle_time = getattr(self.engine, "_current_time", 0)
        reference_time = max(now_ts, last_candle_time)

        if self.engine._has_recent_dev_sell(
            reference_time,
            self.engine._v2_holder_flow_exit_window_seconds,
            self.engine._v2_holder_flow_min_usd
        ):
            sell_event = self.engine._get_recent_dev_sell(
                reference_time, self.engine._v2_holder_flow_exit_window_seconds
            )
            wallet_prefix = sell_event.get("wallet", "")[:8] if sell_event else "unknown"
            return f"dev_sell_exit:{wallet_prefix}"
        return None

    # ── Strategy update loop ──────────────────────────────────────────────────

    def update_historical_candle(
        self,
        time_val: int,
        o: float, h: float, l: float, c: float,
        volume: float = 0.0,
        pool_sol: float = 0.0,
    ) -> dict:
        """
        Warm up the engine with a historical candle using the same 4-state
        expansion + pending model as the backtester.  No real swaps are executed.
        Returns the strategy result dict for the candle.
        """
        self._last_price = c
        result = self._process_completed_candle(time_val, o, h, l, c, volume,
                                                 pool_sol=pool_sol,
                                                 engine_exec=False)
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
        market_cap_usd: float = 0.0,
        pool_sol: float = 0.0,
        boundary_open_price: float = 0.0,
    ) -> dict:
        """
        Process one live tick through the candle-buffering + pending-signal pipeline.

        Indicator evolution is IDENTICAL to the backtester:
          - Completed candles are expanded into 4 sub-states.
          - engine.notify_trade_opened/closed() is called IMMEDIATELY when a
            signal is detected (matching the backtester's synchronous
            _open_long/_close_long → notify at Step 1 of the next candle).
            If a buy fails on-chain, _fail_buy_flat() rolls back with
            notify_trade_closed().

        Live swaps fire IMMEDIATELY (no N+1 bar wait):
          - When a BUY/EXIT signal is detected, the asyncio swap task is fired
            at once and the engine is notified synchronously.

        Parity parameters:
          boundary_open_price — the incoming trade's price on an is_new
            boundary, i.e. the first tick of the newly-started candle == the
            open of the execution candle the backtester fills at.  Used as the
            engine entry reference (× entry slip) so live exit thresholds
            reference the same basis as backtested ones.
        """
        self._last_price = c
        trade_action = None
        opened_trade = None
        swap_request = None

        # ── Candle boundary: process the just-completed candle ────────────────
        if is_new and self._current_accumulating is not None:
            prev = self._current_accumulating

            # Step 0 — retry signals armed on an earlier boundary whose swap
            # could not be dispatched then (another swap in flight, buy TX
            # still settling).  ForwardTester executes queued signals at the
            # start of the next candle no matter what; dropping them used to
            # make live hold positions the backtest had already exited.
            exec0 = self._try_execute_pending(boundary_open_price)
            if exec0["action"]:
                trade_action = exec0["action"]
                opened_trade = exec0["opened_trade"]
                swap_request = exec0["swap_request"]

            # Step 1 — _process_completed_candle mirrors ForwardTester.update():
            #   run 4 sub-states through engine.update(), queue any newly
            #   detected signal as pending.
            result = self._process_completed_candle(
                prev["t"], prev["o"], prev["h"], prev["l"], prev["c"], prev["vol"],
                buy_vol=prev.get("buy_vol", 0.0),
                sell_vol=prev.get("sell_vol", 0.0),
                market_cap_usd=prev.get("market_cap_usd", 0.0),
                pool_sol=prev.get("pool_sol", 0.0),
            )
            self._last_engine_result = result

            # Step 2 — act on the signal freshly detected in this expansion.
            # The live swap launches immediately (no N+1 bar wait); if it is
            # blocked the signal stays armed and Step 0 retries it on the
            # next boundary instead of silently dropping it.
            exec1 = self._try_execute_pending(boundary_open_price)
            if exec1["action"]:
                trade_action = exec1["action"]
                opened_trade = exec1["opened_trade"]
                swap_request = exec1["swap_request"]

        # ── Always buffer the current tick ────────────────────────────────────
        self._current_accumulating = {
            "t": time_val, "o": o, "h": h, "l": l, "c": c, "vol": volume,
            "buy_vol": buy_volume, "sell_vol": sell_volume,
            "market_cap_usd": market_cap_usd,
            "pool_sol": pool_sol,
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
        # Cost basis = the nominal BUY SIZE (the SOL committed to this trade).
        # The on-chain-measured spend (trade.cost_sol) is deliberately NOT used
        # as the basis: that wallet-delta measurement inherits stale cached-
        # balance error and has recorded inflated costs (e.g. 0.0121 SOL for a
        # 0.01 SOL buy), which reported profitable trades as deep losses.
        # PnL is always presented as: pnl = SOL received on the sell − buy size.
        basis = trade.size_sol if trade.size_sol > 0 else trade.cost_sol
        # Adopted-orphan / zero-cost-basis trades carry size_sol=0.0 — guard
        # the division and treat the entire proceeds as the PnL (there is no
        # known entry cost to subtract).
        if basis > 0:
            trade.pnl_sol = sol_received - basis
            trade.pnl_pct = (trade.pnl_sol / basis) * 100
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
        if trade.entry_reason == "watchdog_adopted_orphan":
            # A later delayed buy may land after this adopted bag is sold —
            # re-allow orphan adoption so the next bag is never invisible.
            self._adopted_bag = False
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
