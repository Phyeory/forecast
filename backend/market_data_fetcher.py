"""
Market Data Fetcher
===================
Downloads OHLCV candle data from Yahoo Finance for all supported assets:
  - Index Futures  : ES=F (E-mini S&P 500), NQ=F (E-mini Nasdaq 100)
  - Large-Cap Crypto: BTC-USD, SOL-USD  (spot prices on Yahoo Finance)
  - High-Vol Equities: TSLA, NVDA

All assets use a single unified yfinance path — no external API keys required,
no Binance network restrictions, no SSL certificate issues.

Converts candles into the existing recordings DB format so they can be
backtested immediately with the existing StrategyEngine / ForwardTester
pipeline, without any schema changes.
"""

from __future__ import annotations
import time
import logging
from typing import Optional

logger = logging.getLogger("market-data")


# ── Asset catalogue ──────────────────────────────────────────────────────────

ASSET_CATALOGUE: dict[str, dict] = {
    # Index Futures (Yahoo Finance)
    "ES=F": {
        "name": "E-mini S&P 500 Future",
        "symbol": "ES",
        "yf_ticker": "ES=F",
        "category": "Index Futures",
        "description": "E-mini S&P 500 — CME futures contract",
    },
    "NQ=F": {
        "name": "E-mini Nasdaq 100 Future",
        "symbol": "NQ",
        "yf_ticker": "NQ=F",
        "category": "Index Futures",
        "description": "E-mini Nasdaq 100 — CME futures contract",
    },
    # Large-Cap Crypto (Yahoo Finance spot prices)
    "BTC-USD": {
        "name": "Bitcoin / USD",
        "symbol": "BTC",
        "yf_ticker": "BTC-USD",
        "category": "Crypto Perpetuals",
        "description": "Bitcoin spot price (Yahoo Finance)",
    },
    "SOL-USD": {
        "name": "Solana / USD",
        "symbol": "SOL",
        "yf_ticker": "SOL-USD",
        "category": "Crypto Perpetuals",
        "description": "Solana spot price (Yahoo Finance)",
    },
    # High-Volume Equities (Yahoo Finance)
    "TSLA": {
        "name": "Tesla Inc.",
        "symbol": "TSLA",
        "yf_ticker": "TSLA",
        "category": "Equities",
        "description": "Tesla Inc. — NASDAQ equity",
    },
    "NVDA": {
        "name": "NVIDIA Corporation",
        "symbol": "NVDA",
        "yf_ticker": "NVDA",
        "category": "Equities",
        "description": "NVIDIA Corporation — NASDAQ equity",
    },
}

# ── Timeframe mappings ───────────────────────────────────────────────────────

_YF_INTERVAL: dict[str, str] = {
    "1m":  "1m",
    "5m":  "5m",
    "15m": "15m",
    "1h":  "1h",
    "1d":  "1d",
}

_YF_PERIOD: dict[str, str] = {
    "1m":  "7d",    # yfinance max 7 days for 1-min data
    "5m":  "60d",
    "15m": "60d",
    "1h":  "730d",
    "1d":  "5y",
}


# ── Yahoo Finance v8 direct fetcher ──────────────────────────────────────────
# We call Yahoo's chart API directly with requests (verify=False) instead of
# using the yfinance library, because yfinance ≥0.2.38 uses curl_cffi for its
# cookie/crumb handshake which ignores requests.Session and fails on networks
# with TLS-inspection proxies (corporate VPNs, etc.).

import random as _random

_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
]

# Yahoo v8 chart API — range strings that cover enough history per timeframe
_YF_RANGE: dict[str, str] = {
    "1m":  "7d",    # max 7 days for 1-min
    "5m":  "60d",
    "15m": "60d",
    "1h":  "730d",
    "1d":  "5y",
}

_YF_BASE_URLS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]


def _make_session() -> "requests.Session":
    import requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "User-Agent": _random.choice(_UA_POOL),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def _fetch_yfinance(yf_ticker: str, timeframe: str, lookback_candles: int) -> list[dict]:
    """Fetch OHLCV candles from Yahoo Finance v8 chart API directly (no yfinance library)."""
    interval = _YF_INTERVAL.get(timeframe)
    if interval is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    range_str = _YF_RANGE.get(timeframe, "60d")

    last_err: Exception | None = None
    for base_url in _YF_BASE_URLS:
        url = f"{base_url}/v8/finance/chart/{yf_ticker}"
        params = {
            "interval":           interval,
            "range":              range_str,
            "includePrePost":     "false",
            "events":             "div,splits",
            "corsDomain":         "finance.yahoo.com",
        }
        logger.info(f"[Yahoo] GET {url} interval={interval} range={range_str}")
        try:
            session = _make_session()
            resp = session.get(url, params=params, timeout=20)
            if resp.status_code == 429:
                raise RuntimeError(f"Rate-limited (429) by Yahoo Finance")
            if resp.status_code != 200:
                raise RuntimeError(f"Yahoo Finance returned HTTP {resp.status_code}")
            data = resp.json()
            break
        except Exception as e:
            last_err = e
            logger.warning(f"[Yahoo] {base_url} failed: {e}")
            continue
    else:
        raise RuntimeError(f"All Yahoo Finance endpoints failed. Last error: {last_err}")

    # Parse chart result
    try:
        result = data["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        err = data.get("chart", {}).get("error", {})
        raise ValueError(f"Yahoo Finance API error for {yf_ticker}: {err}")

    timestamps = result.get("timestamp", [])
    ohlcv      = result.get("indicators", {}).get("quote", [{}])[0]
    opens      = ohlcv.get("open",   [])
    highs      = ohlcv.get("high",   [])
    lows       = ohlcv.get("low",    [])
    closes     = ohlcv.get("close",  [])
    volumes    = ohlcv.get("volume", [])

    if not timestamps or not closes:
        raise ValueError(f"No OHLCV data in Yahoo Finance response for {yf_ticker}")

    candles: list[dict] = []
    for i, ts in enumerate(timestamps):
        try:
            o   = float(opens[i])   if i < len(opens)   and opens[i]   is not None else 0.0
            h   = float(highs[i])   if i < len(highs)   and highs[i]   is not None else 0.0
            l   = float(lows[i])    if i < len(lows)    and lows[i]    is not None else 0.0
            c   = float(closes[i])  if i < len(closes)  and closes[i]  is not None else 0.0
            vol = float(volumes[i]) if i < len(volumes) and volumes[i] is not None else 0.0
        except (TypeError, ValueError):
            continue

        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            continue

        candles.append({"time": int(ts), "open": o, "high": h, "low": l, "close": c, "volume": vol})

    candles = candles[-lookback_candles:]
    logger.info(f"[Yahoo] Got {len(candles)} candles for {yf_ticker}")
    return candles


# ── Public API ───────────────────────────────────────────────────────────────



def fetch_market_candles(
    asset_key: str,
    timeframe: str = "5m",
    lookback_candles: int = 500,
) -> list[dict]:
    """
    Fetch OHLCV candles for the given asset_key.
    Returns list of {time, open, high, low, close, volume}.
    """
    info = ASSET_CATALOGUE.get(asset_key)
    if info is None:
        raise ValueError(f"Unknown asset: {asset_key}. Choose from: {list(ASSET_CATALOGUE)}")
    if timeframe not in _YF_INTERVAL:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    lb = lookback_candles or 500
    return _fetch_yfinance(info["yf_ticker"], timeframe, lb)


def create_market_recording(
    asset_key: str,
    timeframe: str = "5m",
    lookback_candles: int = 500,
) -> dict:
    """
    Fetch market candles and save them to the recordings DB as a completed
    recording. Returns a summary dict including the recording_id.
    """
    import data_store

    info = ASSET_CATALOGUE.get(asset_key)
    if info is None:
        raise ValueError(f"Unknown asset: {asset_key}")

    candles = fetch_market_candles(asset_key, timeframe, lookback_candles)
    if not candles:
        raise ValueError(f"No candles returned for {asset_key} on {timeframe}")

    rec_id = data_store.create_recording(
        mint=asset_key,
        timeframe=timeframe,
        token_name=info["name"],
        token_symbol=info["symbol"],
    )
    data_store.insert_candles_batch(rec_id, candles)
    data_store.stop_recording(rec_id)

    return {
        "recording_id":  rec_id,
        "asset_key":     asset_key,
        "name":          info["name"],
        "symbol":        info["symbol"],
        "category":      info["category"],
        "timeframe":     timeframe,
        "candle_count":  len(candles),
    }
