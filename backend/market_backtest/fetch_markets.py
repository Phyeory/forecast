"""fetch_markets.py — prop-tradable instrument universes (daily bars, yfinance).

All groups land in `backend/data/market_data.db` under their own market name,
mapped to the SAME daily-bar engine class as "stocks" (runner.MARKET_CLASS):

    idx_fut     US index futures (continuous): ES, NQ, YM, RTY
    intl_idx    international indices: DAX, FTSE, CAC, STOXX50, N225, HSI
    comm_fut    commodities: gold, silver, WTI, natgas, copper, corn, wheat
    fx          FX majors + crosses, each ALSO stored inverted (synthetic
                "short pair" so a long-only engine covers both directions)
    crypto_spot crypto spot majors (CFD-able): BTC ETH SOL XRP ADA DOGE LINK AVAX
    etf         ETF cross-check anchors: SPY QQQ IWM DIA GLD TLT EEM EFA

Usage:
    python -m market_backtest.fetch_markets                # fetch everything
    python -m market_backtest.fetch_markets --market fx    # one group
    python -m market_backtest.fetch_markets --force        # refetch
"""

from __future__ import annotations

import argparse
import datetime
import json
import time

from .market_store import (
    init_market_db,
    insert_market_candles_batch,
    get_market_recording,
    set_recording_bounds,
    upsert_market_recording,
)

INTERVAL = "1d"

UNIVERSES: dict[str, list[tuple[str, str]]] = {
    # (ticker, label)
    "idx_fut": [
        ("ES=F", "S&P 500 E-mini continuous"),
        ("NQ=F", "Nasdaq 100 E-mini continuous"),
        ("YM=F", "Dow E-mini continuous"),
        ("RTY=F", "Russell 2000 E-mini continuous"),
    ],
    "intl_idx": [
        ("^GDAXI", "DAX 40"),
        ("^FTSE", "FTSE 100"),
        ("^FCHI", "CAC 40"),
        ("^STOXX50E", "Euro Stoxx 50"),
        ("^N225", "Nikkei 225"),
        ("^HSI", "Hang Seng"),
    ],
    "comm_fut": [
        ("GC=F", "Gold continuous"),
        ("SI=F", "Silver continuous"),
        ("CL=F", "WTI crude continuous"),
        ("NG=F", "Natgas continuous"),
        ("HG=F", "Copper continuous"),
        ("ZC=F", "Corn continuous"),
        ("ZW=F", "Wheat continuous"),
        ("PL=F", "Platinum continuous"),
    ],
    "fx": [
        ("EURUSD=X", "EUR/USD"),
        ("GBPUSD=X", "GBP/USD"),
        ("AUDUSD=X", "AUD/USD"),
        ("NZDUSD=X", "NZD/USD"),
        ("USDJPY=X", "USD/JPY"),
        ("USDCAD=X", "USD/CAD"),
        ("USDCHF=X", "USD/CHF"),
        ("EURJPY=X", "EUR/JPY"),
        ("GBPJPY=X", "GBP/JPY"),
        ("EURGBP=X", "EUR/GBP"),
    ],
    "crypto_spot": [
        ("BTC-USD", "Bitcoin spot"),
        ("ETH-USD", "Ether spot"),
        ("SOL-USD", "Solana spot"),
        ("XRP-USD", "XRP spot"),
        ("ADA-USD", "Cardano spot"),
        ("DOGE-USD", "Dogecoin spot"),
        ("LINK-USD", "Chainlink spot"),
        ("AVAX-USD", "Avalanche spot"),
    ],
    "etf": [
        ("SPY", "SPDR S&P 500"),
        ("QQQ", "Invesco QQQ"),
        ("IWM", "iShares Russell 2000"),
        ("DIA", "SPDR Dow"),
        ("GLD", "SPDR Gold Shares"),
        ("TLT", "iShares 20+ Treasury"),
        ("EEM", "iShares MSCI EM"),
        ("EFA", "iShares MSCI EAFE"),
    ],
}


def _invert_fx(candles: list[dict]) -> list[dict]:
    """Synthetic inverse pair (long-only engine covers the other direction)."""
    out = []
    for c in candles:
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        if min(o, h, l, cl) <= 0:
            continue
        inv = {
            "time": c["time"],
            "open": 1.0 / cl, "high": 1.0 / l, "low": 1.0 / h,
            "close": 1.0 / o, "volume": c.get("volume", 0.0),
        }
        if inv["high"] > inv["low"]:
            frac = 0.5 + 0.45 * ((inv["close"] - inv["open"]) /
                                 (inv["high"] - inv["low"]))
        else:
            frac = 0.5
        frac = min(0.95, max(0.05, frac))
        inv["buy_volume"] = inv["volume"] * frac
        inv["sell_volume"] = inv["volume"] * (1.0 - frac)
        out.append(inv)
    return out


def _fetch_yf(ticker: str, years: float) -> list[dict]:
    """Direct Yahoo v8 chart fetch (yfinance 0.2.50 is crumb-blocked; the raw
    chart endpoint is keyless and works with alternating hosts + backoff)."""
    import random as _random
    import urllib.parse
    import urllib.request

    hosts = ["query2.finance.yahoo.com", "query1.finance.yahoo.com"]
    url = (f"https://{{host}}/v8/finance/chart/"
           f"{urllib.parse.quote(ticker, safe='')}"
           f"?range={int(years)}y&interval=1d&includePrePost=false")
    payload = None
    last_err: Exception | None = None
    for attempt in range(8):
        host = hosts[attempt % len(hosts)]
        try:
            req = urllib.request.Request(url.format(host=host), headers={
                "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/124.0.0.0 Safari/537.36"),
                "Accept": "application/json, text/plain, */*",
            })
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = resp.read().decode("utf-8", "replace")
            if not body.lstrip().startswith("{"):
                raise RuntimeError(f"non-JSON body ({body[:40]!r})")
            payload = json.loads(body)
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(20.0 + 15.0 * (attempt % 3) + _random.random() * 10.0)
    if payload is None:
        raise RuntimeError(f"yahoo fetch failed for {ticker}: {last_err!r}")

    res = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        raise RuntimeError(f"yahoo empty for {ticker}")
    ts = res["timestamp"]
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    adj_list = ((res.get("indicators", {}).get("adjclose")
                 or [{}])[0].get("adjclose"))
    candles: list[dict] = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = (q["open"][i], q["high"][i], q["low"][i], q["close"][i])
            v = (q.get("volume") or [None] * len(ts))[i] or 0.0
        except Exception:
            continue
        if None in (o, h, l, c):
            continue
        o, h, l, c, v = float(o), float(h), float(l), float(c), float(v)
        if min(o, h, l, c) <= 0:
            continue
        # dividend/split adjustment via adjclose ratio (as-of-last)
        if adj_list and adj_list[i] and c > 0:
            f = float(adj_list[i]) / c
            o, h, l, c = o * f, h * f, l * f, c * f
        d = datetime.datetime.fromtimestamp(int(t), tz=datetime.timezone.utc)
        ts_mid = int(datetime.datetime(d.year, d.month, d.day,
                                       tzinfo=datetime.timezone.utc).timestamp())
        h = max(h, o, c)
        l = min(l, o, c)
        if h > l:
            frac = 0.5 + 0.45 * ((c - o) / (h - l))
        else:
            frac = 0.5
        frac = min(0.95, max(0.05, frac))
        candles.append({
            "time": ts_mid, "open": o, "high": h, "low": l, "close": c,
            "volume": v, "buy_volume": v * frac, "sell_volume": v * (1 - frac),
        })
    candles.sort(key=lambda r: r["time"])
    dedup: dict[int, dict] = {}
    for r in candles:
        dedup[r["time"]] = r
    return [dedup[t] for t in sorted(dedup)]


def _store(market: str, symbol: str, candles: list[dict], meta: dict) -> dict:
    rec_id = upsert_market_recording(market, symbol, INTERVAL, meta=meta)
    insert_market_candles_batch(rec_id, candles)
    set_recording_bounds(rec_id, candles[0]["time"], candles[-1]["time"],
                         len(candles), meta=meta)
    return {"candles": len(candles), "first": candles[0]["time"],
            "last": candles[-1]["time"]}


def fetch_all(years: float = 10.0, force: bool = False,
              markets: list[str] | None = None,
              only: list[str] | None = None,
              pace_seconds: float = 10.0) -> dict:
    init_market_db()
    report: dict = {}
    for market, universe in UNIVERSES.items():
        if markets and market not in markets:
            continue
        report[market] = {}
        for ticker, label in universe:
            if only and ticker not in only:
                continue
            sym = ticker.replace("=X", "").replace("=", "").replace("^", "")
            existing = None
            try:
                existing = get_market_recording(market, sym, INTERVAL)
            except Exception:
                pass
            if existing and not force:
                print(f"[{market}] {sym}: already fetched "
                      f"({existing.get('candle_count')} bars) — skip")
                report[market][sym] = {"skipped": True}
                continue
            try:
                candles = _fetch_yf(ticker, years)
                if len(candles) < 500:
                    print(f"[{market}] {sym}: THIN ({len(candles)} bars) — skip")
                    report[market][sym] = {"error": f"thin {len(candles)}"}
                    continue
                meta = {"name": label, "ticker": ticker, "source": "yfinance",
                        "adjusted": "auto"}
                info = _store(market, sym, candles, meta)
                line = (f"[{market}] {sym}: {info['candles']} bars "
                        f"({datetime.datetime.fromtimestamp(info['first']):%Y-%m-%d}"
                        f" → {datetime.datetime.fromtimestamp(info['last']):%Y-%m-%d})")
                # FX inverse: long-only coverage of the other side
                if market == "fx":
                    inv = _invert_fx(candles)
                    info_inv = _store(market, sym + "_INV", inv,
                                      {**meta, "name": label + " (inverse)",
                                       "inverse_of": sym})
                    line += f"  + INVERSE ({info_inv['candles']} bars)"
                    report[market][sym + "_INV"] = info_inv
                report[market][sym] = info
                print(line, flush=True)
                time.sleep(pace_seconds)
            except Exception as exc:  # noqa: BLE001
                print(f"[{market}] {sym}: ERROR {exc!r}", flush=True)
                report[market][sym] = {"error": str(exc)}
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=10.0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--market", type=str, default="")
    ap.add_argument("--only", type=str, default="")
    ap.add_argument("--pace", type=float, default=10.0)
    args = ap.parse_args()
    rep = fetch_all(years=args.years, force=args.force,
                    markets=[m for m in args.market.split(",") if m],
                    only=[s for s in args.only.split(",") if s],
                    pace_seconds=args.pace)
    ok = sum(1 for g in rep.values() for v in g.values()
             if isinstance(v, dict) and "candles" in v)
    print(f"\n[done] stored {ok} tapes")
