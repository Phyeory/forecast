"""runner.py — market-tape backtest executor.

Mirrors `backtester.run_backtest`'s replay EXACTLY — the same ForwardTester,
the same 4-state intra-candle expansion with bull/bear midpoint ordering,
state-4-only volume landing, and the `recording_ended` force-close — but
reads candle tapes from `market_data.db` instead of memecoin recordings and
consumes PRE-BAKED market calibration (`market_calibration.py`) instead of
the pump.fun calibration stack (`calibration_startup.initialize_calibration_
sync` is deliberately NOT called: it is memecoin-population specific and
would overwrite the recalibrated coefficients).

Everything is additive/self-contained: nothing in backtester.py,
forward_tester.py, strategy_engineV2.py, or the live booking system is
modified (pipeline parity preserved by construction — same engine code,
same executor code path).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from forward_tester import ForwardTester  # backend cwd; see market_calibration note

from .market_store import get_market_recording, get_market_recording_candles
from .market_calibration import calibrate_market_params, BAR_SPEC

RESULTS_DIR = Path(__file__).resolve().parent / "results"

COSTS = {
    # slippage_pct = simulated step-through friction (%) of the bar's spread;
    # priority_fee = flat per-trade cost in balance units.  Stocks pay
    # exchange-grade (near-zero) taker costs; perps keep crypto-style
    # maker/taker around 5 bp.
    "stocks":         {"slippage_pct": 0.03, "priority_fee": 0.0},
    "crypto_perp_1h": {"slippage_pct": 0.05, "priority_fee": 0.0},
    "crypto_perp_1d": {"slippage_pct": 0.05, "priority_fee": 0.0},
}


def run_market_backtest(
    market: str,
    symbol: str,
    interval: str,
    starting_balance: float = 1.0,
    buy_size: float = 0.1,
    calib_bars: Optional[int] = None,
    engine_params_override: Optional[dict] = None,
    label: str = "",
    results_dir: Optional[Path] = None,
) -> dict:
    """One tape → calibrated engine params → full replay → artifacts."""
    kind = f"{market}_{interval}" if market == "crypto_perp" else market
    spec = BAR_SPEC[kind]
    bar_seconds = spec["bar_seconds"]

    rec = get_market_recording(market, symbol, interval)
    if not rec:
        raise ValueError(f"No market recording for {market}/{symbol}/{interval}")
    candles = get_market_recording_candles(rec["id"])
    if not candles:
        raise ValueError(f"No candles for {market}/{symbol}/{interval}")

    calib = calibrate_market_params(candles, kind, calib_bars)
    engine_params = dict(calib["engine_params"])
    if engine_params_override:
        engine_params.update(engine_params_override)

    costs = COSTS[kind]
    priority_fee = costs["priority_fee"]
    bribe_fee = 0.0

    ft = ForwardTester(
        starting_balance=starting_balance,
        buy_size_sol=buy_size,
        priority_fee=priority_fee,
        bribe_fee=bribe_fee,
        slippage_pct=costs["slippage_pct"],
        engine_kwargs=engine_params,
        engine_version=2,
        exec_model="instant",
    )

    # Engine-keyed latency injection mirrors backtester.run_backtest: the
    # market calibration pins delays at 0.0, so both blocks are inert here
    # (kept for structural parity if a future cell raises the engine knob).
    _eng_delay = float(getattr(ft.engine, "v2_entry_delay_seconds", 0.0))
    if _eng_delay > 0.0:
        ft.enable_entry_latency(_eng_delay)
    _eng_exit_delay = float(getattr(ft.engine, "v2_exit_delay_seconds", 0.0))
    if _eng_exit_delay > 0.0:
        ft.enable_exit_latency(
            _eng_exit_delay,
            armed_only=float(getattr(ft.engine, "v2_exit_delay_armed_only", 0.0)) > 0.0)

    ft_update = ft.update
    engine = ft.engine

    for candle in candles:
        t = int(candle["time"])
        o = float(candle["open"])
        h = float(candle["high"])
        l = float(candle["low"])
        c = float(candle["close"])
        vol = float(candle.get("volume", 0.0))
        buy_vol = float(candle.get("buy_volume", 0.0))
        sell_vol = float(candle.get("sell_volume", 0.0))

        # Bull bar: high comes before low; bear bar: low before high
        bullish = c >= o
        if bullish:
            mid_first, mid_second = h, l
        else:
            mid_first, mid_second = l, h

        # State 1: open tick
        ft_update(time=t, o=o, h=o, l=o, c=o, volume=0.0,
                  _build_full_result=False)
        # State 2: first extreme
        h2 = max(o, mid_first)
        l2 = min(o, mid_first)
        ft_update(time=t, o=o, h=h2, l=l2, c=mid_first, volume=0.0,
                  _build_full_result=False)
        # State 3: both extremes
        ft_update(time=t, o=o, h=h, l=l, c=mid_second, volume=0.0,
                  _build_full_result=False)
        # State 4: close tick — buy/sell split lands here
        ft_update(time=t, o=o, h=h, l=l, c=c, volume=vol,
                  buy_volume=buy_vol, sell_volume=sell_vol,
                  _build_full_result=False)

        last_candle = (t, o, h, l, c)

    if ft.current_trade is not None:
        t_last, o_last, h_last, l_last, c_last = last_candle
        ft._close_long(o_last, h_last, l_last, c_last, t_last,
                       reason="recording_ended")

    trades = [t.to_dict() for t in ft.trade_history]
    stats = ft.stats.to_dict()

    out_dir = Path(results_dir) if results_dir else RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{market}_{symbol}_{interval}"
    if label:
        tag = f"{tag}_{label}"
    out = {
        "market": market, "symbol": symbol, "interval": interval,
        "kind": kind, "label": label,
        "calibration": calib["audit"],
        "engine_params": engine_params,
        "stats": stats,
        "n_candles": len(candles),
        "starting_balance": starting_balance,
        "buy_size": buy_size,
        "slippage_pct": costs["slippage_pct"],
        "priority_fee": priority_fee,
        "ran_at": time.time(),
        "trades": trades,
    }
    with open(out_dir / f"{tag}.json", "w") as fh:
        json.dump(out, fh, default=str)

    summary = {
        "market": market, "symbol": symbol, "interval": interval,
        "stats": stats,
        "n_trades": len(trades),
        "n_candles": len(candles),
        "calibration": calib["audit"],
    }
    return summary
