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

PUMPPORTAL_WS = "wss://pumpportal.fun/api/data"
PUMP_API_V3   = "https://frontend-api-v3.pump.fun"
DEXSCREENER   = "https://api.dexscreener.com"
SOLANA_RPC_HTTP = "https://api.mainnet-beta.solana.com"
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
        backoff = 0.5
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
                    backoff = 0.5
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
            backoff = min(backoff * 2, 30)

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
                "market_cap_sol": float(msg.get("marketCapSol", 0)),
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


class PumpSwapRPCClient:
    """Stream live PumpSwap spot price by watching the pool vault token accounts."""

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

    def stop(self):
        self._stop = True

    async def _rpc(self, session: aiohttp.ClientSession, method: str, params: list) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params,
        }
        async with session.post(SOLANA_RPC_HTTP, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
            if "error" in data:
                raise RuntimeError(f"RPC {method}: {data['error']}")
            return data["result"]

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

    async def _refresh_vaults(self, session: aiohttp.ClientSession):
        assert self._pool is not None
        accounts = await self._rpc(session, "getMultipleAccounts", [[
            self._pool["pool_base_token_account"],
            self._pool["pool_quote_token_account"],
        ], {"encoding": "base64", "commitment": "processed"}])
        vals = accounts.get("value") or []
        if len(vals) != 2 or any(v is None for v in vals):
            raise RuntimeError("Missing vault account data")
        self._base_amount_raw = _decode_spl_token_amount(base64.b64decode(vals[0]["data"][0]))
        self._quote_amount_raw = _decode_spl_token_amount(base64.b64decode(vals[1]["data"][0]))

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

    def _current_trade(self, force: bool = False) -> Optional[dict]:
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

        self._last_emit_ts = time.time()
        return {
            "mint": self._pool["base_mint"],
            "tx_type": "update",
            "sol_amount": 0.0,
            "token_amount": 0.0,
            "price": price,
            "timestamp": self._last_emit_ts,
            "trader": "",
            "tx_hash": "",
            "market_cap_sol": market_cap_sol,
            "market_cap_usd": market_cap_usd,
            "synthetic": True,
        }

    async def stream(self) -> AsyncGenerator[dict, None]:
        backoff = 0.5
        while not self._stop:
            try:
                async with aiohttp.ClientSession() as http_session:
                    await self._load_pool(http_session)
                    await self._refresh_market_caps(http_session)
                    first = self._current_trade()
                    if first:
                        yield first

                    async with websockets.connect(SOLANA_RPC_WS, ping_interval=20, ping_timeout=15, open_timeout=10, max_size=2 ** 20) as ws:
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "accountSubscribe",
                            "params": [self._pool["pool_base_token_account"], {"encoding": "base64", "commitment": "processed"}],
                        }))
                        base_ack = json.loads(await ws.recv())
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "accountSubscribe",
                            "params": [self._pool["pool_quote_token_account"], {"encoding": "base64", "commitment": "processed"}],
                        }))
                        quote_ack = json.loads(await ws.recv())
                        base_sub = base_ack.get("result")
                        quote_sub = quote_ack.get("result")
                        logger.info(f"[PumpSwapRPC] Watching pool {self.pair_address[:8]}...")
                        backoff = 0.5
                        dirty = False
                        last_note_at = 0.0
                        next_mcap_refresh = time.time() + 1.0
                        while not self._stop:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                            except asyncio.TimeoutError:
                                now = time.time()
                                if now >= next_mcap_refresh:
                                    try:
                                        await self._refresh_market_caps(http_session)
                                    except Exception as e:
                                        logger.debug(f"[PumpSwapRPC] market-cap refresh failed: {e}")
                                    next_mcap_refresh = now + 1.0
                                if dirty and (time.time() - last_note_at) >= 0.05:
                                    trade = self._current_trade()
                                    dirty = False
                                    if trade:
                                        yield trade
                                elif self._last_emit_ts and (time.time() - self._last_emit_ts) >= 1.0:
                                    heartbeat = self._current_trade(force=True)
                                    if heartbeat:
                                        yield heartbeat
                                continue
                            if self._stop:
                                return
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if msg.get("method") != "accountNotification":
                                continue
                            params = msg.get("params", {})
                            sub_id = params.get("subscription")
                            value = params.get("result", {}).get("value") or {}
                            data = value.get("data")
                            if not data:
                                continue
                            raw_data = base64.b64decode(data[0])
                            amount = _decode_spl_token_amount(raw_data)
                            if sub_id == base_sub:
                                self._base_amount_raw = amount
                                dirty = True
                                last_note_at = time.time()
                            elif sub_id == quote_sub:
                                self._quote_amount_raw = amount
                                dirty = True
                                last_note_at = time.time()
            except (ConnectionClosed, asyncio.TimeoutError, OSError) as e:
                logger.warning(f"[PumpSwapRPC] {e} — reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
            except RuntimeError as e:
                msg = str(e)
                if "Missing mint or vault accounts" in msg or "Pool account not found" in msg:
                    # Token is not on PumpSwap (still on bonding curve or not migrated).
                    # Stop retrying — this will never succeed.
                    logger.warning(f"[PumpSwapRPC] Token not on PumpSwap ({msg}). Stopping.")
                    return
                logger.error(f"[PumpSwapRPC] RuntimeError: {e} — reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
            except Exception as e:
                logger.error(f"[PumpSwapRPC] Unexpected: {e} — reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10)
