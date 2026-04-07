"""
Backtester — Run the ForwardTester on saved price recordings.

Takes a recording_id, fetches candles from the price DB, runs them through
ForwardTester, and saves the full results (candles + signals + regimes +
trades) to the backtest DB.

Signal model — identical to ForwardTester / LiveTrader:
  Each stored OHLCV candle is expanded into four accumulated-candle states
  that are fed into ft.update() one at a time, exactly replicating what
  main.py does via the live CandleAggregator.

  For each completed candle the CandleAggregator would have emitted these
  four intermediate states (one per intra-candle trade tick):

    State 1  →  o=open, h=open,  l=open,  c=open,  vol=0
    State 2  →  o=open, h=H/L,   l=L/H,   c=mid1,  vol=0
    State 3  →  o=open, h=high,  l=low,   c=mid2,  vol=0
    State 4  →  o=open, h=high,  l=low,   c=close, vol=full

  mid1/mid2 follow the standard high-before-low heuristic for bull bars and
  low-before-high for bear bars (matches _candle_to_ticks ordering).

  Because ft.update() is called with the same sequence of accumulated intra-
  candle states that the live engine sees, all rolling buffers (Kalman filter,
  EMA, ATR, trend confidence, etc.) evolve identically — signals fire at the
  same intra-candle moment as live trading.

  The 1-bar-delay execution model is preserved: a pending BUY/EXIT queued
  during candle N executes at State 1 of candle N+1 (open price of next bar).
"""

from __future__ import annotations
from typing import Optional

from forward_tester import ForwardTester
from data_store import (
    get_recording,
    get_recording_candles,
    create_backtest,
)


def _candle_to_accumulated_states(candle: dict) -> list[dict]:
    """
    Expand a completed OHLCV candle into four accumulated intra-candle states.

    Each state represents the OHLCV snapshot the CandleAggregator would have
    emitted to the ForwardTester after each synthetic intra-candle tick during
    live trading.  The open is always the candle open; high/low accumulate;
    close is the current tick price; volume appears only on the final tick.
    """
    t   = int(candle["time"])
    o   = candle["open"]
    h   = candle["high"]
    l   = candle["low"]
    c   = candle["close"]
    vol = candle.get("volume", 0)

    # Bull bar: high comes before low; bear bar: low before high
    bullish = c >= o
    mid_first, mid_second = (h, l) if bullish else (l, h)

    return [
        # State 1: open tick — candle just started
        {"time": t, "open": o, "high": o,              "low": o,              "close": o,         "volume": 0.0},
        # State 2: first extreme (high for bulls, low for bears)
        {"time": t, "open": o, "high": max(o, mid_first),  "low": min(o, mid_first),  "close": mid_first, "volume": 0.0},
        # State 3: second extreme — both extremes now known
        {"time": t, "open": o, "high": h,              "low": l,              "close": mid_second, "volume": 0.0},
        # State 4: close tick — full OHLCV, volume here to avoid double-counting
        {"time": t, "open": o, "high": h,              "low": l,              "close": c,          "volume": vol},
    ]


def run_backtest(
    recording_id: int,
    engine_params: Optional[dict] = None,
    buy_size_sol: float = 0.1,
    priority_fee: float = 0.0001,
    bribe_fee: float = 0.00001,
    slippage_pct: float = 1.0,
    starting_balance: float = 1.0,
) -> dict:
    """
    Run a full backtest on a saved recording.

    Returns a summary dict with the backtest_id and stats.
    """
    recording = get_recording(recording_id)
    if not recording:
        raise ValueError(f"Recording {recording_id} not found")

    candles = get_recording_candles(recording_id)
    if not candles:
        raise ValueError(f"Recording {recording_id} has no candles")

    if engine_params is None:
        engine_params = {}

    timeframe = recording["timeframe"]

    # Create the forward tester
    ft = ForwardTester(
        starting_balance=starting_balance,
        buy_size_sol=buy_size_sol,
        priority_fee=priority_fee,
        bribe_fee=bribe_fee,
        slippage_pct=slippage_pct,
        engine_kwargs=engine_params,
    )

    # One chart result per stored candle
    candle_results = []

    for candle in candles:
        states = _candle_to_accumulated_states(candle)

        # ── Call ft.update() once per intra-candle state ──────────────────────
        # This is identical to what main.py does in _process_stream():
        #   candle, is_new = aggregator.process_trade(...)
        #   result = forward_tester.update(time, o, h, l, c, vol)
        #
        # Any pending signal from the previous candle executes at State 1
        # (open of this candle) — correct 1-bar-delay model.
        # The engine may queue a new signal at any state; it executes at
        # State 1 of the *next* candle.
        trade_action_for_candle: Optional[str] = None
        trade_label_for_candle:  Optional[str] = None
        last_result: Optional[dict] = None

        for state in states:
            result = ft.update(
                time=state["time"],
                o=state["open"],
                h=state["high"],
                l=state["low"],
                c=state["close"],
                volume=state["volume"],
            )
            fwd = result.get("forward_test", {})
            # Capture the first trade action that fires during this candle
            if trade_action_for_candle is None and fwd.get("trade_action"):
                trade_action_for_candle = fwd["trade_action"]
                trade_label_for_candle  = fwd.get("trade_label")
            last_result = result

        # Store one record per original candle.
        # Regime / signal / indicators taken from the final-tick state.
        # trade_action shows what actually happened during this candle.
        assert last_result is not None
        indicators = last_result.get("indicators", {})
        fwd_final  = last_result.get("forward_test", {})

        candle_results.append({
            "time":            candle["time"],
            "open":            candle["open"],
            "high":            candle["high"],
            "low":             candle["low"],
            "close":           candle["close"],
            "volume":          candle.get("volume", 0),
            "regime":          last_result.get("regime", "idle"),
            "direction":       last_result.get("direction", "none"),
            "signal":          last_result.get("signal", "none"),
            "signal_strength": indicators.get("signal_strength", 0),
            "ema_fast":        indicators.get("ema_fast"),
            "ema_slow":        indicators.get("ema_slow"),
            "atr":             indicators.get("atr"),
            "roc":             indicators.get("roc"),
            "confidence":      indicators.get("trend_confidence", 0),
            "trade_action":    trade_action_for_candle,
            "trade_label":     trade_label_for_candle,
            "balance":         fwd_final.get("balance"),
            "unrealized_pnl":  fwd_final.get("unrealized_pnl", 0),
        })

    # Gather trade history
    trades = [t.to_dict() for t in ft.trade_history]
    stats = ft.stats.to_dict()

    # Save to backtest DB
    bt_id = create_backtest(
        recording_id=recording_id,
        mint=recording["mint"],
        token_name=recording.get("token_name", ""),
        token_symbol=recording.get("token_symbol", ""),
        timeframe=timeframe,
        engine_params=engine_params,
        stats=stats,
        candle_results=candle_results,
        trades=trades,
    )

    return {
        "backtest_id":   bt_id,
        "recording_id":  recording_id,
        "mint":          recording["mint"],
        "token_name":    recording.get("token_name", ""),
        "token_symbol":  recording.get("token_symbol", ""),
        "timeframe":     timeframe,
        "candle_count":  len(candle_results),
        "stats":         stats,
        "trade_count":   len(trades),
    }
