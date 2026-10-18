"""sweep.py — calibration-only knob search on market tapes (V2M clone).

The engine's DECISION LOGIC is fixed (memecoin-identical math); this harness
searches its existing CONFIG surface — the same knobs a live calibration
layer would set — for values that make market tapes tradeable.  Nothing in
this file changes engine behaviour beyond parameter values.

Protocol (no peeking):
  * engine params = per-symbol train-prefix calibration, then CELL OVERRIDES
    applied on top (still causal — overrides are constants, not fits).
  * cell selection reads TRAIN statistics only; TEST stats are carried along
    for the final verification of whichever cell we promote.

Usage:
    python -m market_backtest.sweep --market stocks --round a --workers 8
    python -m market_backtest.sweep --market perps  --round a --workers 8
    python -m market_backtest.sweep --market perps  --round b --base-geom best
"""

from __future__ import annotations

import argparse
import json
import time as _time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

STOCKS_ALL = None   # resolved from fetcher universe at runtime

LIQUID12 = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT",
            "DOGEUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT", "DOTUSDT",
            "NEARUSDT"]

OUT_DIR = Path(__file__).resolve().parent / "sweeps"


# ── geometry shorthands (per market base) ────────────────────────────────────
def _mem(bars: int) -> dict:
    tw = bars * 4
    return {"lambda_0": 1.0 / tw, "tw_window_seconds": float(tw)}


GEOM_A_STOCKS = {                      # base: mem 375 bars (tw=1500), tau 40
    "base":        {},
    "mem187":      _mem(187),
    "mem93":       _mem(93),
    "mem750":      _mem(750),
    "tau20":       {"tau_max": 20.0, "tau_min": 2.5},
    "tau80":       {"tau_max": 80.0, "tau_min": 10.0},
}

GEOM_A_PERPS = {                       # base: mem 500 bars (tw=2000), tau 20
    "base":        {},
    "mem250":      _mem(250),
    "mem125":      _mem(125),
    "mem1000":     _mem(1000),
    "tau10":       {"tau_max": 10.0, "tau_min": 1.5},
    "tau40":       {"tau_max": 40.0, "tau_min": 5.0},
}

# Round B — loss-cut / win-shape knobs, one family at a time.  Values are
# applied ON TOP of the geometry override selected from Round A plus the
# per-symbol calibration.
ROUND_B_STOCKS = {
    "b_kelly_hard": {"no_long_offside_pct": 8.0, "no_long_exit_bars": 20},
    "b_rs5":        {"v2_rate_split_offside_pct": 5.0},
    "b_theta45":    {"v2_rate_split_theta": 0.45},
    "b_evr_lo":     {"v2_evr_offside_min_pct": 8.0, "v2_evr_eval_delay": 2 * 86400,
                     "v2_evr_buy_ratio_max": 0.55},
    "b_evr_hi":     {"v2_evr_confirm_pct": 20.0, "v2_evr_offside_min_pct": 6.0,
                     "v2_evr_buy_ratio_max": 0.60, "v2_evr_eval_delay": 2 * 86400},
    "b_be10":       {"breakeven_arm_dd_pct": 10.0, "breakeven_buffer_pct": 0.5},
    "b_give35":     {"gain_retrace_give_frac": 0.35},
    "b_give65":     {"gain_retrace_give_frac": 0.65},
    "b_arm6":       {"gain_retrace_arm_pct": 6.0, "v2_rate_split_arm_pct": 6.0},
    "b_conf84":     {"entry_confidence_high": 0.84, "confidence_high": 0.84},
    "b_pup72":      {"v2_p_up_min": 0.72},
    "b_rev4":       {"reversal_exit_bars": 4},
    "b_kitchen":    {"no_long_offside_pct": 8.0, "no_long_exit_bars": 20,
                     "v2_rate_split_offside_pct": 5.0, "v2_rate_split_persist": 6,
                     "v2_evr_offside_min_pct": 6.0, "v2_evr_eval_delay": 2 * 86400,
                     "v2_evr_buy_ratio_max": 0.60, "breakeven_arm_dd_pct": 10.0,
                     "breakeven_buffer_pct": 0.5, "reversal_exit_bars": 4},
    "b_kitchen_g187": {"no_long_offside_pct": 8.0, "no_long_exit_bars": 20,
                     "v2_rate_split_offside_pct": 5.0, "v2_rate_split_persist": 6,
                     "v2_evr_offside_min_pct": 6.0, "v2_evr_eval_delay": 2 * 86400,
                     "v2_evr_buy_ratio_max": 0.60, "breakeven_arm_dd_pct": 10.0,
                     "breakeven_buffer_pct": 0.5, "reversal_exit_bars": 4,
                     "lambda_0": 1.0 / 748, "tw_window_seconds": 748.0},
    "b_kitchen_g750": {"no_long_offside_pct": 8.0, "no_long_exit_bars": 20,
                     "v2_rate_split_offside_pct": 5.0, "v2_rate_split_persist": 6,
                     "v2_evr_offside_min_pct": 6.0, "v2_evr_eval_delay": 2 * 86400,
                     "v2_evr_buy_ratio_max": 0.60, "breakeven_arm_dd_pct": 10.0,
                     "breakeven_buffer_pct": 0.5, "reversal_exit_bars": 4,
                     "lambda_0": 1.0 / 3000, "tw_window_seconds": 3000.0},
}

ROUND_B_PERPS = {
    "b_kelly_hard": {"no_long_offside_pct": 6.0, "no_long_exit_bars": 20},
    "b_rs5":        {"v2_rate_split_offside_pct": 5.0},
    "b_theta45":    {"v2_rate_split_theta": 0.45},
    "b_evr_lo":     {"v2_evr_offside_min_pct": 6.0, "v2_evr_eval_delay": 2 * 3600,
                     "v2_evr_buy_ratio_max": 0.55},
    "b_evr_hi":     {"v2_evr_confirm_pct": 20.0, "v2_evr_offside_min_pct": 6.0,
                     "v2_evr_buy_ratio_max": 0.60, "v2_evr_eval_delay": 2 * 3600},
    "b_be10":       {"breakeven_arm_dd_pct": 10.0, "breakeven_buffer_pct": 0.5},
    "b_give35":     {"gain_retrace_give_frac": 0.35},
    "b_give65":     {"gain_retrace_give_frac": 0.65},
    "b_arm6":       {"gain_retrace_arm_pct": 6.0, "v2_rate_split_arm_pct": 6.0},
    "b_conf84":     {"entry_confidence_high": 0.84, "confidence_high": 0.84},
    "b_pup72":      {"v2_p_up_min": 0.72},
    "b_rev4":       {"reversal_exit_bars": 4},
    "b_kitchen":    {"no_long_offside_pct": 6.0, "no_long_exit_bars": 20,
                     "v2_rate_split_offside_pct": 5.0, "v2_rate_split_persist": 6,
                     "v2_evr_offside_min_pct": 6.0, "v2_evr_eval_delay": 2 * 3600,
                     "v2_evr_buy_ratio_max": 0.60, "breakeven_arm_dd_pct": 10.0,
                     "breakeven_buffer_pct": 0.5, "reversal_exit_bars": 4},
    "b_kitchen_g250": {"no_long_offside_pct": 6.0, "no_long_exit_bars": 20,
                     "v2_rate_split_offside_pct": 5.0, "v2_rate_split_persist": 6,
                     "v2_evr_offside_min_pct": 6.0, "v2_evr_eval_delay": 2 * 3600,
                     "v2_evr_buy_ratio_max": 0.60, "breakeven_arm_dd_pct": 10.0,
                     "breakeven_buffer_pct": 0.5, "reversal_exit_bars": 4,
                     "lambda_0": 1.0 / 1000, "tw_window_seconds": 1000.0},
    "b_kitchen_g1000": {"no_long_offside_pct": 6.0, "no_long_exit_bars": 20,
                     "v2_rate_split_offside_pct": 5.0, "v2_rate_split_persist": 6,
                     "v2_evr_offside_min_pct": 6.0, "v2_evr_eval_delay": 2 * 3600,
                     "v2_evr_buy_ratio_max": 0.60, "breakeven_arm_dd_pct": 10.0,
                     "breakeven_buffer_pct": 0.5, "reversal_exit_bars": 4,
                     "lambda_0": 1.0 / 4000, "tw_window_seconds": 4000.0},
}

# Round C — remaining live surface: entry-side decay block thresholds, tau
# ladder density, stricter entry selectivity.  (EVR / geometry / kitchen
# interactions are covered by the probe runs + Round B combos.)
ROUND_C_STOCKS = {
    "c_block50":  {"iter05_decay_window_thresh": 0.5},
    "c_block65":  {"iter05_decay_window_thresh": 0.65},
    "c_ladder10": {"tau_step": 10.0},
    "c_ladder5":  {"tau_step": 5.0},
    "c_pup80":    {"v2_p_up_min": 0.80},
    "c_conf90":   {"entry_confidence_high": 0.90, "confidence_high": 0.90},
    "c_kelly20":  {"no_long_offside_pct": 12.0, "no_long_exit_bars": 20},
}

ROUND_C_PERPS = {
    "c_block50":  {"iter05_decay_window_thresh": 0.5},
    "c_block65":  {"iter05_decay_window_thresh": 0.65},
    "c_ladder10": {"tau_step": 10.0},
    "c_ladder5":  {"tau_step": 5.0},
    "c_pup80":    {"v2_p_up_min": 0.80},
    "c_conf90":   {"entry_confidence_high": 0.90, "confidence_high": 0.90},
    "c_kelly30":  {"no_long_offside_pct": 6.0, "no_long_exit_bars": 30},
}


def _task(task: dict) -> dict:
    """Worker: one (cell, symbol) replay.  Must stay top-level (picklable)."""
    try:
        from market_backtest.runner import run_market_backtest
        s = run_market_backtest(
            market=task["market"], symbol=task["symbol"],
            interval=task["interval"], label="",
            engine_params_override=task["overrides"] or None,
            split_frac=0.7, save=False,
        )
        return {"ok": True, "cell": task["cell"], "symbol": task["symbol"],
                "interval": task["interval"],
                "train": s["stats_train"], "test": s["stats_test"]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "cell": task["cell"], "symbol": task["symbol"],
                "interval": task["interval"], "err": f"{type(exc).__name__}: {exc}"}


def _agg(rows: list[dict], seg: str) -> dict:
    n = sum(r[seg]["trades"] for r in rows)
    wins = sum(r[seg]["wins"] for r in rows)
    pnl = sum(r[seg]["total_pnl"] for r in rows)
    pos = sum(1 for r in rows if r[seg]["total_pnl"] > 0)
    exit_pnl: dict[str, float] = defaultdict(float)
    exit_n: dict[str, int] = defaultdict(int)
    for r in rows:
        for k, v in (r[seg].get("exit_pnl") or {}).items():
            exit_pnl[k] += v
        for k2, v2 in (r[seg].get("exit_reasons") or {}).items():
            exit_n[k2] += v2
    return {
        "n": n, "wr": (100.0 * wins / n if n else 0.0), "pnl": pnl,
        "mean": (pnl / n if n else 0.0),
        "symbols_pos": f"{pos}/{len(rows)}",
        "exit_pnl": {k: round(v, 4) for k, v in sorted(
            exit_pnl.items(), key=lambda kv: kv[1])[:6]},
        "exit_n": dict(sorted(exit_n.items(), key=lambda kv: -kv[1])),
    }


def run_sweep(market: str, cells: dict[str, dict], symbols: list[str],
              interval: str, workers: int, tag: str) -> None:
    tasks = []
    for name, ov in cells.items():
        for sym in symbols:
            tasks.append({"market": market, "symbol": sym, "interval": interval,
                          "cell": name, "overrides": ov})
    print(f"[sweep] {market}: {len(cells)} cells × {len(symbols)} symbols "
          f"= {len(tasks)} replays on {workers} workers")
    rows_by_cell: dict[str, list[dict]] = defaultdict(list)
    t0 = _time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_task, t) for t in tasks]
        for f in as_completed(futs):
            r = f.result()
            done += 1
            if done % 8 == 0 or done == len(tasks):
                el = _time.time() - t0
                print(f"[sweep] {done}/{len(tasks)} done "
                      f"({el:.0f}s, ≈{el / done * (len(tasks) - done):.0f}s left)",
                      flush=True)
            if r.get("ok"):
                rows_by_cell[r["cell"]].append(r)
            else:
                print(f"[sweep] FAIL {r['cell']}/{r['symbol']}: {r['err']}",
                      flush=True)

    # aggregated report
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    print(f"\n{'cell':<12}{'trainN':>7}{'trWR%':>7}{'trPnL':>9}{'trMean%':>9}"
          f"{'tr+':>6}  | {'testN':>6}{'teWR%':>7}{'tePnL':>9}{'teMean%':>9}{'te+':>6}")
    for name in cells:
        rows = rows_by_cell.get(name) or []
        if not rows:
            continue
        tr = _agg(rows, "train")
        te = _agg(rows, "test")
        out[name] = {"overrides": cells[name], "symbols": len(rows),
                     "train": tr, "test": te,
                     "symbols_detail": [
                         {"symbol": r["symbol"], "train": r["train"],
                          "test": r["test"]} for r in rows]}
        print(f"{name:<12}{tr['n']:>7}{tr['wr']:>7.1f}{tr['pnl']:>9.3f}"
              f"{tr['mean'] * 100:>9.3f}{tr['symbols_pos']:>6}  | "
              f"{te['n']:>6}{te['wr']:>7.1f}{te['pnl']:>9.3f}"
              f"{te['mean'] * 100:>9.3f}{te['symbols_pos']:>6}")
    path = OUT_DIR / f"run{tag}.json"
    with open(path, "w") as fh:
        json.dump(out, fh, indent=1, default=str)
    print(f"\n[sweep] → {path}")

    # quick train-side decomposition for the top cells by train mean
    print("\n[sweep] top cells by TRAIN mean/trade — exit-reason decomposition:")
    for name, d in sorted(out.items(), key=lambda kv: -kv[1]["train"]["mean"])[:4]:
        print(f"  {name:<12} train exit pnl: {d['train']['exit_pnl']}")
        print(f"  {'':<12} test  exit pnl: {d['test']['exit_pnl']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["stocks", "perps"], required=True)
    ap.add_argument("--round", choices=["a", "b", "c", "custom"], required=True)
    ap.add_argument("--cells-json", type=str, default="",
                    help="custom: path to {name: overrides} JSON")
    ap.add_argument("--base-geom", type=str, default="",
                    help="merge a named round-A cell's kwargs into every cell")
    ap.add_argument("--symbols", type=str, default="",
                    help="comma-separated override list")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--interval", type=str, default="",
                    help="override interval (defaults: stocks 1d, perps 1h)")
    ap.add_argument("--tag", type=str, default="")
    args = ap.parse_args()

    tag = args.tag or args.round
    if args.market == "stocks":
        market, interval, pool_file = "stocks", "1d", "fetch_stocks"
        if args.symbols:
            symbols = args.symbols.split(",")
        else:
            from market_backtest.fetch_stocks import UNIVERSE
            symbols = [s for s, _n, _sec in UNIVERSE]
        cells = {"a": GEOM_A_STOCKS, "b": ROUND_B_STOCKS,
                 "c": ROUND_C_STOCKS}[args.round] if args.round != "custom" else {}
    else:
        market, interval = "crypto_perp", "1h"
        symbols = args.symbols.split(",") if args.symbols else LIQUID12
        cells = {"a": GEOM_A_PERPS, "b": ROUND_B_PERPS,
                 "c": ROUND_C_PERPS}[args.round] if args.round != "custom" else {}

    if args.interval:
        interval = args.interval

    if args.cells_json:
        cells = json.loads(Path(args.cells_json).read_text())
    # merge base geometry into every cell
    if args.base_geom:
        base_file = OUT_DIR / "runbase.json"
        base = {}
        if base_file.exists():
            base = (json.loads(base_file.read_text()) or {}).get(
                args.base_geom, {}).get("overrides") or {}
        cells = {k: {**base, **v} for k, v in cells.items()}
        print(f"[sweep] base geometry merged: {base}")
    run_sweep(market, cells, symbols, interval, args.workers, tag)


if __name__ == "__main__":
    main()
