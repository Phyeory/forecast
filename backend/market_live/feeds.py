"""feeds.py — closed-bar feeds for live trading.

Semantics that matter
---------------------
The engine only ever sees **closed** bars, exactly like the backtester.  A bar
is closed when the wall clock passes its interval end (plus a small grace).
For daily US equities that is 16:00 ET; for 1h bars it is :00 + grace.

Feeds
-----
TapeFeed     local `market_data.db` replay (paper/parity/backfill testing)
YahooFeed    keyless Yahoo chart API (daily + 1h intraday), incremental
AlpacaFeed   Alpaca market data (optional; needs APCA_API_KEY_ID/SECRET)

All feeds return candle dicts in the same shape the backtester consumes:
time, open, high, low, close, volume, buy_volume, sell_volume.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from market_backtest.market_store import (
    get_market_recording, get_market_recording_candles,
    init_market_db, insert_market_candles_batch, upsert_market_recording,
    set_recording_bounds)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# interval -> (yahoo range, yahoo interval)
YF_RANGE = {
    "1d": ("10y", "1d"),
    "1h": ("730d", "1h"),
    "30m": ("60d", "30m"),
    "15m": ("60d", "15m"),
    "5m": ("60d", "5m"),
}


def _split_buy_sell(o: float, h: float, l: float, c: float, v: float) -> tuple[float, float]:
    if h > l:
        frac = 0.5 + 0.45 * ((c - o) / (h - l))
    else:
        frac = 0.5
    frac = min(0.95, max(0.05, frac))
    return v * frac, v * (1.0 - frac)


def yahoo_bars(ticker: str, interval: str = "1d",
               range_override: Optional[str] = None) -> list[dict]:
    """Direct Yahoo chart fetch -> backtester candle dicts."""
    rng, yi = YF_RANGE.get(interval, ("10y", interval))
    rng = range_override or rng
    url = (f"https://{{host}}/v8/finance/chart/"
           f"{urllib.parse.quote(ticker, safe='')}"
           f"?range={rng}&interval={yi}&includePrePost=false")
    payload = None
    last_err: Exception | None = None
    for attempt in range(6):
        host = ["query2.finance.yahoo.com", "query1.finance.yahoo.com"][attempt % 2]
        try:
            req = urllib.request.Request(url.format(host=host), headers={
                "User-Agent": UA, "Accept": "application/json, text/plain, */*"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = resp.read().decode("utf-8", "replace")
            if not body.lstrip().startswith("{"):
                raise RuntimeError(f"non-JSON ({body[:40]!r})")
            payload = json.loads(body)
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(8.0 * (attempt + 1))
    if payload is None:
        raise RuntimeError(f"yahoo failed {ticker}: {last_err!r}")

    res = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        raise RuntimeError(f"yahoo empty {ticker}")
    ts = res["timestamp"]
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    adj = (res.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose")
    out: list[dict] = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
            v = (q.get("volume") or [0.0] * len(ts))[i] or 0.0
        except Exception:
            continue
        if None in (o, h, l, c):
            continue
        o, h, l, c, v = float(o), float(h), float(l), float(c), float(v)
        if min(o, h, l, c) <= 0:
            continue
        if adj and adj[i] and c > 0 and interval == "1d":
            f = float(adj[i]) / c
            o, h, l, c = o * f, h * f, l * f, c * f
        h2, l2 = max(h, o, c), min(l, o, c)
        bv, sv = _split_buy_sell(o, h2, l2, c, v)
        out.append({"time": int(t), "open": o, "high": h2, "low": l2,
                    "close": c, "volume": v, "buy_volume": bv,
                    "sell_volume": sv})
    out.sort(key=lambda r: r["time"])
    dedup = {r["time"]: r for r in out}
    return [dedup[t] for t in sorted(dedup)]


def bar_is_closed(bar_time: int, interval: str, now: Optional[float] = None) -> bool:
    """True when a bar's interval has fully elapsed (with a grace period)."""
    now = now or time.time()
    if interval == "1d":
        # daily bars are timestamped at 00:00 UTC of the session date; a US
        # session closes 16:00 ET = 20:00/21:00 UTC.  Closed once the UTC day
        # is over.
        end = bar_time + 86400
        return now >= end
    secs = {"1h": 3600, "30m": 1800, "15m": 900, "5m": 300}.get(interval, 3600)
    return now >= bar_time + secs + 120


class TapeFeed:
    """Replay a recorded tape (parity / paper / historical walk-forward)."""

    def __init__(self, market: str, interval: str):
        self.market = market
        self.interval = interval
        self._cache: dict[str, list[dict]] = {}

    def closed_bars(self, symbol: str, since_ts: int = 0) -> list[dict]:
        """All recorded bars after `since_ts` (historical => closed)."""
        return [b for b in self.history(symbol) if int(b["time"]) > since_ts]

    def history(self, symbol: str) -> list[dict]:
        if symbol not in self._cache:
            rec = get_market_recording(self.market, symbol, self.interval)
            if not rec:
                raise KeyError(f"no recording {self.market}/{symbol}/{self.interval}")
            self._cache[symbol] = get_market_recording_candles(rec["id"])
        return self._cache[symbol]


class YahooFeed:
    """Live closed-bar feed with optional incremental persistence."""

    def __init__(self, market: str, interval: str, tickers: dict[str, str],
                 persist: bool = True):
        self.market = market
        self.interval = interval
        self.tickers = dict(tickers)          # symbol -> yahoo ticker
        self.persist = persist
        self._history: dict[str, list[dict]] = {}
        if persist:
            init_market_db()

    def history(self, symbol: str, refresh: bool = False) -> list[dict]:
        if symbol in self._history and not refresh:
            return self._history[symbol]
        ticker = self.tickers.get(symbol, symbol)
        bars = yahoo_bars(ticker, self.interval)
        self._history[symbol] = bars
        if self.persist:
            self._persist(symbol, bars)
        return bars

    def _persist(self, symbol: str, bars: list[dict]) -> None:
        rec = get_market_recording(self.market, symbol, self.interval)
        if rec is None:
            rec_id = upsert_market_recording(self.market, symbol, self.interval,
                                             meta={"source": "yahoo_live"})
        else:
            rec_id = rec["id"]
        insert_market_candles_batch(rec_id, bars)
        set_recording_bounds(rec_id, bars[0]["time"], bars[-1]["time"], len(bars))

    def closed_bars(self, symbol: str, since_ts: int = 0) -> list[dict]:
        bars = self.history(symbol, refresh=True)
        now = time.time()
        return [b for b in bars
                if b["time"] > since_ts and bar_is_closed(b["time"], self.interval, now)]


class AlpacaFeed:
    """Alpaca market-data bars (optional).  Needs APCA_API_KEY_ID/SECRET."""

    BASE = "https://data.alpaca.markets/v2/stocks"

    def __init__(self, interval: str = "1d", feed: str = "iex"):
        self.interval = interval
        self.feed = feed
        self.key = os.environ.get("APCA_API_KEY_ID", "")
        self.secret = os.environ.get("APCA_API_SECRET_KEY", "")
        if not (self.key and self.secret):
            raise RuntimeError("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set")

    def _get(self, path: str, params: dict) -> dict:
        url = f"{self.BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={
            "APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def history(self, symbol: str, limit: int = 1000) -> list[dict]:
        tf = {"1d": "1Day", "1h": "1Hour", "30m": "30Min",
              "15m": "15Min", "5m": "5Min"}[self.interval]
        payload = self._get(f"/{symbol}/bars",
                            {"timeframe": tf, "limit": limit,
                             "adjustment": "all", "feed": self.feed})
        out = []
        for b in payload.get("bars", []):
            o, h, l, c = float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"])
            v = float(b.get("v", 0.0))
            bv, sv = _split_buy_sell(o, h, l, c, v)
            out.append({"time": int(_dt.datetime.fromisoformat(
                b["t"].replace("Z", "+00:00")).timestamp()),
                "open": o, "high": h, "low": l, "close": c, "volume": v,
                "buy_volume": bv, "sell_volume": sv})
        return out


def resolve_ticker(symbol: str, market: str) -> str:
    """Symbol -> Yahoo ticker for the non-equity universes."""
    table = {
        "idx_fut": {"ES": "ES=F", "NQ": "NQ=F", "YM": "YM=F", "RTY": "RTY=F"},
        "comm_fut": {"GC": "GC=F", "SI": "SI=F", "CL": "CL=F", "NG": "NG=F",
                     "HG": "HG=F", "ZC": "ZC=F", "ZW": "ZW=F", "PL": "PL=F"},
        "crypto_spot": {s: f"{s}-USD" for s in
                        ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX"]},
        "intl_idx": {"GDAXI": "^GDAXI", "FTSE": "^FTSE", "FCHI": "^FCHI",
                     "STOXX50E": "^STOXX50E", "N225": "^N225", "HSI": "^HSI"},
        "fx": {"EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "AUDUSD": "AUDUSD=X",
               "NZDUSD": "NZDUSD=X", "USDJPY": "USDJPY=X", "USDCAD": "USDCAD=X",
               "USDCHF": "USDCHF=X", "EURJPY": "EURJPY=X", "GBPJPY": "GBPJPY=X",
               "EURGBP": "EURGBP=X"},
    }
    if market in table:
        return table[market].get(symbol, symbol)
    return symbol
