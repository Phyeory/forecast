"""
SniperRouter — FastAPI router exposing REST + WebSocket endpoints for the sniper.

Mounts at prefix /sniper on the existing FastAPI app.

REST:
  GET  /sniper/status              — Current engine state
  POST /sniper/mode                — Switch forward_test / live
  GET  /sniper/forward-test/trades — Paginated trade records
  GET  /sniper/forward-test/stats  — Aggregate metrics
  GET  /sniper/watching            — Currently watched tokens

WebSocket:
  WS   /ws/sniper                  — Live event stream
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Body
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# The SniperEngine instance is attached at startup via app.state.sniper
# These endpoints access it via the request's app reference.

def _get_sniper(request):
    """Helper to get sniper engine from app state."""
    return getattr(request.app.state, "sniper", None)


# ── REST Endpoints ───────────────────────────────────────────────────────

@router.get("/api/sniper/status")
async def get_sniper_status():
    """Return current SniperEngine state."""
    from sniper.sniper_engine import SniperEngine
    engine: Optional[SniperEngine] = _sniper_engine
    if not engine:
        return JSONResponse({"error": "Sniper not initialized"}, status_code=503)
    
    status = engine.get_status()
    status["forward_test_stats"] = engine.forward_tester.get_stats()
    return JSONResponse(status)


@router.post("/api/sniper/mode")
async def set_sniper_mode(body: dict = Body(...)):
    """Switch between paper trading and live execution."""
    engine = _sniper_engine
    if not engine:
        return JSONResponse({"error": "Sniper not initialized"}, status_code=503)
    
    mode = body.get("mode", "")
    if mode not in ("forward_test", "live"):
        return JSONResponse({"error": "mode must be 'forward_test' or 'live'"}, status_code=400)
    
    result = engine.set_mode(mode)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


@router.post("/api/sniper/start")
async def start_sniper():
    """Start the sniper engine."""
    engine = _sniper_engine
    if not engine:
        return JSONResponse({"error": "Sniper not initialized"}, status_code=503)
    
    await engine.start()
    return JSONResponse({"status": "started", "mode": engine.config.mode})


@router.post("/api/sniper/stop")
async def stop_sniper():
    """Stop the sniper engine."""
    engine = _sniper_engine
    if not engine:
        return JSONResponse({"error": "Sniper not initialized"}, status_code=503)
    
    await engine.stop()
    return JSONResponse({"status": "stopped"})


@router.get("/api/sniper/forward-test/trades")
async def get_forward_test_trades(limit: int = Query(default=50), offset: int = Query(default=0)):
    """Return paginated forward test trade records."""
    engine = _sniper_engine
    if not engine:
        return JSONResponse({"error": "Sniper not initialized"}, status_code=503)
    
    trades = engine.forward_tester.get_recent_trades(limit=limit, offset=offset)
    total = engine.forward_tester.get_total_trade_count()
    return JSONResponse({"trades": trades, "total": total, "limit": limit, "offset": offset})


@router.get("/api/sniper/forward-test/stats")
async def get_forward_test_stats():
    """Return aggregate forward test statistics."""
    engine = _sniper_engine
    if not engine:
        return JSONResponse({"error": "Sniper not initialized"}, status_code=503)
    
    stats = engine.forward_tester.get_stats()
    return JSONResponse(stats)


@router.get("/api/sniper/watching")
async def get_watching():
    """Return list of tokens currently being watched."""
    engine = _sniper_engine
    if not engine:
        return JSONResponse({"error": "Sniper not initialized"}, status_code=503)
    
    return JSONResponse(engine.get_watching_list())


# ── WebSocket Endpoint ───────────────────────────────────────────────────

# Per-client queues: each WebSocket connection gets its own asyncio.Queue so
# that every client independently receives all events (no racing on a shared queue).
_ws_client_queues: dict[WebSocket, asyncio.Queue] = {}

# Module-level dispatcher task handle
_dispatcher_task: asyncio.Task | None = None


async def _dispatcher_loop(engine):
    """
    Single background task that drains engine.event_queue and fans each event
    out to every connected client's individual queue.
    """
    while True:
        try:
            event = await asyncio.wait_for(engine.event_queue.get(), timeout=30.0)
        except asyncio.TimeoutError:
            # Send keepalive ping to all clients
            for ws, q in list(_ws_client_queues.items()):
                try:
                    q.put_nowait({"type": "ping"})
                except asyncio.QueueFull:
                    pass
            continue
        except asyncio.CancelledError:
            return
        except Exception:
            continue

        # Fan out to all client queues
        for ws, q in list(_ws_client_queues.items()):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow client — drop rather than block


def _ensure_dispatcher(engine):
    """Start the dispatcher task if not already running."""
    global _dispatcher_task
    if _dispatcher_task is None or _dispatcher_task.done():
        try:
            loop = asyncio.get_running_loop()
            _dispatcher_task = loop.create_task(_dispatcher_loop(engine))
        except RuntimeError:
            pass


@router.websocket("/ws/sniper")
async def sniper_ws(websocket: WebSocket):
    """
    Stream SniperEngine events to frontend subscribers.

    Event types:
      token_detected  — new token seen
      token_watching  — token passed filters, being watched
      entry           — paper/live entry triggered
      exit            — paper/live exit triggered
      filter_rejected — token failed a hard filter
      act_transition  — token changed lifecycle act
    """
    await websocket.accept()

    engine = _sniper_engine

    # Give this client its own queue and ensure the dispatcher is running
    client_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    _ws_client_queues[websocket] = client_queue
    if engine:
        _ensure_dispatcher(engine)

    logger.info(f"[SniperWS] Client connected ({len(_ws_client_queues)} total)")

    try:
        # Send initial status snapshot
        if engine:
            status = engine.get_status()
            status["forward_test_stats"] = engine.forward_tester.get_stats()
            await websocket.send_json({"type": "status", **status})

        async def forward_events():
            """Drain this client's queue and send to WebSocket."""
            while True:
                try:
                    event = await asyncio.wait_for(client_queue.get(), timeout=35.0)
                    await websocket.send_json(event)
                except asyncio.TimeoutError:
                    # Shouldn't happen (dispatcher sends pings), but be safe
                    await websocket.send_json({"type": "ping"})
                except asyncio.CancelledError:
                    break
                except Exception:
                    break

        async def listen_client():
            """Listen for messages from the frontend."""
            try:
                while True:
                    data = await websocket.receive_text()
                    try:
                        msg = json.loads(data)
                        msg_type = msg.get("type", "")
                        if msg_type == "pong":
                            pass  # keepalive response
                    except json.JSONDecodeError:
                        pass
            except WebSocketDisconnect:
                pass

        await asyncio.gather(forward_events(), listen_client())

    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.error(f"[SniperWS] Error: {e}")
    finally:
        _ws_client_queues.pop(websocket, None)
        logger.info(f"[SniperWS] Client disconnected ({len(_ws_client_queues)} remaining)")


# ── Module-level engine reference ───────────────────────────────────────
# Set by main.py at startup

_sniper_engine = None

def set_engine(engine):
    """Called by main.py to inject the SniperEngine instance."""
    global _sniper_engine
    _sniper_engine = engine
