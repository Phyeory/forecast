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
import json
import re
import datetime

from forward_tester import ForwardTester
from data_store import (
    get_recording,
    get_recording_candles,
    get_holder_flow,
    create_backtest,
    list_recordings,
)


import multiprocessing

# Directory where per-token JSON trade logs are written.
# Allow override via env var so V2 iteration runs can persist to a safe location
# (e.g. backend/v2_results/) that is NOT wiped by external V1 param-sweep scripts
# that do `rm -rf backend/backtest_results`.
_RESULTS_DIR = os.environ.get(
    "BACKTEST_RESULTS_DIR",
    os.path.join(os.path.dirname(__file__), "backtest_results"),
)


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


def _run_backtest_worker_chunk(tasks: list[dict]) -> list[dict]:
    """Run a balanced group of recordings in one process.

    V2 imports SciPy/Numba and compiles kernels lazily.  A chunk keeps each
    warm worker busy through several recordings while retaining process
    isolation and deterministic per-recording execution.
    """
    return [_run_single_backtest_worker(task) for task in tasks]


def _balanced_task_chunks(tasks: list[dict], max_workers: int) -> list[list[dict]]:
    """Greedily balance recording work by declared candle count."""
    worker_count = max(1, min(max_workers, len(tasks)))
    groups: list[list[dict]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for task in sorted(tasks, key=lambda t: t["candle_count"], reverse=True):
        i = min(range(worker_count), key=loads.__getitem__)
        groups[i].append(task["kwargs"])
        loads[i] += task["candle_count"]
    return [group for group in groups if group]


def run_backtest_batch(
    engine_params: Optional[dict] = None,
    buy_size_sol: float = 0.1,
    priority_fee: float = 0.0001,
    bribe_fee: float = 0.0,
    slippage_pct: float = 1.0,
    starting_balance: float = 1.0,
    max_workers: Optional[int] = None,
    batch_id: Optional[str] = None,
    engine_version: int = 1,
    recording_ids: Optional[list[int]] = None,
    last_night: bool = False,
    last_12h: bool = False,
    # Futures mode (option A) — passthroughs; "spot" defaults = no-op.
    market_type: str = "spot",
    leverage: float = 1.0,
    funding_rate_per_interval: float = 0.0001,
    funding_interval_seconds: int = 28800,
    maintenance_margin_rate: float = 0.005,
    futures_taker_fee: float = 0.00045,
    futures_slippage_pct: Optional[float] = None,
) -> list[dict]:
    """
    Run backtests on ALL completed recordings.

    Uses simple sequential execution (faster for typical recording counts
    due to process spawn overhead being larger than computation time).
    Falls back to parallel processes for very large batches (>20).

    If ``last_night`` is True, only recordings whose ``started_at`` falls
    within the "last night" window — 10:00 PM local time of the previous
    calendar day through 12:00 PM (noon) local time of the current day —
    are included.

    If ``last_12h`` is True, only recordings whose ``started_at`` falls
    within the last 12 hours before the moment the batch is run are
    included.
    """
    # Fee is always fixed at 0.0001 SOL priority + 0.0 bribe = 0.0001 SOL/tx.
    priority_fee = 0.0001
    bribe_fee    = 0.0
    recordings = list_recordings()
    completed = [r for r in recordings if r.get("status") == "completed"]

    if last_night:
        now = datetime.datetime.now()
        today_noon = now.replace(hour=12, minute=0, second=0, microsecond=0)
        yesterday_10_50pm = (now - datetime.timedelta(days=1)).replace(hour=22, minute=50, second=0, microsecond=0)
        lo = yesterday_10_50pm.timestamp()
        hi = today_noon.timestamp()
        completed = [r for r in completed if lo <= (r.get("started_at") or 0) <= hi]
    elif last_12h:
        hi = datetime.datetime.now().timestamp()
        lo = (datetime.datetime.now() - datetime.timedelta(hours=12)).timestamp()
        completed = [r for r in completed if lo <= (r.get("started_at") or 0) <= hi]

    if recording_ids is not None:
        sel = set(recording_ids)
        completed = [r for r in completed if r.get("id") in sel]

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
        market_type=market_type,
        leverage=leverage,
        funding_rate_per_interval=funding_rate_per_interval,
        funding_interval_seconds=funding_interval_seconds,
        maintenance_margin_rate=maintenance_margin_rate,
        futures_taker_fee=futures_taker_fee,
        futures_slippage_pct=futures_slippage_pct,
        persist_results=False,
    )

    # Scheduling only (per-recording computation is identical either way):
    # sequential for single-recording runs or when the caller didn't ask
    # for parallelism; parallel pool whenever max_workers > 1 is requested.
    # (Previously batches of ≤20 were forced sequential regardless of
    # max_workers, which made 8-rec smoke sweeps crawl on one core.)
    if len(completed) == 1 or not max_workers or max_workers <= 1:
        results = []
        for rec in completed:
            try:
                results.append(run_backtest(recording_id=rec["id"], **common_kwargs))
            except Exception as e:
                results.append({"error": str(e), "recording_id": rec["id"]})
        return results

    # Large batches: use parallel processes
    tasks = [
        {
            "candle_count": max(int(rec.get("candle_count") or 0), 1000),
            "kwargs": {"recording_id": rec["id"], **common_kwargs},
        }
        for rec in completed
    ]
    workers = min(max_workers or (os.cpu_count() or 4), len(tasks))
    results = []

    pool = _get_pool(workers)
    # One balanced task per worker amortizes imports/JIT compilation and keeps
    # the slow long-recording tail close to the batch's average worker load.
    futures = [pool.submit(_run_backtest_worker_chunk, chunk)
               for chunk in _balanced_task_chunks(tasks, workers)]
    for future in as_completed(futures):
        results.extend(future.result())

    return results


def run_backtest(
    recording_id: int,
    engine_params: Optional[dict] = None,
    buy_size_sol: float = 0.1,
    priority_fee: float = 0.0001,
    bribe_fee: float = 0.0,
    slippage_pct: float = 1.0,
    starting_balance: float = 1.0,
    batch_id: Optional[str] = None,
    engine_version: int = 1,
    # ── Futures mode (option A, additive; "spot" default = zero behaviour change)
    market_type: str = "spot",
    leverage: float = 1.0,
    funding_rate_per_interval: float = 0.0001,
    funding_interval_seconds: int = 28800,
    maintenance_margin_rate: float = 0.005,
    futures_taker_fee: float = 0.00045,
    futures_slippage_pct: Optional[float] = None,
    persist_results: bool = True,
) -> dict:
    """
    Run a full backtest on a saved recording.

    Returns a summary dict with the backtest_id and stats.
    """
    # Fee is always fixed at 0.0001 SOL priority + 0.0 bribe = 0.0001 SOL/tx.
    priority_fee = 0.0001
    bribe_fee    = 0.0
    recording = get_recording(recording_id)
    if not recording:
        raise ValueError(f"Recording {recording_id} not found")

    candles = get_recording_candles(recording_id)
    if not candles:
        raise ValueError(f"Recording {recording_id} has no candles")

    # Load holder-flow events for this recording (dev/insider wallet trades)
    holder_flow_events = get_holder_flow(recording_id)

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
        holder_flow_events=holder_flow_events,
        market_type=market_type,
        leverage=leverage,
        funding_rate_per_interval=funding_rate_per_interval,
        funding_interval_seconds=funding_interval_seconds,
        maintenance_margin_rate=maintenance_margin_rate,
        futures_taker_fee=futures_taker_fee,
        futures_slippage_pct=futures_slippage_pct,
    )

    # Saving full candle series is useful for an interactive single backtest,
    # but it dominates SQLite I/O in iteration batches and is not consumed by
    # aggregate/paired analysis.  Batch workers keep only the execution stream.
    candle_results = [] if persist_results else None

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
        buy_vol  = candle.get("buy_volume", 0.0)
        sell_vol = candle.get("sell_volume", 0.0)
        pool_sol = candle.get("pool_sol", 0.0)
        mcap_usd = candle.get("market_cap_usd", 0.0)
        funding_rate = candle.get("funding_rate", 0.0) or 0.0
        mark_price = candle.get("mark_price", 0.0) or 0.0

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

        # State 4: close tick — buy/sell split lands here
        result = ft_update(time=t, o=o, h=h, l=l, c=c, volume=vol,
                           buy_volume=buy_vol, sell_volume=sell_vol,
                           pool_sol=pool_sol,
                           market_cap_usd=mcap_usd,
                           funding_rate=funding_rate,
                           mark_price=mark_price,
                           _build_full_result=False)
        fwd = result.get("forward_test")
        if fwd and trade_action_for_candle is None and fwd.get("trade_action"):
            trade_action_for_candle = fwd["trade_action"]
            trade_label_for_candle  = fwd.get("trade_label")

        if persist_results:
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

        last_candle = (t, o, h, l, c)

    # Force-close any position still open at end of recording so its PnL is
    # captured in stats (win_rate / total_pnl_sol).  Otherwise trades that
    # entered near the end of data are silently dropped, biasing results.
    if ft.current_trade is not None:
        t_last, o_last, h_last, l_last, c_last = last_candle
        ft._close_long(
            o_last, h_last, l_last, c_last, t_last,
            reason="recording_ended",
        )

    # Gather trade history
    trades = [t.to_dict() for t in ft.trade_history]
    stats = ft.stats.to_dict()

    # Save to backtest DB
    # Merge futures run config into the saved engine_params so paired_diff /
    # per-token logs stay self-describing about which pipeline produced them.
    saved_params = dict(engine_params)
    if market_type == "futures" and ft.futures_cfg_snapshot:
        saved_params.update(
            {"futures_" + k: v for k, v in ft.futures_cfg_snapshot.items()
             if k != "market_type"}
        )
        saved_params["market_type"] = "futures"

    bt_id = None
    if persist_results:
        bt_id = create_backtest(
            recording_id=recording_id,
            mint=recording["mint"],
            token_name=recording.get("token_name", ""),
            token_symbol=recording.get("token_symbol", ""),
            timeframe=timeframe,
            engine_params=saved_params,
            stats=stats,
            candle_results=candle_results,
            trades=trades,
            batch_id=batch_id,
            market_type=market_type,
        )

    # ── Write per-token JSON trade log (only when trades were placed) ────────
    if trades:
        _write_trade_log(
            recording_id=recording_id,
            token_name=recording.get("token_name", ""),
            token_symbol=recording.get("token_symbol", ""),
            engine_params=engine_params,
            stats=stats,
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
        "candle_count":  len(candles),
        "stats":         stats,
        "trade_count":   len(trades),
    }


# ── Historical futures backtest (option B) ─────────────────────────────────
# Reuses the exact same ForwardTester + engine + 4-state intra-candle loop as
# the recorded-spot pipeline, but sources candles from the exchange cache
# (futures_exchange) instead of a recording id.  Results persist through the
# same create_backtest() storage with market_type="futures".

def run_futures_backtest(
    symbol: str,
    engine_params: Optional[dict] = None,
    market_params: Optional[dict] = None,
    engine_version: int = 1,
    batch_id: Optional[str] = None,
    futures_days: int = 30,
    timeframe: str = "1h",
) -> dict:
    """
    Run the shared engine over cached historical perp data for *symbol*.

    market_params keys (all optional; defaults are CEX-realistic for majors):
      buy_size_sol      → margin per trade in USDC (default 100)
      starting_balance  → starting equity (default 1000)
      leverage          → notional = margin × leverage (default 5)
      timeframe         → "1h" (default) | "15m"
      futures_slippage_pct / funding_interval_seconds / funding_rate_per_interval /
        maintenance_margin_rate / futures_taker_fee
    """
    import futures_exchange as fe

    market_params = dict(market_params or {})
    leverage = float(market_params.get("leverage", 5.0))
    timeframe = str(market_params.get("timeframe", timeframe))
    slippage = float(market_params.get("futures_slippage_pct", 0.1))

    if engine_params is None:
        engine_params = {}

    bars = fe.get_futures_candles(symbol, timeframe=timeframe,
                                  days_back=int(market_params.get("futures_days", futures_days)))
    if not bars:
        raise ValueError(f"no cached futures data for symbol {symbol!r} ({timeframe})")

    # ── Futures second param-set (engine V2) ─────────────────────────────
    # Auto-derive recommended `v2_volume_scale_fut` per symbol from the
    # fetched cache so the engine's SOL-scale OU processes are fed sane
    # magnitudes (~1e-3..50/bar median).  Caller may override explicitly via
    # engine_params["v2_volume_scale_fut"] (kept verbatim); otherwise the
    # default is `v2_target_bar_volume_usd / median(turnover)` with a
    # defensive 1e3 bound.  This is a *deterministic* per-symbol function
    # and does not depend on the trade path — spot behaviour is untouched.
    # NB: must run AFTER `bars = fe.get_futures_candles(...)` so the cache
    # is in scope; otherwise `vscale` falls back to 1.0 (no scaling).
    if int(engine_version) == 2 and engine_params is not None:
        import strategy_engineV2
        if "v2_futures_overrides" not in engine_params:
            preset = dict(strategy_engineV2.FUTURES_DEFAULT_CONFIG)
            if "v2_volume_scale_fut" not in engine_params:
                try:
                    med = sorted(b["turnover"] for b in bars)[len(bars) // 2] or 1.0
                except Exception:
                    med = 1.0
                tgt = float(strategy_engineV2.FUTURES_DEFAULT_CONFIG.get(
                    "v2_target_bar_volume_usd", 1.0))
                vscale = tgt / max(med, 1.0)
                # Defensive bound: never scale past 1e3 (or below 1e-12).
                preset["v2_volume_scale_fut"] = min(max(float(vscale), 1e-12), 1e3)
            engine_params = dict(engine_params)
            engine_params["v2_futures_overrides"] = preset

    ft = ForwardTester(
        starting_balance=float(market_params.get("starting_balance", 1000.0)),
        buy_size_sol=float(market_params.get("buy_size_sol", 100.0)),
        priority_fee=0.0,
        bribe_fee=0.0,
        slippage_pct=slippage,
        engine_kwargs=engine_params,
        engine_version=engine_version,
        holder_flow_events=None,
        market_type="futures",
        leverage=leverage,
        funding_rate_per_interval=float(market_params.get("funding_rate_per_interval", 0.0001)),
        funding_interval_seconds=int(market_params.get("funding_interval_seconds", 28800)),
        maintenance_margin_rate=float(market_params.get("maintenance_margin_rate", 0.005)),
        futures_taker_fee=float(market_params.get("futures_taker_fee", 0.00045)),
        futures_slippage_pct=slippage,
        sol_price_usd=1.0,            # USD-priced bars → 1 unit == 1 USDC
    )

    candle_results = []
    ft_update = ft.update
    engine = ft.engine
    last_candle = None

    for bar in bars:
        t = int(bar["ts_s"])          # normalised field name in the cache
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        vol = bar.get("turnover", 0.0)
        funding_rate = bar.get("funding_rate", 0.0) or 0.0
        mark_price = bar.get("mark_price", 0.0) or 0.0
        open_interest = bar.get("open_interest", 0.0) or 0.0

        bullish = c >= o
        mid_first, mid_second = (h, l) if bullish else (l, h)
        buy_vol = float(bar.get("taker_buy_volume", 0.0))
        sell_vol = float(bar.get("taker_sell_volume", 0.0))
        # Spread real (turnover) volume across the 4 intra-candle states so
        # the KDE buffer fills and measurement variance R stays well-scaled.
        # Fall back to a linear 25/25/25/25 split if no taker-side split is
        # available.
        v_each = vol / 4.0
        vb_each = buy_vol / 4.0
        vs_each = sell_vol / 4.0

        trade_action_for_candle = None
        trade_label_for_candle = None

        result = ft_update(time=t, o=o, h=o, l=o, c=o, volume=v_each,
                           buy_volume=vb_each, sell_volume=vs_each,
                           mark_price=mark_price, funding_rate=funding_rate,
                           _build_full_result=False)
        fwd = result.get("forward_test") or {}
        if fwd.get("trade_action"):
            trade_action_for_candle = fwd["trade_action"]
            trade_label_for_candle = fwd.get("trade_label")

        h2, l2 = max(o, mid_first), min(o, mid_first)
        result = ft_update(time=t, o=o, h=h2, l=l2, c=mid_first, volume=v_each,
                           buy_volume=vb_each, sell_volume=vs_each,
                           mark_price=mark_price, funding_rate=funding_rate,
                           _build_full_result=False)
        fwd = result.get("forward_test") or {}
        if trade_action_for_candle is None and fwd.get("trade_action"):
            trade_action_for_candle = fwd["trade_action"]
            trade_label_for_candle = fwd.get("trade_label")

        result = ft_update(time=t, o=o, h=h, l=l, c=mid_second, volume=v_each,
                           buy_volume=vb_each, sell_volume=vs_each,
                           mark_price=mark_price, funding_rate=funding_rate,
                           _build_full_result=False)
        fwd = result.get("forward_test") or {}
        if trade_action_for_candle is None and fwd.get("trade_action"):
            trade_action_for_candle = fwd["trade_action"]
            trade_label_for_candle = fwd.get("trade_label")

        result = ft_update(time=t, o=o, h=h, l=l, c=c, volume=v_each,
                           buy_volume=vb_each, sell_volume=vs_each,
                           pool_sol=0.0, market_cap_usd=0.0,
                           funding_rate=funding_rate, mark_price=mark_price,
                           _build_full_result=False)
        fwd = result.get("forward_test") or {}
        if trade_action_for_candle is None and fwd.get("trade_action"):
            trade_action_for_candle = fwd["trade_action"]
            trade_label_for_candle = fwd.get("trade_label")

        candle_results.append({
            "time": t, "open": o, "high": h, "low": l, "close": c, "volume": vol,
            "regime": engine.regime.value,
            "direction": engine.direction.value,
            "signal": result.get("signal", "none") if result else "none",
            "signal_strength": engine.signal_strength,
            "ema_fast": engine.ema_fast_val,
            "ema_slow": engine.ema_slow_val,
            "atr": engine.atr_val,
            "roc": engine.m_hat,
            "confidence": engine.trend_confidence,
            "trade_action": trade_action_for_candle,
            "trade_label": trade_label_for_candle,
            "balance": round(ft.balance, 6),
            "unrealized_pnl": 0,
        })
        last_candle = (t, o, h, l, c, mark_price, funding_rate)

    # Force-close any position still open at end of data (parity with the
    # recorded pipeline — no right-tail bias), at the LAST mark price.
    if ft.current_trade is not None and last_candle:
        t_l, o_l, h_l, l_l, c_l, mk_l, fr_l = last_candle
        close_px = mk_l or c_l
        ft._close_long(close_px, close_px, close_px, close_px, t_l,
                       reason="recording_ended")

    trades = [t.to_dict() for t in ft.trade_history]
    stats = ft.stats.to_dict()

    saved_params = dict(engine_params)
    if ft.futures_cfg_snapshot:
        saved_params.update(
            {"futures_" + k: v for k, v in ft.futures_cfg_snapshot.items()
             if k != "market_type"}
        )
        saved_params["market_type"] = "futures"
        saved_params["futures_symbol"] = symbol
        saved_params["futures_days"] = futures_days

    ex_symbol = f"{symbol}USDT"
    bt_id = create_backtest(
        recording_id=0,
        mint=f"FUT:{ex_symbol}",
        token_name=symbol,
        token_symbol=f"FUT:{symbol}",
        timeframe=timeframe,
        engine_params=saved_params,
        stats=stats,
        candle_results=candle_results,
        trades=trades,
        batch_id=batch_id,
        market_type="futures",
    )

    if trades:
        _write_trade_log(
            recording_id=0,
            token_name=symbol,
            token_symbol=f"FUT:{symbol}",
            engine_params=saved_params,
            stats=stats,
            trades=trades,
            batch_id=batch_id,
        )

    return {
        "backtest_id": bt_id,
        "recording_id": 0,
        "mint": f"FUT:{ex_symbol}",
        "token_name": symbol,
        "token_symbol": f"FUT:{symbol}",
        "timeframe": timeframe,
        "candle_count": len(candle_results),
        "stats": stats,
        "trade_count": len(trades),
        "symbol": symbol,
        "exchange_symbol": ex_symbol,
        "account_ccy": "USDC",
    }


# ── JSON trade log writer ───────────────────────────────────────────────────

def _safe_filename(s: str) -> str:
    """Strip characters that are unsafe in filenames."""
    return re.sub(r'[^\w\-]', '_', s).strip('_') or "unknown"


def _write_trade_log(
    recording_id: int,
    token_name: str,
    token_symbol: str,
    engine_params: dict,
    stats: dict,
    trades: list[dict],
    batch_id: Optional[str] = None,
) -> str:
    """
    Write a JSON file for a single backtest run.

    File name:  <RESULTS_DIR>/<symbol_or_name>_rec<recording_id>.json
    Each trade entry contains:
      - entry_time, exit_time
      - entry_price, exit_price
      - entry_params   → full engine snapshot captured when the position opened
      - outcome        → "W" or "L"
      - pnl_pct        → percentage PnL for the trade
      - exit_reason
    Returns the path to the written file.
    """
    os.makedirs(_RESULTS_DIR, exist_ok=True)

    label = _safe_filename(token_symbol or token_name or str(recording_id))
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    batch_suffix = f"_{_safe_filename(batch_id)}" if batch_id else ""
    filename = f"{label}_rec{recording_id}{batch_suffix}_{timestamp}.json"
    filepath = os.path.join(_RESULTS_DIR, filename)

    trade_log = []
    for t in trades:
        trade_log.append({
            "entry_time":   t.get("entry_time"),
            "exit_time":    t.get("exit_time"),
            "entry_price":  t.get("entry_price"),
            "exit_price":   t.get("exit_price"),
            "outcome":      t.get("outcome", ""),      # "W" or "L"
            "pnl_pct":      round(t.get("pnl_pct", 0.0), 4),
            "pnl_sol":      round(t.get("pnl_sol", 0.0), 6),
            "exit_reason":  t.get("exit_reason", ""),
            "entry_reason": t.get("entry_reason", ""),
            "entry_params": t.get("entry_params", {}),
            "exit_params":  t.get("exit_params", {}),
        })

    payload = {
        "token_symbol":  token_symbol,
        "token_name":    token_name,
        "recording_id":  recording_id,
        "batch_id":      batch_id,
        "generated_at":  timestamp,
        "engine_params": engine_params,
        "summary": {
            "total_trades":    stats.get("total_trades", 0),
            "winning_trades":  stats.get("winning_trades", 0),
            "losing_trades":   stats.get("losing_trades", 0),
            "win_rate_pct":    round(stats.get("win_rate", 0.0), 2),
            "total_pnl_sol":   round(stats.get("total_pnl_sol", 0.0), 6),
            "max_drawdown_pct": round(stats.get("max_drawdown_pct", 0.0), 2),
        },
        "trades": trade_log,
    }

    with open(filepath, "w") as f:
        json.dump(payload, f, indent=2)

    return filepath
