import asyncio
import json
import logging
import time
import subprocess
import sys
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from candle_aggregator import CandleAggregator, TIMEFRAME_SECONDS
from pumpfun_client import DexScreenerPollClient, PumpFunWSClient, PumpSwapRPCClient, get_historical_candles, get_token_info, resolve_input, SUB_MINUTE_TFS, NewPairsStream
from forward_tester import ForwardTester
from live_trader import LiveTrader, keypair_from_private_key
import data_store
from backtester import run_backtest, run_backtest_batch, score_batch
from sniper.sniper_router import router as sniper_router, set_engine as set_sniper_engine
from sniper.sniper_engine import SniperEngine, SniperConfig
from axiom_scanner import AxiomScanner, ScannerFilters, ScanResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  [%(name)s]  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pump-chart")

# Limit how many tokens can run resolve_input / get_historical_candles
# concurrently.  Without this, N parallel tokens all hammer DexScreener /
# pump.fun v3 simultaneously, causing timeouts that look like connect failures.
_resolve_sem = asyncio.Semaphore(8)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="pump-chart")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount sniper router
app.include_router(sniper_router)

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.on_event("startup")
async def startup_sniper():
    """Initialize and start the SniperEngine on app startup."""
    import os
    config = SniperConfig(
        mode=os.environ.get("SNIPER_MODE", "forward_test"),
        fee_threshold_sol=float(os.environ.get("SNIPER_FEE_THRESHOLD_SOL", "0.1")),
        max_concurrent_positions=int(os.environ.get("SNIPER_MAX_POSITIONS", "3")),
        daily_loss_limit_sol=float(os.environ.get("SNIPER_DAILY_LOSS_LIMIT_SOL", "2.0")),
        min_forward_trades_for_live=int(os.environ.get("SNIPER_MIN_FORWARD_TRADES_FOR_LIVE", "200")),
    )
    engine = SniperEngine(config=config)
    app.state.sniper = engine
    set_sniper_engine(engine)
    # Do not auto-start; wait for /api/sniper/start
    logger.info(f"[Sniper] Initialized (mode={config.mode}, fee_threshold={config.fee_threshold_sol} SOL). Not started yet.")


@app.get("/")
async def index():
    p = FRONTEND_DIR / "index.html"
    if not p.exists():
        return JSONResponse({"status": "ok"})
    return FileResponse(
        str(p),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )

@app.get("/static/js/{filename}")
async def serve_js(filename: str):
    """Serve JS files with no-cache headers to prevent stale code being loaded."""
    p = FRONTEND_DIR / "js" / filename
    if not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(
        str(p),
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )


@app.get("/api/token/{mint}")
async def token_info_endpoint(mint: str):
    _, info = await resolve_input(mint)
    return JSONResponse(info) if info else JSONResponse({"error": "not found"}, status_code=404)


_prism_process = None
PRISM_DIRECTORY = Path(__file__).parent / "polymarket"
PRISM_CONFIG_PATH = PRISM_DIRECTORY / "prism_config.json"
PRISM_LOG_PATH = PRISM_DIRECTORY / "prism_decisions.jsonl"

@app.get("/api/polymarket/config")
async def get_pm_config():
    config = {
        "mode": "paper",
        "bankroll": 500,
        "prism_gate_z": 2.0,
        "max_spread": 0.04,
        "kelly_fraction": 0.25,
        "ou_window": 200,
        "http_proxy": "",
        "private_key": ""
    }
    if PRISM_CONFIG_PATH.exists():
        try:
            with open(PRISM_CONFIG_PATH, "r") as f:
                config.update(json.load(f))
        except: pass
        
    status = "running" if _prism_process and _prism_process.poll() is None else "stopped"
    return {"status": status, "config": config}

@app.post("/api/polymarket/config")
async def save_pm_config(body: dict = Body(...)):
    with open(PRISM_CONFIG_PATH, "w") as f:
        json.dump(body, f)
    return {"success": True}

@app.post("/api/polymarket/start")
async def start_pm():
    global _prism_process
    if _prism_process and _prism_process.poll() is None:
        return {"error": "Already running"}
        
    script_path = PRISM_DIRECTORY / "prism.py"
    _prism_process = subprocess.Popen([sys.executable, str(script_path)], cwd=str(PRISM_DIRECTORY))
    return {"success": True}

@app.post("/api/polymarket/stop")
async def stop_pm():
    global _prism_process
    if _prism_process and _prism_process.poll() is None:
        _prism_process.terminate()
        try:
            _prism_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _prism_process.kill()
    _prism_process = None
    return {"success": True}

@app.get("/api/polymarket/logs")
async def get_polymarket_logs():
    """Endpoint for the PRISM UI to pull the latest JSONL decisions."""
    if not PRISM_LOG_PATH.exists():
        return JSONResponse({"error": "No PRISM logs found yet. Start the bot first."})
    
    try:
        decisions = []
        with open(PRISM_LOG_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    decisions.append(json.loads(line))
                    
        trades = [d for d in decisions if d.get("placed") == True]
        
        # Sort trades newest first, return top 50
        trades.sort(key=lambda x: x["ts"], reverse=True)
        trades_response = trades[:50]
        
        total_exposure = sum(t.get("order_usdc", 0) for t in trades)
        
        return JSONResponse({
            "total_snapshots": len(decisions),
            "total_trades": len(trades),
            "total_exposure": total_exposure,
            "trades": trades_response
        })
    except Exception as e:
        logger.error(f"Failed parsing PRISM log: {e}")
        return JSONResponse({"error": "Failed to parse PRISM logs."})


# ── Active recorder state ────────────────────────────────────────────────────

_active_recorders: dict[int, dict] = {}

# ── Recording API ────────────────────────────────────────────────────────────

@app.post("/api/recorder/start")
async def recorder_start(body: dict = Body(...)):
    """Start recording price data for a coin."""
    global _active_recorders

    mint = body.get("mint", "").strip()
    timeframe = body.get("timeframe", "1s")
    if not mint:
        return JSONResponse({"error": "mint is required"}, status_code=400)
    if timeframe not in TIMEFRAME_SECONDS:
        return JSONResponse({"error": f"Unknown timeframe: {timeframe}"}, status_code=400)

    for rec in _active_recorders.values():
        if rec["mint"] == mint and rec["timeframe"] == timeframe:
            return JSONResponse({"error": "Already recording this coin on this timeframe", "recording_id": rec["recording_id"]}, status_code=409)

    # Resolve token info (semaphore: avoid piling up parallel external calls)
    try:
        async with _resolve_sem:
            real_mint, token_info = await asyncio.wait_for(
                resolve_input(mint), timeout=20.0
            )
    except asyncio.TimeoutError:
        logger.warning(f"[Recorder] resolve_input timed out for {mint[:8]} — using raw mint")
        real_mint, token_info = mint, None
    token_name = (token_info or {}).get("name", "")
    token_symbol = (token_info or {}).get("symbol", "")

    rec_id = data_store.create_recording(real_mint, timeframe, token_name, token_symbol)
    cancelled = asyncio.Event()

    async def _record():
        live_source = (token_info or {}).get("_live_source", "pumpportal")
        live_query = (token_info or {}).get("pair_address") or real_mint
        if live_source == "solana_rpc" and (token_info or {}).get("pair_address"):
            ws_client = PumpSwapRPCClient(live_query)
        elif live_source == "dexscreener":
            poll_seconds = 0.25 if timeframe in {"1s", "5s", "15s"} else 0.5
            ws_client = DexScreenerPollClient(live_query, poll_seconds=poll_seconds)
        else:
            ws_client = PumpFunWSClient(real_mint)

        aggregator = CandleAggregator(timeframe)
        last_candle_time = None

        # Seed with historical candles (also throttled)
        try:
            async with _resolve_sem:
                hist = await asyncio.wait_for(
                    get_historical_candles(real_mint, timeframe), timeout=15.0
                )
        except asyncio.TimeoutError:
            logger.warning(f"[Recorder] get_historical_candles timed out for {real_mint[:8]}")
            hist = []
        if hist:
            data_store.insert_candles_batch(rec_id, hist)
            last = hist[-1]
            aggregator.process_trade(last["close"], 0.0, float(last["time"]))
            last_candle_time = last["time"]
            logger.info(f"[Recorder] Seeded {len(hist)} historical candles")

        try:
            async for trade in ws_client.stream():
                if cancelled.is_set():
                    break
                is_synthetic = bool(trade.get("synthetic"))
                candle, is_new = aggregator.process_trade(
                    trade["price"], trade["sol_amount"], trade["timestamp"],
                    synthetic=is_synthetic,
                )
                # Persist every tick (real or synthetic) so the candle row in
                # the DB always reflects the current accumulated OHLCV state.
                # INSERT OR REPLACE updates the row in-place for the same
                # (recording_id, time) key — safe and correct whether the
                # source emits real trades (PumpPortal) or synthetic polls
                # (DexScreener, which produces only synthetic ticks).
                candle_dict = candle.to_dict()
                ct = candle_dict["time"]
                data_store.insert_candle(
                    rec_id, ct,
                    candle_dict["open"], candle_dict["high"],
                    candle_dict["low"], candle_dict["close"],
                    candle_dict.get("volume", 0),
                )
                last_candle_time = ct
        finally:
            ws_client.stop()
            data_store.stop_recording(rec_id)
            logger.info(f"[Recorder] Stopped recording {rec_id}")

    task = asyncio.create_task(_record())
    _active_recorders[rec_id] = {
        "recording_id": rec_id,
        "mint": real_mint,
        "token_name": token_name,
        "token_symbol": token_symbol,
        "timeframe": timeframe,
        "task": task,
        "cancelled": cancelled,
    }

    return JSONResponse({
        "status": "recording",
        "recording_id": rec_id,
        "mint": real_mint,
        "token_name": token_name,
        "token_symbol": token_symbol,
        "timeframe": timeframe,
    })


@app.post("/api/recorder/stop")
async def recorder_stop(body: dict = Body(default={})):
    global _active_recorders
    rec_id = body.get("recording_id")
    if rec_id is None:
        return JSONResponse({"error": "recording_id required to stop a specific recording"}, status_code=400)
    
    rec = _active_recorders.get(rec_id)
    if rec is None:
        return JSONResponse({"error": "No active recording with that id"}, status_code=404)

    rec["cancelled"].set()
    del _active_recorders[rec_id]
    data_store.update_recording_candle_count(rec_id)
    return JSONResponse({"status": "stopped", "recording_id": rec_id})


@app.get("/api/recorder/status")
async def recorder_status():
    global _active_recorders
    if not _active_recorders:
        return {"active": False, "count": 0}

    reqs = []
    for r_id, rec in _active_recorders.items():
        db_rec = data_store.get_recording(r_id)
        reqs.append({
            "recording_id": r_id,
            "mint": rec["mint"],
            "token_name": rec.get("token_name", ""),
            "token_symbol": rec.get("token_symbol", ""),
            "timeframe": rec["timeframe"],
            "candle_count": db_rec["candle_count"] if db_rec else 0
        })

    return {
        "active": True,
        "count": len(_active_recorders),
        "recordings": reqs
    }


# ── Recordings API ───────────────────────────────────────────────────────────

@app.get("/api/recordings")
async def list_recordings_endpoint():
    return JSONResponse(data_store.list_recordings())


@app.get("/api/recordings/{recording_id}")
async def get_recording_endpoint(recording_id: int):
    rec = data_store.get_recording(recording_id)
    if not rec:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(rec)


@app.get("/api/recordings/{recording_id}/candles")
async def get_recording_candles_endpoint(recording_id: int):
    candles = data_store.get_recording_candles(recording_id)
    return JSONResponse(candles)


@app.delete("/api/recordings/{recording_id}")
async def delete_recording_endpoint(recording_id: int):
    data_store.delete_recording(recording_id)
    return JSONResponse({"status": "deleted"})


# ── Backtest API ─────────────────────────────────────────────────────────────

@app.post("/api/backtest")
async def run_backtest_endpoint(body: dict = Body(...)):
    recording_id = body.get("recording_id")
    if recording_id is None:
        return JSONResponse({"error": "recording_id is required"}, status_code=400)
    engine_params = body.get("engine_params", {})
    engine_version = int(body.get("engine_version", 1))
    try:
        # Run CPU-bound backtest in thread pool to avoid blocking the event loop
        result = await asyncio.to_thread(
            run_backtest,
            recording_id=int(recording_id),
            engine_params=engine_params,
            buy_size_sol=body.get("buy_size_sol", 0.1),
            priority_fee=body.get("priority_fee", 0.0001),
            bribe_fee=body.get("bribe_fee", 0.00001),
            slippage_pct=body.get("slippage_pct", 1.0),
            starting_balance=body.get("starting_balance", 1.0),
            engine_version=engine_version,
        )
        return JSONResponse(result)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.error(f"Backtest error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/backtest/batch")
async def run_backtest_batch_endpoint(body: dict = Body(default={})):
    """Run backtests on ALL completed recordings in parallel."""
    engine_params = body.get("engine_params", {})
    engine_version = int(body.get("engine_version", 1))
    batch_id = str(int(time.time() * 1000))
    try:
        results = await asyncio.to_thread(
            run_backtest_batch,
            engine_params=engine_params,
            buy_size_sol=body.get("buy_size_sol", 0.1),
            priority_fee=body.get("priority_fee", 0.0001),
            bribe_fee=body.get("bribe_fee", 0.00001),
            slippage_pct=body.get("slippage_pct", 1.0),
            starting_balance=body.get("starting_balance", 1.0),
            batch_id=batch_id,
            engine_version=engine_version,
        )
        succeeded = [r for r in results if "error" not in r]
        failed = [r for r in results if "error" in r]
        return JSONResponse({
            "total": len(results),
            "succeeded": len(succeeded),
            "failed": len(failed),
            "results": results,
        })
    except Exception as e:
        logger.error(f"Batch backtest error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/backtests")
async def list_backtests_endpoint():
    return JSONResponse(data_store.list_backtests())


@app.get("/api/backtests/{backtest_id}")
async def get_backtest_endpoint(backtest_id: int):
    bt = data_store.get_backtest(backtest_id)
    if not bt:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(bt)


@app.delete("/api/backtests/{backtest_id}")
async def delete_backtest_endpoint(backtest_id: int):
    data_store.delete_backtest(backtest_id)
    return JSONResponse({"status": "deleted"})


@app.delete("/api/backtests")
async def delete_all_backtests_endpoint():
    data_store.delete_all_backtests()
    return JSONResponse({"status": "deleted_all"})


@app.delete("/api/backtests/batch/{batch_id}")
async def delete_backtest_batch_endpoint(batch_id: str):
    data_store.delete_batch(batch_id)
    return JSONResponse({"status": "deleted"})


# ── Confidence Auto-Tuner ────────────────────────────────────────────────────

@app.post("/api/backtest/autotune")
async def autotune_confidence(body: dict = Body(...)):
    """
    Coordinate-descent hill-climb on confidence_high, confidence_low,
    and confidence_very_high — one at a time, in that order.

    For each parameter the algorithm:
      1. Tries +0.01 and -0.01 from the current best value.
      2. Moves in whichever direction improved the aggregate win-rate.
      3. Keeps stepping until neither direction improves (convergence).
      4. Moves to the next parameter.

    Progress is streamed as newline-delimited JSON so the UI can update
    live.  Results are NOT saved to the backtest DB.
    """
    import json as _json

    engine_params  = dict(body.get("engine_params", {}))
    engine_version = int(body.get("engine_version", 1))
    tester_cfg = dict(
        buy_size_sol     = float(body.get("buy_size_sol",     0.1)),
        priority_fee     = float(body.get("priority_fee",     0.0001)),
        bribe_fee        = float(body.get("bribe_fee",        0.00001)),
        slippage_pct     = float(body.get("slippage_pct",     1.0)),
        starting_balance = float(body.get("starting_balance", 1.0)),
        engine_version   = engine_version,
    )

    STEP         = 0.01
    MIN_TRADES   = 5          # ignore configs that produce too few trades
    MAX_ITERS    = 60         # safety cap per parameter
    PARAMS_ORDER = ["confidence_high", "confidence_low", "entry_confidence_high", "entry_confidence_low", "confidence_very_high"]

    async def _score(params: dict) -> tuple[float, int]:
        wr, trades, _ = await asyncio.to_thread(
            score_batch, engine_params=params, **tester_cfg
        )
        return wr, trades

    async def stream():
        best_params = dict(engine_params)

        def emit(obj: dict) -> str:
            return _json.dumps(obj) + "\n"

        # Baseline score
        base_wr, base_trades = await _score(best_params)
        yield emit({
            "type":        "baseline",
            "win_rate":    round(base_wr, 3),
            "total_trades": base_trades,
            "params":      {p: round(best_params.get(p, 0.5), 4) for p in PARAMS_ORDER},
        })

        overall_best_wr = base_wr

        for param in PARAMS_ORDER:
            current_val = best_params.get(param, 0.5)
            best_val    = current_val
            best_wr     = overall_best_wr
            direction   = None   # +1 = up, -1 = down, None = undecided

            yield emit({
                "type":      "param_start",
                "param":     param,
                "value":     round(best_val, 4),
                "win_rate":  round(best_wr, 3),
            })

            for iteration in range(MAX_ITERS):
                # Decide which candidates to try this iteration
                if direction is None:
                    candidates = [
                        (round(best_val + STEP, 4), +1),
                        (round(best_val - STEP, 4), -1),
                    ]
                else:
                    candidates = [(round(best_val + direction * STEP, 4), direction)]

                improved_this_iter = False
                for candidate_val, cdir in candidates:
                    # Clamp to [0.01, 0.99] and skip if same as current
                    candidate_val = round(max(0.01, min(0.99, candidate_val)), 4)
                    if abs(candidate_val - best_val) < 1e-6:
                        continue

                    test_params = dict(best_params)
                    test_params[param] = candidate_val

                    wr, trades = await _score(test_params)

                    is_better = trades >= MIN_TRADES and wr > best_wr
                    
                    logger.info(f"[Auto-Tune] param={param} value={candidate_val:.4f} wr={wr:.3f} trades={trades} (best_wr={best_wr:.3f}) improved={is_better}")
                    
                    yield emit({
                        "type":          "trial",
                        "param":         param,
                        "value":         candidate_val,
                        "win_rate":      round(wr, 3),
                        "total_trades":  trades,
                        "best_win_rate": round(best_wr, 3),
                        "improved":      is_better,
                    })

                    if is_better:
                        best_wr  = wr
                        best_val = candidate_val
                        direction = cdir
                        improved_this_iter = True
                        best_params[param] = best_val
                        yield emit({
                            "type":         "improvement",
                            "param":        param,
                            "value":        round(best_val, 4),
                            "win_rate":     round(best_wr, 3),
                            "total_trades": trades,
                        })
                        break   # Step one more iteration in this direction

                if not improved_this_iter:
                    # Neither direction helped → converged for this param
                    break

            overall_best_wr = best_wr
            yield emit({
                "type":        "param_done",
                "param":       param,
                "final_value": round(best_val, 4),
                "win_rate":    round(best_wr, 3),
            })

        yield emit({
            "type":         "done",
            "final_params": {p: round(best_params.get(p, 0.5), 4) for p in PARAMS_ORDER},
            "win_rate":     round(overall_best_wr, 3),
        })

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.websocket("/ws/{mint}")
async def chart_ws(
    websocket: WebSocket,
    mint: str,
    timeframe: str = Query(default="1s"),
    params: str = Query(default="{}"),
    buy_size: float = Query(default=0.1),
    slippage_pct: float = Query(default=1.0),
    priority_fee: float = Query(default=0.0001),
    bribe_fee: float = Query(default=0.00001),
    engine_version: int = Query(default=1),
):
    # Guard: reject reserved path segments that have dedicated routes.
    # FastAPI matches /ws/{mint} before /ws/sniper because the wildcard is
    # declared first. Must accept() before close() to avoid a 403.
    _RESERVED = {"sniper", "live"}
    if mint in _RESERVED:
        await websocket.accept()
        await websocket.close(code=4004, reason=f"Use /ws/{mint} dedicated endpoint")
        return

    if timeframe not in TIMEFRAME_SECONDS:
        await websocket.close(code=4000, reason="Unknown timeframe")
        return

    await websocket.accept()
    logger.info(f"Connect  input={mint[:8]}…  tf={timeframe}")

    # Resolve the user input (token mint or pair address) to the actual token mint
    # Use the global semaphore so N parallel dashboard connects don't all hit the
    # external APIs simultaneously (which causes connection timeouts).
    try:
        async with _resolve_sem:
            real_mint, token_info = await asyncio.wait_for(
                resolve_input(mint), timeout=20.0
            )
    except asyncio.TimeoutError:
        logger.warning(f"resolve_input timed out for {mint[:8]} — using raw mint")
        real_mint, token_info = mint, None
    if real_mint != mint:
        logger.info(f"Resolved {mint[:8]} -> {real_mint[:8]}")

    live_source = (token_info or {}).get("_live_source", "pumpportal")
    live_query = (token_info or {}).get("pair_address") or real_mint
    if live_source == "solana_rpc" and (token_info or {}).get("pair_address"):
        logger.info(f"Live source Solana RPC  pair={live_query[:8]}…")
        ws_client = PumpSwapRPCClient(live_query)
    elif live_source == "dexscreener":
        poll_seconds = 0.25 if timeframe in {"1s", "5s", "15s"} else 0.5
        logger.info(f"Live source DexScreener  query={live_query[:8]}…")
        ws_client = DexScreenerPollClient(live_query, poll_seconds=poll_seconds)
    else:
        logger.info(f"Live source PumpPortal  mint={real_mint[:8]}…")
        ws_client = PumpFunWSClient(real_mint)
    aggregator = CandleAggregator(timeframe)
    cancelled  = asyncio.Event()
    last_sent_price = None  # Track to skip unchanged price updates

    try:
        engine_params = json.loads(params)
    except Exception as e:
        logger.error(f"Failed to parse engine params: {e}")
        engine_params = {}

    # Initialize forward tester
    forward_tester = ForwardTester(
        starting_balance=1.0,
        buy_size_sol=buy_size,
        priority_fee=priority_fee,
        bribe_fee=bribe_fee,
        slippage_pct=slippage_pct,
        engine_kwargs=engine_params,
        engine_version=engine_version,
    )

    async def send(obj: dict) -> bool:
        try:
            await websocket.send_json(obj)
            return True
        except Exception:
            return False

    # Send metadata immediately (already fetched during resolution)
    if token_info:
        await send({"type": "token_info", "data": token_info})
    else:
        async def push_metadata():
            info = await get_token_info(real_mint)
            if info:
                await send({"type": "token_info", "data": info})
        asyncio.create_task(push_metadata())

    try:
        async with _resolve_sem:
            hist = await asyncio.wait_for(
                get_historical_candles(real_mint, timeframe), timeout=15.0
            )
    except asyncio.TimeoutError:
        logger.warning(f"get_historical_candles timed out for {real_mint[:8]} — skipping history")
        hist = []
    if hist:
        # Run historical candles through forward tester first
        strategy_results = []
        for candle in hist:
            result = forward_tester.update(
                time=int(candle["time"]),
                o=candle["open"],
                h=candle["high"],
                l=candle["low"],
                c=candle["close"],
                volume=candle.get("volume", 0),
            )
            strategy_results.append(result)

        await send({
            "type": "historical",
            "candles": hist,
            "strategy": strategy_results,
        })
        last = hist[-1]
        aggregator.process_trade(last["close"], 0.0, float(last["time"]))
        logger.info(f"Sent {len(hist)} candles + strategy data")
    else:
        await send(
            {"type": "error", "message": "No historical data found. Waiting for live trades…"}
        )

    async def _process_stream(client) -> bool:
        """Drain a ws_client stream. Returns True if at least one trade was processed."""
        nonlocal last_sent_price
        got_trade = False
        async for trade in client.stream():
            if cancelled.is_set():
                break

            got_trade = True
            is_synthetic = bool(trade.get("synthetic"))

            # Pass synthetic flag so aggregator skips ghost candles when price
            # is flat, but still opens new candle buckets when price moves.
            candle, is_new = aggregator.process_trade(
                trade["price"], trade["sol_amount"], trade["timestamp"],
                synthetic=is_synthetic,
            )

            # Skip if price hasn't changed (dedup rapid-fire identical ticks)
            candle_dict = candle.to_dict()
            current_price = candle_dict["close"]
            if last_sent_price is not None and current_price == last_sent_price and not is_new:
                continue
            last_sent_price = current_price

            # Run strategy engine on new/updated candle
            strategy_result = forward_tester.update(
                time=candle_dict["time"],
                o=candle_dict["open"],
                h=candle_dict["high"],
                l=candle_dict["low"],
                c=candle_dict["close"],
                volume=candle_dict.get("volume", 0),
            )

            # Only show real trades in the trade feed sidebar (not synthetic price polls)
            trade_payload = None if is_synthetic else {
                "price":      trade["price"],
                "sol_amount": trade["sol_amount"],
                "tx_type":    trade["tx_type"],
                "trader":     trade["trader"],
                "tx_hash":    trade["tx_hash"],
            }

            ok = await send(
                {
                    "type":           "candle",
                    "candle":         candle_dict,
                    "is_new":         is_new,
                    "market_cap_sol": trade.get("market_cap_sol", 0),
                    "market_cap_usd": trade.get("market_cap_usd", 0),
                    "trade":          trade_payload,
                    "strategy":       strategy_result,
                }
            )
            if not ok:
                break
        return got_trade

    async def stream_live():
        got = await _process_stream(ws_client)

        # If the primary client stopped without producing any trades, it likely
        # means the token is non-migrated (no PumpSwap pool). Fall back to
        # PumpPortal WebSocket which handles bonding-curve tokens.
        if not got and not cancelled.is_set() and not isinstance(ws_client, PumpFunWSClient):
            logger.warning(
                f"Primary client yielded no trades — falling back to PumpPortal for {real_mint[:8]}…"
            )
            fallback = PumpFunWSClient(real_mint)
            try:
                await _process_stream(fallback)
            finally:
                fallback.stop()

    async def keepalive():
        while not cancelled.is_set():
            await asyncio.sleep(15)
            if not await send({"type": "ping"}):
                break

    async def listen():
        try:
            while not cancelled.is_set():
                data = await websocket.receive_text()
                try:
                    json.loads(data)  # pong / future cmds
                except Exception:
                    pass
        except WebSocketDisconnect:
            pass
        finally:
            cancelled.set()
            ws_client.stop()

    async def shutdown():
        await cancelled.wait()
        ws_client.stop()
        try:
            await websocket.close(code=1000, reason="Session stopped")
        except Exception:
            pass

    try:
        await asyncio.gather(stream_live(), keepalive(), listen(), shutdown())
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        cancelled.set()
        ws_client.stop()
        logger.info(f"Disconnect  mint={real_mint[:8]}…")


# ── Live trading state ───────────────────────────────────────────────────
_active_live_traders: dict[str, dict] = {}   # keyed by token mint


@app.get("/api/live/status")
async def live_status():
    traders = []
    for mint, info in _active_live_traders.items():
        trader: LiveTrader = info["trader"]
        traders.append({
            "mint": mint,
            "token_name": info.get("token_name", ""),
            "token_symbol": info.get("token_symbol", ""),
            "timeframe": info.get("timeframe", "1m"),
            "status": "running" if not info.get("cancelled", asyncio.Event()).is_set() else "stopped",
            "stats": trader.stats.to_dict(),
            "current_trade": trader.current_trade.to_dict() if trader.current_trade else None,
            "trade_count": len(trader.trade_history),
        })
    return JSONResponse({"traders": traders, "count": len(traders)})


@app.post("/api/live/stop")
async def live_stop(body: dict = Body(...)):
    mint = body.get("mint", "").strip()
    if not mint or mint not in _active_live_traders:
        return JSONResponse({"error": "No active trader for this mint"}, status_code=404)
    info = _active_live_traders[mint]
    info["cancelled"].set()
    del _active_live_traders[mint]
    return JSONResponse({"status": "stopped", "mint": mint})


@app.post("/api/live/stop_all")
async def live_stop_all():
    for mint, info in list(_active_live_traders.items()):
        info["cancelled"].set()
    _active_live_traders.clear()
    return JSONResponse({"status": "all_stopped"})


# ── Axiom Scanner — auto-discover & subscribe trending coins ─────────────
_axiom_scanner: Optional[AxiomScanner] = None
_scanner_private_key: str = ""          # stored in memory only
_scanner_engine_params: dict = {}       # strategy engine params for scanner sessions
_scanner_buy_size: float = 0.01         # SOL per trade for scanner sessions
_scanner_timeframe: str = "1s"          # candle timeframe for scanner sessions
_scanner_slippage_bps: int = 1500
_scanner_priority_fee: int = 100_000


async def _scanner_auto_subscribe(result: ScanResult):
    """
    Callback fired when AxiomScanner approves a token.
    Spins up a headless live-trader session (no WebSocket frontend required).
    """
    global _scanner_private_key, _scanner_engine_params
    mint = result.mint

    if mint in _active_live_traders:
        logger.info(f"[Scanner] {result.symbol} ({mint[:8]}…) already has active trader — skipping")
        return

    if not _scanner_private_key:
        logger.warning(f"[Scanner] Cannot subscribe {result.symbol} — no private key configured")
        return

    try:
        keypair = keypair_from_private_key(_scanner_private_key)
    except ValueError as e:
        logger.error(f"[Scanner] Invalid private key: {e}")
        return

    logger.info(
        f"[Scanner] Auto-subscribing {result.symbol} ({mint[:8]}…) "
        f"| mcap=${result.market_cap_usd:,.0f} | txns_1h={result.txns_1h}"
    )

    # Resolve token for live data source
    try:
        async with _resolve_sem:
            real_mint, token_info = await asyncio.wait_for(
                resolve_input(mint), timeout=20.0
            )
    except asyncio.TimeoutError:
        logger.warning(f"[Scanner] resolve_input timed out for {mint[:8]}")
        real_mint, token_info = mint, None

    token_name = (token_info or {}).get("name", result.name)
    token_symbol = (token_info or {}).get("symbol", result.symbol)

    live_trader = LiveTrader(
        token_mint=real_mint,
        keypair=keypair,
        buy_size_sol=_scanner_buy_size,
        slippage_bps=_scanner_slippage_bps,
        priority_fee_lamports=_scanner_priority_fee,
        engine_kwargs=_scanner_engine_params.copy(),
        skip_simulation=True,
    )

    cancelled = asyncio.Event()
    _active_live_traders[real_mint] = {
        "trader": live_trader,
        "token_name": token_name,
        "token_symbol": token_symbol,
        "timeframe": _scanner_timeframe,
        "cancelled": cancelled,
        "wallet": str(keypair.pubkey()),
        "source": "axiom_scanner",    # tag so UI knows this was auto-discovered
    }

    # Resolve live data source
    live_source = (token_info or {}).get("_live_source", "pumpportal")
    live_query = (token_info or {}).get("pair_address") or real_mint
    if live_source == "solana_rpc" and (token_info or {}).get("pair_address"):
        ws_client = PumpSwapRPCClient(live_query)
    elif live_source == "dexscreener":
        ws_client = DexScreenerPollClient(live_query, poll_seconds=0.5)
    else:
        ws_client = PumpFunWSClient(real_mint)

    aggregator = CandleAggregator(_scanner_timeframe)

    # Auto-record candles
    rec_id = data_store.create_recording(
        real_mint, _scanner_timeframe, token_name, token_symbol
    )
    logger.info(f"[Scanner] Auto-recording candles → recording {rec_id}")

    # Fetch historical candles to warm up indicators
    try:
        async with _resolve_sem:
            hist = await asyncio.wait_for(
                get_historical_candles(real_mint, _scanner_timeframe), timeout=15.0
            )
    except asyncio.TimeoutError:
        hist = []
    if hist:
        data_store.insert_candles_batch(rec_id, hist)
        for candle in hist:
            live_trader.update_historical_candle(
                time_val=int(candle["time"]),
                o=candle["open"], h=candle["high"],
                l=candle["low"], c=candle["close"],
                volume=candle.get("volume", 0),
            )
        last = hist[-1]
        aggregator.process_trade(last["close"], 0.0, float(last["time"]))
        logger.info(f"[Scanner] Warmed up {len(hist)} historical candles for {real_mint[:8]}")

    async def _headless_stream():
        """Run the live trader without a WebSocket frontend."""
        last_sent_price = None
        got_trade = False
        async for trade in ws_client.stream():
            if cancelled.is_set():
                break
            got_trade = True
            is_synthetic = bool(trade.get("synthetic"))
            candle, is_new = aggregator.process_trade(
                trade["price"], trade["sol_amount"], trade["timestamp"],
                synthetic=is_synthetic,
            )
            candle_dict = candle.to_dict()
            current_price = candle_dict["close"]

            # Persist candle
            ct = candle_dict["time"]
            data_store.insert_candle(
                rec_id, ct,
                candle_dict["open"], candle_dict["high"],
                candle_dict["low"], candle_dict["close"],
                candle_dict.get("volume", 0),
            )

            if last_sent_price is not None and current_price == last_sent_price and not is_new:
                continue
            last_sent_price = current_price

            live_trader.update(
                time_val=candle_dict["time"],
                o=candle_dict["open"], h=candle_dict["high"],
                l=candle_dict["low"], c=candle_dict["close"],
                volume=candle_dict.get("volume", 0),
                is_new=is_new,
            )

            # Market-cap safety floor check
            mcap_usd = trade.get("market_cap_usd", 0)
            if mcap_usd and await live_trader.update_market_cap(float(mcap_usd)):
                logger.warning(
                    f"[Scanner] Market cap floor triggered for {real_mint[:8]}… — "
                    f"stopping session in 5s"
                )
                await asyncio.sleep(5)
                cancelled.set()
                break

        # Fallback to PumpPortal if primary client yielded nothing
        if not got_trade and not cancelled.is_set() and not isinstance(ws_client, PumpFunWSClient):
            logger.warning(f"[Scanner] Primary client yielded no trades for {real_mint[:8]}… — fallback")
            fallback = PumpFunWSClient(real_mint)
            try:
                async for trade in fallback.stream():
                    if cancelled.is_set():
                        break
                    is_synthetic = bool(trade.get("synthetic"))
                    candle, is_new = aggregator.process_trade(
                        trade["price"], trade["sol_amount"], trade["timestamp"],
                        synthetic=is_synthetic,
                    )
                    candle_dict = candle.to_dict()
                    current_price = candle_dict["close"]
                    ct = candle_dict["time"]
                    data_store.insert_candle(
                        rec_id, ct,
                        candle_dict["open"], candle_dict["high"],
                        candle_dict["low"], candle_dict["close"],
                        candle_dict.get("volume", 0),
                    )
                    if last_sent_price is not None and current_price == last_sent_price and not is_new:
                        continue
                    last_sent_price = current_price
                    live_trader.update(
                        time_val=candle_dict["time"],
                        o=candle_dict["open"], h=candle_dict["high"],
                        l=candle_dict["low"], c=candle_dict["close"],
                        volume=candle_dict.get("volume", 0),
                        is_new=is_new,
                    )
            finally:
                fallback.stop()

    async def _no_motion_check():
        """Periodic no-motion check for headless sessions."""
        while not cancelled.is_set():
            await asyncio.sleep(15)
            if await live_trader.check_no_motion():
                logger.warning(f"[Scanner] No motion stop for {real_mint[:8]}… — stopping in 5s")
                await asyncio.sleep(5)
                cancelled.set()
                break

    async def _run_headless():
        """Complete headless session lifecycle."""
        try:
            await asyncio.gather(_headless_stream(), _no_motion_check())
        except asyncio.CancelledError:
            pass
        finally:
            cancelled.set()
            ws_client.stop()
            data_store.stop_recording(rec_id)
            if real_mint in _active_live_traders:
                del _active_live_traders[real_mint]
            if _axiom_scanner:
                _axiom_scanner.unsubscribe_mint(mint)
            await live_trader.close()
            logger.info(f"[Scanner] Session ended for {token_symbol} ({real_mint[:8]}…)")

    # Fire and forget the headless session
    asyncio.create_task(_run_headless())


@app.post("/api/scanner/start")
async def scanner_start(body: dict = Body(...)):
    """Start the Axiom auto-scanner."""
    global _axiom_scanner, _scanner_private_key, _scanner_engine_params
    global _scanner_buy_size, _scanner_timeframe, _scanner_slippage_bps, _scanner_priority_fee

    private_key = body.get("private_key", "").strip()
    if not private_key:
        return JSONResponse({"error": "private_key is required"}, status_code=400)

    # Validate key
    try:
        kp = keypair_from_private_key(private_key)
        wallet = str(kp.pubkey())
    except ValueError as e:
        return JSONResponse({"error": f"Invalid private key: {e}"}, status_code=400)

    _scanner_private_key = private_key
    _scanner_engine_params = body.get("engine_params", {})
    _scanner_buy_size = float(body.get("buy_size", 0.01))
    _scanner_timeframe = body.get("timeframe", "1s")
    _scanner_slippage_bps = int(body.get("slippage_bps", 1500))
    _scanner_priority_fee = int(body.get("priority_fee", 100_000))

    # Build filter config
    filter_overrides = body.get("filters", {})
    filters = ScannerFilters()
    if filter_overrides:
        for key, val in filter_overrides.items():
            if hasattr(filters, key):
                expected_type = type(getattr(filters, key))
                try:
                    setattr(filters, key, expected_type(val))
                except (ValueError, TypeError):
                    pass

    if _axiom_scanner and _axiom_scanner._running:
        await _axiom_scanner.stop()

    _axiom_scanner = AxiomScanner(filters=filters)
    _axiom_scanner.on_token_approved = _scanner_auto_subscribe
    await _axiom_scanner.start()

    return JSONResponse({
        "status": "started",
        "wallet": wallet,
        "buy_size": _scanner_buy_size,
        "timeframe": _scanner_timeframe,
        "filters": filters.to_dict(),
    })


@app.post("/api/scanner/stop")
async def scanner_stop():
    """Stop the Axiom auto-scanner."""
    global _axiom_scanner
    if _axiom_scanner:
        await _axiom_scanner.stop()
        _axiom_scanner = None
    return JSONResponse({"status": "stopped"})


@app.get("/api/scanner/status")
async def scanner_status():
    """Get current scanner status and recent results."""
    if not _axiom_scanner:
        return JSONResponse({"running": False, "scanner": None})
    return JSONResponse(_axiom_scanner.get_status())


@app.post("/api/scanner/filters")
async def scanner_update_filters(body: dict = Body(...)):
    """Update scanner filters on the fly."""
    if not _axiom_scanner:
        return JSONResponse({"error": "Scanner not running"}, status_code=400)
    _axiom_scanner.update_filters(body)
    return JSONResponse({"status": "updated", "filters": _axiom_scanner.filters.to_dict()})


@app.post("/api/scanner/clear_cooldowns")
async def scanner_clear_cooldowns():
    """Clear token cooldowns to force re-evaluation."""
    if not _axiom_scanner:
        return JSONResponse({"error": "Scanner not running"}, status_code=400)
    _axiom_scanner.clear_cooldowns()
    return JSONResponse({"status": "cooldowns_cleared"})


@app.websocket("/ws/live/{mint}")
async def live_trading_ws(
    websocket: WebSocket,
    mint: str,
    timeframe: str = Query(default="1s"),
    private_key: str = Query(default=""),
    buy_size: float = Query(default=0.1),
    slippage_bps: int = Query(default=1000),
    priority_fee: int = Query(default=100000),
    skip_sim: bool = Query(default=True),
    params: str = Query(default="{}"),
    engine_version: int = Query(default=1),
):
    if timeframe not in TIMEFRAME_SECONDS:
        await websocket.close(code=4000, reason="Unknown timeframe")
        return
    if not private_key:
        await websocket.close(code=4001, reason="Private key required")
        return

    try:
        keypair = keypair_from_private_key(private_key)
    except ValueError as e:
        await websocket.close(code=4002, reason=f"Invalid private key: {e}")
        return

    wallet_pubkey = str(keypair.pubkey())

    await websocket.accept()
    logger.info(f"[LIVE] Connect  mint={mint[:8]}…  wallet={wallet_pubkey[:8]}…  tf={timeframe}")

    try:
        async with _resolve_sem:
            real_mint, token_info = await asyncio.wait_for(
                resolve_input(mint), timeout=20.0
            )
    except asyncio.TimeoutError:
        logger.warning(f"[LIVE] resolve_input timed out for {mint[:8]} — using raw mint")
        real_mint, token_info = mint, None
    token_name = (token_info or {}).get("name", "")
    token_symbol = (token_info or {}).get("symbol", "")

    try:
        engine_params = json.loads(params)
    except Exception:
        engine_params = {}

    live_trader = LiveTrader(
        token_mint=real_mint,
        keypair=keypair,
        buy_size_sol=buy_size,
        slippage_bps=slippage_bps,
        priority_fee_lamports=priority_fee,
        engine_kwargs=engine_params,
        skip_simulation=skip_sim,
        engine_version=engine_version,
    )

    cancelled = asyncio.Event()
    _active_live_traders[real_mint] = {
        "trader": live_trader,
        "token_name": token_name,
        "token_symbol": token_symbol,
        "timeframe": timeframe,
        "cancelled": cancelled,
        "wallet": wallet_pubkey,
    }

    # Resolve live data source
    live_source = (token_info or {}).get("_live_source", "pumpportal")
    live_query = (token_info or {}).get("pair_address") or real_mint
    if live_source == "solana_rpc" and (token_info or {}).get("pair_address"):
        ws_client = PumpSwapRPCClient(live_query)
    elif live_source == "dexscreener":
        poll_seconds = 0.25 if timeframe in {"1s", "5s", "15s"} else 0.5
        ws_client = DexScreenerPollClient(live_query, poll_seconds=poll_seconds)
    else:
        ws_client = PumpFunWSClient(real_mint)

    aggregator = CandleAggregator(timeframe)
    last_sent_price = None

    # ── Auto-record candles so backtests replay identical data ─────────────
    # The live trader and a standalone recorder would use separate
    # CandleAggregator instances fed from separate WebSocket connections,
    # causing subtle OHLCV differences.  By recording directly from the
    # live trader's own aggregator, the backtester sees the exact same
    # candle data.
    rec_id = data_store.create_recording(
        real_mint, timeframe, token_name, token_symbol
    )
    logger.info(f"[LIVE] Auto-recording candles → recording {rec_id}")

    async def send(obj: dict) -> bool:
        try:
            await websocket.send_json(obj)
            return True
        except Exception:
            return False

    if token_info:
        await send({"type": "token_info", "data": token_info})

    # Send historical candles + run through strategy (warm up indicators)
    try:
        async with _resolve_sem:
            hist = await asyncio.wait_for(
                get_historical_candles(real_mint, timeframe), timeout=15.0
            )
    except asyncio.TimeoutError:
        logger.warning(f"[LIVE] get_historical_candles timed out for {real_mint[:8]}")
        hist = []
    if hist:
        # Seed the auto-recording with the same historical candles
        data_store.insert_candles_batch(rec_id, hist)

        strategy_results = []
        for candle in hist:
            result = live_trader.update_historical_candle(
                time_val=int(candle["time"]),
                o=candle["open"], h=candle["high"],
                l=candle["low"], c=candle["close"],
                volume=candle.get("volume", 0),
            )
            strategy_results.append(result)

        await send({"type": "historical", "candles": hist, "strategy": strategy_results})
        last = hist[-1]
        aggregator.process_trade(last["close"], 0.0, float(last["time"]))
        logger.info(f"[LIVE] Sent {len(hist)} historical candles for {real_mint[:8]}")

    async def _process_stream(client) -> bool:
        nonlocal last_sent_price
        got_trade = False
        async for trade in client.stream():
            if cancelled.is_set():
                break
            got_trade = True
            is_synthetic = bool(trade.get("synthetic"))
            candle, is_new = aggregator.process_trade(
                trade["price"], trade["sol_amount"], trade["timestamp"],
                synthetic=is_synthetic,
            )
            candle_dict = candle.to_dict()
            current_price = candle_dict["close"]

            # Persist candle to the auto-recording (same as standalone recorder)
            ct = candle_dict["time"]
            data_store.insert_candle(
                rec_id, ct,
                candle_dict["open"], candle_dict["high"],
                candle_dict["low"], candle_dict["close"],
                candle_dict.get("volume", 0),
            )

            if last_sent_price is not None and current_price == last_sent_price and not is_new:
                continue
            last_sent_price = current_price

            strategy_result = live_trader.update(
                time_val=candle_dict["time"],
                o=candle_dict["open"], h=candle_dict["high"],
                l=candle_dict["low"], c=candle_dict["close"],
                volume=candle_dict.get("volume", 0),
                is_new=is_new,
            )

            trade_payload = None if is_synthetic else {
                "price": trade["price"],
                "sol_amount": trade["sol_amount"],
                "tx_type": trade["tx_type"],
                "trader": trade["trader"],
                "tx_hash": trade["tx_hash"],
            }

            ok = await send({
                "type": "candle",
                "candle": candle_dict,
                "is_new": is_new,
                "market_cap_sol": trade.get("market_cap_sol", 0),
                "market_cap_usd": trade.get("market_cap_usd", 0),
                "trade": trade_payload,
                "strategy": strategy_result,
            })
            if not ok:
                break

            # ── Market-cap safety floor check ─────────────────────────────
            mcap_usd = trade.get("market_cap_usd", 0)
            if mcap_usd and await live_trader.update_market_cap(float(mcap_usd)):
                logger.warning(
                    f"[LIVE] Market cap floor triggered for {real_mint[:8]}… — "
                    f"stopping session in 5s (waiting for emergency sell)"
                )
                await asyncio.sleep(5)  # grace period for emergency sell TX
                cancelled.set()
                break
        return got_trade

    async def stream_live():
        got = await _process_stream(ws_client)
        if not got and not cancelled.is_set() and not isinstance(ws_client, PumpFunWSClient):
            fallback = PumpFunWSClient(real_mint)
            try:
                await _process_stream(fallback)
            finally:
                fallback.stop()

    async def keepalive():
        while not cancelled.is_set():
            await asyncio.sleep(15)
            if not await send({"type": "ping"}):
                break
            if await live_trader.check_no_motion():
                logger.warning(
                    f"[LIVE] No motion stop triggered for {real_mint[:8]}… — "
                    f"stopping session in 5s"
                )
                await asyncio.sleep(5)
                cancelled.set()
                break

    # Allow LiveTrader to push trade updates directly to this WS
    live_trader.broadcast_fn = websocket.send_text

    async def listen():
        """Listen for messages from frontend (config updates, manual trades)."""
        try:
            while not cancelled.is_set():
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except Exception:
                    continue
                msg_type = msg.get("type", "")

                if msg_type == "update_config":
                    if "buy_size" in msg:
                        live_trader.buy_size_sol = float(msg["buy_size"])
                    if "slippage_bps" in msg:
                        live_trader.slippage_bps = int(msg["slippage_bps"])
                    if "priority_fee" in msg:
                        live_trader.priority_fee_lamports = int(msg["priority_fee"])
                elif msg_type == "manual_trade":
                    action = msg.get("action")
                    tx_sig = None
                    if action == "buy":
                        tx_sig = await live_trader.force_buy()
                    elif action == "sell":
                        tx_sig = await live_trader.force_sell()
                    if tx_sig:
                        # Trade updates will be pushed by LiveTrader._broadcast_status
                        pass
                elif msg_type == "pong":
                    pass
        except WebSocketDisconnect:
            pass
        finally:
            cancelled.set()
            ws_client.stop()

    async def shutdown():
        await cancelled.wait()
        ws_client.stop()
        try:
            await websocket.close(code=1000, reason="Session stopped")
        except Exception:
            pass

    try:
        await asyncio.gather(stream_live(), keepalive(), listen(), shutdown())
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        cancelled.set()
        ws_client.stop()
        # Finalize the auto-recording so it's available for backtesting
        data_store.stop_recording(rec_id)
        logger.info(f"[LIVE] Finalized recording {rec_id}")
        if real_mint in _active_live_traders:
            del _active_live_traders[real_mint]
        # Unsubscribe from scanner cooldown so it can be re-evaluated later
        if _axiom_scanner:
            _axiom_scanner.unsubscribe_mint(real_mint)
        logger.info(f"[LIVE] Disconnect  mint={real_mint[:8]}…")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")

