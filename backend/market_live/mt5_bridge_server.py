#!/usr/bin/env python3
"""mt5_bridge_server.py — HTTP bridge between `market_live` and MetaTrader 5.

Runs on the Windows machine/VPS where MT5 is installed and logged into the
prop-firm account (FTMO, or any MT5 broker).  It is the only MT5-aware process;
`market_live` talks plain JSON to it.

Endpoints (match `market_live.brokers.HttpBridgeBroker`)
--------------------------------------------------------
    GET  /health                     -> {ok, mt5, account, login, server}
    GET  /equity                     -> {equity, balance}
    GET  /position?symbol=AAPL       -> {qty, symbol}      (qty in SHARES)
    GET  /symbols?q=AAPL             -> {matches: ["AAPL.US", ...]}
    GET  /state                      -> {equity, cash, positions:[...]}
    POST /order   {symbol, qty, side, reason}
                                     -> {price, id, qty, lots, symbol}
    POST /flatten {reason}           -> [{symbol, qty, price}, ...]

Sizing
------
`market_live` sends **shares** (notional / price).  The bridge converts to lots
using the symbol's `trade_contract_size`, rounds to `volume_step`, clamps to
[min, max], and returns the filled shares-equivalent.

Symbol names
------------
Prop brokers rename symbols (`AAPL.US`, `AAPL.NASDAQ`, ...).  `/order` and
`/position` resolve the real name automatically from a candidate list and cache
it; `/symbols?q=` lets you inspect the mapping before going live.

Run
---
    pip install MetaTrader5
    python mt5_bridge_server.py --port 8765 --magic 77001
    python mt5_bridge_server.py --simulate          # no MT5: test the loop

Security: bind to 127.0.0.1 by default.  If you expose it, pass --token and the
client will send `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

try:
    import MetaTrader5 as mt5  # type: ignore
except Exception:  # noqa: BLE001  (macOS dev machines have no MT5)
    mt5 = None

CANDIDATE_SUFFIXES = ["", ".US", ".NASDAQ", ".NYSE", ".us", "US", "_US", ".cash"]


class MT5Bridge:
    def __init__(self, *, login: Optional[int] = None, password: str = "",
                 server: str = "", path: str = "", magic: int = 77001,
                 deviation: int = 25, simulate: bool = False,
                 contract_overrides: Optional[dict] = None):
        self.magic = int(magic)
        self.deviation = int(deviation)
        self.simulate = bool(simulate) or mt5 is None
        self.login = login
        self.password = password
        self.server = server
        self.path = path
        self.contract_overrides = dict(contract_overrides or {})
        self._lock = threading.RLock()
        self._sym_cache: dict[str, str] = {}
        self._sim_equity = 100_000.0
        self._sim_pos: dict[str, dict] = {}      # symbol -> {qty, avg}
        self._sim_ids: list[str] = []

    # ── lifecycle ─────────────────────────────────────────────────────────
    def connect(self) -> bool:
        if self.simulate:
            return True
        kwargs = {}
        if self.path:
            kwargs["path"] = self.path
        if self.login:
            kwargs.update(login=int(self.login), password=self.password,
                          server=self.server)
        if not mt5.initialize(**kwargs):
            print(f"[bridge] mt5.initialize failed: {mt5.last_error()}")
            return False
        info = mt5.account_info()
        print(f"[bridge] connected: login={getattr(info, 'login', '?')} "
              f"server={getattr(info, 'server', '?')} "
              f"balance={getattr(info, 'balance', 0):,.2f}")
        return True

    # ── symbols ───────────────────────────────────────────────────────────
    def resolve(self, symbol: str) -> str:
        if self.simulate:
            return symbol
        with self._lock:
            if symbol in self._sym_cache:
                return self._sym_cache[symbol]
            base = symbol.split(".")[0].split("_")[0]
            for cand in [symbol] + [base + s for s in CANDIDATE_SUFFIXES if s]:
                if mt5.symbol_info(cand) is not None:
                    self._sym_cache[symbol] = cand
                    return cand
            found = mt5.symbols_get(f"*{base}*") or []
            for s in found:
                self._sym_cache[symbol] = s.name
                return s.name
            self._sym_cache[symbol] = symbol
            return symbol

    def symbols(self, q: str = "") -> list[str]:
        if self.simulate:
            return [q] if q else []
        names = [s.name for s in (mt5.symbols_get() or [])]
        return [n for n in names if q.upper() in n.upper()][:200]

    def _info(self, sym: str):
        return None if self.simulate else mt5.symbol_info(sym)

    def contract_size(self, sym: str) -> float:
        if sym in self.contract_overrides:
            return float(self.contract_overrides[sym])
        info = self._info(sym)
        if info is not None and getattr(info, "trade_contract_size", 0):
            return float(info.trade_contract_size)
        return 1.0

    def _tick_price(self, sym: str, side: str) -> float:
        if self.simulate:
            px = getattr(self, "_sim_price", {}).get(sym) if hasattr(self, "_sim_price") else None
            return float(px or 100.0)
        tick = mt5.symbol_info_tick(sym)
        if tick is None:
            raise RuntimeError(f"no tick for {sym}")
        return float(tick.ask if side == "buy" else tick.bid)

    # ── account ───────────────────────────────────────────────────────────
    def equity(self) -> dict:
        if self.simulate:
            eq = self._sim_equity
            for sym, p in self._sim_pos.items():
                px = (getattr(self, "_sim_price", {}) or {}).get(sym, p["avg"])
                eq += p["qty"] * (px - p["avg"])
            return {"equity": eq, "balance": self._sim_equity}
        info = mt5.account_info()
        if info is None:
            raise RuntimeError("account_info() is None — MT5 not logged in")
        return {"equity": float(info.equity), "balance": float(info.balance)}

    def position_qty(self, symbol: str) -> float:
        sym = self.resolve(symbol)
        if self.simulate:
            return float(self._sim_pos.get(sym, {}).get("qty", 0.0))
        positions = mt5.positions_get(symbol=sym) or []
        cs = self.contract_size(sym)
        net = 0.0
        for p in positions:
            vol = float(p.volume) * cs
            net += vol if p.type == mt5.POSITION_TYPE_BUY else -vol
        return net

    # ── orders ────────────────────────────────────────────────────────────
    def _round_lots(self, sym: str, lots: float, info) -> float:
        if info is None:
            step = 0.01
            vmin, vmax = 0.01, 1e6
        else:
            step = float(getattr(info, "volume_step", 0.01) or 0.01)
            vmin = float(getattr(info, "volume_min", step) or step)
            vmax = float(getattr(info, "volume_max", 1e6) or 1e6)
        lots = round(lots / step) * step
        lots = max(vmin, min(vmax, lots))
        return round(lots, 8)

    def order(self, symbol: str, qty: float, side: str, reason: str = "") -> dict:
        """qty in SHARES (signed magnitude); side buy|sell."""
        sym = self.resolve(symbol)
        qty = abs(float(qty))
        if qty <= 0:
            raise ValueError("qty must be > 0")
        cs = self.contract_size(sym)
        info = self._info(sym)

        if self.simulate:
            px = self._tick_price(sym, side)
            with self._lock:
                pos = self._sim_pos.get(sym, {"qty": 0.0, "avg": 0.0})
                if side == "buy":
                    new_qty = pos["qty"] + qty
                    pos["avg"] = ((pos["qty"] * pos["avg"]) + qty * px) / new_qty
                    pos["qty"] = new_qty
                else:
                    sold = min(pos["qty"], qty)
                    self._sim_equity += sold * (px - pos["avg"])
                    pos["qty"] -= sold
                    if pos["qty"] <= 1e-9:
                        self._sim_pos.pop(sym, None)
                    else:
                        self._sim_pos[sym] = pos
                if side == "buy":
                    self._sim_pos[sym] = pos
                oid = f"sim-{uuid.uuid4().hex[:10]}"
                self._sim_ids.append(oid)
            return {"price": px, "id": oid, "qty": qty, "lots": qty / cs, "symbol": sym}

        lots = self._round_lots(sym, qty / cs, info)
        if lots <= 0:
            raise RuntimeError(f"{sym}: computed lots <= 0 (qty={qty} cs={cs})")

        if not mt5.symbol_select(sym, True):
            raise RuntimeError(f"symbol_select({sym}) failed")

        price = self._tick_price(sym, side)
        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL

        fillings = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK,
                    mt5.ORDER_FILLING_RETURN]
        last = None
        for attempt in range(3):
            for filling in fillings:
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": sym,
                    "volume": lots,
                    "type": order_type,
                    "price": price,
                    "deviation": self.deviation,
                    "magic": self.magic,
                    "comment": (reason or "market_live")[:30],
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": filling,
                }
                res = mt5.order_send(req)
                last = res
                if res is None:
                    time.sleep(0.4)
                    price = self._tick_price(sym, side)
                    continue
                if res.retcode == mt5.TRADE_RETCODE_DONE:
                    return {"price": float(res.price), "id": str(res.order),
                            "qty": float(res.volume) * cs, "lots": float(res.volume),
                            "symbol": sym}
                if res.retcode in (mt5.TRADE_RETCODE_REQUOTE,
                                   mt5.TRADE_RETCODE_PRICE_CHANGED,
                                   mt5.TRADE_RETCODE_PRICE_OFF):
                    time.sleep(0.3)
                    price = self._tick_price(sym, side)
                    continue
                if res.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
                    continue          # next filling mode
                break                 # a real error
            time.sleep(0.5)
        detail = getattr(last, "retcode", None), getattr(last, "comment", None)
        raise RuntimeError(f"order failed {sym} {side} {lots} lots: {detail}")

    def flatten(self, reason: str = "flatten") -> list[dict]:
        out = []
        if self.simulate:
            for sym, p in list(self._sim_pos.items()):
                qty = float(p["qty"])
                if qty > 0:
                    r = self.order(sym, qty, "sell", reason)
                    out.append({"symbol": sym, "qty": qty, "price": r["price"]})
            return out
        positions = mt5.positions_get() or []
        for p in positions:
            if self.magic and p.magic not in (0, self.magic):
                continue
            sym = p.symbol
            cs = self.contract_size(sym)
            qty = float(p.volume) * cs
            side = "sell" if p.type == mt5.POSITION_TYPE_BUY else "buy"
            r = self.order(sym, qty, side, reason)
            out.append({"symbol": sym, "qty": qty, "price": r["price"]})
        return out

    def state(self) -> dict:
        if self.simulate:
            return {"equity": self.equity()["equity"], "cash": self._sim_equity,
                    "positions": [{"symbol": k, "qty": v["qty"],
                                   "avg": v["avg"]} for k, v in self._sim_pos.items()],
                    "simulate": True}
        info = mt5.account_info()
        positions = mt5.positions_get() or []
        return {
            "equity": float(getattr(info, "equity", 0.0)),
            "cash": float(getattr(info, "balance", 0.0)),
            "login": getattr(info, "login", None),
            "server": getattr(info, "server", None),
            "currency": getattr(info, "currency", None),
            "positions": [{"symbol": p.symbol,
                           "qty": float(p.volume) * self.contract_size(p.symbol),
                           "lots": float(p.volume),
                           "avg": float(p.price_open),
                           "pnl": float(p.profit)} for p in positions],
            "simulate": False,
        }

    def symbol_info(self, symbol: str) -> dict:
        sym = self.resolve(symbol)
        if self.simulate:
            return {"symbol": sym, "contract_size": 1.0, "volume_min": 0.01,
                    "volume_max": 1000.0, "volume_step": 0.01,
                    "price": (getattr(self, "_sim_price", {}) or {}).get(sym, 100.0),
                    "trade_mode": "full", "simulate": True}
        info = mt5.symbol_info(sym)
        if info is None:
            raise RuntimeError(f"symbol_info({sym}) is None")
        tick = mt5.symbol_info_tick(sym)
        return {"symbol": sym,
                "contract_size": float(info.trade_contract_size),
                "volume_min": float(info.volume_min),
                "volume_max": float(info.volume_max),
                "volume_step": float(info.volume_step),
                "price": float(tick.ask if tick else info.last or 0.0),
                "trade_mode": int(getattr(info, "trade_mode", 0)),
                "simulate": False}

    def set_sim_price(self, symbol: str, price: float) -> None:
        if not hasattr(self, "_sim_price"):
            self._sim_price = {}
        self._sim_price[symbol] = float(price)


# ─────────────────────────────────────────────────────────────────────────────
# HTTP layer
# ─────────────────────────────────────────────────────────────────────────────
def make_handler(bridge: MT5Bridge, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter logs
            return

        def _send(self, code: int, payload) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _auth(self) -> bool:
            if not token:
                return True
            hdr = self.headers.get("Authorization", "")
            return hdr == f"Bearer {token}"

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode() or "{}")

        def do_GET(self):  # noqa: N802
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            try:
                from urllib.parse import urlparse, parse_qs
                u = urlparse(self.path)
                q = parse_qs(u.query)
                if u.path == "/health":
                    return self._send(200, {"ok": True, "mt5": not bridge.simulate,
                                            "simulate": bridge.simulate})
                if u.path == "/equity":
                    return self._send(200, bridge.equity())
                if u.path == "/position":
                    sym = (q.get("symbol") or [""])[0]
                    return self._send(200, {"qty": bridge.position_qty(sym),
                                            "symbol": sym})
                if u.path == "/symbolinfo":
                    return self._send(200, bridge.symbol_info(
                        (q.get("symbol") or [""])[0]))
                if u.path == "/symbols":
                    return self._send(200, {"matches": bridge.symbols(
                        (q.get("q") or [""])[0])})
                if u.path == "/state":
                    return self._send(200, bridge.state())
                return self._send(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_POST(self):  # noqa: N802
            if not self._auth():
                return self._send(401, {"error": "unauthorized"})
            try:
                from urllib.parse import urlparse
                path = urlparse(self.path).path
                body = self._body()
                if path == "/order":
                    if bridge.simulate and body.get("ref_price"):
                        bridge.set_sim_price(body.get("symbol", ""),
                                             float(body["ref_price"]))
                    r = bridge.order(body.get("symbol", ""),
                                     float(body.get("qty", 0)),
                                     body.get("side", "buy"),
                                     body.get("reason", ""))
                    return self._send(200, r)
                if path == "/flatten":
                    return self._send(200, bridge.flatten(body.get("reason", "flatten")))
                if path == "/price":       # simulation helper
                    bridge.set_sim_price(body.get("symbol", ""),
                                         float(body.get("price", 0)))
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            except Exception as exc:  # noqa: BLE001
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default="")
    ap.add_argument("--login", type=int, default=0)
    ap.add_argument("--password", default="")
    ap.add_argument("--server", default="")
    ap.add_argument("--terminal-path", default="")
    ap.add_argument("--magic", type=int, default=77001)
    ap.add_argument("--deviation", type=int, default=25)
    ap.add_argument("--simulate", action="store_true",
                    help="no MT5: in-memory account for loop testing")
    args = ap.parse_args()

    bridge = MT5Bridge(login=args.login or None, password=args.password,
                       server=args.server, path=args.terminal_path,
                       magic=args.magic, deviation=args.deviation,
                       simulate=args.simulate)
    if not bridge.connect():
        raise SystemExit("MT5 connection failed — is the terminal logged in?")
    if bridge.simulate:
        print("[bridge] SIMULATE mode (no MT5) — for plumbing tests only")

    srv = ThreadingHTTPServer((args.host, args.port),
                              make_handler(bridge, args.token))
    print(f"[bridge] listening on http://{args.host}:{args.port}  magic={args.magic}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[bridge] stopping")
        if not bridge.simulate and mt5:
            mt5.shutdown()


if __name__ == "__main__":
    main()
