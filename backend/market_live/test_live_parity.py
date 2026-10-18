"""test_live_parity.py — THE gate: live host decisions == backtester decisions.

For a set of (market, symbol, interval) tapes, run:
  A) `market_backtest.runner.run_market_backtest(...)` — the reference replay
  B) `MarketEngineHost` fed the same candles one closed bar at a time

and assert the trade books are identical field-by-field (timestamps, fill
prices, size, tokens, PnL, exit reason, fees) plus identical final balance.

This is the same guarantee the memecoin system has (`test_live_parity.py`):
the live loop is not a reimplementation, it is the same executor.

Run:
    cd backend && ./.venv/bin/python -m market_live.test_live_parity
    cd backend && ./.venv/bin/python -m market_live.test_live_parity --market crypto_perp --interval 1h --symbols BTCUSDT,ETHUSDT
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from market_backtest.market_store import (
    get_market_recording, get_market_recording_candles)
from market_backtest.market_calibration import calibrate_market_params, BAR_SPEC
from market_backtest.runner import COSTS, MARKET_CLASS, run_market_backtest
from market_live.host import MarketEngineHost

FIELDS = ["entry_time", "exit_time", "entry_price", "exit_price",
          "size_sol", "size_tokens", "pnl_sol", "pnl_pct", "exit_reason",
          "entry_reason", "fees_paid", "slippage_cost_sol", "outcome"]

TOL = 0.0   # exact: same code path, same floats


def _cmp_trade(a: dict, b: dict) -> list[str]:
    bad = []
    for f in FIELDS:
        va, vb = a.get(f), b.get(f)
        if isinstance(va, float) or isinstance(vb, float):
            if va is None or vb is None:
                if va != vb:
                    bad.append(f"{f}: {va!r} != {vb!r}")
                continue
            if abs(float(va) - float(vb)) > TOL:
                bad.append(f"{f}: {va!r} != {vb!r}")
        elif va != vb:
            bad.append(f"{f}: {va!r} != {vb!r}")
    return bad


def check_symbol(market: str, symbol: str, interval: str, *,
                 label: str = "", overrides: dict | None = None,
                 split_frac: float = 0.7, buy_size: float = 0.1,
                 verbose: bool = False) -> dict:
    kind = f"{market}_{interval}" if interval != "1d" else MARKET_CLASS.get(market, market)
    spec = BAR_SPEC[kind]

    # ── A) reference backtest ─────────────────────────────────────────────
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="parity_ref_"))
    run_market_backtest(
        market=market, symbol=symbol, interval=interval, label="",
        engine_params_override=overrides, split_frac=split_frac,
        buy_size=buy_size, save=True, results_dir=tmpdir)
    ref_files = sorted(tmpdir.glob("*.json"))
    ref = json.loads(ref_files[0].read_text()) if ref_files else {}

    # rebuild the exact same params the runner used (train prefix calibration)
    rec = get_market_recording(market, symbol, interval)
    candles = get_market_recording_candles(rec["id"])
    n = len(candles)
    split_idx = max(1, min(n - 1, int(n * float(split_frac))))
    calib = calibrate_market_params(candles[:split_idx], kind, len(candles[:split_idx]))
    params = dict(calib["engine_params"])
    if overrides:
        params.update(overrides)

    # ── B) live host, same bars one at a time ─────────────────────────────
    host = MarketEngineHost(market, symbol, interval, params,
                            buy_size=buy_size, market_kind=kind)
    for c in candles:
        host.on_bar(c)
    if host.ft.current_trade is not None:
        host.force_close(candles[-1], reason="recording_ended")
    live_trades = host.trades()

    # ── compare ───────────────────────────────────────────────────────────
    ref_trades = ref["trades"]
    problems: list[str] = []
    if len(ref_trades) != len(live_trades):
        problems.append(f"trade count {len(ref_trades)} != {len(live_trades)}")
    for i, (a, b) in enumerate(zip(ref_trades, live_trades)):
        bad = _cmp_trade(a, b)
        if bad:
            problems.append(f"trade[{i}]: " + "; ".join(bad[:6]))
        if len(problems) > 8:
            break

    ref_bal = float(ref["stats"]["current_balance"])
    live_bal = float(host.balance)
    if abs(ref_bal - live_bal) > 1e-12:
        problems.append(f"balance {ref_bal!r} != {live_bal!r}")

    if verbose or problems:
        print(f"  {market}/{symbol}/{interval}: n_ref={len(ref_trades)} "
              f"n_live={len(live_trades)} bal={ref_bal:.10f}")
    return {"market": market, "symbol": symbol, "interval": interval,
            "n_ref": len(ref_trades), "n_live": len(live_trades),
            "balance_ref": ref_bal, "balance_live": live_bal,
            "ok": not problems, "problems": problems}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="stocks")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--label", default="")
    ap.add_argument("--overrides", default="",
                    help='JSON dict of calibration overrides, e.g. \'{"gain_retrace_arm_pct": 14.0}\'')
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    overrides = json.loads(args.overrides) if args.overrides else None

    if args.symbols:
        symbols = [s for s in args.symbols.split(",") if s]
    else:
        from market_backtest.market_store import list_market_recordings
        symbols = [r["symbol"] for r in list_market_recordings(args.market)
                   if r["interval"] == args.interval
                   and r["status"] == "completed"][:args.limit]

    print(f"[parity] {args.market} {args.interval}: {len(symbols)} tape(s), "
          f"overrides={overrides}")
    results = []
    for sym in symbols:
        try:
            results.append(check_symbol(args.market, sym, args.interval,
                                        overrides=overrides, verbose=True))
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: ERROR {type(exc).__name__}: {exc}")
            results.append({"symbol": sym, "ok": False,
                            "problems": [f"{type(exc).__name__}: {exc}"]})

    ok = all(r["ok"] for r in results)
    print("\n[parity] " + ("PASS — live host is decision-identical to the "
                           "backtester" if ok else "FAIL"))
    for r in results:
        if not r["ok"]:
            print(f"  {r['symbol']}:")
            for p in r.get("problems", [])[:8]:
                print(f"    - {p}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=1))
        print(f"[parity] → {args.json_out}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
