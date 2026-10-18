"""prop_guard.py — firm rules as executable code.

The guard is a RISK OVERLAY, not part of the strategy: it can flatten and halt,
never choose entries.  It is asserted inert under normal conditions by the
parity test (the parity path does not instantiate it).

Templates use the dimensions that actually bind for this strategy, verified
against published rules (FTMO checked 2026-10-18):

  targets         profit targets in order ([] = funded account)
  daily           max daily loss %, None = no daily limit
  max_loss        max total loss %, static or trailing
  trailing        True = trail from high-water balance (EOD), False = static
  time_limit      days per phase, None = unlimited
  min_days        minimum trading days
  consistency     best-day share ceiling (None = no rule)
  split, fee      payout split, challenge fee for a 100k-class account

Templates mirror `market_backtest/mc_firms.py` so simulation and enforcement
cannot drift apart.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

TEMPLATES: dict[str, dict] = {
    "ftmo_2step": dict(
        label="FTMO 2-Step (CFD)", targets=[10.0, 5.0], daily=5.0,
        max_loss=10.0, trailing=False, time_limit=None, min_days=4,
        consistency=50.0, split=0.80, fee=540.0, account=100_000.0,
        swing=True, ea_allowed=True,
        note="static 10% max loss, 5% daily, no time limit, 4 min days, "
             "consistency: best day <= 50% of total profit"),
    "ftmo_1step": dict(
        label="FTMO 1-Step", targets=[10.0], daily=3.0, max_loss=10.0,
        trailing=True, time_limit=None, min_days=0, consistency=50.0,
        split=0.90, fee=899.0, account=100_000.0, swing=True, ea_allowed=True,
        note="3% daily, EOD-trailing 10% max loss, 90% split"),
    "nodaily_10": dict(
        label="No-daily-limit 1-step, static 10%", targets=[10.0], daily=None,
        max_loss=10.0, trailing=False, time_limit=None, min_days=0,
        consistency=None, split=0.80, fee=500.0, account=100_000.0,
        swing=True, ea_allowed=True,
        note="best arithmetic fit; confirm the firm actually has no daily cap "
             "and a STATIC (not trailing) max loss before buying"),
    "firm_8_5": dict(
        label="Generic 8%/5% 2-step", targets=[8.0, 5.0], daily=5.0,
        max_loss=10.0, trailing=False, time_limit=None, min_days=3,
        consistency=50.0, split=0.80, fee=450.0, account=100_000.0,
        swing=True, ea_allowed=True,
        note="FundingPips/E8 class"),
    "apex_style": dict(
        label="Futures prop (trailing 4%, no daily)", targets=[6.0], daily=None,
        max_loss=4.0, trailing=True, time_limit=None, min_days=0,
        consistency=None, split=0.90, fee=165.0, account=50_000.0,
        swing=False, ea_allowed=True,
        note="trailing drawdown — measured incompatible"),
    "apex_funded": dict(
        label="Futures funded (trailing 4%, no target)", targets=[], daily=None,
        max_loss=4.0, trailing=True, time_limit=None, min_days=0,
        consistency=None, split=0.90, fee=0.0, account=50_000.0,
        swing=False, ea_allowed=True, note="funded-phase rule set"),
    "topstep_style": dict(
        label="Futures prop (trailing 4%)", targets=[6.0], daily=2.0,
        max_loss=4.0, trailing=True, time_limit=None, min_days=5,
        consistency=None, split=0.90, fee=165.0, account=50_000.0,
        swing=False, ea_allowed=True,
        note="futures trailing drawdown — measured incompatible with this "
             "strategy's daily-bar gaps"),
}


@dataclass
class GuardState:
    phase: int = 0                     # index into targets
    day: str = ""
    day_start_equity: float = 0.0
    equity: float = 0.0
    peak_equity: float = 0.0
    start_equity: float = 0.0
    total_profit: float = 0.0
    best_day: float = 0.0
    trading_days: int = 0
    halted: bool = False
    halt_reason: str = ""
    passed: bool = False
    events: list = field(default_factory=list)


class PropGuard:
    """Enforces a firm template on a live account."""

    def __init__(self, template: str, account_size: Optional[float] = None,
                 log=print, buffer_pct: float = 0.5):
        if template not in TEMPLATES:
            raise KeyError(f"unknown template {template}")
        self.t = dict(TEMPLATES[template])
        self.name = template
        self.account = float(account_size or self.t["account"])
        self.s = GuardState(start_equity=self.account,
                            day_start_equity=self.account,
                            equity=self.account, peak_equity=self.account)
        self.log = log
        self.buffer_pct = float(buffer_pct) / 100.0
        self._phase_start = 0.0

    # ── helpers ───────────────────────────────────────────────────────────
    @property
    def target(self) -> Optional[float]:
        if self.s.phase >= len(self.t["targets"]):
            return None
        return float(self.t["targets"][self.s.phase])

    def floor(self) -> float:
        base = self.s.peak_equity if self.t["trailing"] else self.s.start_equity
        return base * (1.0 - self.t["max_loss"] / 100.0)

    def daily_floor(self) -> Optional[float]:
        if self.t["daily"] is None:
            return None
        return self.s.day_start_equity * (1.0 - self.t["daily"] / 100.0)

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start_day(self, equity: float, day_key: Optional[str] = None) -> None:
        self.s.day = day_key or time.strftime("%Y-%m-%d")
        self.s.day_start_equity = float(equity)
        self.s.equity = float(equity)

    def on_equity(self, equity: float) -> str:
        """Feed current equity.  Returns ok|fail_daily|fail_total|pass."""
        eq = float(equity)
        self.s.equity = eq
        self.s.peak_equity = max(self.s.peak_equity, eq)

        if self.s.halted:
            return "halted"

        if self.t["daily"] is not None and eq <= self.daily_floor():
            self._halt("fail_daily", f"equity {eq:,.2f} <= daily floor "
                                     f"{self.daily_floor():,.2f}")
            return "fail_daily"

        if eq <= self.floor():
            self._halt("fail_total", f"equity {eq:,.2f} <= max-loss floor "
                                     f"{self.floor():,.2f}")
            return "fail_total"

        tgt = self.target
        if tgt is not None and eq >= self.s.start_equity * (1.0 + tgt / 100.0):
            # consistency gate (not a breach): need more profit if violated
            if self.t["consistency"] and self._best_day_share(eq) > self.t["consistency"] / 100.0:
                self._event("consistency_hold",
                            f"best day {self._best_day_share()*100:.0f}% > "
                            f"{self.t['consistency']:.0f}% — continue trading")
                return "consistency_hold"
            self._event("phase_pass", f"phase {self.s.phase} target {tgt}% hit")
            self.s.phase += 1
            if self.s.phase >= len(self.t["targets"]):
                self.s.passed = True
                return "pass"
            # next phase: reset the static floor base, keep equity
            self.s.start_equity = eq
            self.s.peak_equity = eq
            self.s.phase_start_equity = eq  # type: ignore[attr-defined]
            return "phase_pass"
        return "ok"

    def close_day(self, equity: float, day_key: str) -> dict:
        """Book end-of-day: daily PnL, trading days, best day, consistency."""
        pnl = float(equity) - self.s.day_start_equity
        if abs(pnl) > 1e-9:
            self.s.trading_days += 1
        self.s.total_profit += pnl
        self.s.best_day = max(self.s.best_day, pnl)
        rec = {"day": day_key, "equity": float(equity), "pnl": pnl,
               "best_day": self.s.best_day, "total_profit": self.s.total_profit,
               "trading_days": self.s.trading_days,
               "consistency": self._best_day_share()}
        self.s.day_start_equity = float(equity)
        return rec

    def _best_day_share(self, eq: Optional[float] = None) -> float:
        """Best single day as a share of total profit.

        `eq` includes the CURRENT (unbooked) day so the rule is evaluated on
        the same numbers the firm sees at the moment the target is hit — a
        target hit on one huge day must not pass before that day is booked.
        """
        total = self.s.total_profit
        best = self.s.best_day
        if eq is not None:
            today = float(eq) - self.s.day_start_equity
            if today > 0:
                best = max(best, today)
                total += today
        if total <= 0:
            return 0.0
        return best / total

    def _halt(self, reason: str, detail: str) -> None:
        self.s.halted = True
        self.s.halt_reason = reason
        self._event(reason, detail)

    def _event(self, kind: str, detail: str) -> None:
        self.s.events.append({"t": time.time(), "kind": kind, "detail": detail})
        self.log(f"[prop] {kind}: {detail}")

    # ── pre-trade gate ────────────────────────────────────────────────────
    def can_open(self, projected_equity: float) -> tuple[bool, str]:
        """Pre-trade gate with a 0.5% equity buffer so a normal adverse bar
        cannot put the account through a floor between checks."""
        if self.s.halted:
            return False, f"halted:{self.s.halt_reason}"
        if self.s.passed:
            return True, "funded"
        buf = abs(projected_equity) * self.buffer_pct
        if self.t["daily"] is not None:
            dfloor = self.daily_floor()
            if projected_equity - buf <= dfloor:
                return False, f"daily_floor {dfloor:,.2f}"
        if projected_equity - buf <= self.floor():
            return False, f"max_loss_floor {self.floor():,.2f}"
        return True, "ok"

    # ── persistence (a restart must NOT reset the firm's floors) ──────────
    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({
            "name": self.name, "account": self.account,
            "buffer_pct": self.buffer_pct, "state": asdict(self.s)}, indent=1))

    def load(self, path: str | Path) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        d = json.loads(p.read_text())
        self.name = d.get("name", self.name)
        self.account = float(d.get("account", self.account))
        self.buffer_pct = float(d.get("buffer_pct", self.buffer_pct))
        st = d.get("state") or {}
        self.s = GuardState(**{**asdict(GuardState()), **st})
        return True

    def status(self) -> dict:
        return {
            "template": self.name, "phase": self.s.phase,
            "phase_target_pct": self.target, "account": self.account,
            "equity": self.s.equity, "peak": self.s.peak_equity,
            "floor": self.floor(), "daily_floor": self.daily_floor(),
            "trading_days": self.s.trading_days,
            "total_profit": self.s.total_profit,
            "best_day": self.s.best_day,
            "consistency": self._best_day_share(),
            "halted": self.s.halted, "halt_reason": self.s.halt_reason,
            "passed": self.s.passed, "events": self.s.events[-20:],
        }
