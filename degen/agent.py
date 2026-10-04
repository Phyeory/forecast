"""DEGEN agent — the trader loop.

    FILTER (feed) → RESEARCH (our forensics) → DECIDE (typed contract)
      → arm trap / buy / watch / skip → MANAGE (re-looks, exit by judgment)
      → REVIEW (post-trade reflection → playbook)

Modes: shadow (journal only, default) | confirm (UI cards for every order) |
auto (reflex traps + fast-lane buys execute; browser lane stays confirm).

Seatbelt: per-trade size is user-set once and never model-supplied; traps only
exist on decision-model-qualified coins; every action journals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .decision import Decision, DecisionEngine, TraderJournal
from . import research
from .traps import TrapMachine, TrapSpec

logger = logging.getLogger("degen.agent")

TRAP_PARAM_CLAMPS = {
    "nuke_min_pct": (0.30, 0.70, 0.50),
    "max_drawdown_pct": (0.60, 0.90, 0.75),
    "up_ticks": (2, 8, 3),
    "reclaim_pct": (0.0, 0.10, 0.02),
    "expires_in_s": (120, 3600, 900),
}


@dataclass
class RuntimeConfig:
    mode: str = "shadow"                  # shadow | confirm | auto
    buy_size_sol: float = 0.05            # set once by the user — never model-supplied
    max_positions: int = 3
    manage_interval_s: float = 30.0
    fast_lane_enabled: bool = True        # local executor for reflex fires
    browser_lane_enabled: bool = False    # Terminal confirm-mode fills (P6)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Position:
    mint: str
    symbol: str
    entry_ts: float
    entry_price: float
    size_sol: float
    token_raw: float
    decision_id: str = ""
    lane: str = "fast"
    peak_price: float = 0.0
    open_order: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class DegenAgent:
    def __init__(self, feed, engine: DecisionEngine, journal: TraderJournal,
                 executor=None, terminal=None, config: Optional[RuntimeConfig] = None):
        self.feed = feed
        self.engine = engine
        self.journal = journal
        self.executor = executor            # DegenExecutor | None (shadow needs none)
        self.terminal = terminal            # TerminalDriver | None
        self.config = config or RuntimeConfig()

        self.positions: dict[str, Position] = {}
        self.watchlist: dict[str, dict] = {}
        self.pending_approvals: dict[str, asyncio.Future] = {}
        self.traps: dict[str, TrapMachine] = {}
        self._tasks: set[asyncio.Task] = set()
        self._stopped = False
        feed.on_trap_trigger = self._on_trap_trigger

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self):
        self._stopped = False
        await self.feed.start()
        self._spawn(self._candidate_loop())
        self._spawn(self._manage_loop())
        self.journal.append("agent_started", {"mode": self.config.mode,
                                              "config": self.config.to_dict()})
        logger.info("[DegenAgent] started in %s mode", self.config.mode)

    async def stop(self):
        self._stopped = True
        await self.feed.stop()
        await self.engine.close()
        for t in list(self._tasks):
            t.cancel()
        self.journal.append("agent_stopped", {"open_positions": list(self.positions)})
        logger.info("[DegenAgent] stopped")

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def set_mode(self, mode: str) -> None:
        if mode in ("shadow", "confirm", "auto"):
            self.config.mode = mode
            self.journal.append("mode_changed", {"mode": mode})

    # ── candidate loop ───────────────────────────────────────────────────

    async def _candidate_loop(self):
        while not self._stopped:
            try:
                cand = await asyncio.wait_for(self.feed.candidates.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            self._spawn(self._process_candidate(cand))

    async def _process_candidate(self, cand: dict):
        mint = cand.get("mint") or ""
        if not mint or mint in self.positions:
            return
        if len(self.positions) >= self.config.max_positions:
            return
        event = cand.get("event")
        tracker = self.feed.tracker(mint) or (cand.get("tracker") or {})
        birth = self.feed.birth(mint)
        birth_dict = birth.to_dict() if birth else (cand.get("birth") or {})

        forensics = await research.run_forensics(mint, birth=birth_dict)
        if not forensics.get("our_verdict", {}).get("clean"):
            self.journal.append("research_reject", {
                "mint": mint, "event": event, "our_verdict": forensics.get("our_verdict")})
            return

        vector = self._vector(event, cand, tracker, birth_dict, forensics)
        decision = await self.engine.decide("qualify", mint, vector)
        await self._dispatch(decision, vector, event=event)

    def _vector(self, event: str, cand: dict, tracker, birth: dict, forensics: dict) -> dict:
        tr = tracker.snapshot() if hasattr(tracker, "snapshot") else (tracker or {})
        sol_usd = getattr(self.feed, "sol_usd", lambda: 0)()
        holders = forensics.get("tools", {}).get("holders") or {}
        meta = forensics.get("tools", {}).get("meta") or {}
        organic = forensics.get("tools", {}).get("organic") or {}
        dev = forensics.get("tools", {}).get("dev") or {}
        return {
            "event": event,
            "token": {
                "symbol": meta.get("symbol") or birth.get("symbol") or "",
                "name": meta.get("name") or birth.get("name") or "",
                "socials": meta.get("socials") or {k: birth.get(k, "") for k in ("twitter", "telegram", "website")},
            },
            "tape": tr,
            "mcap_usd": round(tr.get("mcap_sol", 0) * sol_usd) if isinstance(tr, dict) else None,
            "curve_frac": tr.get("curve_frac") if isinstance(tr, dict) else None,
            "birth": {k: birth.get(k) for k in ("ts", "initial_buy_sol", "creator") if k in birth},
            "holders": {k: holders.get(k) for k in
                        ("top10_share_pct", "max_single_non_lp_pct", "fingerprints", "warnings")},
            "sellability": forensics.get("tools", {}).get("sellability"),
            "authorities": forensics.get("authorities"),
            "organic": organic,
            "dev_history": {k: dev.get(k) for k in ("available", "prior_count", "prior_graduated")},
            "our_verdict": forensics.get("our_verdict"),
            "cross_checks": {
                "rugcheck_score": (forensics.get("tools", {}).get("rugcheck") or {}).get("score"),
                "goplus_flags": {k: v for k, v in
                                 ((forensics.get("tools", {}).get("goplus") or {}).items()
                                  if isinstance(forensics.get("tools", {}).get("goplus"), dict) else [])
                                 if k in ("honeypot", "transfer_fee", "metadata_modifiable")},
            },
            "runtime": {"mode": self.config.mode, "open_positions": len(self.positions)},
        }

    # ── action dispatch ──────────────────────────────────────────────────

    async def _dispatch(self, decision: Decision, vector: dict, event: str = ""):
        action = decision.action
        mint = decision.mint
        if action == "skip":
            self.journal.append("skipped", {"mint": mint, "reasons": decision.reasons,
                                            "decision_id": decision.decision_id})
            return
        if action == "watch":
            self.watchlist[mint] = {"decision_id": decision.decision_id, "ts": time.time()}
            return
        if action == "arm":
            await self._arm_trap(decision)
            return
        if action == "buy":
            await self._open_position(decision, lane="slow" if event != "nuke_watch" else "fast")
        # exit/hold handled in the manage loop
        self.journal.append("action_unhandled", {"mint": mint, "action": action})

    async def _arm_trap(self, decision: Decision):
        mint = decision.mint
        raw_tp = decision.trap_params or {}
        clamped = {}
        for key, (lo, hi, default) in TRAP_PARAM_CLAMPS.items():
            val = raw_tp.get(key, default)
            try:
                clamped[key] = max(lo, min(hi, type(default)(val)))
            except (TypeError, ValueError):
                clamped[key] = default
        spec = TrapSpec(
            mint=mint,
            size_sol=self.config.buy_size_sol,          # seatbelt: never model-supplied
            nuke_min_pct=clamped["nuke_min_pct"],
            max_drawdown_pct=clamped["max_drawdown_pct"],
            up_ticks=clamped["up_ticks"],
            reclaim_pct=clamped["reclaim_pct"],
            expires_in_s=clamped["expires_in_s"],
            vol_flip=bool(raw_tp.get("vol_flip", True)),
            meta={"reasons": decision.reasons, "p_up": decision.p_up},
        )
        trap = TrapMachine(spec, decision_id=decision.decision_id)
        self.traps[trap.trap_id] = trap
        self.feed.register_trap(trap)
        self.journal.append("trap_armed", {"trap_id": trap.trap_id, "mint": mint,
                                           "decision_id": decision.decision_id,
                                           "spec": spec.to_dict(),
                                           "reasons": decision.reasons})

    # ── reflex fire → execution ──────────────────────────────────────────

    async def _on_trap_trigger(self, fire: dict):
        """Zero-LLM path: trap fired → journal, then execute per mode."""
        trap_id = fire.get("trap_id")
        trap = self.traps.get(trap_id)
        self.journal.append("trap_fired", {"fire": fire})
        if self.config.mode != "auto" or not self.config.fast_lane_enabled or not self.executor:
            self.journal.append("trap_fired_not_executed", {
                "trap_id": trap_id, "mode": self.config.mode})
            return
        if len(self.positions) >= self.config.max_positions:
            self.journal.append("trap_fired_skipped", {"trap_id": trap_id,
                                                       "why": "max positions"})
            return
        result = await self.executor.buy(fire["mint"], fire["size_sol"],
                                         slippage_bps=fire.get("slippage_bps", 2000))
        self.journal.append("buy_executed", {"trap_id": trap_id, "result": result})
        if result.get("ok"):
            tracker = self.feed.tracker(fire["mint"])
            entry_price = float(fire.get("trigger", {}).get("price") or 0)
            pos = Position(
                mint=fire["mint"],
                symbol=(self.feed.birth(fire["mint"]).symbol if self.feed.birth(fire["mint"]) else ""),
                entry_ts=time.time(),
                entry_price=entry_price,
                size_sol=fire["size_sol"],
                token_raw=float(result.get("expected_out_raw") or 0),
                decision_id=fire.get("decision_id", ""),
                lane="fast",
                peak_price=entry_price,
                open_order=result,
            )
            self.positions[fire["mint"]] = pos

    # ── manage (exit by judgment — no mechanical stops) ──────────────────

    async def _manage_loop(self):
        while not self._stopped:
            await asyncio.sleep(self.config.manage_interval_s)
            for mint, pos in list(self.positions.items()):
                try:
                    await self._manage_position(mint, pos)
                except Exception:
                    logger.exception("[DegenAgent] manage failed for %s", mint[:8])

    async def _manage_position(self, mint: str, pos: Position):
        tracker = self.feed.tracker(mint)
        if tracker is None:
            return   # tape dropped (graduated) — handled by graduation path
        pos.peak_price = max(pos.peak_price, tracker.last_price)
        vector = {
            "symbol": pos.symbol,
            "position": {
                "age_s": round(time.time() - pos.entry_ts, 1),
                "entry_price": pos.entry_price,
                "peak_since_entry": pos.peak_price,
                "unrealized_pct": round((tracker.last_price - pos.entry_price) / pos.entry_price * 100, 2)
                if pos.entry_price else None,
                "size_sol": pos.size_sol,
            },
            "tape": tracker.snapshot(),
            "held_decisions": {"reasons_at_entry": self._entry_reasons(pos)},
        }
        decision = await self.engine.decide("manage", mint, vector)
        if decision.action == "exit":
            await self._close_position(mint, pos, decision)

    def _entry_reasons(self, pos: Position) -> list[str]:
        for rec in self._journal_tail(200):
            if rec.get("kind") == "decision" and rec.get("id") == pos.decision_id:
                return rec.get("decision", {}).get("reasons", [])
        return []

    def _journal_tail(self, n: int) -> list[dict]:
        try:
            lines = self.journal.journal.read_text(encoding="utf-8").splitlines()[-n:]
            out = []
            for line in lines:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
            return out
        except FileNotFoundError:
            return []

    async def _close_position(self, mint: str, pos: Position, decision: Decision):
        if not self.executor:
            self.journal.append("exit_intent", {"mint": mint, "decision": decision.to_dict()})
            return
        result = await self.executor.sell(mint, token_amount_raw=int(pos.token_raw) or None)
        self.journal.append("sell_executed", {"mint": mint, "result": result,
                                              "decision_id": decision.decision_id,
                                              "reasons": decision.reasons})
        self.positions.pop(mint, None)
        self.journal.append("trade_closed", {
            "mint": mint, "entry_ts": pos.entry_ts, "exit_ts": time.time(),
            "entry_price": pos.entry_price, "size_sol": pos.size_sol,
            "decision_id": pos.decision_id, "sell_result": result,
        })
        self._spawn(self._review_trade(mint, pos, decision))

    # ── review tier ──────────────────────────────────────────────────────

    async def _review_trade(self, mint: str, pos: Position, exit_decision: Decision):
        try:
            prompt = (
                "Post-trade review. Entry reasons were: "
                f"{self._entry_reasons(pos)}. Exit reasons: {exit_decision.reasons}. "
                f"Entry price {pos.entry_price}, size {pos.size_sol} SOL, held "
                f"{round(time.time() - pos.entry_ts)}s. Write 2-4 terse playbook "
                "lessons (what to repeat, what to avoid). No position sizes."
            )
            review, _ = await self.engine.analyst(prompt)
            if review:
                self.journal.append("trade_review", {"mint": mint, "review": review[:2000]})
                self.journal.append_playbook(f"[{pos.symbol or mint[:8]}] {review[:1500]}")
        except Exception:
            logger.exception("[DegenAgent] review failed")

    # ── UI surface ───────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "ts": time.time(),
            "config": self.config.to_dict(),
            "engine": self.engine.health(),
            "positions": [p.to_dict() for p in self.positions.values()],
            "traps": [t.describe() for t in self.traps.values()],
            "watchlist": list(self.watchlist.keys()),
            "calibration": self.engine.calibration.reliability(),
            "feed": self.feed.snapshot(),
        }
