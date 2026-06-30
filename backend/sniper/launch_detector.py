"""
LaunchDetector — Tracks the lifecycle of a new pump.fun token through 3 acts.

Act 1: Launch spike (dev + bundlers buy up)
Act 2: Sell-off dip (dev/bundlers dump)
Act 3: Organic recovery (retail returns)

Emits ActTransitionEvent via an asyncio.Queue when the token transitions acts.
"""

from __future__ import annotations
import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, Literal

logger = logging.getLogger(__name__)


@dataclass
class ActTransitionEvent:
    mint: str
    new_act: int
    timestamp: float
    spike_high_mc: float
    floor_mc: float
    dip_depth: float
    fees_paid_sol: float


class LaunchDetector:
    """Per-token lifecycle tracker: classifies which act the token is in."""

    def __init__(self, mint: str, genesis_mc: float, genesis_time: float,
                 event_queue: Optional[asyncio.Queue] = None):
        self.mint = mint
        self.genesis_mc = genesis_mc
        self.genesis_time = genesis_time
        self.event_queue = event_queue

        # State tracking
        self.spike_high_mc: float = genesis_mc
        self.spike_high_time: float = genesis_time
        self.lowest_mc_since_spike: float = genesis_mc
        self.lowest_mc_time: float = genesis_time
        self.current_mc: float = genesis_mc
        self.current_act: int = 1
        self.fees_paid_sol: float = 0.0

        # Floor stability tracking (Act 2 → Act 3)
        # Uses real timestamps rather than a trade counter so that a flurry of
        # trades in <1s doesn't prematurely trigger the transition.
        self._floor_stable_since: Optional[float] = None   # timestamp when stability began
        self._floor_stable_seconds_required: float = 8.0   # real seconds needed
        self._last_floor_check_mc: float = genesis_mc

        # Trade tracking for chart validation
        self.trade_sizes: list[float] = []
        self.buy_count: int = 0
        self.sell_count: int = 0

        # Token metadata
        self.token_name: str = ""
        self.token_symbol: str = ""
        self.creator: str = ""
        self.bonding_curve_key: str = ""

        # Stale flag
        self.is_stale: bool = False

    @property
    def dip_depth(self) -> float:
        """Current dip depth from spike high."""
        if self.spike_high_mc <= 0:
            return 0.0
        return (self.spike_high_mc - self.lowest_mc_since_spike) / self.spike_high_mc

    async def on_trade(self, trade: dict) -> None:
        """
        Called for every buy/sell trade on this token.
        Updates lifecycle state and emits act transitions.
        """
        price = trade.get("price", 0)
        sol_amount = trade.get("sol_amount", 0)
        tx_type = trade.get("tx_type", "buy")
        mc_sol = trade.get("market_cap_sol", 0)

        if mc_sol <= 0 and price > 0:
            # Approximate MC from price (1B token supply)
            mc_sol = price * 1_000_000_000

        self.current_mc = mc_sol

        # Accumulate fees on buys (pump.fun charges 1% on buys)
        if tx_type == "buy" and sol_amount > 0:
            self.fees_paid_sol += sol_amount * 0.01
            self.buy_count += 1

        if tx_type == "sell":
            self.sell_count += 1

        # Track trade sizes for chart validation
        if sol_amount > 0:
            self.trade_sizes.append(sol_amount)

        # Update highs and lows based on current act
        if self.current_act == 1:
            await self._process_act1(mc_sol, trade.get("timestamp", time.time()))
        elif self.current_act == 2:
            await self._process_act2(mc_sol, trade.get("timestamp", time.time()))
        # Act 3: no transitions — entry/exit handled by SniperEngine

    async def _process_act1(self, mc: float, ts: float) -> None:
        """Track spike high; transition to Act 2 when price drops >15% from peak."""
        if mc > self.spike_high_mc:
            self.spike_high_mc = mc
            self.spike_high_time = ts
            self.lowest_mc_since_spike = mc
            self.lowest_mc_time = ts

        # Check for Act 1 -> Act 2 transition.
        # Spike threshold lowered from 1.60 -> 1.30: PumpPortal's subscribeNewToken
        # event already captures the state AFTER the creator's initial buy, so
        # genesis_mc is already elevated. A 60% further rise is too strict.
        if self.spike_high_mc < self.genesis_mc * 1.30:
            return

        # Price has dropped >15% from spike high
        if mc < self.spike_high_mc * 0.85:
            self.current_act = 2
            self.lowest_mc_since_spike = mc
            self.lowest_mc_time = ts
            logger.info(
                f"[LaunchDetector] {self.mint[:8]} ({self.token_symbol}) Act 1->2: "
                f"genesis={self.genesis_mc:.1f} spike={self.spike_high_mc:.1f} "
                f"dip_to={mc:.1f} MC"
            )
            await self._emit_transition(ts)

    async def _process_act2(self, mc: float, ts: float) -> None:
        """Track floor formation; transition to Act 3 when floor is stable."""
        # Update lowest
        if mc < self.lowest_mc_since_spike:
            self.lowest_mc_since_spike = mc
            self.lowest_mc_time = ts
            self._floor_stable_since = None   # new low resets stability window
            return

        # Check dip depth requirement (>= 35%)
        dip_depth = self.dip_depth
        if dip_depth < 0.35:
            return

        # Check floor stability using real time.
        # Band widened 5%->8%: bonding-curve 1s trades bounce naturally; a tight
        # 5% band was resetting the timer on every micro-bounce.
        # Duration reduced 8s->5s to pair with the wider acceptance band.
        within_band = (
            abs(mc - self.lowest_mc_since_spike)
            / max(self.lowest_mc_since_spike, 1e-12)
        ) <= 0.08

        if within_band:
            if self._floor_stable_since is None:
                self._floor_stable_since = ts   # stability window starts
                logger.debug(
                    f"[LaunchDetector] {self.mint[:8]} floor band entered "
                    f"mc={mc:.1f} floor={self.lowest_mc_since_spike:.1f} "
                    f"dip={dip_depth:.1%}"
                )
            elif ts - self._floor_stable_since >= 5.0:   # 5s (was 8s)
                self.current_act = 3
                logger.info(
                    f"[LaunchDetector] {self.mint[:8]} ({self.token_symbol}) Act 2->3: "
                    f"dip_depth={dip_depth:.1%} floor={self.lowest_mc_since_spike:.1f} MC "
                    f"stable={ts - self._floor_stable_since:.1f}s "
                    f"fees={self.fees_paid_sol:.3f} SOL"
                )
                await self._emit_transition(ts)
        else:
            self._floor_stable_since = None   # price moved outside band - reset

    async def _emit_transition(self, ts: float) -> None:
        """Send an act transition event to the queue."""
        if self.event_queue is None:
            return
        event = {
            "type": "act_transition",
            "mint": self.mint,
            "new_act": self.current_act,
            "timestamp": ts,
            "spike_high_mc": self.spike_high_mc,
            "floor_mc": self.lowest_mc_since_spike,
            "dip_depth": self.dip_depth,
            "fees_paid_sol": self.fees_paid_sol,
        }
        try:
            self.event_queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

