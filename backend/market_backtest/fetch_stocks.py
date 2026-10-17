"""fetch_stocks.py — 10-year stock backtest data from Nasdaq's public API.

Source: `api.nasdaq.com` quote/chart endpoint (the same data source the
nasdaq.com website charts consume).  Free, keyless, official exchange data.
The daily series is back-adjusted (splits AND dividends — the AAPL 2016 row
reads ~29.4, matching a 4:1-split- and dividend-adjusted level, not the raw
~113 close), so a long-window engine sees coherent log-returns.

Universe (chosen for the engine, not convenience)
-------------------------------------------------
The V2 engine is a long-only momentum/regime machine that exits on Bayesian
posterior flips and gain-retrace trails — it needs liquid names with real
trends AND real reversals across the window.  30 large-caps spanning

  * high-beta semis/growth (NVDA AMD AVGO MU INTC QCOM TSLA META NFLX AMZN)
  * mega-cap anchors (AAPL MSFT GOOGL)
  * financials/cyclicals (JPM GS BAC CAT DE BA GE)
  * defensives/dividend value (XOM CVX WMT KO JNJ UNH PFE MCD NKE)

All 30 list well before the 10-year window start, giving a full-cohort
10-year tape with no survivorship bias inside the window.

Usage
-----
    python -m market_backtest.fetch_stocks           # fetch default window
    python -m market_backtest.fetch_stocks --force   # overwrite candles
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import time
import urllib.request

from .market_store import (
    init_market_db,
    insert_market_candles_batch,
    set_recording_bounds,
    upsert_market_recording,
)

MARKET = "stocks"
INTERVAL = "1d"

# (symbol, name, sector)
UNIVERSE = [
    ("NVDA", "NVIDIA Corp", "semis"),
    ("AMD", "Advanced Micro Devices", "semis"),
    ("AVGO", "Broadcom", "semis"),
    ("MU", "Micron Technology", "semis"),
    ("INTC", "Intel Corp", "semis"),
    ("QCOM", "Qualcomm", "semis"),
    ("AAPL", "Apple Inc", "mega_tech"),
    ("MSFT", "Microsoft Corp", "mega_tech"),
    ("GOOGL", "Alphabet Inc", "mega_tech"),
    ("META", "Meta Platforms", "mega_tech"),
    ("AMZN", "Amazon.com", "growth_tech"),
    ("NFLX", "Netflix Inc", "growth_tech"),
    ("TSLA", "Tesla Inc", "growth_tech"),
    ("JPM", "JPMorgan Chase", "financials"),
    ("GS", "Goldman Sachs Group", "financials"),
    ("BAC", "Bank of America", "financials"),
    ("CAT", "Caterpillar Inc", "industrials"),
    ("DE", "Deere & Co", "industrials"),
    ("BA", "Boeing Co", "industrials"),
    ("GE", "GE Aerospace", "industrials"),
    ("XOM", "Exxon Mobil", "energy"),
    ("CVX", "Chevron Corp", "energy"),
    ("WMT", "Walmart Inc", "consumer_def"),
    ("KO", "Coca-Cola Co", "consumer_def"),
    ("JNJ", "Johnson & Johnson", "healthcare"),
    ("UNH", "UnitedHealth Group", "healthcare"),
    ("PFE", "Pfizer Inc", "healthcare"),
    ("MCD", "McDonald's Corp", "consumer_def"),
    ("NKE", "Nike Inc", "consumer_def"),
    ("MSTR", "MicroStrategy Inc", "growth_tech"),
]

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _years_ago_date(years: float) -> str:
    ms = datetime.datetime.now() - datetime.timedelta(days=int(years * 365.25))
    return ms.strftime("%Y-%m-%d")


def fetch_symbol_daily(symbol: str, from_date: str, to_date: str,
                       retries: int = 3) -> list[dict]:
    """10Y daily adjusted OHLCV rows for one symbol from api.nasdaq.com."""
    url = (
        f"https://api.nasdaq.com/api/quote/{symbol}/chart"
        f"?assetclass=stocks&fromdate={from_date}&todate={to_date}"
        f"&limits=10&ytd=false&type=1y"
    )
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": _UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://www.nasdaq.com",
                "Referer": f"https://www.nasdaq.com/market-activity/stocks/{symbol.lower()}",
            })
            with urllib.request.urlopen(req, timeout=45) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            data = payload.get("data") or {}
            rows = data.get("chart") or []
            candles: list[dict] = []
            for row in rows:
                z = row.get("z") or {}
                dt_txt = (z.get("dateTime") or "").strip()
                try:
                    ts = int(datetime.datetime.strptime(
                        dt_txt, "%m/%d/%Y").replace(
                        tzinfo=datetime.timezone.utc).timestamp())
                except ValueError:
                    continue
                def _num(key: str) -> float:
                    v = (z.get(key) or "").replace("$", "").replace(",", "").strip()
                    if v in ("", "N/A", "--"):
                        return 0.0
                    return float(v)
                o, h, l, c = (_num("open"), _num("high"), _num("low"), _num("close"))
                vol = _num("volume")
                if o <= 0 or c <= 0 or l <= 0:
                    continue
                h = max(h, o, c)
                l = min(l, o, c) or o
                # buy/sell split proxy: flow taker-bias ~ (c-o)/(h-l)
                # (crypto perps get REAL taker splits; equities split is
                # a documented proxy, flow-neutral on 0 range bars)
                if h > l:
                    frac_buy = 0.5 + 0.45 * ((c - o) / (h - l))
                else:
                    frac_buy = 0.5
                frac_buy = min(0.95, max(0.05, frac_buy))
                candles.append({
                    "time": ts,
                    "open": o, "high": h, "low": l, "close": c,
                    "volume": vol,
                    "buy_volume": vol * frac_buy,
                    "sell_volume": vol * (1.0 - frac_buy),
                })
            candles.sort(key=lambda r: r["time"])
            # dedupe on time (keep last = freshest close for that day)
            dedup: dict[int, dict] = {}
            for r in candles:
                dedup[r["time"]] = r
            return [dedup[t] for t in sorted(dedup)]
        except Exception as exc:  # noqa: BLE001 — retry probe
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Nasdaq fetch failed for {symbol}: {last_err!r}")


def fetch_all(years: float = 10.0, force: bool = False, only: list[str] | None = None) -> dict:
    init_market_db()
    from_date = _years_ago_date(years)
    to_date = datetime.datetime.now().strftime("%Y-%m-%d")
    report = {}
    for sym, name, sector in UNIVERSE:
        if only and sym not in only:
            continue
        existing = None
        try:
            from market_store import get_market_recording
            existing = get_market_recording(MARKET, sym, INTERVAL)
        except Exception:
            pass
        if existing and not force:
            report[sym] = {"skipped": True, "candles": existing.get("candle_count")}
            print(f"[stocks] {sym}: already fetched ({existing.get('candle_count')} bars) — skip")
            continue
        try:
            candles = fetch_symbol_daily(sym, from_date, to_date)
            if len(candles) < 500:
                report[sym] = {"error": f"thin tape {len(candles)} bars"}
                print(f"[stocks] {sym}: THIN tape ({len(candles)} bars) — skipped")
                continue
            rec_id = upsert_market_recording(MARKET, sym, INTERVAL, meta={
                "name": name, "sector": sector, "source": "nasdaq_api",
                "from_date": from_date, "to_date": to_date,
                "adjusted": "splits+dividends",
            })
            insert_market_candles_batch(rec_id, candles)
            set_recording_bounds(rec_id, candles[0]["time"], candles[-1]["time"],
                                 len(candles), meta={"name": name, "sector": sector})
            report[sym] = {"candles": len(candles),
                           "first": candles[0]["time"], "last": candles[-1]["time"]}
            print(f"[stocks] {sym}: {len(candles)} bars "
                  f"({datetime.datetime.fromtimestamp(candles[0]['time']):%Y-%m-%d}"
                  f" → {datetime.datetime.fromtimestamp(candles[-1]['time']):%Y-%m-%d})")
            time.sleep(0.8)
        except Exception as exc:  # noqa: BLE001 — continue the universe
            report[sym] = {"error": str(exc)}
            print(f"[stocks] {sym}: ERROR {exc!r}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=10.0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", type=str, default="")
    args = ap.parse_args()
    rep = fetch_all(years=args.years, force=args.force,
                    only=[s for s in args.only.split(",") if s])
    print(json.dumps({k: v for k, v in rep.items()}, indent=2)[:4000])
