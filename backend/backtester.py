"""
Backtester — Run the ForwardTester on saved price recordings.

Takes a recording_id, fetches candles from the price DB, runs them through
ForwardTester, and saves the full results (candles + signals + regimes +
trades) to the backtest DB.
"""

from __future__ import annotations
from typing import Optional

from forward_tester import ForwardTester
from data_store import (
    get_recording,
    get_recording_candles,
    create_backtest,
)


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

    # Create the forward tester
    ft = ForwardTester(
        starting_balance=starting_balance,
        buy_size_sol=buy_size_sol,
        priority_fee=priority_fee,
        bribe_fee=bribe_fee,
        slippage_pct=slippage_pct,
        engine_kwargs=engine_params,
    )

    # Run all candles through the engine
    candle_results = []
    for candle in candles:
        result = ft.update(
            time=int(candle["time"]),
            o=candle["open"],
            h=candle["high"],
            l=candle["low"],
            c=candle["close"],
            volume=candle.get("volume", 0),
        )

        # Build a flat record for storage
        indicators = result.get("indicators", {})
        fwd = result.get("forward_test", {})

        candle_results.append({
            "time": candle["time"],
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "volume": candle.get("volume", 0),
            "regime": result.get("regime", "idle"),
            "direction": result.get("direction", "none"),
            "signal": result.get("signal", "none"),
            "signal_strength": indicators.get("signal_strength", 0),
            "ema_fast": indicators.get("ema_fast"),
            "ema_slow": indicators.get("ema_slow"),
            "atr": indicators.get("atr"),
            "roc": indicators.get("roc"),
            "confidence": indicators.get("trend_confidence", 0),
            "trade_action": fwd.get("trade_action"),
            "trade_label": fwd.get("trade_label"),
            "balance": fwd.get("balance"),
            "unrealized_pnl": fwd.get("unrealized_pnl", 0),
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
        timeframe=recording["timeframe"],
        engine_params=engine_params,
        stats=stats,
        candle_results=candle_results,
        trades=trades,
    )

    return {
        "backtest_id": bt_id,
        "recording_id": recording_id,
        "mint": recording["mint"],
        "token_name": recording.get("token_name", ""),
        "token_symbol": recording.get("token_symbol", ""),
        "timeframe": recording["timeframe"],
        "candle_count": len(candle_results),
        "stats": stats,
        "trade_count": len(trades),
    }
