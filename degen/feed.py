"""DEGEN feed — new-pair + final-stretch scanner with Setuh's filter table.

Two discovery lanes on top of the shared PumpPortal infrastructure:

  * NEW-PAIRS lane   — births from ``NewPairsStream`` passing the Setuh table
                       (mcap band, age cap) get a per-mint tape watcher.
  * SOON/FINAL-STRETCH lane — a watched coin whose bonding-curve progress
                       crosses the 85-97% band while still young and above the
                       mcap floor is promoted to the research queue.

The feed also owns the per-mint tape trackers and the **trap registry**: the
agent (decision model) arms ``TrapMachine`` objects; the tape loop calls
``trap.on_trade()`` synchronously inside the same event handler, so the reflex
path from trade event → trigger decision costs zero awaits.  Firing is
dispatched to ``on_trap_trigger`` (the executor).

All thresholds are Setuh's, verified against captions (see RESEARCH.md §1.1);
SOL-anchored numbers are configurable because he retunes them as SOL moves.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from pumpfun_client import NewPairsStream, PumpFunWSClient  # backend plumbing

logger = logging.getLogger("degen.feed")

# ── Curve constants ──────────────────────────────────────────────────────────
# pump.fun graduation is a fixed curve event: ~85 SOL raised ⇒ mcap ≈ 410.7 SOL
# regardless of USD price (same constant family as autofeed's
# GRAD_MCAP_USD_PER_SOL).  PumpPortal's vSolInBondingCurve is the virtual SOL
# on the curve, so bonded-fraction ≈ vSol / 410.7 (0 at birth → 1 at migration).
CURVE_SOL_AT_GRAD = 410.7

DEXSCREENER_SOL = "https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112"
SOL_USD_FALLBACK = 170.0

CandidateFilter = Callable[[dict], bool]


# ── Config (Setuh day-4 table) ───────────────────────────────────────────────

@dataclass
class DegenConfig:
    # NEW-PAIRS lane
    new_pairs_min_mcap_usd: float = 3_500.0   # launch-price anchored; retune as SOL moves
    new_pairs_max_age_min: float = 6.0        # “age max 6” for the fresh-pairs page
    # SOON (final stretch) lane — his preferred venue
    soon_max_age_min: float = 30.0
    soon_pro_traders_min: int = 20            # needs GMGN enrichment (research.py); feed-only gate = None
    soon_min_mcap_usd: float = 6_000.0        # “the equivalent of 3,500 for our new pairs”
    soon_curve_min: float = 0.85              # bonded-fraction band for "about to graduate"
    soon_curve_max: float = 0.97
    # Migrated lane (pro traders ≥100, mcap ≥30k) handled by the agent on demand, not scanned here
    # Tape watching
    nuke_watch_drawdown: float = 0.35         # flag a research candidate at ≥35% off the peak
    max_watchers: int = 48                    # concurrent per-mint tape subscriptions
    watch_ttl_min: float = 35.0               # stop watching a birth after this
    stale_watch_s: float = 180.0              # no trades for this long → drop (unless armed/held)
    # USD conversion
    sol_usd_refresh_s: float = 60.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# ── Tape tracker ─────────────────────────────────────────────────────────────

@dataclass
class TapeBucket:
    second: int
    open: float
    high: float
    low: float
    close: float
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    trades: int = 0


class TapeTracker:
    """Rolling per-mint tape state maintained synchronously in the tape loop.

    Pure bookkeeping — no awaits, no I/O — so the trap path can share it at
    zero cost.  1-second buckets, lifetime peak/low since peak, curve fraction.
    """

    def __init__(self, mint: str, first_seen: float, bucket_seconds: int = 1,
                 max_buckets: int = 300):
        self.mint = mint
        self.first_seen = first_seen
        self.bucket_seconds = bucket_seconds
        self.buckets: deque[TapeBucket] = deque(maxlen=max_buckets)
        self.last_price: float = 0.0
        self.last_mcap_sol: float = 0.0
        self.curve_frac: float = 0.0          # vSol / CURVE_SOL_AT_GRAD (0→1, -1 unknown)
        self.pool_sol: float = 0.0
        self.peak_price: float = 0.0          # since first_seen
        self.peak_mcap_sol: float = 0.0
        self.low_since_peak: float = 0.0      # the nuke low once a drawdown starts
        self.drawdown: float = 0.0            # (peak - low_since_peak) / peak
        self.in_drawdown: bool = False
        self.last_trade_ts: float = 0.0
        self.graduated: bool = False
        self.up_tick_streak: int = 0
        self.recent_buy_sol: float = 0.0      # rolling 10 s
        self.recent_sell_sol: float = 0.0

    def age_s(self, now: Optional[float] = None) -> float:
        return (now or time.time()) - self.first_seen

    def update(self, trade: dict) -> None:
        ts = float(trade.get("timestamp") or time.time())
        price = float(trade.get("price") or 0.0)
        if price <= 0:
            return
        second = int(ts // self.bucket_seconds) * self.bucket_seconds
        sol = abs(float(trade.get("sol_amount") or 0.0))
        is_buy = str(trade.get("tx_type", "buy")).lower() == "buy"

        bucket = self.buckets[-1] if self.buckets and self.buckets[-1].second == second else None
        if bucket is None:
            bucket = TapeBucket(second=second, open=price, high=price, low=price, close=price)
            self.buckets.append(bucket)
        bucket.high = max(bucket.high, price)
        bucket.low = min(bucket.low, price)
        bucket.close = price
        bucket.trades += 1
        if is_buy:
            bucket.buy_sol += sol
        else:
            bucket.sell_sol += sol

        # rolling 10 s buy/sell pressure
        cutoff = second - 10
        rb = rs = 0.0
        for b in reversed(self.buckets):
            if b.second < cutoff:
                break
            rb += b.buy_sol
            rs += b.sell_sol
        self.recent_buy_sol, self.recent_sell_sol = rb, rs

        # price / peak / drawdown bookkeeping
        if self.last_price and price > self.last_price:
            self.up_tick_streak += 1
        elif price < self.last_price:
            self.up_tick_streak = 0

        if price >= self.peak_price:
            self.peak_price = price
            self.peak_mcap_sol = max(self.peak_mcap_sol, float(trade.get("market_cap_sol") or 0.0))
            self.low_since_peak = price
            self.in_drawdown = False
        else:
            if not self.in_drawdown:
                self.in_drawdown = True
                self.low_since_peak = price
            self.low_since_peak = min(self.low_since_peak, price)
        if self.peak_price > 0:
            self.drawdown = (self.peak_price - self.low_since_peak) / self.peak_price

        mcap_sol = float(trade.get("market_cap_sol") or 0.0)
        if mcap_sol > 0:
            self.last_mcap_sol = mcap_sol
        pool_sol = float(trade.get("pool_sol") or 0.0)
        if pool_sol > 0:
            self.pool_sol = pool_sol
            self.curve_frac = min(pool_sol / CURVE_SOL_AT_GRAD, 1.0)
        self.last_price = price
        self.last_trade_ts = ts

    def momentum_resume(self, up_ticks: int = 3, vol_flip: bool = True,
                        reclaim_pct: float = 0.0) -> bool:
        """Setuh: buy the momentum AFTER the nuke, never the falling knife.

        Confirmed when the tape shows a consecutive up-tick streak, buy pressure
        back above sell pressure over the rolling window, and (optionally) the
        price having reclaimed reclaim_pct off the nuke low.
        """
        if self.up_tick_streak < up_ticks:
            return False
        if vol_flip and self.recent_buy_sol <= self.recent_sell_sol:
            return False
        if reclaim_pct > 0 and self.peak_price > self.low_since_peak > 0:
            reclaim = (self.last_price - self.low_since_peak) / self.low_since_peak
            if reclaim < reclaim_pct:
                return False
        return True

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "mint": self.mint,
            "age_s": round(self.age_s(now), 1),
            "price": self.last_price,
            "mcap_sol": self.last_mcap_sol,
            "curve_frac": round(self.curve_frac, 4),
            "peak_price": self.peak_price,
            "drawdown": round(self.drawdown, 4),
            "low_since_peak": self.low_since_peak,
            "up_tick_streak": self.up_tick_streak,
            "buy_sol_10s": round(self.recent_buy_sol, 4),
            "sell_sol_10s": round(self.recent_sell_sol, 4),
            "pool_sol": self.pool_sol,
            "last_trade_ts": self.last_trade_ts,
            "graduated": self.graduated,
        }


# ── Birth record ─────────────────────────────────────────────────────────────

@dataclass
class RecentBirth:
    mint: str
    name: str = ""
    symbol: str = ""
    twitter: str = ""
    telegram: str = ""
    website: str = ""
    uri: str = ""
    creator: str = ""
    bonding_curve_key: str = ""
    ts: float = 0.0
    birth_mcap_sol: float = 0.0
    initial_buy_sol: float = 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


# ── The feed ─────────────────────────────────────────────────────────────────

class DegenFeed:
    """Births intake + tape watching + lane promotion + trap dispatch.

    Usage:
        feed = DegenFeed(config)
        feed.on_trap_trigger = my_async_executor   # fires armed traps
        await feed.start()
        cand = await feed.candidates.get()          # {'event': 'soon'|'nuke_watch'|…}
    """

    def __init__(self, config: Optional[DegenConfig] = None):
        self.config = config or DegenConfig()
        self.candidates: asyncio.Queue = asyncio.Queue(maxsize=256)
        self.on_trap_trigger: Optional[Callable[[dict], Awaitable[None]]] = None

        self.births: dict[str, RecentBirth] = {}
        self.trackers: dict[str, TapeTracker] = {}
        self._ws: dict[str, PumpFunWSClient] = {}
        self._tasks: set[asyncio.Task] = set()
        self._promoted: dict[str, float] = {}      # mint → last promotion ts per lane prefix
        self._traps: dict[str, list] = {}          # mint → [TrapMachine, …] (armed)
        self._stopped = False
        self._sol_usd: float = SOL_USD_FALLBACK
        self._sol_usd_ts: float = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._stopped = False
        self._spawn(self._births_loop())
        self._spawn(self._sol_price_loop())
        self._spawn(self._watchdog_loop())
        logger.info("[DegenFeed] started (max %d watchers)", self.config.max_watchers)

    async def stop(self) -> None:
        self._stopped = True
        for t in list(self._tasks):
            t.cancel()
        for ws in self._ws.values():
            ws.stop()
        self._tasks.clear()
        logger.info("[DegenFeed] stopped")

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── public surface ───────────────────────────────────────────────────

    def sol_usd(self) -> float:
        return self._sol_usd

    def tracker(self, mint: str) -> Optional[TapeTracker]:
        return self.trackers.get(mint)

    def register_trap(self, trap) -> bool:
        """Arm a trap (traps.TrapMachine).  Ensures a tape watcher exists.

        Synchronous and safe to call from async code; the trap starts seeing
        trades from the next tape event.
        """
        mint = trap.mint
        lst = self._traps.setdefault(mint, [])
        if any(t.trap_id == trap.trap_id for t in lst):
            return True
        lst.append(trap)
        if mint not in self._ws:
            self._start_watcher(mint, first_seen=time.time())
        logger.info("[DegenFeed] trap armed %s on %s…", trap.trap_id, mint[:8])
        return True

    def cancel_trap(self, mint: str, trap_id: str) -> bool:
        lst = self._traps.get(mint, [])
        for t in list(lst):
            if t.trap_id == trap_id:
                lst.remove(t)
                if not lst:
                    self._traps.pop(mint, None)
                logger.info("[DegenFeed] trap %s cancelled on %s…", trap_id, mint[:8])
                return True
        return False

    def armed_traps(self) -> list[dict]:
        out = []
        for mint, lst in self._traps.items():
            for t in lst:
                out.append(t.describe())
        return out

    def birth(self, mint: str) -> Optional[RecentBirth]:
        return self.births.get(mint)

    def snapshot(self) -> dict:
        """UI payload: watched coins + recent candidates + armed traps."""
        now = time.time()
        watched = []
        for mint, tr in list(self.trackers.items())[:200]:
            b = self.births.get(mint)
            snap = tr.snapshot()
            snap["symbol"] = (b.symbol if b else "")[:24]
            snap["name"] = (b.name if b else "")[:48]
            snap["mcap_usd"] = round(snap["mcap_sol"] * self._sol_usd, 0)
            snap["armed_traps"] = sum(
                1 for t in self._traps.get(mint, []) if getattr(t, "state", "") == "armed")
            watched.append(snap)
        watched.sort(key=lambda s: s["mcap_usd"], reverse=True)
        return {
            "ts": now,
            "sol_usd": self._sol_usd,
            "config": self.config.to_dict(),
            "watching": len(self.trackers),
            "watched": watched[:80],
            "traps": self.armed_traps(),
        }

    # ── lane gates (Setuh table) ──────────────────────────────────────────

    def _new_pairs_pass(self, b: RecentBirth, now: float) -> tuple[bool, str]:
        cfg = self.config
        mcap_usd = b.birth_mcap_sol * self._sol_usd
        if mcap_usd < cfg.new_pairs_min_mcap_usd:
            return False, f"mcap ${mcap_usd:,.0f} < ${cfg.new_pairs_min_mcap_usd:,.0f}"
        if b.ts and (now - b.ts) > cfg.new_pairs_max_age_min * 60:
            return False, "too old at discovery"
        return True, "ok"

    def _soon_pass(self, tr: TapeTracker, now: float) -> tuple[bool, str]:
        cfg = self.config
        if not (cfg.soon_curve_min <= tr.curve_frac <= cfg.soon_curve_max):
            return False, f"curve {tr.curve_frac:.2f} outside band"
        if tr.age_s(now) > cfg.soon_max_age_min * 60:
            return False, "older than soon window"
        if tr.last_mcap_sol * self._sol_usd < cfg.soon_min_mcap_usd:
            return False, "below soon mcap floor"
        return True, "ok"

    # ── background loops ──────────────────────────────────────────────────

    async def _births_loop(self) -> None:
        stream = NewPairsStream()
        backoff = 1.0
        while not self._stopped:
            try:
                async for ev in stream.stream():
                    if self._stopped:
                        break
                    try:
                        self._handle_birth(ev)
                        backoff = 1.0
                    except Exception:
                        logger.exception("[DegenFeed] birth handler failed")
            except asyncio.CancelledError:
                stream.stop()
                return
            except Exception as e:
                logger.warning("[DegenFeed] births stream %s — retry in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def _handle_birth(self, ev: dict) -> None:
        mint = ev.get("mint") or ""
        if not mint or mint in self.births:
            return
        now = time.time()
        b = RecentBirth(
            mint=mint,
            name=str(ev.get("name") or ""),
            symbol=str(ev.get("symbol") or ""),
            twitter=ev.get("twitter") or "",
            telegram=ev.get("telegram") or "",
            website=ev.get("website") or "",
            uri=ev.get("uri") or "",
            creator=ev.get("creator") or "",
            bonding_curve_key=ev.get("bondingCurveKey") or "",
            ts=float(ev.get("timestamp") or now),
            birth_mcap_sol=float(ev.get("marketCapSol") or 0.0),
            initial_buy_sol=float(ev.get("solAmount") or 0.0),
        )
        self.births[mint] = b
        # prune births older than 2 h
        if len(self.births) > 4_000:
            cutoff = now - 7_200
            for m in [m for m, x in self.births.items() if x.ts < cutoff]:
                self.births.pop(m, None)

        ok, why = self._new_pairs_pass(b, now)
        if not ok:
            return
        if len(self.trackers) >= self.config.max_watchers:
            self._evict_weakest()
        if len(self.trackers) >= self.config.max_watchers:
            return  # still full — drop quietly
        self._start_watcher(mint, first_seen=b.ts or now)
        self._emit("new_pair", mint, {"why": why, "birth": b.to_dict()})

    def _start_watcher(self, mint: str, first_seen: float) -> None:
        if mint in self._ws:
            return
        tr = TapeTracker(mint, first_seen=first_seen)
        self.trackers[mint] = tr
        ws = PumpFunWSClient(mint)
        self._ws[mint] = ws
        self._spawn(self._tape_loop(mint, ws))

    async def _tape_loop(self, mint: str, ws: PumpFunWSClient) -> None:
        tr = self.trackers[mint]
        try:
            async for trade in ws.stream():
                if self._stopped:
                    break
                # 1) traps first — synchronous, zero-await reflex path
                for trap in list(self._traps.get(mint, [])):
                    try:
                        fire = trap.on_trade(trade, tr)
                    except Exception:
                        logger.exception("[DegenFeed] trap %s error", getattr(trap, "trap_id", "?"))
                        fire = None
                    if fire and self.on_trap_trigger is not None:
                        self._spawn(self._dispatch_trigger(fire))
                # 2) tracker bookkeeping
                tr.update(trade)
                # 3) lane promotions
                self._check_promotions(mint, tr)
                if tr.graduated:
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self._ws.pop(mint, None)
            self.trackers.pop(mint, None)

    async def _dispatch_trigger(self, fire: dict) -> None:
        try:
            await self.on_trap_trigger(fire)
        except Exception:
            logger.exception("[DegenFeed] trap trigger dispatch failed")

    def _check_promotions(self, mint: str, tr: TapeTracker) -> None:
        now = time.time()
        if tr.graduated:
            return
        # SOON lane — final stretch
        ok, why = self._soon_pass(tr, now)
        if ok and self._once_per(mint, "soon", cooldown_s=3600):
            b = self.births.get(mint)
            self._emit("soon", mint, {
                "why": why,
                "symbol": b.symbol if b else "",
                "name": b.name if b else "",
                "tracker": tr.snapshot(),
            })
            return
        # NUKE-WATCH lane — the initial dip forming on a watched coin
        if tr.in_drawdown and tr.drawdown >= self.config.nuke_watch_drawdown:
            if self._once_per(mint, "nuke_watch", cooldown_s=1800):
                b = self.births.get(mint)
                self._emit("nuke_watch", mint, {
                    "why": f"drawdown {tr.drawdown:.0%} from peak",
                    "symbol": b.symbol if b else "",
                    "tracker": tr.snapshot(),
                })

    def _once_per(self, mint: str, lane: str, cooldown_s: float) -> bool:
        key = f"{lane}:{mint}"
        now = time.time()
        if now - self._promoted.get(key, 0) < cooldown_s:
            return False
        self._promoted[key] = now
        return True

    def _emit(self, event: str, mint: str, payload: dict) -> None:
        item = {"event": event, "mint": mint, "ts": time.time(), **payload}
        try:
            self.candidates.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self.candidates.get_nowait()   # drop oldest, keep newest
                self.candidates.put_nowait(item)
            except Exception:
                pass
        logger.info("[DegenFeed] candidate %s %s… (%s)", event, mint[:8],
                    payload.get("why", ""))

    # ── housekeeping ─────────────────────────────────────────────────────

    async def _watchdog_loop(self) -> None:
        """Evict stale/expired watchers; watch capacity is capped."""
        while not self._stopped:
            await asyncio.sleep(15)
            now = time.time()
            for mint, tr in list(self.trackers.items()):
                if mint in self._traps:          # armed traps keep their tape
                    continue
                if tr.age_s(now) > self.config.watch_ttl_min * 60:
                    self._drop_watcher(mint, "ttl")
                elif now - (tr.last_trade_ts or tr.first_seen) > self.config.stale_watch_s:
                    self._drop_watcher(mint, "stale")

    def _drop_watcher(self, mint: str, why: str) -> None:
        ws = self._ws.pop(mint, None)
        if ws:
            ws.stop()
        logger.debug("[DegenFeed] dropped watcher %s… (%s)", mint[:8], why)

    def _evict_weakest(self) -> None:
        """Drop the least interesting watcher (oldest, unarmed, not in drawdown)."""
        best, best_key = None, -1.0
        now = time.time()
        for mint, tr in self.trackers.items():
            if mint in self._traps:
                continue
            key = tr.age_s(now)          # oldest first
            if tr.in_drawdown or tr.curve_frac >= self.config.soon_curve_min:
                continue                  # keep interesting tapes
            if key > best_key:
                best, best_key = mint, key
        if best:
            self._drop_watcher(best, "evicted")

    async def _sol_price_loop(self) -> None:
        import aiohttp
        while not self._stopped:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
                    async with sess.get(DEXSCREENER_SOL) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            pairs = (data or {}).get("pairs") or []
                            best = max(
                                (p for p in pairs if p.get("quoteToken", {}).get("symbol") in ("USDC", "USDT")),
                                key=lambda p: float(p.get("liquidity", {}).get("usd") or 0),
                                default=None,
                            )
                            if best:
                                self._sol_usd = float(best.get("priceUsd") or self._sol_usd)
                                self._sol_usd_ts = time.time()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("[DegenFeed] sol price fetch failed: %s", e)
            await asyncio.sleep(self.config.sol_usd_refresh_s)
