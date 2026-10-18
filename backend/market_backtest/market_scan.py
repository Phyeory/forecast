"""market_scan.py — which market actually pays?

Per-market league table built the only way that is comparable across markets:
run one config on every symbol, pool the per-symbol ledgers into ONE account
(daily equity = 1 + Σ realized-so-far + open marks), and score the portfolio:

    trades/yr, Σ PnL, Sharpe, maxDD, worst day, lean (positive symbols),
    PnL per symbol-year, and (optionally) prop MC pass/blow.

Usage
-----
    # one market
    python -m market_backtest.market_scan --market stocks \
        --label arm14 --overrides '{"gain_retrace_arm_pct": 14.0}'

    # league table across every fetched market
    python -m market_backtest.market_scan --all \
        --overrides '{"gain_retrace_arm_pct": 14.0}' --prop
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from .market_store import list_market_recordings
from .stats_report import load_results, ledger_daily_equity, _sharpe

RESULTS_ROOT = Path(__file__).resolve().parent / "results"
SECONDS_YEAR = 365.25 * 86400


def _task(t: dict) -> dict:
    try:
        from .runner import run_market_backtest
        s = run_market_backtest(
            market=t["market"], symbol=t["symbol"], interval=t["interval"],
            label=t["label"], engine_params_override=t["overrides"] or None,
            split_frac=0.7, save=True, results_dir=Path(t["out_dir"]))
        return {"ok": True, "symbol": t["symbol"], "n_trades": s["n_trades"]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "symbol": t["symbol"],
                "err": f"{type(exc).__name__}: {exc}"}


def _pooled(rows: list[dict], seg: str) -> tuple[list[tuple[int, float]], float]:
    """Pooled daily ledger + span in years (test segment)."""
    days, eq = ledger_daily_equity(rows, seg)
    series: list[tuple[int, float]] = []
    prev = 1.0
    for i, e in enumerate(eq):
        series.append((i, e - prev))
        prev = e
    # span from the actual trade timestamps in the segment
    ts = []
    for r in rows:
        for t in r.get("trades", []):
            et = int(t.get("entry_time") or 0)
            if et:
                ts.append(et)
    if ts:
        span_years = max(30 / 365.25, (max(ts) - min(ts)) / SECONDS_YEAR)
    else:
        span_years = 1.0
    return series, span_years


def _score(series: list[tuple[int, float]], span_years: float) -> dict:
    vals = [r for _, r in series]
    n = len(vals)
    if n < 2:
        return {}
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    sd = var ** 0.5
    eq = 1.0
    peak = 1.0
    maxdd = 0.0
    for r in vals:
        eq += r
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak)
    return {
        "days": n, "total_pnl": sum(vals),
        "mean_pct": mean * 100, "sd_pct": sd * 100,
        "sharpe": (mean / sd * (252 ** 0.5)) if sd > 0 else 0.0,
        "maxdd_pct": maxdd * 100,
        "worst_day_pct": min(vals) * 100,
        "best_day_pct": max(vals) * 100,
        "cagr_pct": ((1.0 + sum(vals)) ** (1.0 / max(span_years, 1e-9)) - 1.0) * 100
        if sum(vals) > -1 else -100.0,
        "span_years": span_years,
    }


def scan(market: str, interval: str, label: str, overrides: dict,
         workers: int, rerun: bool, prop: bool, prop_ks: list[float],
         reuse_dir: str = "") -> dict:
    out_dir = Path(reuse_dir) if reuse_dir else RESULTS_ROOT / f"scan_{market}_{label}"
    out_dir.mkdir(parents=True, exist_ok=True)

    symbols = [r["symbol"] for r in list_market_recordings(market)
               if r["interval"] == interval and r["status"] == "completed"]
    if not symbols:
        return {"market": market, "error": "no tapes"}

    have = {f.name for f in out_dir.glob("*.json")}
    tasks = [{"market": market, "symbol": s, "interval": interval,
              "label": label, "overrides": overrides, "out_dir": str(out_dir)}
             for s in symbols
             if rerun or not any(f.startswith(f"{market}_{s}_{interval}")
                                 for f in have)]

    t0 = time.time()
    if tasks:
        print(f"[scan] {market}: {len(tasks)} replays on {workers} workers")
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_task, t) for t in tasks]
            done = 0
            for f in as_completed(futs):
                r = f.result()
                done += 1
                if not r.get("ok"):
                    print(f"[scan] FAIL {r['symbol']}: {r['err']}")
                elif done % 10 == 0:
                    print(f"[scan] {market}: {done}/{len(tasks)} "
                          f"({time.time() - t0:.0f}s)", flush=True)

    rows = load_results(out_dir, market, label, interval)
    if not rows:
        return {"market": market, "error": "no results"}

    res = {"market": market, "interval": interval, "label": label,
           "overrides": overrides, "symbols": len(rows)}
    for seg in ("train", "test"):
        series, span = _pooled(rows, seg)
        sc = _score(series, span)
        trades = sum(int(r.get(f"stats_{seg}", {}).get("trades", 0)) for r in rows)
        pos = sum(1 for r in rows
                  if float(r.get(f"stats_{seg}", {}).get("total_pnl", 0)) > 0)
        sc.update({"trades": trades,
                   "trades_per_year": trades / max(span, 1e-9),
                   "symbols_positive": pos,
                   "pnl_per_symbol_year": (sc.get("total_pnl", 0) /
                                           max(1, len(rows)) / max(span, 1e-9))})
        res[seg] = sc

    if prop and rows:
        # prop MC on the pooled TEST ledger (same machinery as mc_firms)
        try:
            from .mc_firms import TEMPLATES, full_challenge_mc, funded_mc
            series, _span = _pooled(rows, "test")
            rules = TEMPLATES["ftmo_2step"]
            prop_out = {}
            for k in prop_ks:
                fc = full_challenge_mc(series, k, rules, 800)
                fu = funded_mc(series, k, rules, 0.8, 400, reset=True)
                prop_out[str(k)] = {"p_full": fc["p_full"],
                                    "p1": fc["p1"],
                                    "blown": fu["p_blown"],
                                    "E_payout": fu["mean_payout"]}
            res["prop_ftmo_2step"] = prop_out
        except Exception as exc:  # noqa: BLE001
            res["prop_error"] = f"{type(exc).__name__}: {exc}"

    (out_dir / "summary.json").write_text(json.dumps(res, indent=1, default=str))
    res["out_dir"] = str(out_dir)
    return res


def _print_row(res: dict) -> None:
    if res.get("error"):
        print(f"{res['market']:<14} ERROR {res['error']}")
        return
    tr, te = res.get("train") or {}, res.get("test") or {}
    print(f"{res['market']:<14}{res['symbols']:>4} "
          f"{te.get('trades', 0):>6}{te.get('trades_per_year', 0):>7.1f} "
          f"{te.get('total_pnl', 0):>9.3f}{te.get('mean_pct', 0):>8.3f} "
          f"{te.get('sharpe', 0):>7.2f}{te.get('maxdd_pct', 0):>8.1f} "
          f"{te.get('worst_day_pct', 0):>9.2f} "
          f"{te.get('symbols_positive', 0):>4}/{res['symbols']:<3} "
          f"{tr.get('total_pnl', 0):>9.3f} "
          + (f"  pfull={res['prop_ftmo_2step'].get('0.75', {}).get('p_full', 0)*100:5.1f}%"
             if res.get("prop_ftmo_2step") else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--label", default="scan")
    ap.add_argument("--overrides", default="")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--rerun", action="store_true")
    ap.add_argument("--dir", default="", help="reuse an existing labeled results dir")
    ap.add_argument("--prop", action="store_true")
    ap.add_argument("--prop-ks", default="0.5,0.75,1.0,1.5,2.0")
    args = ap.parse_args()

    overrides = json.loads(args.overrides) if args.overrides else {}
    ks = [float(x) for x in args.prop_ks.split(",")]

    if args.all:
        markets = sorted({r["market"] for r in list_market_recordings()})
    elif args.market:
        markets = [args.market]
    else:
        raise SystemExit("--market or --all required")

    print(f"\n{'market':<14}{'sym':>4}{'teN':>6}{'N/yr':>7}{'teΣ':>9}"
          f"{'mean%':>8}{'Sharpe':>7}{'maxDD%':>8}{'worst%':>9}{'pos':>8}"
          f"{'trΣ':>9}")
    results = []
    for m in markets:
        reuse = args.dir if (args.dir and not args.all and len(markets) == 1) else ""
        r = scan(m, args.interval, args.label, overrides, args.workers,
                 args.rerun, args.prop, ks, reuse_dir=reuse)
        results.append(r)
        _print_row(r)

    RESULTS_ROOT.mkdir(exist_ok=True)
    (RESULTS_ROOT / f"league_{args.label}.json").write_text(
        json.dumps(results, indent=1, default=str))
    print(f"\n[scan] → {RESULTS_ROOT / f'league_{args.label}.json'}")


if __name__ == "__main__":
    main()
