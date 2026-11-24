"""Bounded public-chain history for pre-session calibration.

Callers must freeze ``before_unix`` at session start / the first replay candle.
Only complete seconds in [ceil(before - lookback), floor(before)) are retained;
the current wall-clock second is also excluded. Defaults freeze now once and
use a 6000-second lookback. Signature cursors walk backwards from the provider's
head to the requested historical window, independently of the transaction cap.

Coverage is best effort, NOT a claim of complete mint history: public RPCs may
prune history, and today's DexScreener address discovery may miss retired pools.
An account scan with an internal GAP (failed page/transaction, unknown block
Time, repeated cursor) contributes no candles.  A window with more transactions
than ``_MAX_SIGS`` is TRUNCATED at its left edge (the newest transactions are
kept) instead of being dropped — a left-edge truncation is not a gap, and hot
mints otherwise yielded nothing at all. Other successfully scanned accounts may
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

# Healthy public endpoints only.  rpc.ankr.com/solana was removed 2026-11-23:
# it answers every call with 403 ("API key is not allowed to access
# blockchain"), and the old pool treated ANY failed leg as a failed scan, so
# a single dead endpoint silently zeroed every chain calibration fetch
# (129/138 live sessions recorded chain_inserted=0).  `_rpc_call` now fails
# over across the list, so one bad endpoint can never fail the scan.
_SOLANA_RPCS = [
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]

# Public RPCs prune signatures (~2 days).  A calibration cutoff older than
# this cannot be reconstructed from chain history — callers fall through to
# the DB/population layers instead of walking hundreds of empty pages.
_RETENTION_SECONDS = 2 * 24 * 3600.0

# PumpSwap protocol virtual-quote-reserves fallback (measured constant).
# Only used when the per-pool resolver is unavailable; log-return statistics
# are invariant to this additive quote offset.
_PUMPSWAP_VR_FALLBACK_SOL = 17.584505637

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
    """Round-robin over the endpoint list with spacing and health cooldowns.

    A leg that answers 403/429/5xx is cooled down instead of being retried on
    every subsequent call: public RPCs have wildly different capabilities
    (e.g. api.mainnet-beta rate-limits getTransaction to a trickle), and a
    scan of hundreds of transactions must not waste half its attempts on the
    throttled leg.
    """

    def __init__(self, endpoints: list[str], min_interval: float = 0.08):
        self.endpoints = list(endpoints)
        self.min_interval = min_interval
        self._idx = 0
        self._last: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self._cooldown: dict[str, float] = {}

    def next(self):
        now = time.time()
        # Prefer the FIRST healthy leg and spill onto the others only while it
        # is cooling down.  Round-robin sent half of every scan to
        # api.mainnet-beta — the SAME host that carries the shared price
        # WebSocket hub (SOLANA_RPC_WS) — so the scan's traffic rate-limited
        # the provider by IP and the hub's reconnect was rejected with 429
        # ("server rejected WebSocket connection: HTTP 429"), starving every
        # live session.  Chain calibration must stay on the fetch leg.
        for ep in self.endpoints:
            if now >= self._cooldown.get(ep, 0.0):
                wait = self.min_interval - (now - self._last.get(ep, 0.0))
                if wait > 0:
                    # Endpoint is healthy but still spacing; another leg may
                    # be ready sooner — pick the earliest-ready healthy one.
                    best, best_wait = ep, wait
                    for other in self.endpoints:
                        if other is ep or now < self._cooldown.get(other, 0.0):
                            continue
                        w = self.min_interval - (now - self._last.get(other, 0.0))
                        if w < best_wait:
                            best, best_wait = other, w
                    return best, max(best_wait, 0.0)
                return ep, 0.0
        # Every leg is cooling down: retry the one that recovers soonest.
        ep = min(self.endpoints, key=lambda e: self._cooldown.get(e, 0.0))
        return ep, max(0.0, self._cooldown.get(ep, 0.0) - now)

    def mark(self, ep: str) -> None:
        self._last[ep] = time.time()

    def mark_success(self, ep: str) -> None:
        self._fails.pop(ep, None)
        self._cooldown.pop(ep, None)

    def mark_failure(self, ep: str, backoff: float = 2.0) -> None:
        n = self._fails.get(ep, 0) + 1
        self._fails[ep] = n
        self._cooldown[ep] = time.time() + min(backoff * n, 30.0)


# Distinct from a successful empty result: `_RPC_FAILED` means every
# endpoint failed, `None` means a caller-imposed timeout.  Scans treat both
# as "no data", but only a failed call may indicate endpoint health.
_RPC_FAILED = object()


async def _rpc_call(session: aiohttp.ClientSession, pool: _EndpointPool,
                    method: str, params: list):
    """Call ``method``, failing over across every endpoint once.

    Returns the RPC ``result`` on success, ``_RPC_FAILED`` when no endpoint
    produced a result.  A dead leg (403/429/network error/JSON-RPC error)
    must never poison a scan — the Sep-18/Nov-20 regression where one broken
    endpoint silently dropped every historical surface.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    attempts = max(1, len(pool.endpoints))
    for _ in range(attempts):
        ep, wait = pool.next()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with session.post(
                    ep, json=payload,
                    timeout=aiohttp.ClientTimeout(total=_TX_TIMEOUT)) as r:
                pool.mark(ep)
                if r.status == 429:
                    pool.mark_failure(ep)
                    await asyncio.sleep(0.2)
                    continue
                r.raise_for_status()
                data = await r.json(content_type=None)
                if "error" in data:
                    pool.mark_failure(ep)
                    continue
                pool.mark_success(ep)
                return data.get("result")
        except Exception:
            pool.mark(ep)
            pool.mark_failure(ep)
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
                "sol": abs(sol_delta), "side": side,
                # curve SOL reserve = the same `pool_sol` live recordings
                # persist (liquidity estimators read it)
                "pool_sol": sol_post}
    except Exception:
        return None


_WSOL_MINT = "So11111111111111111111111111111111111111112"


def _parse_pool_swap_tx(tx: dict, token_mint: str) -> Optional[dict]:
    """Extract a PumpSwap pool trade from the POOL-OWNED vault balances.

    Post-graduation price is the executable basis (AGENTS invariant 11):
        price = (quote_vault_post + V) / base_vault_post
    with V = the pool's virtual quote reserves.  `pool_sol` is the raw quote
    vault, exactly what the live recorder persists.

    ``pre/postTokenBalances`` contain several entries per mint (the pool's
    vaults AND every taker's ATAs).  Keying them by mint alone — the pre-fix
    behaviour — picked whichever entry came last, so prices were computed
    from a random wallet's balances (~4000x wrong).

    The pool is self-identified from the transaction: it is the one
    NON-SIGNER account that owns both the token and the WSOL balance entries
    in pre AND post.  Excluding signers removes every taker (buyer or seller
    carries both ATAs).  Ambiguity (0 or >1 candidates) fails closed rather
    than emitting garbage physics.
    """
    try:
        block_time = tx.get("blockTime")
        meta = tx.get("meta")
        if not block_time or not meta:
            return None

        keys = tx["transaction"]["message"]["accountKeys"]
        signers = {
            (k["pubkey"] if isinstance(k, dict) else k)
            for k in keys
            if isinstance(k, dict) and k.get("signer")
        }

        pre_entries = meta.get("preTokenBalances") or []
        post_entries = meta.get("postTokenBalances") or []

        def _owners(entries, mint):
            return {tb.get("owner") for tb in entries
                    if tb.get("mint") == mint and tb.get("owner")}

        candidates = (_owners(pre_entries, token_mint)
                      & _owners(pre_entries, _WSOL_MINT)
                      & _owners(post_entries, token_mint)
                      & _owners(post_entries, _WSOL_MINT)) - signers
        if len(candidates) != 1:
            return None
        pool = next(iter(candidates))

        def _owned(entries, mint):
            for tb in entries:
                if tb.get("mint") == mint and tb.get("owner") == pool:
                    return tb
            return None

        def amt(entry):
            return float(entry["uiTokenAmount"]["amount"]) / (
                10 ** int(entry["uiTokenAmount"].get("decimals", 9)))

        pre_tok, post_tok = _owned(pre_entries, token_mint), _owned(post_entries, token_mint)
        pre_wsol, post_wsol = _owned(pre_entries, _WSOL_MINT), _owned(post_entries, _WSOL_MINT)
        if not (pre_tok and post_tok and pre_wsol and post_wsol):
            return None  # USDC-quoted / exotic pair / not this pool — skip

        tok_pre, tok_post = amt(pre_tok), amt(post_tok)
        wsol_pre, wsol_post = amt(pre_wsol), amt(post_wsol)
        tok_delta = tok_post - tok_pre   # pool side: + = user sold
        wsol_delta = wsol_post - wsol_pre
        if abs(tok_delta) < 1.0 or abs(wsol_delta) < 1e-9:
            return None
        if tok_post <= 0 or wsol_post <= 0:
            return None
        price = (wsol_post + _pool_v_sol(token_mint)) / tok_post
        side = "sell" if tok_delta > 0 else "buy"
        return {"time": int(block_time), "price": price,
                "sol": abs(wsol_delta), "side": side,
                "pool_sol": wsol_post}
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
                        "volume": 0.0, "buy_volume": 0.0, "sell_volume": 0.0,
                        "pool_sol": float(t.get("pool_sol", 0.0) or 0.0)}
            c = out[sec]
        c["high"] = max(c["high"], t["price"])
        c["low"] = min(c["low"], t["price"])
        c["close"] = t["price"]
        c["volume"] += t["sol"]
        if t.get("pool_sol"):
            c["pool_sol"] = float(t["pool_sol"])
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
    coverage could not be PROVEN without a gap — page/time budget tripped, an
    entry with unknown blockTime, a repeated cursor, or a page/transaction
    call failing.  Callers then treat the account as "no data this run"
    rather than persisting an ambiguous partial window.  The ``max_sigs`` cap
    is NOT a gap: it truncates the oldest end (newest-first walk) and stops.
    Failed calls (``_RPC_FAILED``/``None``) differ from empty pages ([]).
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
        if page is _RPC_FAILED or page is None:
            break  # failure/timeout, not an empty successful page.
        if not isinstance(page, list):
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
                sigs.append(sig)
            seen.add(sig)
        if len(sigs) >= max_sigs:
            # Newest-first walk: keep the newest max_sigs and stop.  The cap
            # is a LEFT-EDGE truncation (no internal gaps), never a dropped
            # account — an active pump.fun mint prints thousands of sigs in
            # minutes, and discarding the surface meant chain calibration
            # never yielded anything for exactly the tokens worth trading.
            sigs = sigs[:max_sigs]
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
    # Which signatures had their transaction SUCCESSFULLY fetched (a parsed
    # trade may still be None: non-swap tx, or a tx outside the window).
    # Needed to cut a gap-free newest prefix when the budget runs out.
    fetched: list[bool] = [False] * len(sigs)
    budget_exhausted = False

    async def grab(i: int, sig: str):
        nonlocal budget_exhausted
        async with sem:
            res = None
            # Transient 429s are common under concurrency; a single failed
            # fetch must not discard the whole account (537/539 succeeded on
            # the Nov-23 probes and the surface was still dropped).  Retry
            # the individual transaction, then fail closed on a real gap.
            for attempt in range(3):
                if time.time() - t0 > timeout_s:
                    budget_exhausted = True
                    return
                res = await call(
                    "getTransaction",
                    [sig, {"encoding": "jsonParsed",
                           # v1 transactions exist on current mainnet; asking
                           # for version 0 rejects them with -32015 and
                           # silently dropped part of every fetched window.
                           "maxSupportedTransactionVersion": 1,
                           "commitment": "finalized"}],
                )
                if res is not _RPC_FAILED and res and res.get("blockTime") is not None:
                    break
                await asyncio.sleep(0.25 * (attempt + 1))
            else:
                budget_exhausted = True
                return
            fetched[i] = True
            if lower <= res["blockTime"] < upper_eff:
                results[i] = parse_fn(res)

    await asyncio.gather(*(grab(i, s) for i, s in enumerate(sigs)))
    if budget_exhausted:
        # Wall-clock budget ran out mid-fetch.  The old behaviour discarded
        # everything, so a busy token (1000 in-window signatures + provider
        # 429 cooldowns) spent the full 90 s and STILL calibrated from
        # population.  Keep the NEWEST gap-free prefix instead: the signature
        # walk is newest-first, so stopping at the first transaction that was
        # not fetched yields a contiguous recent surface — exactly the tail
        # the estimator wants (MIN_MINT_CANDLES=120).  Only a total loss
        # still fails closed.
        keep = 0
        for i in range(len(sigs)):
            if not fetched[i]:
                break
            keep = i + 1
        partial = [t for t in reversed(results[:keep]) if t]
        if not partial:
            return [], False
        logger.info("[MintChain] %s kept newest %d/%d transactions after "
                    "budget exhaustion — partial surface", label, keep, len(sigs))
        return _within_bounds(partial, lower, upper_eff), True
    # RPC signatures are newest-first; reverse before stable candle sorting
    # so within-second open/close follow provider order, not HTTP completion.
    trades = [t for t in reversed(results) if t]
    # Trades carry raw blockTime; keep only complete, in-window seconds.
    return _within_bounds(trades, lower, upper_eff), True


_V_MEMO: dict[str, float] = {}


def _pool_v_sol(mint: str) -> float:
    """Virtual quote reserves for the pool surface (offline, memoized).

    The sidecar written by pool_virtual_reserves is read once per mint per
    process; the measured protocol constant is the fallback.  Log-return
    statistics are invariant to this additive quote offset, so a fallback can
    never distort calibration — only the recorded price level.
    """
    if mint in _V_MEMO:
        return _V_MEMO[mint]
    v = _PUMPSWAP_VR_FALLBACK_SOL
    try:
        from pool_virtual_reserves import _sidecar_load
        info = _sidecar_load(mint)
        if info is not None:
            v = float(info.get("v_sol") or 0.0)
    except Exception:
        pass
    _V_MEMO[mint] = v
    return v


async def fetch_mint_history(mint: str, max_sigs: int = _MAX_SIGS,
                             endpoints: Optional[list[str]] = None,
                             timeout_s: float = 90.0,
                             before_unix: Optional[float] = None,
                             lookback_seconds: float =
                             _DEFAULT_LOOKBACK_SECONDS,
                             v_sol: Optional[float] = None) -> list[dict]:
    """Fetch a bounded historical slice of the mint's on-chain trade history.

    Two surfaces, merged (disjoint in time across the token's life):
      * bonding-curve PDA — pre-graduation trades (reserves-formula price)
      * PumpSwap pool     — post-graduation trades (executable basis
        (quote_vault+V)/base_vault from the POOL-OWNED vaults; pool resolved
        via DexScreener, WSOL-quoted pairs only)

    Candles are complete seconds in [ceil(before - lookback), floor(before)),
    where ``before`` defaults to (frozen) now.  Pagination walks backwards
    through newer signatures so a historical cutoff still reaches past data.
    Accounts with detected gaps (RPC/page/time failures) are dropped; a
    ``max_sigs`` overrun truncates the oldest end instead.
    Successful scans still only cover provider-visible, recognized swaps;
    the existing transaction parsers are not a full transaction indexer.

    Returns candles sorted by time; [] when no account supplies usable data.
    Invalid bounds raise ValueError. Persisted by fetch_and_persist.
    """
    lower, upper = _history_bounds(before_unix, lookback_seconds)
    if lower >= upper or max_sigs <= 0 or timeout_s <= 0:
        return []
    if v_sol is not None:
        try:
            _V_MEMO[mint] = max(0.0, float(v_sol))
        except (TypeError, ValueError):
            pass
    # Equivalent integer window passed to all account scans, frozen once.
    before_unix, lookback_seconds = upper, upper - lower
    curve_pda = _bonding_curve_pda(mint)
    if not curve_pda:
        return []
    eps = endpoints or _SOLANA_RPCS
    rpc_pool = _EndpointPool(eps, min_interval=0.08)
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
            #    under the documented coverage caveats.  The FIRST Solana pair
            #    can belong to another DEX (Meteora/Raydium); scanning that
            #    with the PumpSwap parser yielded garbage, so the pumpswap
            #    pair is selected explicitly.
            try:
                from pumpfun_client import get_ds_pair_by_mint
                remaining = timeout_s - (time.time() - t0)
                if remaining <= 0:
                    raise asyncio.TimeoutError
                def _pump_wsol(p) -> bool:
                    return bool(
                        p and p.get("pairAddress")
                        and p.get("dexId", "pumpswap") == "pumpswap"
                        and p.get("quoteToken", {}).get("address") == _WSOL_MINT)

                pair = await asyncio.wait_for(
                    get_ds_pair_by_mint(session, mint), remaining)
                if not _pump_wsol(pair):
                    try:
                        from pumpfun_client import get_ds_pairs_by_mints
                        remaining = timeout_s - (time.time() - t0)
                        if remaining <= 0:
                            raise asyncio.TimeoutError
                        pairs = (await asyncio.wait_for(
                            get_ds_pairs_by_mints([mint]), remaining)).get(mint) or []
                        pair = next((p for p in pairs if _pump_wsol(p)), None)
                    except Exception:
                        pair = None
                if _pump_wsol(pair):
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
    # Provider retention: a cutoff older than the RPC's pruning horizon can
    # only return empty pages.  Callers fall through to the DB/population
    # layers (and, for replay, to the tick-0 calibration snapshot).
    if upper < time.time() - _RETENTION_SECONDS:
        logger.info(f"[MintChain] {mint[:8]}… cutoff {upper} is past provider "
                    f"retention ({_RETENTION_SECONDS/86400:.0f}d) — skipping fetch")
        return 0
    candles = await fetch_mint_history(mint, max_sigs=max_sigs,
                                       timeout_s=timeout_s,
                                       before_unix=upper,
                                       lookback_seconds=upper - lower)
    # Defense at the persistence boundary as well as within provider parsing.
    return persist_mint_history(mint, _within_bounds(candles, lower, upper),
                                db_path=db_path)
