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

# SPL Token / ATA program ids (used for deterministic ATA derivation and
# Token-2022 detection without pulling in the heavyweight `spl-token` package).
SPL_TOKEN_PROGRAM_ID  = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
TOKEN_2022_PROGRAM_ID = Pubkey.from_string("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
ATA_PROGRAM_ID        = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

logger = logging.getLogger("live-trader")

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
SOLANA_RPCS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
]
SOLANA_RPC_PRIMARY = SOLANA_RPCS[0]

# Confirmation polling and retry tuning
# Hot path: confirmation polled at ~standby-tick cadence; rebroadcast fires
# every 1.0s so a slow slot leader gets retried within the 12s window.
CONFIRM_TIMEOUT_S:  float = 12.0   # per-attempt confirm window (short — we retry w/ fresh blockhash)
CONFIRM_POLL_MS:    float = 0.3    # poll cadence while waiting for confirmation
CONFIRM_REBROADCAST_S: float = 1.0 # re-broadcast same signed TX every N s while confirming

# ── RETRY / ESCALATION POLICY ────────────────────────────────────────────────
# "Give up after 5 tries" was the root cause of unsold positions riding to zero.
# We never give up within a logical swap call anymore — we keep cycling
# (fresh quote → fresh TX → broadcast → confirm) until either the swap lands or
# the caller aborts.  Sells are especially aggressive because a missed sell on a
# memecoin can mean riding it to zero.
QUOTE_RETRIES_PER_GROUP = 3          # fresh quotes fetched per attempt group
NONSIMULATION_ABORT_CODES = frozenset({6024, 1, 0x1771})  # swallowed — handled by re-quote
PRIORITY_FEE_ESCALATION  = [0, 1_000_000, 3_000_000, 8_000_000]  # micro-lamports by attempt group
MAX_PRIORITY_FEE         = 15_000_000

# Watchdog: if the on-chain position hasn't reached zero after a confirmed sell
# signal within this many seconds, force another sell pass.
WATCHDOG_INTERVAL_S: float = 6.0
WATCHDOG_TIMEOUT_S:  float = 45.0


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

    def __init__(
        self,
        token_mint: str,
        keypair: Keypair,
        buy_size_sol: float = 0.01,
        slippage_bps: int = 1500,
        priority_fee_lamports: int = 500_000,
        min_market_cap_usd: float = 6_000.0,
        engine_kwargs: Optional[dict] = None,

        # Skip on-chain simulation on the hot path (saves ~300 ms per swap).
        # Simulation is still run on explicit test buys if desired.
        skip_simulation: bool = True,
        engine_version: int = 1,
    ):
        if engine_kwargs is None:
            engine_kwargs = {}

        self.engine = create_engine(engine_version, **engine_kwargs)
        self.token_mint = token_mint
        self.keypair = keypair
        self.wallet_pubkey = str(keypair.pubkey())
        self.buy_size_sol = buy_size_sol
        self.slippage_bps = slippage_bps
        self.priority_fee_lamports = priority_fee_lamports
        self.skip_simulation = skip_simulation

        # ── Market-cap safety floor ───────────────────────────────────────
        # If the live market cap (USD) drops below this value while a
        # position is open, an emergency sell is triggered and the session
        # is flagged for shutdown by main.py.
        self.min_market_cap_usd: float = min_market_cap_usd
        self._last_market_cap_usd: float = 0.0
        self.mcap_stop_triggered: bool = False   # set True once triggered

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

        # ── Hot-path RPC affinity ──────────────────────────────────────────
        # Free public RPCs vary wildly in latency. Track the last RPC that
        # successfully served a balance/confirm read and try it FIRST next
        # time — empirically cuts "first-wins" wait time in half. Falls back
        # to the full fanout if that RPC ever fails or stalls.
        self._fast_rpc_idx: int = 0

    def start_watchdog(self):
        """Spawn `_monitor_trade` exactly once per session (called by main.py)."""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.ensure_future(self._monitor_trade())
            logger.info("[WATCHDOG] Task started")

    async def close(self):
        """Stop the watchdog and close the HTTP session."""
        self._alive = False
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

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

        Returns:
            {"confirmed": bool, "error": str | None, "slot": int | None}
        """
        logger.info(f"[CONFIRM] Waiting for confirmation of {sig[:16]}… (max {timeout_s}s)")
        start = time.time()
        s = await self._get_session()
        last_broadcast = 0.0

        while time.time() - start < timeout_s:
            # ── Aggressive rebroadcast of the same signed TX while confirming ──
            if signed_b64 and (time.time() - last_broadcast) >= CONFIRM_REBROADCAST_S:
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

    async def _sign_and_send(self, swap_tx_b64: str, wait_for_confirmation: bool = False) -> Optional[str]:
        """
        Sign the base64-encoded versioned transaction and broadcast it
        to ALL RPCs simultaneously.

        Hot path:
          - skip_simulation=True  → no simulate call (saves ~300 ms)
          - Multi-RPC fanout for maximum landing probability
          - If wait_for_confirmation is True, await confirmation before returning.
            Otherwise, dispatch confirmation as a fire-and-forget background task.
        """
        try:
            raw_tx = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(raw_tx)

            # Sign with our keypair
            signed_tx = VersionedTransaction(tx.message, [self.keypair])
            signed_bytes = bytes(signed_tx)
            signed_b64 = base64.b64encode(signed_bytes).decode()

            logger.info(f"[SIGN] Transaction signed ({len(signed_bytes)} bytes)")

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
                asyncio.ensure_future(self._background_confirm(sig, signed_b64))

            return sig

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

    async def _get_token_balance(self) -> int:
        """
        Authoritative on-chain token balance (raw smallest units).

        Hot-path rewrite: tries the cached-fast RPC first with a short
        1.2 s timeout, then falls back to a parallel fanout across all
        remaining RPCs (each capped at 1.2 s). First non-zero account
        amount wins. Auto-detects Token vs Token-2022 program ownership
        from the parsed account info.
        """
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                self.wallet_pubkey,
                {"mint": self.token_mint},
                {"encoding": "jsonParsed"},
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
        Full buy cycle with grouped-retry escalation.

        Strategy:
          - Outer loop: unlimited "attempt groups" (previously the code bailed
            after 5 total tries — the leading cause of missed entries).
          - Inner loop: QUOTE_RETRIES_PER_GROUP fresh quotes, each sent with an
            escalating priority fee taken from PRIORITY_FEE_ESCALATION.
          - Mid-retry balance verification: between groups, re-check whether
            tokens ALREADY arrived on-chain (a previous TX may have confirmed
            silently while we were polling another).
          - `_swap_in_flight` is held for the entire duration so the 1s tick
            can't queue a duplicate buy.
        """
        if self._swap_in_flight:
            logger.warning("[BUY] Swap already in flight — skipping")
            return None
        self._swap_in_flight = True
        buy_start = time.time()
        attempt_group = 0
        try:
            amount_lam = int(self.buy_size_sol * 1e9)
            mint_str = str(self.token_mint)

            sol_bal = await self._get_sol_balance()
            logger.info(
                f"[BUY] Starting buy: mint={mint_str[:8]}… size={self.buy_size_sol} SOL "
                f"balance={sol_bal:.4f} SOL reason={reason}"
            )
            if sol_bal * 1e9 < amount_lam + 50_000:  # buy size + gas buffer
                logger.error(f"[BUY FAILED] Insufficient balance: {sol_bal:.4f} SOL but need ~{self.buy_size_sol} SOL")
                await self._broadcast_status(
                    "buy_failed", f"Insufficient SOL ({sol_bal:.4f} available)", reason
                )
                return None

            while True:  # grouped-retry — never give up mid-call
                attempt_group += 1
                fee = PRIORITY_FEE_ESCALATION[
                    min(attempt_group - 1, len(PRIORITY_FEE_ESCALATION) - 1)
                ]
                logger.info(
                    f"[BUY] Attempt group {attempt_group} "
                    f"(fee={fee} micro-lamports)"
                )

                for inner in range(1, QUOTE_RETRIES_PER_GROUP + 1):
                    label = f"G{attempt_group}/Q{inner}"

                    quote = await self._get_quote(WSOL_MINT, self.token_mint, amount_lam)
                    if not quote:
                        logger.error(f"[BUY FAILED {label}] Jupiter quote failed")
                        await asyncio.sleep(0.4)
                        continue

                    swap_tx = await self._get_swap_tx(quote, priority_fee_override=fee)
                    if not swap_tx:
                        logger.error(f"[BUY FAILED {label}] Swap TX build failed")
                        await asyncio.sleep(0.4)
                        continue

                    sig = await self._sign_and_send(swap_tx, wait_for_confirmation=True)
                    if not sig:
                        logger.warning(f"[BUY FAILED {label}] TX not confirmed — will retry with fresh quote")
                        await asyncio.sleep(0.3)
                        continue

                    # ── Success path ─────────────────────────────────────────
                    elapsed = time.time() - buy_start
                    out_amount = int(quote.get("outAmount", 0))
                    quote_tokens = out_amount / (10 ** self._token_decimals)

                    # Authoritative on-chain balance — but DON'T block the
                    # buy return path on it.  Jupiter's confirmed outAmount
                    # is what actually landed; the prior 3× poll (0.4 s
                    # apart × 3) added ~5–12 s per buy when the chosen RPC
                    # was rate-limited.  Run a single async verification
                    # in the background; if it disagrees, we log only.
                    tokens = quote_tokens
                    asyncio.ensure_future(self._verify_buy_settled(sig, out_amount))

                    ct = self.current_trade
                    if ct:
                        ct.tx_hash_buy = sig
                        ct.size_tokens = tokens
                        ct.status = "open"
                    self._token_balance = out_amount
                    # Refresh SOL balance without blocking (used for stats).
                    asyncio.ensure_future(self._refresh_sol_balance_async())
                    self._last_exit_signal_ts = 0.0  # reset watchdog

                    await self._broadcast_status("buy_confirmed", sig, reason, tokens=tokens)
                    logger.info(
                        f"[BUY OK] sig={sig} tokens={tokens:.4f} elapsed={elapsed:.1f}s "
                        f"group={attempt_group}"
                    )
                    logger.info(f"[BUY OK] https://solscan.io/tx/{sig}")
                    return sig

                # ── Between groups: maybe tokens already arrived ──────────────
                bal = await self._get_token_balance()
                if bal > 0:
                    logger.info(
                        f"[BUY VERIFIED] Tokens detected on-chain mid-retry "
                        f"({bal} raw units) — treating buy as successful."
                    )
                    tokens = bal / (10 ** self._token_decimals)
                    ct = self.current_trade
                    if ct:
                        ct.size_tokens = tokens
                        ct.status = "open"
                    self._token_balance = bal
                    self.stats.starting_balance = await self._get_sol_balance()
                    self._last_exit_signal_ts = 0.0
                    await self._broadcast_status("buy_confirmed", "", reason, tokens=tokens)
                    return "verified_on_chain"

                # Escalating back-off between groups (0.5 s → 1 s → 2 s, capped)
                backoff = min(0.5 * (2 ** (attempt_group - 1)), 2.0)
                logger.info(f"[BUY] Group {attempt_group} exhausted; sleeping {backoff:.1f}s before retry")
                await asyncio.sleep(backoff)

        except Exception as e:
            logger.error(f"[BUY ERROR] Unexpected error: {e}", exc_info=True)
            await self._broadcast_status("buy_failed", f"Unexpected: {e}", reason)
            return None
        finally:
            self._swap_in_flight = False

    async def _verify_buy_settled(self, sig: str, expected_amount: int):
        """Background post-buy balance check. Non-blocking — only corrects
        ``_token_balance`` if the on-chain figure differs from the Jupiter
        outAmount. A small settle delay lets the RPC's processed commitment
        catch up so we don't fan a false alarm."""
        await asyncio.sleep(0.4)
        settled = await self._get_token_balance()
        if settled > 0 and settled != expected_amount:
            logger.info(
                f"[BUY VERIFY] On-chain balance {settled} differs from Jupiter "
                f"outAmount {expected_amount} — adopting on-chain figure."
            )
            self._token_balance = settled
            ct = self.current_trade
            if ct and ct.status == "open":
                ct.size_tokens = settled / (10 ** self._token_decimals)
        elif settled == 0:
            # The swap confirmed via Solana's getSignatureStatuses; this
            # is just bandwidth-limited processed-commitment read lag.
            logger.debug(
                f"[BUY VERIFY] Token balance query lagged (sig={sig[:8]}…); "
                f"trusting Jupiter outAmount."
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
        """
        await asyncio.sleep(0.6)  # let processed commitment catch up
        post_balance = await self._get_token_balance()
        if post_balance > 0:
            logger.warning(
                f"[SELL PARTIAL] {post_balance} tokens still on-chain after "
                f"confirmed sell sig={sig[:8]}… — re-arming watchdog to retry."
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

            logger.info(
                f"[SELL] Starting sell: mint={mint_str[:8]}… reason={reason} "
                f"cached_balance={self._token_balance} units"
            )
            # Parallel fetch of fresh token balance — doesn't block on the
            # hot path because we fall through to the cached figure if the
            # RPC lags. We DO still want a fresh figure on the first attempt
            # because the buy's outAmount may not have settled by the time
            # we sell (manual sells happen ~10 s after a buy).
            fresh_bal = await self._get_token_balance()
            token_balance = fresh_bal if fresh_bal > 0 else self._token_balance

            if token_balance <= 0:
                logger.warning(f"[SELL FAILED] No token balance to sell for {mint_str[:8]}…")
                await self._broadcast_status("sell_failed", "No token balance", reason)
                return None

            logger.info(
                f"[SELL] Live balance: {token_balance} units (fresh={fresh_bal > 0})"
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
                        logger.error(f"[SELL FAILED {label}] Jupiter quote failed")
                        await asyncio.sleep(0.4)
                        continue

                    swap_tx = await self._get_swap_tx(quote, priority_fee_override=fee)
                    if not swap_tx:
                        logger.error(f"[SELL FAILED {label}] Swap TX build failed")
                        await asyncio.sleep(0.4)
                        continue

                    sig = await self._sign_and_send(swap_tx, wait_for_confirmation=True)
                    if not sig:
                        logger.warning(f"[SELL FAILED {label}] TX not confirmed — retrying with fresh quote")
                        await asyncio.sleep(0.3)
                        continue

                    # ── Success ──────────────────────────────────────────────
                    elapsed = time.time() - sell_start
                    sol_received = int(quote.get("outAmount", 0)) / 1e9

                    # Finalise the trade immediately — the swap is on-chain
                    # confirmed. Post-confirm balance verification is run in
                    # the background: if tokens remain, the watchdog will
                    # schedule another sell pass. This shaves 4–10 s off
                    # the sell_return leg when the read RPC is rate-limited.
                    self._token_balance = 0
                    self._last_exit_signal_ts = 0.0  # clear watchdog on the happy path
                    closed_trade = None
                    if self.current_trade:
                        closed_trade = self.confirm_sell(sig, sol_received, self._last_price)

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
                    return sig

                # ── Group exhausted ──────────────────────────────────────────
                bal = await self._get_token_balance()
                if bal == 0:
                    logger.info(
                        "[SELL VERIFIED] Wallet is now empty — exiting sell loop cleanly."
                    )
                    self._token_balance = 0
                    self._last_exit_signal_ts = 0.0
                    if self.current_trade:
                        self.confirm_sell("", 0.0, self._last_price)
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

                backoff = min(0.5 * (2 ** (attempt_group - 1)), 2.0)
                logger.info(
                    f"[SELL] Group {attempt_group} exhausted; retry in {backoff:.1f}s "
                    f"(still holding {bal} raw tokens)"
                )
                await asyncio.sleep(backoff)

        except Exception as e:
            logger.error(f"[SELL ERROR] Unexpected error: {e}", exc_info=True)
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

                if self.current_trade is None or self._swap_in_flight:
                    continue

                if self._last_exit_signal_ts <= 0:
                    continue  # no exit signal seen yet — nothing to do

                time_since_exit = time.time() - self._last_exit_signal_ts
                if time_since_exit < WATCHDOG_TIMEOUT_S:
                    continue  # normal sell retry may still be running

                on_chain = await self._get_token_balance()
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
                    logger.info(
                        f"[WATCHDOG] On-chain balance = 0 but current_trade still "
                        f"open — finalising trade locally."
                    )
                    self.confirm_sell("watchdog_finalise", 0.0, self._last_price)
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

        Returns True if the mcap floor was breached and the session should
        be stopped by the caller (main.py cancels the WebSocket loop).

        Behaviour:
          - If mcap ≥ floor  → nothing happens.
          - If mcap < floor and NO open position → block new entries by
            setting mcap_stop_triggered; broadcast warning.
          - If mcap < floor and position IS open → emergency sell first,
            then set mcap_stop_triggered; broadcast stop event.
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

        if self.current_trade is not None and not self._swap_in_flight:
            logger.warning("[MCAP STOP] Position open — triggering emergency sell")
            self.current_trade.status = "closing"
            self.current_trade.exit_reason = "mcap_floor_stop"
            asyncio.ensure_future(self.execute_sell("mcap_floor_stop"))

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
        if self._pending_buy and self.current_trade is not None:
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

            if self._pending_buy and self.current_trade is None and not self._swap_in_flight and not self.mcap_stop_triggered:
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
        if self.current_trade is not None:
            logger.warning("[MANUAL BUY] Already in a trade — ignoring")
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
        self.engine.notify_trade_opened(c, Direction.UP)
        sig = await self.execute_buy("manual_test_buy")
        if sig is None:
            # Buy failed — clean up so future trades aren't blocked
            logger.warning("[MANUAL BUY] Buy failed — resetting trade state")
            self.current_trade = None
            self.engine.notify_trade_closed()
        return sig

    async def force_sell(self) -> Optional[str]:
        """Manually trigger a test sell from the dashboard."""
        if self.current_trade is None:
            logger.warning("[MANUAL SELL] No position open — ignoring")
            return None
        logger.info("[MANUAL SELL] Initiating manual sell")
        self.current_trade.status = "closing"
        self.current_trade.exit_reason = "manual_test_sell"
        sig = await self.execute_sell("manual_test_sell")
        if sig is None and self.current_trade is not None:
            # Sell failed — revert status so position isn't stuck
            logger.warning("[MANUAL SELL] Sell failed — reverting trade status to open")
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
        trade.pnl_sol = sol_received - trade.size_sol
        if trade.entry_price > 0:
            trade.pnl_pct = (trade.pnl_sol / trade.size_sol) * 100
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
        return trade

    def confirm_failed(self, action: str, error: str):
        logger.error(f"Swap FAILED ({action}): {error}")
        if action == "buy" and self.current_trade is not None:
            self.current_trade = None
            self.engine.notify_trade_closed()
        elif action == "sell" and self.current_trade is not None:
            self.current_trade.status = "open"
