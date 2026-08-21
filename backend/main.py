import asyncio
import json
import logging
import os
import time
from pathlib import Path

# Load .env from backend/ OR project root (whichever exists) early — used
# by autofeed to find GMGN_API_KEY.  dotenv does NOT override env vars
# already set in the process.  We try backend/.env first, then ../.env.
try:
    from dotenv import load_dotenv
    _backend_dir = Path(__file__).parent
    _root_dir = _backend_dir.parent
    for _candidate in (_backend_dir / ".env", _root_dir / ".env"):
        if _candidate.exists():
            load_dotenv(_candidate)
            break
except ImportError:
    pass
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
from autofeed import AutoFeed, AutofeedConfig, Candidate
import data_store
from backtester import run_backtest, run_backtest_batch, run_futures_backtest
import futures_exchange
from sniper.sniper_router import router as sniper_router, set_engine as set_sniper_engine
from sniper.sniper_engine import SniperEngine, SniperConfig
from holder_flow import HolderFlowMonitor, get_shared_monitor

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


KEY_FILE_PATH = Path(__file__).parent / "data" / "live_key.json"


def _save_backend_private_key(pk: str):
    try:
        KEY_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE_PATH.write_text(json.dumps({"private_key": pk}), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[LIVE] Failed to persist private key to disk: {e}")


def _clear_backend_private_key():
    try:
        if KEY_FILE_PATH.exists():
            KEY_FILE_PATH.unlink()
    except Exception as e:
        logger.warning(f"[LIVE] Failed to remove persistent private key file: {e}")


def _load_backend_private_key() -> str:
    try:
        if KEY_FILE_PATH.exists():
            data = json.loads(KEY_FILE_PATH.read_text(encoding="utf-8"))
            return data.get("private_key", "")
    except Exception as e:
        logger.warning(f"[LIVE] Failed to read persistent private key file: {e}")
    return ""


# ── Live buy size: single-source-of-truth store ────────────────────────────
# The dashboard's "Buy Size (SOL)" input field is the ONLY source of the live
# buy size.  The frontend pushes its value here (POST /api/live/buy_size);
# every session-creation path (WS connect, autofeed server-side spawn) reads
# it from this store.  There is deliberately NO default — creation without a
# user-supplied size is refused instead of guessing one.
LIVE_SETTINGS_PATH = Path(__file__).parent / "data" / "live_settings.json"


def _save_live_buy_size(size: float):
    try:
        LIVE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIVE_SETTINGS_PATH.write_text(
            json.dumps({"buy_size": size}), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"[LIVE] Failed to persist buy size to disk: {e}")


def _load_live_buy_size() -> Optional[float]:
    try:
        if LIVE_SETTINGS_PATH.exists():
            data = json.loads(LIVE_SETTINGS_PATH.read_text(encoding="utf-8"))
            size = float(data.get("buy_size", 0.0))
            if size > 0:
                return size
    except Exception as e:
        logger.warning(f"[LIVE] Failed to read persistent buy size file: {e}")
    return None


def _get_live_buy_size() -> Optional[float]:
    """Current user-supplied buy size (in-memory first, disk fallback).

    Returns None when the dashboard has never pushed a valid value — callers
    must treat that as 'refuse to trade', never as 'use a default'."""
    size = getattr(app.state, "lt_buy_size", None)
    if size is None or not isinstance(size, (int, float)) or size <= 0:
        size = _load_live_buy_size()
        if size is not None:
            app.state.lt_buy_size = size
    if isinstance(size, (int, float)) and size > 0:
        return float(size)
    return None


@app.on_event("startup")
async def startup_load_live_key():
    pk = _load_backend_private_key()
    if pk:
        try:
            kp = keypair_from_private_key(pk)
            app.state.lt_private_key = pk
            app.state.lt_wallet_connected = True
            logger.info(f"[LIVE] Restored server-side private key for wallet {str(kp.pubkey())[:8]}…")
        except Exception as e:
            logger.warning(f"[LIVE] Saved private key file invalid: {e}")


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

        # ── Holder-flow monitor (records dev/insider wallet sells) ────────
        # Shared process-wide monitor — one GMGN poller regardless of how many
        # recordings/sessions are active (iter36 rate-limit fix).
        holder_monitor = get_shared_monitor()
        await holder_monitor.start()
        holder_monitor.watch_token(real_mint, recording_id=rec_id)
        # Consume events from the queue so it doesn't fill up
        async def _consume_holder_events():
            while not cancelled.is_set():
                try:
                    event = await asyncio.wait_for(
                        holder_monitor.event_queue.get(), timeout=1.0
                    )
                    # Event is already persisted to DB by the monitor itself
                    logger.debug(
                        f"[Recorder] Holder-flow event: {event.mint[:8]} "
                        f"{event.side} ${event.amount_usd:.2f} "
                        f"tag={event.tag or 'unknown'}"
                    )
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
        holder_consumer_task = asyncio.create_task(_consume_holder_events())

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
                is_buy_rec: Optional[bool] = None
                if not is_synthetic:
                    tx_rec = trade.get("tx_type", "")
                    if tx_rec == "buy":
                        is_buy_rec = True
                    elif tx_rec == "sell":
                        is_buy_rec = False
                candle, is_new = aggregator.process_trade(
                    trade["price"], trade["sol_amount"], trade["timestamp"],
                    synthetic=is_synthetic,
                    is_buy=is_buy_rec,
                    pool_sol=trade.get("pool_sol", 0.0),
                    market_cap_usd=trade.get("market_cap_usd", 0.0),
                )
                # Persist every tick (real or synthetic) so the candle row in
                # the DB always reflects the current accumulated OHLCV state.
                candle_dict = candle.to_dict()
                ct = candle_dict["time"]
                data_store.insert_candle(
                    rec_id, ct,
                    candle_dict["open"], candle_dict["high"],
                    candle_dict["low"], candle_dict["close"],
                    candle_dict.get("volume", 0),
                    candle_dict.get("buy_volume", 0.0),
                    candle_dict.get("sell_volume", 0.0),
                    candle_dict.get("pool_sol", 0.0),
                    candle_dict.get("market_cap_usd", 0.0),
                )
                last_candle_time = ct
        finally:
            ws_client.stop()
            holder_monitor.unwatch_token(real_mint)
            await holder_monitor.stop()
            holder_consumer_task.cancel()
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


@app.post("/api/recordings/finish_stale")
@app.delete("/api/recordings/finish_stale")
async def finish_stale_recordings(stale_seconds: int = 60):
    """Find recordings still marked as `recording` but with no recent candle updates
    and mark them as completed. Useful when a recorder task crashed or the
    process was restarted and left DB rows in a recording state.

    `stale_seconds` defaults to 60s; recordings whose last candle time is
    older than `now - stale_seconds` will be stopped.
    """
    now = time.time()
    fixed = []
    checked = 0
    recs = data_store.list_recordings()
    for r in recs:
        checked += 1
        if r.get("status") != "recording":
            continue
        rid = r.get("id")
        # Do not auto-finish recordings that have an active recorder task
        if rid in _active_recorders:
            continue
        try:
            candles = data_store.get_recording_candles(rid)
            if not candles:
                # No candles at all — treat as stale
                data_store.stop_recording(rid)
                fixed.append(rid)
                continue
            last_time = candles[-1]["time"]
            if last_time < (now - stale_seconds):
                data_store.stop_recording(rid)
                fixed.append(rid)
        except Exception:
            continue

    return JSONResponse({"fixed": fixed, "checked": checked, "stale_seconds": stale_seconds})


async def _stale_scanner_loop(interval_seconds: int = 60, stale_seconds: int = 60):
    """Background loop: periodically run the stale-finish pass."""
    while True:
        try:
            await finish_stale_recordings(stale_seconds=stale_seconds)
        except Exception:
            logger.exception("stale scanner error")
        await asyncio.sleep(interval_seconds)


@app.on_event("startup")
async def start_stale_scanner():
    # Run once at startup to catch any stale DB rows from previous crashes
    try:
        await finish_stale_recordings(stale_seconds=60)
    except Exception:
        logger.exception("initial stale finish failed")
    # Start background task to run periodically
    app.state._stale_scanner_task = asyncio.create_task(_stale_scanner_loop())


@app.on_event("shutdown")
async def stop_stale_scanner():
    t = getattr(app.state, "_stale_scanner_task", None)
    if t:
        t.cancel()


@app.delete("/api/recordings/{recording_id}")
async def delete_recording_endpoint(recording_id: int):
    data_store.delete_recording(recording_id)
    return JSONResponse({"status": "deleted"})


@app.post("/api/recordings/cleanup")
@app.delete("/api/recordings/cleanup")
async def cleanup_recordings_endpoint(min_candles: int = Query(default=100)):
    deleted_count = data_store.cleanup_small_recordings(min_candles=min_candles)
    return JSONResponse({"status": "cleaned", "deleted_count": deleted_count, "min_candles": min_candles})


# ── Backtest API ─────────────────────────────────────────────────────────────

@app.post("/api/backtest")
async def run_backtest_endpoint(body: dict = Body(...)):
    recording_id = body.get("recording_id")
    if recording_id is None:
        return JSONResponse({"error": "recording_id is required"}, status_code=400)
    engine_params = body.get("engine_params", {})
    engine_version = int(body.get("engine_version", 1))
    # ── Futures mode params (validated; "spot" is the no-op default) ──────
    market_type = str(body.get("market_type", "spot")).lower()
    if market_type not in ("spot", "futures"):
        return JSONResponse({"error": "market_type must be 'spot' or 'futures'"}, status_code=400)
    leverage = float(body.get("leverage", 1.0))
    if not (1.0 <= leverage <= 125.0):
        return JSONResponse({"error": "leverage must be between 1 and 125"}, status_code=400)
    futures_kwargs = {}
    if market_type == "futures":
        futures_kwargs = {
            "market_type": "futures",
            "leverage": leverage,
            "funding_rate_per_interval": float(body.get("funding_rate_per_interval", 0.0001)),
            "funding_interval_seconds": int(body.get("funding_interval_seconds", 28800)),
            "maintenance_margin_rate": float(body.get("maintenance_margin_rate", 0.005)),
            "futures_taker_fee": float(body.get("futures_taker_fee", 0.00045)),
            "futures_slippage_pct": body.get("futures_slippage_pct"),
        }
    try:
        # Run CPU-bound backtest in thread pool to avoid blocking the event loop
        result = await asyncio.to_thread(
            run_backtest,
            recording_id=int(recording_id),
            engine_params=engine_params,
            buy_size_sol=body.get("buy_size_sol", 0.1),
            priority_fee=0.0001,
            bribe_fee=0.0,
            slippage_pct=body.get("slippage_pct", 1.0),
            starting_balance=body.get("starting_balance", 1.0),
            engine_version=engine_version,
            **futures_kwargs,
        )
        return JSONResponse(result)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.error(f"Backtest error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/backtest/batch")
async def run_backtest_batch_endpoint(body: dict = Body(default={})):
    """Run backtests on ALL completed recordings in parallel.

    Optional filters in the request body:
      - ``recording_ids``: explicit list of recording IDs to run.
      - ``last_night``: if true, restrict to recordings started between
        10:00 PM local time of the previous day and 12:00 PM (noon) local
        time of the current day.
    """
    engine_params = body.get("engine_params", {})
    engine_version = int(body.get("engine_version", 1))
    batch_id = str(int(time.time() * 1000))
    recording_ids = body.get("recording_ids")
    if recording_ids is not None:
        recording_ids = [int(r) for r in recording_ids]
    last_night = bool(body.get("last_night", False))
    last_12h = bool(body.get("last_12h", False))
    # ── Futures mode params ────────────────────────────────────────────────
    market_type = str(body.get("market_type", "spot")).lower()
    if market_type not in ("spot", "futures"):
        return JSONResponse({"error": "market_type must be 'spot' or 'futures'"}, status_code=400)
    leverage = float(body.get("leverage", 1.0))
    if not (1.0 <= leverage <= 125.0):
        return JSONResponse({"error": "leverage must be between 1 and 125"}, status_code=400)
    futures_kwargs = {}
    if market_type == "futures":
        futures_kwargs = {
            "market_type": "futures",
            "leverage": leverage,
            "funding_rate_per_interval": float(body.get("funding_rate_per_interval", 0.0001)),
            "funding_interval_seconds": int(body.get("funding_interval_seconds", 28800)),
            "maintenance_margin_rate": float(body.get("maintenance_margin_rate", 0.005)),
            "futures_taker_fee": float(body.get("futures_taker_fee", 0.00045)),
            "futures_slippage_pct": body.get("futures_slippage_pct"),
        }
    try:
        results = await asyncio.to_thread(
            run_backtest_batch,
            engine_params=engine_params,
            max_workers=os.cpu_count() or 8,
            buy_size_sol=body.get("buy_size_sol", 0.1),
            priority_fee=0.0001,
            bribe_fee=0.0,
            slippage_pct=body.get("slippage_pct", 1.0),
            starting_balance=body.get("starting_balance", 1.0),
            batch_id=batch_id,
            engine_version=engine_version,
            recording_ids=recording_ids,
            last_night=last_night,
            last_12h=last_12h,
            **futures_kwargs,
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
async def list_backtests_endpoint(market_type: Optional[str] = None):
    rows = data_store.list_backtests()
    if market_type:
        rows = [r for r in rows if (r.get("market_type") or "spot") == market_type]
    return JSONResponse(rows)


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


# ── Futures data + backtest API ───────────────────────────────────────────────

@app.get("/api/futures/markets")
async def list_futures_markets():
    """Listed perp instruments + how much history is currently cached."""
    return JSONResponse(futures_exchange.list_supported_symbols())


@app.post("/api/futures/backtest")
async def run_futures_backtest_endpoint(body: dict = Body(...)):
    """
    Historical perp backtest on Bybit linear-USDT klines (accounted in USDC).

    Required body fields:
      symbol      – one of BTC / ETH / SOL / LTC
    Optional:
      days_back   – 30 (default) … 90    how much history to pull/cache
      timeframe   – "1h" (default) | "15m"
      leverage    – 1 (default) … 50
      buy_size_sol (margin per trade, USDC)          – default 100
      starting_balance (USDC)                        – default 1000
      funding_rate_per_interval / funding_interval_seconds
      futures_taker_fee / futures_slippage_pct / maintenance_margin_rate
      engine_params / engine_version (1 default, 2 = V2 RBPF)
      batch_id    – batch label for grouping
    """
    symbol = str(body.get("symbol", "")).strip().upper()
    if symbol not in futures_exchange.DEFAULT_SYMBOLS:
        return JSONResponse(
            {"error": f"symbol must be one of {sorted(futures_exchange.DEFAULT_SYMBOLS)}"},
            status_code=400,
        )

    timeframe = str(body.get("timeframe", "1h"))
    days_back = int(body.get("days_back", 30))
    if days_back < 1 or days_back > 90:
        return JSONResponse({"error": "days_back must be 1..90"}, status_code=400)
    if timeframe not in ("15m", "1h"):
        return JSONResponse({"error": "timeframe must be '1h' or '15m'"}, status_code=400)

    # Pull market-level defaults from the FUTURES_MARKET_DEFAULTS constant so
    # the user-facing tab inherits the Pareto-optimal starting point (iter42
    # convergence). Caller-supplied body fields always override.
    try:
        from strategy_engineV2 import FUTURES_MARKET_DEFAULTS
    except Exception:
        FUTURES_MARKET_DEFAULTS = {}
    _md = FUTURES_MARKET_DEFAULTS or {}

    leverage = float(body.get("leverage", _md.get("leverage", 1.0)))
    if not (1.0 <= leverage <= 50.0):
        return JSONResponse({"error": "leverage must be 1..50"}, status_code=400)

    market_params = {
        "leverage": leverage,
        "timeframe": timeframe,
        "futures_days": days_back,
        "buy_size_sol": float(body.get("buy_size_sol", _md.get("buy_size_sol", 100.0))),
        "starting_balance": float(body.get("starting_balance", _md.get("starting_balance", 1000.0))),
        "futures_taker_fee": float(body.get("futures_taker_fee", _md.get("futures_taker_fee", 0.00045))),
        "futures_slippage_pct": float(body.get("futures_slippage_pct", _md.get("futures_slippage_pct", 0.1))),
        "maintenance_margin_rate": float(body.get("maintenance_margin_rate", _md.get("maintenance_margin_rate", 0.005))),
        "funding_interval_seconds": int(body.get("funding_interval_seconds", _md.get("funding_interval_seconds", 28800))),
        "funding_rate_per_interval": float(body.get("funding_rate_per_interval", _md.get("funding_rate_per_interval", 0.0001))),
    }
    engine_params = body.get("engine_params", {}) or {}
    engine_version = int(body.get("engine_version", 1))
    batch_id = body.get("batch_id") or None

    try:
        # Materialise the cache + run the backtest off-thread (CPU-bound and
        # does outbound HTTP with retry — never on the event loop).
        def _run():
            futures_exchange.get_futures_candles(
                symbol, timeframe=timeframe, days_back=days_back,
            )
            saved = dict(engine_params)
            saved["futures_symbol"] = symbol
            saved["futures_timeframe"] = timeframe
            return run_futures_backtest(
                symbol=symbol,
                engine_params=saved,
                market_params=market_params,
                engine_version=engine_version,
                batch_id=batch_id,
                futures_days=days_back,
            )
        return JSONResponse(await asyncio.to_thread(_run))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.error(f"Futures backtest error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.websocket("/ws/{mint}")
async def chart_ws(
    websocket: WebSocket,
    mint: str,
    timeframe: str = Query(default="1s"),
    params: str = Query(default="{}"),
    buy_size: float = Query(default=0.1),
    slippage_pct: float = Query(default=1.0),
    engine_version: int = Query(default=1),
):
    # Guard: reject reserved path segments that have dedicated routes.
    # FastAPI matches /ws/{mint} before /ws/sniper because the wildcard is
    # declared first. Must accept() before close() to avoid a 403.
    # For "autofeed", delegate directly — FastAPI WS routing doesn't fall
    # through, so the dedicated /ws/autofeed endpoint is unreachable.
    if mint == "autofeed":
        await _autofeed_ws_handler(websocket)
        return
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
        priority_fee=0.0001,
        bribe_fee=0.0,
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
                buy_volume=candle.get("buy_volume", 0.0),
                sell_volume=candle.get("sell_volume", 0.0),
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
            is_buy_trade: Optional[bool] = None
            if not is_synthetic:
                tx = trade.get("tx_type", "")
                if tx == "buy":
                    is_buy_trade = True
                elif tx == "sell":
                    is_buy_trade = False
            candle, is_new = aggregator.process_trade(
                trade["price"], trade["sol_amount"], trade["timestamp"],
                synthetic=is_synthetic,
                is_buy=is_buy_trade,
                market_cap_usd=trade.get("market_cap_usd", 0.0),
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
                buy_volume=candle_dict.get("buy_volume", 0.0),
                sell_volume=candle_dict.get("sell_volume", 0.0),
                market_cap_usd=candle_dict.get("market_cap_usd", 0.0),
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
# Live sessions are SERVER-SIDED: they own the trader, the market stream, the
# auto-recording and the holder-flow pump, and keep running independently of
# any browser tab.  Tabs attach as viewers over /ws/live/{mint}; closing a tab
# only detaches that viewer.  A session ends when it is explicitly stopped
# (POST /api/live/stop, the ⏹ Stop button) or a safety stop fires.
_active_live_traders: dict[str, "_LiveSession"] = {}   # keyed by token mint
_live_session_lock = asyncio.Lock()

# Persistent session stats and trade events for the current server run
_completed_live_sessions: list[dict] = []
_server_trade_events: list[dict] = []


def _record_server_trade_event(*, mint: str, token_symbol: str, event: str, payload: dict):
    ts = payload.get("timestamp") or time.time()
    sig = payload.get("detail") or ""
    if event == "buy_confirmed":
        ct = payload.get("current_trade") or {}
        _server_trade_events.append({
            "id": len(_server_trade_events) + 1,
            "mint": mint,
            "token_symbol": token_symbol,
            "action": "BUY",
            "timestamp": ts,
            "price": ct.get("entry_price") or 0.0,
            "pnl_sol": 0.0,
            "pnl_pct": 0.0,
            "tx_hash": sig,
            "status": "confirmed",
        })
    elif event == "sell_confirmed":
        ct = payload.get("closed_trade") or payload.get("current_trade") or {}
        _server_trade_events.append({
            "id": len(_server_trade_events) + 1,
            "mint": mint,
            "token_symbol": token_symbol,
            "action": "SELL",
            "timestamp": ts,
            "price": ct.get("exit_price") or 0.0,
            "pnl_sol": ct.get("pnl_sol") or 0.0,
            "pnl_pct": ct.get("pnl_pct") or 0.0,
            "tx_hash": sig,
            "status": "confirmed",
        })


class _LiveSession:
    """Server-side live-trading session, decoupled from any browser tab.

    The /ws/live/{mint} handler creates one _LiveSession per token and then
    attaches as a viewer.  All trading logic lives in `run()`, a detached
    background task that survives tab closes/reopens.  Viewers receive every
    candle/strategy/trade_update via per-viewer broadcast queues and can send
    control messages (update_config, manual_trade) at any time.
    """

    def __init__(self, *, real_mint: str, token_name: str, token_symbol: str,
                 timeframe: str, trader: LiveTrader, token_info: Optional[dict],
                 engine_version: int = 2):
        self.real_mint = real_mint
        self.token_name = token_name
        self.token_symbol = token_symbol
        self.timeframe = timeframe
        self.trader = trader
        self.token_info = token_info
        self.engine_version = engine_version
        self.cancelled = asyncio.Event()
        self.warmed = asyncio.Event()      # set once historical warmup finished
        self.subscribers: set[asyncio.Queue] = set()
        self.task: Optional[asyncio.Task] = None
        self.rec_id: Optional[int] = None
        self.warmup_candles: list[dict] = []
        self.warmup_strategy: list[dict] = []
        self.last_strategy_result: Optional[dict] = None
        self.stop_reason: str = ""
        # LiveTrader pushes trade_update JSON strings here → fanned out to viewers
        trader.broadcast_fn = self.broadcast_text

    # ── viewer fan-out ────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def broadcast(self, obj) -> None:
        """Fan a message out to every attached viewer.  Never blocks the
        trading loop: a slow/full viewer just misses messages."""
        for q in list(self.subscribers):
            try:
                q.put_nowait(obj)
            except asyncio.QueueFull:
                pass

    async def broadcast_text(self, payload: str) -> None:
        # LiveTrader._broadcast_status sends pre-serialised JSON strings
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
            if isinstance(data, dict) and data.get("type") == "trade_update":
                evt = data.get("event")
                if evt in ("buy_confirmed", "sell_confirmed"):
                    _record_server_trade_event(
                        mint=self.real_mint,
                        token_symbol=self.token_symbol,
                        event=evt,
                        payload=data,
                    )
        except Exception:
            pass
        self.broadcast(payload)

    def wallet(self) -> str:
        return str(self.trader.keypair.pubkey())

    def stop(self, reason: str = "manual_stop") -> None:
        if not self.cancelled.is_set():
            self.stop_reason = reason
            self.cancelled.set()

    # ── session runner ────────────────────────────────────────────────────

    async def run(self) -> None:
        real_mint = self.real_mint
        live_trader = self.trader
        timeframe = self.timeframe
        token_info = self.token_info or {}
        rec_id = self.rec_id
        logger.info(f"[LIVE] Session start  mint={real_mint[:8]}…  wallet={self.wallet()[:8]}…  tf={timeframe}")

        # Resolve live data source
        live_source = token_info.get("_live_source", "pumpportal")
        live_query = token_info.get("pair_address") or real_mint
        if live_source == "solana_rpc" and token_info.get("pair_address"):
            ws_client = PumpSwapRPCClient(live_query)
        elif live_source == "dexscreener":
            poll_seconds = 0.25 if timeframe in {"1s", "5s", "15s"} else 0.5
            ws_client = DexScreenerPollClient(live_query, poll_seconds=poll_seconds)
        else:
            ws_client = PumpFunWSClient(real_mint)

        aggregator = CandleAggregator(timeframe)
        last_sent_price = None

        # ── Holder-flow monitor (live dev/insider sell detection) ────────────
        # Watches the token and persists events to the auto-recording's
        # holder_flow table (so the live session is itself backtestable), and
        # feeds the events into the strategy engine so the iter36 entry gate /
        # exit trigger fire in real time.  Parity: the engine checks the same
        # event list the backtester would later replay from the DB.
        # Shared process-wide monitor — one GMGN poller regardless of how many
        # sessions are active (iter36 rate-limit fix).
        holder_monitor = None
        _hf_pump_task = None
        _hf_stop = asyncio.Event()
        # Track how many events we've already pushed into the engine (append-only)
        _hf_pushed = {"n": 0}
        # iter57: global regime refresh task (Q(t) for the V2 regime controller)
        _regime_task = None

        async def _holder_flow_pump():
            """Background task that pushes new holder-flow events into the engine
            every 1s, decoupled from trade ticks.  Without this, events discovered
            by the GMGN poller sit in the monitor's buffer until the next trade
            arrives — which may be 10s+ on illiquid tokens, making dev_sell_exit
            fire far later than the backtester (which sees events at their exact
            on-chain timestamp)."""
            while not _hf_stop.is_set():
                try:
                    _hf_events = holder_monitor.get_events_as_dicts(real_mint)
                    if len(_hf_events) > _hf_pushed["n"]:
                        _new = _hf_events[_hf_pushed["n"]:]
                        _hf_pushed["n"] = len(_hf_events)
                        try:
                            live_trader.engine.append_holder_flow_events(_new)
                            # Immediately check if this triggers an immediate dev_sell_exit
                            exit_reason = live_trader.check_immediate_holder_flow_exit()
                            if exit_reason:
                                logger.info(f"[HOLDER FLOW] Immediate exit triggered: {exit_reason}")
                                live_trader._pending_exit = True
                                live_trader._pending_exit_reason = exit_reason
                                live_trader._pending_buy = False
                                live_trader.engine.notify_trade_closed()
                                # Fire off the sell swap immediately in the background
                                asyncio.create_task(live_trader.execute_sell(exit_reason))
                        except AttributeError:
                            pass  # V1 engine has no holder-flow surface
                except Exception:
                    pass
                await asyncio.sleep(1.0)

        try:
            holder_monitor = get_shared_monitor()
            await holder_monitor.start()
            holder_monitor.watch_token(real_mint, recording_id=rec_id)
            # Pre-load any holder_flow events already persisted for this
            # recording (e.g. from a prior session on the same token, or events
            # the monitor captured before the session started).  This matches
            # the backtester which calls set_holder_flow_events() with the full
            # event list before the candle loop starts.
            _existing_hf = data_store.get_holder_flow(rec_id)
            if _existing_hf:
                try:
                    live_trader.engine.set_holder_flow_events(_existing_hf)
                except AttributeError:
                    pass  # V1 engine has no holder-flow surface
            _hf_pushed["n"] = len(_existing_hf)
            _hf_pump_task = asyncio.ensure_future(_holder_flow_pump())

            # ── iter57: global regime Q(t) pre-load + live refresh ──────────
            # Parity with the backtester join (candle.time → UTC date →
            # global_regime_cache.json): pre-load the cached history, then a
            # 60s poller recomputes TODAY's Q from the persisted trade history
            # (fetch_global_regime.compute_live_q — identical lag-3 math) and
            # appends it.  The engine ignores the map unless
            # v2_regime_enable > 0, so this is a no-op when the controller
            # is OFF (default).
            try:
                import fetch_global_regime as _fgr
                if hasattr(live_trader.engine, "set_global_regime_map"):
                    _qmap = await asyncio.to_thread(_fgr.load_cache)
                    if _qmap:
                        live_trader.engine.set_global_regime_map(_qmap)

                    async def _regime_refresh():
                        import datetime as _dt_r
                        while not _hf_stop.is_set():
                            try:
                                _q = await asyncio.to_thread(_fgr.compute_live_q)
                                if _q is not None:
                                    _today = _dt_r.datetime.now(
                                        _dt_r.timezone.utc).strftime("%Y-%m-%d")
                                    live_trader.engine.append_global_regime(_today, _q)
                            except Exception:
                                pass
                            await asyncio.sleep(60.0)

                    if float(getattr(live_trader.engine, "_v2_regime_enable", 0.0)) > 0.0:
                        _regime_task = asyncio.ensure_future(_regime_refresh())
            except Exception:
                pass  # regime feed is strictly additive — never break a session

            # ── Historical warm-up (seeds the recording + the engine) ────────
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
                        pool_sol=candle.get("pool_sol", 0.0),
                    )
                    strategy_results.append(result)

                self.warmup_candles = hist
                self.warmup_strategy = strategy_results
                if strategy_results:
                    self.last_strategy_result = strategy_results[-1]
                last = hist[-1]
                aggregator.process_trade(last["close"], 0.0, float(last["time"]))
                logger.info(f"[LIVE] Warmed up on {len(hist)} historical candles for {real_mint[:8]}")
            self.warmed.set()

            async def _process_stream(client) -> bool:
                nonlocal last_sent_price
                got_trade = False
                async for trade in client.stream():
                    if self.cancelled.is_set():
                        break
                    got_trade = True
                    is_synthetic = bool(trade.get("synthetic"))
                    is_buy_live: Optional[bool] = None
                    if not is_synthetic:
                        tx_live = trade.get("tx_type", "")
                        if tx_live == "buy":
                            is_buy_live = True
                        elif tx_live == "sell":
                            is_buy_live = False
                    candle, is_new = aggregator.process_trade(
                        trade["price"], trade["sol_amount"], trade["timestamp"],
                        synthetic=is_synthetic,
                        is_buy=is_buy_live,
                        pool_sol=trade.get("pool_sol", 0.0),
                        market_cap_usd=trade.get("market_cap_usd", 0.0),
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
                        candle_dict.get("buy_volume", 0.0),
                        candle_dict.get("sell_volume", 0.0),
                        candle_dict.get("pool_sol", 0.0),
                        candle_dict.get("market_cap_usd", 0.0),
                    )

                    if last_sent_price is not None and current_price == last_sent_price and not is_new:
                        continue
                    last_sent_price = current_price

                    strategy_result = live_trader.update(
                        time_val=candle_dict["time"],
                        o=candle_dict["open"], h=candle_dict["high"],
                        l=candle_dict["low"], c=candle_dict["close"],
                        volume=candle_dict.get("volume", 0),
                        buy_volume=candle_dict.get("buy_volume", 0.0),
                        sell_volume=candle_dict.get("sell_volume", 0.0),
                        is_new=is_new,
                        market_cap_usd=candle_dict.get("market_cap_usd", 0.0),
                        pool_sol=candle_dict.get("pool_sol", 0.0),
                    )
                    self.last_strategy_result = strategy_result

                    trade_payload = None if is_synthetic else {
                        "price": trade["price"],
                        "sol_amount": trade["sol_amount"],
                        "tx_type": trade["tx_type"],
                        "trader": trade["trader"],
                        "tx_hash": trade["tx_hash"],
                    }

                    self.broadcast({
                        "type": "candle",
                        "candle": candle_dict,
                        "is_new": is_new,
                        "market_cap_sol": trade.get("market_cap_sol", 0),
                        "market_cap_usd": trade.get("market_cap_usd", 0),
                        "trade": trade_payload,
                        "strategy": strategy_result,
                    })

                    # ── Market-cap safety floor check ─────────────────────────
                    # update_market_cap() blocks until any emergency sell has
                    # fully settled on-chain (or the MCAP_STOP_SELL_TIMEOUT_SECONDS
                    # backstop fires), so by the time it returns True the wallet
                    # is empty and it is safe to terminate immediately — no
                    # grace sleep needed.
                    mcap_usd = trade.get("market_cap_usd", 0)
                    if mcap_usd and await live_trader.update_market_cap(float(mcap_usd)):
                        logger.warning(
                            f"[LIVE] Market cap floor triggered for {real_mint[:8]}… — "
                            f"emergency sell settled, stopping session"
                        )
                        self.stop("mcap_floor")
                        break

                    # ── No-motion stop check ─────────────────────────────────
                    # Only fires when idle (no position, no pending signals) —
                    # never interrupts an active position.
                    if live_trader.no_motion_stop_triggered:
                        logger.warning(
                            f"[LIVE] No-motion stop triggered for {real_mint[:8]}… — "
                            f"session idle, shutting down"
                        )
                        self.stop("no_motion")
                        break
                return got_trade

            async def stream_live():
                got = await _process_stream(ws_client)
                if not got and not self.cancelled.is_set() and not isinstance(ws_client, PumpFunWSClient):
                    fallback = PumpFunWSClient(real_mint)
                    try:
                        await _process_stream(fallback)
                    finally:
                        fallback.stop()

            async def _shutdown():
                await self.cancelled.wait()
                ws_client.stop()

            await asyncio.gather(stream_live(), _shutdown())
            if not self.cancelled.is_set():
                # The market stream itself died (source disconnect).  An
                # unattended position with no data feed must not be held —
                # stop the session; the finally block emergency-sells.
                logger.warning(f"[LIVE] Market stream ended for {real_mint[:8]}… — stopping session")
                self.stop("stream_ended")
        except asyncio.CancelledError:
            # Server shutdown — let the finally block run its cleanup
            if not self.cancelled.is_set():
                self.stop_reason = "server_shutdown"
        except Exception:
            # Unexpected runner crash — mark the reason, teardown still runs
            logger.exception(f"[LIVE] Session runner crashed  mint={real_mint[:8]}…")
            if not self.cancelled.is_set():
                self.stop_reason = "runner_error"
        finally:
            ws_client.stop()

            # Stop the holder-flow background pump
            _hf_stop.set()
            if _hf_pump_task is not None:
                _hf_pump_task.cancel()
                try:
                    await _hf_pump_task
                except (asyncio.CancelledError, Exception):
                    pass
            # iter57: stop the global regime refresher
            if _regime_task is not None:
                _regime_task.cancel()
                try:
                    await _regime_task
                except (asyncio.CancelledError, Exception):
                    pass

            # Stop the holder-flow monitor for this session
            if holder_monitor is not None:
                try:
                    holder_monitor.unwatch_token(real_mint)
                    await holder_monitor.stop()
                except Exception:
                    pass

            # ── Emergency position cleanup before session teardown ──────────
            # If a position is still open when the session ends, execute an
            # emergency sell to avoid leaving positions unattended.
            await live_trader.cleanup(reason=self.stop_reason or "session_ended")

            # Finalize the auto-recording so it's available for backtesting
            data_store.stop_recording(rec_id)
            logger.info(f"[LIVE] Finalized recording {rec_id}")

            # Wake up any remaining viewers so their UIs reflect the end
            self.broadcast({"type": "session_ended", "reason": self.stop_reason or "session_ended"})
            _completed_live_sessions.append({
                "mint": real_mint,
                "token_name": self.token_name,
                "token_symbol": self.token_symbol,
                "stats": live_trader.stats.to_dict(),
                "rec_id": rec_id,
                "ended_at": time.time(),
            })
            if _active_live_traders.get(real_mint) is self:
                del _active_live_traders[real_mint]
            logger.info(
                f"[LIVE] Session ended  mint={real_mint[:8]}…  reason={self.stop_reason or 'session_ended'}"
            )


@app.get("/api/live/status")
async def live_status():
    traders = []
    seen_mints = set()

    total_pnl = 0.0
    unrealized_pnl = 0.0
    winning_trades = 0
    losing_trades = 0
    total_trades = 0

    # 1. Completed sessions in this server run — in-memory only, so the
    # session performance (pnl / winrate) resets whenever the program
    # restarts, but survives a mere page refresh.
    for cs in _completed_live_sessions:
        m = cs.get("mint")
        if m:
            seen_mints.add(m)
        st = cs.get("stats", {})
        total_pnl += st.get("total_pnl_sol", 0.0)
        winning_trades += st.get("winning_trades", 0)
        losing_trades += st.get("losing_trades", 0)
        total_trades += st.get("total_trades", 0)

    # 2. Active sessions
    for mint, session in _active_live_traders.items():
        trader: LiveTrader = session.trader
        st = trader.stats.to_dict()
        if mint:
            seen_mints.add(mint)
        total_pnl += st.get("total_pnl_sol", 0.0)
        winning_trades += st.get("winning_trades", 0)
        losing_trades += st.get("losing_trades", 0)
        total_trades += st.get("total_trades", 0)

        if trader.current_trade and trader._last_price > 0 and trader.current_trade.entry_price > 0:
            upnl = (trader._last_price - trader.current_trade.entry_price) / trader.current_trade.entry_price * trader.current_trade.size_sol
            unrealized_pnl += upnl

        traders.append({
            "mint": mint,
            "token_name": session.token_name,
            "token_symbol": session.token_symbol,
            "timeframe": session.timeframe,
            "engine_version": session.engine_version,
            "status": "running" if not session.cancelled.is_set() else "stopping",
            "recording_id": session.rec_id,
            "viewers": len(session.subscribers),
            "stats": st,
            "current_trade": trader.current_trade.to_dict() if trader.current_trade else None,
            "trade_count": len(trader.trade_history),
        })

    win_rate = (winning_trades / total_trades * 100.0) if total_trades > 0 else 0.0

    return JSONResponse({
        "traders": traders,
        "count": len(traders),
        "session_summary": {
            "total_pnl_sol": round(total_pnl, 6),
            "unrealized_pnl_sol": round(unrealized_pnl, 6),
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "total_trades": total_trades,
            "win_rate": round(win_rate, 2),
            "tokens_traded": len(seen_mints),
        },
        "trades": _server_trade_events,
    })


@app.get("/api/live/history")
async def live_history(limit: int = Query(20, ge=1, le=100)):
    """Recent completed live-trading sessions with their closed trades.

    Read from each session's physical trades.jsonl ledger (survives restarts
    and sessions that ended before the page was opened).  Enables the
    frontend to rebuild the live trade-history table on page load — SELL rows
    for trades that closed while the dashboard was closed were previously
    lost forever because the table was only ever fed by live WS events.
    """
    from live_session_logger import LOG_ROOT
    sessions = []
    if LOG_ROOT.is_dir():
        dirs = sorted(
            (p for p in LOG_ROOT.iterdir()
             if p.is_dir() and (p / "trades.jsonl").exists()),
            key=lambda p: p.name, reverse=True,
        )[:limit]
        for d in dirs:
            meta = {"token_mint": "", "wallet": ""}
            trades = []
            try:
                with open(d / "trades.jsonl", "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        event = rec.get("event")
                        if event == "session_open":
                            meta["token_mint"] = rec.get("token_mint", meta["token_mint"])
                            meta["wallet"] = rec.get("wallet", meta["wallet"])
                        elif event == "trade_closed":
                            t = rec.get("trade") or {}
                            if isinstance(t, dict) and t.get("status") == "closed":
                                trades.append({
                                    "event_ts": rec.get("ts"),
                                    **t,
                                })
            except Exception:
                pass
            sessions.append({
                "session_id": d.name,
                "token_mint": meta["token_mint"] or d.name.split("_", 2)[-1],
                "wallet": meta["wallet"],
                "trades": trades,
            })
    return JSONResponse({"sessions": sessions, "count": len(sessions)})


@app.post("/api/live/stop")
async def live_stop(body: dict = Body(...)):
    mint = body.get("mint", "").strip()
    session = _active_live_traders.get(mint)
    if not mint or session is None:
        return JSONResponse({"error": "No active trader for this mint"}, status_code=404)
    # Signals the session runner to stop.  The runner performs the emergency
    # position cleanup + recording finalization asynchronously and removes
    # itself from _active_live_traders when done.
    session.stop("manual_stop")
    return JSONResponse({"status": "stopping", "mint": mint})


@app.post("/api/live/stop_all")
async def live_stop_all():
    n = 0
    for session in list(_active_live_traders.values()):
        session.stop("manual_stop")
        n += 1
    return JSONResponse({"status": "all_stopped", "count": n})


async def _get_or_create_live_session(
    *,
    real_mint: str,
    private_key: str = "",
    buy_size: Optional[float] = None,
    slippage_bps: int = 1000,
    skip_sim: bool = True,
    engine_params: Optional[dict] = None,
    engine_version: int = 2,
    timeframe: str = "1s",
    token_name: str = "",
    token_symbol: str = "",
    token_info: Optional[dict] = None,
) -> tuple[Optional["_LiveSession"], bool]:
    """Get an existing server-side live session or create a new one.

    Returns (session, created_flag). If no session exists and no valid private
    key is provided or stored in backend memory, returns (None, False).
    ``buy_size`` has NO default: creating a session without an explicit,
    user-supplied size (> 0, sourced from the dashboard input field) is
    refused — a hard-coded size must never be traded.
    """
    async with _live_session_lock:
        session = _active_live_traders.get(real_mint)
        if session is not None and not session.cancelled.is_set():
            return session, False

        # ── Attach-only guard — never spawn a new session at defaults ─────
        # Re-attaching viewers (page reload, tab sleep, /api/live/status
        # re-probe) never pass a private key.  Without this guard, the
        # stored-key fallback below silently CREATED a new session at
        # endpoint defaults whenever the intended session had ended between
        # the status probe and this connect — the "live trades always enter
        # a hard-coded size" bug.  Creating a session therefore requires an
        # explicit key from the caller; a bare attach can only ever attach.
        if not private_key:
            return None, False

        # Buy size must come from the dashboard input field (pushed through
        # the WS query param or the /api/live/buy_size store for autofeed).
        # No fallback default exists or ever may exist here.
        if buy_size is None or buy_size <= 0:
            logger.warning(
                f"[LIVE] Refusing to create session for {real_mint[:8]}… — "
                f"no valid buy_size supplied from the dashboard input field"
            )
            return None, False

        # Fallback to server-side stored private key if none passed in call
        key_str = private_key or getattr(app.state, "lt_private_key", "") or _load_backend_private_key()
        if not key_str:
            return None, False

        try:
            keypair = keypair_from_private_key(key_str)
        except Exception as e:
            logger.warning(f"[LIVE] Invalid private key for session creation: {e}")
            return None, False

        # Keep server-side memory synced with working key
        app.state.lt_private_key = key_str
        app.state.lt_wallet_connected = True

        if token_info is None or not token_name:
            try:
                async with _resolve_sem:
                    real_mint, fetched_info = await asyncio.wait_for(
                        resolve_input(real_mint), timeout=20.0
                    )
                    token_info = token_info or fetched_info
            except Exception:
                pass

        token_name = token_name or (token_info or {}).get("name", "")
        token_symbol = token_symbol or (token_info or {}).get("symbol", "")

        live_trader = LiveTrader(
            token_mint=real_mint,
            keypair=keypair,
            buy_size_sol=buy_size,
            slippage_bps=slippage_bps,
            priority_fee_lamports=100_000,
            engine_kwargs=engine_params or {},
            skip_simulation=skip_sim,
            engine_version=engine_version,
        )
        live_trader.start_watchdog()

        session = _LiveSession(
            real_mint=real_mint,
            token_name=token_name,
            token_symbol=token_symbol,
            timeframe=timeframe,
            trader=live_trader,
            token_info=token_info,
            engine_version=engine_version,
        )

        session.rec_id = data_store.create_recording(
            real_mint, timeframe, token_name, token_symbol
        )
        logger.info(f"[LIVE] Auto-recording candles → recording {session.rec_id}")
        live_trader.set_session_meta(
            recording_id=session.rec_id, token_name=token_name,
            token_symbol=token_symbol, timeframe=timeframe,
        )

        _active_live_traders[real_mint] = session
        session.task = asyncio.create_task(session.run())
        return session, True


# ── AutoFeed state & manager ───────────────────────────────────────────────
# AutoFeed is a *discovery-only* loop that polls gmgn.ai for migrated pump.fun
# tokens (mcap ≥ 15k) and pushes candidate mint addresses to connected Live
# Trading dashboards via /ws/autofeed.  The frontend then opens the SAME
# /ws/live/{mint} WebSocket the manual "Start Trading" button uses — autofeed
# performs no trading itself.

_autofeed: AutoFeed = AutoFeed(AutofeedConfig())
# Per-WS client queue of pending candidates to push
_autofeed_clients: set[asyncio.Queue] = set()


def _autofeed_active_count() -> int:
    """Number of currently-active live traders (used for backpressure)."""
    return len(_active_live_traders)


async def _autofeed_forward(cand: Candidate):
    """Called by AutoFeed for each accepted candidate.
    Broadcasts to /ws/autofeed clients and auto-starts live trader server-side if
    private key is configured."""
    payload = {
        "type": "autofeed_candidate",
        "candidate": cand.to_dict(),
        "timestamp": time.time(),
    }
    n_clients = len(_autofeed_clients)
    pushed = 0
    for q in list(_autofeed_clients):
        try:
            q.put_nowait(payload)
            pushed += 1
        except asyncio.QueueFull:
            pass
    logger.info(f"[AutoFeed] Forward ${cand.symbol} to {pushed}/{n_clients} WS clients")

    # If backend has a private key, auto-spawn live trader session server-side.
    # Buy size comes exclusively from the dashboard input field (pushed via
    # POST /api/live/buy_size).  Without a pushed size we skip the spawn —
    # connected dashboards still create the session themselves through
    # /ws/live/{mint} carrying the field value in the query string.
    pk = getattr(app.state, "lt_private_key", "") or _load_backend_private_key()
    af_buy_size = _get_live_buy_size()
    if pk and af_buy_size is None:
        logger.warning(
            "[AutoFeed] No buy size pushed from the dashboard input field "
            "(POST /api/live/buy_size) — skipping server-side session spawn"
        )
    if pk and af_buy_size is not None:
        try:
            session, created = await _get_or_create_live_session(
                real_mint=cand.mint,
                private_key=pk,
                buy_size=af_buy_size,
                token_name=cand.name or "",
                token_symbol=cand.symbol or "",
                timeframe="1s",
            )
            if created:
                logger.info(f"[AutoFeed] Auto-started server-side session for ${cand.symbol} ({cand.mint[:8]}…)")
                # Tell connected dashboards a session now exists so they can
                # attach a viewer card WITHOUT needing the browser private key.
                # Without this push the UI only discovered sessions by polling
                # /api/live/status (page load / tab switch / WS close), so
                # autofeed sessions stayed invisible while recording candles.
                for q in list(_autofeed_clients):
                    try:
                        q.put_nowait({
                            "type": "session_started",
                            "mint": session.real_mint,
                            "token_name": session.token_name,
                            "token_symbol": session.token_symbol,
                            "timeframe": session.timeframe,
                            "engine_version": session.engine_version,
                            "timestamp": time.time(),
                        })
                    except asyncio.QueueFull:
                        pass
        except Exception as e:
            logger.error(f"[AutoFeed] Error auto-starting session for {cand.mint[:8]}…: {e}")


@app.get("/api/autofeed/status")
async def autofeed_status():
    snap = _autofeed.snapshot()
    snap["clients_connected"] = len(_autofeed_clients)
    # Wallet-gate exposed for the frontend toggle state
    pk = getattr(app.state, "lt_private_key", "") or _load_backend_private_key()
    snap["wallet_connected"] = bool(pk) or getattr(app.state, "lt_wallet_connected", False)
    return JSONResponse(snap)


@app.post("/api/autofeed/config")
async def autofeed_config(body: dict = Body(...)):
    """Hot-update any config keys (e.g. poll_seconds, min_mcap_usd, gmgn_timeframe).
    Does NOT honor `enabled` here — use /api/autofeed/start + /stop for that gate."""
    safe = {k: v for k, v in body.items() if k != "enabled"}
    changed = _autofeed.set_config(safe)
    return JSONResponse({"status": "updated", "changed": changed, "snapshot": _autofeed.snapshot()})


@app.post("/api/autofeed/start")
async def autofeed_start(body: dict = Body(default={})):
    """Turn ON the autofeed. Refuses if no private key is set in the backend."""
    private_key = getattr(app.state, "lt_private_key", "") or _load_backend_private_key()
    if not private_key:
        return JSONResponse(
            {"error": "Cannot start autofeed without a private key set."},
            status_code=400,
        )
    app.state.lt_private_key = private_key
    app.state.lt_wallet_connected = True
    if body:
        _autofeed.set_config({k: v for k, v in body.items() if k != "enabled"})
    _autofeed.config.enabled = True
    _autofeed.start(forward_fn=_autofeed_forward, active_count_fn=_autofeed_active_count)
    return JSONResponse({"status": "started", "snapshot": _autofeed.snapshot()})


@app.post("/api/autofeed/stop")
async def autofeed_stop():
    _autofeed.config.enabled = False
    await _autofeed.stop()
    return JSONResponse({"status": "stopped", "snapshot": _autofeed.snapshot()})


@app.get("/api/live/private_key")
async def live_get_private_key():
    """Get the current wallet connection status and public key."""
    pk = getattr(app.state, "lt_private_key", "") or _load_backend_private_key()
    if pk and not getattr(app.state, "lt_private_key", ""):
        app.state.lt_private_key = pk
        app.state.lt_wallet_connected = True
    pubkey = ""
    if pk:
        try:
            pubkey = str(keypair_from_private_key(pk).pubkey())
        except Exception:
            pass
    return JSONResponse({
        "connected": bool(pk),
        "pubkey": pubkey,
        "autofeed_running": _autofeed.is_running(),
    })


@app.post("/api/live/private_key")
async def live_set_private_key(body: dict = Body(...)):
    """Frontend tells backend it has a private key set (or passes the key to persist)."""
    connected = bool(body.get("connected", False))
    pk = str(body.get("private_key", "")).strip()

    if not connected or (not pk and not getattr(app.state, "lt_private_key", "") and not _load_backend_private_key()):
        app.state.lt_wallet_connected = False
        app.state.lt_private_key = ""
        _clear_backend_private_key()
        _autofeed.config.enabled = False
        await _autofeed.stop()
        return JSONResponse({"connected": False, "autofeed_running": False})

    if pk:
        try:
            kp = keypair_from_private_key(pk)
            app.state.lt_private_key = pk
            app.state.lt_wallet_connected = True
            _save_backend_private_key(pk)
            pubkey = str(kp.pubkey())
        except ValueError as e:
            return JSONResponse({"error": f"Invalid private key: {e}"}, status_code=400)
    else:
        # Re-affirming connected state with key already in memory or disk
        pk = getattr(app.state, "lt_private_key", "") or _load_backend_private_key()
        app.state.lt_private_key = pk
        app.state.lt_wallet_connected = True
        pubkey = str(keypair_from_private_key(pk).pubkey()) if pk else ""

    return JSONResponse({
        "connected": True,
        "pubkey": pubkey,
        "autofeed_running": _autofeed.is_running(),
    })


@app.post("/api/live/buy_size")
async def live_set_buy_size(body: dict = Body(...)):
    """Push the dashboard's Live Trader "Buy Size (SOL)" input value.

    This is the ONLY way the backend learns the live buy size — there is no
    default anywhere.  The value is persisted, used by autofeed's server-side
    session spawns, and hot-applied to every running live session so the
    input field always stays authoritative.
    """
    try:
        size = float(body.get("buy_size"))
    except (TypeError, ValueError):
        return JSONResponse(
            {"error": "buy_size must be a positive number of SOL"}, status_code=400
        )
    if not size > 0:
        return JSONResponse(
            {"error": "buy_size must be > 0"}, status_code=400
        )

    app.state.lt_buy_size = size
    _save_live_buy_size(size)

    # Keep every running session in sync with the field.
    updated = 0
    for session in list(_active_live_traders.values()):
        trader = getattr(session, "trader", None)
        if trader is not None:
            trader.buy_size_sol = size
            updated += 1

    logger.info(f"[LIVE] Buy size set from dashboard input field: {size} SOL "
                f"({updated} active session(s) synced)")
    return JSONResponse({"status": "updated", "buy_size": size, "sessions_synced": updated})


async def _autofeed_ws_handler(websocket: WebSocket):
    """Core autofeed WS logic — callable from both the dedicated route and the
    /ws/{mint} wildcard delegation."""
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _autofeed_clients.add(q)
    logger.info(f"[AutoFeed WS] Client connected (total={len(_autofeed_clients)})")
    try:
        await websocket.send_json({"type": "autofeed_status", "data": _autofeed.snapshot()})
        # Send any already-tracked candidates for instant UI population
        for cand in list(_autofeed._seen.values())[-5:]:
            await websocket.send_json({
                "type": "autofeed_candidate",
                "candidate": cand.to_dict(),
                "timestamp": time.time(),
            })
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                # Heartbeat
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[AutoFeed WS] Error: {e}")
    finally:
        _autofeed_clients.discard(q)
        logger.info(f"[AutoFeed WS] Client disconnected (total={len(_autofeed_clients)})")




@app.websocket("/ws/live/{mint}")
async def live_trading_ws(
    websocket: WebSocket,
    mint: str,
    timeframe: str = Query(default="1s"),
    private_key: str = Query(default=""),
    buy_size: Optional[float] = Query(default=None),
    slippage_bps: int = Query(default=1000),
    skip_sim: bool = Query(default=True),
    params: str = Query(default="{}"),
    engine_version: int = Query(default=2),
):
    """Viewer/control socket for a live session.

    If no session exists for the token, one is created (private_key required)
    and this socket becomes its first viewer.  If a session already exists —
    same tab re-opened, second tab, autofeed re-feed — this socket simply
    attaches to it; no private key is needed to view/control an existing
    session because the trader's keypair already lives server-side.  Closing
    the socket only detaches this viewer; the session keeps trading until it
    is stopped via /api/live/stop or a safety stop fires.
    """
    if timeframe not in TIMEFRAME_SECONDS:
        await websocket.close(code=4000, reason="Unknown timeframe")
        return

    # Resolve the mint first so we can find an existing session regardless of
    # whether the caller passed a mint / pair address / symbol.
    # Fast path: re-attach to an existing session by its exact registry key
    # WITHOUT hitting external resolvers.  The frontend always sends the key
    # from /api/live/status on attach.  Without this, the handshake queued
    # behind _resolve_sem (held by other sessions' warmups for up to 15s each)
    # and could stall for minutes, leaving the UI stuck at
    # "Re-attaching to running session…" with no data.
    existing = _active_live_traders.get(mint)
    if existing is not None and not existing.cancelled.is_set():
        real_mint, token_info = existing.real_mint, existing.token_info
    else:
        try:
            async with _resolve_sem:
                real_mint, token_info = await asyncio.wait_for(
                    resolve_input(mint), timeout=20.0
                )
        except asyncio.TimeoutError:
            logger.warning(f"[LIVE] resolve_input timed out for {mint[:8]} — using raw mint")
            real_mint, token_info = mint, None
        except Exception as e:
            # A flaky resolver (rate limit, network blip) must never kill the
            # socket — that sent the frontend into an infinite re-attach loop
            # ("Re-attaching to running session…" with no live data).  Fall back
            # to the raw mint so an existing session keyed under it still attaches.
            logger.warning(f"[LIVE] resolve_input failed for {mint[:8]} ({e}) — using raw mint")
            real_mint, token_info = mint, None

    try:
        engine_params = json.loads(params)
    except Exception:
        engine_params = {}

    session, created = await _get_or_create_live_session(
        real_mint=real_mint,
        private_key=private_key,
        buy_size=buy_size,
        slippage_bps=slippage_bps,
        skip_sim=skip_sim,
        engine_params=engine_params,
        engine_version=engine_version,
        timeframe=timeframe,
        token_info=token_info,
    )

    if session is None:
        # Creation was refused: no key (attach-only guard) or no valid
        # buy_size from the dashboard input field.  There is no default.
        await websocket.close(
            code=4001,
            reason="Session requires a private key and a valid buy_size "
                   "from the dashboard input field",
        )
        return

    # ── Attach as a viewer ─────────────────────────────────────────────────
    await websocket.accept()
    logger.info(
        f"[LIVE] {'Start' if created else 'Attach'} viewer  mint={real_mint[:8]}…  "
        f"wallet={session.wallet()[:8]}…  tf={session.timeframe}  "
        f"viewers={len(session.subscribers) + 1}"
    )

    q = session.subscribe()

    async def send(obj: dict) -> bool:
        try:
            await websocket.send_json(obj)
            return True
        except Exception:
            return False

    try:
        if session.cancelled.is_set():
            # Session is already tearing down — tell the viewer instead of
            # leaving it parked on a dead broadcast queue.
            await send({"type": "session_ended", "reason": session.stop_reason or "session_ended"})
            return

        if session.token_info:
            await send({"type": "token_info", "data": session.token_info})
        await send({
            "type": "session_info",
            "real_mint": real_mint,
            "token_name": session.token_name,
            "token_symbol": session.token_symbol,
            "timeframe": session.timeframe,
            "engine_version": session.engine_version,
            "recording_id": session.rec_id,
            "created": created,
        })

        # ── Historical candles for the chart ─────────────────────────────
        if created:
            # The runner is warming up; wait so this viewer gets the same
            # warmup strategy results the old single-tab flow provided.
            try:
                await asyncio.wait_for(session.warmed.wait(), timeout=45.0)
            except asyncio.TimeoutError:
                pass
            candles = session.warmup_candles
            strategy = session.warmup_strategy
        else:
            # Re-attach: replay everything recorded so far.  The engine has
            # already processed these candles — the chart just needs the data.
            try:
                candles = data_store.get_recording_candles(session.rec_id) if session.rec_id else []
            except Exception:
                candles = []
            strategy = [session.last_strategy_result] if session.last_strategy_result else []
        if candles:
            await send({"type": "historical", "candles": candles, "strategy": strategy})
            logger.info(f"[LIVE] Sent {len(candles)} historical candles for {real_mint[:8]}")

        async def viewer():
            """Drain the session broadcast queue → this tab."""
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Heartbeat
                    if not await send({"type": "ping"}):
                        return
                    continue
                try:
                    if isinstance(msg, str):
                        await websocket.send_text(msg)
                    else:
                        await websocket.send_json(msg)
                except Exception:
                    return

        async def listen():
            """Control channel from the tab (config updates, manual trades).
            A disconnect here detaches only this viewer — the session itself
            keeps running server-side."""
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except Exception:
                    continue
                msg_type = msg.get("type", "")

                if msg_type == "update_config":
                    if "buy_size" in msg:
                        # Only a valid dashboard-supplied size is applied —
                        # never a fallback default.
                        try:
                            v = float(msg["buy_size"])
                        except (TypeError, ValueError):
                            v = 0.0
                        if v > 0:
                            session.trader.buy_size_sol = v
                    if "slippage_bps" in msg:
                        session.trader.slippage_bps = int(msg["slippage_bps"])
                    # priority_fee is fixed at 100_000 micro-lamports (0.0001 SOL) — ignored
                elif msg_type == "manual_trade":
                    action = msg.get("action")
                    if action == "buy":
                        await session.trader.force_buy()
                    elif action == "sell":
                        await session.trader.force_sell()
                elif msg_type == "pong":
                    pass

        v_task = asyncio.ensure_future(viewer())
        l_task = asyncio.ensure_future(listen())
        try:
            await asyncio.gather(v_task, l_task)
        except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
            pass
        finally:
            for t in (v_task, l_task):
                if not t.done():
                    t.cancel()
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    finally:
        # Detach only — never terminates the session.
        session.unsubscribe(q)
        logger.info(
            f"[LIVE] Detach viewer  mint={real_mint[:8]}…  "
            f"remaining={len(session.subscribers)}"
        )


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info", access_log=False)

