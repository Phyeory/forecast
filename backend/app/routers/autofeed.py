"""AutoFeed discovery-feed routes."""
import logging

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from app.deps import _autofeed, _autofeed_clients, _load_backend_private_key

logger = logging.getLogger("pump-chart")

router = APIRouter()


@router.get("/api/autofeed/status")
async def autofeed_status(request: Request):
    snap = _autofeed.snapshot()
    snap["clients_connected"] = len(_autofeed_clients)
    # Wallet-gate exposed for the frontend toggle state
    pk = getattr(request.app.state, "lt_private_key", "") or _load_backend_private_key()
    snap["wallet_connected"] = bool(pk) or getattr(request.app.state, "lt_wallet_connected", False)
    return JSONResponse(snap)


@router.post("/api/autofeed/config")
async def autofeed_config(body: dict = Body(...)):
    """Hot-update config keys (e.g. poll_seconds, min_mcap_usd, gmgn_timeframe).

    Ignores `enabled`: that gate belongs to /api/autofeed/start and /stop.
    """
    safe = {k: v for k, v in body.items() if k != "enabled"}
    changed = _autofeed.set_config(safe)
    return JSONResponse({"status": "updated", "changed": changed, "snapshot": _autofeed.snapshot()})


@router.post("/api/autofeed/start")
async def autofeed_start(request: Request, body: dict = Body(default={})):
    """Turn ON the autofeed. Refuses if no private key is set in the backend."""
    private_key = getattr(request.app.state, "lt_private_key", "") or _load_backend_private_key()
    if not private_key:
        return JSONResponse(
            {"error": "Cannot start autofeed without a private key set."},
            status_code=400,
        )
    request.app.state.lt_private_key = private_key
    request.app.state.lt_wallet_connected = True
    if body:
        _autofeed.set_config({k: v for k, v in body.items() if k != "enabled"})
    _autofeed.config.enabled = True
    # Late import: the forward callback lives in main.py alongside the live
    # session factory in this seam; importing at module level would be circular.
    from main import _autofeed_forward, _autofeed_active_count

    _autofeed.start(forward_fn=_autofeed_forward, active_count_fn=_autofeed_active_count)
    return JSONResponse({"status": "started", "snapshot": _autofeed.snapshot()})


@router.post("/api/autofeed/stop")
async def autofeed_stop():
    _autofeed.config.enabled = False
    await _autofeed.stop()
    return JSONResponse({"status": "stopped", "snapshot": _autofeed.snapshot()})


@router.post("/api/autofeed/tune_now")
async def autofeed_tune_now():
    """Force an immediate market-conditions refresh and gate recompute.

    Works while running or idle; the effective gates apply on the next poll.
    """
    result = await _autofeed.tune_now()
    return JSONResponse({"status": "ok", **result})
