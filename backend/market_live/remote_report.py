"""remote_report.py — write the dashboard-ready account snapshot.

Runs on the remote (GitHub Actions) after each daily step and writes the
files the dashboard reads from the repo:

    runtime/account.json  equity, cash, realized, fees, positions, guard
                          phase/floors/counters, updated_at
    runtime/equity.json   the equity curve  [[epoch, equity], ...]
    runtime/trades.json   {"open": [...], "closed": [...]}  round trips

It also honours `runtime/paused`: when that file exists the runner makes no
new decisions (the sim is stopped) but still refreshes the report.

    python -m market_live.remote_report --root /path/to/repo
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


def _round_trips(fills: list[dict]) -> tuple[list[dict], list[dict]]:
    """Pair sequential buy/sell fills into round trips (long-only book)."""
    open_t = None
    closed: list[dict] = []
    for f in fills:
        try:
            qty = float(f.get("qty") or 0.0)
            px = float(f.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
        side = str(f.get("side") or "").lower()
        ts = float(f.get("time") or 0.0)
        if side == "buy":
            if open_t is None:
                open_t = {"symbol": f.get("symbol"), "qty": qty,
                          "entry_price": px, "entry_time": ts,
                          "entry_reason": f.get("reason", ""),
                          "notional": qty * px}
            else:                                  # top-up → weighted entry
                tot = open_t["qty"] + qty
                open_t["entry_price"] = ((open_t["entry_price"] * open_t["qty"]
                                          + px * qty) / tot) if tot else px
                open_t["qty"] = tot
                open_t["notional"] = open_t["entry_price"] * tot
        elif side == "sell" and open_t is not None:
            sell_qty = min(open_t["qty"], qty)
            pnl = sell_qty * (px - open_t["entry_price"])
            closed.append({
                "symbol": open_t["symbol"],
                "qty": sell_qty,
                "entry_price": round(open_t["entry_price"], 4),
                "exit_price": round(px, 4),
                "entry_time": open_t["entry_time"],
                "exit_time": ts,
                "pnl": round(pnl, 2),
                "pnl_pct": round((px / open_t["entry_price"] - 1.0) * 100.0, 3)
                if open_t["entry_price"] else 0.0,
                "entry_reason": open_t.get("entry_reason", ""),
                "exit_reason": f.get("reason", ""),
                "days": round((ts - open_t["entry_time"]) / 86400.0, 2)
                if open_t["entry_time"] and ts else None,
            })
            open_t["qty"] -= sell_qty
            if open_t["qty"] <= 1e-9:
                open_t = None
    open_rows = []
    if open_t is not None:
        open_rows.append({"symbol": open_t["symbol"], "qty": open_t["qty"],
                           "entry_price": round(open_t["entry_price"], 4),
                           "entry_time": open_t["entry_time"],
                           "notional": round(open_t["notional"], 2),
                           "entry_reason": open_t.get("entry_reason", "")})
    return open_rows, closed


def build_report(root: Path) -> dict:
    runtime = root / "runtime"
    state = {}
    try:
        state = json.loads((runtime / "state.json").read_text())
    except Exception:  # noqa: BLE001
        pass
    book = state.get("book") or {}
    positions = book.get("positions") or {}

    # guard state
    guard = {}
    try:
        guard = json.loads((runtime / "guard_state.json").read_text())
    except Exception:  # noqa: BLE001
        pass

    # fills from the journal (all sessions)
    fills: list[dict] = []
    journal = runtime / "journal"
    if journal.is_dir():
        for sess in sorted(journal.glob("2*")):
            f = sess / "orders.jsonl"
            if not f.exists():
                continue
            try:
                for line in f.read_text().splitlines():
                    try:
                        rec = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if rec.get("side") in ("buy", "sell"):
                        rec["time"] = rec.get("time") or rec.get("ts") or 0
                        fills.append(rec)
            except OSError:
                continue
    fills.sort(key=lambda r: float(r.get("time") or 0.0))
    open_rows, closed = _round_trips(fills)

    equity = book.get("equity")
    if equity is None:
        cash = float(book.get("cash") or 0.0)
        equity = cash + sum(float(p.get("qty") or 0.0) * float(p.get("avg_price") or 0.0)
                            for p in positions.values())

    account = {
        "equity": round(float(equity), 2),
        "cash": round(float(book.get("cash") or 0.0), 2),
        "realized": round(float(book.get("realized") or 0.0), 2),
        "fees": round(float(book.get("fees") or 0.0), 2),
        "n_fills": book.get("n_fills", len(fills)),
        "positions": [{"symbol": k, "qty": float(v.get("qty") or 0.0),
                       "avg_price": float(v.get("avg_price") or 0.0),
                       "notional": round(float(v.get("qty") or 0.0)
                                         * float(v.get("avg_price") or 0.0), 2)}
                      for k, v in positions.items()],
        "guard": guard,
        "n_closed_trades": len(closed),
        "n_open_positions": len(open_rows),
        "updated_at": time.time(),
        "updated_at_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    return {"account": account, "equity": equity, "open": open_rows,
            "closed": closed}


def write_report(root: Path) -> dict:
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    rep = build_report(root)
    account = rep["account"]

    (runtime / "account.json").write_text(json.dumps(account, indent=1))

    # equity curve: append the latest point (keep the series monotonic in time)
    curve = []
    try:
        curve = json.loads((runtime / "equity.json").read_text())
        if not isinstance(curve, list):
            curve = []
    except Exception:  # noqa: BLE001
        curve = []
    ts = time.time()
    if not curve or abs(float(curve[-1][1]) - float(rep["equity"])) > 1e-9 or \
            ts - float(curve[-1][0]) > 3600:
        curve.append([round(ts, 3), round(float(rep["equity"]), 2)])
    (runtime / "equity.json").write_text(json.dumps(curve[-2000:], indent=0))

    (runtime / "trades.json").write_text(json.dumps(
        {"open": rep["open"], "closed": rep["closed"][-500:]}, indent=1))
    return account


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    args = ap.parse_args()
    acc = write_report(Path(args.root).resolve())
    print(f"[report] equity={acc['equity']:,.2f} cash={acc['cash']:,.2f} "
          f"open={acc['n_open_positions']} closed={acc['n_closed_trades']}")


if __name__ == "__main__":
    main()
