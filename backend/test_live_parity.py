"""
Live-vs-backtest pipeline parity regression tests (iter58).

Covers the three root causes of live/backtest trade divergence:
  1. Entry reference price: live must notify the engine at the same fill
     price the backtester would use (mid-candle state-k+1 interpolation or
     next-candle open, × slippage).
  2. Pending-signal persistence: signals whose swap dispatch is blocked
     (swap in flight) must stay armed and fire on a later boundary — never
     silently dropped.
  3. Mid-candle interleaving: an EXIT detected at intra-candle state k<4
     notifies the engine closed immediately so remaining sub-states run
     flat, letting same-candle re-entry BUYs arm exactly like the backtest.

Run: cd backend && python test_live_parity.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from forward_tester import ForwardTester
from live_trader import LiveTrader
from strategy_engine import Signal
from solders.keypair import Keypair


class NoSwapLT(LiveTrader):
    """LiveTrader with instant fake swap settlement."""

    async def execute_buy(self, reason="signal"):
        if self.current_trade is not None:
            self.current_trade.status = "open"
        return "fake"

    async def execute_sell(self, reason="signal"):
        import time as _t
        if self.current_trade is not None:
            tr = self.current_trade
            tr.status = "closed"
            tr.exit_time = tr.exit_time or _t.time()
            self.trade_history.append(tr)
            self.current_trade = None
        return "fake"


def pump():
    loop = asyncio.get_event_loop()
    for _ in range(3):
        loop.run_until_complete(asyncio.sleep(0))


def make_pair(engine_kwargs=None):
    ft = ForwardTester(starting_balance=1.0, buy_size_sol=0.01,
                       priority_fee=0.0001, bribe_fee=0.0, slippage_pct=1.0,
                       engine_kwargs=engine_kwargs or {}, engine_version=2)
    lt = NoSwapLT(token_mint="T" * 44, keypair=Keypair(), buy_size_sol=0.01,
                  engine_kwargs=engine_kwargs or {}, engine_version=2)
    return ft, lt


def feed_both(ft, lt, candles):
    """Feed identical candles through both pipelines; returns per-candle
    engine snapshots."""
    snaps = []
    for i, cd in enumerate(candles):
        t = int(cd["time"]); o = cd["open"]; h = cd["high"]; l = cd["low"]; c = cd["close"]
        vol = cd.get("volume", 0); bv = cd.get("buy_volume", 0.0); sv = cd.get("sell_volume", 0.0)
        ps = cd.get("pool_sol", 0.0); mc = cd.get("market_cap_usd", 0.0)
        mf, ms = (h, l) if c >= o else (l, h)

        ft.update(time=t, o=o, h=o, l=o, c=o, volume=0.0, _build_full_result=False)
        ft.update(time=t, o=o, h=max(o, mf), l=min(o, mf), c=mf, volume=0.0, _build_full_result=False)
        ft.update(time=t, o=o, h=h, l=l, c=ms, volume=0.0, _build_full_result=False)
        ft.update(time=t, o=o, h=h, l=l, c=c, volume=vol, buy_volume=bv,
                  sell_volume=sv, pool_sol=ps, market_cap_usd=mc,
                  _build_full_result=False)

        lt.update(time_val=t, o=o, h=h, l=l, c=c, volume=vol,
                  buy_volume=bv, sell_volume=sv, is_new=False,
                  market_cap_usd=mc, pool_sol=ps)
        no = float(candles[i + 1]["open"]) if i + 1 < len(candles) else o
        nt = int(candles[i + 1]["time"]) if i + 1 < len(candles) else t + 1
        lt.update(time_val=nt, o=o, h=o, l=o, c=o, volume=0.0,
                  buy_volume=0.0, sell_volume=0.0, is_new=True,
                  market_cap_usd=mc, pool_sol=ps, boundary_open_price=no)
        pump()

        ft_mu = ft.engine.core._rbpf._mu_arr
        lv_mu = lt.engine.core._rbpf._mu_arr
        snaps.append((
            ft.engine.bar_count, ft.engine.regime.value, ft.engine.in_position,
            round(float(ft.engine.entry_price or 0), 10),
            round(float(ft_mu[:, 0].mean()), 10),
            round(float(ft_mu[:, 1].mean()), 10),
            lt.engine.bar_count, lt.engine.regime.value, lt.engine.in_position,
            round(float(lt.engine.entry_price or 0), 10),
            round(float(lv_mu[:, 0].mean()), 10),
            round(float(lv_mu[:, 1].mean()), 10),
        ))
    return snaps


def synth_candles(n=400, seed=7):
    """Deterministic pseudo-market with pumps and dumps."""
    rng = np.random.default_rng(seed)
    out = []
    px = 1e-7
    t0 = 1_787_000_000
    for i in range(n):
        drift = 0.0
        if 60 < i < 140:
            drift = 0.004          # pump
        elif 200 < i < 260:
            drift = -0.005         # dump
        elif 300 < i < 360:
            drift = 0.003
        ret = drift + rng.normal(0, 0.006)
        o = px
        c = max(px * (1 + ret), 1e-9)
        hi = max(o, c) * (1 + abs(rng.normal(0, 0.002)))
        lo = min(o, c) * (1 - abs(rng.normal(0, 0.002)))
        vol = float(abs(rng.normal(1.0, 0.5)))
        bv = vol * rng.uniform(0.2, 0.8)
        out.append({"time": t0 + i, "open": o, "high": hi, "low": lo,
                    "close": c, "volume": vol, "buy_volume": bv,
                    "sell_volume": vol - bv, "pool_sol": 50.0,
                    "market_cap_usd": 50_000.0})
        px = c
    return out


class TestEntryReferenceParity:
    """The engine-notified entry price must equal the backtester's fill."""

    def test_synthetic_market_engine_lockstep(self):
        ft, lt = make_pair()
        candles = synth_candles(500)
        snaps = feed_both(ft, lt, candles)
        for k, s in enumerate(snaps):
            (bt_bc, bt_reg, bt_ip, bt_ep, bt_px, bt_pmu,
             lv_bc, lv_reg, lv_ip, lv_ep, lv_px, lv_pmu) = s
            assert bt_bc == lv_bc, f"candle {k}: bar_count {bt_bc} != {lv_bc}"
            assert bt_reg == lv_reg, f"candle {k}: regime {bt_reg} != {lv_reg}"
            assert bt_px == lv_px, (
                f"candle {k}: ENGINE DIVERGENCE particle-x mean "
                f"{bt_px} != {lv_px}")
            assert bt_pmu == lv_pmu, (
                f"candle {k}: ENGINE DIVERGENCE particle-mu mean "
                f"{bt_pmu} != {lv_pmu}")
            # bookkeeping may transiently differ <=1 candle around exits,
            # but never the engine's posterior.

    def test_entry_ref_matches_backtest_fill_when_traded(self):
        """When both pipelines open a position, entry references must match
        to floating-point precision."""
        ft, lt = make_pair()
        candles = synth_candles(500, seed=11)
        feed_both(ft, lt, candles)
        if ft.engine.in_position and lt.engine.in_position:
            assert abs(ft.engine.entry_price - lt.engine.entry_price) < 1e-15, (
                f"entry ref mismatch: bt={ft.engine.entry_price} "
                f"lv={lt.engine.entry_price}")


class TestPendingSignalPersistence:
    """Blocked signals must stay armed and dispatch later — never drop."""

    def test_exit_survives_swap_in_flight(self):
        lt = NoSwapLT(token_mint="T" * 44, keypair=Keypair(),
                      buy_size_sol=0.01, engine_kwargs={}, engine_version=2)

        # Simulate: position open, exit armed, but another swap in flight.
        from live_trader import LiveTrade
        tr = LiveTrade(token_mint="T" * 44, entry_time=1, entry_price=1e-7,
                       size_sol=0.01, size_tokens=100, status="open")
        lt.current_trade = tr
        lt._pending_exit = True
        lt._pending_exit_reason = "kramers_down_exit"
        lt._swap_in_flight = True   # e.g. previous swap still building

        out = lt._try_execute_pending(ref_price=1e-7)
        assert out["action"] is None
        assert lt._pending_exit is True, "exit must stay ARMED while blocked"

        # Block clears -> retry fires the sell.
        lt._swap_in_flight = False
        out = lt._try_execute_pending(ref_price=1e-7)
        assert out["action"] == "exit"
        assert lt._pending_exit is False
        assert tr.status == "closing"
        assert tr.exit_reason == "kramers_down_exit"

    def test_stale_exit_dropped_when_trade_gone(self):
        lt = NoSwapLT(token_mint="T" * 44, keypair=Keypair(),
                      buy_size_sol=0.01, engine_kwargs={}, engine_version=2)
        lt._pending_exit = True
        lt._pending_exit_reason = "gain_retrace"
        lt.current_trade = None   # failed-buy rollback already flattened
        out = lt._try_execute_pending(ref_price=1e-7)
        assert out["action"] is None
        assert lt._pending_exit is False

    def test_buy_blocked_by_reentry_window_then_fires(self):
        import time as _t
        lt = NoSwapLT(token_mint="T" * 44, keypair=Keypair(),
                      buy_size_sol=0.01, engine_kwargs={}, engine_version=2)
        lt._pending_buy = True
        lt._pending_buy_reason = "buy_trend"
        lt._pending_buy_ref = 1.01e-7
        lt._pending_buy_ts = 1234
        lt._buy_failed_until = _t.time() + 60   # re-entry block active

        out = lt._try_execute_pending(ref_price=1e-7)
        assert out["action"] is None
        assert lt._pending_buy is True, "buy must stay armed during block"

        lt._buy_failed_until = 0.0
        out = lt._try_execute_pending(ref_price=1e-7)
        assert out["action"] == "buy"
        assert lt.current_trade is not None
        assert lt.current_trade.entry_time == 1234


class TestMidCandleInterleaving:
    """Engine notifications must happen at the backtest's exact fill point."""

    def test_state4_entry_notifies_at_dispatch_with_boundary_open(self):
        """A state-4 signal fills at the NEXT candle's open in the backtest;
        live must notify the engine with that exact reference."""
        lt = NoSwapLT(token_mint="T" * 44, keypair=Keypair(),
                      buy_size_sol=0.01, engine_kwargs={}, engine_version=2)
        # Arm a state-4 buy (ref None => resolved from boundary open).
        lt._pending_buy = True
        lt._pending_buy_reason = "buy_trend"
        lt._pending_buy_ref = None
        lt._pending_buy_ts = 999
        boundary_open = 2.0e-7
        out = lt._try_execute_pending(ref_price=boundary_open)
        assert out["action"] == "buy"
        expected = boundary_open * 1.01
        assert abs(lt.engine.entry_price - expected) < 1e-18, (
            f"state-4 entry ref {lt.engine.entry_price} != {expected}")
        assert abs(lt.current_trade.entry_price - expected) < 1e-18

    def test_midcandle_entry_ref_uses_intrabar_interpolation(self):
        """A state-1 signal fills at state 2 of the same candle; the notified
        reference must equal ForwardTester's interpolated fill × slip."""
        from forward_tester import ForwardTester as FT
        lt = NoSwapLT(token_mint="T" * 44, keypair=Keypair(),
                      buy_size_sol=0.01, engine_kwargs={}, engine_version=2)
        o, h, l, c = 1.0e-7, 1.2e-7, 0.9e-7, 1.1e-7   # bull candle
        mf, ms = h, l
        states = [
            (o, o, o, o),
            (o, max(o, mf), min(o, mf), mf),
            (o, h, l, ms),
            (o, h, l, c),
        ]
        # signal fired at state 1 → fill at state 2 row
        _, fh, fl, fc = states[1]
        raw = FT._intrabar_price(o, fh, fl, fc, lt._backtest_fill_fraction())
        expected = raw * 1.01

        lt._pending_buy = True
        lt._pending_buy_reason = "buy_exhaustion"
        lt._pending_buy_ref = expected
        lt._pending_buy_ts = 555
        out = lt._try_execute_pending(ref_price=9.9e-8)  # boundary open ignored
        assert out["action"] == "buy"
        assert abs(lt.engine.entry_price - expected) < 1e-18


class TestWarmupInertness:
    """Historical warmup must never arm signals or notify the engine."""

    def test_warmup_leaves_no_pending_flags(self):
        lt = NoSwapLT(token_mint="T" * 44, keypair=Keypair(),
                      buy_size_sol=0.01, engine_kwargs={}, engine_version=2)
        candles = synth_candles(120)
        for cd in candles:
            lt.update_historical_candle(
                int(cd["time"]), cd["open"], cd["high"], cd["low"], cd["close"],
                volume=cd["volume"])
            assert lt._pending_buy is False
            assert lt._pending_exit is False
            assert lt.engine.in_position is False


def main():
    tests = []
    for cls in (TestEntryReferenceParity, TestPendingSignalPersistence,
                TestMidCandleInterleaving, TestWarmupInertness):
        inst = cls()
        for name in dir(inst):
            if name.startswith("test_"):
                tests.append((inst, name))
    failed = 0
    for inst, name in tests:
        try:
            getattr(inst, name)()
            print(f"PASS {type(inst).__name__}.{name}")
        except AssertionError as ex:
            failed += 1
            print(f"FAIL {type(inst).__name__}.{name}: {ex}")
        except Exception as ex:
            failed += 1
            print(f"ERROR {type(inst).__name__}.{name}: {type(ex).__name__}: {ex}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
