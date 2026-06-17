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

Performance optimisation:
  Intra-candle states are generated inline (no list allocation) and the
  ForwardTester skips building the full result dict for intermediate states
  via the `_build_full_result=False` fast path.
"""

from __future__ import annotations
from typing import Optional
from concurrent.futures import ProcessPoolExecutor, as_completed
import os

from forward_tester import ForwardTester
from data_store import (
    get_recording,
    get_recording_candles,
    create_backtest,
    list_recordings,
)


import multiprocessing


def score_batch(
    engine_params: Optional[dict] = None,
    buy_size_sol: float = 0.1,
    priority_fee: float = 0.0001,
    bribe_fee: float = 0.00001,
    slippage_pct: float = 1.0,
    starting_balance: float = 1.0,
    engine_version: int = 1,
) -> tuple[float, int, int]:
    """
    Run ALL completed recordings through the engine without saving to DB.

    Returns:
        (win_rate_pct, total_trades, total_wins)

    Used by the confidence auto-tuner to score candidate parameters
    quickly without polluting the backtest database.
    """
    recordings = list_recordings()
    completed = [r for r in recordings if r.get("status") == "completed"]

    if not completed:
        return 0.0, 0, 0

    if engine_params is None:
        engine_params = {}

    total_trades = 0
    winning_trades = 0

    for rec in completed:
        try:
            candles = get_recording_candles(rec["id"])
            if not candles:
                continue

            ft = ForwardTester(
                starting_balance=starting_balance,
                buy_size_sol=buy_size_sol,
                priority_fee=priority_fee,
                bribe_fee=bribe_fee,
                slippage_pct=slippage_pct,
                engine_kwargs=engine_params,
                engine_version=engine_version,
            )

            ft_update = ft.update

            for candle in candles:
                t   = int(candle["time"])
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

                # Replicate the 4-state tick expansion used in run_backtest
                ft_update(time=t, o=o, h=o, l=o, c=o, volume=0.0,
                          _build_full_result=False)
                h2 = max(o, mid_first)
                l2 = min(o, mid_first)
                ft_update(time=t, o=o, h=h2, l=l2, c=mid_first, volume=0.0,
                          _build_full_result=False)
                ft_update(time=t, o=o, h=h, l=l, c=mid_second, volume=0.0,
                          _build_full_result=False)
                ft_update(time=t, o=o, h=h, l=l, c=c, volume=vol,
                          _build_full_result=False)

            stats = ft.stats
            total_trades  += stats.total_trades
            winning_trades += stats.winning_trades

        except Exception:
            continue

    win_rate = winning_trades / total_trades * 100.0 if total_trades > 0 else 0.0
    return win_rate, total_trades, winning_trades

_pool: ProcessPoolExecutor | None = None

def _get_pool(max_workers: int) -> ProcessPoolExecutor:
    global _pool
    if multiprocessing.current_process().name != 'MainProcess':
        raise RuntimeError("Cannot spawn process pool from a child process")
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=max_workers)
    return _pool


def _run_single_backtest_worker(args: dict) -> dict:
    """Worker function for process pool — must be top-level and picklable."""
    try:
        return run_backtest(**args)
    except Exception as e:
        return {"error": str(e), "recording_id": args.get("recording_id")}


def run_backtest_batch(
    engine_params: Optional[dict] = None,
    buy_size_sol: float = 0.1,
    priority_fee: float = 0.0001,
    bribe_fee: float = 0.00001,
    slippage_pct: float = 1.0,
    starting_balance: float = 1.0,
    max_workers: Optional[int] = None,
    batch_id: Optional[str] = None,
    engine_version: int = 1,
) -> list[dict]:
    """
    Run backtests on ALL completed recordings.

    Uses simple sequential execution (faster for typical recording counts
    due to process spawn overhead being larger than computation time).
    Falls back to parallel processes for very large batches (>20).
    """
    recordings = list_recordings()
    completed = [r for r in recordings if r.get("status") == "completed"]

    if not completed:
        return []

    if engine_params is None:
        engine_params = {}

    common_kwargs = dict(
        engine_params=engine_params,
        buy_size_sol=buy_size_sol,
        priority_fee=priority_fee,
        bribe_fee=bribe_fee,
        slippage_pct=slippage_pct,
        starting_balance=starting_balance,
        batch_id=batch_id,
        engine_version=engine_version,
    )

    # For typical batch sizes, sequential is faster (no spawn overhead)
    if len(completed) <= 20:
        results = []
        for rec in completed:
            try:
                results.append(run_backtest(recording_id=rec["id"], **common_kwargs))
            except Exception as e:
                results.append({"error": str(e), "recording_id": rec["id"]})
        return results

    # Large batches: use parallel processes
    tasks = [{"recording_id": rec["id"], **common_kwargs} for rec in completed]
    workers = max_workers or min(len(tasks), max(1, os.cpu_count() or 4))
    results = []

    pool = _get_pool(workers)
    futures = {pool.submit(_run_single_backtest_worker, t): t for t in tasks}
    for future in as_completed(futures):
        results.append(future.result())

    return results


def run_backtest(
    recording_id: int,
    engine_params: Optional[dict] = None,
    buy_size_sol: float = 0.1,
    priority_fee: float = 0.0001,
    bribe_fee: float = 0.00001,
    slippage_pct: float = 1.0,
    starting_balance: float = 1.0,
    batch_id: Optional[str] = None,
    engine_version: int = 1,
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
        engine_version=engine_version,
    )

    # One chart result per stored candle
    candle_results = []

    # Local refs for speed
    ft_update = ft.update
    engine = ft.engine

    for candle in candles:
        t   = int(candle["time"])
        o   = candle["open"]
        h   = candle["high"]
        l   = candle["low"]
        c   = candle["close"]
        vol = candle.get("volume", 0)

        # Bull bar: high comes before low; bear bar: low before high
        bullish = c >= o
        if bullish:
            mid_first, mid_second = h, l
        else:
            mid_first, mid_second = l, h

        # ── ALL 4 states use fast path — no result dict construction ──────
        trade_action_for_candle: Optional[str] = None
        trade_label_for_candle:  Optional[str] = None

        # State 1: open tick
        result = ft_update(time=t, o=o, h=o, l=o, c=o, volume=0.0,
                           _build_full_result=False)
        fwd = result.get("forward_test")
        if fwd and fwd.get("trade_action"):
            trade_action_for_candle = fwd["trade_action"]
            trade_label_for_candle  = fwd.get("trade_label")

        # State 2: first extreme
        h2 = max(o, mid_first)
        l2 = min(o, mid_first)
        result = ft_update(time=t, o=o, h=h2, l=l2, c=mid_first, volume=0.0,
                           _build_full_result=False)
        fwd = result.get("forward_test")
        if fwd and trade_action_for_candle is None and fwd.get("trade_action"):
            trade_action_for_candle = fwd["trade_action"]
            trade_label_for_candle  = fwd.get("trade_label")

        # State 3: both extremes
        result = ft_update(time=t, o=o, h=h, l=l, c=mid_second, volume=0.0,
                           _build_full_result=False)
        fwd = result.get("forward_test")
        if fwd and trade_action_for_candle is None and fwd.get("trade_action"):
            trade_action_for_candle = fwd["trade_action"]
            trade_label_for_candle  = fwd.get("trade_label")

        # State 4: close tick — also fast path, read from engine directly
        result = ft_update(time=t, o=o, h=h, l=l, c=c, volume=vol,
                           _build_full_result=False)
        fwd = result.get("forward_test")
        if fwd and trade_action_for_candle is None and fwd.get("trade_action"):
            trade_action_for_candle = fwd["trade_action"]
            trade_label_for_candle  = fwd.get("trade_label")

        # Read indicators directly from engine state — no dict overhead
        candle_results.append({
            "time":            t,
            "open":            o,
            "high":            h,
            "low":             l,
            "close":           c,
            "volume":          vol,
            "regime":          engine.regime.value,
            "direction":       engine.direction.value,
            "signal":          result.get("signal", "none") if result else "none",
            "signal_strength": engine.signal_strength,
            "ema_fast":        engine.ema_fast_val,
            "ema_slow":        engine.ema_slow_val,
            "atr":             engine.atr_val,
            "roc":             engine.m_hat,
            "confidence":      engine.trend_confidence,
            "trade_action":    trade_action_for_candle,
            "trade_label":     trade_label_for_candle,
            "balance":         round(ft.balance, 6),
            "unrealized_pnl":  0,
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
        batch_id=batch_id,
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
