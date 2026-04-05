"""
Backtester — Run the ForwardTester on saved price recordings.

Takes a recording_id, fetches candles from the price DB, runs them through
ForwardTester, and saves the full results (candles + signals + regimes +
trades) to the backtest DB.

Signal model — identical to ForwardTester / LiveTrader:
  Each stored OHLCV candle is expanded into a stream of synthetic price
  ticks that are piped through a CandleAggregator exactly as the live
  stream does in main.py.  This means the strategy engine sees price
  evolve *within* a candle (open → mid-point → close) so signals fire
  at the same intra-candle moment they would during live trading, rather
  than being delayed to the start of the next bar.

  Tick sequence per OHLCV candle:
    1. open   (start of bar)
    2. high   if close > open  (bullish bar — high comes before low)
       low    if close <= open (bearish bar — low comes before high)
    3. low    (bullish bar)
       high   (bearish bar)
    4. close  (end of bar)

  All ticks carry the same candle timestamp so the aggregator treats them
  as updates to the same bar.  Only the final tick (close) has non-zero
  volume to avoid double-counting.
"""

from __future__ import annotations
from typing import Optional

from forward_tester import ForwardTester
from candle_aggregator import CandleAggregator
from data_store import (
    get_recording,
    get_recording_candles,
    create_backtest,
)


def _candle_to_ticks(candle: dict) -> list[dict]:
    """
    Expand a completed OHLCV candle into an ordered sequence of price ticks.

    The ordering heuristic (high-before-low for bull bars, low-before-high
    for bear bars) matches the most common real-world price path and gives
    the strategy engine the same signal-firing opportunity it would have
    during live trading.
    """
    t   = float(candle["time"])
    o   = candle["open"]
    h   = candle["high"]
    l   = candle["low"]
    c   = candle["close"]
    vol = candle.get("volume", 0)

    bullish = c >= o

    if bullish:
        mid_first, mid_second = h, l
    else:
        mid_first, mid_second = l, h

    # Volume attributed only to the closing tick (avoids inflating volume)
    return [
        {"price": o,          "volume": 0.0,  "timestamp": t},
        {"price": mid_first,  "volume": 0.0,  "timestamp": t},
        {"price": mid_second, "volume": 0.0,  "timestamp": t},
        {"price": c,          "volume": vol,  "timestamp": t},
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

    # CandleAggregator — mirrors the one used in main.py live stream
    aggregator = CandleAggregator(timeframe)

    # We collect one result per *candle* (final tick of each bar).
    # Intermediate ticks update the aggregator/engine but we only store the
    # candle-level snapshot so the output shape matches the old format.
    candle_results = []

    for candle in candles:
        ticks = _candle_to_ticks(candle)
        last_result = None

        for tick in ticks:
            agg_candle, _is_new = aggregator.process_trade(
                tick["price"],
                tick["volume"],
                tick["timestamp"],
                synthetic=True,   # synthetic=True so aggregator doesn't open ghost candles
            )
            agg_dict = agg_candle.to_dict()

            result = ft.update(
                time=agg_dict["time"],
                o=agg_dict["open"],
                h=agg_dict["high"],
                l=agg_dict["low"],
                c=agg_dict["close"],
                volume=agg_dict.get("volume", 0),
            )
            last_result = result

        # Store one record per original candle using the final tick's result
        if last_result is not None:
            indicators = last_result.get("indicators", {})
            fwd = last_result.get("forward_test", {})

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
                "trade_action":    fwd.get("trade_action"),
                "trade_label":     fwd.get("trade_label"),
                "balance":         fwd.get("balance"),
                "unrealized_pnl":  fwd.get("unrealized_pnl", 0),
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
