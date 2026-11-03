"""Live trading and portfolio routes.

`router` holds the status/history/stop group; `ws_router` holds the wallet,
buy-size and /ws/live/{mint} routes, which the app mounts later to keep the
original route registration order.
"""
import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Body, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from candle_aggregator import TIMEFRAME_SECONDS
from live_trader import LiveTrader, keypair_from_private_key
from pumpfun_client import resolve_input

import data_store
from app.deps import (
    _active_live_traders,
    _completed_live_sessions,
    _server_trade_events,
    _load_ledger_sessions,
    _fetch_wallet_sol_balance,
    _load_backend_private_key,
    _clear_backend_private_key,
    _save_backend_private_key,
    _save_live_buy_size,
    _resolve_sem,
    _autofeed,
)

logger = logging.getLogger("pump-chart")

router = APIRouter()


@router.get("/api/live/status")
async def live_status():
    traders = []
    seen_mints = set()

    total_pnl = 0.0
    unrealized_pnl = 0.0
    winning_trades = 0
    losing_trades = 0
    total_trades = 0
    # iter95 display truth: wallet-basis aggregates alongside the model PnL.
    total_cash_pnl = 0.0
    total_fees = 0.0
    total_rent = 0.0

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
        total_cash_pnl += st.get("total_cash_pnl_sol", 0.0)
        total_fees += st.get("total_fees_sol", 0.0)
        total_rent += st.get("total_rent_sol", 0.0)

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
        total_cash_pnl += st.get("total_cash_pnl_sol", 0.0)
        total_fees += st.get("total_fees_sol", 0.0)
        total_rent += st.get("total_rent_sol", 0.0)

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
            # iter95 display truth: wallet-basis aggregates.  total_pnl_sol
            # is the backtest-basis model PnL; wallet_delta_sol is the
            # all-in wallet change (cash − fees − net rent); the ex-rent
            # form (display surface, user directive 2026-10-03) drops the
            # stranded ATA rent the user reclaims manually.
            "total_cash_pnl_sol": round(total_cash_pnl, 6),
            "total_fees_sol": round(total_fees, 6),
            "total_rent_sol": round(total_rent, 6),
            "wallet_delta_sol": round(total_cash_pnl - total_fees - total_rent, 6),
            "wallet_delta_ex_rent_sol": round(total_cash_pnl - total_fees, 6),
        },
        "trades": _server_trade_events,
    })


@router.get("/api/live/history")
async def live_history(limit: int = Query(20, ge=1, le=100)):
    """Recent completed live-trading sessions with their closed trades."""
    sessions = _load_ledger_sessions(limit)
    return JSONResponse({"sessions": sessions, "count": len(sessions)})


@router.get("/api/portfolio")
async def portfolio_status(request: Request):
    """Aggregated portfolio state for the Portfolio tab.

    One call returns everything the page renders:
      wallet        — pubkey + on-chain SOL balance (cached when sessions run)
      positions     — open positions across active live sessions
      trades        — durable closed-trade history (session ledgers + this
                      run's confirmed events, deduped by sell tx signature)
      summary       — realized / unrealized / total PnL, win rate, counts
      equity_curve  — cumulative realized PnL over time for the PnL chart
    """
    # ── Wallet ────────────────────────────────────────────────────────────
    pubkey = ""
    sol_balance: Optional[float] = None
    active = [s for s in _active_live_traders.values() if not s.cancelled.is_set()]
    if active:
        pubkey = active[0].wallet()
        try:
            sol_balance = active[0].trader._get_cached_sol_balance()
        except Exception:
            sol_balance = None
    else:
        pk = getattr(request.app.state, "lt_private_key", "") or _load_backend_private_key()
        if pk:
            try:
                pubkey = str(keypair_from_private_key(pk).pubkey())
                sol_balance = await _fetch_wallet_sol_balance(pubkey)
            except Exception:
                pubkey = ""

    # ── Open positions across active sessions ─────────────────────────────
    positions = []
    unrealized_pnl = 0.0
    seen_mints = set()
    for mint, session in _active_live_traders.items():
        seen_mints.add(mint)
        trader = session.trader
        ct = trader.current_trade
        if ct is None:
            continue
        last = float(getattr(trader, "_last_price", 0.0) or 0.0)
        upnl = upnl_pct = 0.0
        if last > 0 and ct.entry_price > 0:
            upnl_pct = (last - ct.entry_price) / ct.entry_price * 100.0
            upnl = upnl_pct / 100.0 * ct.size_sol
        unrealized_pnl += upnl
        positions.append({
            "mint": mint,
            "token_name": session.token_name,
            "token_symbol": session.token_symbol,
            "size_sol": ct.size_sol,
            "size_tokens": ct.size_tokens,
            "entry_price": ct.entry_price,
            "last_price": last,
            "unrealized_pnl_sol": upnl,
            "unrealized_pnl_pct": upnl_pct,
            "entry_time": ct.entry_time,
            "entry_reason": ct.entry_reason,
        })

    # ── Closed-trade history: ledger (durable) + this run's events ────────
    closed: list[dict] = []
    seen_sigs: set[str] = set()

    def _add_closed(item: dict) -> None:
        sig = item.get("tx_hash") or ""
        if sig and sig in seen_sigs:
            return
        if sig:
            seen_sigs.add(sig)
        closed.append(item)

    for ev in _server_trade_events:
        if ev.get("action") != "SELL":
            continue
        _add_closed({
            "mint": ev.get("mint", ""),
            "token_symbol": ev.get("token_symbol", ""),
            "ts": ev.get("timestamp"),
            "pnl_sol": ev.get("pnl_sol", 0.0),
            "pnl_pct": ev.get("pnl_pct", 0.0),
            "cash_pnl_sol": ev.get("cash_pnl_sol", ev.get("pnl_sol", 0.0)),
            "tx_hash": ev.get("tx_hash", ""),
        })

    for sess in _load_ledger_sessions(limit=100):
        mint = sess.get("token_mint", "")
        seen_mints.add(mint)
        for t in sess.get("trades", []):
            _add_closed({
                "mint": t.get("token_mint") or mint,
                "token_symbol": "",
                "ts": t.get("exit_time") or t.get("event_ts"),
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"),
                "pnl_sol": t.get("pnl_sol", 0.0),
                "pnl_pct": t.get("pnl_pct", 0.0),
                # iter95: wallet-basis cash PnL (ledgers pre-iter91b lack
                # the field — fall back to the booked figure).
                "cash_pnl_sol": t.get("cash_pnl_sol", t.get("pnl_sol", 0.0)),
                "exit_reason": t.get("exit_reason", ""),
                "tx_hash": t.get("tx_hash_sell") or "",
            })

    closed.sort(key=lambda t: t.get("ts") or 0, reverse=True)

    realized_pnl = sum(t.get("pnl_sol", 0.0) for t in closed)
    realized_cash_pnl = sum(t.get("cash_pnl_sol", t.get("pnl_sol", 0.0)) for t in closed)
    winning = sum(1 for t in closed if (t.get("pnl_sol") or 0.0) > 0)
    losing = len(closed) - winning
    win_rate = (winning / len(closed) * 100.0) if closed else 0.0

    # Cumulative realized PnL (chronological) for the PnL curve
    equity_curve = []
    cum = 0.0
    for t in sorted(closed, key=lambda t: t.get("ts") or 0):
        cum += t.get("pnl_sol", 0.0)
        ts = t.get("ts")
        if ts:
            equity_curve.append({"time": int(ts), "value": round(cum, 6)})

    return JSONResponse({
        "wallet": {
            "pubkey": pubkey,
            "connected": bool(pubkey),
            "sol_balance": sol_balance,
        },
        "positions": positions,
        "trades": closed,
        "summary": {
            "realized_pnl_sol": round(realized_pnl, 6),
            "unrealized_pnl_sol": round(unrealized_pnl, 6),
            "total_pnl_sol": round(realized_pnl + unrealized_pnl, 6),
            # iter95 display truth: wallet-basis realized cash PnL.
            "realized_cash_pnl_sol": round(realized_cash_pnl, 6),
            "winning_trades": winning,
            "losing_trades": losing,
            "total_trades": len(closed),
            "win_rate": round(win_rate, 2),
            "tokens_traded": len(seen_mints),
        },
        "equity_curve": equity_curve,
    })


@router.post("/api/live/stop")
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


@router.post("/api/live/stop_all")
async def live_stop_all():
    n = 0
    for session in list(_active_live_traders.values()):
        session.stop("manual_stop")
        n += 1
    return JSONResponse({"status": "all_stopped", "count": n})


ws_router = APIRouter()


@ws_router.get("/api/live/private_key")
async def live_get_private_key(request: Request):
    """Get the current wallet connection status and public key."""
    pk = getattr(request.app.state, "lt_private_key", "") or _load_backend_private_key()
    if pk and not getattr(request.app.state, "lt_private_key", ""):
        request.app.state.lt_private_key = pk
        request.app.state.lt_wallet_connected = True
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


@ws_router.post("/api/live/private_key")
async def live_set_private_key(request: Request, body: dict = Body(...)):
    """Frontend tells backend it has a private key set (or passes the key to persist)."""
    connected = bool(body.get("connected", False))
    pk = str(body.get("private_key", "")).strip()

    if not connected or (not pk and not getattr(request.app.state, "lt_private_key", "") and not _load_backend_private_key()):
        request.app.state.lt_wallet_connected = False
        request.app.state.lt_private_key = ""
        _clear_backend_private_key()
        _autofeed.config.enabled = False
        await _autofeed.stop()
        return JSONResponse({"connected": False, "autofeed_running": False})

    if pk:
        try:
            kp = keypair_from_private_key(pk)
            request.app.state.lt_private_key = pk
            request.app.state.lt_wallet_connected = True
            _save_backend_private_key(pk)
            pubkey = str(kp.pubkey())
        except ValueError as e:
            return JSONResponse({"error": f"Invalid private key: {e}"}, status_code=400)
    else:
        # Re-affirming connected state with key already in memory or disk
        pk = getattr(request.app.state, "lt_private_key", "") or _load_backend_private_key()
        request.app.state.lt_private_key = pk
        request.app.state.lt_wallet_connected = True
        pubkey = str(keypair_from_private_key(pk).pubkey()) if pk else ""

    return JSONResponse({
        "connected": True,
        "pubkey": pubkey,
        "autofeed_running": _autofeed.is_running(),
    })


@ws_router.post("/api/live/buy_size")
async def live_set_buy_size(request: Request, body: dict = Body(...)):
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

    request.app.state.lt_buy_size = size
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


@ws_router.websocket("/ws/live/{mint}")
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
    engine_versions: str = Query(default=""),
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

    # Late import: the session factory lives in main.py alongside the session
    # classes in this seam; importing at module level would be circular.
    from main import _get_or_create_live_session

    session, created = await _get_or_create_live_session(
        real_mint=real_mint,
        private_key=private_key,
        buy_size=buy_size,
        slippage_bps=slippage_bps,
        skip_sim=skip_sim,
        engine_params=engine_params,
        engine_version=engine_version,
        engine_versions=engine_versions,
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
            "engine_versions": session.engine_versions,
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
