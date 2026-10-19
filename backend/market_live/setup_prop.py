"""setup_prop.py — preflight: prove the account is wired before you trade it.

Run this after buying the challenge and logging MT5 into the account.  It
checks every failure mode that would otherwise surface on the first real
session:

  1. data   — every symbol has a tape and frozen engine params
  2. broker — bridge reachable, account equity readable, login correct
  3. naming — each data symbol resolves on the broker (AAPL -> AAPL.US ...)
  4. sizing — target notional per position >= the broker's minimum lot
  5. guard  — firm template loads and floors compute against real equity

    cd backend
    ./.venv/bin/python -m market_live.setup_prop \
        --config market_live/config/prop_ftmo_100k.json

Exit code 0 = GO.  Anything else = NO-GO with the exact fix.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from market_backtest.runner import COSTS, MARKET_CLASS
from market_backtest.market_store import get_market_recording
from market_live.brokers import build_broker
from market_live.host import params_map
from market_live.prop_guard import PropGuard
from market_live.trader import LiveConfig


def _load_cfg(path: str) -> LiveConfig:
    d = json.loads(Path(path).read_text())
    d["symbols"] = tuple(d.get("symbols") or ())
    return LiveConfig(**{k: v for k, v in d.items()
                         if k in LiveConfig.__dataclass_fields__})


def preflight(cfg: LiveConfig, *, sample: int = 0,
              place_test_order: bool = False) -> bool:
    ok = True
    print(f"[preflight] {cfg.market}/{cfg.interval}  broker={cfg.broker}  "
          f"firm={cfg.firm}  k={cfg.k}")
    print(f"[preflight] sizing: {cfg.k*cfg.buy_size*100:.2f}% of equity per "
          f"position (${cfg.k*cfg.buy_size*cfg.starting_equity:,.0f} on "
          f"${cfg.starting_equity:,.0f})")

    # ── 1. data + frozen params ──────────────────────────────────────────
    params = params_map(cfg.results_dir, cfg.market, cfg.interval, cfg.label)
    missing_rec, missing_params = [], []
    for sym in cfg.symbols:
        try:
            if get_market_recording(cfg.market, sym, cfg.interval) is None:
                missing_rec.append(sym)
        except Exception:
            missing_rec.append(sym)
        if sym not in params:
            missing_params.append(sym)
    if missing_rec:
        ok = False
        print(f"  ✗ {len(missing_rec)} symbols without a tape: {missing_rec[:8]}")
    if missing_params:
        ok = False
        print(f"  ✗ {len(missing_params)} symbols without frozen params: "
              f"{missing_params[:8]}  -> run market_scan/sweep for this book")
    if not missing_rec and not missing_params:
        print(f"  ✓ data: {len(cfg.symbols)} symbols have tapes + frozen params")

    # ── 2. broker connectivity ───────────────────────────────────────────
    try:
        broker = build_broker(cfg.broker, **cfg.broker_kwargs)
        st = broker.state()
        eq = broker.equity()
        print(f"  ✓ broker: equity ${eq:,.2f}  state={ {k: v for k, v in st.items() if k != 'positions'} }")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ broker unreachable: {type(exc).__name__}: {exc}")
        print("    -> is mt5_bridge_server.py running and MT5 logged in?")
        return False

    if abs(eq - cfg.starting_equity) > max(1.0, 0.02 * cfg.starting_equity):
        print(f"  ! broker equity ${eq:,.2f} != configured "
              f"${cfg.starting_equity:,.2f} — set starting_equity in the config "
              f"to the account size")

    # ── 3. symbol naming ─────────────────────────────────────────────────
    syms = cfg.symbols if not sample else cfg.symbols[:sample]
    unresolved, mapped = [], {}
    for s in syms:
        try:
            matches = broker.symbols(s)
        except Exception:
            matches = []
        if matches:
            mapped[s] = matches[0]
        elif broker.symbol_info(s).get("symbol"):
            mapped[s] = broker.symbol_info(s)["symbol"]
        else:
            unresolved.append(s)
    if unresolved:
        ok = False
        print(f"  ✗ {len(unresolved)} symbols do not resolve on the broker: "
              f"{unresolved[:8]}")
        print("    -> add them to `broker_symbols` in the config")
    else:
        odd = {k: v for k, v in mapped.items() if k != v}
        print(f"  ✓ naming: {len(mapped)} symbols resolved"
              + (f"; remapped {list(odd.items())[:4]}" if odd else ""))

    # ── 4. sizing vs broker minimums ─────────────────────────────────────
    target_notional = cfg.k * cfg.buy_size * eq
    too_small = []
    for s in syms[:8]:
        try:
            info = broker.symbol_info(s)
        except Exception:
            continue
        if not info:
            continue
        cs = float(info.get("contract_size") or 1.0)
        vmin = float(info.get("volume_min") or 0.0)
        px = float(info.get("price") or 0.0)
        min_notional = vmin * cs * px
        if px and min_notional > target_notional:
            too_small.append((s, min_notional))
    if too_small:
        ok = False
        print(f"  ✗ position size below broker minimum for {len(too_small)} "
              f"symbols (target ${target_notional:,.0f}):")
        for s, mn in too_small[:6]:
            print(f"      {s}: min notional ${mn:,.2f}")
        print("    -> raise k, or trade a larger account")
    else:
        print(f"  ✓ sizing: ${target_notional:,.0f}/position clears minimums")

    # ── 5. guard ─────────────────────────────────────────────────────────
    if cfg.firm:
        g = PropGuard(cfg.firm, account_size=eq, log=lambda m: None)
        g.start_day(eq)
        st = g.status()
        print(f"  ✓ guard: {st['template']}  floor ${st['floor']:,.2f}  "
              f"daily floor ${st['daily_floor']:,.2f}  target {st['phase_target_pct']}%")
    else:
        print("  ! no firm template — running without prop rules")

    # ── 6. optional live order round-trip ────────────────────────────────
    if place_test_order:
        sym = syms[0]
        try:
            qty = max(1.0, target_notional / 1000.0)   # deliberately tiny
            f = broker.target_position(sym, qty, 1000.0, reason="preflight_test")
            print(f"  ✓ test order: {sym} {getattr(f, 'qty', 0)} units "
                  f"@{getattr(f, 'price', 0)}")
            broker.target_position(sym, 0.0, 1000.0, reason="preflight_close")
            print(f"  ✓ test order closed")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  ✗ test order failed: {type(exc).__name__}: {exc}")

    print("\n[preflight] " + ("GO — buy the account, then run the daily loop"
                              if ok else "NO-GO — fix the ✗ items above"))
    return ok


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--sample", type=int, default=0,
                    help="check only the first N symbols (0 = all)")
    ap.add_argument("--place-test-order", action="store_true",
                    help="send and close one tiny order (do this on the demo first)")
    args = ap.parse_args()
    cfg = _load_cfg(args.config)
    sys.exit(0 if preflight(cfg, sample=args.sample,
                            place_test_order=args.place_test_order) else 1)


if __name__ == "__main__":
    main()
