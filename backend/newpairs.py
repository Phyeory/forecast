"""
NewPairs Feed — auto-feeding newly-born pump.fun tokens (pre-migration,
bonding-curve phase) into the New Pairs monitoring pipeline.

Design contract (must never be violated):
  * The NewPairs feed performs NO trading, NO swaps, NO order placement.
  * NO strategy engine is attached anywhere on this path.  Sessions are
    pure price-action recorders: stream → candles → newpairs_data.db.
  * The ONLY termination policy is the NO-MOTION stop (2 minutes without
    a price change).  There is deliberately NO market-cap floor: a
    newborn token routinely sits below any floor while still generating
    perfectly valid price action worth recording.

Discovery source — PumpPortal's `subscribeNewToken` WebSocket feed
(`NewPairsStream` in pumpfun_client.py): every token creation event on
pump.fun, pushed in real time with full birth metadata (name/symbol/socials/
dev's initial buy, market cap, bonding-curve reserves).  No third-party
indexers, no API keys, no rate limits.

Pre-migration guarantee: tokens arriving over subscribeNewToken are by
definition still on the pump.fun bonding curve (a token migrates to
PumpSwap only at ~$69k mcap after filling the curve — these are second-old
at delivery).  Feed-side filters below can additionally screen by initial
market cap / dev-buy size if desired; everything is hot-configurable over
REST like the gmgn AutoFeed.

Lifecycle mirrors autofeed.AutoFeed:
    start(forward_fn, active_count_fn) -> spawns the discovery task
    stop()                             -> cancels it
    set_config(partial_dict)           -> hot-update settings
The discovery loop is fully decoupled from the recording sessions it spawns:
if the feed stops, sessions keep recording until their own no-motion stop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, asdict
from typing import Optional, Callable, Awaitable

from pumpfun_client import NewPairsStream

logger = logging.getLogger("newpairs")


# ── Candidate record ──────────────────────────────────────────────────────────

@dataclass
class NewPairCandidate:
    mint:             str
    name:             str = ""
    symbol:           str = ""
    twitter:          str = ""
    telegram:         str = ""
    website:          str = ""
    creator:          str = ""
    initial_sol:      float = 0.0   # dev's initial buy (SOL)
    market_cap_sol:   float = 0.0   # market cap (SOL) at creation
    market_cap_usd:   float = 0.0   # approximate USD mcap at creation
    pool_sol:         float = 0.0   # vSolInBondingCurve (SOL)
    reason:           str = "pumpportal_new_token"
    first_seen:        float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def age_seconds(self) -> float:
        return time.time() - (self.first_seen or time.time())


# ── Settings ─────────────────────────────────────────────────────────────────

@dataclass
class NewPairsConfig:
    enabled:                       bool = False
    # ── Feed-side filters (all optional; defaults accept everything) ──────
    min_initial_buy_sol:           float = 0.0   # dev's initial buy ≥ this (SOL); 0 = no floor
    max_initial_buy_sol:           float = 0.0   # upper bound (SOL); 0 = no cap
    min_mcap_usd:                  float = 0.0   # birth market cap ≥ this; 0 = no floor
    max_mcap_usd:                  float = 0.0   # birth market cap ≤ this; 0 = no cap
    require_social:                bool = False  # at least one social link on the token
    exclude_mints:                 str = ""      # comma-separated manual exclusion
    # ── Session management ─────────────────────────────────────────────────
    max_concurrent_sessions:       int = 20      # cap on parallel recording sessions
    session_timeframe:             str = "1s"    # candle timeframe recorded per token
    # per-mint cooldown before the same mint can spawn another session (s)
    cooldown_seconds:              float = 3600.0
    # no-motion session stop: seconds without a price change that ends a
    # session.  The ONLY termination policy on this tab — no mcap floor.
    no_motion_stop_seconds:        float = 120.0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Feed engine ──────────────────────────────────────────────────────────────

class NewPairsFeed:
    """
    Discovery-only loop over PumpPortal's new-token stream.

    Every accepted birth event is forwarded through `forward_fn` (the
    main.py wiring spawns a recording session); `active_count_fn` supplies
    backpressure (number of live sessions).
    """

    def __init__(
        self,
        config: Optional[NewPairsConfig] = None,
        forward_fn: Optional[Callable[[NewPairCandidate], Awaitable[None]]] = None,
        active_count_fn: Optional[Callable[[], int]] = None,
    ):
        self.config = config or NewPairsConfig()
        self._forward_fn = forward_fn
        self._active_count_fn = active_count_fn
        self._task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()

        # State
        self._seen: dict[str, NewPairCandidate] = {}   # mint -> candidate (for dedupe/UI)
        self._last_fed: dict[str, float] = {}          # mint -> last spawn timestamp
        self.last_error: str = ""
        self.last_event_at: float = 0.0
        self.total_seen: int = 0
        self.total_fed: int = 0

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, forward_fn=None, active_count_fn=None):
        if forward_fn is not None:
            self._forward_fn = forward_fn
        if active_count_fn is not None:
            self._active_count_fn = active_count_fn

        if self._task and not self._task.done():
            logger.warning("[NewPairs] Already running — ignoring duplicate start()")
            return False

        self._stop_evt.clear()
        self._task = asyncio.ensure_future(self._loop())
        logger.info(
            f"[NewPairs] Started  min_dev_buy={self.config.min_initial_buy_sol} SOL  "
            f"mcap_usd=[{self.config.min_mcap_usd}, {self.config.max_mcap_usd or '∞'}]  "
            f"max_sessions={self.config.max_concurrent_sessions}  "
            f"no_motion_stop={self.config.no_motion_stop_seconds:.0f}s"
        )
        return True

    async def stop(self):
        self._stop_evt.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass
        self._task = None
        logger.info("[NewPairs] Stopped")

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done() and self.config.enabled)

    def set_config(self, partial: dict):
        changed = []
        for k, v in partial.items():
            if hasattr(self.config, k) and v is not None:
                old = getattr(self.config, k)
                if old != v:
                    setattr(self.config, k, v)
                    changed.append(k)
        return changed

    # ── filter ───────────────────────────────────────────────────────────────

    def _excluded_set(self) -> set[str]:
        raw = (self.config.exclude_mints or "").strip()
        return {x.strip().lower() for x in raw.split(",") if x.strip()}

    def _to_candidate(self, raw: dict) -> Optional[NewPairCandidate]:
        """Raw PumpPortal new-token event → candidate.  None if filtered out."""
        mint = (raw.get("mint") or "").strip()
        if not mint or len(mint) < 32 or len(mint) > 44:
            return None
        # Base58 mints are case-sensitive — exclude-set compared case-insensitively.
        if mint.lower() in self._excluded_set():
            return None

        cf = self.config

        initial_sol = float(raw.get("solAmount", 0) or 0)
        mcap_sol = float(raw.get("marketCapSol", 0) or 0)
        v_sol = float(raw.get("vSolInBondingCurve", 0) or 0)
        # PumpPortal carries marketCapSol on the creation event; approximate
        # USD via the SOL-side depth if a SOL/USD feed isn't handy.  0 = unknown.
        mcap_usd = mcap_sol * 150.0 if mcap_sol > 0 else 0.0

        # ── Numeric gates ──
        if cf.min_initial_buy_sol > 0 and initial_sol < cf.min_initial_buy_sol:
            return None
        if cf.max_initial_buy_sol > 0 and initial_sol > cf.max_initial_buy_sol:
            return None
        if cf.min_mcap_usd > 0 and mcap_usd > 0 and mcap_usd < cf.min_mcap_usd:
            return None
        if cf.max_mcap_usd > 0 and mcap_usd > cf.max_mcap_usd:
            return None

        # ── Boolean gates ──
        if cf.require_social and not (
            raw.get("twitter") or raw.get("telegram") or raw.get("website")
        ):
            return None

        return NewPairCandidate(
            mint=mint,
            name=(raw.get("name") or "").strip(),
            symbol=(raw.get("symbol") or "").strip(),
            twitter=raw.get("twitter") or "",
            telegram=raw.get("telegram") or "",
            website=raw.get("website") or "",
            creator=raw.get("creator") or "",
            initial_sol=initial_sol,
            market_cap_sol=mcap_sol,
            market_cap_usd=mcap_usd,
            pool_sol=v_sol,
        )

    # ── discovery loop ───────────────────────────────────────────────────────

    async def _loop(self):
        logger.info("[NewPairs] Discovery loop entered")
        stream = NewPairsStream()
        while not self._stop_evt.is_set():
            try:
                async for raw in stream.stream():
                    if self._stop_evt.is_set():
                        break
                    await self._handle_event(raw)
            except asyncio.CancelledError:
                logger.info("[NewPairs] Discovery loop cancelled")
                return
            except Exception as e:
                self.last_error = f"stream error: {e}"
                logger.error(f"[NewPairs] stream error: {e}", exc_info=True)
                await asyncio.sleep(1.0)
        stream.stop()
        logger.info("[NewPairs] Discovery loop exited")

    async def _handle_event(self, raw: dict):
        cand = self._to_candidate(raw)
        if cand is None:
            return
        self.total_seen += 1
        self.last_event_at = time.time()
        cand.first_seen = time.time()
        self._seen[cand.mint] = cand

        # Keep the seen-cache bounded (new tokens arrive at ~1/s peak).
        if len(self._seen) > 500:
            # Drop the oldest quarter by first_seen.
            ordered = sorted(self._seen.items(), key=lambda kv: kv[1].first_seen or 0)
            for m, _ in ordered[:125]:
                self._seen.pop(m, None)
                self._last_fed.pop(m, None)

        # Push to any attached viewer WS (status broadcast fan-out happens in
        # main.py's forward_fn wrapper).
        if self._forward_fn is None:
            return
        try:
            await self._forward_fn(cand)
        except Exception as e:
            self.last_error = f"forward error: {e}"
            logger.warning(f"[NewPairs] forward_fn error: {e}")

    def can_spawn_session(self, mint: str) -> bool:
        """Dedupe + backpressure check, shared by feed spawns and manual starts."""
        now = time.time()
        last = self._last_fed.get(mint, 0.0)
        if last > 0 and (now - last) < self.config.cooldown_seconds:
            return False
        active = 0
        if self._active_count_fn is not None:
            try:
                active = int(self._active_count_fn())
            except Exception:
                active = 0
        if active >= self.config.max_concurrent_sessions:
            return False
        return True

    def note_spawned(self, mint: str):
        self._last_fed[mint] = time.time()
        self.total_fed += 1

    # ── snapshot (REST/WS UI) ────────────────────────────────────────────────

    def snapshot(self) -> dict:
        recent = sorted(
            self._seen.values(), key=lambda c: c.first_seen or 0, reverse=True
        )
        return {
            "enabled": self.config.enabled,
            "is_running": self.is_running(),
            "min_initial_buy_sol": self.config.min_initial_buy_sol,
            "max_initial_buy_sol": self.config.max_initial_buy_sol,
            "min_mcap_usd": self.config.min_mcap_usd,
            "max_mcap_usd": self.config.max_mcap_usd,
            "require_social": self.config.require_social,
            "exclude_mints": [x.strip() for x in (self.config.exclude_mints or "").split(",") if x.strip()],
            "max_concurrent_sessions": self.config.max_concurrent_sessions,
            "session_timeframe": self.config.session_timeframe,
            "cooldown_seconds": self.config.cooldown_seconds,
            "no_motion_stop_seconds": self.config.no_motion_stop_seconds,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
            "total_seen": self.total_seen,
            "total_fed": self.total_fed,
            "active_tracked": len(self._seen),
            "recent_candidates": [c.to_dict() for c in recent[:10]],
        }
