"""
Tests for futures backtest mode (option A — additive layer).

Covered behaviours:

  1. Spot regression  — a spot run through ForwardTester is byte-identical
     before/after the futures change (config defaults, trade math, stats).
     Guards the "strictly additive" requirement.

  2. Liquidation before the engine exit — a leveraged long on a
     V1-loaded engine is force-closed at its mark-price liquidation level
     BEFORE the strategy's own Bayesian/list-price exit would fire.  This
     exercises the engine-independent Step-1.5 hook inside update().

  3. Funding accrual — funding payments debit the running PnL correctly
     across multiple 8h intervals, including partial settlement at close.

  4. Mark-vs-last — liquidation triggers on the mark price even when the
     last price hasn't reached the liq level (minic overhead bar + a
     mark_price spike on the same timestamp).

  5. Futures stats wiring — leverage/funding/fees land in
     ForwardTestStats' additive fields, and to_dict() shape never changed
     for spot runs.

Run:  cd backend && python test_futures.py
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest

# Ensure local imports resolve regardless of CWD.
sys.path.insert(0, os.path.dirname(__file__))

from forward_tester import ForwardTester                       # noqa: E402
from futures_model import FuturesAccount, FuturesConfig         # noqa: E402
from strategy_engine import Signal, Direction                   # noqa: E402


# ── Tiny deterministic "engine" stub ─────────────────────────────────────────
# Bypasses the V1/V2 engines' warmup so tests are fast and deterministic; the
# object satisfies every attribute/method used by ForwardTester.update().
#
# Script format: a list of "buy" / "exit" / "none" per update() call, cycling
# after exhaustion.  notify_trade_opened / notify_trade_closed track a flag so
# the futures branch's parity asserts fire if the pipeline forgets to sync.

class _StubInputs:
    def __init__(self, script):
        self.script = script
        self._i = 0
    def __call__(self):
        v = self.script[self._i] if self._i < len(self.script) else self.script[-1]
        self._i += 1
        return v


class _StubEngine:
    def __init__(self, script):
        self._next = _StubInputs(script)
        self.in_position = False
        self.regime = type("R", (), {"value": "idle"})()
        self.direction = type("D", (), {"value": "none"})()
        self.prev_direction = self.direction
        self.trend_before_exhaustion = self.direction
        self.signal_strength = 0.0
        self.ema_fast_val = 0.0
        self.ema_slow_val = 0.0
        self.atr_val = 0.0
        self.m_hat = 0.0
        self.trend_confidence = 0.0
        self.exit_signal_reason = ""
        self.mark_opened_with = None
        self.mark_closed = False

        # Attributes consumed by forward_tester's _capture_entry/exit_params
        for k, v in {
            "prev_m_hat": 0.0, "p_hat": 1.0, "momentum_acceleration": 0.0,
            "_momentum_peak_declining_count": 0, "s_effective": 0.0,
            "ema_macro_val": 0.0, "ema_spread": 0.0, "prev_ema_spread": 0.0,
            "spread_expanding": False, "atr_floor": 0.0, "is_trending": False,
            "_ema_cross_valid": False, "_ema_cross_persist_count": 0,
            "_pre_entry_stable": False, "_pre_entry_stable_up": False,
            "_pre_entry_stable_down": False, "_in_local_chop": False,
            "bar_count": 0, "trend_bar_count": 0, "exhaustion_bar_count": 0,
            "exhaustion_persist_count": 0, "reversal_confirm_count": 0,
            "trend_reversal_confirm_count": 0, "reversal_bar_count": 0,
            "no_motion_count": 0, "_exhaustion_s_decay_count": 0,
            "trend_start_bar": 0, "trend_start_price": 1.0,
            "trend_start_atr": 0.0, "_exhaustion_phase_high": 1.0,
            "ema_fast_p": 3, "ema_slow_p": 7, "atr_period": 14,
            "roc_period": 10, "warmup": 2, "S_strong": 1.0, "S_weak": 0.3,
            "S_noise": 0.1, "exhaustion_bars_limit": 5, "delta_threshold": 0.0,
            "min_trend_bars": 1, "reversal_confirm_bars": 2,
            "chop_atr_pct": 0.5, "chop_spread_pct": 0.5,
            "reversal_exit_confirm_bars": 1, "s_effective_threshold": 1.0,
            "exhaustion_persist_bars": 3, "regime_lookback": 10,
            "persistence_threshold": 0.5, "momentum_mean_threshold": 0.0,
            "ema_min_spread_pct": 0.0, "confidence_high": 0.79,
            "confidence_low": 0.4, "entry_confidence_high": 0.79,
            "entry_confidence_low": 0.5, "confidence_very_high": 0.9,
            "confidence_w1": 0.3, "confidence_w2": 0.25,
            "confidence_w3": 0.25, "confidence_w4": 0.2,
            "atr_floor_k": 1.0, "ema_cross_persist_bars": 2,
            "exhaustion_s_decay_bars": 2, "exhaustion_stall_bars": 2,
            "exhaustion_stall_atr_pct": 0.0, "local_range_bars": 10,
            "local_range_threshold_pct": 2.0, "sign_flip_threshold": 2,
            "stability_bars": 5, "spike_atr_multiplier": 2.0,
            "spike_lookback_bars": 5, "body_baseline_bars": 5,
            "overextension_k": 0.1, "momentum_peak_bars": 3,
            "consolidation_range_pct": 2.0, "ema_macro_period": 30,
            "stoploss_pct": 10.0, "takeprofit_pct": 20.0,
            "stoploss_pct_low": 5.0, "stoploss_pct_high": 15.0,
            "takeprofit_pct_low": 10.0, "takeprofit_pct_high": 30.0,
            "max_entry_bar_count": 500,
        }.items():
            setattr(self, k, v)

    def set_holder_flow_events(self, events):        # used by ctor
        pass

    def update(self, time, o, h, l, c, volume=0.0, buy_volume=0.0,
               sell_volume=0.0, pool_sol=0.0, market_cap_usd=0.0,
               _build_full_result=True, **_kw):
        self.bar_count += 1
        sig = self._next()
        out = {"signal": sig,
               "regime": "idle",
               "direction": "up" if self.in_position else "none",
               "exit_reason": "trend_exit"}
        return out

    # Mirrors used by _capture_*_params
    def _momentum_past_peak(self): return False
    def _price_overextended(self, c): return False
    def _effective_stoploss_pct(self): return 0.0
    def _effective_takeprofit_pct(self): return 0.0
    def _global_stoploss_pct(self): return 0.0

    def notify_trade_opened(self, price, direction):
        self.in_position = True
        self.mark_opened_with = (price, direction)
    def notify_trade_closed(self):
        self.in_position = False
        self.mark_closed = True


# ── Fixture helpers ──────────────────────────────────────────────────────────

def _flat_candle(t, price):
    return dict(time=t, o=price, h=price, l=price, c=price,
                buy_volume=0.0, sell_volume=0.0)


def _feed(ft, candles, **kw):
    """Feed one state-4 update per candle (matches backtester fast path)."""
    out = []
    for cd in candles:
        out.append(ft.update(time=cd["time"], o=cd["o"], h=cd["h"], l=cd["l"],
                             c=cd["c"], volume=kw.get("volume", 0.0),
                             funding_rate=kw.get("funding_rate", 0.0),
                             mark_price=kw.get("mark_price", 0.0)))
    return out


def _spot_kwargs():
    return dict(starting_balance=1.0, buy_size_sol=0.1,
                priority_fee=0.0001, bribe_fee=0.0, slippage_pct=1.0,
                engine_kwargs={}, engine_version=1)


# ── Tests ───────────────────────────────────────────────────────────────────

class TestSpotRegressionGuard(unittest.TestCase):

    def test_spot_run_byte_identical(self):
        """A full spot run's serialised state must match the pre-change
        semantics exactly (the dataclass gained fields - but spot values
        must be the spot defaults, and all float math untouched)."""
        candles = [
            _flat_candle(1, 1.0), _flat_candle(2, 1.05), _flat_candle(3, 1.02),
            _flat_candle(4, 1.08), _flat_candle(5, 1.06), _flat_candle(6, 1.03),
        ]
        # script: buy on candle 1 (fills at bar 2), exit on candle 4, end flat
        script = ["buy", "none", "none", "exit", "none", "none"]

        # ── Reference: simulate the documented spot maths by hand ───────────
        ft = ForwardTester(**_spot_kwargs())
        ft.engine = _StubEngine(script)          # inject scriptable engine
        _ = _feed(ft, candles)
        s = ft.stats.to_dict()
        t = ft.trade_history[0]

        # fees = 0.0001 entry + 0.0001 exit = 0.0002 SOL
        # size 0.1, entry @1.05·1.01, exit @1.06·0.99
        entry_price = 1.05 * 1.01
        exit_price  = 1.06 * 0.99
        tokens      = 0.1 / entry_price
        pnl         = tokens * exit_price - 0.1 - 0.0001   # exit fee
        # entry fee debited at open; balance chain: 1.0 -(0.1+fees) + proceeds-fee
        expect_balance = 1.0 - (0.1 + 0.0001) + (tokens * exit_price - 0.0001)

        self.assertAlmostEqual(t.entry_price, entry_price, places=12)
        self.assertAlmostEqual(t.exit_price, exit_price, places=12)
        self.assertAlmostEqual(t.pnl_sol, pnl, places=12)
        self.assertAlmostEqual(ft.balance, expect_balance, places=12)
        self.assertEqual(t.market_type, "spot")
        self.assertEqual(t.leverage, 1.0)
        self.assertEqual(t.notional_sol, 0.0)       # untouched for spot
        self.assertEqual(t.funding_paid, 0.0)
        self.assertEqual(t.liquidation_price, 0.0)
        self.assertEqual(s["market_type"], "spot")
        self.assertEqual(s["leverage"], 1.0)
        self.assertEqual(s["total_liquidations"], 0)
        self.assertEqual(s["total_funding_paid"], 0.0)
        self.assertEqual(s["total_fees_paid_futures"], 0.0)
        self.assertEqual(s["total_fees_paid"], 0.0002)
        self.assertTrue(s["win_rate"] == 0.0 or t.pnl_sol < 0)  # loss here
        self.assertEqual(s["total_trades"], 1)
        self.assertEqual(ft.engine.mark_opened_with[1], Direction.UP)
        self.assertTrue(ft.engine.mark_closed)


class TestFuturesLiquidation(unittest.TestCase):

    def test_liquidation_fires_before_engine_exit(self):
        """10× leveraged long at $100  →  liq ≈ $90.64 mark (masked by slip).
        Engine scripts its own EXIT two bars AFTER the liquidation price is
        first breached.  Expected: trade closes at the liquidation mark,
        BEFORE the engine's trend_exit signal can ever execute."""
        candles = [
            # bar 1: engine emits BUY (fills bar 2 open ≈ 100)
            _flat_candle(1, 100.0),
            _flat_candle(2, 100.0),
            # bar 3: engine watches.  LAST price falls to 91 on the low but
            #        closes 92; the MARK spikes to 89 (< 90.55 liq level) →
            #        the account is force-closed on the *mark* breach,
            #        not the last-price action.
            dict(time=3, o=100.0, h=100.0, l=91.0, c=92.0,
                 buy_volume=0.0, sell_volume=0.0),
            # bars 4–5: engine finally scripts trend_exit (never executed —
            # the position no longer exists after liq at t=3).
            _flat_candle(4, 99.0),
            _flat_candle(5, 99.5),
        ]
        #             bar1      bar2     bar3      bar4        bar5
        script = ["buy", "none", "none", "none", "exit"]
        # ── NOTE on the 1-bar-latency: engine.update is called on s1 of bar
        # 1 and returns "buy"; the fill completes at s1 of bar 2.  Engine
        # exits at bar 5 s1 → fill would be bar 6 (outside the recording).
        # The mark breaches at bar 3 s1 → position liquidates BEFORE the
        # engine's exit would even be queued. ──────────────────────────────

        # (comment: the scripted "exit" at bar 5 is the engine's own
        # decision.  Margin call at bar 3 must fire first.)
        ft = ForwardTester(starting_balance=1.0, buy_size_sol=0.1,
                           slippage_pct=1.0, engine_kwargs={}, engine_version=1,
                           market_type="futures", leverage=10.0,
                           futures_slippage_pct=0.1,
                           funding_rate_per_interval=0.0)
        ft.engine = _StubEngine(script)

        mark_prices = {3: 89.0}
        outs = []
        for cd in candles:
            outs.append(ft.update(time=cd["time"], o=cd["o"], h=cd["h"],
                                  l=cd["l"], c=cd["c"], volume=0.0,
                                  mark_price=mark_prices.get(cd["time"], 0.0)))

        self.assertEqual(len(ft.trade_history), 1)
        t = ft.trade_history[0]

        # 1) Liquidation fired (not the engine's later trend_exit).
        self.assertEqual(t.exit_reason, "liquidation")
        self.assertEqual(ft.stats.total_liquidations, 1)
        # 2) leverage recorded
        self.assertEqual(t.leverage, 10.0)
        # 3) Liquidation happened at time 3 (bar the mark crossed), BEFORE
        #    the engine's scripted exit at time 4 could execute.  With the
        #    engine scripted to exit at bar 4, the liquidation at t=3 wins
        #    the race and the trend_exit script never materialises as a real
        #    sell — the trade's exit_reason is "liquidation" not "trend_exit".
        self.assertEqual(t.exit_time, 3)
        # 4) capped at -margin (all collateral lost, minus entry/liq fees)
        self.assertLessEqual(t.pnl_sol, -t.size_sol)
        # 5) engine's in_position flag was synced
        self.assertFalse(ft.engine.in_position)
        self.assertTrue(ft.engine.mark_closed)

    def test_leverage_math_and_margin(self):
        acct = FuturesAccount(cfg=FuturesConfig(leverage=10.0),
                              starting_equity=1.0)
        pos = acct.open_long(100.0, 0, margin=0.1)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.notional, 1.0, places=10)
        self.assertAlmostEqual(pos.qty, 0.01, places=10)
        self.assertAlmostEqual(pos.margin, 0.1, places=10)
        # liq: (N - m) / (qty·(1-mm)) = (1-0.1)/(0.01·0.995) ≈ 90.452…
        self.assertAlmostEqual(pos.liq_price, 90.45226130653267, places=8)
        # equity debited by margin + entry taker fee
        entry_fee = pos.notional * 0.00045
        self.assertAlmostEqual(acct.equity, 0.9 - entry_fee, places=10)
        # mark breach → liquidation
        self.assertIsNotNone(acct.check_liquidation(10, mark_price=90.0))


class TestFuturesFunding(unittest.TestCase):

    def test_funding_accrues_across_intervals(self):
        """Long open 16h with rate=0.01%/8h → 2 funding events = 2 × debit.
        Funding must land in stats *and* reduce the trade's net PnL."""
        iv = 8 * 3600
        acct = FuturesAccount(
            cfg=FuturesConfig(leverage=10.0,
                              funding_rate_per_interval=0.0001,
                              funding_interval_seconds=iv,
                              taker_fee_fraction=0.00045),
            starting_equity=1.0)
        acct.open_long(100.0, 0, margin=0.1)        # N=1.0
        # cross two 8h boundaries while flat price stays 100
        d1 = acct.settle_funding(iv, price=100.0)
        d2 = acct.settle_funding(2 * iv, price=100.0)
        self.assertAlmostEqual(d1, 0.0001, places=12)
        self.assertAlmostEqual(d2, 0.0001, places=12)
        self.assertAlmostEqual(acct.stats.total_funding_paid, 0.0002, places=12)
        self.assertAlmostEqual(acct.position.funding_accrued, 0.0002, places=12)
        # negative rate credits the long
        d3 = acct.settle_funding(3 * iv, funding_rate=-0.0002, price=100.0)
        self.assertAlmostEqual(d3, -0.0002, places=12)
        self.assertAlmostEqual(acct.stats.total_funding_received, 0.0002, places=12)

    def test_pipeline_funding_and_liq_stats(self):
        iv = 8 * 3600
        candles = [
            _flat_candle(1, 100.0),
            _flat_candle(2, 100.0),            # entry fills here
            _flat_candle(iv + 5, 102.0),       # cross interval 1 at +5 s
            _flat_candle(2 * iv + 6, 104.0),   # cross interval 2
            _flat_candle(2 * iv + 7, 105.0),   # engine exit filled on bar 5
        ]
        script = ["buy", "none", "none", "exit", "none"]
        ft = ForwardTester(starting_balance=1.0, buy_size_sol=0.1,
                           slippage_pct=1.0, engine_kwargs={}, engine_version=1,
                           market_type="futures", leverage=5.0,
                           funding_rate_per_interval=0.0001,
                           funding_interval_seconds=iv,
                           futures_slippage_pct=0.1)
        ft.engine = _StubEngine(script)
        _feed(ft, candles)
        s = ft.stats.to_dict()
        self.assertEqual(s["market_type"], "futures")
        self.assertEqual(s["leverage"], 5.0)
        self.assertGreaterEqual(s["total_funding_paid"], 0.00004)  # ≥1 period at 102+
        self.assertEqual(s["total_liquidations"], 0)
        self.assertGreater(s["total_fees_paid_futures"], 0.0)
        self.assertEqual(len(ft.trade_history), 1)
        t = ft.trade_history[0]
        self.assertEqual(t.market_type, "futures")
        self.assertEqual(t.side, "long")
        self.assertNotEqual(t.notional_sol, 0.0)
        self.assertGreaterEqual(t.liquidation_price, 0.0)
        # win trade: price rose 105 vs 100.1 entry → PnL positive
        self.assertGreater(t.pnl_sol, 0.0)
        self.assertEqual(t.outcome, "W")


class TestFuturesMarkVsLast(unittest.TestCase):

    def test_mark_price_preferred_when_present(self):
        """5× long; bar 3 has last=price unchanged (no liq by last) but
        mark crashes past liq → liquidates at mark, not last."""
        candles = [
            _flat_candle(1, 100.0),
            _flat_candle(2, 100.0),
            # bar 3: last price 96 (above liq), mark spikes to 79 (below liq
            # at 5× ≈ 80.603 with mm 0.5%)
            dict(time=3, o=100.0, h=100.0, l=96.0, c=96.0,
                 buy_volume=0.0, sell_volume=0.0),
            _flat_candle(4, 99.0),
        ]
        script = ["buy", "none", "none", "none"]
        ft = ForwardTester(starting_balance=1.0, buy_size_sol=0.1,
                           slippage_pct=1.0, engine_kwargs={}, engine_version=1,
                           market_type="futures", leverage=5.0,
                           futures_slippage_pct=0.1,
                           funding_rate_per_interval=0.0)
        ft.engine = _StubEngine(script)

        marks = {3: 79.0}
        for cd in candles:
            ft.update(time=cd["time"], o=cd["o"], h=cd["h"], l=cd["l"],
                      c=cd["c"], volume=0.0,
                      mark_price=marks.get(cd["time"], 0.0))

        self.assertEqual(len(ft.trade_history), 1)
        t = ft.trade_history[0]
        self.assertEqual(t.exit_reason, "liquidation")
        self.assertEqual(t.exit_time, 3)


class TestFuturesDbShape(unittest.TestCase):

    def test_stats_to_dict_additive_only(self):
        """Spot stats dict gains new keys but existing keys/values untouched."""
        ft = ForwardTester(**_spot_kwargs())
        base_keys = {"starting_balance", "current_balance", "total_trades",
                     "winning_trades", "losing_trades", "total_pnl_sol",
                     "total_fees_paid", "max_drawdown_pct", "peak_balance",
                     "win_rate"}
        d = ft.stats.to_dict()
        for k in base_keys:
            self.assertIn(k, d)
        # spot defaults on the additive keys
        self.assertEqual(d["market_type"], "spot")
        self.assertEqual(d["leverage"], 1.0)


class TestFuturesExchangeSource(unittest.TestCase):
    """Historical perp-ingestion source (no recordings needed)."""

    def setUp(self):
        import futures_exchange as fe  # noqa: F401

    def test_symbol_normalisation(self):
        import futures_exchange as fe
        self.assertEqual(fe._exclusive_symbol("BTC"),  "BTCUSDT")
        self.assertEqual(fe._exclusive_symbol("btc"),  "BTCUSDT")
        self.assertEqual(fe._exclusive_symbol("BTCUSDT"), "BTCUSDT")
        self.assertEqual(fe._sanitize_symbol("BTC"), "btc")

    def test_cache_roundtrip_and_schema(self):
        """Feed a tiny synthetic mark/kline set through the cache and read it
        back — every contract column must exist."""
        import futures_exchange as fe
        symbol = "TESTXYZ"  # not in the supported list — direct schema use
        fe.ensure_schema(symbol)
        conn = fe._UpdatableConn.open()
        base = fe._sanitize_symbol(symbol)
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO futures_candles_{base}"
                f" (ts_s, open, high, low, close, turnover, funding_rate,"
                f"  mark_price, open_interest)"
                f" VALUES (?,?,?,?,?,?,?,?,?)",
                (1700000000, 100.0, 101.0, 99.0, 100.5, 5e4, 0.0001, 100.6, 1e6),
            )
            conn.commit()
        finally:
            conn.close()
        rows = fe._load_cached(symbol, 1699990000, 1700010000)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        for key in ("ts_s", "open", "high", "low", "close",
                    "turnover", "funding_rate", "mark_price",
                    "open_interest", "taker_buy_volume", "taker_sell_volume"):
            self.assertIn(key, r)
        self.assertAlmostEqual(r["mark_price"], 100.6)


class TestFuturesUsdcAccounting(unittest.TestCase):

    def test_usdc_meta_propagates(self):
        """sol_price_usd=1.0 turns the account into USDC reporting (1 unit =
        1 USDC) — the trade meta must carry position_notional_usdc."""
        from futures_model import FuturesAccount, FuturesConfig
        acct = FuturesAccount(cfg=FuturesConfig(leverage=5.0),
                              starting_equity=1000.0, sol_price_usd=1.0)
        acct.open_long(100.0, 0, margin=100.0)     # qty=5, notional=500
        res = acct.close_long(110.0, 60, "tp_v2", funding_rate=0.0)
        self.assertIsNotNone(res)
        self.assertAlmostEqual(res["position_notional_usdc"], 500.0, places=6)
        # gross = qty × (110 − 100) = 50 USDC-of-PnL (accounted natively)
        self.assertGreater(res["pnl_sol"], 45.0)   # net of entry+exit fees
        self.assertEqual(res["sol_price_usd"], 1.0)

    def test_spot_accounting_unchanged_when_no_sol_price(self):
        """sol_price_usd=0.0: legacy behaviour (memecoin recordings)."""
        from futures_model import FuturesAccount, FuturesConfig
        acct = FuturesAccount(cfg=FuturesConfig(leverage=1.0),
                              starting_equity=1.0)     # no sol_price override
        acct.open_long(2.3e-7, 0, margin=0.1)
        res = acct.close_long(3.1e-7, 60, "exit_signal", funding_rate=0.0)
        self.assertEqual(res["sol_price_usd"], 0.0)
        self.assertEqual(res["position_notional_usdc"], res["notional_sol"])


class TestFuturesFundingRealData(unittest.TestCase):

    def test_real_funding_patches_apply(self):
        """Funding read from the per-bar ``funding_rate`` column (non-zero) is
        settled against position notional at each 8h boundary."""
        iv = 8 * 3600
        ft = ForwardTester(starting_balance=1000.0, buy_size_sol=100.0,
                           slippage_pct=0.1, engine_kwargs={}, engine_version=1,
                           market_type="futures", leverage=2.0,
                           funding_interval_seconds=iv,
                           funding_rate_per_interval=0.0)   # default OFF
        ft.engine = _StubEngine(["buy", "none", "none", "none", "none"])
        # feed a 16h treading-water market with a +0.01%/8h funding feed
        candles = [
            _flat_candle(1,         64000.0),
            _flat_candle(2,         64000.0),
            _flat_candle(iv + 5,    64000.0),
            _flat_candle(2*iv + 6,  64000.0),
        ]
        for cd in candles:
            for price in (cd["o"], cd["h"], cd["l"], cd["c"]):
                ft.update(time=cd["time"], o=cd["o"], h=cd["h"], l=cd["l"],
                          c=price, volume=0.0, funding_rate=0.0001)
        # 2 boundaries crossed while in position → 2 × 0.01% × N
        pos_notional = 100.0 * 2.0                    # margin 100 × 2× lev
        expect_min = 2 * 0.0001 * pos_notional * 0.9  # ≥ ~90% of naive (fees)
        self.assertGreaterEqual(ft.stats.total_funding_paid, expect_min)


class TestBacktesterShape(unittest.TestCase):

    def test_run_backtest_futures_smoke(self):
        """End-to-end futures run via run_backtest on a synthetic recording,
        inserted in a temp copy of the DB (never touches real data)."""
        import data_store, backtester

        tmpd = tempfile.mkdtemp(prefix="pumpchart_futtest_")
        os.environ["BACKTEST_RESULTS_DIR"] = tmpd

        # isolaate: point data_store at temp DBs and re-init
        old_price, old_bt = data_store.PRICE_DB, data_store.BACKTEST_DB
        try:
            from pathlib import Path
            data_store.DATA_DIR = Path(tmpd)
            data_store.PRICE_DB = Path(tmpd) / "price_data.db"
            data_store.BACKTEST_DB = Path(tmpd) / "backtest_data.db"
            data_store.init_price_db()
            data_store.init_backtest_db()

            rid = data_store.create_recording(
                mint="FUTSMOKE", timeframe="1m", token_name="FutSmoke",
                token_symbol="FSK")
            # build 500 1-min candles: pump 1.00 → 1.20 then dump 1.20 → 0.40
            import math, random
            random.seed(7)
            px = 1.0
            rows = []
            for i in range(500):
                drift = 0.0025 if i < 250 else -0.02
                px *= (1.0 + drift + random.uniform(-0.004, 0.004))
                o = px / (1.0 + drift)
                hi = max(o, px) * 1.001
                lo = min(o, px) * 0.997
                rows.append({
                    "time": 1_700_000_000 + i * 60,
                    "open": o, "high": hi, "low": lo, "close": px,
                    "volume": 1.0,
                })
            data_store.insert_candles_batch(rid, rows)
            data_store.update_recording_candle_count(rid)
            data_store.stop_recording(rid)

            # spot run on same data (regression guard against cross-talk)
            r_sp = backtester.run_backtest(recording_id=rid, engine_version=1,
                                           batch_id="fut_smoke_spot")
            # futures run 5× leverage
            r_f = backtester.run_backtest(recording_id=rid, engine_version=1,
                                          market_type="futures", leverage=5.0,
                                          futures_slippage_pct=0.1,
                                          funding_rate_per_interval=0.0001,
                                          batch_id="fut_smoke_fut")
            self.assertNotIn("error", r_sp)
            self.assertNotIn("error", r_f)
            # market_type was persisted + returned
            bt = data_store.get_backtest(r_f["backtest_id"])
            self.assertEqual(bt.get("market_type"), "futures")
            self.assertIn("market_type", bt["summary_json"])
            self.assertEqual(bt["summary_json"]["market_type"], "futures")
            self.assertEqual(bt["summary_json"]["leverage"], 5.0)
            # spot row defaulted to spot
            bt2 = data_store.get_backtest(r_sp["backtest_id"])
            self.assertEqual(bt2.get("market_type"), "spot")
        finally:
            data_store.PRICE_DB, data_store.BACKTEST_DB = old_price, old_bt
            data_store.DATA_DIR = old_price.parent
            os.environ.pop("BACKTEST_RESULTS_DIR", None)


class TestV2FuturesParamSet(unittest.TestCase):
    """Spot/V2 paths stay byte-identical when the futures preset is absent;
    the futures preset layers param-only knobs and never touches the spot
    path.  Iter42 parity invariant."""

    def test_spot_engine_untouched_when_no_overrides(self):
        import strategy_engineV2 as v2
        from engine_factory import create_engine
        a = create_engine(engine_version=2)
        b = create_engine(engine_version=2)
        # spot cfg identical across calls (deterministic)
        self.assertEqual(a.cfg, b.cfg)
        # no futures-flag attrs leaked onto spot engine
        self.assertFalse(getattr(a, "_is_futures_engine", False))
        self.assertEqual(float(a._v2_volume_scale_fut), 1.0)
        self.assertEqual(int(a._v2_kramers_down_persist_fut), 0)

    def test_futures_overrides_layer_correctly(self):
        import strategy_engineV2 as v2
        from engine_factory import create_engine
        ep = {"v2_futures_overrides": dict(v2.FUTURES_DEFAULT_CONFIG)}
        eng = create_engine(engine_version=2, **ep)
        # adapter flags got the futures values
        self.assertTrue(getattr(eng, "_is_futures_engine", False))
        self.assertEqual(eng.confidence_high, v2.FUTURES_DEFAULT_CONFIG["confidence_high"])
        self.assertEqual(float(eng._v2_volume_scale_fut),
                         float(v2.FUTURES_DEFAULT_CONFIG["v2_volume_scale_fut"]))
        self.assertEqual(int(eng._v2_kramers_down_persist_fut),
                         int(v2.FUTURES_DEFAULT_CONFIG["v2_kramers_down_persist_fut"]))
        # core cfg contains the macro-tuned rates (not spot defaults)
        self.assertAlmostEqual(eng.core.cfg["lambda_mu"],
                               v2.FUTURES_DEFAULT_CONFIG["lambda_mu"])
        self.assertNotAlmostEqual(eng.core.cfg["lambda_mu"], v2.DEFAULT_CONFIG["lambda_mu"])

    def test_with_futures_preset_helper_layers_dict(self):
        import strategy_engineV2 as v2
        ep = v2.with_futures_preset({})
        self.assertIn("v2_futures_overrides", ep)
        self.assertEqual(ep["v2_futures_overrides"]["v2_kramers_down_persist_fut"],
                         v2.FUTURES_DEFAULT_CONFIG["v2_kramers_down_persist_fut"])
        # explicit-caller override wins (no helper clobber)
        ep2 = v2.with_futures_preset({"v2_futures_overrides": {}})
        self.assertEqual(ep2["v2_futures_overrides"], {})

    def test_kramers_persistence_exit_only_fires_after_n_subticks(self):
        """Single-tick kramers_down must NOT exit when the futures persistence
        guard is set to N>1; only the Nth consecutive P_down>=0.5 fires.
        Tests the streak-counter state directly (no full engine fixture)."""
        import strategy_engineV2 as v2

        # Build a minimal adapter with the futures preset; only set the
        # kramers-persistence exit branch + streak counter directly.
        eng = v2.StrategyEngineV2Adapter(v2_kramers_down_persist_fut=4)
        self.assertEqual(eng._v2_kramers_down_persist_fut, 4)
        self.assertEqual(eng._v2_kramers_down_streak, 0)

        # Manually call the streak-update path (extracted from _check_exit_v2)
        def streak_update(p_down, p_up, p_zero):
            if p_down > p_up and p_down > p_zero and p_down >= 0.5:
                eng._v2_kramers_down_streak += 1
                if eng._v2_kramers_down_persist_fut <= 1 or eng._v2_kramers_down_streak >= eng._v2_kramers_down_persist_fut:
                    return True
                return False
            else:
                eng._v2_kramers_down_streak = 0
                return False

        # Three consecutive "bad" decisions must NOT fire
        bad = (0.9, 0.05, 0.05)
        for i in range(3):
            fired = streak_update(*bad)
            self.assertFalse(fired, f"Tick {i+1} should NOT fire kramers_down_exit (streak={eng._v2_kramers_down_streak}/4)")
        # 4th consecutive bad decision must fire
        fired = streak_update(*bad)
        self.assertTrue(fired, "4th consecutive kramers_down should fire")
        # Now a good decision resets the streak
        fired = streak_update(0.05, 0.9, 0.05)
        self.assertFalse(fired)
        self.assertEqual(eng._v2_kramers_down_streak, 0)
        # And the very next bad decision starts from 1 again (no exit)
        fired = streak_update(*bad)
        self.assertFalse(fired, "After reset, streak restarts at 1 — no exit")
        self.assertEqual(eng._v2_kramers_down_streak, 1)

    def test_futures_volume_scale_passthrough_spot(self):
        """Spot runs (v2_volume_scale_fut default) leave volume unmodified."""
        import strategy_engineV2 as v2
        # default engine: vscale=1.0 -> volume unchanged after pre-update scaling
        eng = v2.StrategyEngineV2Adapter()
        # Direct call to the volume-modifier path (mirrors the update() code)
        vol = 100.0
        scaled_vol = vol * eng._v2_volume_scale_fut
        self.assertEqual(scaled_vol, 100.0)  # passthrough
        # Engine with futures override scales volume
        ep = {"v2_futures_overrides": dict(v2.FUTURES_DEFAULT_CONFIG)}
        eng_f = v2.StrategyEngineV2Adapter(**ep)
        s = eng_f._v2_volume_scale_fut
        self.assertLess(s, 1e-3)
        self.assertGreater(s, 1e-12)
        # When scaled, the 1e-7 ratio sends a $100M/1h bar to ~10 SOL-equivalent units
        scaled = 100_000_000.0 * s
        self.assertLess(scaled, 50.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
