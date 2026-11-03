import asyncio
import json
import logging
import time
from pathlib import Path

# Load .env from backend/ AND project root (both merged) early — used
# by autofeed/holder_flow to find GMGN_API_KEY.  dotenv does NOT override
# env vars already set in the process.  backend/.env wins on conflicts.
try:
    from dotenv import load_dotenv
    _backend_dir = Path(__file__).parent
    _root_dir = _backend_dir.parent
    # Load root first, then backend overlay (no override=False semantics:
    # first-loaded wins).  Old code `break`ed after backend/.env, so a root
    # GMGN_API_KEY was silently never loaded.
    for _candidate in (_root_dir / ".env", _backend_dir / ".env"):
        if _candidate.exists():
            load_dotenv(_candidate, override=False)
except ImportError:
    pass
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from candle_aggregator import CandleAggregator
from pumpfun_client import (
    DexScreenerPollClient,
    PumpFunWSClient,
    PumpSwapRPCClient,
    get_ds_pair_by_mint,
    get_historical_candles,
    resolve_input,
)
from live_trader import LiveTrader, keypair_from_private_key
from autofeed import Candidate
from newpairs import NewPairCandidate
import data_store
import newpairs_store
from holder_flow import HolderFlowMonitor, get_shared_monitor
from session_calibrator import calibrate_async

from app.deps import (
    FRONTEND_DIR,
    _resolve_sem,
    _active_live_traders,
    _live_session_lock,
    _completed_live_sessions,
    _record_server_trade_event,
    _active_newpair_sessions,
    _newpair_session_lock,
    _completed_newpair_sessions,
    _autofeed,
    _autofeed_clients,
    _newpairs_feed,
    _newpairs_clients,
    _load_backend_private_key,
    _load_live_buy_size,
)
from app.routers.chart import router as chart_router, ws_router as chart_ws_router
from app.routers.recordings import (
    router as recorder_recordings_router,
    newpairs_recordings_router,
    finish_stale_recordings,
)
from app.routers.backtests import router as backtests_router
from app.routers.live import router as live_router, ws_router as live_ws_router
from app.routers.autofeed import router as autofeed_router
from app.routers.newpairs import router as newpairs_router, ws_router as newpairs_ws_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  [%(name)s]  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pump-chart")

app = FastAPI(title="pump-chart")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# Route registration order mirrors the pre-split decorator order exactly:
# path matching and the generated OpenAPI document both depend on it.
app.include_router(chart_router)
app.include_router(recorder_recordings_router)
app.include_router(backtests_router)
app.include_router(chart_ws_router)
app.include_router(live_router)
app.include_router(autofeed_router)
app.include_router(newpairs_router)
app.include_router(newpairs_recordings_router)
app.include_router(newpairs_ws_router)
app.include_router(live_ws_router)


def _get_live_buy_size() -> Optional[float]:
    """Current user-supplied buy size (in-memory first, disk fallback).

    Returns None when the dashboard has never pushed a valid value — callers
    must treat that as 'refuse to trade', never as 'use a default'."""
    size = getattr(app.state, "lt_buy_size", None)
    if size is None or not isinstance(size, (int, float)) or size <= 0:
        size = _load_live_buy_size()
        if size is not None:
            app.state.lt_buy_size = size
    if isinstance(size, (int, float)) and size > 0:
        return float(size)
    return None


@app.on_event("startup")
async def startup_load_live_key():
    pk = _load_backend_private_key()
    if pk:
        try:
            kp = keypair_from_private_key(pk)
            app.state.lt_private_key = pk
            app.state.lt_wallet_connected = True
            logger.info(f"[LIVE] Restored server-side private key for wallet {str(kp.pubkey())[:8]}…")
        except Exception as e:
            logger.warning(f"[LIVE] Saved private key file invalid: {e}")


async def _stale_scanner_loop(interval_seconds: int = 60, stale_seconds: int = 60):
    """Background loop: periodically run the stale-finish pass."""
    while True:
        try:
            await finish_stale_recordings(stale_seconds=stale_seconds)
        except Exception:
            logger.exception("stale scanner error")
        await asyncio.sleep(interval_seconds)


@app.on_event("startup")
async def start_stale_scanner():
    # Run once at startup to catch any stale DB rows from previous crashes
    try:
        await finish_stale_recordings(stale_seconds=60)
    except Exception:
        logger.exception("initial stale finish failed")
    # Start background task to run periodically
    app.state._stale_scanner_task = asyncio.create_task(_stale_scanner_loop())


@app.on_event("shutdown")
async def stop_stale_scanner():
    t = getattr(app.state, "_stale_scanner_task", None)
    if t:
        t.cancel()


# ── Live trading state ───────────────────────────────────────────────────
# Live sessions are SERVER-SIDED: they own the trader, the market stream, the
# auto-recording and the holder-flow pump, and keep running independently of
# any browser tab.  Tabs attach as viewers over /ws/live/{mint}; closing a tab
# only detaches that viewer.  A session ends when it is explicitly stopped
# (POST /api/live/stop, the ⏹ Stop button) or a safety stop fires.


class _LiveSession:
    """Server-side live-trading session, decoupled from any browser tab.

    The /ws/live/{mint} handler creates one _LiveSession per token and then
    attaches as a viewer.  All trading logic lives in `run()`, a detached
    background task that survives tab closes/reopens.  Viewers receive every
    candle/strategy/trade_update via per-viewer broadcast queues and can send
    control messages (update_config, manual_trade) at any time.
    """

    def __init__(self, *, real_mint: str, token_name: str, token_symbol: str,
                 timeframe: str, trader: LiveTrader, token_info: Optional[dict],
                 engine_version: int = 2,
                 extra_traders: Optional[list[LiveTrader]] = None,
                 engine_versions: Optional[list[int]] = None):
        self.real_mint = real_mint
        self.token_name = token_name
        self.token_symbol = token_symbol
        self.timeframe = timeframe
        self.trader = trader
        self.token_info = token_info
        # iter77 multi-engine fleet: `traders[0]` is the primary (back-compat
        # `self.trader`); extra_traders are sibling engines on the SAME
        # wallet/stream, each with its own engine + position + journal, sized
        # buy_size/len(traders).  Shared-wallet token isolation is enforced
        # by the fleet share registry in live_trader.py.
        self.traders: list[LiveTrader] = [trader] + list(extra_traders or [])
        self.engine_version = engine_version
        self.engine_versions: list[int] = list(
            engine_versions or ([engine_version] * len(self.traders)))
        self.cancelled = asyncio.Event()
        self.warmed = asyncio.Event()      # set once historical warmup finished
        self.subscribers: set[asyncio.Queue] = set()
        self.task: Optional[asyncio.Task] = None
        self.rec_id: Optional[int] = None
        self.warmup_candles: list[dict] = []
        self.warmup_strategy: list[dict] = []
        self.last_strategy_result: Optional[dict] = None
        self.stop_reason: str = ""
        # LiveTrader pushes trade_update JSON strings here → fanned out to viewers
        trader.broadcast_fn = self.broadcast_text
        # iter77 fleet: sibling traders broadcast through the same fan-out;
        # their payloads carry per-engine tags (set in _get_or_create_live_session).
        for _tr in self.traders[1:]:
            _tr.broadcast_fn = self.broadcast_text

    # ── viewer fan-out ────────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def broadcast(self, obj) -> None:
        """Fan a message out to every attached viewer.  Never blocks the
        trading loop: a slow/full viewer just misses messages."""
        for q in list(self.subscribers):
            try:
                q.put_nowait(obj)
            except asyncio.QueueFull:
                pass

    async def broadcast_text(self, payload: str) -> None:
        # LiveTrader._broadcast_status sends pre-serialised JSON strings
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
            if isinstance(data, dict) and data.get("type") == "trade_update":
                evt = data.get("event")
                if evt in ("buy_confirmed", "sell_confirmed"):
                    _record_server_trade_event(
                        mint=self.real_mint,
                        token_symbol=self.token_symbol,
                        event=evt,
                        payload=data,
                    )
        except Exception:
            pass
        self.broadcast(payload)

    def wallet(self) -> str:
        return str(self.trader.keypair.pubkey())

    def stop(self, reason: str = "manual_stop") -> None:
        if not self.cancelled.is_set():
            self.stop_reason = reason
            self.cancelled.set()

    # ── session runner ────────────────────────────────────────────────────

    async def run(self) -> None:
        real_mint = self.real_mint
        live_trader = self.trader          # primary (back-compat references)
        traders = self.traders             # iter77: full fleet (≥1 trader)
        timeframe = self.timeframe
        token_info = self.token_info or {}
        rec_id = self.rec_id
        logger.info(
            f"[LIVE] Session start  mint={real_mint[:8]}…  wallet={self.wallet()[:8]}…  "
            f"tf={timeframe}  engines={self.engine_versions}"
        )

        # Resolve live data source
        live_source = token_info.get("_live_source", "pumpportal")
        live_query = token_info.get("pair_address") or real_mint
        if live_source == "solana_rpc" and token_info.get("pair_address"):
            ws_client = PumpSwapRPCClient(live_query)
        elif live_source == "dexscreener":
            poll_seconds = 0.25 if timeframe in {"1s", "5s", "15s"} else 0.5
            ws_client = DexScreenerPollClient(live_query, poll_seconds=poll_seconds)
        else:
            ws_client = PumpFunWSClient(real_mint)

        aggregator = CandleAggregator(timeframe)
        last_sent_price = None

        # ── Holder-flow monitor (live dev/insider sell detection) ────────────
        # Watches the token and persists events to the auto-recording's
        # holder_flow table (so the live session is itself backtestable), and
        # feeds the events into the strategy engine so the iter36 entry gate /
        # exit trigger fire in real time.  Parity: the engine checks the same
        # event list the backtester would later replay from the DB.
        # Shared process-wide monitor — one GMGN poller regardless of how many
        # sessions are active (iter36 rate-limit fix).  iter66: the monitor
        # ALSO runs a realtime on-chain dev-sell watcher per token (PumpPortal
        # trade stream × pool-account coin_creator match — no GMGN, no rate
        # limits, no indexing lag); both sources funnel into the same
        # holder_flow table with cross-source tx-hash dedupe, and engine
        # delivery stays exclusively via the id-cursor pump below so the
        # backtester replays exactly what the live engine saw.
        holder_monitor = None
        # Track holder-flow events already pushed into the engine.  iter57
        # parity fix: delivery is by DB row id (lossless), NOT by counting the
        # monitor's in-memory list.  The monitor trims `recent_events` to a
        # 60 s window, so a count-based diff desynchronises the moment an
        # event ages out — every event landing below the stale high-water mark
        # was silently never delivered to the engine (empirically only 8–25 %
        # of events reached it).  The DB rows are persisted at discovery time
        # and are exactly what the backtester later replays, so an id-cursor
        # read makes the live engine's event stream identical to backtest.
        _hf_pump_task = None
        _hf_stop = asyncio.Event()
        _nm_watch_task = None  # no-motion idle-session terminator
        # iter57 parity fix: holder-flow delivery cursor is the last DB row id
        # pushed into the engine (lossless, matches backtest replay exactly).
        _hf_last_id = {"id": 0}

        async def _holder_flow_pump():
            """Background task that pushes new holder-flow events into the engine
            every 1s, decoupled from trade ticks.  Without this, events discovered
            by the GMGN poller sit in the monitor's buffer until the next trade
            arrives — which may be 10s+ on illiquid tokens, making dev_sell_exit
            fire far later than the backtester (which sees events at their exact
            on-chain timestamp)."""
            while not _hf_stop.is_set():
                try:
                    if rec_id is not None:
                        _new = data_store.get_holder_flow_since(rec_id, _hf_last_id["id"])
                        if _new:
                            _hf_last_id["id"] = _new[-1]["id"]
                            # iter77 fleet: deliver to EVERY engine — each
                            # trader's engine consumes the same event stream,
                            # exactly as separate backtests would.
                            for _tr in traders:
                                try:
                                    _tr.engine.append_holder_flow_events(_new)
                                    # Immediately check if this triggers an immediate dev_sell_exit
                                    exit_reason = _tr.check_immediate_holder_flow_exit()
                                    if exit_reason:
                                        logger.info(f"[HOLDER FLOW] Immediate exit triggered: {exit_reason}")
                                        _tr.immediate_holder_flow_exit(exit_reason)
                                except AttributeError:
                                    pass  # V1 engine has no holder-flow surface
                except Exception:
                    pass
                # Realtime delivery: wake the instant the shared monitor
                # dispatches any event instead of sleeping out a full second.
                # Entry gates and dev_sell exits must see holder-flow data at
                # tick time to match the backtester, which always has events
                # available at their exact on-chain timestamp (the fixed 1 s
                # poll let late-arriving GMGN whales miss buy signals that had
                # already fired).  1 s timeout bounds idle polling.
                try:
                    await asyncio.wait_for(
                        holder_monitor.new_event_signal().wait(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    await asyncio.sleep(0.05)
                else:
                    try:
                        holder_monitor.new_event_signal().clear()
                    except Exception:
                        pass

        try:
            holder_monitor = get_shared_monitor()
            await holder_monitor.start()
            # pool_address (iter66) lets the realtime on-chain watcher resolve
            # the dev wallet from the PumpSwap pool account — no GMGN involved.
            _hf_pool = token_info.get("pair_address") \
                if live_source == "solana_rpc" else None
            holder_monitor.watch_token(real_mint, recording_id=rec_id,
                                       pool_address=_hf_pool)
            # Pre-load any holder_flow events already persisted for this
            # recording (e.g. from a prior session on the same token, or events
            # the monitor captured before the session started).  This matches
            # the backtester which calls set_holder_flow_events() with the full
            # event list before the candle loop starts.  The pump's id cursor
            # starts after the newest pre-loaded row so nothing is delivered
            # twice.  (max id, NOT the last row by time — a late-discovered
            # event can carry an earlier chain time than an already-loaded
            # row and would otherwise be re-delivered by the pump.)
            _existing_hf = data_store.get_holder_flow(rec_id)
            if _existing_hf:
                for _tr in traders:
                    try:
                        _tr.engine.set_holder_flow_events(_existing_hf)
                    except AttributeError:
                        pass  # V1 engine has no holder-flow surface
            _hf_last_id["id"] = max((r["id"] for r in _existing_hf), default=0)
            _hf_pump_task = asyncio.ensure_future(_holder_flow_pump())


            # ── No-motion idle-session terminator ───────────────────────────
            # Companion to the in-stream check in _process_stream below: a
            # coin whose trade feed has gone completely silent never yields
            # another tick, so the stream-loop check would starve exactly
            # when it is needed.  The LiveTrader watchdog (time-driven) sets
            # no_motion_stop_triggered once the coin has had no price motion
            # for no_motion_stop_seconds while fully idle; this 5s poller
            # shuts the session down when it fires.  Identical to the mcap
            # floor stop minus the emergency sell — teardown finalises the
            # auto-recording via data_store.stop_recording().
            async def _no_motion_watch():
                while not self.cancelled.is_set():
                    await asyncio.sleep(5.0)
                    # iter77 fleet: ANY trader's no-motion stop ends the
                    # session (a motionless coin means every engine is idle).
                    for _tr in traders:
                        if _tr.no_motion_stop_triggered:
                            logger.warning(
                                f"[LIVE] No-motion stop triggered for {real_mint[:8]}… — "
                                f"session idle, shutting down"
                            )
                            self.stop("no_motion")
                            return

            _nm_watch_task = asyncio.ensure_future(_no_motion_watch())

            # ── Historical warm-up (seeds the recording + the engine) ────────
            try:
                async with _resolve_sem:
                    hist = await asyncio.wait_for(
                        get_historical_candles(real_mint, timeframe), timeout=15.0
                    )
            except asyncio.TimeoutError:
                logger.warning(f"[LIVE] get_historical_candles timed out for {real_mint[:8]}")
                hist = []
            if hist:
                # Seed the auto-recording with the same historical candles
                data_store.insert_candles_batch(rec_id, hist)

                strategy_results = []
                for candle in hist:
                    # iter77 fleet: every engine warms up on the same tape —
                    # identical to N separate sessions' warmups.
                    for _tr in traders:
                        _tr.update_historical_candle(
                            time_val=int(candle["time"]),
                            o=candle["open"], h=candle["high"],
                            l=candle["low"], c=candle["close"],
                            volume=candle.get("volume", 0),
                            buy_volume=candle.get("buy_volume", 0.0),
                            sell_volume=candle.get("sell_volume", 0.0),
                            market_cap_usd=candle.get("market_cap_usd", 0.0),
                            pool_sol=candle.get("pool_sol", 0.0),
                        )
                    # The chart/UI consumes the PRIMARY engine's result stream.
                    result = live_trader._last_engine_result or {}
                    strategy_results.append(result)

                self.warmup_candles = hist
                self.warmup_strategy = strategy_results
                if strategy_results:
                    self.last_strategy_result = strategy_results[-1]
                last = hist[-1]
                aggregator.process_trade(last["close"], 0.0, float(last["time"]))
                logger.info(f"[LIVE] Warmed up on {len(hist)} historical candles for {real_mint[:8]}")
            self.warmed.set()

            async def _process_stream(client) -> bool:
                nonlocal last_sent_price
                got_trade = False
                async for trade in client.stream():
                    if self.cancelled.is_set():
                        break
                    got_trade = True
                    is_synthetic = bool(trade.get("synthetic"))
                    is_buy_live: Optional[bool] = None
                    if not is_synthetic:
                        tx_live = trade.get("tx_type", "")
                        if tx_live == "buy":
                            is_buy_live = True
                        elif tx_live == "sell":
                            is_buy_live = False
                        # iter66: realtime holder-flow watcher feed (same as
                        # the recorder loop — whale sells classified here,
                        # dev trades via the watcher's ATA subscription).
                        if holder_monitor is not None:
                            holder_monitor.observe_trade(real_mint, trade)
                    candle, is_new = aggregator.process_trade(
                        trade["price"], trade["sol_amount"], trade["timestamp"],
                        synthetic=is_synthetic,
                        is_buy=is_buy_live,
                        pool_sol=trade.get("pool_sol", 0.0),
                        market_cap_usd=trade.get("market_cap_usd", 0.0),
                    )
                    candle_dict = candle.to_dict()
                    current_price = candle_dict["close"]

                    # Persist candle to the auto-recording (same as standalone recorder)
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

                    # ── Engine feed (iter57 parity fix) ──────────────────────
                    # live_trader.update() MUST run on EVERY tick, including
                    # same-price ticks.  The update call refreshes the
                    # accumulating-candle buffer (volume / buy / sell split /
                    # mcap / pool_sol); skipping same-price ticks left the
                    # buffer holding a stale snapshot, so the completed candle
                    # was expanded into engine states with understated volume
                    # while the recorded candle (persisted below on every
                    # tick) carried the full volume the backtester replays.
                    # Only the *UI broadcast* is throttled to price changes
                    # and candle boundaries.
                    # iter77 fleet: EVERY trader's engine consumes the tick —
                    # identical to N single-engine sessions on one stream.
                    fleet_results = []
                    for _tr in traders:
                        fleet_results.append(_tr.update(
                            time_val=candle_dict["time"],
                            o=candle_dict["open"], h=candle_dict["high"],
                            l=candle_dict["low"], c=candle_dict["close"],
                            volume=candle_dict.get("volume", 0),
                            buy_volume=candle_dict.get("buy_volume", 0.0),
                            sell_volume=candle_dict.get("sell_volume", 0.0),
                            is_new=is_new,
                            market_cap_usd=candle_dict.get("market_cap_usd", 0.0),
                            pool_sol=candle_dict.get("pool_sol", 0.0),
                        ))
                    # The chart consumes the PRIMARY engine's payload; when a
                    # fleet is active, sibling engines' live_trade blocks are
                    # attached under `fleet` (per-engine positions + stats).
                    strategy_result = fleet_results[0]
                    if len(traders) > 1:
                        strategy_result = dict(strategy_result)
                        strategy_result["fleet"] = [
                            {
                                "engine_version": ev,
                                "live_trade": (fr.get("live_trade") or {}),
                            }
                            for ev, fr in zip(self.engine_versions, fleet_results)
                        ]
                    self.last_strategy_result = strategy_result

                    trade_payload = None if is_synthetic else {
                        "price": trade["price"],
                        "sol_amount": trade["sol_amount"],
                        "tx_type": trade["tx_type"],
                        "trader": trade["trader"],
                        "tx_hash": trade["tx_hash"],
                    }

                    if (last_sent_price is None or current_price != last_sent_price
                            or is_new):
                        self.broadcast({
                            "type": "candle",
                            "candle": candle_dict,
                            "is_new": is_new,
                            "market_cap_sol": trade.get("market_cap_sol", 0),
                            "market_cap_usd": trade.get("market_cap_usd", 0),
                            "trade": trade_payload,
                            "strategy": strategy_result,
                        })
                    last_sent_price = current_price

                    # ── Market-cap safety floor check ─────────────────────────
                    # update_market_cap() blocks until any emergency sell has
                    # fully settled on-chain (or the MCAP_STOP_SELL_TIMEOUT_SECONDS
                    # backstop fires), so by the time it returns True the wallet
                    # is empty and it is safe to terminate immediately — no
                    # grace sleep needed.
                    mcap_usd = trade.get("market_cap_usd", 0)
                    if mcap_usd:
                        _mcap_stop = False
                        for _tr in traders:
                            # iter77 fleet: every engine sees the same mcap
                            # floor; the FIRST breach stops the session after
                            # ALL engines' positions are emergency-sold.
                            if await _tr.update_market_cap(float(mcap_usd)):
                                _mcap_stop = True
                        if _mcap_stop:
                            logger.warning(
                                f"[LIVE] Market cap floor triggered for {real_mint[:8]}… — "
                                f"emergency sell settled, stopping session"
                            )
                            self.stop("mcap_floor")
                            break

                    # ── No-motion stop check ─────────────────────────────────
                    # Only fires when idle (no position, no pending signals) —
                    # never interrupts an active position.
                    if any(_tr.no_motion_stop_triggered for _tr in traders):
                        logger.warning(
                            f"[LIVE] No-motion stop triggered for {real_mint[:8]}… — "
                            f"session idle, shutting down"
                        )
                        self.stop("no_motion")
                        break
                return got_trade

            async def stream_live():
                got = await _process_stream(ws_client)
                if not got and not self.cancelled.is_set() and not isinstance(ws_client, PumpFunWSClient):
                    fallback = PumpFunWSClient(real_mint)
                    try:
                        await _process_stream(fallback)
                    finally:
                        fallback.stop()

            async def _shutdown():
                await self.cancelled.wait()
                ws_client.stop()

            await asyncio.gather(stream_live(), _shutdown())
            if not self.cancelled.is_set():
                # The market stream itself died (source disconnect).  An
                # unattended position with no data feed must not be held —
                # stop the session; the finally block emergency-sells.
                logger.warning(f"[LIVE] Market stream ended for {real_mint[:8]}… — stopping session")
                self.stop("stream_ended")
        except asyncio.CancelledError:
            # Server shutdown — let the finally block run its cleanup
            if not self.cancelled.is_set():
                self.stop_reason = "server_shutdown"
        except Exception:
            # Unexpected runner crash — mark the reason, teardown still runs
            logger.exception(f"[LIVE] Session runner crashed  mint={real_mint[:8]}…")
            if not self.cancelled.is_set():
                self.stop_reason = "runner_error"
        finally:
            ws_client.stop()

            # Stop the holder-flow background pump
            _hf_stop.set()
            if _hf_pump_task is not None:
                _hf_pump_task.cancel()
                try:
                    await _hf_pump_task
                except (asyncio.CancelledError, Exception):
                    pass

            # Stop the no-motion watcher
            if _nm_watch_task is not None:
                _nm_watch_task.cancel()
                try:
                    await _nm_watch_task
                except (asyncio.CancelledError, Exception):
                    pass

            # Stop the holder-flow monitor for this session
            if holder_monitor is not None:
                try:
                    holder_monitor.unwatch_token(real_mint)
                    await holder_monitor.stop()
                except Exception:
                    pass

            # ── Emergency position cleanup before session teardown ──────────
            # If a position is still open when the session ends, execute an
            # emergency sell to avoid leaving positions unattended.
            # iter77 fleet: cleanup every trader (each may hold its own open
            # position; sell-amount isolation keeps them independent).
            for _tr in traders:
                try:
                    await _tr.cleanup(reason=self.stop_reason or "session_ended")
                except Exception:
                    logger.exception("[LIVE] trader cleanup failed")

            # Finalize the auto-recording so it's available for backtesting
            data_store.stop_recording(rec_id)
            logger.info(f"[LIVE] Finalized recording {rec_id}")

            # Wake up any remaining viewers so their UIs reflect the end
            self.broadcast({"type": "session_ended", "reason": self.stop_reason or "session_ended"})
            # iter77 fleet: completed-session stats aggregate every engine.
            # iter95: wallet-truth fields aggregate alongside the model PnL.
            _fleet_stats = {
                "total_trades": 0, "total_pnl_sol": 0.0,
                "winning_trades": 0, "losing_trades": 0, "engines": [],
                "total_cash_pnl_sol": 0.0, "total_fees_sol": 0.0,
                "total_rent_sol": 0.0, "wallet_delta_sol": 0.0,
                "wallet_delta_ex_rent_sol": 0.0,
            }
            for ev, _tr in zip(self.engine_versions, traders):
                _s = _tr.stats.to_dict()
                _fleet_stats["total_trades"] += int(_s.get("total_trades", 0))
                _fleet_stats["total_pnl_sol"] += float(_s.get("total_pnl_sol", 0.0))
                _fleet_stats["winning_trades"] += int(_s.get("winning_trades", 0))
                _fleet_stats["losing_trades"] += int(_s.get("losing_trades", 0))
                _fleet_stats["total_cash_pnl_sol"] += float(_s.get("total_cash_pnl_sol", 0.0))
                _fleet_stats["total_fees_sol"] += float(_s.get("total_fees_sol", 0.0))
                _fleet_stats["total_rent_sol"] += float(_s.get("total_rent_sol", 0.0))
                _fleet_stats["wallet_delta_sol"] += float(_s.get("wallet_delta_sol", 0.0))
                _fleet_stats["wallet_delta_ex_rent_sol"] += float(
                    _s.get("wallet_delta_ex_rent_sol", 0.0))
                _fleet_stats["engines"].append({"engine_version": ev, "stats": _s})
            _completed_live_sessions.append({
                "mint": real_mint,
                "token_name": self.token_name,
                "token_symbol": self.token_symbol,
                "stats": _fleet_stats,
                "rec_id": rec_id,
                "ended_at": time.time(),
            })
            if _active_live_traders.get(real_mint) is self:
                del _active_live_traders[real_mint]
            logger.info(
                f"[LIVE] Session ended  mint={real_mint[:8]}…  reason={self.stop_reason or 'session_ended'}"
            )


# ── New Pairs state ─────────────────────────────────────────────────────────
# New-pair sessions are SERVER-SIDED pure recorders for newly-born pump.fun
# tokens (pre-migration / bonding-curve phase).  They mirror _LiveSession's
# detachable-viewer architecture exactly — sessions survive tab closes, tabs
# attach as viewers over /ws/newpairs/{mint} — with three deliberate
# differences:
#   1. NO strategy engine is attached anywhere (no LiveTrader, no ForwardTester,
#      no engine_factory): the runner is stream → CandleAggregator → DB.
#   2. Recordings persist to a SEPARATE database (newpairs_data.db via
#      newpairs_store) so the newborn-token regime never contaminates the
#      migrated-token research corpus in price_data.db.
#   3. Termination policy is the NO-MOTION stop only (default 120 s without a
#      price change).  There is NO market-cap floor — newborn tokens routinely
#      sit below any floor while still printing valid price action.

# Silence-gated DexScreener fallback: PumpPortal per-mint trades can be
# IP-throttled for hours while the birth feed keeps flowing (measured
# 2026-08-30).  When no price change has been seen for this many seconds, the
# fallback poller (below) takes over at a 5 s cadence until trades return.
NP_FALLBACK_AFTER_S = 20.0   # PumpPortal silence gate
NP_FALLBACK_POLL_S = 5.0     # poll cadence once engaged


class _NewPairSession:
    """Server-side recording session for a newly-born pump.fun token.

    Same viewer fan-out / control-socket contract as _LiveSession, minus the
    trading: every candle is broadcast to viewers and persisted to
    newpairs_data.db; the session ends on manual stop, stream end, or the
    no-motion stop (2 minutes without a price change — the ONLY stop policy).
    """

    def __init__(self, *, real_mint: str, token_name: str, token_symbol: str,
                 timeframe: str, token_info: Optional[dict],
                 no_motion_stop_seconds: float = 120.0,
                 birth_meta: Optional[dict] = None):
        self.real_mint = real_mint
        self.token_name = token_name
        self.token_symbol = token_symbol
        self.timeframe = timeframe
        self.token_info = token_info
        self.no_motion_stop_seconds = no_motion_stop_seconds
        self.cancelled = asyncio.Event()
        self.warmed = asyncio.Event()      # set once the (empty) warmup phase passed
        self.subscribers: set[asyncio.Queue] = set()
        self.task: Optional[asyncio.Task] = None
        self.rec_id: Optional[int] = None
        self.warmup_candles: list[dict] = []
        self.stop_reason: str = ""
        # Price-action state surfaced to viewers
        self.last_price: float = 0.0
        self.first_price: float = 0.0
        self.last_candle: Optional[dict] = None
        self.trade_count: int = 0
        self.peak_price: float = 0.0
        self.candle_count: int = 0
        self._last_motion_ts: float = time.time()
        self._last_motion_price: float = 0.0
        self.birth_meta = birth_meta or {}

    # ── viewer fan-out (identical contract to _LiveSession) ────────────────

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def broadcast(self, obj) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(obj)
            except asyncio.QueueFull:
                pass

    def stop(self, reason: str = "manual_stop") -> None:
        if not self.cancelled.is_set():
            self.stop_reason = reason
            self.cancelled.set()

    # ── session runner ────────────────────────────────────────────────────

    async def run(self) -> None:
        real_mint = self.real_mint
        timeframe = self.timeframe
        logger.info(
            f"[NEWPAIR] Session start  mint={real_mint[:8]}…  tf={timeframe}  "
            f"no_motion_stop={self.no_motion_stop_seconds:.0f}s"
        )

        # Newborn tokens are on the pump.fun bonding curve by definition —
        # PumpPortal's token-trade stream is the only correct live source
        # (PumpSwap RPC requires a migrated pool; DexScreener rarely indexes
        # second-old tokens).
        ws_client = PumpFunWSClient(real_mint)
        aggregator = CandleAggregator(timeframe)

        # ── Birth-candle seed ───────────────────────────────────────────────
        # The creation event carries the token's first real price and volume
        # (the dev's create-buy).  Emit it as the first candle so every
        # recording starts at birth, even for stillborn tokens that never
        # print another trade.
        _b = self.birth_meta or {}
        _b_price = 0.0
        try:
            _v_sol = float(_b.get("pool_sol", 0.0) or 0.0)
            _b_mcap_sol = float(_b.get("market_cap_sol", 0.0) or 0.0)
            if _b_mcap_sol > 0:
                _b_price = _b_mcap_sol / 1_000_000_000.0
            elif _v_sol > 0:
                _b_price = _v_sol / 793.1   # pump.fun curve: 793.1 SOL fill ≈ $69k graduation
        except (TypeError, ValueError):
            _b_price = 0.0
        if _b_price > 0:
            _b_ts = float(_b.get("first_seen", 0) or time.time())
            candle, is_new = aggregator.process_trade(
                _b_price, float(_b.get("initial_sol", 0.0) or 0.0), _b_ts,
                synthetic=False, is_buy=True,
                pool_sol=_v_sol,
                market_cap_usd=float(_b.get("market_cap_usd", 0.0) or 0.0),
            )
            _cd = candle.to_dict()
            newpairs_store.insert_candle(
                self.rec_id, _cd["time"],
                _cd["open"], _cd["high"], _cd["low"], _cd["close"],
                _cd.get("volume", 0), _cd.get("buy_volume", 0.0),
                _cd.get("sell_volume", 0.0), _cd.get("pool_sol", 0.0),
                _cd.get("market_cap_usd", 0.0),
            )
            self.first_price = _cd["close"]
            self.last_price = _cd["close"]
            self.peak_price = _cd["close"]
            self.trade_count += 1
            self.candle_count += 1
            self.last_candle = _cd
            self._last_motion_ts = time.time()
            self._last_motion_price = _cd["close"]
            self.broadcast({
                "type": "candle",
                "candle": _cd,
                "is_new": True,
                "market_cap_sol": _b.get("market_cap_sol", 0.0),
                "market_cap_usd": _b.get("market_cap_usd", 0.0),
                "trade": None,
            })
            logger.info(f"[NEWPAIR] Birth candle seeded at {_b_price:.3e} SOL for {real_mint[:8]}…")

        # ── No-motion stop (the ONLY termination policy) ────────────────────
        # Fires when no price change has been observed for
        # no_motion_stop_seconds.  Time-driven (not tick-driven) so a token
        # whose trade stream goes completely silent still terminates — a
        # tick-loop-only check would starve exactly when it is needed.
        async def _no_motion_watch():
            while not self.cancelled.is_set():
                await asyncio.sleep(5.0)
                if self.cancelled.is_set():
                    return
                if (time.time() - self._last_motion_ts) > self.no_motion_stop_seconds:
                    logger.warning(
                        f"[NEWPAIR] No-motion stop for {real_mint[:8]}… — "
                        f"no price change for {self.no_motion_stop_seconds:.0f}s, ending session"
                    )
                    self.stop("no_motion")
                    return

        nm_task = asyncio.ensure_future(_no_motion_watch())

        # ── Silence-gated DexScreener fallback poller ───────────────────────
        # Newborns appear on DexScreener ~45–70 s after birth.  This poller
        # only speaks when PumpPortal's trade stream has been silent for
        # _NP_FALLBACK_AFTER_S seconds, so it adds zero overhead whenever
        # PumpPortal delivers trades normally — but when the per-mint trade
        # stream is throttled (measured: births flow while trades deliver
        # nothing), the recording still captures price action.
        async def _ds_fallback_loop():
            import aiohttp
            async with aiohttp.ClientSession() as http:
                while not self.cancelled.is_set():
                    await asyncio.sleep(NP_FALLBACK_POLL_S)
                    if self.cancelled.is_set():
                        return
                    # Gate: only when PumpPortal has been silent.
                    if (time.time() - self._last_motion_ts) < NP_FALLBACK_AFTER_S:
                        continue
                    try:
                        pair = await get_ds_pair_by_mint(http, real_mint)
                        if not pair:
                            continue  # not indexed yet — common for newborns
                        price = float(pair.get("priceNative") or 0)
                        mcap_usd = float(pair.get("marketCap") or pair.get("fdv") or 0)
                        if price <= 0:
                            continue
                        candle, is_new = aggregator.process_trade(
                            price, 0.0, time.time(),
                            synthetic=True, is_buy=None,
                            market_cap_usd=mcap_usd,
                        )
                        _cd = candle.to_dict()
                        newpairs_store.insert_candle(
                            self.rec_id, _cd["time"],
                            _cd["open"], _cd["high"], _cd["low"], _cd["close"],
                            _cd.get("volume", 0), _cd.get("buy_volume", 0.0),
                            _cd.get("sell_volume", 0.0), _cd.get("pool_sol", 0.0),
                            _cd.get("market_cap_usd", 0.0),
                        )
                        if is_new:
                            self.candle_count += 1
                        if self.first_price <= 0:
                            self.first_price = price
                        self.last_price = price
                        self.peak_price = max(self.peak_price, price)
                        self.last_candle = _cd
                        # A REAL price change (even from a poll) is motion —
                        # it refreshes the no-motion clock, because it is the
                        # token's price actually moving.
                        if price != self._last_motion_price:
                            self._last_motion_price = price
                            self._last_motion_ts = time.time()
                        self.broadcast({
                            "type": "candle",
                            "candle": _cd,
                            "is_new": is_new,
                            "market_cap_sol": 0,
                            "market_cap_usd": mcap_usd,
                            "trade": None,
                        })
                    except asyncio.CancelledError:
                        return
                    except Exception:
                        pass  # transient network blip — retry next cycle

        ds_task = asyncio.ensure_future(_ds_fallback_loop())

        # No historical warmup for newborns: second-old tokens have no
        # candlestick history (and none is needed — no engine to warm).
        self.warmed.set()

        try:
            async def _process_stream(client) -> bool:
                got_trade = False
                async for trade in client.stream():
                    if self.cancelled.is_set():
                        break
                    got_trade = True
                    is_synthetic = bool(trade.get("synthetic"))
                    is_buy_np: Optional[bool] = None
                    if not is_synthetic:
                        tx_np = trade.get("tx_type", "")
                        if tx_np == "buy":
                            is_buy_np = True
                        elif tx_np == "sell":
                            is_buy_np = False
                        self.trade_count += 1
                    candle, is_new = aggregator.process_trade(
                        trade["price"], trade["sol_amount"], trade["timestamp"],
                        synthetic=is_synthetic,
                        is_buy=is_buy_np,
                        pool_sol=trade.get("pool_sol", 0.0),
                        market_cap_usd=trade.get("market_cap_usd", 0.0),
                    )
                    candle_dict = candle.to_dict()
                    current_price = candle_dict["close"]

                    # Persist every tick to the auto-recording (same contract
                    # as the live recorder so backtests replay exactly this).
                    ct = candle_dict["time"]
                    newpairs_store.insert_candle(
                        self.rec_id, ct,
                        candle_dict["open"], candle_dict["high"],
                        candle_dict["low"], candle_dict["close"],
                        candle_dict.get("volume", 0),
                        candle_dict.get("buy_volume", 0.0),
                        candle_dict.get("sell_volume", 0.0),
                        candle_dict.get("pool_sol", 0.0),
                        candle_dict.get("market_cap_usd", 0.0),
                    )

                    # Motion tracking (price CHANGE, not mere tick arrival).
                    if self._last_motion_price != current_price:
                        self._last_motion_price = current_price
                        self._last_motion_ts = time.time()

                    # Price-action state for the viewer cards.
                    if self.first_price <= 0:
                        self.first_price = current_price
                    self.last_price = current_price
                    self.peak_price = max(self.peak_price, current_price)
                    self.last_candle = candle_dict
                    if is_new:
                        self.candle_count += 1

                    self.broadcast({
                        "type": "candle",
                        "candle": candle_dict,
                        "is_new": is_new,
                        "market_cap_sol": trade.get("market_cap_sol", 0),
                        "market_cap_usd": trade.get("market_cap_usd", 0),
                        "trade": None if is_synthetic else {
                            "price": trade["price"],
                            "sol_amount": trade["sol_amount"],
                            "tx_type": trade["tx_type"],
                            "trader": trade["trader"],
                            "tx_hash": trade["tx_hash"],
                        },
                    })
                return got_trade

            async def _shutdown():
                await self.cancelled.wait()
                ws_client.stop()

            await asyncio.gather(_process_stream(ws_client), _shutdown())
            if not self.cancelled.is_set():
                # Stream died on its own (source disconnect).
                logger.warning(f"[NEWPAIR] Stream ended for {real_mint[:8]}… — ending session")
                self.stop("stream_ended")
        except asyncio.CancelledError:
            if not self.cancelled.is_set():
                self.stop_reason = "server_shutdown"
        except Exception:
            logger.exception(f"[NEWPAIR] Session runner crashed  mint={real_mint[:8]}…")
            if not self.cancelled.is_set():
                self.stop_reason = "runner_error"
        finally:
            ws_client.stop()
            for t in (nm_task, ds_task):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

            # Finalize the recording (no position cleanup — nothing was ever
            # traded; recording finalisation is the whole teardown).
            newpairs_store.stop_recording(self.rec_id)

            self.broadcast({"type": "session_ended", "reason": self.stop_reason or "session_ended"})
            _completed_newpair_sessions.append({
                "mint": real_mint,
                "token_name": self.token_name,
                "token_symbol": self.token_symbol,
                "rec_id": self.rec_id,
                "candles": self.candle_count,
                "trades": self.trade_count,
                "ended_at": time.time(),
                "stop_reason": self.stop_reason or "session_ended",
            })
            if _active_newpair_sessions.get(real_mint) is self:
                del _active_newpair_sessions[real_mint]
            logger.info(
                f"[NEWPAIR] Session ended  mint={real_mint[:8]}…  reason={self.stop_reason or 'session_ended'}"
            )


async def _get_or_create_newpair_session(
    *,
    real_mint: str,
    timeframe: str = "1s",
    token_name: str = "",
    token_symbol: str = "",
    token_info: Optional[dict] = None,
    birth_meta: Optional[dict] = None,
) -> tuple[Optional["_NewPairSession"], bool]:
    """Get an existing new-pair recording session or create a new one.

    Returns (session, created_flag).  Unlike live sessions, NO private key and
    NO buy size are required — nothing is traded.  Backpressure (max concurrent
    sessions + per-mint cooldown) is enforced by the caller (feed wrapper or
    the manual-start endpoint).
    """
    async with _newpair_session_lock:
        session = _active_newpair_sessions.get(real_mint)
        if session is not None and not session.cancelled.is_set():
            return session, False

        no_motion = _newpairs_feed.config.no_motion_stop_seconds
        session = _NewPairSession(
            real_mint=real_mint,
            token_name=token_name,
            token_symbol=token_symbol,
            timeframe=timeframe,
            token_info=token_info,
            no_motion_stop_seconds=no_motion if no_motion > 0 else 120.0,
            birth_meta=birth_meta,
        )
        meta = birth_meta or {}
        session.rec_id = newpairs_store.create_recording(
            real_mint, timeframe, token_name, token_symbol,
            creator=meta.get("creator", ""),
            twitter=meta.get("twitter", ""),
            telegram=meta.get("telegram", ""),
            website=meta.get("website", ""),
            initial_sol=float(meta.get("initial_sol", 0.0) or 0.0),
            market_cap_sol0=float(meta.get("market_cap_sol", 0.0) or 0.0),
            global_fees_sol=float(meta.get("global_fees_sol", 0.0) or 0.0),
        )
        logger.info(f"[NEWPAIR] Auto-recording candles → recording {session.rec_id}")
        _active_newpair_sessions[real_mint] = session
        session.task = asyncio.create_task(session.run())
        return session, True


async def _get_or_create_live_session(
    *,
    real_mint: str,
    private_key: str = "",
    buy_size: Optional[float] = None,
    slippage_bps: int = 1000,
    skip_sim: bool = True,
    engine_params: Optional[dict] = None,
    engine_version: int = 2,
    engine_versions: Optional[list[int]] = None,
    timeframe: str = "1s",
    token_name: str = "",
    token_symbol: str = "",
    token_info: Optional[dict] = None,
) -> tuple[Optional["_LiveSession"], bool]:
    """Get an existing server-side live session or create a new one.

    Returns (session, created_flag). If no session exists and no valid private
    key is provided or stored in backend memory, returns (None, False).
    ``buy_size`` has NO default: creating a session without an explicit,
    user-supplied size (> 0, sourced from the dashboard input field) is
    refused — a hard-coded size must never be traded.
    """
    async with _live_session_lock:
        session = _active_live_traders.get(real_mint)
        if session is not None and not session.cancelled.is_set():
            return session, False

        # ── Attach-only guard — never spawn a new session at defaults ─────
        # Re-attaching viewers (page reload, tab sleep, /api/live/status
        # re-probe) never pass a private key.  Without this guard, the
        # stored-key fallback below silently CREATED a new session at
        # endpoint defaults whenever the intended session had ended between
        # the status probe and this connect — the "live trades always enter
        # a hard-coded size" bug.  Creating a session therefore requires an
        # explicit key from the caller; a bare attach can only ever attach.
        if not private_key:
            return None, False

        # Buy size must come from the dashboard input field (pushed through
        # the WS query param or the /api/live/buy_size store for autofeed).
        # No fallback default exists or ever may exist here.
        if buy_size is None or buy_size <= 0:
            logger.warning(
                f"[LIVE] Refusing to create session for {real_mint[:8]}… — "
                f"no valid buy_size supplied from the dashboard input field"
            )
            return None, False

        # Fallback to server-side stored private key if none passed in call
        key_str = private_key or getattr(app.state, "lt_private_key", "") or _load_backend_private_key()
        if not key_str:
            return None, False

        try:
            keypair = keypair_from_private_key(key_str)
        except Exception as e:
            logger.warning(f"[LIVE] Invalid private key for session creation: {e}")
            return None, False

        # Keep server-side memory synced with working key
        app.state.lt_private_key = key_str
        app.state.lt_wallet_connected = True

        if token_info is None or not token_name:
            try:
                async with _resolve_sem:
                    real_mint, fetched_info = await asyncio.wait_for(
                        resolve_input(real_mint), timeout=20.0
                    )
                    token_info = token_info or fetched_info
            except Exception:
                pass

        token_name = token_name or (token_info or {}).get("name", "")
        token_symbol = token_symbol or (token_info or {}).get("symbol", "")

        # ── iter77 multi-engine fleet construction ─────────────────────────
        # `engine_versions` (comma-separated or list from the dashboard) runs
        # N engines together on this token: one LiveTrader per engine, all
        # sharing the wallet/stream/recording, each sized buy_size/N so the
        # TOTAL notional matches the dashboard field.  Duplicates collapse;
        # an empty/invalid list falls back to the single `engine_version`.
        fleet_versions = []
        if engine_versions:
            if isinstance(engine_versions, str):
                fleet_versions = [
                    int(v) for v in engine_versions.split(",")
                    if str(v).strip().isdigit()
                ]
            else:
                fleet_versions = [
                    int(v) for v in engine_versions
                    if str(v).strip().isdigit()
                ]
        seen = set()
        fleet_versions = [v for v in fleet_versions
                          if not (v in seen or seen.add(v))]
        # Only KNOWN engine versions may join the fleet (1/2/3/4/6) — an
        # unknown digit must fail session creation loudly at the factory,
        # not produce a confusing ValueError inside the runner.
        fleet_versions = [v for v in fleet_versions if v in (1, 2, 3, 4, 6)]
        # The primary engine must appear exactly ONCE — strip it from the
        # fleet list (a duplicate primary would create two same-version
        # traders on split capital).
        fleet_versions = [v for v in fleet_versions if v != engine_version]
        n_engines = 1 + len(fleet_versions)

        from calibration_startup import (
            initialize_calibration, strip_stale_sde_echo)
        from live_session_logger import _code_commit, _code_dirty
        calibration_started_at = time.time()
        primary_kwargs = dict(engine_params or {})
        calibration_audit = {}
        if engine_version == 2 or 2 in fleet_versions:
            # ── iter101 CALIBRATION PARITY: stale-echo strip (live-only) ────
            # See strip_stale_sde_echo (calibration_startup.py): the
            # dashboard mirror echoes the previous session's calibrated SDE
            # coefficients into the launch payload; left in place they win
            # the calibration merge and the session fights the last tape
            # with stale physics (Sep-29 divergence, 8/13 coefficients).
            # Production backtests calibrate from `{}` — live now does too.
            # Policy knobs (delays, gates, gain_retrace_arm_pct) untouched.
            _primary_kwargs, _stripped = strip_stale_sde_echo(primary_kwargs)
            primary_kwargs, calibration_audit = await initialize_calibration(
                str(real_mint), calibration_started_at, _primary_kwargs,
            )
            if _stripped:
                calibration_audit['stripped_stale_echo'] = _stripped
                logger.warning(
                    "[Calibration] %s stripped %d stale mirror SDE echo(s) "
                    "before calibration: %s", real_mint[:8],
                    len(_stripped),
                    {k: round(v, 6) if isinstance(v, float) else v
                     for k, v in _stripped.items()})
        logger.info("[Calibration] %s %s", real_mint[:8], calibration_audit)
        # ── iter101 DEPLOY GUARD: stale-code visibility at session open ────
        # The journal already stamps code_commit/code_dirty into
        # session_open; this makes a stale/dirty process LOUD at open time
        # instead of silent (INU Sep-29: a session on dirty pre-iter94 code
        # traded on the raw-vault price basis).  Deliberately does NOT
        # block — the operator decides; the journal records the truth.
        _open_commit, _open_dirty = _code_commit(), _code_dirty()
        if _open_dirty or _open_commit in ("", "unknown"):
            logger.warning(
                "[DEPLOY GUARD] %s session opening with %s CODE "
                "(commit=%s, dirty=%s) — live-vs-BT parity not guaranteed; "
                "restart main.py at HEAD and hard-refresh after every deploy.",
                real_mint[:8],
                "DIRTY" if _open_dirty else "UNKNOWN-COMMIT",
                _open_commit, _open_dirty)
        live_trader = LiveTrader(
            token_mint=real_mint,
            keypair=keypair,
            buy_size_sol=buy_size / n_engines,
            slippage_bps=slippage_bps,
            priority_fee_lamports=100_000,
            engine_kwargs=primary_kwargs,
            skip_simulation=skip_sim,
            engine_version=engine_version,
            # User policy (2026-08-26): terminate motionless coins to free
            # hardware.  Fires ONLY while fully idle (no position / pending
            # signal / swap), so teardown never needs an emergency sell; the
            # auto-recording is finalised by the normal session teardown.
            no_motion_stop_seconds=120.0,
            calibration_audit=calibration_audit,
        )
        extra_traders: list[LiveTrader] = []
        for ev in fleet_versions:
            extra_traders.append(LiveTrader(
                token_mint=real_mint,
                keypair=keypair,
                buy_size_sol=buy_size / n_engines,
                slippage_bps=slippage_bps,
                priority_fee_lamports=100_000,
                engine_kwargs=dict(primary_kwargs),   # fresh dict per engine
                skip_simulation=skip_sim,
                engine_version=ev,
                no_motion_stop_seconds=120.0,
                calibration_audit=calibration_audit,
            ))
        live_trader.start_watchdog()
        for _tr in extra_traders:
            _tr.start_watchdog()

        all_versions = [engine_version] + fleet_versions
        session = _LiveSession(
            real_mint=real_mint,
            token_name=token_name,
            token_symbol=token_symbol,
            timeframe=timeframe,
            trader=live_trader,
            token_info=token_info,
            engine_version=engine_version,
            extra_traders=extra_traders,
            engine_versions=all_versions,
        )

        session.rec_id = data_store.create_recording(
            real_mint, timeframe, token_name, token_symbol,
            started_at=calibration_started_at,
        )
        session.calibration_audit = calibration_audit
        logger.info(f"[LIVE] Auto-recording candles → recording {session.rec_id}")
        live_trader.set_session_meta(
            recording_id=session.rec_id, token_name=token_name,
            token_symbol=token_symbol, timeframe=timeframe,
        )

        _active_live_traders[real_mint] = session
        session.task = asyncio.create_task(session.run())
        return session, True


def _autofeed_active_count() -> int:
    """Number of currently-active live traders (used for backpressure)."""
    return len(_active_live_traders)


async def _autofeed_forward(cand: Candidate):
    """Called by AutoFeed for each accepted candidate.
    Broadcasts to /ws/autofeed clients and auto-starts live trader server-side if
    private key is configured."""
    payload = {
        "type": "autofeed_candidate",
        "candidate": cand.to_dict(),
        "timestamp": time.time(),
    }
    n_clients = len(_autofeed_clients)
    pushed = 0
    for q in list(_autofeed_clients):
        try:
            q.put_nowait(payload)
            pushed += 1
        except asyncio.QueueFull:
            pass
    logger.info(f"[AutoFeed] Forward ${cand.symbol} to {pushed}/{n_clients} WS clients")

    # If backend has a private key, auto-spawn live trader session server-side.
    # Buy size comes exclusively from the dashboard input field (pushed via
    # POST /api/live/buy_size).  Without a pushed size we skip the spawn —
    # connected dashboards still create the session themselves through
    # /ws/live/{mint} carrying the field value in the query string.
    pk = getattr(app.state, "lt_private_key", "") or _load_backend_private_key()
    af_buy_size = _get_live_buy_size()
    if pk and af_buy_size is None:
        logger.warning(
            "[AutoFeed] No buy size pushed from the dashboard input field "
            "(POST /api/live/buy_size) — skipping server-side session spawn"
        )
    if pk and af_buy_size is not None:
        try:
            session, created = await _get_or_create_live_session(
                real_mint=cand.mint,
                private_key=pk,
                buy_size=af_buy_size,
                token_name=cand.name or "",
                token_symbol=cand.symbol or "",
                timeframe="1s",
            )
            if created:
                logger.info(f"[AutoFeed] Auto-started server-side session for ${cand.symbol} ({cand.mint[:8]}…)")
                # Tell connected dashboards a session now exists so they can
                # attach a viewer card WITHOUT needing the browser private key.
                # Without this push the UI only discovered sessions by polling
                # /api/live/status (page load / tab switch / WS close), so
                # autofeed sessions stayed invisible while recording candles.
                for q in list(_autofeed_clients):
                    try:
                        q.put_nowait({
                            "type": "session_started",
                            "mint": session.real_mint,
                            "token_name": session.token_name,
                            "token_symbol": session.token_symbol,
                            "timeframe": session.timeframe,
                            "engine_version": session.engine_version,
                            "timestamp": time.time(),
                        })
                    except asyncio.QueueFull:
                        pass
        except Exception as e:
            logger.error(f"[AutoFeed] Error auto-starting session for {cand.mint[:8]}…: {e}")


def _newpairs_active_count() -> int:
    """Number of currently-recording new-pair sessions (backpressure)."""
    return len(_active_newpair_sessions)


async def _newpairs_notify(cand: NewPairCandidate):
    """Feed callback: broadcast every accepted birth event to /ws/newpairs/feed
    viewers.  Called for ALL births — qualified or still awaiting the
    global-fees check — so the UI shows the full birth stream."""
    payload = {
        "type": "newpair_candidate",
        "candidate": cand.to_dict(),
        "timestamp": time.time(),
    }
    for q in list(_newpairs_clients):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


async def _newpairs_spawn(cand: NewPairCandidate):
    """Feed callback: spawn a recording session for a QUALIFIED birth (fees
    gate passed, or gate disabled).  Backpressure (max concurrent sessions +
    per-mint cooldown) is enforced by the caller (feed)."""
    try:
        session, created = await _get_or_create_newpair_session(
            real_mint=cand.mint,
            timeframe=_newpairs_feed.config.session_timeframe,
            token_name=cand.name or "",
            token_symbol=cand.symbol or "",
            token_info=None,
            birth_meta=cand.to_dict(),
        )
        if created:
            _newpairs_feed.note_spawned(cand.mint)
            logger.info(
                f"[NewPairs] Recording ${cand.symbol} ({cand.mint[:8]}…) "
                f"dev_buy={cand.initial_sol:.2f} SOL fees_paid={cand.global_fees_sol:.3f} SOL "
                f"mcap≈${cand.market_cap_usd:.0f}"
            )
            for q in list(_newpairs_clients):
                try:
                    q.put_nowait({
                        "type": "session_started",
                        "mint": session.real_mint,
                        "token_name": session.token_name,
                        "token_symbol": session.token_symbol,
                        "timeframe": session.timeframe,
                        "timestamp": time.time(),
                    })
                except asyncio.QueueFull:
                    pass
    except Exception as e:
        logger.error(f"[NewPairs] Error auto-starting session for {cand.mint[:8]}…: {e}")


async def _autofeed_ws_handler(websocket: WebSocket):
    """Core autofeed WS logic — callable from both the dedicated route and the
    /ws/{mint} wildcard delegation."""
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    _autofeed_clients.add(q)
    logger.info(f"[AutoFeed WS] Client connected (total={len(_autofeed_clients)})")
    try:
        await websocket.send_json({"type": "autofeed_status", "data": _autofeed.snapshot()})
        # Send any already-tracked candidates for instant UI population
        for cand in list(_autofeed._seen.values())[-5:]:
            await websocket.send_json({
                "type": "autofeed_candidate",
                "candidate": cand.to_dict(),
                "timestamp": time.time(),
            })
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                # Heartbeat
                await websocket.send_json({"type": "ping"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"[AutoFeed WS] Error: {e}")
    finally:
        _autofeed_clients.discard(q)
        logger.info(f"[AutoFeed WS] Client disconnected (total={len(_autofeed_clients)})")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False, log_level="info", access_log=False)
