"""mint_chain_history.py — Fetch a token's full on-chain trade history (iter86d).

The user proposal, live path: at session start, fetch the token's COMPLETE
bonding-curve trade history from Solana RPC — the full picture of the coin
being traded, available at tick 0 even for mints this platform has never
recorded.

Mechanics:
  1. Derive the bonding-curve PDA (same derivation as the live stream).
  2. getSignaturesForAddress(PDA) — every swap tx that ever touched the curve.
  3. getTransaction(jsonParsed) for the newest `max_sigs` signatures, fanned
     across the RPC endpoint pool with concurrency.
  4. Per tx: curve SOL balance delta (preBalances/postBalances at the curve
     PDA index) and curve token-account delta (pre/postTokenBalances).
     Side: token_delta < 0 = BUY (curve loses tokens), > 0 = SELL.
     Price: pump.fun reserves formula  (v_sol + 85) / (1e9 − v_tok) applied
     to post-tx reserves — the same spot definition the live stream uses.
  5. Aggregate per blockTime into 1s candles (o/h/l/c, volume SOL,
     buy/sell split) and persist into price_data.db `mint_history_candles`
     (INSERT OR IGNORE — the table is the shared calibration input for live
     AND backtest, which keeps the two pipelines in parity).

Determinism/parity: fetched rows are keyed by candle time; both the live
calibration and the backtest calibration read the same table.  Nothing here
touches RNG or wall-clock-dependent engine state.

Failure posture: every failure mode returns [] — the caller falls back to
whatever the DB already has (recordings), then population, then DEFAULT.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_SOLANA_RPCS = [
    "https://solana-rpc.publicnode.com",
    "https://rpc.ankr.com/solana",
    "https://api.mainnet-beta.solana.com",
]

# pump.fun curve constants (memory: 1B supply, ~85.0054 SOL graduation)
_PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
_VIRTUAL_SOL = 85.0054
_SUPPLY_TOKENS = 1_000_000_000.0

_MAX_SIGS = 1000          # one getSignaturesForAddress page — full picture cap
_FETCH_CONCURRENCY = 4    # parallel getTransaction calls across endpoints
_TX_TIMEOUT = 8.0

_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "price_data.db")


def _bonding_curve_pda(mint: str) -> Optional[str]:
    try:
        from solders.pubkey import Pubkey
        program = Pubkey.from_string(_PUMP_PROGRAM)
        mint_pk = Pubkey.from_string(mint)
        pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pk)], program)
        return str(pda)
    except Exception:
        return None


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mint_history_candles (
            mint TEXT NOT NULL,
            time INTEGER NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL,
            low REAL NOT NULL, close REAL NOT NULL,
            volume REAL NOT NULL, buy_volume REAL NOT NULL,
            sell_volume REAL NOT NULL, pool_sol REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'chain',
            fetched_at INTEGER NOT NULL,
            PRIMARY KEY (mint, time, source)
        )
    """)


def persist_mint_history(mint: str, candles: list[dict],
                         db_path: str = _DB_PATH) -> int:
    """INSERT OR IGNORE fetched candles.  Returns rows actually inserted."""
    if not candles:
        return 0
    now = int(time.time())
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        _ensure_table(conn)
        cur = conn.cursor()
        inserted = 0
        for c in candles:
            cur.execute(
                """INSERT OR IGNORE INTO mint_history_candles
                   (mint, time, open, high, low, close, volume,
                    buy_volume, sell_volume, pool_sol, source, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'chain', ?)""",
                (mint, int(c["time"]), float(c["open"]), float(c["high"]),
                 float(c["low"]), float(c["close"]), float(c["volume"]),
                 float(c["buy_volume"]), float(c["sell_volume"]),
                 float(c.get("pool_sol", 0.0)), now),
            )
            inserted += cur.rowcount if cur.rowcount > 0 else 0
        conn.commit()
        return inserted
    except Exception as e:
        logger.warning(f"[MintChain] persist error: {e}")
        return 0
    finally:
        conn.close()


def load_chain_candles(mint: str, before_unix: float,
                       db_path: str = _DB_PATH) -> list[dict]:
    """Read persisted chain candles for a mint (time < before_unix)."""
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        _ensure_table(conn)
        rows = conn.execute(
            """SELECT time, open, high, low, close, volume,
                      buy_volume, sell_volume, pool_sol
               FROM mint_history_candles
               WHERE mint=? AND time < ? AND source='chain'
               ORDER BY time ASC""",
            (mint, float(before_unix)),
        ).fetchall()
        return [
            {"time": r[0], "open": r[1], "high": r[2], "low": r[3],
             "close": r[4], "volume": r[5], "buy_volume": r[6],
             "sell_volume": r[7], "pool_sol": r[8]}
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[MintChain] load error: {e}")
        return []
    finally:
        conn.close()


# ── RPC plumbing ─────────────────────────────────────────────────────────────

class _EndpointPool:
    """Round-robin over the endpoint list with per-endpoint spacing."""

    def __init__(self, endpoints: list[str], min_interval: float = 0.12):
        self.endpoints = endpoints
        self.min_interval = min_interval
        self._idx = 0
        self._last: dict[str, float] = {}

    def next(self) -> str:
        ep = self.endpoints[self._idx % len(self.endpoints)]
        self._idx += 1
        wait = self.min_interval - (time.time() - self._last.get(ep, 0.0))
        return ep, max(wait, 0.0)

    def mark(self, ep: str) -> None:
        self._last[ep] = time.time()


async def _rpc_call(session: aiohttp.ClientSession, pool: _EndpointPool,
                    method: str, params: list) -> Optional[dict]:
    ep, wait = pool.next()
    if wait > 0:
        await asyncio.sleep(wait)
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with session.post(ep, json=payload,
                                timeout=aiohttp.ClientTimeout(total=_TX_TIMEOUT)) as r:
            pool.mark(ep)
            if r.status == 429:
                await asyncio.sleep(1.0)
                return None
            r.raise_for_status()
            data = await r.json(content_type=None)
            if "error" in data:
                return None
            return data.get("result")
    except Exception:
        pool.mark(ep)
        return None


def _parse_swap_tx(tx: dict, curve_pda: str) -> Optional[dict]:
    """Extract (time, price, sol_delta, side) from one jsonParsed tx."""
    try:
        block_time = tx.get("blockTime")
        meta = tx.get("meta")
        if not block_time or not meta:
            return None
        keys = tx["transaction"]["message"]["accountKeys"]
        pubkeys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
        try:
            curve_idx = pubkeys.index(curve_pda)
        except ValueError:
            return None

        lam = meta.get("preBalances"), meta.get("postBalances")
        if not lam[0] or not lam[1]:
            return None
        sol_pre = lam[0][curve_idx] / 1e9
        sol_post = lam[1][curve_idx] / 1e9
        sol_delta = sol_post - sol_pre  # curve SOL change (buys: +)

        # curve token account: pre/postTokenBalances entry owned by curve PDA
        tok_pre = tok_post = None
        decimals = 6
        for tb in (meta.get("preTokenBalances") or []):
            if tb.get("owner") == curve_pda:
                tok_pre = float(tb["uiTokenAmount"]["amount"])
                decimals = int(tb["uiTokenAmount"].get("decimals", 6))
        for tb in (meta.get("postTokenBalances") or []):
            if tb.get("owner") == curve_pda:
                tok_post = float(tb["uiTokenAmount"]["amount"])
                decimals = int(tb["uiTokenAmount"].get("decimals", 6))
        if tok_pre is None or tok_post is None:
            return None
        tok_delta = (tok_post - tok_pre) / (10 ** max(decimals, 1))  # buys: −
        if abs(tok_delta) < 1.0 or abs(sol_delta) < 1e-9:
            return None  # fee-only / dust / rent tx

        v_tok_tokens = tok_post / (10 ** max(decimals, 1))
        v_sol_sol = sol_post
        price = (v_sol_sol + _VIRTUAL_SOL) / max(_SUPPLY_TOKENS - v_tok_tokens, 1.0)
        side = "buy" if tok_delta < 0 else "sell"
        return {"time": int(block_time), "price": price,
                "sol": abs(sol_delta), "side": side}
    except Exception:
        return None


_WSOL_MINT = "So11111111111111111111111111111111111111112"


def _parse_pool_swap_tx(tx: dict, token_mint: str) -> Optional[dict]:
    """Extract a PumpSwap pool trade: price = |ΔWSOL| / |Δtoken| (pool side).

    The pool owns both vaults; the traded-token vault and the WSOL vault are
    discriminated by their `mint` field in pre/postTokenBalances.  Pool
    token_delta > 0 = user SOLD into the pool; < 0 = user bought.
    """
    try:
        block_time = tx.get("blockTime")
        meta = tx.get("meta")
        if not block_time or not meta:
            return None
        pre = {tb["mint"]: tb for tb in (meta.get("preTokenBalances") or [])}
        post = {tb["mint"]: tb for tb in (meta.get("postTokenBalances") or [])}
        if token_mint not in pre or token_mint not in post:
            return None
        if _WSOL_MINT not in pre or _WSOL_MINT not in post:
            return None  # USDC-quoted or exotic pair — skip

        def amt(entry):
            return float(entry["uiTokenAmount"]["amount"]) / (
                10 ** int(entry["uiTokenAmount"].get("decimals", 9)))

        tok_pre, tok_post = amt(pre[token_mint]), amt(post[token_mint])
        wsol_pre, wsol_post = amt(pre[_WSOL_MINT]), amt(post[_WSOL_MINT])
        tok_delta = tok_post - tok_pre   # pool side: + = user sold
        wsol_delta = wsol_post - wsol_pre
        if abs(tok_delta) < 1.0 or abs(wsol_delta) < 1e-9:
            return None
        price = abs(wsol_delta) / abs(tok_delta)
        side = "sell" if tok_delta > 0 else "buy"
        return {"time": int(block_time), "price": price,
                "sol": abs(wsol_delta), "side": side}
    except Exception:
        return None


def _candles_from_trades(trades: list[dict]) -> list[dict]:
    """Aggregate per-second trade ticks into 1s candles."""
    if not trades:
        return []
    trades.sort(key=lambda t: t["time"])
    out: dict[int, dict] = {}
    for t in trades:
        sec = t["time"]
        c = out.get(sec)
        if c is None:
            out[sec] = {"time": sec, "open": t["price"], "high": t["price"],
                        "low": t["price"], "close": t["price"],
                        "volume": 0.0, "buy_volume": 0.0, "sell_volume": 0.0}
            c = out[sec]
        c["high"] = max(c["high"], t["price"])
        c["low"] = min(c["low"], t["price"])
        c["close"] = t["price"]
        c["volume"] += t["sol"]
        if t["side"] == "buy":
            c["buy_volume"] += t["sol"]
        else:
            c["sell_volume"] += t["sol"]
    return [out[k] for k in sorted(out)]


async def _fetch_account_history(session: aiohttp.ClientSession,
                                 pool: _EndpointPool, account: str,
                                 parse_fn, max_sigs: int,
                                 t0: float, timeout_s: float,
                                 label: str) -> list[dict]:
    """Fetch + parse the swap history of one on-chain account (curve or pool)."""
    sigs_res = await _rpc_call(
        session, pool, "getSignaturesForAddress",
        [account, {"limit": min(max_sigs, 1000)}],
    )
    if not sigs_res:
        logger.info(f"[MintChain] no signatures for {label} {account[:8]}…")
        return []
    sigs = [s["signature"] for s in sigs_res if s.get("err") is None][:max_sigs]
    if not sigs:
        return []

    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)
    results: list[Optional[dict]] = [None] * len(sigs)

    async def grab(i: int, sig: str):
        async with sem:
            if time.time() - t0 > timeout_s:
                return
            res = await _rpc_call(
                session, pool, "getTransaction",
                [sig, {"encoding": "jsonParsed",
                       "maxSupportedTransactionVersion": 0,
                       "commitment": "confirmed"}],
            )
            if res:
                results[i] = parse_fn(res)

    await asyncio.gather(*(grab(i, s) for i, s in enumerate(sigs)))
    return [t for t in results if t]


async def fetch_mint_history(mint: str, max_sigs: int = _MAX_SIGS,
                             endpoints: Optional[list[str]] = None,
                             timeout_s: float = 90.0) -> list[dict]:
    """Fetch the mint's FULL on-chain trade history → 1s candles.

    Two surfaces, merged (disjoint in time across the token's life):
      * bonding-curve PDA — pre-graduation trades (reserves-formula price)
      * PumpSwap pool     — post-graduation trades (ΔWSOL/Δtoken price;
        pool resolved via DexScreener, WSOL-quoted pairs only)

    Returns candles sorted by time; [] on any failure.  Persisted by the
    caller (or use fetch_and_persist).
    """
    curve_pda = _bonding_curve_pda(mint)
    if not curve_pda:
        return []
    eps = endpoints or _SOLANA_RPCS
    rpc_pool = _EndpointPool(eps, min_interval=0.12)
    t0 = time.time()

    try:
        async with aiohttp.ClientSession() as session:
            trades: list[dict] = []

            # 1. bonding-curve history (pre-graduation)
            trades += await _fetch_account_history(
                session, rpc_pool, curve_pda,
                lambda res: _parse_swap_tx(res, curve_pda),
                max_sigs, t0, timeout_s, "curve",
            )

            # 2. PumpSwap pool history (post-graduation) — resolved via
            #    DexScreener (the repo's proven lookup); WSOL-quoted only.
            try:
                from pumpfun_client import get_ds_pair_by_mint
                pair = await get_ds_pair_by_mint(session, mint)
                if pair and pair.get("pairAddress"):
                    trades += await _fetch_account_history(
                        session, rpc_pool, pair["pairAddress"],
                        lambda res: _parse_pool_swap_tx(res, mint),
                        max_sigs, t0, timeout_s, "pool",
                    )
            except Exception as e:
                logger.info(f"[MintChain] pool resolution failed for {mint[:8]}…: {e}")

        candles = _candles_from_trades(trades)
        logger.info(f"[MintChain] {mint[:8]}… fetched → {len(trades)} swaps → "
                    f"{len(candles)} candles ({time.time()-t0:.0f}s)")
        return candles
    except Exception as e:
        logger.warning(f"[MintChain] fetch error for {mint[:8]}…: {e}")
        return []


async def fetch_and_persist(mint: str, max_sigs: int = _MAX_SIGS,
                            timeout_s: float = 90.0) -> int:
    """Fetch + persist.  Returns the number of rows inserted."""
    candles = await fetch_mint_history(mint, max_sigs=max_sigs,
                                       timeout_s=timeout_s)
    return persist_mint_history(mint, candles)
