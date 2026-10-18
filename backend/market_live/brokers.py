"""brokers.py — execution adapters with a truthful local ledger.

Design rule: the ENGINE decides; the broker only executes the target position.
`MarketEngineHost.desired_position` is the single source of truth, so a broker
that fills reliably produces a trade book identical to the backtester's
(modulo the slippage model, which is shared).

Brokers
-------
PaperBroker        local ledger, fills at bar close ± the backtest COSTS
AlpacaBroker       Alpaca REST v2 (paper endpoint by default) — US equities
HttpBridgeBroker   generic JSON-over-HTTP bridge (MT5 / cTrader / custom)
MT5Broker          HttpBridgeBroker preset for a MetaTrader 5 bridge

Sizing: notional = clamp(k * buy_size * equity, ...) — the ONLY live-specific
number.  It scales the position, never the decision.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BrokerFill:
    symbol: str
    side: str                # "buy" | "sell"
    qty: float
    price: float
    notional: float
    fee: float
    time: float
    order_id: str = ""
    reason: str = ""
    synthetic: bool = False  # warm-start reconciliation print

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "side": self.side, "qty": self.qty,
            "price": self.price, "notional": self.notional, "fee": self.fee,
            "time": self.time, "order_id": self.order_id,
            "reason": self.reason, "synthetic": self.synthetic,
        }


class Broker:
    name = "base"

    def equity(self) -> float:
        raise NotImplementedError

    def position_qty(self, symbol: str) -> float:
        raise NotImplementedError

    def target_position(self, symbol: str, target_qty: float,
                        ref_price: float, reason: str = "") -> Optional[BrokerFill]:
        raise NotImplementedError

    def flatten_all(self, prices: dict[str, float], reason: str = "flatten") -> list[BrokerFill]:
        raise NotImplementedError

    def state(self) -> dict:
        raise NotImplementedError


class PaperBroker(Broker):
    """Local ledger.  Fills mirror the backtest cost model exactly."""

    name = "paper"

    def __init__(self, starting_equity: float = 100_000.0,
                 slippage_pct: float = 0.03, fee_bps: float = 0.0,
                 min_commission: float = 0.0):
        self.starting_equity = float(starting_equity)
        self.cash = float(starting_equity)
        self.slippage_pct = float(slippage_pct)
        self.fee_bps = float(fee_bps)
        self.min_commission = float(min_commission)
        self.positions: dict[str, dict] = {}     # symbol -> {qty, avg_price}
        self.realized = 0.0
        self.fees = 0.0
        self.fills: list[BrokerFill] = []

    # ── accounting ────────────────────────────────────────────────────────
    def position_qty(self, symbol: str) -> float:
        return float(self.positions.get(symbol, {}).get("qty", 0.0))

    def equity(self, prices: Optional[dict[str, float]] = None) -> float:
        eq = self.cash
        for sym, p in self.positions.items():
            px = (prices or {}).get(sym, p["avg_price"])
            eq += p["qty"] * float(px)
        return eq

    def state(self) -> dict:
        return {"cash": self.cash, "realized": self.realized, "fees": self.fees,
                "positions": {k: dict(v) for k, v in self.positions.items()},
                "equity": self.equity(), "n_fills": len(self.fills)}

    # ── execution ─────────────────────────────────────────────────────────
    def _fee(self, notional: float) -> float:
        return max(self.min_commission, abs(notional) * self.fee_bps / 10_000.0)

    def target_position(self, symbol: str, target_qty: float,
                        ref_price: float, reason: str = "",
                        synthetic: bool = False) -> Optional[BrokerFill]:
        cur = self.position_qty(symbol)
        delta = float(target_qty) - cur
        if abs(delta) < 1e-12:
            return None
        px = float(ref_price)
        if px <= 0:
            return None
        slip = self.slippage_pct / 100.0
        fill_px = px * (1.0 + slip) if delta > 0 else px * (1.0 - slip)
        notional = abs(delta) * fill_px
        fee = self._fee(notional)

        # cash + realized update (long-only)
        if delta > 0:
            self.cash -= notional + fee
            prev = self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})
            new_qty = prev["qty"] + delta
            avg = ((prev["qty"] * prev["avg_price"]) + delta * fill_px) / new_qty \
                if new_qty else 0.0
            self.positions[symbol] = {"qty": new_qty, "avg_price": avg}
        else:
            prev = self.positions[symbol]
            sold = min(prev["qty"], -delta)
            pnl = sold * (fill_px - prev["avg_price"])
            self.realized += pnl
            self.cash += sold * fill_px - fee
            new_qty = prev["qty"] - sold
            if new_qty <= 1e-12:
                self.positions.pop(symbol, None)
            else:
                self.positions[symbol] = {"qty": new_qty,
                                          "avg_price": prev["avg_price"]}
        self.fees += fee
        f = BrokerFill(symbol=symbol, side="buy" if delta > 0 else "sell",
                       qty=abs(delta), price=fill_px, notional=notional,
                       fee=fee, time=time.time(), order_id=f"paper-{uuid.uuid4().hex[:10]}",
                       reason=reason, synthetic=synthetic)
        self.fills.append(f)
        return f

    def flatten_all(self, prices: dict[str, float],
                    reason: str = "flatten") -> list[BrokerFill]:
        out = []
        for sym in list(self.positions):
            f = self.target_position(sym, 0.0, prices.get(sym, 0.0), reason)
            if f:
                out.append(f)
        return out


class AlpacaBroker(Broker):
    """Alpaca REST v2.  Defaults to the paper endpoint.

    Orders are sent as `market`/`cls` (market-on-close) for daily bars so the
    fill lands at the same instant the backtester fills (the bar close).
    """

    name = "alpaca"

    def __init__(self, key: Optional[str] = None, secret: Optional[str] = None,
                 paper: bool = True, time_in_force: Optional[str] = None):
        self.key = key or os.environ.get("APCA_API_KEY_ID", "")
        self.secret = secret or os.environ.get("APCA_API_SECRET_KEY", "")
        if not (self.key and self.secret):
            raise RuntimeError("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set")
        self.base = ("https://paper-api.alpaca.markets" if paper
                     else "https://api.alpaca.markets")
        self.paper = paper
        self.tif = time_in_force or "cls"

    # ── HTTP ──────────────────────────────────────────────────────────────
    def _req(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read().decode()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode()[:400]
                if exc.code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"alpaca {method} {path} -> {exc.code}: {detail}")
        return {}

    # ── broker API ────────────────────────────────────────────────────────
    def equity(self) -> float:
        acct = self._req("GET", "/v2/account")
        return float(acct.get("equity", 0.0))

    def position_qty(self, symbol: str) -> float:
        try:
            p = self._req("GET", f"/v2/positions/{symbol}")
            return float(p.get("qty", 0.0))
        except RuntimeError as exc:
            if "404" in str(exc):
                return 0.0
            raise

    def target_position(self, symbol: str, target_qty: float,
                        ref_price: float, reason: str = "") -> Optional[BrokerFill]:
        cur = self.position_qty(symbol)
        delta = float(target_qty) - cur
        if abs(delta) * float(ref_price) < 1.0:      # < $1 notional → skip
            return None
        body = {
            "symbol": symbol,
            "qty": f"{abs(delta):.6f}",
            "side": "buy" if delta > 0 else "sell",
            "type": "market",
            "time_in_force": self.tif,
            "client_order_id": f"mkt-{symbol}-{uuid.uuid4().hex[:12]}",
        }
        resp = self._req("POST", "/v2/orders", body)
        return BrokerFill(symbol=symbol,
                          side="buy" if delta > 0 else "sell",
                          qty=abs(delta),
                          price=float(resp.get("filled_avg_price") or ref_price),
                          notional=abs(delta) * float(ref_price),
                          fee=0.0, time=time.time(),
                          order_id=str(resp.get("id", "")), reason=reason)

    def flatten_all(self, prices: dict[str, float],
                    reason: str = "flatten") -> list[BrokerFill]:
        resp = self._req("DELETE", "/v2/positions")
        return [BrokerFill(symbol=str(r.get("symbol", "")), side="sell",
                           qty=float(r.get("qty", 0) or 0),
                           price=float(r.get("current_price", 0) or 0),
                           notional=0.0, fee=0.0, time=time.time(),
                           reason=reason) for r in (resp or [])]

    def state(self) -> dict:
        acct = self._req("GET", "/v2/account")
        pos = self._req("GET", "/v2/positions")
        return {"equity": float(acct.get("equity", 0.0)),
                "cash": float(acct.get("cash", 0.0)),
                "buying_power": float(acct.get("buying_power", 0.0)),
                "positions": pos, "paper": self.paper}


class HttpBridgeBroker(Broker):
    """Generic JSON bridge.  Implement these four endpoints anywhere:

        GET  {base}/equity                      -> {"equity": 100000.0}
        GET  {base}/position?symbol=X           -> {"qty": 0.0}
        POST {base}/order  {symbol, qty, side, reason} -> {"price":..., "id":...}
        POST {base}/flatten {reason}            -> [ {symbol, qty, price}, ... ]

    A MetaTrader 5 bridge (Windows + `MetaTrader5` package) is ~80 lines and is
    the path to FTMO/MT5 prop accounts; `MT5Broker` presets the same interface.
    """

    name = "bridge"

    def __init__(self, base_url: str, token: str = ""):
        self.base = base_url.rstrip("/")
        self.token = token

    def _req(self, method: str, path: str, body: Optional[dict] = None):
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}

    def equity(self) -> float:
        return float(self._req("GET", "/equity").get("equity", 0.0))

    def position_qty(self, symbol: str) -> float:
        return float(self._req("GET", f"/position?symbol={symbol}").get("qty", 0.0))

    def target_position(self, symbol: str, target_qty: float,
                        ref_price: float, reason: str = "") -> Optional[BrokerFill]:
        cur = self.position_qty(symbol)
        delta = float(target_qty) - cur
        if abs(delta) < 1e-12:
            return None
        r = self._req("POST", "/order", {
            "symbol": symbol, "qty": abs(delta),
            "side": "buy" if delta > 0 else "sell", "reason": reason})
        return BrokerFill(symbol=symbol, side="buy" if delta > 0 else "sell",
                          qty=abs(delta), price=float(r.get("price", ref_price)),
                          notional=abs(delta) * float(ref_price),
                          fee=float(r.get("fee", 0.0)), time=time.time(),
                          order_id=str(r.get("id", "")), reason=reason)

    def flatten_all(self, prices: dict[str, float],
                    reason: str = "flatten") -> list[BrokerFill]:
        res = self._req("POST", "/flatten", {"reason": reason}) or []
        return [BrokerFill(symbol=str(r.get("symbol", "")), side="sell",
                           qty=float(r.get("qty", 0) or 0),
                           price=float(r.get("price", 0) or 0),
                           notional=0.0, fee=0.0, time=time.time(),
                           reason=reason) for r in res]

    def state(self) -> dict:
        return self._req("GET", "/state")


class MT5Broker(HttpBridgeBroker):
    """Preset for a local MT5 bridge (default port 8765)."""

    name = "mt5"

    def __init__(self, base_url: str = "http://127.0.0.1:8765", token: str = ""):
        super().__init__(base_url, token)


def build_broker(kind: str, **kwargs) -> Broker:
    if kind == "paper":
        return PaperBroker(**kwargs)
    if kind == "alpaca":
        return AlpacaBroker(**kwargs)
    if kind == "mt5":
        return MT5Broker(**kwargs)
    if kind == "bridge":
        return HttpBridgeBroker(**kwargs)
    raise ValueError(f"unknown broker {kind}")
