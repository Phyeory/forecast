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

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from market_backtest.market_calibration import calibrate_market_params
from market_backtest.runner import COSTS

from market_live.brokers import Broker, BrokerFill, build_broker
from market_live.feeds import TapeFeed, bar_is_closed, market_is_open
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
    broker_symbols: dict = field(default_factory=dict)  # data sym -> broker sym
    # True  = materialise the engine's inherited book at the broker (the
    #         campaign's book continues onto the live account).
    # False = FLAT START: the engine's inherited positions are adopted as our
    #         desired state but NOT traded — the account opens only on fresh
    #         signals from now on.  Exits of inherited (never-bought) names
    #         are no-ops: the broker delta is zero, so nothing is submitted.
    warm_sync: bool = True
    # Stable paths for restart-safe continuity.  Without them the account
    # lives in memory + per-session journal dirs, so ANY restart resets the
    # guard phase and the book (a prop sim would silently restart at $100k).
    # state_path : JSON with {book, last_ts, desired} — the resume state.
    # guard_path : JSON with the PropGuard phase/equity/floors.
    state_path: Optional[str] = None
    guard_path: Optional[str] = None
    # Engine-state snapshot (pickled + gzipped).  Restoring it skips the
    # multi-minute warm and is decision-identical to a fresh warm.
    snapshot_path: Optional[str] = None

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

    # ── restart-safe state (resume) ─────────────────────────────────────
    def _state_file(self) -> Optional[Path]:
        return Path(self.cfg.state_path) if self.cfg.state_path else None

    def _load_state(self) -> Optional[dict]:
        p = self._state_file()
        if not p or not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
        except Exception as exc:  # noqa: BLE001
            self._log(f"state file unreadable ({exc}) — starting fresh")
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _save_state(self) -> None:
        """Persist the book + decision cursor atomically (restart safety)."""
        p = self._state_file()
        if not p:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "saved_at": time.time(),
                "book": self.broker.state(),
                "last_ts": {k: int(v) for k, v in self.last_ts.items()},
                "desired": {k: int(v) for k, v in self.desired.items()},
                "pending": {k: {kk: vv for kk, vv in v.items()
                                if kk in ("desired", "reason")}
                            for k, v in self.pending.items()},
            }
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, indent=1, default=str))
            tmp.replace(p)                      # atomic swap
        except Exception as exc:  # noqa: BLE001
            self._log(f"state save failed: {type(exc).__name__}: {exc}")

    def _restore_book(self, book: Optional[dict]) -> None:
        """Rehydrate the paper broker from a saved state file."""
        if not book:
            return
        b = self.broker
        if hasattr(b, "cash"):
            b.cash = float(book.get("cash", b.cash))
        if hasattr(b, "realized"):
            b.realized = float(book.get("realized", b.realized))
        if hasattr(b, "fees"):
            b.fees = float(book.get("fees", b.fees))
        pos = book.get("positions") or {}
        if hasattr(b, "positions") and isinstance(pos, dict):
            b.positions = {k: dict(v) for k, v in pos.items()}

    def prepare(self) -> dict:
        if self.journal:
            self.journal.open_session(self.cfg.as_dict())
        self._frozen = params_map(self.cfg.results_dir, self.cfg.market,
                                  self.cfg.interval, self.cfg.label)
        if not self._frozen:
            self._log(f"no frozen params in {self.cfg.results_dir} — rolling "
                      f"calibration per symbol")

        # ── resume: restore the book + decision cursor, warm the engine only
        #    through the last processed bar so the NEW bar is decided live
        state = self._load_state()
        resume_ts = (state or {}).get("last_ts") or {}
        if state:
            self._restore_book((state or {}).get("book"))
            for sym, d in ((state or {}).get("desired") or {}).items():
                self.desired[sym] = int(d)
            self._log(f"resumed from {self.cfg.state_path}: "
                      f"{len(resume_ts)} symbols, equity "
                      f"{self.broker.equity(self.prices):,.2f}")

        prepared = {}
        # ── snapshot restore: skips the warm entirely when the engine code
        #    and the frozen params are unchanged (decision-identical)
        snap_restored: set[str] = set()
        if self.cfg.snapshot_path:
            from market_live.snapshot import load_snapshot
            snap_restored = load_snapshot(self, self.cfg.snapshot_path)
            if snap_restored:
                self._log(f"snapshot restored {len(snap_restored)} symbols "
                          f"(warm skipped)")
        for sym in self.cfg.symbols:
            if sym in snap_restored:
                prepared[sym] = {"bars": 0,
                                 "desired": self.desired.get(sym, 0),
                                 "last_ts": self.last_ts.get(sym, 0),
                                 "restored": True}
                continue
            history = self.feed.history(sym)
            if not history:
                self._log(f"{sym}: no history — skipped")
                continue
            if sym in resume_ts:
                t_last = int(resume_ts[sym])
                history = [b for b in history if int(b["time"]) <= t_last]
                if not history:
                    history = []
            if self.cfg.warmup_bars and history:
                history = history[-self.cfg.warmup_bars:]
            if history:
                host = self._build_host(sym, history)
                prepared[sym] = {"bars": len(history),
                                 "desired": host.desired_position,
                                 "last_ts": int(history[-1]["time"])}
                self._log(f"{sym}: {len(history)} bars, warm position "
                          f"{host.desired_position}, frozen params="
                          f"{bool(self._frozen)}")
            else:
                self._log(f"{sym}: no warm history (fresh start)")

        # ── reconcile in-flight intent.  Flat start (warm_sync=False) means
        #    an inherited engine position is deliberately NOT traded: only
        #    (a) orders that were in flight at the crash and (b) exits of
        #    positions WE own may be re-armed.
        if state:
            saved_pending = (state or {}).get("pending") or {}
            for sym, p in saved_pending.items():
                if sym in self.hosts:
                    self.pending[sym] = {"desired": int(p.get("desired", 0)),
                                         "reason": p.get("reason", "resume")}
                    self._log(f"{sym}: resume — re-arming the in-flight "
                              f"{'entry' if p.get('desired') else 'exit'}")
            for sym in list(self.hosts):
                held = self.broker.position_qty(self._bsym(sym)) > 0
                d = int(self.desired.get(sym, 0))
                if held and not d and sym not in self.pending:
                    self.pending[sym] = {"desired": 0,
                                         "reason": "resume_exit"}
                    self._log(f"{sym}: resume — re-arming the exit order")
        if self.cfg.firm:
            self.guard = PropGuard(self.cfg.firm,
                                   account_size=self.cfg.starting_equity,
                                   log=self._log)
            self._guard_path = (Path(self.cfg.guard_path) if self.cfg.guard_path
                                else (self.journal.dir / "guard_state.json"
                                      if self.journal else None))
            if self._guard_path and self.guard.load(self._guard_path):
                self._log(f"guard state restored (phase {self.guard.s.phase}, "
                          f"equity {self.guard.s.equity:,.2f})")
            else:
                self.guard.start_day(self._equity())
            if self.journal:
                self.journal.guard({"kind": "guard_init",
                                    "status": self.guard.status()})

        if self.cfg.warm_sync:
            self._sync_positions(synthetic=True)
        else:
            # flat start: adopt the engine's book as desired state, trade none
            # of it.  Fresh signals from here on open real positions.
            for sym, host in self.hosts.items():
                self.desired[sym] = host.desired_position
            self._log("flat start (warm_sync=False): inherited engine "
                      "positions adopted as desired state, NOT traded — the "
                      "account opens only on fresh signals")
        self.ready = True
        if self.journal:
            self.journal.heartbeat({"event": "ready",
                                    "broker_state": self.broker.state(),
                                    "prepared": prepared})
        return prepared

    # ── accounting helpers ────────────────────────────────────────────────
    def _equity(self) -> float:
        return self.broker.equity(self.prices)

    def _bsym(self, sym: str) -> str:
        return (self.cfg.broker_symbols or {}).get(sym, sym)

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
            already = []
            if target and getattr(self.broker, "submits_immediately", False) \
                    and hasattr(self.broker, "open_orders"):
                try:
                    already = self.broker.open_orders(self._bsym(sym))
                except Exception:  # noqa: BLE001
                    already = []
            if already:
                self._log(f"{sym}: sync skipped — open order queued")
            else:
                f = self.broker.target_position(self._bsym(sym), target, px,
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
            # ── late-fill rule (mirrors the live Alpaca broker): if the order
            #    was created while the market was OPEN, the targeted open had
            #    already passed → the achievable fill is the current price,
            #    not an open we could not catch.
            if pend.get("late"):
                px_fill = float(candle["close"])
                pend["reason"] = f"{pend.get('reason', '')} [late_fill]".strip()
            else:
                px_fill = px_open
            target = pend.get("target_qty")
            if target is None:
                target = self._buy_qty(px_open) if pend["desired"] else 0.0
            done = False
            if pend.get("submitted") and pend.get("order_id") is not None:
                # the order went out at signal time (queueing broker); resolve
                # the actual fill so the journal carries the broker's price
                st = self.broker.order_status(pend["order_id"])
                status = str((st or {}).get("status", "")).lower()
                fq = float((st or {}).get("filled_qty") or 0.0)
                if st is None:
                    # no real order-status API (e.g. MT5 bridge) — the fill the
                    # broker returned at submission time is the truth
                    fq = float(pend.get("stroke_qty") or 0.0)
                ap = (float((st or {}).get("avg_price") or 0.0)
                      or float(pend.get("stroke_price") or 0.0) or px_open)
                if status in ("rejected", "canceled", "expired", "gone"):
                    self._log(f"{sym}: signal order {status or 'lost'} — "
                              f"resubmitting at next_open")
                    pend.pop("submitted", None)
                    pend.pop("order_id", None)
                elif fq > 0:
                    f = BrokerFill(symbol=self._bsym(sym),
                                   side="buy" if pend["desired"] else "sell",
                                   qty=abs(fq), price=ap,
                                   notional=abs(fq) * px_open, fee=0.0,
                                   time=time.time(),
                                   order_id=str(pend["order_id"]),
                                   reason=pend.get("reason", "next_open"))
                    deferred_fills.append(f)
                    if self.journal:
                        self.journal.order({**f.to_dict(),
                                            "event": "next_open_fill"})
                    done = True
                else:
                    # submitted but not yet filled (opg awaiting the open):
                    # keep deferring — the intent is already at the broker
                    self.pending[sym] = pend
            if not done and not pend.get("submitted"):
                f = self.broker.target_position(self._bsym(sym), target, px_fill,
                                                reason=pend.get("reason", "next_open"))
                if f:
                    deferred_fills.append(f)
                    if self.journal:
                        self.journal.order({**f.to_dict(),
                                            "event": "next_open_fill"})
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
                f = self.broker.target_position(self._bsym(sym), target,
                                                float(candle["close"]),
                                                reason=f"signal_{t}")
                if f:
                    fills.append(f)
                    if self.journal:
                        self.journal.order({**f.to_dict(), "event": "close_fill"})
                self.desired[sym] = new
            else:
                # defer to the next bar's open — unless the market is open
                # right now, in which case that open has already passed and
                # the achievable fill is the current price (late_fill).
                pend = {"desired": new, "reason": f"signal_{t}",
                        "signal_close": float(candle["close"]),
                        "late": market_is_open()}
                tgt = self._buy_qty(float(candle["close"])) if new else 0.0
                if getattr(self.broker, "submits_immediately", False):
                    # queueing broker: submit NOW — an opg order sent after the
                    # close fills at the next bar's open, the same instant the
                    # backtester books. Reconciled on the next closed bar.
                    try:
                        already = (self.broker.open_orders(self._bsym(sym))
                                   if hasattr(self.broker, "open_orders") else [])
                        if already:
                            pend["submitted"] = True
                            pend["order_id"] = str(already[0].get("id", ""))
                            pend["target_qty"] = tgt
                            pend["stroke_price"] = float(candle["close"])
                            pend["stroke_qty"] = tgt
                            self._log(f"{sym}: open order already queued — "
                                      f"skipping duplicate submit")
                        else:
                            f0 = self.broker.target_position(
                                self._bsym(sym), tgt, float(candle["close"]),
                                reason=f"signal_{t}")
                            if f0:
                                pend["submitted"] = True
                                pend["order_id"] = f0.order_id
                                pend["target_qty"] = tgt
                                # brokers that fill on receipt (MT5 bridge) carry
                                # the executable price in the response.
                                pend["stroke_price"] = f0.price
                                pend["stroke_qty"] = f0.qty
                                if self.journal:
                                    self.journal.order({**f0.to_dict(),
                                                        "event": "signal_submit"})
                    except Exception as exc:  # noqa: BLE001
                        self._log(f"{sym}: signal submit failed ({type(exc).__name__}:"
                                  f"{exc}) — deferring to next bar")
                self.pending[sym] = pend
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

        if self.guard and getattr(self, "_guard_path", None):
            self.guard.save(self._guard_path)
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
        # persist the cursor + book so a restart resumes instead of resetting
        self._save_state()
        if self.cfg.snapshot_path:
            from market_live.snapshot import save_snapshot
            save_snapshot(self, self.cfg.snapshot_path)
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
