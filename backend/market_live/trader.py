"""trader.py — live orchestration: feed -> hosts -> guard -> broker -> journal.

Execution identity
------------------
For every closed bar the trader calls `MarketEngineHost.on_bar`, which is the
backtester's four-state replay.  The broker is then told the *target position*
the engine wants; it never invents a trade.  So the sequence of entries/exits
and their reasons is the backtester's sequence, scaled by `k`.

Execution modes (the only honest difference from the backtest)
--------------------------------------------------------------
  close      fill at the signal bar's close — byte-identical to `exec_model=
             "instant"`.  Deployable with market-on-close orders IF the broker
             accepts a MOC before the close (approximate: the signal is only
             final at the close).
  next_open  signal at close(t), fill at open(t+1).  This is what a daily-bar
             system can guarantee.  It is NOT identical to the backtest, and
             `replay --compare-modes` measures exactly what it costs.

Warm start
----------
`prepare()` replays history through the hosts with trades enabled, so engine
state is exactly what a backtest would have at the same bar.  If the engine is
in a position at the boundary, the broker reconciles INTO that position at the
current market price (flagged `synthetic`).  From then on decisions match.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from market_backtest.market_calibration import calibrate_market_params
from market_backtest.runner import COSTS

from market_live.brokers import Broker, BrokerFill, build_broker
from market_live.feeds import TapeFeed, bar_is_closed
from market_live.host import MarketEngineHost, params_map
from market_live.journal import SessionJournal
from market_live.prop_guard import PropGuard


@dataclass
class LiveConfig:
    market: str = "stocks"
    interval: str = "1d"
    symbols: tuple[str, ...] = ()
    results_dir: str = "market_backtest/results/arm14"
    label: str = "arm14"
    overrides: dict = field(default_factory=dict)
    k: float = 0.75                  # sizing multiplier on the 10% base notional
    buy_size: float = 0.1
    starting_equity: float = 100_000.0
    broker: str = "paper"
    broker_kwargs: dict = field(default_factory=dict)
    firm: Optional[str] = None       # prop template name
    warmup_bars: int = 0             # 0 = all history
    journal: bool = True
    journal_root: Optional[str] = None
    exec_mode: str = "next_open"     # close | next_open

    def as_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v)
                for k, v in self.__dict__.items()}


class MarketLiveTrader:
    def __init__(self, cfg: LiveConfig, feed=None, broker: Optional[Broker] = None,
                 journal: Optional[SessionJournal] = None, verbose: bool = True):
        self.cfg = cfg
        self.feed = feed or TapeFeed(cfg.market, cfg.interval)
        self.broker = broker or build_broker(cfg.broker, **cfg.broker_kwargs)
        self.journal = journal or (SessionJournal(root=cfg.journal_root)
                                   if cfg.journal else None)
        self.verbose = verbose

        self.hosts: dict[str, MarketEngineHost] = {}
        self.prices: dict[str, float] = {}
        self.last_ts: dict[str, int] = {}
        self.desired: dict[str, int] = {}
        self.pending: dict[str, dict] = {}     # deferred next_open orders
        self.current_day: Optional[str] = None
        self.guard: Optional[PropGuard] = None
        self._frozen: dict[str, dict] = {}
        self.ready = False
        self.trades: list[dict] = []

    # ── logging ───────────────────────────────────────────────────────────
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[live] {msg}", flush=True)

    # ── setup ─────────────────────────────────────────────────────────────
    def _resolve_params(self, symbol: str, history: list[dict]) -> dict:
        if symbol in self._frozen:
            p = dict(self._frozen[symbol])
            p.update(self.cfg.overrides)
            return p
        kind = MarketEngineHost._kind(self.cfg.market, self.cfg.interval)
        calib = calibrate_market_params(history, kind, len(history))
        p = dict(calib["engine_params"])
        p.update(self.cfg.overrides)
        return p

    def _build_host(self, sym: str, history: list[dict]) -> MarketEngineHost:
        params = self._resolve_params(sym, history)
        host = MarketEngineHost(self.cfg.market, sym, self.cfg.interval,
                                params, buy_size=self.cfg.buy_size)
        host.warm(history)
        self.hosts[sym] = host
        self.prices[sym] = float(history[-1]["close"])
        self.last_ts[sym] = int(history[-1]["time"])
        self.desired[sym] = host.desired_position
        return host

    def prepare(self) -> dict:
        if self.journal:
            self.journal.open_session(self.cfg.as_dict())
        self._frozen = params_map(self.cfg.results_dir, self.cfg.market,
                                  self.cfg.interval, self.cfg.label)
        if not self._frozen:
            self._log(f"no frozen params in {self.cfg.results_dir} — rolling "
                      f"calibration per symbol")

        prepared = {}
        for sym in self.cfg.symbols:
            history = self.feed.history(sym)
            if not history:
                self._log(f"{sym}: no history — skipped")
                continue
            if self.cfg.warmup_bars:
                history = history[-self.cfg.warmup_bars:]
            host = self._build_host(sym, history)
            prepared[sym] = {"bars": len(history),
                             "desired": host.desired_position,
                             "last_ts": int(history[-1]["time"])}
            self._log(f"{sym}: {len(history)} bars, warm position "
                      f"{host.desired_position}, frozen params={bool(self._frozen)}")

        if self.cfg.firm:
            self.guard = PropGuard(self.cfg.firm,
                                   account_size=self.cfg.starting_equity,
                                   log=self._log)
            self.guard.start_day(self._equity())
            if self.journal:
                self.journal.guard({"kind": "guard_init",
                                    "status": self.guard.status()})

        self._sync_positions(synthetic=True)
        self.ready = True
        if self.journal:
            self.journal.heartbeat({"event": "ready",
                                    "broker_state": self.broker.state(),
                                    "prepared": prepared})
        return prepared

    # ── accounting helpers ────────────────────────────────────────────────
    def _equity(self) -> float:
        return self.broker.equity(self.prices)

    def _buy_qty(self, price: float, equity: Optional[float] = None) -> float:
        eq = equity if equity is not None else self._equity()
        notional = max(0.0, self.cfg.k * self.cfg.buy_size * eq)
        return notional / price if price > 0 else 0.0

    def _sync_positions(self, synthetic: bool = False) -> list[BrokerFill]:
        fills: list[BrokerFill] = []
        eq = self._equity()
        for sym, host in self.hosts.items():
            px = self.prices.get(sym, 0.0)
            if px <= 0:
                continue
            target = self._buy_qty(px, eq) if host.desired_position else 0.0
            f = self.broker.target_position(sym, target, px,
                                            reason="state_sync")
            if f:
                f.synthetic = synthetic
                fills.append(f)
                if self.journal:
                    self.journal.order({**f.to_dict(), "event": "sync_fill"})
            self.desired[sym] = host.desired_position
        return fills

    def _guard_allows(self) -> tuple[bool, str]:
        if not self.guard:
            return True, "no_guard"
        return self.guard.can_open(self._equity())

    # ── guard day rollover (global, once per date) ────────────────────────
    def _roll_day(self, t: int) -> None:
        if not self.guard:
            return
        day_key = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")
        if self.current_day == day_key:
            return
        if self.current_day:
            rec = self.guard.close_day(self._equity(), self.current_day)
            if self.journal:
                self.journal.equity({"event": "day_close", **rec,
                                     "guard": self.guard.status()})
        self.guard.start_day(self._equity(), day_key)
        self.current_day = day_key

    # ── one closed bar ────────────────────────────────────────────────────
    def step_bar(self, sym: str, candle: dict) -> dict:
        host = self.hosts[sym]
        t = int(candle["time"])
        self._roll_day(t)

        # 1) deferred (next_open) order from the previous close fills HERE
        deferred_fills: list[BrokerFill] = []
        pend = self.pending.pop(sym, None)
        if pend is not None:
            px_open = float(candle["open"])
            self.prices[sym] = px_open
            target = self._buy_qty(px_open) if pend["desired"] else 0.0
            f = self.broker.target_position(sym, target, px_open,
                                            reason=pend.get("reason", "next_open"))
            if f:
                deferred_fills.append(f)
                if self.journal:
                    self.journal.order({**f.to_dict(), "event": "next_open_fill"})
            self.desired[sym] = pend["desired"]

        # 2) engine sees the bar — the invariant call
        decision = host.on_bar(candle)
        self.last_ts[sym] = t
        self.prices[sym] = float(candle["close"])

        if self.journal:
            self.journal.bar({"symbol": sym, **{k: candle[k] for k in
                                                ("time", "open", "high", "low",
                                                 "close", "volume")}})
            self.journal.decision(decision.to_dict())

        # 3) act on position transitions only (never rebalance on drift)
        fills = list(deferred_fills)
        prev = self.desired.get(sym, 0)
        new = decision.desired_position
        if new != prev:
            eq = self._equity()
            if new == 1:
                ok, why = self._guard_allows()
                if not ok:
                    self._log(f"{sym}: entry blocked ({why})")
                    new = 0
            if self.cfg.exec_mode == "close":
                target = self._buy_qty(float(candle["close"]), eq) if new else 0.0
                f = self.broker.target_position(sym, target, float(candle["close"]),
                                                reason=f"signal_{t}")
                if f:
                    fills.append(f)
                    if self.journal:
                        self.journal.order({**f.to_dict(), "event": "close_fill"})
                self.desired[sym] = new
            else:
                # defer to the next bar's open
                self.pending[sym] = {"desired": new,
                                     "reason": f"signal_{t}",
                                     "signal_close": float(candle["close"])}
                self.desired[sym] = prev       # not yet filled

        # 4) guard on post-bar equity
        status = "ok"
        if self.guard:
            status = self.guard.on_equity(self._equity())
            if status in ("fail_daily", "fail_total"):
                self._log(f"GUARD {status} — flattening everything")
                flat = self.broker.flatten_all(self.prices, reason=status)
                for ff in flat:
                    fills.append(ff)
                    if self.journal:
                        self.journal.order({**ff.to_dict(), "event": "guard_flatten"})
                self.pending.clear()
                for s in self.desired:
                    self.desired[s] = 0
                if self.journal:
                    self.journal.guard({"kind": status, "status": self.guard.status()})
            elif status in ("phase_pass", "pass", "consistency_hold"):
                if self.journal:
                    self.journal.guard({"kind": status, "status": self.guard.status()})

        if self.journal:
            self.journal.equity({"event": "bar", "symbol": sym, "time": t,
                                 "equity": self._equity(),
                                 "guard_status": status})
        rec = {"symbol": sym, "time": t, "decision": decision.to_dict(),
               "fills": [x.to_dict() for x in fills], "guard": status}
        self.trades.append(rec)
        return rec

    # ── polling (live) ────────────────────────────────────────────────────
    def poll_once(self) -> dict:
        out = {"processed": 0, "fills": 0}
        for sym in list(self.hosts):
            try:
                bars = self.feed.closed_bars(sym, since_ts=self.last_ts[sym])
            except Exception as exc:  # noqa: BLE001
                self._log(f"{sym}: feed error {type(exc).__name__}: {exc}")
                continue
            for candle in bars:
                if not bar_is_closed(int(candle["time"]), self.cfg.interval):
                    continue
                r = self.step_bar(sym, candle)
                out["processed"] += 1
                out["fills"] += len(r["fills"])
        if self.journal:
            self.journal.heartbeat({"event": "poll",
                                    "equity": self._equity(),
                                    "broker": self.broker.state(),
                                    "guard": self.guard.status() if self.guard else None})
        return out

    def flatten_and_stop(self, reason: str = "operator") -> None:
        fills = self.broker.flatten_all(self.prices, reason=reason)
        if self.journal:
            for f in fills:
                self.journal.order({**f.to_dict(), "event": "manual_flatten"})
            self.journal.heartbeat({"event": "stop", "reason": reason,
                                    "broker": self.broker.state()})

    # ── reporting ─────────────────────────────────────────────────────────
    def summary(self) -> dict:
        eq = self._equity()
        closed = [t for t in self.trades for f in t["fills"] if f["side"] == "sell"]
        return {
            "equity": eq,
            "pnl": eq - self.cfg.starting_equity,
            "pnl_pct": (eq / self.cfg.starting_equity - 1.0) * 100.0,
            "n_bars": len(self.trades),
            "n_exits": len(closed),
            "broker": self.broker.state(),
            "guard": self.guard.status() if self.guard else None,
            "open_positions": sorted(s for s, d in self.desired.items() if d),
            "engine_pnl": {s: h.balance - h.ft.stats.starting_balance
                           for s, h in self.hosts.items()},
        }


# ─────────────────────────────────────────────────────────────────────────
# replay driver (deployment dry-run)
# ─────────────────────────────────────────────────────────────────────────
def run_replay(cfg: LiveConfig, start_ts: Optional[int] = None,
               max_bars: Optional[int] = None, verbose: bool = True) -> dict:
    """Feed a historical tape through the FULL live stack (paper broker)."""
    feed = TapeFeed(cfg.market, cfg.interval)
    kind = MarketEngineHost._kind(cfg.market, cfg.interval)
    broker = build_broker("paper", starting_equity=cfg.starting_equity,
                          slippage_pct=COSTS[kind]["slippage_pct"])
    trader = MarketLiveTrader(cfg, feed=feed, broker=broker, verbose=verbose)

    if trader.journal:
        trader.journal.open_session({**cfg.as_dict(), "mode": "replay",
                                     "start_ts": start_ts})
    trader._frozen = params_map(cfg.results_dir, cfg.market, cfg.interval, cfg.label)

    for sym in cfg.symbols:
        hist = feed.history(sym)
        if start_ts:
            hist = [b for b in hist if int(b["time"]) < start_ts]
        if hist:
            trader._build_host(sym, hist)

    if cfg.firm:
        trader.guard = PropGuard(cfg.firm, account_size=cfg.starting_equity,
                                 log=trader._log)
        trader.guard.start_day(trader._equity())
    trader._sync_positions(synthetic=True)
    trader.ready = True

    pending: list[tuple[int, str, dict]] = []
    for sym in cfg.symbols:
        for b in feed.history(sym):
            t = int(b["time"])
            if start_ts and t < start_ts:
                continue
            pending.append((t, sym, b))
    pending.sort(key=lambda x: (x[0], x[1]))
    if max_bars:
        pending = pending[:max_bars]

    for t, sym, b in pending:
        trader.step_bar(sym, b)

    return trader.summary()
