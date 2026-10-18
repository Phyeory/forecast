"""fetch_perps.py — crypto perpetual-futures backtest data via Binance.

Source: `data.binance.vision` — Binance's official public data dumps
(monthly kline zips for the USDT-M perpetual futures market).  Free,
rate-limit-free, keyless, and the futures klines carry the REAL taker
buy base volume, so the buy/sell split is measured, not proxied.

Universe (BTC/ETH/SOL plus the other liquid majors)
----------------------------------------------------
  BTCUSDT ETHUSDT SOLUSDT XRPUSDT DOGEUSDT BNBUSDT ADAUSDT
  LINKUSDT AVAXUSDT LTCUSDT DOTUSDT ATOMUSDT BCHUSDT AAVEUSDT

Every symbol is ingested from 2020-01 — the earliest month Binance
publishes um-futures kline zips (earlier listing months exist neither as
monthly nor per-day dumps) — to the current month, i.e. ~6.8 years of free
history.  Ingested at BOTH 1h (primary engine tape, ~60k bars each) and
1d (secondary, ~2.5k bars each).

Usage
-----
    python -m market_backtest.fetch_perps            # both intervals
    python -m market_backtest.fetch_perps --interval 1h
"""

from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import time
import urllib.request
import zipfile

from .market_store import (
    init_market_db,
    insert_market_candles_batch,
    set_recording_bounds,
    upsert_market_recording,
)

MARKET = "crypto_perp"

SYMBOLS = [
    ("BTCUSDT", "Bitcoin", 2020, 1),
    ("LTCUSDT", "Litecoin", 2020, 1),
    ("ETHUSDT", "Ethereum", 2020, 1),
    ("BCHUSDT", "Bitcoin Cash", 2020, 1),
    ("XRPUSDT", "XRP", 2020, 1),
    ("ADAUSDT", "Cardano", 2020, 1),
    ("XLMUSDT", "Stellar", 2020, 1),
    ("BNBUSDT", "BNB", 2020, 1),
    ("LINKUSDT", "Chainlink", 2020, 1),
    ("ATOMUSDT", "Cosmos", 2020, 1),
    ("DOTUSDT", "Polkadot", 2020, 8),
    ("SOLUSDT", "Solana", 2020, 9),
    ("FILUSDT", "Filecoin", 2020, 10),
    ("AAVEUSDT", "Aave", 2020, 12),
    ("AVAXUSDT", "Avalanche", 2021, 9),
    ("DOGEUSDT", "Dogecoin", 2021, 7),
    # ── second cohort (2026-10-18): broad-factor expansion for train/
    #    test statistical power.  Start months set conservatively late
    #    (pre-listing months 404-skip cleanly); newer listings keep
    #    2-4y of hourly tape each, still ≥4k test trades aggregated.
    ("TRXUSDT", "TRON", 2020, 6),
    ("ETCUSDT", "Ethereum Classic", 2020, 9),
    ("EOSUSDT", "EOS", 2020, 9),
    ("XTZUSDT", "Tezos", 2020, 9),
    ("ALGOUSDT", "Algorand", 2020, 10),
    ("VETUSDT", "VeChain", 2020, 10),
    ("ZECUSDT", "Zcash", 2020, 4),
    ("DASHUSDT", "Dash", 2020, 5),
    ("THETAUSDT", "Theta Network", 2021, 1),
    ("SANDUSDT", "The Sandbox", 2021, 4),
    ("MANAUSDT", "Decentraland", 2021, 2),
    ("CRVUSDT", "Curve DAO", 2020, 10),
    ("COMPUSDT", "Compound", 2020, 9),
    ("MKRUSDT", "Maker", 2020, 8),
    ("SNXUSDT", "Synthetix", 2020, 10),
    ("UNIUSDT", "Uniswap", 2020, 11),
    ("YFIUSDT", "yearn.finance", 2020, 10),
    ("SUSHIUSDT", "SushiSwap", 2020, 10),
    ("NEARUSDT", "NEAR Protocol", 2021, 4),
    ("EGLDUSDT", "MultiversX", 2021, 5),
    ("GRTUSDT", "The Graph", 2021, 4),
    ("1INCHUSDT", "1inch", 2021, 4),
    ("ARBUSDT", "Arbitrum", 2023, 4),
    ("OPUSDT", "Optimism", 2022, 7),
    ("SUIUSDT", "Sui", 2023, 6),
    ("APTUSDT", "Aptos", 2022, 11),
    ("TIAUSDT", "Celestia", 2023, 11),
    ("1000PEPEUSDT", "Pepe (1000x)", 2023, 6),
]

_BASE = "https://data.binance.vision/data/futures/um/monthly/klines"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _months_range(start_year: int, start_month: int) -> list[tuple[int, int]]:
    now = datetime.datetime.now()
    out = []
    y, m = start_year, start_month
    while (y, m) <= (now.year, now.month):
        out.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def fetch_month(symbol: str, interval: str, year: int, month: int,
                retries: int = 2) -> list[dict] | None:
    """One monthly kline zip → candle dict list (None when not offered)."""
    fname = f"{symbol}-{interval}-{year}-{month:02d}.zip"
    url = f"{_BASE}/{symbol}/{interval}/{fname}"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                blob = resp.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                member = zf.namelist()[0]
                with zf.open(member) as fh:
                    reader = csv.reader(io.TextIOWrapper(fh, "utf-8"))
                    candles: list[dict] = []
                    for row in reader:
                        # um-futures kline columns:
                        # 0 open_time_ms, 1 o, 2 h, 3 l, 4 c, 5 vol(base),
                        # 6 close_time_ms, 7 quote_vol, 8 trades,
                        # 9 taker_buy_base_vol, 10 taker_buy_quote, 11 ignore
                        if len(row) < 10 or row[0] in ("open_time", ""):
                            continue
                        try:
                            ts_ms = int(row[0])
                            o = float(row[1]); h = float(row[2])
                            l = float(row[3]); c = float(row[4])
                            vol = float(row[5])
                            taker_buy = float(row[9])
                        except (ValueError, IndexError):
                            continue
                        if o <= 0 or c <= 0 or vol < 0:
                            continue
                        h = max(h, o, c)
                        l = min(l, o, c)
                        taker_buy = min(max(taker_buy, 0.0), vol)
                        candles.append({
                            "time": ts_ms // 1000,
                            "open": o, "high": h, "low": l, "close": c,
                            "volume": vol,
                            "buy_volume": taker_buy,
                            "sell_volume": max(vol - taker_buy, 0.0),
                        })
                    return candles
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None          # symbol not listed that month yet
            time.sleep(1.0 + attempt)
        except Exception:  # noqa: BLE001 — network retry
            time.sleep(1.0 + attempt)
    return None


def fetch_symbol(symbol: str, interval: str, start_year: int, start_month: int,
                 force: bool = False) -> dict:
    from .market_store import get_market_recording
    if not force:
        existing = get_market_recording(MARKET, symbol, interval)
        if existing:
            return {"skipped": True, "candles": existing.get("candle_count")}
    months = _months_range(start_year, start_month)
    away: list[dict] = []
    n_missing = 0
    for (y, m) in months:
        rows = fetch_month(symbol, interval, y, m)
        if rows is None:
            n_missing += 1  # pre-listing months
            continue
        away.extend(rows)
        time.sleep(0.15)
    if len(away) < 500:
        return {"error": f"thin tape {len(away)} bars"}
    away.sort(key=lambda r: r["time"])
    dedup: dict[int, dict] = {}
    for r in away:
        dedup[r["time"]] = r
    candles = [dedup[t] for t in sorted(dedup)]
    rec_id = upsert_market_recording(MARKET, symbol, interval, meta={
        "source": "binance_vision_um_futures", "start": f"{start_year}-{start_month:02d}",
        "taker_split": True,
    })
    insert_market_candles_batch(rec_id, candles)
    set_recording_bounds(rec_id, candles[0]["time"], candles[-1]["time"],
                         len(candles), meta={"source": "binance_vision"})
    return {"candles": len(candles), "first": candles[0]["time"],
            "last": candles[-1]["time"], "missing_months": n_missing}


def fetch_all(interval: str | None = None, force: bool = False,
              only: list[str] | None = None) -> dict:
    init_market_db()
    intervals = [interval] if interval else ["1d", "1h"]
    report = {}
    for sym, name, sy, sm in SYMBOLS:
        if only and sym not in only:
            continue
        for iv in intervals:
            try:
                res = fetch_symbol(sym, iv, sy, sm, force=force)
                report[f"{sym}:{iv}"] = res
                if res.get("skipped"):
                    print(f"[perp] {sym} {iv}: already fetched "
                          f"({res.get('candles')} bars) — skip")
                elif "first" in res:
                    f = datetime.datetime.fromtimestamp(res["first"])
                    l = datetime.datetime.fromtimestamp(res["last"])
                    print(f"[perp] {sym} {iv}: {res['candles']} bars "
                          f"({f:%Y-%m-%d} → {l:%Y-%m-%d})")
                else:
                    print(f"[perp] {sym} {iv}: {res}")
            except Exception as exc:  # noqa: BLE001 — continue the universe
                report[f"{sym}:{iv}"] = {"error": str(exc)}
                print(f"[perp] {sym} {iv}: ERROR {exc!r}")
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", choices=["1d", "1h"], default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", type=str, default="")
    args = ap.parse_args()
    rep = fetch_all(interval=args.interval, force=args.force,
                    only=[s for s in args.only.split(",") if s])
    print(json.dumps(rep, indent=2)[:4000])
