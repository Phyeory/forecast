"""Backtest API: single runs, batches and result management."""
import asyncio
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from backtester import run_backtest, run_backtest_batch

import data_store

logger = logging.getLogger("pump-chart")

router = APIRouter()


@router.post("/api/backtest")
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
            priority_fee=0.0001,
            bribe_fee=0.0,
            slippage_pct=body.get("slippage_pct", 1.0),
            starting_balance=body.get("starting_balance", 1.0),
            engine_version=engine_version,
            # "instant" fills at the signal-instant price (live parity);
            # "legacy" fills mid-next-bar.  Latency overlays defer fills to
            # t_signal + latency on the recorded price path.
            exec_model=body.get("exec_model", "instant"),
            entry_latency_seconds=float(body.get("entry_latency_seconds", 0.0)),
            exit_latency_seconds=float(body.get("exit_latency_seconds", 0.0)),
        )
        return JSONResponse(result)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.error(f"Backtest error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/backtest/batch")
async def run_backtest_batch_endpoint(body: dict = Body(default={})):
    """Run backtests on ALL completed recordings in parallel.

    Optional filters in the request body:
      - ``recording_ids``: explicit list of recording IDs to run.
      - ``last_night``: if true, restrict to recordings started between
        10:00 PM local time of the previous day and 12:00 PM (noon) local
        time of the current day.
      - ``last_days``: if > 0, restrict to recordings started within the
        last N days (rolling window ending at the moment the batch runs).
    """
    engine_params = body.get("engine_params", {})
    engine_version = int(body.get("engine_version", 1))
    batch_id = str(int(time.time() * 1000))
    recording_ids = body.get("recording_ids")
    if recording_ids is not None:
        recording_ids = [int(r) for r in recording_ids]
    last_night = bool(body.get("last_night", False))
    last_12h = bool(body.get("last_12h", False))
    last_days = float(body.get("last_days") or 0)
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
            last_days=last_days,
            exec_model=body.get("exec_model", "instant"),
            entry_latency_seconds=float(body.get("entry_latency_seconds", 0.0)),
            exit_latency_seconds=float(body.get("exit_latency_seconds", 0.0)),
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


@router.get("/api/backtests")
async def list_backtests_endpoint(market_type: Optional[str] = None):
    rows = data_store.list_backtests()
    if market_type:
        rows = [r for r in rows if (r.get("market_type") or "spot") == market_type]
    return JSONResponse(rows)


@router.get("/api/backtests/{backtest_id}")
async def get_backtest_endpoint(backtest_id: int):
    bt = data_store.get_backtest(backtest_id)
    if not bt:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(bt)


@router.delete("/api/backtests/{backtest_id}")
async def delete_backtest_endpoint(backtest_id: int):
    data_store.delete_backtest(backtest_id)
    return JSONResponse({"status": "deleted"})


@router.delete("/api/backtests")
async def delete_all_backtests_endpoint():
    data_store.delete_all_backtests()
    return JSONResponse({"status": "deleted_all"})


@router.delete("/api/backtests/batch/{batch_id}")
async def delete_backtest_batch_endpoint(batch_id: str):
    data_store.delete_batch(batch_id)
    return JSONResponse({"status": "deleted"})
