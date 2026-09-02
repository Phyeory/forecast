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

  Execution models (ForwardTester exec_model):
    "instant" (default): a signal from state k fills at THAT state's close
      price ± slippage — the exact mirror of the live trader's signal-instant
      execution (2026-08-30).  entry/exit_latency_seconds>0 defer the fill to
      t_signal + latency, priced on the recorded intra-candle path.
    "legacy": the historical n+1 model — a pending BUY/EXIT queued during
      candle N executed at State 1 of candle N+1 with a fill_fraction
      mid-bar price.  Kept byte-identical so every pre-change baseline
      batch remains reproducible.

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

from process_watchdog import guard_parent

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
        _pool = ProcessPoolExecutor(max_workers=max_workers, initializer=guard_parent)
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
    guard_parent()
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
    # iter67: replay holder-flow events at the live delivery lag instead of
    # their exact on-chain timestamp (0.0 = legacy, byte-identical baselines).
    holder_flow_latency_seconds: float = 0.0,
    persist_results: bool = True,
    # 2026-08-30 execution model (see ForwardTester docstring):
    #   exec_model="instant" (default) fills at the signal-instant price —
    #   the exact mirror of the live trader.  "legacy" reproduces the
    #   pre-change n+1 mid-bar model byte-identically (historical baselines).
    #   entry/exit_latency_seconds>0 add a measured-latency overlay that
    #   prices the fill on the recorded path at t_signal + latency
    #   (live journals: buy signal→confirm median 10.0 s, sell 2.3 s).
    exec_model: str = "instant",
    entry_latency_seconds: float = 0.0,
    exit_latency_seconds: float = 0.0,
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
        holder_flow_latency_seconds=holder_flow_latency_seconds,
        persist_results=persist_results,
        persist_candles=False,
        exec_model=exec_model,
        entry_latency_seconds=entry_latency_seconds,
        exit_latency_seconds=exit_latency_seconds,
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
    if engine_version in (2, 3):
        try:
            from engine_factory import create_engine
            _w_eng = create_engine(engine_version=engine_version)
            _w_eng.update(time=0, o=1.0, h=1.0, l=1.0, c=1.0, volume=1.0, buy_volume=0.5, sell_volume=0.5)
        except Exception:
            pass

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
    # iter66: execution-level calibration — shifts simulated fill prices to
    # the live-executable level (default 0.0 = exact legacy behaviour).
    exec_offset_pct_buy: float = 0.0,
    exec_offset_pct_sell: float = 0.0,
    # iter67: holder-flow delivery latency.  The backtester indexes every
    # holder_flow row at its exact on-chain timestamp, but live can only act
    # once the GMGN smartmoney poll surfaces the row.  Shifting event times
    # forward by the measured live lag reproduces the live decision stream
    # (entry blocks that never fired, dev_sell exits that fired seconds late).
    # Default 0.0 = exact legacy behaviour, so every existing baseline is
    # byte-identical.
    holder_flow_latency_seconds: float = 0.0,
    persist_results: bool = True,
    persist_candles: bool = True,
    # 2026-08-30 execution model (see ForwardTester docstring): "instant"
    # fills at the signal-instant price (live mirror); "legacy" reproduces
    # the pre-change n+1 mid-bar model byte-identically.  Latency overlays
    # defer the fill to t_signal + latency on the recorded price path.
    exec_model: str = "instant",
    entry_latency_seconds: float = 0.0,
    exit_latency_seconds: float = 0.0,
) -> dict:
    """
    Run a full backtest on a saved recording.

    Returns a summary dict with the backtest_id and stats.

    ``persist_candles=False`` still saves the backtest row and trade list to
    the DB (so results show up on the UI) but skips the heavy per-candle
    series insert, which is what batch mode uses.
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
        holder_flow_latency_seconds=holder_flow_latency_seconds,
        exec_offset_pct_buy=exec_offset_pct_buy,
        exec_offset_pct_sell=exec_offset_pct_sell,
        exec_model=exec_model,
        entry_latency_seconds=entry_latency_seconds,
        exit_latency_seconds=exit_latency_seconds,
    )

    # iter78 ADOPTION: 5-second deferred-entry execution cell.  The knob
    # lives on the ENGINE (`v2_entry_delay_seconds`, default 5.0 since the
    # 2026-09-02 adoption), so bare {} runs the adopted model — the same
    # engine-keyed injection pattern the iter74d MSM adoption used.  An
    # EXPLICIT entry_latency_seconds argument (run_iteration
    # --entry-latency-seconds / the batch kwarg) overrides the engine knob;
    # `{"v2_entry_delay_seconds": 0.0}` restores the pre-iter78
    # signal-instant fill byte-exactly.  Only the instant/latency exec
    # models consume it (ForwardTester.__init__ merges it into
    # entry_latency_seconds); "legacy" is never altered.
    if entry_latency_seconds <= 0.0 and exec_model != "legacy":
        _eng_delay = float(getattr(ft.engine, "v2_entry_delay_seconds", 0.0))
        if _eng_delay > 0.0:
            ft.enable_entry_latency(_eng_delay)

    # iter74: MSM fleet-regime entry gate — inject the precomputed causal
    # fleet-state lookup whenever the ENGINE has the MSM gate enabled
    # (configuration B is the production DEFAULT since 2026-08-31, so this
    # fires on bare {} too; explicit v2_msm_enable=0.0 leaves the engine
    # gate off and no source is injected = byte-exact pre-iter74).
    # Missing artifacts → loud log + gate never blocks (safe degradation).
    # iter75: also inject the causal fleet-MEDIUM timeline (low-turbulence
    # flag per bin) when the s2 gate is enabled — same pattern, same parity.
    if engine_version == 2:
        try:
            if float(getattr(ft.engine, "_v2_msm_enable", 0.0)) > 0.0:
                from fleet_regime_online import FleetRegimeFilter
                # iter75sw: env override lets bin-size / ablation sweep cells
                # point at their own state artifacts; default = production.
                _states_path = os.environ.get("V2_MSM_STATES_PATH", "")
                if not _states_path:
                    _states_path = os.path.join(os.path.dirname(__file__), "fleet_filtered_states.json")
                if not os.path.exists(_states_path):
                    _states_path = os.path.join(os.path.dirname(__file__), "analysis", "fleet_filtered_states.json")
                ft.engine.set_fleet_regime_states(FleetRegimeFilter.from_panel(_states_path))
        except Exception as _msm_err:
            print(f"[iter74/75] fleet state injection FAILED for rec {recording_id}: "
                  f"{_msm_err} — gates will not block")

    # Saving full candle series is useful for an interactive single backtest,
    # but it dominates SQLite I/O in iteration batches.  Batch runs keep the
    # backtest row + trades (so results appear on the UI) while skipping the
    # candle series, which aggregate/paired analysis does not consume.
    candle_results = [] if persist_results and persist_candles else None

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
                           _build_full_result=False)
        fwd = result.get("forward_test")
        if fwd and trade_action_for_candle is None and fwd.get("trade_action"):
            trade_action_for_candle = fwd["trade_action"]
            trade_label_for_candle  = fwd.get("trade_label")

        if candle_results is not None:
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
    saved_params = dict(engine_params)

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
            candle_results=candle_results or [],
            trades=trades,
            batch_id=batch_id,
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
