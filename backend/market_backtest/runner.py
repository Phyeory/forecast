"""runner.py — market-tape backtest executor (V2M clone engine).

Mirrors `backtester.run_backtest`'s replay EXACTLY — the same ForwardTester,
the same 4-state intra-candle expansion with bull/bear midpoint ordering,
state-4-only volume landing, and the `recording_ended` force-close — but:

  * runs the market-tailored engine CLONE (`strategy_engineV2M.py`,
    fd. `MarketStrategyEngine` / ζ=0 default) instead of the memecoin V2 —
    the memecoin engine file is untouched (sha-verified per batch run).
  * reads candle tapes from `market_data.db` instead of memecoin recordings
    and consumes PRE-BAKED market calibration (`market_calibration.py`).
  * splits the tape into TRAIN / UNSEEN segments: engine params are frozen
    from the train prefix ONLY (`candles[:split_idx]`), the replay then runs
    over the full tape, and trade statistics are partitioned by entry time
    into `stats_train` / `stats_test` for honest out-of-sample reads.

Everything is additive/self-contained: nothing in backtester.py,
forward_tester.py, strategy_engineV2.py, or the live booking system is
modified (pipeline parity preserved by construction — same executor code
path, cloned engine math).
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

# Prop-tradable daily universes (fetch_markets.py) reuse the stocks
# daily-bar engine class (BAR_SPEC / calibration / costs).
MARKET_CLASS = {
    "idx_fut": "stocks",
    "intl_idx": "stocks",
    "comm_fut": "stocks",
    "fx": "stocks",
    "crypto_spot": "stocks",
    "etf": "stocks",
}


class MarketForwardTester(ForwardTester):
    """ForwardTester hosting the V2M clone engine.

    ForwardTester constructs its engine inside `__init__` via
    `create_engine(engine_version, **engine_kwargs)` — a single call site we
    don't want to fork.  Instead we let the base build the cheap pure-Python
    V1 placeholder, then swap in the clone.  Every downstream FT path only
    touches `self.engine` afterwards, so the swap is behaviourally exact.
    """

    def __init__(self, *args, market_engine_kwargs: Optional[dict] = None,
                 **kwargs):
        kwargs.pop("engine_kwargs", None)
        super().__init__(*args, engine_kwargs={}, engine_version=1, **kwargs)
        from .strategy_engineV2M import StrategyEngineV2MAdapter
        self.engine = StrategyEngineV2MAdapter(**(market_engine_kwargs or {}))


def _partition_stats(trades: list[dict]) -> dict:
    """WR / PnL / exit-mix over a trade slice (dicts from Trade.to_dict)."""
    n = len(trades)
    wins = sum(1 for t in trades if float(t.get("pnl_sol") or 0.0) > 0.0)
    pnl = sum(float(t.get("pnl_sol") or 0.0) for t in trades)
    reasons: dict[str, int] = {}
    exit_pnl: dict[str, float] = {}
    hold_days: list[float] = []
    for t in trades:
        r = str(t.get("exit_reason") or "?")
        reasons[r] = reasons.get(r, 0) + 1
        exit_pnl[r] = exit_pnl.get(r, 0.0) + float(t.get("pnl_sol") or 0.0)
        et, xt = int(t.get("entry_time") or 0), int(t.get("exit_time") or 0)
        if et and xt:
            hold_days.append((xt - et) / 86400.0)
    pnls = [float(t.get("pnl_sol") or 0.0) for t in trades]
    return {
        "trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": (100.0 * wins / n) if n else 0.0,
        "total_pnl": pnl,
        "avg_pnl": (pnl / n) if n else 0.0,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
        "exit_reasons": reasons,
        "exit_pnl": {k: round(v, 6) for k, v in exit_pnl.items()},
        "hold_days_p50": (sorted(hold_days)[len(hold_days) // 2]
                          if hold_days else 0.0),
        "hold_days_p90": (sorted(hold_days)[int(0.9 * (len(hold_days) - 1))]
                          if hold_days else 0.0),
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
    split_frac: float = 0.7,
    save: bool = True,
) -> dict:
    """One tape → calibrated (train-frozen) engine params → full replay.

    Statistics are partitioned at the train/test boundary by trade ENTRY
    time: entries before the boundary are train (in-sample for the
    calibration), entries at/after it are unseen out-of-sample.
    """
    kind = f"{market}_{interval}" if market == "crypto_perp" else market
    kind = MARKET_CLASS.get(kind, kind)
    spec = BAR_SPEC[kind]
    bar_seconds = spec["bar_seconds"]

    rec = get_market_recording(market, symbol, interval)
    if not rec:
        raise ValueError(f"No market recording for {market}/{symbol}/{interval}")
    candles = get_market_recording_candles(rec["id"])
    if not candles:
        raise ValueError(f"No candles for {market}/{symbol}/{interval}")

    n = len(candles)
    split_idx = max(1, min(n - 1, int(n * float(split_frac))))
    split_ts = int(candles[split_idx]["time"])
    train_tape = candles[:split_idx]

    # Calibration: TRAIN PREFIX ONLY (causal).  Percentiles, σ gate and the
    # KDE/τ knobs see nothing that happens after `split_ts`.
    calib = calibrate_market_params(
        train_tape, kind,
        calib_bars if calib_bars is not None else len(train_tape))
    engine_params = dict(calib["engine_params"])
    if engine_params_override:
        engine_params.update(engine_params_override)

    costs = COSTS[kind]

    ft = MarketForwardTester(
        starting_balance=starting_balance,
        buy_size_sol=buy_size,
        priority_fee=costs["priority_fee"],
        bribe_fee=0.0,
        slippage_pct=costs["slippage_pct"],
        market_engine_kwargs=engine_params,
        exec_model="instant",
    )

    # Engine-keyed latency injection mirrors backtester.run_backtest.
    _eng_delay = float(getattr(ft.engine, "v2_entry_delay_seconds", 0.0))
    if _eng_delay > 0.0:
        ft.enable_entry_latency(_eng_delay)
    _eng_exit_delay = float(getattr(ft.engine, "v2_exit_delay_seconds", 0.0))
    if _eng_exit_delay > 0.0:
        ft.enable_exit_latency(
            _eng_exit_delay,
            armed_only=float(getattr(ft.engine, "v2_exit_delay_armed_only", 0.0)) > 0.0)

    ft_update = ft.update

    last_candle = None
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

    # ── Train / unseen partition (by ENTRY time) ─────────────────────
    trades_train = [t for t in trades if int(t.get("entry_time") or 0) < split_ts]
    trades_test = [t for t in trades if int(t.get("entry_time") or 0) >= split_ts]
    stats_train = _partition_stats(trades_train)
    stats_test = _partition_stats(trades_test)

    out_dir = Path(results_dir) if results_dir else RESULTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = f"{market}_{symbol}_{interval}"
    if label:
        tag = f"{tag}_{label}"
    out = {
        "market": market, "symbol": symbol, "interval": interval,
        "kind": kind, "label": label,
        "split_frac": split_frac, "split_ts": split_ts,
        "split_iso": time.strftime("%Y-%m-%d", time.gmtime(split_ts)),
        "n_candles": n, "n_candles_train": split_idx,
        "calibration": calib["audit"],
        "engine_params": engine_params,
        "stats": stats,
        "stats_train": stats_train,
        "stats_test": stats_test,
        "starting_balance": starting_balance,
        "buy_size": buy_size,
        "slippage_pct": costs["slippage_pct"],
        "priority_fee": costs["priority_fee"],
        "ran_at": time.time(),
        "trades": trades,
    }
    if save:
        with open(out_dir / f"{tag}.json", "w") as fh:
            json.dump(out, fh, default=str)

    summary = {
        "market": market, "symbol": symbol, "interval": interval,
        "stats": stats, "stats_train": stats_train, "stats_test": stats_test,
        "n_trades": len(trades),
        "n_candles": n,
        "split_iso": out["split_iso"],
        "calibration": calib["audit"],
        "engine_params": engine_params,
    }
    return summary
