"""Recorder control routes and the recordings REST API.

The price store (/api/recordings) and the newpairs store
(/api/newpairs/recordings) expose endpoint-for-endpoint mirrors; both groups
are built by one router factory parameterised by store and cleanup threshold.
"""
import asyncio
import logging
import time
from typing import Optional

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

from candle_aggregator import CandleAggregator, TIMEFRAME_SECONDS
from holder_flow import get_shared_monitor
from pumpfun_client import (
    DexScreenerPollClient,
    PumpFunWSClient,
    PumpSwapRPCClient,
    get_historical_candles,
    resolve_input,
)

import data_store
import newpairs_store
from app.deps import _resolve_sem

logger = logging.getLogger("pump-chart")

router = APIRouter()


_active_recorders: dict[int, dict] = {}


@router.post("/api/recorder/start")
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

        # Shared process-wide monitor: one poller however many recordings are
        # active (per-process rate limits).
        holder_monitor = get_shared_monitor()
        await holder_monitor.start()
        # pool_address lets the realtime on-chain watcher resolve the dev
        # wallet from the PumpSwap pool account, no GMGN involved.
        _hf_pool = (token_info or {}).get("pair_address") \
            if live_source == "solana_rpc" else None
        holder_monitor.watch_token(real_mint, recording_id=rec_id,
                                   pool_address=_hf_pool)
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
                    # Feed the realtime holder-flow watcher: whale sells are
                    # classified off this stream, dev trades via its ATA
                    # subscription; observe_trade never blocks.
                    if holder_monitor is not None:
                        holder_monitor.observe_trade(real_mint, trade)
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


@router.post("/api/recorder/stop")
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


@router.get("/api/recorder/status")
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
                # No candles at all: treat as stale
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


_PRICE_HANDLER_NAMES = {
    "list": "list_recordings_endpoint",
    "get": "get_recording_endpoint",
    "candles": "get_recording_candles_endpoint",
    "delete": "delete_recording_endpoint",
    "cleanup": "cleanup_recordings_endpoint",
}
_NEWPAIRS_HANDLER_NAMES = {
    "list": "newpairs_list_recordings",
    "get": "newpairs_get_recording",
    "candles": "newpairs_get_recording_candles",
    "delete": "newpairs_delete_recording",
    "cleanup": "newpairs_cleanup_recordings",
}
# Per-group docstrings: FastAPI turns each handler docstring into the OpenAPI
# operation description, so the originals are preserved verbatim.
_NEWPAIRS_HANDLER_DOCS = {
    "cleanup": """Delete new-pair recordings with fewer than min_candles candles.
    Newborns die fast — the default cleanup threshold is lower than the
    migrated-token store's 100.""",
}


def recordings_router(
    prefix: str,
    store,
    default_min_candles: int,
    *,
    include_finish_stale: bool = False,
    handler_names: Optional[dict] = None,
    handler_docs: Optional[dict] = None,
):
    """Build the recordings REST routes for one store.

    Handler names and docstrings are re-pointed per group before registration
    so route names and the OpenAPI document match the pre-split app exactly.
    """
    names = dict(_PRICE_HANDLER_NAMES)
    names.update(handler_names or {})
    docs = dict(handler_docs or {})

    def _named(fn, role):
        fn.__name__ = names[role]
        fn.__qualname__ = names[role]
        fn.__doc__ = docs.get(role)
        return fn

    async def list_recordings():
        return JSONResponse(store.list_recordings())

    async def get_recording(recording_id: int):
        rec = store.get_recording(recording_id)
        if not rec:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(rec)

    async def get_recording_candles(recording_id: int):
        candles = store.get_recording_candles(recording_id)
        return JSONResponse(candles)

    async def delete_recording(recording_id: int):
        store.delete_recording(recording_id)
        return JSONResponse({"status": "deleted"})

    async def cleanup_recordings(min_candles: int = Query(default=default_min_candles)):
        deleted_count = store.cleanup_small_recordings(min_candles=min_candles)
        return JSONResponse({"status": "cleaned", "deleted_count": deleted_count, "min_candles": min_candles})

    router = APIRouter()
    router.add_api_route(prefix, _named(list_recordings, "list"), methods=["GET"])
    router.add_api_route(prefix + "/{recording_id}", _named(get_recording, "get"), methods=["GET"])
    router.add_api_route(prefix + "/{recording_id}/candles", _named(get_recording_candles, "candles"), methods=["GET"])
    if include_finish_stale:
        # Price-store only: it consults the active recorder registry, which
        # has no newpairs counterpart.  DELETE registers first: the original
        # stacked decorators applied bottom-up.
        router.add_api_route(prefix + "/finish_stale", finish_stale_recordings, methods=["DELETE"])
        router.add_api_route(prefix + "/finish_stale", finish_stale_recordings, methods=["POST"])
    router.add_api_route(prefix + "/{recording_id}", _named(delete_recording, "delete"), methods=["DELETE"])
    router.add_api_route(prefix + "/cleanup", _named(cleanup_recordings, "cleanup"), methods=["DELETE"])
    router.add_api_route(prefix + "/cleanup", _named(cleanup_recordings, "cleanup"), methods=["POST"])
    return router


router.include_router(recordings_router("/api/recordings", data_store, 100, include_finish_stale=True))

newpairs_recordings_router = recordings_router(
    "/api/newpairs/recordings", newpairs_store, 30,
    handler_names=_NEWPAIRS_HANDLER_NAMES, handler_docs=_NEWPAIRS_HANDLER_DOCS,
)
