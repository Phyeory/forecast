"""host.py — the decision-identical engine host.

`MarketEngineHost` is the backtester's replay loop, driven one closed bar at a
time.  It builds `MarketForwardTester` exactly as `runner.run_market_backtest`
does (same costs, same exec_model, same engine kwargs, same latency wiring) and
replays each bar with the identical four-state expansion.

Invariants enforced here (see test_live_parity.py):
  * 4 `update()` calls per bar, exact order and exact OHLC per state
  * `_build_full_result=False` on the parity path (same as the runner fast path)
  * `recording_ended` force-close semantics available at tape end
  * no per-bar state of our own that the backtester lacks
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from forward_tester import ForwardTester  # noqa: F401  (backend cwd import)
from market_backtest.runner import COSTS, MARKET_CLASS, MarketForwardTester


@dataclass
class Fill:
    """One execution, in backtester units (balance fractions)."""
    symbol: str
    side: str                 # "buy" | "exit"
    time: int
    price: float              # fill price AFTER slippage (same as Trade fields)
    size_sol: float           # notional committed (balance units)
    size_tokens: float
    reason: str
    trade_index: int          # index in ft.trade_history once closed
    pnl_sol: Optional[float] = None
    fee: float = 0.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "side": self.side, "time": self.time,
            "price": self.price, "size_sol": self.size_sol,
            "size_tokens": self.size_tokens, "reason": self.reason,
            "trade_index": self.trade_index,
            "pnl_sol": self.pnl_sol, "fee": self.fee,
        }


@dataclass
class BarDecision:
    """What one closed bar produced."""
    symbol: str
    time: int
    fills: list[Fill] = field(default_factory=list)
    desired_position: int = 0      # 1 = should be long after this bar
    last_price: float = 0.0

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "time": self.time,
                "desired_position": self.desired_position,
                "last_price": self.last_price,
                "fills": [f.to_dict() for f in self.fills]}


class MarketEngineHost:
    """One symbol, one engine, one forward tester — replay-safe."""

    def __init__(self, market: str, symbol: str, interval: str,
                 engine_params: dict, *,
                 starting_balance: float = 1.0,
                 buy_size: float = 0.1,
                 market_kind: Optional[str] = None,
                 exec_model: str = "instant",
                 costs: Optional[dict] = None):
        self.market = market
        self.symbol = symbol
        self.interval = interval

        kind = market_kind or self._kind(market, interval)
        self.kind = kind
        self.costs = costs or COSTS[kind]

        self.ft = MarketForwardTester(
            starting_balance=starting_balance,
            buy_size_sol=buy_size,
            priority_fee=self.costs["priority_fee"],
            bribe_fee=0.0,
            slippage_pct=self.costs["slippage_pct"],
            market_engine_kwargs=dict(engine_params),
            exec_model=exec_model,
        )
        # latency wiring — byte-identical to runner.run_market_backtest
        _eng_delay = float(getattr(self.ft.engine, "v2_entry_delay_seconds", 0.0))
        if _eng_delay > 0.0:
            self.ft.enable_entry_latency(_eng_delay)
        _eng_exit_delay = float(getattr(self.ft.engine, "v2_exit_delay_seconds", 0.0))
        if _eng_exit_delay > 0.0:
            self.ft.enable_exit_latency(
                _eng_exit_delay,
                armed_only=float(getattr(self.ft.engine,
                                         "v2_exit_delay_armed_only", 0.0)) > 0.0)

        self._update = self.ft.update
        self.last_bar_time: int = 0
        self.bars_seen: int = 0
        self._closed_count = 0

    @staticmethod
    def _kind(market: str, interval: str) -> str:
        if interval == "1d":
            return MARKET_CLASS.get(market, market)
        if market == "stocks":
            return f"stocks_{interval}"
        return f"{market}_{interval}"

    # ── the invariant: one bar, four states, fixed order ──────────────────
    def on_bar(self, candle: dict, *, build_full_result: bool = False) -> BarDecision:
        t = int(candle["time"])
        o = float(candle["open"])
        h = float(candle["high"])
        l = float(candle["low"])
        c = float(candle["close"])
        vol = float(candle.get("volume", 0.0) or 0.0)
        buy_vol = float(candle.get("buy_volume", 0.0) or 0.0)
        sell_vol = float(candle.get("sell_volume", 0.0) or 0.0)

        bullish = c >= o
        mid_first, mid_second = (h, l) if bullish else (l, h)

        before_open = self.ft.current_trade is not None
        before_closed = len(self.ft.trade_history)

        # state 1: open tick
        self._update(time=t, o=o, h=o, l=o, c=o, volume=0.0,
                     _build_full_result=build_full_result)
        # state 2: first extreme
        self._update(time=t, o=o, h=max(o, mid_first), l=min(o, mid_first),
                     c=mid_first, volume=0.0, _build_full_result=build_full_result)
        # state 3: both extremes
        self._update(time=t, o=o, h=h, l=l, c=mid_second, volume=0.0,
                     _build_full_result=build_full_result)
        # state 4: close tick (volume lands here)
        self._update(time=t, o=o, h=h, l=l, c=c, volume=vol,
                     buy_volume=buy_vol, sell_volume=sell_vol,
                     _build_full_result=build_full_result)

        self.last_bar_time = t
        self.bars_seen += 1

        fills: list[Fill] = []
        now_open = self.ft.current_trade is not None
        now_closed = len(self.ft.trade_history)

        if now_closed > before_closed:
            tr = self.ft.trade_history[-1]
            fills.append(Fill(symbol=self.symbol, side="exit",
                              time=int(tr.exit_time or t),
                              price=float(tr.exit_price or 0.0),
                              size_sol=float(tr.size_sol),
                              size_tokens=float(tr.size_tokens),
                              reason=str(tr.exit_reason or ""),
                              trade_index=now_closed - 1,
                              pnl_sol=float(tr.pnl_sol),
                              fee=float(tr.fees_paid)))
        if now_open and not before_open and self.ft.current_trade is not None:
            tr = self.ft.current_trade
            fills.append(Fill(symbol=self.symbol, side="buy",
                              time=int(tr.entry_time),
                              price=float(tr.entry_price),
                              size_sol=float(tr.size_sol),
                              size_tokens=float(tr.size_tokens),
                              reason=str(tr.entry_reason or ""),
                              trade_index=len(self.ft.trade_history),
                              fee=float(tr.fees_paid)))

        return BarDecision(symbol=self.symbol, time=t, fills=fills,
                           desired_position=1 if now_open else 0,
                           last_price=c)

    # ── bulk replay ───────────────────────────────────────────────────────
    def warm(self, candles: Iterable[dict]) -> None:
        """Replay history exactly as the backtester does (trades included).

        Engine state after `warm` is identical to a backtest that processed the
        same candles, so every decision from here on matches.  The live account
        reconciles to `desired_position` at the current market price; the
        synthetic historical fills are for engine state only.
        """
        for c in candles:
            self.on_bar(c)

    def force_close(self, candle: dict, reason: str = "recording_ended") -> Optional[Fill]:
        """Tape-end force-close, same call the runner makes."""
        if self.ft.current_trade is None:
            return None
        o, h, l, c = (float(candle["open"]), float(candle["high"]),
                      float(candle["low"]), float(candle["close"]))
        t = int(candle["time"])
        before = len(self.ft.trade_history)
        self.ft._close_long(o, h, l, c, t, reason=reason)
        if len(self.ft.trade_history) > before:
            tr = self.ft.trade_history[-1]
            return Fill(symbol=self.symbol, side="exit",
                        time=int(tr.exit_time or t),
                        price=float(tr.exit_price or 0.0),
                        size_sol=float(tr.size_sol),
                        size_tokens=float(tr.size_tokens),
                        reason=str(tr.exit_reason or ""),
                        trade_index=before,
                        pnl_sol=float(tr.pnl_sol), fee=float(tr.fees_paid))
        return None

    # ── introspection ─────────────────────────────────────────────────────
    @property
    def desired_position(self) -> int:
        return 1 if self.ft.current_trade is not None else 0

    @property
    def balance(self) -> float:
        return float(self.ft.balance)

    def trades(self) -> list[dict]:
        return [t.to_dict() for t in self.ft.trade_history]

    def stats(self) -> dict:
        return self.ft.stats.to_dict()

    def reset_pnl_tracking_only(self) -> None:
        """Deployment helper: keep engine state, zero the synthetic ledger.

        Does NOT touch engine state, so decisions are unchanged.  Use after a
        warm replay when the live account's own ledger is authoritative.
        """
        self.ft.balance = float(self.ft.stats.starting_balance)
        self.ft.trade_history = []
        self.ft.stats = type(self.ft.stats)(starting_balance=self.ft.balance)
        self._closed_count = 0


def load_engine_params(results_file: str, symbol: Optional[str] = None) -> dict:
    """Frozen params from a labeled batch result (parity default)."""
    import json
    from pathlib import Path
    p = Path(results_file)
    if p.is_dir():
        cand = sorted(p.glob("*.json"))
        if symbol:
            named = [c for c in cand if f"_{symbol}_" in c.name]
            cand = named or cand
        for c in cand:
            try:
                d = json.loads(c.read_text())
            except Exception:
                continue
            if isinstance(d, dict) and d.get("engine_params"):
                return dict(d["engine_params"])
        raise FileNotFoundError(f"no engine_params in {p}")
    d = json.loads(p.read_text())
    return dict(d["engine_params"])


def params_map(results_dir: str, market: str, interval: str,
               label: str) -> dict[str, dict]:
    """{symbol: frozen engine_params} for a whole labeled batch."""
    import json
    from pathlib import Path
    out: dict[str, dict] = {}
    for f in sorted(Path(results_dir).glob("*.json")):
        if f.name.startswith(("batch_", "summary", "mc_", "stats_")):
            continue
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("market") != market or d.get("interval") != interval:
            continue
        if label and (d.get("label") or "") != label:
            continue
        if d.get("engine_params") and d.get("symbol"):
            out[str(d["symbol"])] = dict(d["engine_params"])
    return out
