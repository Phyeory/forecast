"""DEGEN FastAPI router — mounts the standalone page + control API.

Integration in backend/main.py (~3 lines):
    from degen.router import router as degen_router   # needs repo root on sys.path
    app.include_router(degen_router)

Owns the lazy runtime: DegenFeed + DecisionEngine + TraderJournal + optional
DegenExecutor (wallet = the same backend live key) + TerminalDriver.  Nothing
here touches the V2 engine stack.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from .agent import DegenAgent, RuntimeConfig
from .decision import DecisionEngine, TraderJournal
from .feed import DegenConfig, DegenFeed
from .terminal_driver import TerminalDriver

logger = logging.getLogger("degen.router")

router = APIRouter(tags=["degen"])

_WEB_DIR = Path(__file__).resolve().parent / "web"
JOURNAL_DIR = Path(__file__).resolve().parent / "journals"


class DegenRuntime:
    def __init__(self):
        self.config = RuntimeConfig(
            mode=os.environ.get("DEGEN_MODE", "shadow"),
            buy_size_sol=float(os.environ.get("DEGEN_BUY_SIZE_SOL", "0.05")),
        )
        self.journal = TraderJournal(JOURNAL_DIR)
        self.engine = DecisionEngine(self.journal)
        self.feed = DegenFeed(DegenConfig())
        self.executor = None
        self.terminal = TerminalDriver(headless=os.environ.get("DEGEN_TERMINAL_HEADLESS", "1") == "1")
        self.agent: Optional[DegenAgent] = None
        self.started = False

    def _maybe_executor(self):
        if self.executor is not None:
            return
        try:
            from live_trader import keypair_from_private_key
            key_file = Path(__file__).resolve().parent.parent / "backend" / "data" / "live_key.json"
            if not key_file.exists():
                return
            data = json.loads(key_file.read_text())
            pk = data.get("private_key") or data.get("privateKey") or ""
            if pk:
                self.executor = ExecutorShim(keypair_from_private_key(pk))
        except Exception as e:
            logger.warning("[DegenRouter] executor unavailable: %s", e)

    async def start(self):
        if self.started:
            return
        self._maybe_executor()
        self.agent = DegenAgent(self.feed, self.engine, self.journal,
                                executor=self.executor, terminal=self.terminal,
                                config=self.config)
        await self.agent.start()
        self.started = True

    async def stop(self):
        if not self.started:
            return
        if self.agent:
            await self.agent.stop()
        await self.terminal.close()
        self.started = False


class ExecutorShim:
    """DegenExecutor built from the backend live key (same wallet)."""

    def __init__(self, keypair):
        from .executor import DegenExecutor
        self._impl = DegenExecutor(keypair)

    async def buy(self, mint, size_sol, slippage_bps=2000):
        return await self._impl.buy(mint, size_sol, slippage_bps)

    async def sell(self, mint, token_amount_raw=None, slippage_bps=5000):
        return await self._impl.sell(mint, token_amount_raw, slippage_bps)


_runtime: Optional[DegenRuntime] = None


def get_runtime() -> DegenRuntime:
    global _runtime
    if _runtime is None:
        _runtime = DegenRuntime()
    return _runtime


# ── page ─────────────────────────────────────────────────────────────────────

@router.get("/degen")
async def degen_page():
    return FileResponse(_WEB_DIR / "index.html")


# ── status / config / lifecycle ──────────────────────────────────────────────

def _snapshot_dict(rt: "DegenRuntime") -> dict:
    if rt.started and rt.agent:
        snap = rt.agent.snapshot()
    else:
        snap = {
            "ts": 0, "config": rt.config.to_dict(), "engine": rt.engine.health(),
            "positions": [], "traps": [], "watchlist": [], "calibration": {},
            "feed": {"watching": 0, "watched": [], "traps": [], "sol_usd": None},
        }
    snap["started"] = rt.started
    return snap


@router.get("/api/degen/status")
async def status():
    return JSONResponse(_snapshot_dict(get_runtime()))


@router.post("/api/degen/config")
async def set_config(body: dict):
    rt = get_runtime()
    if "mode" in body:
        if body["mode"] not in ("shadow", "confirm", "auto"):
            raise HTTPException(400, "mode must be shadow|confirm|auto")
        rt.config.mode = body["mode"]
        if rt.agent:
            rt.agent.set_mode(body["mode"])
    for key in ("buy_size_sol", "max_positions", "manage_interval_s"):
        if key in body:
            try:
                setattr(rt.config, key, type(getattr(rt.config, key))(body[key]))
            except (TypeError, ValueError):
                raise HTTPException(400, f"bad value for {key}")
    return {"ok": True, "config": rt.config.to_dict()}


@router.post("/api/degen/start")
async def start():
    await get_runtime().start()
    return {"ok": True, "mode": get_runtime().config.mode}


@router.post("/api/degen/stop")
async def stop():
    await get_runtime().stop()
    return {"ok": True}


# ── journal / calibration ────────────────────────────────────────────────────

@router.get("/api/degen/journal")
async def journal_tail(n: int = 80):
    rt = get_runtime()
    try:
        lines = rt.journal.journal.read_text(encoding="utf-8").splitlines()[-max(1, min(n, 500)):]
    except FileNotFoundError:
        return {"entries": []}
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return {"entries": entries}


@router.get("/api/degen/calibration")
async def calibration():
    return get_runtime().engine.calibration.reliability()


# ── manual actions ───────────────────────────────────────────────────────────

@router.post("/api/degen/qualify/{mint}")
async def qualify(mint: str):
    """Manual qualify: forensics + decision, journaled; never auto-executes."""
    rt = get_runtime()
    feed = rt.feed
    birth = feed.birth(mint)
    from . import research
    forensics = await research.run_forensics(
        mint, birth=birth.to_dict() if birth else {}, creator=birth.creator if birth else "")
    if not rt.started or not rt.agent:
        return {"forensics": forensics, "decision": None,
                "note": "agent not started — forensics only"}
    vector = rt.agent._vector("manual", {}, feed.tracker(mint) or {},
                              birth.to_dict() if birth else {}, forensics)
    decision = await rt.engine.decide("qualify", mint, vector)
    return {"forensics": forensics, "decision": decision.to_dict()}


@router.post("/api/degen/trap/cancel")
async def cancel_trap(body: dict):
    rt = get_runtime()
    trap_id = body.get("trap_id") or ""
    mint = body.get("mint") or ""
    if rt.agent:
        rt.agent.traps.pop(trap_id, None)
    ok = rt.feed.cancel_trap(mint, trap_id)
    return {"ok": ok}


# ── terminal browser lane ────────────────────────────────────────────────────

@router.post("/api/degen/terminal/login")
async def terminal_login():
    rt = get_runtime()
    return await rt.terminal.open_login()


@router.post("/api/degen/terminal/prepare")
async def terminal_prepare(body: dict):
    rt = get_runtime()
    mint = body.get("mint") or ""
    if not mint:
        raise HTTPException(400, "mint required")
    size = float(body.get("size_sol") or rt.config.buy_size_sol)
    return await rt.terminal.prepare_buy(mint, size, fill=bool(body.get("fill", True)))


# ── live push ────────────────────────────────────────────────────────────────

@router.websocket("/ws/degen")
async def ws_degen(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            rt = get_runtime()
            snap = _snapshot_dict(rt)
            await ws.send_text(json.dumps(snap, default=str))
            await asyncio.sleep(2.0)
    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
