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
    broker_symbols: dict = field(default_factory=dict)  # data sym -> broker sym
    # Manual adoption only: path to a recalibration snapshot JSON (or a
    # snapshot directory containing `adopted.json`).  Empty = frozen params
    # from results_dir, i.e. the campaign behaviour, byte-identical.
    calibration_snapshot: str = ""
    calibration_require_adopted: bool = True

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
        self._snap_symbols: set[str] = set()
        self._calib_meta: dict = {}
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
            # A snapshot is authoritative: config overrides apply only to
            # symbols that fell back to the frozen results_dir params.
            if symbol not in self._snap_symbols:
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

    def _load_calibration_snapshot(self) -> tuple[dict[str, dict], dict]:
        """Explicit, session-start-only snapshot load (never mid-session).

        Contract:
          * empty config path       -> ({}, {}) and the caller uses frozen params
          * directory                -> `adopted.json` (manual adoption record)
          * file                     -> that file (the path IS the adoption)
          * unreadable / not adopted -> rejected + journal event, frozen params
        """
        raw = str(self.cfg.calibration_snapshot or "").strip()
        if not raw:
            return {}, {}
        p = Path(raw)
        meta: dict = {"requested": raw}
        try:
            if p.is_dir():
                adopted = p / "adopted.json"
                if not adopted.exists() and self.cfg.calibration_require_adopted:
                    raise FileNotFoundError(
                        f"{p} has no adopted.json (candidate snapshot — run "
                        f"`recalibrate_market adopt` first)")
                if adopted.exists():
                    rec = json.loads(adopted.read_text())
                    p = p / rec["file"]
                    meta.update({"adopted_at": rec.get("adopted_at"),
                                 "adopted_by": rec.get("adopted_by", "manual")})
                else:
                    p = p / json.loads((p / "manifest.json").read_text())["latest"]
            snap = json.loads(p.read_text())
            params = dict(snap.get("params") or {})
            if not params:
                raise ValueError("snapshot has no params")
            meta.update({
                "version": snap.get("version"),
                "asof": snap.get("asof"),
                "git_commit": snap.get("git_commit"),
                "n_symbols": len(params),
                "source": str(p),
            })
            self._log(f"calibration snapshot {meta.get('version')} "
                      f"(asof {meta.get('asof')}, {len(params)} symbols)")
            return params, meta
        except Exception as exc:  # noqa: BLE001
            meta["error"] = f"{type(exc).__name__}: {exc}"
            self._log(f"calibration snapshot REJECTED — {meta['error']} "
                      f"(using frozen params)")
            if self.journal:
                self.journal.write("recalibration_rejected", meta)
            return {}, meta

    def prepare(self) -> dict:
        if self.journal:
            self.journal.open_session(self.cfg.as_dict())
        self._frozen = params_map(self.cfg.results_dir, self.cfg.market,
                                  self.cfg.interval, self.cfg.label)
        snap_params, self._calib_meta = self._load_calibration_snapshot()
        if snap_params:
            # Snapshot wins for symbols it covers; frozen params remain the
            # fallback for anything missing (and are logged).
            self._snap_symbols = set(snap_params)
            missing = [s for s in self.cfg.symbols if s not in snap_params]
            for s in self.cfg.symbols:
                if s in snap_params:
                    self._frozen[s] = dict(snap_params[s])
            if self.journal:
                self.journal.write("recalibration", {
                    **self._calib_meta, "missing_symbols": missing,
                    "mode": "snapshot"})
            if missing:
                self._log(f"snapshot missing {len(missing)} symbols — frozen fallback")
        elif not self._frozen:
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
            self._guard_path = (self.journal.dir / "guard_state.json"
                                if self.journal else None)
            if self._guard_path and self.guard.load(self._guard_path):
                self._log(f"guard state restored (phase {self.guard.s.phase}, "
                          f"equity {self.guard.s.equity:,.2f})")
            else:
                self.guard.start_day(self._equity())
            if self.journal:
                self.journal.guard({"kind": "guard_init",
                                    "status": self.guard.status()})

        self._sync_positions(synthetic=True)
        self.ready = True
        if self.journal:
            self.journal.heartbeat({"event": "ready",
                                    "broker_state": self.broker.state(),
                                    "calibration": self._calib_meta or
                                                   {"mode": "frozen"},
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
                f = self.broker.target_position(self._bsym(sym), target, px_open,
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
                # defer to the next bar's open
                pend = {"desired": new, "reason": f"signal_{t}",
                        "signal_close": float(candle["close"])}
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
    snap_params, trader._calib_meta = trader._load_calibration_snapshot()
    if snap_params:
        trader._frozen.update({k: dict(v) for k, v in snap_params.items()})
        trader._snap_symbols = set(snap_params)
        if trader.journal:
            trader.journal.write("recalibration", {
                **trader._calib_meta, "mode": "snapshot"})

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
    if trader.journal:
        trader.journal.heartbeat({"event": "ready",
                                  "calibration": trader._calib_meta or
                                                 {"mode": "frozen"},
                                  "n_hosts": len(trader.hosts)})

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
