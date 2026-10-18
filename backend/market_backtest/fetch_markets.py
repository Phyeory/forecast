"""fetch_markets.py — prop-tradable universes (free, keyless sources).

Sources (all verified working 2026-10-18; Yahoo is IP-rate-limited here):
  * api.nasdaq.com quote/chart  — US stocks & ETFs, 10y daily, split+dividend
    adjusted (same endpoint fetch_stocks.py uses)
  * fred.stlouisfed.org CSV     — FX majors (Fed H.10) + WTI/Brent spot
  * Binance Vision klines       — crypto (handled by fetch_perps.py)

Market groups (each maps to the daily-bar engine class in runner.MARKET_CLASS):

  idx_etf     US index proxies:        SPY QQQ IWM DIA MDY
  intl_etf    country/region proxies:  EWJ EWG EWU EWQ INDA FXI EWA EWC EWZ
  comm_etf    commodity proxies:       GLD SLV USO UNG CPER DBA DBB PPLT
  sector_etf  US sectors:              XLK XLF XLE XLV XLI XLP XLY XLU XLB XLRE XLC SMH XBI
  bond_etf    rates/credit:            TLT IEF LQD HYG
  fx          FX majors (FRED) + inverted synthetic pairs

Why proxies: prop CFD firms quote indices/commodities/FX directly; the ETF is
the same underlying return stream with a small tracking/expense wedge, and it
is what we have a decade of clean free data for.  Live execution still goes to
the CFD symbol — the engine sees an equivalent price series.

Usage:
    python -m market_backtest.fetch_markets               # all groups
    python -m market_backtest.fetch_markets --market fx
    python -m market_backtest.fetch_markets --force
"""

from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import time
import urllib.request

from .market_store import (
    init_market_db, insert_market_candles_batch, get_market_recording,
    set_recording_bounds, upsert_market_recording)

INTERVAL = "1d"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

ETF_UNIVERSES: dict[str, list[tuple[str, str]]] = {
    "idx_etf": [
        ("SPY", "S&P 500 (US500 proxy)"),
        ("QQQ", "Nasdaq 100 (US100 proxy)"),
        ("IWM", "Russell 2000 (US2000 proxy)"),
        ("DIA", "Dow 30 (US30 proxy)"),
        ("MDY", "S&P MidCap 400"),
    ],
    "intl_etf": [
        ("EWJ", "Japan (JP225 proxy)"),
        ("EWG", "Germany (GER40 proxy)"),
        ("EWU", "United Kingdom (UK100 proxy)"),
        ("EWQ", "France (FRA40 proxy)"),
        ("INDA", "India"),
        ("FXI", "China large-cap (HK50 proxy)"),
        ("EWA", "Australia (AUS200 proxy)"),
        ("EWC", "Canada"),
        ("EWZ", "Brazil"),
    ],
    "comm_etf": [
        ("GLD", "Gold (XAU/USD proxy)"),
        ("SLV", "Silver (XAG/USD proxy)"),
        ("USO", "WTI crude (USOIL proxy)"),
        ("UNG", "Natural gas (NATGAS proxy)"),
        ("CPER", "Copper"),
        ("DBA", "Agriculture basket"),
        ("DBB", "Base metals"),
        ("PPLT", "Platinum"),
    ],
    "sector_etf": [
        ("XLK", "Technology"), ("XLF", "Financials"), ("XLE", "Energy"),
        ("XLV", "Health care"), ("XLI", "Industrials"), ("XLP", "Staples"),
        ("XLY", "Discretionary"), ("XLU", "Utilities"), ("XLB", "Materials"),
        ("XLRE", "Real estate"), ("XLC", "Communications"), ("SMH", "Semis"),
        ("XBI", "Biotech"),
    ],
    "fx_etf": [
        ("FXE", "Euro (EURUSD proxy)"), ("FXY", "Yen (USDJPY proxy)"),
        ("FXB", "Pound (GBPUSD proxy)"), ("FXA", "Aussie (AUDUSD proxy)"),
        ("FXF", "Swiss franc (USDCHF proxy)"), ("UUP", "Dollar index"),
    ],
    "bond_etf": [
        ("TLT", "20+yr Treasuries"), ("IEF", "7-10yr Treasuries"),
        ("LQD", "IG credit"), ("HYG", "High yield"),
    ],
}

# FRED series -> (label, invert?)
FRED_FX = {
    "DEXUSEU": ("EURUSD", False),
    "DEXUSUK": ("GBPUSD", False),
    "DEXJPUS": ("USDJPY", True),     # yen per USD -> invert to USDJPY-like long
    "DEXUSAL": ("AUDUSD", False),
    "DEXCAUS": ("USDCAD", True),
    "DEXSZUS": ("USDCHF", True),
    "DEXNZUS": ("NZDUSD", False),
}
FRED_COMM = {
    "DCOILWTICO": ("WTI", "WTI crude spot"),
    "DCOILBRENTEU": ("BRENT", "Brent crude spot"),
}


# ── helpers ──────────────────────────────────────────────────────────────────
def _num(v) -> float:
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except Exception:
        return 0.0


def _split(o: float, h: float, l: float, c: float, v: float) -> tuple[float, float]:
    if h > l:
        frac = 0.5 + 0.45 * ((c - o) / (h - l))
    else:
        frac = 0.5
    frac = min(0.95, max(0.05, frac))
    return v * frac, v * (1 - frac)


def fetch_nasdaq_etf(symbol: str, years: float = 10.0,
                     assetclass: str = "etf") -> list[dict]:
    to_d = datetime.datetime.now().strftime("%Y-%m-%d")
    from_d = (datetime.datetime.now() -
              datetime.timedelta(days=int(years * 365.25))).strftime("%Y-%m-%d")
    url = (f"https://api.nasdaq.com/api/quote/{symbol}/chart"
           f"?assetclass={assetclass}&fromdate={from_d}&todate={to_d}"
           f"&limits=10&ytd=false&type=1y")
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://www.nasdaq.com",
                "Referer": f"https://www.nasdaq.com/market-activity/etf/{symbol.lower()}",
            })
            with urllib.request.urlopen(req, timeout=45) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            rows = ((payload.get("data") or {}).get("chart") or [])
            out: list[dict] = []
            for row in rows:
                z = row.get("z") or {}
                try:
                    ts = int(datetime.datetime.strptime(
                        (z.get("dateTime") or "").strip(), "%m/%d/%Y").replace(
                        tzinfo=datetime.timezone.utc).timestamp())
                except ValueError:
                    continue
                o, h, l, c = (_num(z.get("open")), _num(z.get("high")),
                              _num(z.get("low")), _num(z.get("close")))
                if min(o, h, l, c) <= 0:
                    continue
                v = _num(z.get("volume"))
                h, l = max(h, o, c), min(l, o, c)
                bv, sv = _split(o, h, l, c, v)
                out.append({"time": ts, "open": o, "high": h, "low": l,
                            "close": c, "volume": v, "buy_volume": bv,
                            "sell_volume": sv})
            out.sort(key=lambda r: r["time"])
            dedup = {r["time"]: r for r in out}
            res = [dedup[t] for t in sorted(dedup)]
            if len(res) < 400:
                raise RuntimeError(f"thin tape {len(res)}")
            return res
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(2.5 * (attempt + 1))
    raise RuntimeError(f"nasdaq {symbol}: {last_err!r}")


def fetch_fred(series: str, years: float = 10.0) -> list[dict]:
    start = (datetime.datetime.now() -
             datetime.timedelta(days=int(years * 365.25))).strftime("%Y-%m-%d")
    url = (f"https://fred.stlouisfed.org/graph/fredgraph.csv"
           f"?id={series}&cosd={start}")
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as resp:
                text = resp.read().decode("utf-8", "replace")
            rdr = csv.DictReader(io.StringIO(text))
            out = []
            for row in rdr:
                d = row.get("observation_date") or row.get("DATE") or ""
                val = row.get(series) or ""
                if not d or val in ("", "."):
                    continue
                try:
                    px = float(val)
                    ts = int(datetime.datetime.strptime(d, "%Y-%m-%d").replace(
                        tzinfo=datetime.timezone.utc).timestamp())
                except ValueError:
                    continue
                if px <= 0:
                    continue
                # no OHLC in FRED: synthesise a flat bar at the close
                out.append({"time": ts, "open": px, "high": px, "low": px,
                            "close": px, "volume": 0.0,
                            "buy_volume": 0.0, "sell_volume": 0.0})
            if len(out) < 400:
                raise RuntimeError(f"thin series {len(out)}")
            return out
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(2.5 * (attempt + 1))
    raise RuntimeError(f"fred {series}: {last_err!r}")


def invert_candles(candles: list[dict]) -> list[dict]:
    out = []
    for c in candles:
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        if min(o, h, l, cl) <= 0:
            continue
        inv = {"time": c["time"], "open": 1.0 / cl, "high": 1.0 / l,
               "low": 1.0 / h, "close": 1.0 / o, "volume": 0.0,
               "buy_volume": 0.0, "sell_volume": 0.0}
        out.append(inv)
    return out


def _store(market: str, symbol: str, candles: list[dict], meta: dict) -> dict:
    rec_id = upsert_market_recording(market, symbol, INTERVAL, meta=meta)
    insert_market_candles_batch(rec_id, candles)
    set_recording_bounds(rec_id, candles[0]["time"], candles[-1]["time"],
                         len(candles), meta=meta)
    return {"candles": len(candles), "first": candles[0]["time"],
            "last": candles[-1]["time"]}


def fetch_all(years: float = 10.0, force: bool = False,
              markets: list[str] | None = None,
              only: list[str] | None = None) -> dict:
    init_market_db()
    report: dict = {}

    def _wanted(market: str) -> bool:
        return not markets or market in markets

    for market, universe in ETF_UNIVERSES.items():
        if not _wanted(market):
            continue
        report[market] = {}
        for sym, label in universe:
            if only and sym not in only:
                continue
            existing = get_market_recording(market, sym, INTERVAL)
            if existing and not force:
                print(f"[{market}] {sym}: cached ({existing.get('candle_count')}) — skip")
                report[market][sym] = {"skipped": True}
                continue
            try:
                candles = fetch_nasdaq_etf(sym, years)
                info = _store(market, sym, candles,
                              {"name": label, "source": "nasdaq_api",
                               "assetclass": "etf"})
                report[market][sym] = info
                print(f"[{market}] {sym}: {info['candles']} bars "
                      f"({datetime.datetime.fromtimestamp(info['first']):%Y-%m-%d}"
                      f" → {datetime.datetime.fromtimestamp(info['last']):%Y-%m-%d})",
                      flush=True)
                time.sleep(1.2)
            except Exception as exc:  # noqa: BLE001
                print(f"[{market}] {sym}: ERROR {exc!r}", flush=True)
                report[market][sym] = {"error": str(exc)}

    if _wanted("fx"):
        report["fx"] = {}
        for series, (sym, invert) in FRED_FX.items():
            if only and sym not in only:
                continue
            existing = get_market_recording("fx", sym, INTERVAL)
            if existing and not force:
                print(f"[fx] {sym}: cached — skip")
                report["fx"][sym] = {"skipped": True}
                continue
            try:
                raw = fetch_fred(series)
                candles = invert_candles(raw) if invert else raw
                info = _store("fx", sym, candles,
                              {"name": f"{sym} ({series})", "source": "fred",
                               "inverted": invert})
                # long-only coverage of the other side
                other = invert_candles(candles)
                _store("fx", sym + "_INV", other,
                       {"name": f"{sym} inverse", "source": "fred"})
                report["fx"][sym] = info
                print(f"[fx] {sym}: {info['candles']} bars (+inverse)", flush=True)
                time.sleep(0.8)
            except Exception as exc:  # noqa: BLE001
                print(f"[fx] {sym}: ERROR {exc!r}", flush=True)
                report["fx"][sym] = {"error": str(exc)}

    if _wanted("comm_spot") or _wanted("comm_etf"):
        pass  # WTI is covered by USO in comm_etf; Brent skipped by default

    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=10.0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--market", type=str, default="")
    ap.add_argument("--only", type=str, default="")
    args = ap.parse_args()
    rep = fetch_all(years=args.years, force=args.force,
                    markets=[m for m in args.market.split(",") if m],
                    only=[s for s in args.only.split(",") if s])
    ok = sum(1 for g in rep.values() for v in g.values()
             if isinstance(v, dict) and "candles" in v)
    print(f"\n[done] stored {ok} tapes")
