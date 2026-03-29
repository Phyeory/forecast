import asyncio
import json
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from candle_aggregator import CandleAggregator, TIMEFRAME_SECONDS
from pumpfun_client import DexScreenerPollClient, PumpFunWSClient, PumpSwapRPCClient, get_historical_candles, get_token_info, resolve_input, SUB_MINUTE_TFS
from forward_tester import ForwardTester
from live_trader import LiveTrader
import data_store
from backtester import run_backtest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pump-chart")

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="pump-chart")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index():
    p = FRONTEND_DIR / "index.html"
    return FileResponse(str(p)) if p.exists() else JSONResponse({"status": "ok"})


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

    # Resolve token info
    real_mint, token_info = await resolve_input(mint)
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

        # Seed with historical candles
        hist = await get_historical_candles(real_mint, timeframe)
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
                candle_dict = candle.to_dict()
                ct = candle_dict["time"]
                if ct != last_candle_time or is_new:
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
    try:
        result = run_backtest(
            recording_id=int(recording_id),
            engine_params=engine_params,
            buy_size_sol=body.get("buy_size_sol", 0.1),
            priority_fee=body.get("priority_fee", 0.0001),
            bribe_fee=body.get("bribe_fee", 0.00001),
            slippage_pct=body.get("slippage_pct", 1.0),
            starting_balance=body.get("starting_balance", 1.0),
        )
        return JSONResponse(result)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.error(f"Backtest error: {e}")
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


@app.websocket("/ws/{mint}")
async def chart_ws(
    websocket: WebSocket,
    mint: str,
    timeframe: str = Query(default="1m"),
    params: str = Query(default="{}"),
):
    if timeframe not in TIMEFRAME_SECONDS:
        await websocket.close(code=4000, reason="Unknown timeframe")
        return

    await websocket.accept()
    logger.info(f"Connect  input={mint[:8]}…  tf={timeframe}")

    # Resolve the user input (token mint or pair address) to the actual token mint
    real_mint, token_info = await resolve_input(mint)
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
        buy_size_sol=0.1,
        priority_fee=0.0001,
        bribe_fee=0.00001,
        slippage_pct=1.0,
        engine_kwargs=engine_params,
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

    hist = await get_historical_candles(real_mint, timeframe)
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

    try:
        await asyncio.gather(stream_live(), keepalive(), listen())
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


@app.websocket("/ws/live/{mint}")
async def live_trading_ws(
    websocket: WebSocket,
    mint: str,
    timeframe: str = Query(default="1m"),
    wallet: str = Query(default=""),
    buy_size: float = Query(default=0.1),
    slippage_bps: int = Query(default=1000),
    priority_fee: int = Query(default=100000),
    params: str = Query(default="{}"),
):
    if timeframe not in TIMEFRAME_SECONDS:
        await websocket.close(code=4000, reason="Unknown timeframe")
        return
    if not wallet:
        await websocket.close(code=4001, reason="Wallet pubkey required")
        return

    await websocket.accept()
    logger.info(f"[LIVE] Connect  mint={mint[:8]}…  wallet={wallet[:8]}…  tf={timeframe}")

    real_mint, token_info = await resolve_input(mint)
    token_name = (token_info or {}).get("name", "")
    token_symbol = (token_info or {}).get("symbol", "")

    try:
        engine_params = json.loads(params)
    except Exception:
        engine_params = {}

    live_trader = LiveTrader(
        token_mint=real_mint,
        wallet_pubkey=wallet,
        buy_size_sol=buy_size,
        slippage_bps=slippage_bps,
        priority_fee_lamports=priority_fee,
        engine_kwargs=engine_params,
    )

    cancelled = asyncio.Event()
    _active_live_traders[real_mint] = {
        "trader": live_trader,
        "token_name": token_name,
        "token_symbol": token_symbol,
        "timeframe": timeframe,
        "cancelled": cancelled,
        "wallet": wallet,
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

    async def send(obj: dict) -> bool:
        try:
            await websocket.send_json(obj)
            return True
        except Exception:
            return False

    if token_info:
        await send({"type": "token_info", "data": token_info})

    # Send historical candles + run through strategy (warm up indicators)
    hist = await get_historical_candles(real_mint, timeframe)
    if hist:
        strategy_results = []
        for candle in hist:
            result = live_trader.update(
                time_val=int(candle["time"]),
                o=candle["open"], h=candle["high"],
                l=candle["low"], c=candle["close"],
                volume=candle.get("volume", 0),
            )
            # Strip swap_requests from historical warmup
            if result.get("live_trade", {}).get("swap_request"):
                result["live_trade"]["swap_request"] = None
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
            if last_sent_price is not None and current_price == last_sent_price and not is_new:
                continue
            last_sent_price = current_price

            strategy_result = live_trader.update(
                time_val=candle_dict["time"],
                o=candle_dict["open"], h=candle_dict["high"],
                l=candle_dict["low"], c=candle_dict["close"],
                volume=candle_dict.get("volume", 0),
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

    async def listen():
        """Listen for messages from frontend (tx confirmations, config updates)."""
        try:
            while not cancelled.is_set():
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except Exception:
                    continue
                msg_type = msg.get("type", "")
                if msg_type == "tx_confirmed":
                    action = msg.get("action", "")
                    if action == "buy":
                        live_trader.confirm_buy(
                            tx_hash=msg.get("tx_hash", ""),
                            tokens_received=float(msg.get("tokens_received", 0)),
                            actual_price=float(msg.get("actual_price", 0)),
                        )
                    elif action == "sell":
                        live_trader.confirm_sell(
                            tx_hash=msg.get("tx_hash", ""),
                            sol_received=float(msg.get("sol_received", 0)),
                            actual_price=float(msg.get("actual_price", 0)),
                        )
                elif msg_type == "tx_failed":
                    live_trader.confirm_failed(
                        action=msg.get("action", ""),
                        error=msg.get("error", "unknown"),
                    )
                elif msg_type == "update_config":
                    if "buy_size" in msg:
                        live_trader.buy_size_sol = float(msg["buy_size"])
                    if "slippage_bps" in msg:
                        live_trader.slippage_bps = int(msg["slippage_bps"])
                    if "priority_fee" in msg:
                        live_trader.priority_fee_lamports = int(msg["priority_fee"])
                elif msg_type == "pong":
                    pass
        except WebSocketDisconnect:
            pass
        finally:
            cancelled.set()
            ws_client.stop()

    try:
        await asyncio.gather(stream_live(), keepalive(), listen())
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        cancelled.set()
        ws_client.stop()
        if real_mint in _active_live_traders:
            del _active_live_traders[real_mint]
        logger.info(f"[LIVE] Disconnect  mint={real_mint[:8]}…")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")

