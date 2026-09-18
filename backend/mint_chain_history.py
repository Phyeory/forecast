"""Bounded public-chain history for pre-session calibration.

Callers must freeze ``before_unix`` at session start / the first replay candle.
Only complete seconds in [ceil(before - lookback), floor(before)) are retained;
the current wall-clock second is also excluded. Defaults freeze now once and
use a 6000-second lookback. Signature cursors walk backwards from the provider's
head to the requested historical window, independently of the transaction cap.

Coverage is best effort, NOT a claim of complete mint history: public RPCs may
prune history, and today's DexScreener address discovery may miss retired pools.
A failed, capped, or ambiguous account scan contributes no candles (rather than
persisting incomplete OHLCV seconds). Other successfully scanned accounts may
still contribute. Missing seconds are not filled. Only transaction-era balances
are used; current pool liquidity is never copied into historical candles.
Persisted rows are shared inputs, not a guarantee that historical data existed
in this application's cache at session start. Callers retain their DB/population
fallback and apply the same fixed bounds when loading cached rows.
"""
from __future__ import annotations

import asyncio
import logging
import math
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

_MAX_SIGS = 1000          # in-window transaction cap per account
_MAX_SIGNATURE_PAGES = 32  # independent budget for walking past newer history
_DEFAULT_LOOKBACK_SECONDS = 6000.0
_FETCH_CONCURRENCY = 4    # parallel getTransaction calls across endpoints
_TX_TIMEOUT = 8.0


def _history_bounds(before_unix: Optional[float],
                    lookback_seconds: float) -> tuple[int, int]:
    now = time.time()
    before = now if before_unix is None else float(before_unix)
    lookback = float(lookback_seconds)
    if not math.isfinite(before) or not math.isfinite(lookback) or lookback <= 0:
        raise ValueError("History cutoff must be finite and lookback positive")
    return math.ceil(before - lookback), math.floor(min(before, now))


def _within_bounds(rows: list[dict], lower: int, upper: int) -> list[dict]:
    # A fractional timestamp is not a complete, second-aligned candle.
    return [r for r in rows if lower <= r["time"] < upper
            and r["time"] == int(r["time"])]

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
                       db_path: str = _DB_PATH,
                       lookback_seconds: Optional[float] = None) -> list[dict]:
    """Read complete prior seconds; optionally restrict the historical window.

    Omitting lookback preserves the legacy unbounded-lower-edge read.
    """
    lower, upper = _history_bounds(before_unix, lookback_seconds
                                   if lookback_seconds is not None else 1e15)
    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        _ensure_table(conn)
        rows = conn.execute(
            """SELECT time, open, high, low, close, volume,
                      buy_volume, sell_volume, pool_sol
               FROM mint_history_candles
               WHERE mint=? AND time >= ? AND time < ? AND source='chain'
               ORDER BY time ASC""",
            (mint, float(lower), float(upper)),
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
                                 label: str,
                                 before_unix: Optional[float] = None,
                                 lookback_seconds: float =
                                 _DEFAULT_LOOKBACK_SECONDS) -> tuple[list[dict], bool]:
    """Fetch + parse one on-chain account's swap history in [lower, upper).

    Bounded historical scan.  Pagination walks ``before`` cursors backwards
    from the provider's head (present day) until the page's oldest blockTime
    is below ``lower`` — so a historical cutoff still reaches past data —
    with page-count and wall-clock budgets.  Signatures are filtered to the
    window before the ``max_sigs`` transaction-fetch cap applies, so
    present-day volume cannot crowd out the historical window.

    Returns (trades, complete).  ``complete`` is False whenever the window's
    coverage could not be PROVEN — page/time budget tripped, more in-window
    transactions than ``max_sigs``, an entry with unknown blockTime, a
    repeated cursor, or a page call failing mid-walk.  Callers then treat the
    account as "no data this run" rather than persisting an ambiguous partial
    window. Failed calls (None) differ from successful empty pages ([]).
    Provider pruning can make an exhausted-looking walk incomplete; that
    cannot be detected from these responses.
    """
    lower, upper_eff = _history_bounds(before_unix, lookback_seconds)
    if lower >= upper_eff or max_sigs <= 0:
        return [], True
    sigs: list[str] = []
    seen: set[str] = set()
    cursors: set[str] = set()
    complete = False
    before_sig: Optional[str] = None
    pages = 0

    async def call(method, params):
        remaining = timeout_s - (time.time() - t0)
        if remaining <= 0:
            return None
        try:
            return await asyncio.wait_for(
                _rpc_call(session, pool, method, params), remaining)
        except asyncio.TimeoutError:
            return None

    # Even a short nonempty page needs a cursor: providers can cap page size.
    while pages < _MAX_SIGNATURE_PAGES:
        query: dict = {"limit": 1000, "commitment": "finalized"}
        if before_sig is not None:
            query["before"] = before_sig
        page = await call("getSignaturesForAddress", [account, query])
        if not isinstance(page, list):
            break  # None is a failure, not an empty successful page.
        if not page:
            complete = True  # provider exhausted (pruning remains possible)
            break
        pages += 1
        for s in page:
            bt = s.get("blockTime")
            sig = s.get("signature")
            if bt is None or not sig:
                return [], False  # unknown time could hide an in-window swap
            if sig not in seen and s.get("err") is None and lower <= bt < upper_eff:
                sigs.append(sig)
                if len(sigs) > max_sigs:
                    return [], False
            seen.add(sig)
        if page[-1]["blockTime"] < lower:
            complete = True
            break
        before_sig = page[-1]["signature"]
        if before_sig in cursors:
            break  # provider ignored cursor; bounded, fail closed
        cursors.add(before_sig)

    if not complete:
        logger.info("[MintChain] %s historical scan incomplete; fallback", label)
        return [], False
    if not sigs:
        return [], True

    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)
    results: list[Optional[dict]] = [None] * len(sigs)
    budget_exhausted = False

    async def grab(i: int, sig: str):
        nonlocal budget_exhausted
        async with sem:
            if time.time() - t0 > timeout_s:
                budget_exhausted = True
                return
            res = await call(
                "getTransaction",
                [sig, {"encoding": "jsonParsed",
                       "maxSupportedTransactionVersion": 0,
                       "commitment": "finalized"}],
            )
            if not res or res.get("blockTime") is None:
                budget_exhausted = True
                return
            if lower <= res["blockTime"] < upper_eff:
                results[i] = parse_fn(res)

    await asyncio.gather(*(grab(i, s) for i, s in enumerate(sigs)))
    if budget_exhausted:
        return [], False
    # RPC signatures are newest-first; reverse before stable candle sorting
    # so within-second open/close follow provider order, not HTTP completion.
    trades = [t for t in reversed(results) if t]
    # Trades carry raw blockTime; keep only complete, in-window seconds.
    return _within_bounds(trades, lower, upper_eff), True


async def fetch_mint_history(mint: str, max_sigs: int = _MAX_SIGS,
                             endpoints: Optional[list[str]] = None,
                             timeout_s: float = 90.0,
                             before_unix: Optional[float] = None,
                             lookback_seconds: float =
                             _DEFAULT_LOOKBACK_SECONDS) -> list[dict]:
    """Fetch a bounded historical slice of the mint's on-chain trade history.

    Two surfaces, merged (disjoint in time across the token's life):
      * bonding-curve PDA — pre-graduation trades (reserves-formula price)
      * PumpSwap pool     — post-graduation trades (ΔWSOL/Δtoken price;
        pool resolved via DexScreener, WSOL-quoted pairs only)

    Candles are complete seconds in [ceil(before - lookback), floor(before)),
    where ``before`` defaults to (frozen) now.  Pagination walks backwards
    through newer signatures so a historical cutoff still reaches past data.
    Accounts with detected gaps (RPC/page/time/cap failures) are dropped.
    Successful scans still only cover provider-visible, recognized swaps;
    the existing transaction parsers are not a full transaction indexer.

    Returns candles sorted by time; [] when no account supplies usable data.
    Invalid bounds raise ValueError. Persisted by fetch_and_persist.
    """
    lower, upper = _history_bounds(before_unix, lookback_seconds)
    if lower >= upper or max_sigs <= 0 or timeout_s <= 0:
        return []
    # Equivalent integer window passed to all account scans, frozen once.
    before_unix, lookback_seconds = upper, upper - lower
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
            curve_trades, curve_complete = await _fetch_account_history(
                session, rpc_pool, curve_pda,
                lambda res: _parse_swap_tx(res, curve_pda),
                max_sigs, t0, timeout_s, "curve", before_unix, lookback_seconds,
            )
            if curve_complete:
                trades += curve_trades
            else:
                logger.warning(
                    f"[MintChain] {mint[:8]}… curve scan incomplete "
                    f"(budget/time) — dropping surface"
                )

            # 2. PumpSwap pool history (post-graduation) — resolved via
            #    DexScreener (the repo's proven lookup); WSOL-quoted only.
            #    DexScreener returns the CURRENT pair snapshot; a pool it
            #    no longer lists can't be scanned this run — acceptable
            #    under the documented coverage caveats.
            try:
                from pumpfun_client import get_ds_pair_by_mint
                remaining = timeout_s - (time.time() - t0)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                pair = await asyncio.wait_for(
                    get_ds_pair_by_mint(session, mint), remaining)
                if (pair and pair.get("pairAddress")
                        and pair.get("quoteToken", {}).get("address") == _WSOL_MINT):
                    pool_trades, pool_complete = await _fetch_account_history(
                        session, rpc_pool, pair["pairAddress"],
                        lambda res: _parse_pool_swap_tx(res, mint),
                        max_sigs, t0, timeout_s, "pool", before_unix, lookback_seconds,
                    )
                    if pool_complete:
                        trades += pool_trades
                    else:
                        logger.warning(
                            f"[MintChain] {mint[:8]}… pool scan incomplete "
                            f"(budget/time) — dropping surface"
                        )
            except Exception as e:
                logger.info(f"[MintChain] pool resolution failed for {mint[:8]}…: {e}")

        candles = _within_bounds(_candles_from_trades(trades), lower, upper)
        logger.info(f"[MintChain] {mint[:8]}… fetched → {len(trades)} swaps → "
                    f"{len(candles)} candles "
                    f"[{lower}..{upper}) ({time.time()-t0:.0f}s)")
        return candles
    except ValueError as e:
        logger.warning(f"[MintChain] invalid history bounds for {mint[:8]}…: {e}")
        return []
    except Exception as e:
        logger.warning(f"[MintChain] fetch error for {mint[:8]}…: {e}")
        return []


async def fetch_and_persist(mint: str, max_sigs: int = _MAX_SIGS,
                            timeout_s: float = 90.0,
                            before_unix: Optional[float] = None,
                            lookback_seconds: float =
                            _DEFAULT_LOOKBACK_SECONDS,
                            db_path: str = _DB_PATH) -> int:
    """Fetch a bounded historical slice and persist it.

    ``before_unix`` is the fixed calibration cutoff (session start / first
    replay candle); None freezes the wall clock once, inside these bounds.
    Lookback is clamped to the fixed cutoff — present-day data can never leak
    past ``before_unix``.  Returns the number of rows inserted.
    """
    lower, upper = _history_bounds(before_unix, lookback_seconds)
    if lower >= upper:
        return 0
    candles = await fetch_mint_history(mint, max_sigs=max_sigs,
                                       timeout_s=timeout_s,
                                       before_unix=upper,
                                       lookback_seconds=upper - lower)
    # Defense at the persistence boundary as well as within provider parsing.
    return persist_mint_history(mint, _within_bounds(candles, lower, upper),
                                db_path=db_path)
