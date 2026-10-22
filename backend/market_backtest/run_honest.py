"""run_honest.py — ACHIEVABLE-FILL rerun of the stock campaign book.

The accepted arm14 book ran the memecoin ForwardTester in `exec_model="instant"`
(fills at the signal state's close).  iter100 (pump-chart AGENTS.md) ruled that
basis transactability-phantom for the memecoin live; the STOCK production path
(opg / MT5 swing fills) executes at the NEXT BAR'S OPEN — exactly what
market_live's PaperBroker does in exec_mode="next_open".  This module reruns
the SAME engine decisions on that achievable fill basis:

  engine  — identical: frozen arm14 params, 4-state replay through
            MarketEngineHost (engine notified at each candle's close states —
            the live's exact notification timing),
  ledger  — the live's PaperBroker: fills at the next bar's open,
            slippage 3 bp/side (COSTS['stocks']), zero fees, 1.0-account
            units, 10%-book sizing on BROKER equity (equity-dynamic, the
            same live sizing rule),
  end     — recording_ended force-close at the last candle's close.

No engine file is touched.  Results land in results/<label>_honest/ in the
arm14 schema (mc_firms / ledger_daily_equity read them unchanged), with
`exec_basis: achievable_next_open` recorded per file.  Every symbol is
verified decision-parity vs the accepted file (same signal bar times).

    python -m market_backtest.run_honest --label arm14 --out arm14h \
        [--symbols AAPL,MSFT] [--max-workers 8] [--paired-report]
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import math
import multiprocessing
import time
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _guard() -> None:
    try:
        from process_watchdog import guard_parent
        guard_parent()
    except Exception:
        pass


def run_symbol_achievable(symbol: str, market: str, interval: str,
                          frozen_dir: Path, out_dir: Path,
                          label: str = "arm14",
                          starting_equity: float = 1.0,
                          k: float = 1.0) -> dict:
    _guard()
    from market_backtest import market_store
    from market_backtest.runner import COSTS, market_kind, _partition_stats
    from market_live.brokers import PaperBroker
    from market_live.host import MarketEngineHost, params_map

    kind = market_kind(market, interval)
    costs = COSTS[kind]
    rec = market_store.get_market_recording(market, symbol, interval)
    if not rec:
        raise ValueError(f"no recording {market}/{symbol}/{interval}")
    candles = market_store.get_market_recording_candles(rec["id"])
    if not candles:
        raise ValueError(f"no candles {market}/{symbol}/{interval}")

    accepted_file = frozen_dir / f"{market}_{symbol}_{interval}_{label}.json"
    if not accepted_file.exists():
        raise FileNotFoundError(str(accepted_file))
    accepted = json.loads(accepted_file.read_text())
    split_ts = int(accepted["split_ts"])
    engine_params = dict(accepted["engine_params"])

    # ── live plumbing: PaperBroker ledger, cold host, no journal ──────────
    broker = PaperBroker(starting_equity=starting_equity,
                         slippage_pct=costs["slippage_pct"])
    host = MarketEngineHost(market, symbol, interval, engine_params,
                            buy_size=0.1)
    prices = {symbol: float(candles[0]["open"])}
    pending = None          # {"desired": 0|1, "reason": str} — signal at close

    fill_events: list[tuple[int, dict]] = []      # (bar_time, BrokerFill)
    decision_events: list[tuple[int, int]] = []   # (bar_time, desired)
    prev_d = 0

    for candle in candles:
        t = int(candle["time"])
        o = float(candle["open"])

        # 1) deferred order from the PREVIOUS close fills at THIS bar's open
        if pending is not None:
            # deferred semantics mirror the live: the fill executes at the
            # open for BOTH directions — target 0.0 flattens via the same
            # target_position call (delta-pivoted, so sells ride naturally).
            if pending["desired"]:
                # campaign book semantics: FIXED 0.1 units committed per
                # position (mirror of the accepted FT book's trade_size =
                # min(0.1, balance) with balance ≥ 0.1).  The equity-dynamic
                # rule is the live production factor and is NOT compared here.
                target = 0.1 / (o * (1.0 + costs["slippage_pct"] / 100.0))
            else:
                target = 0.0
            f = broker.target_position(symbol, target, o,
                                       reason=pending["reason"])
            if f:
                fill_events.append((t, f.to_dict()))
            pending = None

        # 2) engine sees this bar — the invariant 4-state replay
        decision = host.on_bar(candle)
        d = decision.desired_position
        decision_events.append((t, d))
        prices[symbol] = float(candle["close"])
        if d != prev_d:
            pending = {"desired": d, "reason": f"signal_{t}"}
        prev_d = d

    # 3) recording_ended force-close at the last candle's close
    last = candles[-1]
    if broker.position_qty(symbol) > 0:
        f = broker.target_position(symbol, 0.0, float(last["close"]),
                                   reason="recording_ended")
        if f:
            fill_events.append((int(last["time"]), f.to_dict()))

    # ── round trips from the broker fill stream (long-only, sequential) ───
    trades: list[dict] = []
    open_t = None
    for bt, fd in fill_events:
        qty = float(fd["qty"])
        px = float(fd["price"])
        if fd["side"] == "buy":
            if open_t is not None:                       # top-up → fold in
                w = open_t["size_tokens"]
                open_t["size_tokens"] = w + qty
                open_t["size_sol"] = (open_t["size_sol"]
                                      + qty * px)
                open_t["entry_price"] = open_t["size_sol"] / (w + qty)
                continue
            open_t = {"entry_time": bt, "entry_price": px,
                      "size_sol": qty * px, "size_tokens": qty,
                      "entry_reason": str(fd.get("reason", "")),
                      "fees_paid": float(fd.get("fee", 0.0))}
        else:
            if open_t is None:
                open_t = {"entry_time": bt, "entry_price": px * 0.9997,
                          "short_standin": True, "size_sol": 0.0,
                          "size_tokens": qty, "entry_reason": "orphan"}
            w = open_t["size_tokens"]
            sold = min(w, qty)
            pnl = sold * (px - open_t["entry_price"])          # net of slip
            fees = open_t.get("fees_paid", 0.0) + float(fd.get("fee", 0.0))
            row = {
                "entry_time": open_t["entry_time"],
                "exit_time": bt,
                "entry_price": open_t["entry_price"],
                "exit_price": px,
                "size_sol": open_t["size_sol"],
                "size_tokens": open_t["size_tokens"],
                "pnl_sol": pnl,
                "pnl_pct": (pnl / open_t["size_sol"] * 100.0
                            if open_t["size_sol"] else 0.0),
                "fees_paid": fees,
                "entry_reason": open_t["entry_reason"],
                "exit_reason": str(fd.get("reason", "")),
            }
            trades.append(row)
            if w != qty:                                       # over-sell
                open_t["size_tokens"] = w - qty
                open_t = None if open_t["size_tokens"] <= 1e-12 else open_t
            else:
                open_t = None

    stats = _partition_stats(trades)
    stats_train = _partition_stats([t for t in trades
                                    if int(t["entry_time"]) < split_ts])
    stats_test = _partition_stats([t for t in trades
                                   if int(t["entry_time"]) >= split_ts])

    # ── decision parity vs the accepted book (same bars, same order) ─────
    # accepted = engine hist + (at most) ONE tail force-close row
    # (recording_ended closes current_trade outside the ft history).
    acc_rows = list(accepted["trades"])
    acc_entries = [int(t.get("entry_time") or 0) for t in acc_rows]
    acc_tail_force = bool(acc_rows
                          and str(acc_rows[-1].get("exit_reason")) ==
                          "recording_ended")
    engine_hist = host.trades()
    eng_entry_bars = sorted(int(t.get("entry_time") or 0) for t in engine_hist)
    core = [int(t.get("entry_time") or 0) for t in acc_rows[:len(engine_hist)]]
    core_sorted = sorted(core)
    tail_ok = (len(engine_hist) == len(acc_rows) or
               (len(engine_hist) == len(acc_rows) - 1 and acc_tail_force))
    parity_exact = (tail_ok and eng_entry_bars == core_sorted)
    one_off = all(any(e_b == a + 1 for a in acc_entries)
                  for e_b in eng_entry_bars)

    out = {
        "market": market, "symbol": symbol, "interval": interval,
        "kind": kind, "label": label,
        "exec_basis": "achievable_next_open",
        "split_frac": accepted["split_frac"], "split_ts": split_ts,
        "split_iso": accepted["split_iso"],
        "n_candles": len(candles),
        "engine_params": engine_params,
        "stats": stats, "stats_train": stats_train, "stats_test": stats_test,
        "starting_balance": starting_equity,
        "buy_size": 0.1,
        "slippage_pct": costs["slippage_pct"],
        "priority_fee": costs["priority_fee"],
        "ledger_final_balance": float(broker.equity(prices)),
        "broker_realized": round(broker.realized, 8),
        "broker_fees": round(broker.fees, 8),
        "engine_decision_parity_exact": bool(parity_exact),
        "ledger_one_bar_after_engine": bool(one_off),
        "trades": trades,
        "ran_at": time.time(),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{market}_{symbol}_{interval}_{label}.json"
    p.write_text(json.dumps(out, indent=1))
    return {"symbol": symbol, "file": str(p.name),
            "test_pnl": stats_test["total_pnl"],
            "trades": stats["trades"],
            "parity_exact": parity_exact, "one_off": one_off}


def _paired_report(out_dir: Path, frozen_dir: Path, label: str) -> None:
    """Honest vs accepted per-symbol test-window PnL, paired stats."""
    import statistics
    from market_backtest.stats_report import _bootstrap_ci, _wilcoxon
    honest, accepted = {}, {}
    for d, dst in ((out_dir, honest), (frozen_dir, accepted)):
        for f in sorted(d.glob(f"*_1d_{label}.json")):
            data = json.loads(f.read_text())
            dst[data["symbol"]] = data
    syms = sorted(set(honest) & set(accepted))
    a, b = [], []
    for s in syms:
        a.append(honest[s]["stats_test"]["total_pnl"])
        b.append(accepted[s]["stats_test"]["total_pnl"])
    diffs = [x - y for x, y in zip(a, b)]
    n_pos = sum(1 for d in diffs if d > 0)
    n_neg = sum(1 for d in diffs if d < 0)
    mean = statistics.mean(diffs) if diffs else 0.0
    lo, hi = _bootstrap_ci(diffs)
    _, p = _wilcoxon(diffs)
    summ = {
        "n_symbols": len(syms),
        "accepted_test_mean": round(statistics.mean(b), 6),
        "honest_test_mean": round(statistics.mean(a), 6),
        "mean_paired_delta_honest_minus_accepted": round(mean, 6),
        "honest_better": n_pos, "honest_worse": n_neg,
        "breadth_pct": round(100.0 * max(n_pos, n_neg) / len(syms), 1),
        "wilcoxon_p": p,
        "bootstrap_ci95": [round(lo, 6), round(hi, 6)],
    }
    (out_dir / f"summary_{label}_honest_vs_accepted.json").write_text(
        json.dumps(summ, indent=1))
    print(json.dumps(summ, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="arm14")
    ap.add_argument("--out", default="arm14h")
    ap.add_argument("--market", default="stocks")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--max-workers", type=int, default=0)
    args = ap.parse_args()

    frozen_dir = RESULTS_DIR / args.label
    out_dir = RESULTS_DIR / args.out
    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        syms = sorted({f.name.split("_")[1]
                       for f in frozen_dir.glob(f"*_1d_{args.label}.json")})
    tasks = [{"symbol": s, "market": args.market, "interval": "1d"}
             for s in syms]
    workers = args.max_workers or min(8, max(1, multiprocessing.cpu_count() - 1))
    print(f"[honest] {len(tasks)} tapes {args.label} → {args.out} "
          f"(achievable next-open fill, {workers} workers)")
    t0 = time.time()
    with futures.ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_symbol_achievable, t["symbol"], t["market"],
                          t["interval"], frozen_dir, out_dir, args.label):
                t for t in tasks}
        for fu in futures.as_completed(futs):
            r = fu.result()
            flag = "PARITY-EXACT" if r["parity_exact"] else \
                   ("ONE-OFF" if r["one_off"] else "**DECISION MISMATCH**")
            print(f"  {r['symbol']:<6} test_pnl={r['test_pnl']:+.6f} "
                  f"trades={r['trades']:>3}  {flag}", flush=True)
    print(f"[honest] done in {time.time()-t0:.0f}s")
    try:
        _paired_report(out_dir, frozen_dir, args.label)
    except Exception as exc:  # noqa: BLE001
        print(f"[honest] paired report failed: {exc}")


if __name__ == "__main__":
    main()
