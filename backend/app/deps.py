"""Process-wide state shared by main.py and the routers.

Registries, locks, semaphores, feed singletons and the file-backed wallet
helpers.  Every object is created once per process, exactly as when it lived
in main.py; handlers import the names they need.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from autofeed import AutoFeed, AutofeedConfig
from live_trader import SOLANA_RPC_PRIMARY
from newpairs import NewPairsFeed, NewPairsConfig

logger = logging.getLogger("pump-chart")

# Limit how many tokens can run resolve_input / get_historical_candles
# concurrently.  Without this, N parallel tokens all hammer DexScreener /
# pump.fun v3 simultaneously, causing timeouts that look like connect failures.
_resolve_sem = asyncio.Semaphore(8)

FRONTEND_DIR = Path(__file__).parent.parent.parent / "frontend"


KEY_FILE_PATH = Path(__file__).parent.parent / "data" / "live_key.json"


def _save_backend_private_key(pk: str):
    try:
        KEY_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE_PATH.write_text(json.dumps({"private_key": pk}), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[LIVE] Failed to persist private key to disk: {e}")


def _clear_backend_private_key():
    try:
        if KEY_FILE_PATH.exists():
            KEY_FILE_PATH.unlink()
    except Exception as e:
        logger.warning(f"[LIVE] Failed to remove persistent private key file: {e}")


def _load_backend_private_key() -> str:
    try:
        if KEY_FILE_PATH.exists():
            data = json.loads(KEY_FILE_PATH.read_text(encoding="utf-8"))
            return data.get("private_key", "")
    except Exception as e:
        logger.warning(f"[LIVE] Failed to read persistent private key file: {e}")
    return ""


# ── Live buy size: single-source-of-truth store ────────────────────────────
# The dashboard's "Buy Size (SOL)" input field is the ONLY source of the live
# buy size.  The frontend pushes its value here (POST /api/live/buy_size);
# every session-creation path (WS connect, autofeed server-side spawn) reads
# it from this store.  There is deliberately NO default — creation without a
# user-supplied size is refused instead of guessing one.
LIVE_SETTINGS_PATH = Path(__file__).parent.parent / "data" / "live_settings.json"


def _save_live_buy_size(size: float):
    try:
        LIVE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIVE_SETTINGS_PATH.write_text(
            json.dumps({"buy_size": size}), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"[LIVE] Failed to persist buy size to disk: {e}")


def _load_live_buy_size() -> Optional[float]:
    try:
        if LIVE_SETTINGS_PATH.exists():
            data = json.loads(LIVE_SETTINGS_PATH.read_text(encoding="utf-8"))
            size = float(data.get("buy_size", 0.0))
            if size > 0:
                return size
    except Exception as e:
        logger.warning(f"[LIVE] Failed to read persistent buy size file: {e}")
    return None


# ── Live trading state ───────────────────────────────────────────────────
# Live sessions are SERVER-SIDED: they own the trader, the market stream, the
# auto-recording and the holder-flow pump, and keep running independently of
# any browser tab.  Tabs attach as viewers over /ws/live/{mint}; closing a tab
# only detaches that viewer.  A session ends when it is explicitly stopped
# (POST /api/live/stop, the ⏹ Stop button) or a safety stop fires.
_active_live_traders: dict[str, "_LiveSession"] = {}   # keyed by token mint
_live_session_lock = asyncio.Lock()

# Persistent session stats and trade events for the current server run
_completed_live_sessions: list[dict] = []
_server_trade_events: list[dict] = []


def _record_server_trade_event(*, mint: str, token_symbol: str, event: str, payload: dict):
    ts = payload.get("timestamp") or time.time()
    sig = payload.get("detail") or ""
    if event == "buy_confirmed":
        ct = payload.get("current_trade") or {}
        _server_trade_events.append({
            "id": len(_server_trade_events) + 1,
            "mint": mint,
            "token_symbol": token_symbol,
            "action": "BUY",
            "timestamp": ts,
            "price": ct.get("entry_price") or 0.0,
            "pnl_sol": 0.0,
            "pnl_pct": 0.0,
            "tx_hash": sig,
            "status": "confirmed",
        })
    elif event == "sell_confirmed":
        ct = payload.get("closed_trade") or payload.get("current_trade") or {}
        _server_trade_events.append({
            "id": len(_server_trade_events) + 1,
            "mint": mint,
            "token_symbol": token_symbol,
            "action": "SELL",
            "timestamp": ts,
            "price": ct.get("exit_price") or 0.0,
            "pnl_sol": ct.get("pnl_sol") or 0.0,
            "pnl_pct": ct.get("pnl_pct") or 0.0,
            # iter95 display truth: wallet-basis cash PnL rides along so
            # the portfolio can show model vs wallet side by side.
            "cash_pnl_sol": ct.get("cash_pnl_sol", ct.get("pnl_sol") or 0.0),
            "tx_hash": sig,
            "status": "confirmed",
        })


# ── New Pairs state ─────────────────────────────────────────────────────────
_active_newpair_sessions: dict[str, "_NewPairSession"] = {}   # keyed by mint
_newpair_session_lock = asyncio.Lock()
_completed_newpair_sessions: list[dict] = []


# ── AutoFeed state & manager ───────────────────────────────────────────────
# AutoFeed is a *discovery-only* loop that polls gmgn.ai for migrated pump.fun
# tokens (mcap ≥ 15k) and pushes candidate mint addresses to connected Live
# Trading dashboards via /ws/autofeed.  The frontend then opens the SAME
# /ws/live/{mint} WebSocket the manual "Start Trading" button uses — autofeed
# performs no trading itself.

_autofeed: AutoFeed = AutoFeed(AutofeedConfig())
# Per-WS client queue of pending candidates to push
_autofeed_clients: set[asyncio.Queue] = set()


# ── New Pairs feed & API ────────────────────────────────────────────────────
# Discovery-only loop over PumpPortal's subscribeNewToken stream.  Every
# accepted newborn token is auto-recorded (NO engine, NO trading) with a
# no-motion-only stop; price action lands in newpairs_data.db.

_newpairs_feed: NewPairsFeed = NewPairsFeed(NewPairsConfig())
_newpairs_clients: set[asyncio.Queue] = set()   # /ws/newpairs/feed viewer queues


def _load_ledger_sessions(limit: int = 20) -> list[dict]:
    """Recent completed live-trading sessions with their closed trades.

    Read from each session's physical trades.jsonl ledger (survives restarts
    and sessions that ended before the page was opened).  Feeds /api/portfolio
    only — the Live Execution tab deliberately does NOT consume this (2026-09-27:
    its trade history is the in-memory current-run list and must clear on
    server restart).  The ledgers themselves stay on disk as the audit trail
    (parity guard, fill forensics).
    """
    from live_session_logger import LOG_ROOT
    sessions = []
    if LOG_ROOT.is_dir():
        dirs = sorted(
            (p for p in LOG_ROOT.iterdir()
             if p.is_dir() and (p / "trades.jsonl").exists()),
            key=lambda p: p.name, reverse=True,
        )[:limit]
        for d in dirs:
            meta = {"token_mint": "", "wallet": ""}
            trades = []
            try:
                with open(d / "trades.jsonl", "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                        except Exception:
                            logger.debug("malformed journal line skipped, rest still parsed")
                            continue
                        event = rec.get("event")
                        if event == "session_open":
                            meta["token_mint"] = rec.get("token_mint", meta["token_mint"])
                            meta["wallet"] = rec.get("wallet", meta["wallet"])
                        elif event == "trade_closed":
                            t = rec.get("trade") or {}
                            if isinstance(t, dict) and t.get("status") == "closed":
                                trades.append({
                                    "event_ts": rec.get("ts"),
                                    **t,
                                })
            except Exception:
                logger.debug("journal read failed, session listed with default meta",
                             exc_info=True)
            sessions.append({
                "session_id": d.name,
                "token_mint": meta["token_mint"] or d.name.split("_", 2)[-1],
                "wallet": meta["wallet"],
                "trades": trades,
            })
    return sessions


async def _fetch_wallet_sol_balance(pubkey: str) -> Optional[float]:
    """Standalone wallet SOL balance via the primary public RPC — used when
    no live session is running (active sessions expose their own cached
    balance, so no extra RPC is spent while trading)."""
    import aiohttp
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getBalance",
        "params": [pubkey, {"commitment": "confirmed"}],
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=3.0)
        ) as http:
            async with http.post(SOLANA_RPC_PRIMARY, json=payload) as resp:
                data = await resp.json(content_type=None)
                lamports = (data.get("result") or {}).get("value") or 0
                return float(lamports) / 1e9
    except Exception as e:
        logger.warning(f"[PORTFOLIO] Wallet balance fetch failed: {e}")
        return None
