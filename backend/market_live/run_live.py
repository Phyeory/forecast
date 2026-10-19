"""run_live.py — CLI for the market live stack.

Examples
--------
Replay a historical slice through the full live stack (paper broker + guard):

    cd backend
    ./.venv/bin/python -m market_live.run_live replay \
        --market stocks --symbols AAPL,MSFT,NVDA,SPY --start 2024-06-01 \
        --k 0.75 --firm ftmo_2step --results-dir market_backtest/results/arm14

Measure the real-world execution wedge (close-fill vs next-open):

    ./.venv/bin/python -m market_live.run_live compare \
        --market stocks --symbols AAPL,MSFT,NVDA,SPY --start 2024-06-01 --k 0.75

Live paper session (Yahoo closed bars, no orders sent):

    ./.venv/bin/python -m market_live.run_live paper \
        --market stocks --symbols AAPL,MSFT --k 0.75 --firm ftmo_2step

Live with a real broker:

    APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... \
    ./.venv/bin/python -m market_live.run_live live \
        --broker alpaca --market stocks --symbols AAPL,MSFT --k 0.75

    ./.venv/bin/python -m market_live.run_live live \
        --broker mt5 --bridge http://127.0.0.1:8765 --market stocks \
        --symbols AAPL,MSFT --k 0.75 --firm ftmo_2step
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path

from market_live.feeds import YahooFeed, resolve_ticker
from market_live.trader import LiveConfig, MarketLiveTrader, run_replay


def _ts(date_str: str) -> int:
    d = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def _cfg_from_file(path: str) -> LiveConfig:
    d = json.loads(Path(path).read_text())
    d["symbols"] = tuple(d.get("symbols") or ())
    return LiveConfig(**{k: v for k, v in d.items()
                         if k in LiveConfig.__dataclass_fields__})


def _cfg_from_args(args) -> LiveConfig:
    symbols = tuple(s for s in args.symbols.split(",") if s)
    if not symbols:
        from market_backtest.market_store import list_market_recordings
        symbols = tuple(r["symbol"] for r in list_market_recordings(args.market)
                        if r["interval"] == args.interval)[:args.limit]
    overrides = json.loads(args.overrides) if args.overrides else {}
    return LiveConfig(
        market=args.market, interval=args.interval, symbols=symbols,
        results_dir=args.results_dir, label=args.label, overrides=overrides,
        k=args.k, buy_size=args.buy_size, starting_equity=args.equity,
        broker=args.broker, firm=args.firm,
        exec_mode=args.exec_mode or "next_open",
        journal=not args.no_journal,
        broker_kwargs=({"starting_equity": args.equity,
                        "slippage_pct": args.slippage,
                        "fee_bps": args.fee_bps} if args.broker == "paper"
                       else {"base_url": args.bridge} if args.broker in ("mt5", "bridge")
                       else {}),
        calibration_snapshot=args.calibration_snapshot,
    )


def _print_summary(s: dict) -> None:
    print("\n── summary")
    print(f"  equity       {s['equity']:,.2f}  ({s['pnl']:+,.2f} / {s['pnl_pct']:+.2f}%)")
    print(f"  bars         {s['n_bars']}   exits {s['n_exits']}")
    print(f"  open         {s['open_positions']}")
    b = s["broker"]
    print(f"  broker       cash {b.get('cash', 0):,.2f}  realized "
          f"{b.get('realized', 0):+,.2f}  fees {b.get('fees', 0):,.2f}")
    g = s.get("guard")
    if g:
        print(f"  guard        {g['template']} phase {g['phase']}/{len(g['events'])} "
              f"equity {g['equity']:,.0f} floor {g['floor']:,.0f} "
              f"daily_floor {g['daily_floor']} days {g['trading_days']} "
              f"halted={g['halted']} passed={g['passed']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["replay", "paper", "live", "compare",
                                     "preflight"])
    ap.add_argument("--config", default="", help="JSON LiveConfig (overrides flags)")
    ap.add_argument("--market", default="stocks")
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--results-dir", default="market_backtest/results/arm14")
    ap.add_argument("--label", default="arm14")
    ap.add_argument("--overrides", default="")
    ap.add_argument("--k", type=float, default=0.75)
    ap.add_argument("--buy-size", type=float, default=0.1)
    ap.add_argument("--equity", type=float, default=100_000.0)
    ap.add_argument("--firm", default="")
    ap.add_argument("--broker", default="paper")
    ap.add_argument("--bridge", default="http://127.0.0.1:8765")
    ap.add_argument("--slippage", type=float, default=0.03)
    ap.add_argument("--fee-bps", type=float, default=0.0)
    ap.add_argument("--start", default="")
    ap.add_argument("--max-bars", type=int, default=0)
    ap.add_argument("--exec-mode", default="", choices=["", "close", "next_open"])
    ap.add_argument("--feed", default="auto", choices=["auto", "tape", "yahoo", "alpaca"])
    ap.add_argument("--poll-seconds", type=float, default=60.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--no-journal", action="store_true")
    ap.add_argument("--place-test-order", action="store_true")
    ap.add_argument("--calibration-snapshot", default="",
                    help="manually adopted recalibration snapshot file/dir")
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    cfg = _cfg_from_file(args.config) if args.config else _cfg_from_args(args)
    if args.config:
        # explicit CLI overrides still win
        if args.symbols:
            cfg.symbols = tuple(s for s in args.symbols.split(",") if s)
        if args.k is not None and args.k != 0.75:
            cfg.k = args.k
        if args.calibration_snapshot:
            cfg.calibration_snapshot = args.calibration_snapshot
        if args.exec_mode:
            cfg.exec_mode = args.exec_mode
    if not cfg.firm:
        cfg.firm = None

    if args.mode == "preflight":
        from market_live.setup_prop import preflight
        import sys as _sys
        _sys.exit(0 if preflight(cfg, sample=args.limit,
                                 place_test_order=args.place_test_order) else 1)

    if args.mode == "replay":
        start = _ts(args.start) if args.start else None
        s = run_replay(cfg, start_ts=start,
                       max_bars=args.max_bars or None)
        _print_summary(s)
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(s, indent=1, default=str))

    elif args.mode == "compare":
        # honest live-vs-backtest wedge: same decisions, two fill conventions
        start = _ts(args.start) if args.start else None
        out = {}
        for mode in ("close", "next_open"):
            cfg.exec_mode = mode
            cfg.journal = False
            s = run_replay(cfg, start_ts=start, max_bars=args.max_bars or None,
                           verbose=False)
            out[mode] = {"pnl_pct": s["pnl_pct"], "pnl": s["pnl"],
                         "n_exits": s["n_exits"], "equity": s["equity"]}
        print("\n── execution-mode wedge (same engine decisions)")
        for mode, r in out.items():
            print(f"  {mode:<10} {r['pnl_pct']:+7.2f}%  exits {r['n_exits']:>4}  "
                  f"equity {r['equity']:,.0f}")
        d = out["next_open"]["pnl_pct"] - out["close"]["pnl_pct"]
        print(f"  wedge      {d:+.2f} pp over the slice")
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(out, indent=1))

    else:  # paper | live
        if args.feed == "tape":
            from market_live.feeds import TapeFeed
            feed = TapeFeed(cfg.market, cfg.interval)
        elif args.feed == "alpaca":
            from market_live.feeds import AlpacaFeed
            feed = AlpacaFeed(cfg.interval)
        else:
            tickers = {s: resolve_ticker(s, cfg.market) for s in cfg.symbols}
            feed = YahooFeed(cfg.market, cfg.interval, tickers, persist=True)
        cfg.broker_kwargs = cfg.broker_kwargs or {}
        trader = MarketLiveTrader(cfg, feed=feed)
        trader.prepare()
        print(f"[live] ready — {len(trader.hosts)} symbols, broker={trader.broker.name}")
        while True:
            r = trader.poll_once()
            print(f"[live] poll: {r['processed']} bars, {r['fills']} fills, "
                  f"equity {trader._equity():,.2f}", flush=True)
            if args.once:
                break
            time.sleep(args.poll_seconds)
        _print_summary(trader.summary())


if __name__ == "__main__":
    main()
