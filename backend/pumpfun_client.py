import asyncio
import base64
import json
import logging
import time
from typing import AsyncGenerator, Optional
from collections import defaultdict

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# ── Global Solana RPC rate limiter ────────────────────────────────────────────
# All PumpSwapRPCClient instances share this semaphore so that at most
# _RPC_CONCURRENCY requests hit api.mainnet-beta.solana.com at the same time.
# This prevents 429 errors when monitoring 5+ coins simultaneously.
_RPC_CONCURRENCY = 3          # max parallel RPC calls across ALL coins
_RPC_MIN_INTERVAL = 0.4       # minimum seconds between any two RPC calls
_rpc_semaphore: Optional[asyncio.Semaphore] = None
_rpc_last_call_ts: float = 0.0
_rpc_lock: Optional[asyncio.Lock] = None


def _get_rpc_semaphore() -> asyncio.Semaphore:
    global _rpc_semaphore
    if _rpc_semaphore is None:
        _rpc_semaphore = asyncio.Semaphore(_RPC_CONCURRENCY)
    return _rpc_semaphore


def _get_rpc_lock() -> asyncio.Lock:
    global _rpc_lock
    if _rpc_lock is None:
        _rpc_lock = asyncio.Lock()
    return _rpc_lock

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
PUMP_API_V3   = "https://frontend-api-v3.pump.fun"
DEXSCREENER   = "https://api.dexscreener.com"
# HTTP RPC moved to publicnode (mainnet-beta is too rate-limited for the
# 2-call _load_pool sequence that fires on every PumpSwapRPCClient connect).
SOLANA_RPC_HTTP = "https://solana-rpc.publicnode.com"
# WS stays on mainnet-beta — websocket accountSubscribe doesn't count toward
# the same per-IP HTTP rate budget, and mainnet-beta's WS is the most reliable.
SOLANA_RPC_WS   = "wss://api.mainnet-beta.solana.com"
WSOL_MINT = "So11111111111111111111111111111111111111112"

# Maps app timeframe key → pump.fun API "timeframe" param (minutes).
# Sub-minute TFs have no server-side candlestick history; map to 1 m so we still
# seed the aggregator with the most-recent close price.
TIMEFRAME_MAP: dict[str, int] = {
    "1s": 1, "5s": 1, "15s": 1,
    "1m": 1, "5m": 5, "15m": 15, "1h": 60,
}

# Timeframes that have no REST candle history
SUB_MINUTE_TFS = {"1s", "5s", "15s"}

_HDR_PUMP = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Origin": "https://pump.fun",
    "Referer": "https://pump.fun/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

_HDR_DS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


# ---------- DexScreener helpers -----------------------------------------------

async def _ds_search(session: aiohttp.ClientSession, query: str) -> Optional[dict]:
    """Return the best Solana pair from a DexScreener search."""
    try:
        async with session.get(
            f"{DEXSCREENER}/latest/dex/search",
            params={"q": query},
            headers=_HDR_DS,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
            pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
            if not pairs:
                return None
            q_lower = query.lower()
            for p in pairs:
                if (p.get("pairAddress", "").lower() == q_lower or
                        p.get("baseToken", {}).get("address", "").lower() == q_lower):
                    return p
            return pairs[0]
    except Exception as e:
        logger.debug(f"_ds_search: {e}")
        return None

async def _ds_token(session: aiohttp.ClientSession, mint: str) -> Optional[dict]:
    """Return an exact Solana pair by token mint address."""
    try:
        async with session.get(
            f"{DEXSCREENER}/latest/dex/tokens/{mint}",
            headers=_HDR_DS,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
            pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
            if not pairs:
                return None
            return pairs[0]
    except Exception as e:
        logger.debug(f"_ds_token: {e}")
        return None


async def _ds_pair(session: aiohttp.ClientSession, pair_address: str) -> Optional[dict]:
    """Return an exact Solana pair by pair address."""
    try:
        async with session.get(
            f"{DEXSCREENER}/latest/dex/pairs/solana/{pair_address}",
            headers=_HDR_DS,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json(content_type=None)
            pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
            if not pairs:
                return None
            q_lower = pair_address.lower()
            for p in pairs:
                if p.get("pairAddress", "").lower() == q_lower:
                    return p
            return pairs[0]
    except Exception as e:
        logger.debug(f"_ds_pair: {e}")
        return None


def _info_from_ds(pair: dict) -> dict:
    """Build a normalised token-info dict from a DexScreener pair."""
    base = pair.get("baseToken", {})
    ib = pair.get("info", {})
    socials = {s["type"]: s["url"] for s in ib.get("socials", []) if "type" in s}
    sites = [w.get("url", "") for w in ib.get("websites", []) if w.get("url")]
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    liq_quote = pair.get("liquidity", {}).get("quote", 0)
    return {
        "mint":                base.get("address", ""),
        "pair_address":        pair.get("pairAddress", ""),
        "name":                base.get("name", ""),
        "symbol":              base.get("symbol", ""),
        "image_uri":           ib.get("imageUrl", ""),
        "description":         "",
        "twitter":             socials.get("twitter", ""),
        "telegram":            socials.get("telegram", ""),
        "website":             sites[0] if sites else "",
        "usd_market_cap":      mcap,
        "price_usd":           pair.get("priceUsd", ""),
        "price_sol":           pair.get("priceNative", ""),
        "virtual_sol_reserves": liq_quote * 1e9,
        "_source":             "dexscreener",
    }


def _is_migrated_coin(info: dict) -> bool:
    """Best-effort migration check from pump.fun v3 token payload."""
    if not isinstance(info, dict):
        return False

    explicit_flags = (
        "complete",
        "is_complete",
        "isComplete",
        "bonding_curve_complete",
        "bondingCurveComplete",
        "migrated",
        "is_migrated",
        "isMigrated",
        "graduated",
        "is_graduated",
        "isGraduated",
    )
    for key in explicit_flags:
        if key in info:
            return bool(info.get(key))

    # Some v3 payloads expose only a pool field when migrated.
    pool_keys = (
        "raydium_pool",
        "raydiumPool",
        "amm_pool",
        "ammPool",
        "pool_address",
        "poolAddress",
    )
    for key in pool_keys:
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return True

    return False


def _u64_le(buf: bytes, offset: int) -> int:
    return int.from_bytes(buf[offset:offset + 8], "little")


def _pubkey_str(buf: bytes, offset: int) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = int.from_bytes(buf[offset:offset + 32], "big")
    if num == 0:
        return "1" * 32
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = alphabet[rem] + out
    leading_zeros = 0
    for b in buf[offset:offset + 32]:
        if b == 0:
            leading_zeros += 1
        else:
            break
    return ("1" * leading_zeros) + out


def _decode_pool_account(data: bytes) -> dict:
    """Decode PumpSwap Pool account fields needed for pricing."""
    # 8 discriminator + u8 + u16 + 5 pubkeys + u64 + pubkey + bool + optionBool...
    return {
        "pool_bump": data[8],
        "index": int.from_bytes(data[9:11], "little"),
        "creator": _pubkey_str(data, 11),
        "base_mint": _pubkey_str(data, 43),
        "quote_mint": _pubkey_str(data, 75),
        "lp_mint": _pubkey_str(data, 107),
        "pool_base_token_account": _pubkey_str(data, 139),
        "pool_quote_token_account": _pubkey_str(data, 171),
        "lp_supply": _u64_le(data, 203),
        "coin_creator": _pubkey_str(data, 211),
    }


def _decode_spl_token_amount(data: bytes) -> int:
    return _u64_le(data, 64)


def _decode_mint_decimals(data: bytes) -> int:
    return data[44]


def _decode_mint_supply(data: bytes) -> int:
    return _u64_le(data, 36)


# ---------- Pump.fun v3 helpers -----------------------------------------------

async def _v3_coin(session: aiohttp.ClientSession, mint: str) -> Optional[dict]:
    """Fetch token info from pump.fun v3. Returns None if absent/empty."""
    try:
        async with session.get(
            f"{PUMP_API_V3}/coins/{mint}",
            headers=_HDR_PUMP,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as r:
            if r.status != 200:
                return None
            text = await r.text()
            if not text or not text.strip().startswith("{"):
                return None
            d = json.loads(text)
            if isinstance(d, dict) and d.get("mint"):
                return d
            return None
    except Exception as e:
        logger.debug(f"_v3_coin: {e}")
        return None


# ---------- Public API --------------------------------------------------------

async def resolve_input(user_input: str) -> tuple[str, Optional[dict]]:
    """
    Accept either a pump.fun token mint or a PumpSwap/Raydium pair address.
    Returns (real_token_mint, info_dict_or_None).

    Order:
      1. Try pump.fun v3 directly (fast path for bonding-curve tokens)
      2. DexScreener search to find the base-token mint from a pair address
      3. Retry v3 with resolved mint; fall back to DexScreener metadata
    """
    async with aiohttp.ClientSession() as session:
        # 1. Direct v3 lookup
        info = await _v3_coin(session, user_input)
        if info:
            migrated = _is_migrated_coin(info)
            pair = await _ds_search(session, user_input)
            if migrated and pair and pair.get("pairAddress") and pair.get("pairAddress") != user_input:
                info["pair_address"] = pair.get("pairAddress", "")
                info["usd_market_cap"] = pair.get("marketCap") or pair.get("fdv") or info.get("usd_market_cap", 0)
                info["price_usd"] = pair.get("priceUsd") or info.get("price_usd", "")
                info["price_sol"] = pair.get("priceNative") or info.get("price_sol", "")
                info["_live_source"] = "solana_rpc"
            else:
                info["_live_source"] = "pumpportal"
            logger.info(f"resolve_input: v3 hit {user_input[:8]}")
            return user_input, info

        # 2. DexScreener resolution
        pair = await _ds_search(session, user_input)
        if pair:
            base_mint = pair.get("baseToken", {}).get("address", "")
            if base_mint and base_mint != user_input:
                logger.info(f"resolve_input: {user_input[:8]} -> {base_mint[:8]} via DexScreener")
                info = await _v3_coin(session, base_mint)
                if info:
                    if _is_migrated_coin(info):
                        info["pair_address"] = pair.get("pairAddress", "")
                        info["usd_market_cap"] = pair.get("marketCap") or pair.get("fdv") or info.get("usd_market_cap", 0)
                        info["price_usd"] = pair.get("priceUsd") or info.get("price_usd", "")
                        info["price_sol"] = pair.get("priceNative") or info.get("price_sol", "")
                        info["_live_source"] = "solana_rpc"
                    else:
                        info["_live_source"] = "pumpportal"
                    return base_mint, info
                return base_mint, _info_from_ds(pair)
            # pair_address == user_input: DexScreener gave us info but couldn't
            # resolve to a different base mint. Build info from DS.
            info = _info_from_ds(pair)
            # Only use Solana RPC pool streaming if there's a known PumpSwap
            # pair address that differs from the token mint.
            pair_addr = pair.get("pairAddress", "")
            dex_id = pair.get("dexId", "").lower()
            is_pumpswap = "pump" in dex_id or "pumpswap" in dex_id
            if is_pumpswap and pair_addr and pair_addr != user_input:
                info["pair_address"] = pair_addr
                info["_live_source"] = "solana_rpc"
            else:
                # Default: poll DexScreener for price updates
                info["_live_source"] = "dexscreener"
            return user_input, info

        logger.warning(f"resolve_input: unresolved {user_input[:8]}")
        return user_input, None


async def get_token_info(mint: str) -> Optional[dict]:
    """Refresh token metadata (v3 first, then DexScreener)."""
    async with aiohttp.ClientSession() as session:
        info = await _v3_coin(session, mint)
        if info:
            return info
        pair = await _ds_search(session, mint)
        return _info_from_ds(pair) if pair else None


async def get_historical_candles(
    mint: str, timeframe: str = "1m", limit: int = 500
) -> list[dict]:
    if timeframe in SUB_MINUTE_TFS:
        return []
    params = {
        "mint": mint, "offset": 0, "limit": limit,
        "timeframe": TIMEFRAME_MAP.get(timeframe, 1),
    }
    try:
        async with aiohttp.ClientSession(headers=_HDR_PUMP) as session:
            async with session.get(
                f"{PUMP_API_V3}/candlesticks",
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                if r.status != 200:
                    logger.info(f"candlesticks: {r.status} for {mint[:8]}")
                    return []
                text = await r.text()
                if not text or not text.strip().startswith("["):
                    return []
                raw = json.loads(text)
                if not isinstance(raw, list):
                    return []
    except Exception as e:
        logger.error(f"get_historical_candles: {e}")
        return []

    candles = []
    for row in raw:
        try:
            candles.append({
                "time":   int(row.get("timestamp", row.get("time", 0))),
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": float(row.get("volume", 0)),
            })
        except (KeyError, ValueError, TypeError):
            continue
    candles.sort(key=lambda c: c["time"])
    return candles


# ---------- Live-trade WebSocket ---------------------------------------------

# ── Shared PumpPortal hub ─────────────────────────────────────────────────
# A single persistent WebSocket to pumpportal.fun is shared across ALL token
# subscriptions.  Each mint gets its own asyncio.Queue; the hub fans incoming
# messages into the correct queue(s).  This avoids the N-connections-per-IP
# rate limit that causes connection failures when many tokens run in parallel.

class _SharedPumpPortalHub:
    """Process-global multiplexed PumpPortal WebSocket hub."""

    def __init__(self):
        # mint -> set of asyncio.Queue  (multiple consumers per mint allowed)
        self._queues: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        # mints pending subscription on the next reconnect / live connection
        self._pending_subscribe: set[str] = set()
        # reference to the live websockets connection (to send subscribe msgs)
        self._ws = None

    # ── Internal helpers ──────────────────────────────────────────────────

    async def _subscribe(self, mint: str):
        """Send a subscribe message if connection is live."""
        self._pending_subscribe.add(mint)
        if self._ws is not None:
            try:
                await self._ws.send(
                    json.dumps({"method": "subscribeTokenTrade", "keys": [mint]})
                )
                self._pending_subscribe.discard(mint)
                logger.info(f"[PumpHub] +subscribe {mint[:8]}…")
            except Exception:
                pass  # will be re-sent on reconnect

    async def _unsubscribe(self, mint: str):
        """Send an unsubscribe message if the connection is live."""
        if self._ws is not None:
            try:
                await self._ws.send(
                    json.dumps({"method": "unsubscribeTokenTrade", "keys": [mint]})
                )
                logger.info(f"[PumpHub] -unsubscribe {mint[:8]}…")
            except Exception:
                pass

    def _fan_out(self, trade: dict):
        """Push a normalised trade to every queue watching that mint."""
        mint = trade.get("mint", "")
        for q in list(self._queues.get(mint, set())):
            try:
                q.put_nowait(trade)
            except asyncio.QueueFull:
                pass  # slow consumer — drop rather than back-pressure the hub

    async def _run(self):
        backoff = 0.1
        while True:
            try:
                async with websockets.connect(
                    PUMPPORTAL_WS,
                    ping_interval=20,
                    ping_timeout=15,
                    open_timeout=15,
                    max_size=2 ** 22,
                ) as ws:
                    self._ws = ws
                    backoff = 0.1
                    logger.info("[PumpHub] Connected")

                    # Subscribe all currently registered mints
                    async with self._lock:
                        all_mints = list(self._queues.keys())
                        # Also include any that were pending from before
                        all_mints += list(self._pending_subscribe)
                        all_mints = list(set(all_mints))

                    if all_mints:
                        await ws.send(
                            json.dumps({"method": "subscribeTokenTrade", "keys": all_mints})
                        )
                        self._pending_subscribe.clear()
                        logger.info(f"[PumpHub] Subscribed {len(all_mints)} mints on reconnect")

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if "txType" not in msg:
                            continue
                        trade = PumpFunWSClient._normalise(msg)
                        if trade:
                            self._fan_out(trade)

            except (ConnectionClosed, asyncio.TimeoutError, OSError) as e:
                logger.warning(f"[PumpHub] {e} — reconnecting in {backoff:.1f}s")
            except asyncio.CancelledError:
                logger.info("[PumpHub] Cancelled")
                return
            except Exception as e:
                logger.error(f"[PumpHub] Unexpected: {e}")
            finally:
                self._ws = None
            await asyncio.sleep(backoff)

    # ── Public API ────────────────────────────────────────────────────────

    def _ensure_running(self):
        """Start the background hub task if not already running."""
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_running_loop()
                self._task = loop.create_task(self._run())
            except RuntimeError:
                pass  # no event loop yet — will be started on first register

    async def register(self, mint: str) -> asyncio.Queue:
        """Register a consumer for *mint* and return its dedicated queue."""
        self._ensure_running()
        async with self._lock:
            q: asyncio.Queue = asyncio.Queue(maxsize=512)
            self._queues[mint].add(q)
            first_for_mint = len(self._queues[mint]) == 1
        if first_for_mint:
            await self._subscribe(mint)
        return q

    async def unregister(self, mint: str, q: asyncio.Queue):
        """Remove a consumer queue.  Unsubscribes from PumpPortal when last consumer leaves."""
        async with self._lock:
            self._queues[mint].discard(q)
            last_consumer = len(self._queues[mint]) == 0
            if last_consumer:
                del self._queues[mint]
        if last_consumer:
            await self._unsubscribe(mint)


# Process-global singleton
_pump_hub = _SharedPumpPortalHub()


class PumpFunWSClient:
    """Yields normalised trade dicts for a given token mint.

    Internally shares a single WebSocket connection to PumpPortal across all
    concurrent instances via _SharedPumpPortalHub — no per-instance connection
    is opened.  The public API (stream / stop) is unchanged.
    """

    def __init__(self, mint: str):
        self.mint  = mint
        self._stop = False
        self._queue: Optional[asyncio.Queue] = None

    def stop(self):
        self._stop = True
        # Wake up the stream coroutine if it's blocked on queue.get()
        if self._queue is not None:
            try:
                self._queue.put_nowait(None)  # sentinel
            except asyncio.QueueFull:
                pass

    async def stream(self) -> AsyncGenerator[dict, None]:
        q = await _pump_hub.register(self.mint)
        self._queue = q
        try:
            while not self._stop:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if item is None:          # stop() sentinel
                    break
                yield item
        finally:
            self._queue = None
            await _pump_hub.unregister(self.mint, q)

    @staticmethod
    def _normalise(msg: dict) -> Optional[dict]:
        try:
            # ── Timestamp ───────────────────────────────────────────────────
            ts_raw = msg.get("timestamp") or msg.get("tradeCreatedAt")
            if ts_raw:
                ts = float(ts_raw) / 1000.0 if float(ts_raw) > 1_000_000_000_000 else float(ts_raw)
            else:
                ts = time.time()

            # ── Spot price from bonding-curve virtual reserves (most accurate) ──
            v_sol = float(msg.get("vSolInBondingCurve", 0))
            v_tok = float(msg.get("vTokensInBondingCurve", 0))
            if v_sol > 0 and v_tok > 0:
                # vSol is in lamports (1e9/SOL), vTokens in raw units (1e6/token)
                price = (v_sol / 1e9) / (v_tok / 1e6)
            else:
                # Fallback: derive price from this trade's own amounts
                sol = float(msg.get("solAmount", msg.get("sol_amount", 0))) / 1e9
                tok = float(msg.get("tokenAmount", msg.get("token_amount", 0))) / 1e6
                if tok <= 0 or sol <= 0:
                    return None
                price = sol / tok

            sol_amount = float(msg.get("solAmount", msg.get("sol_amount", 0))) / 1e9

            return {
                "mint":           msg.get("mint", ""),
                "tx_type":        msg.get("txType", "buy"),
                "sol_amount":     sol_amount,
                "token_amount":   float(msg.get("tokenAmount", msg.get("token_amount", 0))) / 1e6,
                "price":          price,
                "timestamp":      ts,
                "trader":         msg.get("traderPublicKey", ""),
                "tx_hash":        msg.get("signature", ""),
                "market_cap_sol": float(msg.get("marketCapSol", 0)) if float(msg.get("marketCapSol", 0)) > 0 else (price * 1_000_000_000),
                # iter28: pool liquidity depth (SOL in bonding curve) — the live
                # signal that flags dead-coin liquidity-drain dumps, absent from
                # OHLCV.  0.0 when the source message does not carry reserves.
                "pool_sol":       (v_sol / 1e9) if v_sol > 0 else 0.0,
            }
        except Exception:
            return None


class DexScreenerPollClient:
    """Poll DexScreener for near-real-time price updates when PumpSwap WS is unavailable."""

    def __init__(self, query: str, poll_seconds: float = 0.5):
        self.query = query
        self.poll_seconds = poll_seconds
        self._stop = False
        self._last_price: Optional[float] = None
        self._last_market_cap: Optional[float] = None

    def stop(self):
        self._stop = True

    async def stream(self) -> AsyncGenerator[dict, None]:
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                try:
                    pair = await _ds_search(session, self.query)
                    if pair:
                        price = float(pair.get("priceNative") or 0)
                        market_cap_usd = float(pair.get("marketCap") or pair.get("fdv") or 0)
                        price_usd = float(pair.get("priceUsd") or 0)
                        market_cap_sol = 0.0
                        if market_cap_usd > 0 and price > 0 and price_usd > 0:
                            sol_usd = price_usd / price
                            if sol_usd > 0:
                                market_cap_sol = market_cap_usd / sol_usd

                        if price > 0:
                            self._last_price = price
                            self._last_market_cap = market_cap_usd
                            yield {
                                "mint": pair.get("baseToken", {}).get("address", ""),
                                "tx_type": "update",
                                "sol_amount": 0.0,
                                "token_amount": 0.0,
                                "price": price,
                                "timestamp": time.time(),
                                "trader": "",
                                "tx_hash": "",
                                "market_cap_sol": market_cap_sol,
                                "market_cap_usd": market_cap_usd,
                                "synthetic": True,
                            }
                except Exception as e:
                    logger.warning(f"[DexScreenerPoll] {e}")
                await asyncio.sleep(self.poll_seconds)

class PumpFunPollClient:
    """Poll Pump.fun v3 frontend API for real-time market cap when WS is paywalled."""

    def __init__(self, mint: str, poll_seconds: float = 0.5):
        self.mint = mint
        self.poll_seconds = poll_seconds
        self._stop = False

    def stop(self):
        self._stop = True

    async def stream(self) -> AsyncGenerator[dict, None]:
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                try:
                    info = await _v3_coin(session, self.mint)
                    if info:
                        mcap_usd = float(info.get("usd_market_cap") or 0)
                        if mcap_usd > 0:
                            price_sol = float(info.get("price_sol") or 0)
                            price_usd = float(info.get("price_usd") or 0)
                            sol_usd = price_usd / price_sol if price_sol > 0 else 0
                            mcap_sol = mcap_usd / sol_usd if sol_usd > 0 else 0
                            
                            yield {
                                "mint": self.mint,
                                "tx_type": "update",
                                "sol_amount": 0.0,
                                "token_amount": 0.0,
                                "price": price_sol,
                                "timestamp": time.time(),
                                "trader": "",
                                "tx_hash": "",
                                "market_cap_sol": mcap_sol,
                                "market_cap_usd": mcap_usd,
                                "synthetic": True,
                            }
                except Exception as e:
                    logger.warning(f"[PumpFunPoll] {e}")
                
                await asyncio.sleep(self.poll_seconds)



# ── Shared Solana RPC WebSocket hub ──────────────────────────────────────────
# A single persistent WebSocket to api.mainnet-beta.solana.com is shared across
# ALL PumpSwapRPCClient instances.  Each vault account gets one subscription;
# notifications are fanned out to the correct client queues.
# This eliminates the N-connections-per-IP 429 that occurs with 5+ coins.

class _SharedSolanaWSHub:
    """Process-global multiplexed Solana RPC WebSocket hub."""

    def __init__(self):
        # subscription_id (int) → queue set
        self._sub_to_queues: dict[int, set[asyncio.Queue]] = defaultdict(set)
        # account_pubkey → subscription_id (filled once ack received)
        self._account_to_sub: dict[str, int] = {}
        # account_pubkey → set of queues (registered before ack)
        self._account_to_queues: dict[str, set[asyncio.Queue]] = defaultdict(set)
        # pending subscribe requests: request_id → account_pubkey
        self._req_to_account: dict[int, str] = {}
        self._next_req_id = 100
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._ws = None

    def _ensure_running(self):
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_running_loop()
                self._task = loop.create_task(self._run())
            except RuntimeError:
                pass

    async def _send_subscribe(self, account: str):
        """Send accountSubscribe for *account* on the live WS (if available)."""
        if self._ws is None:
            return
        req_id = self._next_req_id
        self._next_req_id += 1
        self._req_to_account[req_id] = account
        try:
            await self._ws.send(json.dumps({
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "accountSubscribe",
                "params": [account, {"encoding": "base64", "commitment": "processed"}],
            }))
            logger.debug(f"[SolanaHub] subscribed account {account[:8]} req={req_id}")
        except Exception as e:
            logger.debug(f"[SolanaHub] subscribe send failed: {e}")
            self._req_to_account.pop(req_id, None)

    async def _run(self):
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    SOLANA_RPC_WS,
                    ping_interval=20,
                    ping_timeout=15,
                    open_timeout=10,
                    max_size=2 ** 22,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0
                    logger.info("[SolanaHub] Connected")

                    # Re-subscribe all known accounts
                    async with self._lock:
                        accounts_to_resub = list(self._account_to_queues.keys())
                        self._account_to_sub.clear()
                        self._req_to_account.clear()

                    for account in accounts_to_resub:
                        await self._send_subscribe(account)

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        # Handle subscription acknowledgement
                        msg_id = msg.get("id")
                        if msg_id is not None and "result" in msg:
                            account = self._req_to_account.pop(msg_id, None)
                            if account is not None:
                                sub_id = msg["result"]
                                async with self._lock:
                                    self._account_to_sub[account] = sub_id
                                    # Move queues from account map to sub map
                                    qs = self._account_to_queues.get(account, set())
                                    self._sub_to_queues[sub_id] = set(qs)
                                logger.debug(f"[SolanaHub] ack account {account[:8]} → sub {sub_id}")
                            continue

                        # Handle account notification
                        if msg.get("method") != "accountNotification":
                            continue
                        params = msg.get("params", {})
                        sub_id = params.get("subscription")
                        value = params.get("result", {}).get("value") or {}
                        data = value.get("data")
                        if not data or sub_id is None:
                            continue

                        raw_data = base64.b64decode(data[0])
                        amount = _decode_spl_token_amount(raw_data)

                        async with self._lock:
                            queues = list(self._sub_to_queues.get(sub_id, set()))
                        for q in queues:
                            try:
                                q.put_nowait((sub_id, amount))
                            except asyncio.QueueFull:
                                pass

            except (ConnectionClosed, asyncio.TimeoutError, OSError) as e:
                logger.warning(f"[SolanaHub] {e} — reconnecting in {backoff:.1f}s")
            except asyncio.CancelledError:
                logger.info("[SolanaHub] Cancelled")
                return
            except Exception as e:
                logger.error(f"[SolanaHub] Unexpected: {e} — reconnecting in {backoff:.1f}s")
            finally:
                self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def subscribe(self, account: str, queue: asyncio.Queue):
        """Start delivering notifications for *account* to *queue*."""
        self._ensure_running()
        async with self._lock:
            self._account_to_queues[account].add(queue)
            # If already acked, also add to the sub map immediately
            sub_id = self._account_to_sub.get(account)
            if sub_id is not None:
                self._sub_to_queues[sub_id].add(queue)
        # Subscribe if this is the first queue for this account
        if self._ws is not None:
            await self._send_subscribe(account)

    async def unsubscribe(self, account: str, queue: asyncio.Queue):
        """Remove *queue* from notifications for *account*."""
        async with self._lock:
            self._account_to_queues[account].discard(queue)
            sub_id = self._account_to_sub.get(account)
            if sub_id is not None:
                self._sub_to_queues[sub_id].discard(queue)
            # If no more consumers, forget the subscription entirely
            if not self._account_to_queues[account]:
                del self._account_to_queues[account]
                if sub_id is not None:
                    self._account_to_sub.pop(account, None)
                    self._sub_to_queues.pop(sub_id, None)


# Process-global singleton
_solana_hub = _SharedSolanaWSHub()


class PumpSwapRPCClient:
    """Stream live PumpSwap spot price by watching the pool vault token accounts.

    Uses a single shared Solana RPC WebSocket (_solana_hub) so that any number
    of coins can be monitored without opening multiple connections and hitting
    the public RPC's 429 / connection-limit.
    """

    def __init__(self, pair_address: str):
        self.pair_address = pair_address
        self._stop = False
        self._pool: Optional[dict] = None
        self._base_amount_raw: Optional[int] = None
        self._quote_amount_raw: Optional[int] = None
        self._last_emitted: Optional[tuple[int, int]] = None
        self._last_emit_ts = 0.0
        self._market_cap_usd = 0.0
        self._market_cap_sol = 0.0
        self._sol_usd = 0.0
        self._implied_supply_tokens = 0.0
        # iter15 recorder-fix: previous raw vault balances for delta extraction.
        # Persisted across drain batches so we can compute net trade volume and
        # direction (WSOL inflow = buy, WSOL outflow = sell) on each vault
        # accountSubscribe notification. None until the first observation is
        # captured, so the first emitted sample carries zero trade volume
        # (it is a state snapshot, not a trade).
        self._prev_base_raw: Optional[int] = None
        self._prev_quote_raw: Optional[int] = None

    def stop(self):
        self._stop = True

    async def _rpc(self, session: aiohttp.ClientSession, method: str, params: list) -> dict:
        """Execute a Solana JSON-RPC call with global rate-limiting and 429 retry."""
        global _rpc_last_call_ts
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        max_attempts = 4
        backoff = 1.0
        for attempt in range(max_attempts):
            async with _get_rpc_semaphore():
                # Enforce a minimum gap between consecutive RPC calls
                async with _get_rpc_lock():
                    now = time.time()
                    wait = _RPC_MIN_INTERVAL - (now - _rpc_last_call_ts)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    _rpc_last_call_ts = time.time()

                try:
                    async with session.post(
                        SOLANA_RPC_HTTP, json=payload,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as r:
                        if r.status == 429:
                            retry_after = float(r.headers.get("Retry-After", backoff))
                            logger.warning(
                                f"[RPC] 429 Too Many Requests on {method} — "
                                f"retrying in {retry_after:.1f}s (attempt {attempt+1}/{max_attempts})"
                            )
                            await asyncio.sleep(retry_after)
                            backoff = min(backoff * 2, 30)
                            continue
                        r.raise_for_status()
                        data = await r.json(content_type=None)
                        if "error" in data:
                            raise RuntimeError(f"RPC {method}: {data['error']}")
                        return data["result"]
                except (aiohttp.ClientResponseError, RuntimeError):
                    raise
                except Exception as e:
                    if attempt < max_attempts - 1:
                        logger.warning(f"[RPC] {method} error ({e}), retry {attempt+1}")
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 30)
                    else:
                        raise
        raise RuntimeError(f"RPC {method}: exceeded {max_attempts} attempts")

    async def _load_pool(self, session: aiohttp.ClientSession):
        result = await self._rpc(session, "getAccountInfo", [self.pair_address, {"encoding": "base64", "commitment": "processed"}])
        value = result.get("value")
        if not value:
            raise RuntimeError(f"Pool account not found: {self.pair_address}")
        raw = base64.b64decode(value["data"][0])
        pool = _decode_pool_account(raw)

        accounts = await self._rpc(session, "getMultipleAccounts", [[
            pool["base_mint"],
            pool["quote_mint"],
            pool["pool_base_token_account"],
            pool["pool_quote_token_account"],
        ], {"encoding": "base64", "commitment": "processed"}])
        vals = accounts.get("value") or []
        if len(vals) != 4 or any(v is None for v in vals):
            raise RuntimeError("Missing mint or vault accounts for pool")

        base_mint_data = base64.b64decode(vals[0]["data"][0])
        quote_mint_data = base64.b64decode(vals[1]["data"][0])
        base_vault_data = base64.b64decode(vals[2]["data"][0])
        quote_vault_data = base64.b64decode(vals[3]["data"][0])

        pool["base_decimals"] = _decode_mint_decimals(base_mint_data)
        pool["quote_decimals"] = _decode_mint_decimals(quote_mint_data)
        pool["base_supply_raw"] = _decode_mint_supply(base_mint_data)
        self._pool = pool
        self._base_amount_raw = _decode_spl_token_amount(base_vault_data)
        self._quote_amount_raw = _decode_spl_token_amount(quote_vault_data)

    async def _refresh_market_caps(self, session: aiohttp.ClientSession):
        pair = await _ds_pair(session, self.pair_address)
        if not pair:
            pair = await _ds_search(session, self.pair_address)
        if not pair and self._pool:
            pair = await _ds_search(session, self._pool.get("base_mint", ""))
        if not pair:
            return

        market_cap_usd = float(pair.get("marketCap") or pair.get("fdv") or 0)
        price_sol = float(pair.get("priceNative") or 0)
        price_usd = float(pair.get("priceUsd") or 0)
        sol_usd = 0.0
        market_cap_sol = 0.0
        if market_cap_usd > 0 and price_sol > 0 and price_usd > 0:
            sol_usd = price_usd / price_sol
            if sol_usd > 0:
                market_cap_sol = market_cap_usd / sol_usd

        self._market_cap_usd = market_cap_usd
        self._market_cap_sol = market_cap_sol
        self._sol_usd = sol_usd if sol_usd > 0 else self._sol_usd

        if market_cap_usd > 0 and price_usd > 0:
            self._implied_supply_tokens = market_cap_usd / price_usd

    def _current_trade(
        self,
        force: bool = False,
        delta_base_raw: int = 0,
        delta_quote_raw: int = 0,
    ) -> Optional[dict]:
        if not self._pool or not self._base_amount_raw or self._quote_amount_raw is None:
            return None
        state_key = (self._base_amount_raw, self._quote_amount_raw)
        if state_key == self._last_emitted and not force:
            return None
        self._last_emitted = state_key

        base_decimals = self._pool["base_decimals"]
        quote_decimals = self._pool["quote_decimals"]
        base = self._base_amount_raw / (10 ** base_decimals)
        quote = self._quote_amount_raw / (10 ** quote_decimals)
        if base <= 0 or quote <= 0:
            return None

        if self._pool["quote_mint"] == WSOL_MINT:
            price = quote / base
        elif self._pool["base_mint"] == WSOL_MINT:
            price = base / quote
        else:
            price = quote / base

        market_cap_usd = self._market_cap_usd
        market_cap_sol = self._market_cap_sol
        if self._sol_usd > 0 and self._implied_supply_tokens > 0:
            live_price_usd = price * self._sol_usd
            market_cap_usd = live_price_usd * self._implied_supply_tokens
            market_cap_sol = market_cap_usd / self._sol_usd

        # iter15 recorder-fix: translate raw vault deltas into trade volume +
        # direction. PumpSwap CP-AMM swaps move reserves in opposite directions
        # (one vault up, one vault down); a same-direction move or zero delta
        # is a deposit/withdraw event (LP add/remove or fee accrual), treated
        # as a non-trade (`tx_type="update"`, no volume).
        token_amount = abs(delta_base_raw) / (10 ** base_decimals)
        sol_amount = abs(delta_quote_raw) / (10 ** quote_decimals)
        # When quote-mint is WSOL (the typical pump memecoin pairing), WSOL
        # flowing IN (quote_delta > 0) means a taker paid SOL for tokens:
        # that is a "buy" of the base token. WSOL flowing OUT is a "sell".
        quote_is_sol = self._pool["quote_mint"] == WSOL_MINT
        base_is_sol = self._pool["base_mint"] == WSOL_MINT
        has_trade_deltas = (delta_base_raw != 0) and (delta_quote_raw != 0) and \
            ((delta_base_raw > 0) != (delta_quote_raw > 0))

        tx_type = "update"
        synthetic = True
        if has_trade_deltas and (quote_is_sol or base_is_sol):
            # Direction by which vault the SOL flowed into.
            # If quote-mint is WSOL: quote_delta > 0 -> WSOL into pool -> buy.
            # If base-mint is WSOL:  base_delta  > 0 -> WSOL into pool -> sell of quote token;
            #   for a memecoin-base pairing this is rare; we still label it
            #   "buy" if the non-SOL reserve (quote token) decreased, i.e. taker
            #   bought the quote token with SOL.
            if quote_is_sol:
                tx_type = "buy" if delta_quote_raw > 0 else "sell"
            else:
                # base_is_sol: SOL in the base vault. base_delta > 0 means
                # WSOL flowed in (taker sold the quote token for SOL). From
                # the quote-token's perspective that's a "sell".
                tx_type = "sell" if delta_base_raw > 0 else "buy"
            # sol_amount tracks WSOL moved regardless of orientation.
            sol_amount = (abs(delta_quote_raw) if quote_is_sol
                          else abs(delta_base_raw)) / (10 ** 9)
            synthetic = False

        self._last_emit_ts = time.time()
        # iter28: pool liquidity depth.  The SOL-side vault of a PumpSwap pool
        # holds WSOL, so its current balance IS the pool's SOL depth — the
        # exact liquidity-drain observable that flags dead-coin dumps.
        sol_vault_raw = (self._quote_amount_raw if quote_is_sol
                         else (self._base_amount_raw if base_is_sol else 0)) or 0
        pool_sol = sol_vault_raw / (10 ** 9)
        return {
            "mint": self._pool["base_mint"],
            "tx_type":        tx_type,
            "sol_amount":     sol_amount,
            "token_amount":   token_amount,
            "price":          price,
            "timestamp":      self._last_emit_ts,
            "trader":         "",
            "tx_hash":        "",
            "market_cap_sol": market_cap_sol,
            "market_cap_usd": market_cap_usd,
            "synthetic":      synthetic,
            "pool_sol":       pool_sol,
        }

    async def stream(self) -> AsyncGenerator[dict, None]:
        backoff = 1.0
        while not self._stop:
            try:
                async with aiohttp.ClientSession() as http_session:
                    await self._load_pool(http_session)
                    await self._refresh_market_caps(http_session)
                    first = self._current_trade()
                    if first:
                        yield first

                    assert self._pool is not None
                    base_account = self._pool["pool_base_token_account"]
                    quote_account = self._pool["pool_quote_token_account"]

                    # Use the shared hub — no new WS connection opened here
                    queue: asyncio.Queue = asyncio.Queue(maxsize=512)
                    await _solana_hub.subscribe(base_account, queue)
                    await _solana_hub.subscribe(quote_account, queue)

                    logger.info(f"[PumpSwapRPC] Watching pool {self.pair_address[:8]}… (shared WS)")
                    backoff = 1.0
                    last_emit_ts = time.time()
                    next_mcap_refresh = time.time() + 5.0
                    dirty = False

                    try:
                        while not self._stop:
                            now = time.time()

                            # Periodic market-cap refresh
                            if now >= next_mcap_refresh:
                                try:
                                    await self._refresh_market_caps(http_session)
                                except Exception as e:
                                    logger.debug(f"[PumpSwapRPC] mcap refresh: {e}")
                                next_mcap_refresh = now + 5.0

                            # Drain all pending notifications from shared hub.
                            # iter15 recorder-fix: accumulate net vault deltas
                            # across the drain batch so a single swap (which
                            # typically fires both base and quote
                            # accountSubscribe notifications within milliseconds)
                            # is recognised as ONE trade with both legs observed.
                            drained = False
                            delta_base_raw = 0
                            delta_quote_raw = 0
                            while True:
                                try:
                                    _sub_id, amount = queue.get_nowait()
                                    # Map sub_id → base or quote
                                    async with _solana_hub._lock:
                                        base_sub = _solana_hub._account_to_sub.get(base_account)
                                        quote_sub = _solana_hub._account_to_sub.get(quote_account)
                                    if _sub_id == base_sub:
                                        prev = self._base_amount_raw or 0
                                        delta_base_raw += amount - prev
                                        self._base_amount_raw = amount
                                        dirty = True
                                    elif _sub_id == quote_sub:
                                        prev = self._quote_amount_raw or 0
                                        delta_quote_raw += amount - prev
                                        self._quote_amount_raw = amount
                                        dirty = True
                                    drained = True
                                except asyncio.QueueEmpty:
                                    break

                            if dirty and drained:
                                trade = self._current_trade(
                                    delta_base_raw=delta_base_raw,
                                    delta_quote_raw=delta_quote_raw,
                                )
                                if trade:
                                    dirty = False
                                    last_emit_ts = now
                                    yield trade
                            elif (now - last_emit_ts) >= 1.0:
                                # Heartbeat so callers know we're alive
                                heartbeat = self._current_trade(force=True)
                                if heartbeat:
                                    last_emit_ts = now
                                    yield heartbeat

                            await asyncio.sleep(0.05)
                    finally:
                        await _solana_hub.unsubscribe(base_account, queue)
                        await _solana_hub.unsubscribe(quote_account, queue)

            except RuntimeError as e:
                msg = str(e)
                if "Missing mint or vault accounts" in msg or "Pool account not found" in msg:
                    logger.warning(f"[PumpSwapRPC] Token not on PumpSwap ({msg}). Stopping.")
                    return
                logger.error(f"[PumpSwapRPC] RuntimeError: {e} — reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except (ConnectionClosed, asyncio.TimeoutError, OSError) as e:
                logger.warning(f"[PumpSwapRPC] {e} — reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except Exception as e:
                logger.error(f"[PumpSwapRPC] Unexpected: {e} — reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


class PumpFunRPCClient:
    """Stream live Pump.fun spot price by natively watching the bonding curve PDA.
    Provides 0-second latency directly from the Solana RPC WebSocket, bypassing all paywalls.
    """

    def __init__(self, mint: str):
        self.mint = mint
        self._stop = False
        self._curve_pubkey = self._get_bonding_curve(mint)
        self._last_emitted: Optional[tuple[int, int]] = None
        self._market_cap_sol = 0.0

    @staticmethod
    def _get_bonding_curve(mint_str: str) -> str:
        from solders.pubkey import Pubkey
        PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfX9PNXQjCEwX1qZhN")
        mint_pk = Pubkey.from_string(mint_str)
        pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)], PUMP_PROGRAM)
        return str(pda)

    def stop(self):
        self._stop = True

    @staticmethod
    def _decode_curve(data_b64: str) -> Optional[dict]:
        import base64
        try:
            raw = base64.b64decode(data_b64)
            if len(raw) < 41:
                return None
            v_tok = int.from_bytes(raw[8:16], "little")
            v_sol = int.from_bytes(raw[16:24], "little")
            return {"v_tok": v_tok, "v_sol": v_sol}
        except Exception:
            return None

    async def stream(self) -> AsyncGenerator[dict, None]:
        if not self._curve_pubkey:
            return

        queue = asyncio.Queue(maxsize=512)
        await _solana_hub.subscribe(self._curve_pubkey, queue)
        logger.info(f"[PumpFunRPC] Watching native bonding curve: {self._curve_pubkey[:8]}… for mint {self.mint[:8]}…")

        try:
            while not self._stop:
                try:
                    update = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if self._stop:
                    break

                data_str = update["account"]["data"][0]
                curve = self._decode_curve(data_str)
                if not curve:
                    continue

                v_tok = curve["v_tok"]
                v_sol = curve["v_sol"]

                state_tuple = (v_tok, v_sol)
                if self._last_emitted == state_tuple:
                    continue
                self._last_emitted = state_tuple

                if v_tok <= 0:
                    continue

                price_sol = (v_sol / 1e9) / (v_tok / 1e6)
                sol_amount = 0.0
                token_amount = 0.0

                # ── Estimate vol from curve changes ──
                if hasattr(self, "_prev_v_tok") and self._prev_v_tok:
                    tok_diff = abs(v_tok - self._prev_v_tok) / 1e6
                    sol_diff = abs(v_sol - self._prev_v_sol) / 1e9
                    token_amount = tok_diff
                    sol_amount = sol_diff
                else:
                    # Initial synthetic burst to ensure volume starts pumping
                    sol_amount = 0.1
                    token_amount = 0.1 / price_sol

                self._prev_v_tok = v_tok
                self._prev_v_sol = v_sol

                # Reconstruct market cap
                total_supply_tok = 1_000_000_000
                mcap_sol = price_sol * total_supply_tok
                # Base approximation at $150/sol since sol_usd isn't guaranteed natively 
                mcap_usd = mcap_sol * 150.0  

                yield {
                    "mint": self.mint,
                    "tx_type": "update",
                    "sol_amount": sol_amount,
                    "token_amount": token_amount,
                    "price": price_sol,
                    "timestamp": time.time(),
                    "trader": "",
                    "tx_hash": "",
                    "market_cap_sol": mcap_sol,
                    "market_cap_usd": mcap_usd,
                    "synthetic": True,
                }
        finally:
            await _solana_hub.unsubscribe(self._curve_pubkey, queue)

# ── New-Pair Stream (for sniper scanner) ──────────────────────────────────────

class NewPairsStream:
    """
    Subscribe to PumpPortal's 'subscribeNewToken' feed.
    Yields normalised dicts for every new token created on pump.fun.

    Each yielded dict contains:
        mint, name, symbol, twitter, telegram, website,
        initialBuy (lamports), marketCapSol, vSolInBondingCurve,
        creator, timestamp, uri
    """

    def __init__(self):
        self._stop = False

    def stop(self):
        self._stop = True

    async def stream(self) -> AsyncGenerator[dict, None]:
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(
                    PUMPPORTAL_WS,
                    ping_interval=20,
                    ping_timeout=15,
                    open_timeout=15,
                    max_size=2 ** 22,
                ) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    backoff = 1.0
                    logger.info("[NewPairsStream] Connected — subscribed to new token events")

                    async for raw in ws:
                        if self._stop:
                            break
                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if not isinstance(msg, dict):
                            continue
                        if "mint" not in msg:
                            continue

                        # Normalise timestamp
                        ts_raw = msg.get("timestamp") or msg.get("blockTime")
                        if ts_raw:
                            ts = float(ts_raw) / 1000.0 if float(ts_raw) > 1_000_000_000_000 else float(ts_raw)
                        else:
                            ts = time.time()

                        yield {
                            "mint":                    msg.get("mint", ""),
                            "name":                    msg.get("name", ""),
                            "symbol":                  msg.get("symbol", ""),
                            "twitter":                 msg.get("twitter", "") or "",
                            "telegram":                msg.get("telegram", "") or "",
                            "website":                 msg.get("website", "") or "",
                            "uri":                     msg.get("uri", "") or "",
                            # solAmount = SOL spent on initial buy (already in SOL)
                            "solAmount":               float(msg.get("solAmount", 0) or 0),
                            # initialBuy = tokens received (raw token units) — kept for reference
                            "initialBuy":              float(msg.get("initialBuy", 0) or 0),
                            # vSolInBondingCurve already in SOL
                            "marketCapSol":            float(msg.get("marketCapSol", 0) or 0),
                            "vSolInBondingCurve":      float(msg.get("vSolInBondingCurve", 0) or 0),
                            "vTokensInBondingCurve":   float(msg.get("vTokensInBondingCurve", 0) or 0),
                            "creator":                 msg.get("traderPublicKey", "") or msg.get("creator", ""),
                            "bondingCurveKey":         msg.get("bondingCurveKey", ""),
                            "timestamp":               ts,
                        }

            except asyncio.CancelledError:
                logger.info("[NewPairsStream] Cancelled")
                return
            except (ConnectionClosed, asyncio.TimeoutError, OSError) as e:
                logger.warning(f"[NewPairsStream] {e} — reconnecting in {backoff:.1f}s")
            except Exception as e:
                logger.error(f"[NewPairsStream] Unexpected: {e}")
            if not self._stop:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
