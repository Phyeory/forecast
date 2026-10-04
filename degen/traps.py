"""DEGEN reflex tier — armed nuke→momentum traps, zero LLM in the loop.

The decision model decides WHETHER a coin is worth sniping when it arms a
trap (Setuh: never buy the falling knife — buy the momentum after the nuke).
The trap then rides the tick tape synchronously inside the feed's tape loop:

    armed → (nuke deepens: track the low) → momentum resumes → FIRE

Firing returns a pre-authorized event to the feed, which dispatches it to the
executor.  Aborts: structure broken (deeper than the max drawdown the
decision model authorized), trap expired, graduation.

Pure state machine — no awaits, no I/O.  Latency from trade event to fire
decision is one function call.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .feed import TapeTracker


@dataclass
class TrapSpec:
    """Pre-authorization written by the decision model at arm time."""

    mint: str
    size_sol: float                       # buy size, SOL — set once by the user's profile
    nuke_min_pct: float = 0.50            # drawdown from peak that counts as "the nuke"
    max_drawdown_pct: float = 0.75        # deeper than this = structure broken, abort
    up_ticks: int = 3                     # consecutive up-ticks that confirm momentum
    vol_flip: bool = True                 # require buy pressure > sell pressure (10 s)
    reclaim_pct: float = 0.02             # price back up ≥2% off the nuke low
    expires_in_s: float = 900.0           # trap lifetime from arm time
    min_mcap_sol: float = 0.0             # abort floor: nuke below this = dead coin
    slippage_bps: int = 2000              # Setuh buy slippage 20%
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d.pop("meta", None)
        return d


class TrapMachine:
    """One armed trap on one mint.  Fed trades synchronously by the feed."""

    def __init__(self, spec: TrapSpec, decision_id: str = ""):
        self.spec = spec
        self.mint = spec.mint
        self.trap_id = f"trap-{uuid.uuid4().hex[:8]}"
        self.decision_id = decision_id
        self.state = "armed"                 # armed | fired | expired | aborted
        self.armed_ts = time.time()
        self.nuke_confirmed = False
        self.nuke_low = 0.0
        self.abort_reason = ""
        self.fired_event: Optional[dict] = None

    # ── reflex path (called from the feed's tape loop — keep it sync) ────

    def on_trade(self, trade: dict, tracker: TapeTracker) -> Optional[dict]:
        if self.state != "armed":
            return None
        now = time.time()
        if now - self.armed_ts > self.spec.expires_in_s:
            self.state, self.abort_reason = "expired", "lifetime elapsed"
            return None
        if tracker.graduated:
            self.state, self.abort_reason = "aborted", "graduated"
            return None

        # nuke leg: drawdown deep enough to count as THE nuke
        if not self.nuke_confirmed:
            if tracker.in_drawdown and tracker.drawdown >= self.spec.nuke_min_pct:
                self.nuke_confirmed = True
                self.nuke_low = tracker.low_since_peak
            return None

        # aborts once the nuke is on
        if tracker.drawdown > self.spec.max_drawdown_pct:
            self.state, self.abort_reason = "aborted", f"drawdown {tracker.drawdown:.0%} beyond authorization"
            return None
        if self.spec.min_mcap_sol > 0 and tracker.last_mcap_sol > 0 \
                and tracker.last_mcap_sol < self.spec.min_mcap_sol:
            self.state, self.abort_reason = "aborted", "mcap floor breached"
            return None
        if tracker.low_since_peak < self.nuke_low * (1 - 0.20):
            # nuke low extended >20% below what we tracked — knife still falling hard
            self.nuke_low = tracker.low_since_peak

        # momentum leg: the turn, confirmed on the tape
        if tracker.momentum_resume(
            up_ticks=self.spec.up_ticks,
            vol_flip=self.spec.vol_flip,
            reclaim_pct=self.spec.reclaim_pct,
        ):
            self.state = "fired"
            self.fired_event = {
                "kind": "nuke_momentum",
                "trap_id": self.trap_id,
                "decision_id": self.decision_id,
                "mint": self.mint,
                "ts": now,
                "size_sol": self.spec.size_sol,
                "slippage_bps": self.spec.slippage_bps,
                "trigger": {
                    "price": tracker.last_price,
                    "nuke_low": self.nuke_low,
                    "peak_price": tracker.peak_price,
                    "drawdown": round(tracker.drawdown, 4),
                    "up_tick_streak": tracker.up_tick_streak,
                    "buy_sol_10s": round(tracker.recent_buy_sol, 4),
                    "sell_sol_10s": round(tracker.recent_sell_sol, 4),
                    "mcap_sol": tracker.last_mcap_sol,
                },
                "spec": self.spec.to_dict(),
            }
            return self.fired_event
        return None

    # ── introspection ─────────────────────────────────────────────────────

    def describe(self) -> dict:
        return {
            "trap_id": self.trap_id,
            "decision_id": self.decision_id,
            "mint": self.mint,
            "state": self.state,
            "armed_ts": self.armed_ts,
            "nuke_confirmed": self.nuke_confirmed,
            "nuke_low": self.nuke_low,
            "abort_reason": self.abort_reason,
            "spec": self.spec.to_dict(),
        }


# ── Trigger synthesiser for tests / replay ───────────────────────────────────

def replay_trap(spec: TrapSpec, trades: list[dict]) -> Optional[dict]:
    """Run a trap over a recorded trade list (deterministic test harness).

    Builds a TapeTracker the same way the live feed does, then feeds trades in
    order.  Returns the fire event or None (expired/aborted/never triggered).
    """
    from .feed import TapeTracker as _TT
    trap = TrapMachine(spec)
    tracker = _TT(spec.mint, first_seen=float(trades[0]["timestamp"]) if trades else time.time())
    for trade in trades:
        fire = trap.on_trade(trade, tracker)
        tracker.update(trade)
        if fire:
            return fire
        if trap.state != "armed":
            return None
    return None
