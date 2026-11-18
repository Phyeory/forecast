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

import data_store

logger = logging.getLogger(__name__)

# Read endpoints for the history scan.  ankr's public host is NOT here: it
# answers HTTP 403 ("API key is not allowed to access blockchain") on every
# call, and because the pool is round-robin that single dead leg failed every
# paginated scan (see _rpc_call failover).  mainnet-beta is rate-limited but
# kept as the redundancy leg — same ordering live_trader.SOLANA_RPCS uses.
_SOLANA_RPCS = [
    "https://solana-rpc.publicnode.com",
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
# Public RPCs retain roughly two days of transaction bodies (getTransaction
# starts answering null past it, while getSignaturesForAddress keeps the sigs).
# A cutoff older than this can only ever return pruned transactions, so the
# scan is skipped instead of burning the whole 90 s budget per recording —
# measured 2026-11-18: 72/72 historical slices (1-30 days old) came back with
# zero usable swaps, at up to 90 s each.
_RETENTION_SECONDS = 2 * 24 * 3600.0


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

_DB_PATH = os.environ.get("PUMPCHART_PRICE_DB") or str(data_store.PRICE_DB)


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


class _RPCFailed:
    """Sentinel: every endpoint in the pool failed this call.

    Distinct from ``None``, which is a SUCCESSFUL call whose JSON-RPC result
    is null (e.g. getTransaction for a pruned signature).  Conflating the two
    made a single bad endpoint indistinguishable from provider pruning.
    """
    __slots__ = ()


_RPC_FAILED = _RPCFailed()


async def _rpc_call(session: aiohttp.ClientSession, pool: _EndpointPool,
                    method: str, params: list):
    """POST one JSON-RPC method, failing over across the endpoint pool.

    Every endpoint is tried at most once per call; the first usable answer
    wins.  Returns ``_RPC_FAILED`` only when ALL of them fail (transport
    error, HTTP status, or JSON-RPC error object).

    Why this matters (Sep-18 regression, fixed 2026-11-18): the historical
    walk below needs a page 2 for any token younger than the lookback, and a
    round-robin pool put that second page on a permanently-403ing host.  One
    dead leg therefore dropped every scan as "incomplete" — zero chain
    candles persisted from 2026-09-16 until this fix, in live and backtest
    alike.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for _ in range(max(1, len(pool.endpoints))):
        ep, wait = pool.next()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with session.post(
                    ep, json=payload,
                    timeout=aiohttp.ClientTimeout(total=_TX_TIMEOUT)) as r:
                pool.mark(ep)
                if r.status == 429:
                    await asyncio.sleep(1.0)
                    continue
                r.raise_for_status()
                data = await r.json(content_type=None)
                if "error" in data:
                    continue
                return data.get("result")
        except Exception:
            pool.mark(ep)
            continue
    return _RPC_FAILED


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
    coverage could not be PROVEN — page/time budget tripped, an entry with
    unknown blockTime, a repeated cursor, or a page call failing mid-walk.
    Callers then treat the account as "no data this run" rather than
    persisting an ambiguous partial window. Failed calls (None) differ from
    successful empty pages ([]).

    A busy account yields more in-window signatures than ``max_sigs``.  The
    walk is newest-first, so collection STOPS at the cap and keeps the newest
    ``max_sigs`` — the exact slice the calibration consumes (most recent
    <= ``MAX_MINT_CANDLES`` seconds before the cutoff).  Discarding the whole
    account instead (pre-fix) made the layer yield nothing for every actively
    traded token: a fresh pump.fun mint prints ~3,400 sigs in 8 minutes.
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
    truncated = False
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
            # Timeout (None) or an all-endpoints failure (_RPC_FAILED) is a
            # failure, not an empty successful page.
            break
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
                if len(sigs) >= max_sigs:
                    truncated = True  # keep the newest max_sigs, stop walking
                    break
                sigs.append(sig)
            seen.add(sig)
        if truncated:
            complete = True
            break
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
    missing = 0

    async def grab(i: int, sig: str):
        nonlocal missing
        async with sem:
            if time.time() - t0 > timeout_s:
                missing += 1
                return
            res = await call(
                "getTransaction",
                [sig, {"encoding": "jsonParsed",
                       # pump.fun swaps are version-1 txs now; a 0 ceiling makes
                       # the RPC reject them (-32015 "Transaction version (1)
                       # is not supported") and the scan fail closed.  1 is a
                       # MAXIMUM, so v0 transactions still parse identically.
                       "maxSupportedTransactionVersion": 1,
                       "commitment": "finalized"}],
            )
            if res is _RPC_FAILED or not res or res.get("blockTime") is None:
                missing += 1
                return
            if lower <= res["blockTime"] < upper_eff:
                results[i] = parse_fn(res)

    await asyncio.gather(*(grab(i, s) for i, s in enumerate(sigs)))
    # Public endpoints serve ~3-8 getTransaction/s, so a busy account can hit
    # the wall-clock budget before all of `sigs` is read, and a few reads may
    # be pruned.  Keep the trades we actually parsed (missing seconds are
    # never filled — the same contract the DB already documents) instead of
    # discarding the whole account: dropping everything on one failed read is
    # what kept this layer at zero candles.  Only a surface with NO usable
    # trade is reported as a failure.
    if missing:
        logger.info("[MintChain] %s: %d/%d transactions unfetched (budget or "
                    "pruning) — keeping %d parsed trades",
                    label, missing, len(sigs),
                    sum(1 for r in results if r))
    # RPC signatures are newest-first; reverse before stable candle sorting
    # so within-second open/close follow provider order, not HTTP completion.
    trades = [t for t in reversed(results) if t]
    if sigs and not trades:
        return [], False  # nothing usable — do not claim this surface
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
            # Resolve the PumpSwap pool FIRST (one cheap DexScreener call):
            # on a graduated token the pool is where the trades are, and
            # resolving it after the curve scan burned the budget meant it
            # was never even attempted ("pool resolution failed: " with an
            # empty message = the remaining-budget check).
            pair = None
            try:
                from pumpfun_client import get_ds_pair_by_mint
                pair = await asyncio.wait_for(
                    get_ds_pair_by_mint(session, mint),
                    max(1.0, min(10.0, timeout_s * 0.1)))
                if not (pair and pair.get("pairAddress")
                        and pair.get("quoteToken", {}).get("address") == _WSOL_MINT):
                    if pair and pair.get("pairAddress"):
                        quote = (pair.get("quoteToken") or {}).get("symbol", "?")
                        logger.info(f"[MintChain] {mint[:8]}… pool skipped: "
                                    f"{quote}-quoted pair (WSOL only)")
                    pair = None
            except Exception as e:
                logger.info(f"[MintChain] pool resolution failed for "
                            f"{mint[:8]}…: {type(e).__name__}")

            # Both surfaces run CONCURRENTLY against the shared budget.  They
            # are disjoint in time (curve = pre-graduation, pool = post), and
            # serialising them let whichever ran first consume the whole
            # wall-clock allowance — the second surface then contributed
            # nothing on exactly the tokens that needed it.
            async def surface(account: str, parse, label: str):
                try:
                    return await _fetch_account_history(
                        session, rpc_pool, account, parse,
                        max_sigs, t0, timeout_s, label,
                        before_unix, lookback_seconds,
                    )
                except Exception as e:
                    logger.warning(f"[MintChain] {label} scan error for "
                                   f"{mint[:8]}…: {type(e).__name__}: {e}")
                    return [], False

            tasks = [surface(curve_pda,
                             lambda res: _parse_swap_tx(res, curve_pda),
                             "curve")]
            if pair:
                # DexScreener returns the CURRENT pair snapshot; a pool it no
                # longer lists can't be scanned — documented coverage caveat.
                tasks.append(surface(pair["pairAddress"],
                                     lambda res: _parse_pool_swap_tx(res, mint),
                                     "pool"))

            trades: list[dict] = []
            for label, (surface_trades, ok) in zip(
                    ["curve"] + (["pool"] if pair else []),
                    await asyncio.gather(*tasks)):
                if ok:
                    trades += surface_trades
                else:
                    logger.warning(
                        f"[MintChain] {mint[:8]}… {label} scan incomplete "
                        f"(budget/time) — dropping surface"
                    )

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
    if upper < time.time() - _RETENTION_SECONDS:
        logger.info(f"[MintChain] {mint[:8]}… cutoff is "
                    f"{(time.time() - upper) / 86400:.1f}d old — past provider "
                    f"retention, skipping chain fetch")
        return 0
    candles = await fetch_mint_history(mint, max_sigs=max_sigs,
                                       timeout_s=timeout_s,
                                       before_unix=upper,
                                       lookback_seconds=upper - lower)
    # Defense at the persistence boundary as well as within provider parsing.
    return persist_mint_history(mint, _within_bounds(candles, lower, upper),
                                db_path=db_path)
