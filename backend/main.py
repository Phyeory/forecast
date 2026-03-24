import asyncio
import json
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from candle_aggregator import CandleAggregator, TIMEFRAME_SECONDS
from pumpfun_client import DexScreenerPollClient, PumpFunWSClient, PumpSwapRPCClient, get_historical_candles, get_token_info, resolve_input, SUB_MINUTE_TFS
from forward_tester import ForwardTester

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


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info")
