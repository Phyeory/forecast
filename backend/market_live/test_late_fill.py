"""test_late_fill.py — the paper path must never book an unachievable open.

The live Alpaca broker refuses an `opg` order when the market is already
open (the targeted open has passed) and falls back to a day order at the
current price.  The paper path (used by the prop-firm forward sim) now
mirrors that rule: an order created while the market is open fills at the
current price (the bar's close), tagged `[late_fill]`, instead of at an open
that had already passed.

Run:  python -m pytest market_live/test_late_fill.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_live.trader as trader_mod
from market_live.brokers import PaperBroker
from market_live.trader import LiveConfig, MarketLiveTrader


def _cfg(tmp: Path) -> LiveConfig:
    return LiveConfig(market="stocks", interval="1d", symbols=("AAA",),
                      starting_equity=100_000.0, buy_size=0.1,
                      journal=False, broker="paper",
                      broker_kwargs={"starting_equity": 100_000.0,
                                     "slippage_pct": 0.0, "fee_bps": 0.0},
                      state_path=str(tmp / "state.json"),
                      guard_path=str(tmp / "guard.json"))


class _StubHost:
    """Minimal host: the fill path is what this test exercises."""

    def on_bar(self, candle):
        class _Decision:
            desired_position = 0
            fills: list = []

            def to_dict(self):
                return {"desired_position": 0, "fills": []}

        return _Decision()


def _bar(t: int, o: float, h: float, l: float, c: float) -> dict:
    return {"time": t, "open": o, "high": h, "low": l, "close": c,
            "volume": 1_000_000.0}


def test_late_order_fills_at_current_price_not_the_passed_open(tmp_path):
    """Market OPEN at signal time → the fill is the close, not the open."""
    cfg = _cfg(tmp_path)
    tr = MarketLiveTrader(cfg, feed=None, broker=PaperBroker(100_000.0, 0.0, 0.0),
                           journal=None, verbose=False)
    tr.hosts["AAA"] = _StubHost()
    tr.last_ts["AAA"] = 0
    tr.desired["AAA"] = 0
    tr.prices["AAA"] = 100.0

    # signal bar: the market is OPEN when the order is created
    tr.pending["AAA"] = {"desired": 1, "reason": "signal_test", "late": True}
    tr.step_bar("AAA", _bar(1_700_000_000, 100.0, 101.0, 99.0, 100.5))

    fills = tr.broker.fills
    assert len(fills) == 1, f"expected one fill, got {len(fills)}"
    fill = fills[0]
    # the bar's OPEN is 100.0; a late fill must be priced at the CLOSE 100.5
    assert abs(fill.price - 100.5) < 1e-9, f"late fill priced at {fill.price}"
    assert "[late_fill]" in (fill.reason or ""), fill.reason


def test_ontime_order_fills_at_the_next_open(tmp_path):
    """Market CLOSED at signal time → the fill is the next bar's open."""
    cfg = _cfg(tmp_path)
    tr = MarketLiveTrader(cfg, feed=None, broker=PaperBroker(100_000.0, 0.0, 0.0),
                           journal=None, verbose=False)
    tr.hosts["AAA"] = _StubHost()
    tr.last_ts["AAA"] = 0
    tr.desired["AAA"] = 0
    tr.prices["AAA"] = 100.0

    tr.pending["AAA"] = {"desired": 1, "reason": "signal_test", "late": False}
    tr.step_bar("AAA", _bar(1_700_000_000, 100.0, 101.0, 99.0, 100.5))

    fills = tr.broker.fills
    assert len(fills) == 1
    fill = fills[0]
    assert abs(fill.price - 100.0) < 1e-9, f"on-time fill priced at {fill.price}"
    assert "[late_fill]" not in (fill.reason or ""), fill.reason
