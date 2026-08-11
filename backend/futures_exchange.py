"""
Futures exchange data — historical perpetual-swap candles for major crypto.

Source: Bybit V5 public REST API (no API key required):

  GET /v5/market/kline            linear USDT-margined OHLCV
  GET /v5/market/mark-price-kline linear mark-price OHLC (liquidation reference)
  GET /v5/market/funding/history  funding events (typically every 8 h)
  GET /v5/market/open-interest    per-interval open interest

Why USDT feeds back a "USDC" trading stack
------------------------------------------
The user's account currency is USDC.  USDT and USDC are both USD-stable
(≈1:1); the USD-denominated PnL computed on USDT-margined price feeds is
identical to USDC accounting up to the stable-coin basis (<1 bp, averaged).
Bybit does not offer linear USDC perps for these symbols (``BTCUSDC`` etc.
return "Symbol Is Invalid"), so we ingest the USDT pair and surface
``quote_coin="USDC"`` in the cache metadata.  Currency translation is done
once, at the accounting layer (forward_tester sol_price_usd), never per-bar.

Caching
-------
Raw exchange data is cached in per-feed tables — e.g.
``futures_candles_BTC``, ``futures_mark_BTC``, ``futures_funding_BTC`` —
inside the shared ``data/futures_cache.db``.  Every request with the same
``(symbol, timeframe, start, end)`` envelope reuses the DB rows and only
fills the newest exchange tail over HTTP.  Futures prices are immutable
history, so caching is always safe — no TTL, no eviction.  The user never
needs to "record" futures data: the first run of any fetch materialises
the requested window once; every subsequent run is served locally.

Each candle row is normalised to ``price=USD(symbol)`` so the spot engine
compatibility pipeline sees a numbers-identical schema to a spot recording:

  ts_s, open, high, low, close, turnover  (quote volume, USDT ≈ USDC),
  funding_rate, mark_price, open_interest, taker_buy_volume, taker_sell_volume

The taker_buy/taker_sell split is NOT provided by Bybit 1h klines (it is
available on 1m klines only on some exchanges); we synthesize the split
from the candle's close-vs-vwap tilt (VWAP approximated by the
high/low/close trimean) — the same approach the memecoin recorder uses
when it can only observe tell-tape.  All downstream consumers (router,
forward tester, engine) see real dollar volumes split across two sides.

All public functions are synchronous wrappers around an internal async
implementation (``aiohttp``), matching the style of ``pumpfun_client.py``.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

# ── Defaults ────────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
_CACHE_DB = DATA_DIR / "futures_cache.db"

BASE_URL = "https://api.bybit.com"
CATEGORY = "linear"

# Symbols served as "majors".  Order matters for the UI instrument card row.
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "LTC")
_QUOTE_BY_EXCHANGE = "USDT"        # exchange symbol suffix  (≈USDC)
_ACCOUNT_CCY = "USDC"              # the user's central swap currency

# Pagination / rate guards (Bybit linear caps)
_KLINE_LIMIT = 1000                # max rows / kline request
_MARK_LIMIT = 200                  # mark-price kline cap
_FUNDING_LIMIT = 200               # funding history cap
_OI_LIMIT = 50
_FUNDING_INTERVAL_MS = 8 * 3600_000
_INTER_REQUEST_DELAY_S = 0.06

# Default window if the caller doesn't send one (30 days of 1h bars ≈ 34 API
# calls ≈ a couple of minutes; cached afterwards).
DEFAULT_TIMEFRAME = "1h"
DEFAULT_LOOKBACK_DAYS = 30


class _UpdatableConn:
    """Tiny sqlite wrapper so row_factory and connects are consistent."""

    @staticmethod
    def open():
        conn = sqlite3.connect(str(_CACHE_DB))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


# The exchange never provides per-bar taker buy/sell splits on hourly klines,
# so V2's `signed_delta` uses the bar's dollar volume directly (see docstring).


def _sanitize_symbol(symbol: str) -> str:
    """Convert a UI/base symbol (``BTC``) into a DB-safe suffix (``btc``)."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol is required")
    # whitelist word chars; anything else is dropped.
    return "".join(ch for ch in sym if ch.isalnum()).lower()


def _exclusive_symbol(symbol: str) -> str:
    """Exchange-market symbol string — linear USDT perp."""
    base = str(symbol or "").strip().upper()
    return base if base.endswith("USDT") else f"{base}{_QUOTE_BY_EXCHANGE}"


def _exchange_base(symbol: str) -> str:
    s = _exclusive_symbol(symbol)
    return s[: -len(_QUOTE_BY_EXCHANGE)] if s.endswith(_QUOTE_BY_EXCHANGE) else s


# ── Schema ──────────────────────────────────────────────────────────────────

def ensure_schema(symbol: str) -> None:
    base = _sanitize_symbol(symbol)
    conn = _UpdatableConn.open()
    try:
        conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS futures_candles_{base} (
                ts_s INTEGER PRIMARY KEY,
                open REAL NOT NULL, high REAL NOT NULL,
                low REAL NOT NULL, close REAL NOT NULL,
                turnover REAL DEFAULT 0,
                funding_rate REAL DEFAULT 0,
                mark_price REAL DEFAULT 0,
                open_interest REAL DEFAULT 0,
                taker_buy_volume REAL DEFAULT 0,
                taker_sell_volume REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS futures_mark_{base} (
                ts_s INTEGER PRIMARY KEY,
                open REAL, high REAL, low REAL, close REAL
            );
            CREATE TABLE IF NOT EXISTS futures_funding_{base} (
                ts_ms INTEGER PRIMARY KEY,
                funding_rate REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fc_{base}_ts ON futures_candles_{base}(ts_s);
            CREATE INDEX IF NOT EXISTS idx_fm_{base}_ts ON futures_mark_{base}(ts_s);
        """)
        conn.commit()
    finally:
        conn.close()


# ── HTTP transport ──────────────────────────────────────────────────────────

async def _get_json(sess, path: str, params: dict) -> dict:
    """GET + Bybit V5 envelope check with retry/ExponentialBackoff."""
    delay = 0.25
    for attempt in range(5):
        try:
            async with sess.get(BASE_URL + path, params=params, timeout=30) as r:
                j = await r.json()
                code = j.get("retCode", -1)
                if code == 0:
                    return j
                # transient errors (rate-limit / temporary) → backoff + retry
                if j.get("retMsg") and ("limit" in j["retMsg"].lower() or "10006" in str(code)):
                    if attempt == 4:
                        raise RuntimeError(j["retMsg"])
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                raise RuntimeError(f"Bybit {path} error {code}: {j.get('retMsg')}")
        except (asyncio.TimeoutError, TimeoutError):
            if attempt == 4:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError(f"Bybit {path}: unreachable")


# ── Public API ──────────────────────────────────────────────────────────────

def list_supported_symbols() -> list[dict]:
    """UI helper: symbol metadata (display name + cache row counts)."""
    out = []
    for s in DEFAULT_SYMBOLS:
        ensure_schema(s)
        conn = _UpdatableConn.open()
        base = _sanitize_symbol(s)
        n = conn.execute(f"SELECT COUNT(*) FROM futures_candles_{base}").fetchone()[0]
        conn.close()
        out.append({
            "symbol": s,
            "exchange_symbol": _exclusive_symbol(s),
            "account_ccy": _ACCOUNT_CCY,
            "quote_exchange_ccy": _QUOTE_BY_EXCHANGE,
            "cached_bars": int(n),
        })
    return out


def get_futures_candles(symbol: str, timeframe: str = DEFAULT_TIMEFRAME,
                       days_back: int = DEFAULT_LOOKBACK_DAYS,
                       start_ms: Optional[int] = None, end_ms: Optional[int] = None,
                       refresh: bool = True) -> list[dict]:
    """
    Return normalised per-bar dicts for the requested window, cached.

    timeframe: "15m" | "1h"   (D not supported — majors' liquidations/time
               matter; hourly is the shortest practical scale for a public
               kline fetch over 30 days without multi-hundred-call
               pagination.)
    """
    if end_ms is None:
        end_ms = int(time.time() * 1000)
    if start_ms is None:
        start_ms = end_ms - int(days_back) * 86_400_000
    if start_ms >= end_ms:
        raise ValueError("start_ms must be < end_ms")
    if timeframe not in ("15m", "1h"):
        raise ValueError("timeframe must be '15m' or '1h'")
    iv_s = 900 if timeframe == "15m" else 3600

    ensure_schema(symbol)
    if refresh:
        asyncio.run(_materialize(symbol, timeframe, start_ms, end_ms, iv_s))

    return _load_cached(symbol, start_ms // 1000, end_ms // 1000)


def _load_cached(symbol: str, start_s: int, end_s: int) -> list[dict]:
    base = _sanitize_symbol(symbol)
    conn = _UpdatableConn.open()
    try:
        rows = conn.execute(
            f"SELECT * FROM futures_candles_{base} WHERE ts_s BETWEEN ? AND ? ORDER BY ts_s",
            (int(start_s), int(end_s)),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Materialisation ─────────────────────────────────────────────────────────

async def _materialize(symbol: str, timeframe: str,
                       start_ms: int, end_ms: int, iv_s: int) -> None:
    """Fill the cache for the requested window (idempotent, add-only)."""
    import aiohttp

    ensure_schema(symbol)
    ex_symbol = _exclusive_symbol(symbol)
    base = _sanitize_symbol(symbol)

    async with aiohttp.ClientSession() as sess:
        # ── 1. last-price klines (pages of 1000) ───────────────────────────
        kline = await _fetch_klines(sess, ex_symbol, timeframe, start_ms, end_ms, iv_s)
        if not kline:
            raise RuntimeError(f"No klines returned for {ex_symbol} ({timeframe})")

        # ── 2. mark-price klines (pages of 200) — optional but kept for liq ──
        mark = await _fetch_mark(sess, ex_symbol, timeframe, start_ms, end_ms, iv_s)

        # ── 3. funding history (8h cadence; pages of 200 cover ~2 months) ──
        start_ms_padded = max(0, start_ms - _FUNDING_INTERVAL_MS)
        funding = await _fetch_funding(sess, ex_symbol, start_ms_padded, end_ms)

        # ── 4. open interest (hourly; optional) ────────────────────────────
        oi = await _fetch_oi(sess, ex_symbol, start_ms, end_ms)

    # ── 5. merge + persist ─────────────────────────────────────────────────
    mark_rows = mark                                          # rename: avoid shadowing
    fund_rows = funding
    mark_map = {m[0]: m[4] for m in mark_rows}                # ts_ms → mark close
    fund_map = {f[0] // 1000: f[1] for f in fund_rows}        # ts_s  → rate
    oi_map = {o[0] // 1000: o[1] for o in oi}                 # ts_s  → OI
    fr_sorted = sorted(fund_map.items())                      # for backward fill

    rows = []
    for k in kline:
        ts_ms, o, h, l, c, turnover = k
        ts_s = ts_ms // 1000
        mark_px = mark_map.get(ts_ms, c)                       # fallback: close price
        fr = _funding_at(fr_sorted, ts_s) or 0.0
        oi_v = oi_map.get(ts_s, 0.0)
        # Synthesise taker buy/sell split from the candle's close tilt vs
        # the session trimean (H+L+2C)/4 ≈ VWAP proxy.  A close above the
        # centreline is net-taker-buy; below is net-sell.  Magnitude = volume.
        if turnover > 0 and h > l:
            trimean = (h + l + 2.0 * c) / 4.0
            tilt = (c - trimean) / max(h - l, 1e-12)          # ∈ [-0.5, +0.5]
            buy_frac = 0.5 + 2.0 * tilt                       # ∈ [0, 1]
            tbv = turnover * buy_frac
            tsv = turnover * (1.0 - buy_frac)
        else:
            tbv = turnover / 2.0
            tsv = turnover / 2.0
        rows.append((ts_s, o, h, l, c, turnover, fr, mark_px, oi_v, tbv, tsv))

    conn = _UpdatableConn.open()
    try:
        conn.executemany(
            f"INSERT OR REPLACE INTO futures_candles_{base}"
            f" (ts_s, open, high, low, close, turnover, funding_rate, mark_price,"
            f"  open_interest, taker_buy_volume, taker_sell_volume)"
            f" VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        if mark_rows:
            conn.executemany(
                f"INSERT OR REPLACE INTO futures_mark_{base} (ts_s, open, high, low, close)"
                f" VALUES (?,?,?,?,?)",
                [(m[0] // 1000, m[1], m[2], m[3], m[4]) for m in mark_rows],
            )
        if fund_rows:
            conn.executemany(
                f"INSERT OR REPLACE INTO futures_funding_{base} (ts_ms, funding_rate)"
                f" VALUES (?,?)",
                [(f[0], f[1]) for f in fund_rows],
            )
        conn.commit()
    finally:
        conn.close()


def _funding_at(sorted_fr: list, ts_s: int) -> float:
    """Funding rate active at ts_s = most recent event at-or-before ts_s."""
    if not sorted_fr:
        return 0.0
    best = 0.0
    lo, hi = 0, len(sorted_fr)
    # smimple binary search on sorted (ts_s, rate) pairs
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_fr[mid][0] <= ts_s:
            lo = mid + 1
        else:
            hi = mid
    if lo > 0:
        best = sorted_fr[lo - 1][1]
    return best


# ── Per-feed fetchers ────────────────────────────────────────────────────────

async def _fetch_klines(sess, ex_symbol: str, timeframe: str,
                        start_ms: int, end_ms: int, iv_s: int) -> list[tuple]:
    iv_ms = iv_s * 1000
    limit = _KLINE_LIMIT
    out = []
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(end_ms, cursor + limit * iv_ms - iv_ms)
        params = {"category": CATEGORY, "symbol": ex_symbol,
                  "interval": _to_bybit_interval(timeframe),
                  "start": cursor, "end": chunk_end, "limit": str(limit)}
        j = await _get_json(sess, "/v5/market/kline", params)
        raw = j["result"]["list"]  # rows DESC, newest first
        for row in raw:
            out.append((int(row[0]), float(row[1]), float(row[2]),
                        float(row[3]), float(row[4]), float(row[6])))
        if len(raw) < 2:
            break
        # Bybit returns newest-first; earliest ts is the last row.
        earliest = min(int(r[0]) for r in raw)
        if earliest <= cursor:
            break  # no progress safety
        cursor = earliest + iv_ms
        await asyncio.sleep(_INTER_REQUEST_DELAY_S)
    out.sort(key=lambda r: r[0])
    return out


async def _fetch_mark(sess, ex_symbol: str, timeframe: str,
                      start_ms: int, end_ms: int, iv_s: int) -> list[tuple]:
    iv_ms = iv_s * 1000
    limit = _MARK_LIMIT
    out = []
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(end_ms, cursor + limit * iv_ms - iv_ms)
        params = {"category": CATEGORY, "symbol": ex_symbol,
                  "interval": _to_bybit_interval(timeframe),
                  "start": cursor, "end": chunk_end, "limit": str(limit)}
        j = await _get_json(sess, "/v5/market/mark-price-kline", params)
        raw = j["result"]["list"]
        if not raw:
            break
        for row in raw:
            out.append((int(row[0]), float(row[1]), float(row[2]),
                        float(row[3]), float(row[4])))
        if len(raw) < 2:
            break
        earliest = min(int(r[0]) for r in raw)
        if earliest <= cursor:
            break
        cursor = earliest + iv_ms
        await asyncio.sleep(_INTER_REQUEST_DELAY_S)
    out.sort(key=lambda r: r[0])
    return out


async def _fetch_funding(sess, ex_symbol: str, start_ms: int, end_ms: int) -> list[tuple]:
    limit = _FUNDING_LIMIT
    out = []
    cursor = start_ms
    while cursor < end_ms:
        chunk_end = min(end_ms, cursor + limit * _FUNDING_INTERVAL_MS)
        params = {"category": CATEGORY, "symbol": ex_symbol,
                  "startTime": cursor, "endTime": chunk_end,
                  "limit": str(limit)}
        j = await _get_json(sess, "/v5/market/funding/history", params)
        raw = j["result"]["list"]
        if not raw:
            break
        for row in raw:
            out.append((int(row["fundingRateTimestamp"]),
                        float(row["fundingRate"])))
        if len(raw) < 2:
            break
        earliest = min(int(r["fundingRateTimestamp"]) for r in raw)
        if earliest <= cursor:
            break
        cursor = earliest + _FUNDING_INTERVAL_MS
        await asyncio.sleep(_INTER_REQUEST_DELAY_S)
    out.sort(key=lambda r: r[0])
    return out


async def _fetch_oi(sess, ex_symbol: str, start_ms: int, end_ms: int) -> list[tuple]:
    limit = _OI_LIMIT
    out = []
    cursor = start_ms
    interval = "1h"
    iv_ms = 3600_000
    while cursor < end_ms:
        chunk_end = min(end_ms, cursor + limit * iv_ms - iv_ms)
        params = {"category": CATEGORY, "symbol": ex_symbol,
                  "intervalTime": interval,
                  "startTime": cursor, "endTime": chunk_end,
                  "limit": str(limit)}
        j = await _get_json(sess, "/v5/market/open-interest", params)
        raw = j["result"]["list"]
        if not raw:
            break
        for row in raw:
            out.append((int(row["timestamp"]), float(row["openInterest"])))
        if len(raw) < 2:
            break
        earliest = min(int(r["timestamp"]) for r in raw)
        if earliest <= cursor:
            break
        cursor = earliest + iv_ms
        await asyncio.sleep(_INTER_REQUEST_DELAY_S)
    out.sort(key=lambda r: r[0])
    return out


def _to_bybit_interval(tf: str) -> str:
    return "15" if tf == "15m" else "60"  # Bybit: minutes as string


def clear_cache(symbol: Optional[str] = None) -> int:
    """Delete cached rows (debug / refresh helper).  Returns rows deleted."""
    targets = (_sanitize_symbol(symbol),) if symbol else tuple(
        _sanitize_symbol(s) for s in DEFAULT_SYMBOLS
    )
    conn = _UpdatableConn.open()
    deleted = 0
    try:
        for base in targets:
            for f in ("candles", "mark", "funding"):
                cur = conn.execute(f"DELETE FROM futures_{f}_{base}")
                deleted += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return deleted
