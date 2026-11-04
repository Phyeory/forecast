"""New-pairs discovery feed routes and session viewer sockets.

`router` holds the REST group; `ws_router` holds the WebSockets, which the
app mounts after the newpairs recordings router to keep the original route
registration order.
"""
import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Body, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from candle_aggregator import TIMEFRAME_SECONDS
from pumpfun_client import resolve_input

import newpairs_store
from app.deps import (
    _active_newpair_sessions,
    _completed_newpair_sessions,
    _newpairs_feed,
    _newpairs_clients,
    _resolve_sem,
)

logger = logging.getLogger("pump-chart")

router = APIRouter()


@router.get("/api/newpairs/status")
async def newpairs_status():
    snap = _newpairs_feed.snapshot()
    snap["clients_connected"] = len(_newpairs_clients)
    snap["active_sessions"] = len(_active_newpair_sessions)
    snap["sessions"] = [{
        "mint": mint,
        "token_name": s.token_name,
        "token_symbol": s.token_symbol,
        "timeframe": s.timeframe,
        "status": "recording" if not s.cancelled.is_set() else "stopping",
        "recording_id": s.rec_id,
        "viewers": len(s.subscribers),
        "trade_count": s.trade_count,
        "last_price": s.last_price,
        "first_price": s.first_price,
        "change_pct": (
            ((s.last_price - s.first_price) / s.first_price * 100.0)
            if s.first_price > 0 and s.last_price > 0 else 0.0
        ),
        "last_motion_ts": s._last_motion_ts,
    } for mint, s in _active_newpair_sessions.items()]
    snap["completed"] = _completed_newpair_sessions[-50:]
    return JSONResponse(snap)


@router.post("/api/newpairs/config")
async def newpairs_config(body: dict = Body(...)):
    """Hot-update any NewPairs config keys.

    `enabled` is NOT honored here: use /api/newpairs/start + /stop for that gate.
    """
    safe = {k: v for k, v in body.items() if k != "enabled"}
    changed = _newpairs_feed.set_config(safe)
    return JSONResponse({"status": "updated", "changed": changed, "snapshot": _newpairs_feed.snapshot()})


@router.post("/api/newpairs/start")
async def newpairs_start(body: dict = Body(default={})):
    """Turn ON the new-pairs discovery feed.

    No wallet key is required: this tab never trades, it only records
    newborn price action.
    """
    if body:
        _newpairs_feed.set_config({k: v for k, v in body.items() if k != "enabled"})
    _newpairs_feed.config.enabled = True
    # Late import: the feed callbacks live in main.py alongside the session
    # factory in this seam; importing at module level would be circular.
    from main import _newpairs_notify, _newpairs_spawn, _newpairs_active_count

    _newpairs_feed.start(notify_fn=_newpairs_notify, forward_fn=_newpairs_spawn,
                         active_count_fn=_newpairs_active_count)
    return JSONResponse({"status": "started", "snapshot": _newpairs_feed.snapshot()})


@router.post("/api/newpairs/stop")
async def newpairs_stop():
    """Stop the discovery feed.  Already-recording sessions keep running until
    their own no-motion stop / manual stop — by design, they are decoupled."""
    _newpairs_feed.config.enabled = False
    await _newpairs_feed.stop()
    return JSONResponse({"status": "stopped", "snapshot": _newpairs_feed.snapshot()})


@router.post("/api/newpairs/session/start")
async def newpairs_session_start(body: dict = Body(...)):
    """Manually start a recording session for a specific newborn token
    (no feed required).  Same filters as feed-spawned sessions: per-mint
    cooldown + max-concurrent backpressure, no engine, no-motion-only stop."""
    mint = (body.get("mint") or "").strip()
    if not mint:
        return JSONResponse({"error": "mint is required"}, status_code=400)
    timeframe = body.get("timeframe") or _newpairs_feed.config.session_timeframe
    if timeframe not in TIMEFRAME_SECONDS:
        return JSONResponse({"error": f"Unknown timeframe: {timeframe}"}, status_code=400)
    if not _newpairs_feed.can_spawn_session(mint):
        return JSONResponse({"error": "Session cap reached or mint in cooldown"}, status_code=409)

    # Resolve token info best-effort (newborns often have none).
    try:
        async with _resolve_sem:
            real_mint, token_info = await asyncio.wait_for(
                resolve_input(mint), timeout=20.0
            )
    except Exception:
        real_mint, token_info = mint, None
    token_name = (token_info or {}).get("name", "")
    token_symbol = (token_info or {}).get("symbol", "")

    from main import _get_or_create_newpair_session  # late import: lives in main.py

    session, created = await _get_or_create_newpair_session(
        real_mint=real_mint,
        timeframe=timeframe,
        token_name=token_name,
        token_symbol=token_symbol,
        token_info=token_info,
    )
    if session is None:
        return JSONResponse({"error": "Failed to create session"}, status_code=500)
    if created:
        _newpairs_feed.note_spawned(real_mint)
    return JSONResponse({
        "status": "recording" if created else "already_recording",
        "mint": real_mint,
        "recording_id": session.rec_id,
        "timeframe": timeframe,
    })


@router.post("/api/newpairs/session/stop")
async def newpairs_session_stop(body: dict = Body(...)):
    mint = (body.get("mint") or "").strip()
    session = _active_newpair_sessions.get(mint)
    if not mint or session is None:
        return JSONResponse({"error": "No active new-pair session for this mint"}, status_code=404)
    session.stop("manual_stop")
    return JSONResponse({"status": "stopping", "mint": mint})


@router.post("/api/newpairs/session/stop_all")
async def newpairs_session_stop_all():
    n = 0
    for session in list(_active_newpair_sessions.values()):
        session.stop("manual_stop")
        n += 1
    return JSONResponse({"status": "all_stopped", "count": n})


ws_router = APIRouter()


@ws_router.websocket("/ws/newpairs/feed")
async def newpairs_feed_ws(websocket: WebSocket):
    """Status/candidate stream for the New Pairs tab: snapshot on connect,
    then every birth event + session-started push."""
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _newpairs_clients.add(q)
    logger.info(f"[NewPairs WS] Feed client connected (total={len(_newpairs_clients)})")
    try:
        await websocket.send_json({"type": "newpairs_status", "data": _newpairs_feed.snapshot()})
        for cand in list(_newpairs_feed._seen.values())[-5:]:
            await websocket.send_json({
                "type": "newpair_candidate",
                "candidate": cand.to_dict(),
                "timestamp": time.time(),
            })
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[NewPairs WS] Feed error: {e}")
    finally:
        _newpairs_clients.discard(q)
        logger.info(f"[NewPairs WS] Feed client disconnected (total={len(_newpairs_clients)})")


@ws_router.websocket("/ws/newpairs/{mint}")
async def newpairs_session_ws(
    websocket: WebSocket,
    mint: str,
    timeframe: str = Query(default="1s"),
):
    """Viewer socket for a new-pair recording session.

    The /ws/live/{mint} viewer contract minus trading: attach to the running
    session (creating one if none exists; no key, no buy size, nothing to
    trade), then replay recorded candles and stream live ones.
    """
    if mint == "feed":
        await newpairs_feed_ws(websocket)
        return
    if timeframe not in TIMEFRAME_SECONDS:
        await websocket.close(code=4000, reason="Unknown timeframe")
        return

    # Fast path: attach to a running session without any resolver calls.
    session = _active_newpair_sessions.get(mint)
    created = False
    if session is None:
        # Resolve best-effort, then create (manual viewer-initiated start).
        try:
            async with _resolve_sem:
                real_mint, token_info = await asyncio.wait_for(
                    resolve_input(mint), timeout=20.0
                )
        except Exception:
            real_mint, token_info = mint, None
        token_name = (token_info or {}).get("name", "")
        token_symbol = (token_info or {}).get("symbol", "")
        if not _newpairs_feed.can_spawn_session(real_mint):
            await websocket.close(
                code=4009,
                reason="Session cap reached or mint in cooldown",
            )
            return
        from main import _get_or_create_newpair_session  # late import: lives in main.py

        session, created = await _get_or_create_newpair_session(
            real_mint=real_mint,
            timeframe=timeframe,
            token_name=token_name,
            token_symbol=token_symbol,
            token_info=token_info,
        )
        if created:
            _newpairs_feed.note_spawned(real_mint)

    if session is None:
        await websocket.close(code=4001, reason="Failed to create session")
        return

    await websocket.accept()
    logger.info(
        f"[NEWPAIR] {'Start' if created else 'Attach'} viewer  mint={session.real_mint[:8]}…  "
        f"tf={session.timeframe}  viewers={len(session.subscribers) + 1}"
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
            await send({"type": "session_ended", "reason": session.stop_reason or "session_ended"})
            return

        if session.token_info:
            await send({"type": "token_info", "data": session.token_info})
        await send({
            "type": "session_info",
            "real_mint": session.real_mint,
            "token_name": session.token_name,
            "token_symbol": session.token_symbol,
            "timeframe": session.timeframe,
            "recording_id": session.rec_id,
            "no_motion_stop_seconds": session.no_motion_stop_seconds,
            "created": created,
        })

        # Replay everything recorded so far (engine-free: no strategy array).
        try:
            candles = newpairs_store.get_recording_candles(session.rec_id) if session.rec_id else []
        except Exception:
            candles = []
        if candles:
            await send({"type": "historical", "candles": candles, "strategy": []})
            logger.info(f"[NEWPAIR] Sent {len(candles)} historical candles for {session.real_mint[:8]}")

        async def viewer():
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
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
            # Control channel (detach-only semantics; pong handling).  There is
            # deliberately NO manual_trade — nothing can be traded here.
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except Exception:
                    continue
                # No control messages modify a recording session; only pongs.

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
        # Detach only: never terminates the session.
        session.unsubscribe(q)
        logger.info(
            f"[NEWPAIR] Detach viewer  mint={session.real_mint[:8]}…  "
            f"remaining={len(session.subscribers)}"
        )
