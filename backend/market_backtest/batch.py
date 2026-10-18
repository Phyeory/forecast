"""batch.py — orchestrate the market backtest systems.

    python -m market_backtest.batch fetch-stocks [--years 10]
    python -m market_backtest.batch fetch-perps [--interval 1d|1h]
    python -m market_backtest.batch run  --market stocks|perps|both
                                  [--intervals 1d,1h] [--symbols ...]
                                  [--label fuel] [--forgot-calibration]
    python -m market_backtest.batch summary <batch_dir>

`run` computes the calibration per symbol from the causal tape PREFIX
(`market_calibration.BAR_SPEC`), then replays the FULL tape through the
production ForwardTester path (`runner.run_market_backtest`).  Parallel
across symbols via ProcessPoolExecutor; pool workers carry the same
`guard_parent` orphan protection as backtester batches.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import multiprocessing
import os
import sys
import time
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _guard() -> None:
    try:
        from process_watchdog import guard_parent
        guard_parent()
    except Exception:
        pass


def _run_one(task: dict) -> dict:
    """Pool worker: one symbol/interval/override → summary or error dict."""
    try:
        _guard()
        from market_backtest.runner import run_market_backtest
        return run_market_backtest(
            market=task["market"],
            symbol=task["symbol"],
            interval=task["interval"],
            starting_balance=1.0,
            buy_size=0.1,
            label=task.get("label", ""),
            engine_params_override=task.get("engine_params_override"),
            results_dir=task.get("results_dir"),
            split_frac=task.get("split_frac", 0.7),
        )
    except Exception as exc:  # noqa: BLE001 — record per-job failure
        return {"error": f"{type(exc).__name__}: {exc}",
                "task": task.get("symbol"), "interval": task.get("interval"),
                "market": task.get("market")}


# Single source of truth = the fetcher universes (they define what's in the DB).
from .fetch_stocks import UNIVERSE as _STOCK_UNIVERSE  # noqa: E402
from .fetch_perps import SYMBOLS as _PERP_SYMBOLS      # noqa: E402

STOCK_UNIVERSE = [s for s, _n, _sec in _STOCK_UNIVERSE]
PERP_UNIVERSE = [s for s, _n, _y, _m in _PERP_SYMBOLS]


def cmd_run(args: argparse.Namespace) -> None:
    tasks: list[dict] = []
    label = args.label
    override: dict | None = None

    if args.forgot_calibration:
        # Diagnostics cell: /calibration-forgot/ sends a tape across epochs.
        override = {"v2_mintcal_enable": 0.0, "v2_popcal_enable": 0.0}

    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        syms = None

    if args.market in ("stocks", "both"):
        for sym in (syms or STOCK_UNIVERSE):
            tasks.append({
                "market": "stocks", "symbol": sym, "interval": "1d",
                "label": label, "engine_params_override": override,
                "results_dir": RESULTS_DIR / label if label else RESULTS_DIR,
                "split_frac": args.split_frac,
            })
    if args.market in ("perps", "both"):
        intervals = [i.strip() for i in args.intervals.split(",") if i.strip()]
        for sym in (syms or PERP_UNIVERSE):
            for interval in intervals:
                tasks.append({
                    "market": "crypto_perp", "symbol": sym, "interval": interval,
                    "label": label, "engine_params_override": override,
                    "results_dir": RESULTS_DIR / label if label else RESULTS_DIR,
                    "split_frac": args.split_frac,
                })

    print(f"[batch] {len(tasks)} tape-jobs {label or ''} …")
    t0 = time.time()
    out_dir = RESULTS_DIR / label if label else RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    max_workers = args.max_workers or min(8, max(1, multiprocessing.cpu_count() - 1))
    with futures.ProcessPoolExecutor(max_workers=max_workers,
                                     initializer=_guard) as pool:
        for res in pool.map(_run_one, tasks):
            summaries.append(res)
            if "error" in res:
                print(f"[batch] {res.get('market')}/{res.get('task')}"
                      f"{':' + res.get('interval', '') if res.get('interval') else ''}"
                      f" → ERROR {res['error']}")
            else:
                st = res["stats"]
                stt = res.get("stats_test") or {}
                tr = res.get("stats_train") or {}
                print(f"[batch] {res['symbol']}{':' + res['interval'] if res['interval'] else ''}"
                      f" → all {st.get('total_trades', 0):>4} "
                      f"(train {tr.get('trades', 0):>4} / test {stt.get('trades', 0):>4}) "
                      f"| test WR {stt.get('win_rate', 0.0):>5.1f}% "
                      f"PnL {stt.get('total_pnl', 0.0):>9.5f}")

    batch_file = out_dir / f"batch_{int(time.time())}.json"
    with open(batch_file, "w") as fh:
        json.dump({"label": label, "ran_at": time.time(),
                   "elapsed_s": time.time() - t0, "results": summaries}, fh, default=str)
    print(f"[batch] {len(summaries)} done in {time.time() - t0:.1f}s → {batch_file}")


def cmd_summary(args: argparse.Namespace) -> None:
    d = Path(args.batch_dir)
    files = sorted(d.glob("*.json"))
    rows = []
    for f in files:
        if f.name.startswith("batch_"):
            continue
        try:
            data = json.loads(f.read_text())
            if isinstance(data, list):
                continue
        except Exception:
            continue
        st = data.get("stats") or {}
        stt = data.get("stats_test") or {}
        tr = data.get("stats_train") or {}
        cb = data.get("calibration") or {}
        rows.append({
            "symbol": data.get("symbol"), "interval": data.get("interval"),
            "market": data.get("market"),
            "trades": st.get("total_trades", 0),
            "wins": st.get("winning_trades", 0),
            "win_rate": st.get("win_rate", 0.0),
            "pnl": st.get("total_pnl_sol", 0.0),
            "trades_train": tr.get("trades", 0),
            "pnl_train": tr.get("total_pnl", 0.0),
            "trades_test": stt.get("trades", 0),
            "wr_test": stt.get("win_rate", 0.0),
            "pnl_test": stt.get("total_pnl", 0.0),
            "tau_max": cb.get("tau_max"),
            "arm_pct": cb.get("gain_retrace_arm_pct"),
            "n_candles": data.get("n_candles"),
        })
    if not rows:
        print(f"[summary] no per-symbol result JSONs in {d}")
        return
    trades_total = sum(r["trades"] for r in rows)
    pnl_total = sum(r["pnl"] for r in rows)
    pnl_test_total = sum(r["pnl_test"] for r in rows)
    print(f"\n{'market':<12}{'symbol':<10}{'int':<4}{'trades':>7}{'tr/tst':>8}"
          f"{'WR%all':>8}{'PnL all':>10}{'WR test':>8}{'PnL test':>10}")
    for r in rows:
        wr = r["win_rate"] or 0.0
        print(f"{r['market']:<12}{r['symbol']:<10}{r['interval']:<4}"
              f"{r['trades']:>7}{str(r['trades_train']) + '/' + str(r['trades_test']):>8}"
              f"{wr:>8.1f}{r['pnl']:>10.5f}{r['wr_test']:>8.1f}{r['pnl_test']:>10.5f}")
    print(f"\nTOTAL: {trades_total} trades, PnL all {pnl_total:+.5f}, "
          f"test-only PnL {pnl_test_total:+.5f}\n")
    out = d / "summary.json"
    with open(out, "w") as fh:
        json.dump(rows, fh, default=str)
    print(f"[summary] → {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch-stocks")
    p.add_argument("--years", type=float, default=10.0)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("fetch-perps")
    p.add_argument("--interval", choices=["1d", "1h"], default=None)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("--market", choices=["stocks", "perps", "both"], required=True)
    p.add_argument("--intervals", type=str, default="1d,1h")
    p.add_argument("--symbols", type=str, default="")
    p.add_argument("--label", type=str, default="")
    p.add_argument("--forgot-calibration", action="store_true",
                   help="diagnostic: skip the market calibration layers")
    p.add_argument("--split-frac", type=float, default=0.7,
                   help="train fraction of the tape (calibration prefix)")
    p.add_argument("--max-workers", type=int, default=8)
    s = sub.add_parser("summary")
    s.add_argument("batch_dir")
    args = ap.parse_args()

    if args.cmd == "fetch-stocks":
        from .fetch_stocks import fetch_all
        rep = fetch_all(years=args.years, force=args.force)
        print(json.dumps(rep, indent=2)[:4000])
    elif args.cmd == "fetch-perps":
        from .fetch_perps import fetch_all
        rep = fetch_all(interval=args.interval, force=args.force)
        print(json.dumps(rep, indent=2)[:4000])
    elif args.cmd == "run":
        cmd_run(args)
    elif args.cmd == "summary":
        cmd_summary(args)


if __name__ == "__main__":
    main()
