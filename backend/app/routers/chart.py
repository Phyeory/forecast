"""Chart routes: index page, static JS, token lookup and the /ws/{mint}
candle stream.

`ws_router` is mounted separately so the wildcard socket keeps its original
position in the app's route sequence.
"""
import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from candle_aggregator import CandleAggregator, TIMEFRAME_SECONDS
from forward_tester import ForwardTester
from pumpfun_client import (
    DexScreenerPollClient,
    PumpFunWSClient,
    PumpSwapRPCClient,
    get_historical_candles,
    get_token_info,
    resolve_input,
)

from app.deps import FRONTEND_DIR, _resolve_sem

logger = logging.getLogger("pump-chart")

router = APIRouter()


@router.get("/")
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

@router.get("/static/js/{filename}")
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


@router.get("/api/token/{mint}")
async def token_info_endpoint(mint: str):
    _, info = await resolve_input(mint)
    return JSONResponse(info) if info else JSONResponse({"error": "not found"}, status_code=404)



ws_router = APIRouter()


@ws_router.websocket("/ws/{mint}")
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
    # FastAPI matches /ws/{mint} before /ws/live because the wildcard is
    # declared first. Must accept() before close() to avoid a 403.
    # For "autofeed", delegate directly — FastAPI WS routing doesn't fall
    # through, so the dedicated /ws/autofeed endpoint is unreachable.
    if mint == "autofeed":
        from main import _autofeed_ws_handler  # late import: defined in main.py
        await _autofeed_ws_handler(websocket)
        return
    _RESERVED = {"live", "newpairs"}
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
