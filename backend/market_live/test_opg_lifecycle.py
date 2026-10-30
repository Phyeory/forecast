"""test_opg_lifecycle.py — a queueing broker's pending order must survive
restarts and reconcile against the venue, never book a phantom fill.

Production chain that broke the Oct-25..30 arm14 paper campaign (zero fills
in six sessions while the journal showed a fill):

  1. `_save_state` dropped `submitted`/`order_id` from the pending record →
     every daily restart downgraded a working opg order into a bare intent.
  2. The resubmit branch booked the submission stroke as a `next_open_fill`
     and consumed the pending — the engine believed ADBE was open while the
     broker stayed flat.  `desired=1` with no pending is a permanent stall:
     the transition guard only acts on CHANGES.
  3. Alpaca paper never fills `opg` orders and cancels them ~9:45 ET, so an
     intent that only ever resubmits pre-open never converges.

Run:  python -m pytest market_live/test_opg_lifecycle.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_live.trader as trader_mod
from market_live.brokers import Broker, BrokerFill
from market_live.trader import LiveConfig, MarketLiveTrader


class _StubHost:
    def __init__(self, desired: int = 0):
        self._desired = desired

    def on_bar(self, candle):
        d = self._desired

        class _Decision:
            desired_position = d
            fills: list = []

            def to_dict(self):
                return {"desired_position": d, "fills": []}

        return _Decision()


class _QueueBroker(Broker):
    """Stands in for AlpacaBroker: submissions queue, fills only via status."""

    name = "queue"
    submits_immediately = True
    order_status_supported = True

    def __init__(self):
        self.submissions: list[dict] = []
        self.cancels: list[str] = []
        self._status: dict[str, dict] = {}
        self._open: list[dict] = []
        self._n = 0

    def queue_status(self, order_id: str, status: dict) -> None:
        self._status[order_id] = status

    def open_orders(self, symbol: str) -> list[dict]:
        return [o for o in self._open
                if self._status.get(o["id"], {}).get("status") == "open"]

    def equity(self, prices=None) -> float:
        return 100_000.0

    def position_qty(self, symbol: str) -> float:
        return 0.0

    def target_position(self, symbol, target_qty, ref_price,
                        reason="") -> BrokerFill:
        self._n += 1
        oid = f"ord-{self._n}"
        self.submissions.append({"symbol": symbol, "qty": target_qty,
                                 "order_id": oid, "reason": reason})
        self.queue_status(oid, {"status": "open", "filled_qty": 0.0,
                                "avg_price": 0.0})
        return BrokerFill(symbol=symbol, side="buy", qty=abs(target_qty),
                          price=ref_price, notional=abs(target_qty) * ref_price,
                          fee=0.0, time=0.0, order_id=oid, reason=reason,
                          filled=False)

    def order_status(self, order_id: str) -> dict:
        return self._status.get(order_id)

    def cancel_order(self, order_id: str) -> bool:
        self.cancels.append(order_id)
        return True

    def state(self) -> dict:
        return {}


def _trader(tmp_path, broker) -> MarketLiveTrader:
    cfg = LiveConfig(market="stocks", interval="1d", symbols=("AAA",),
                     starting_equity=100_000.0, buy_size=0.1,
                     journal=False, broker="queue",
                     state_path=str(tmp_path / "state.json"))
    tr = MarketLiveTrader(cfg, feed=None, broker=broker,
                          journal=None, verbose=False)
    tr.hosts["AAA"] = _StubHost(desired=0)
    tr.last_ts["AAA"] = 0
    tr.prices["AAA"] = 100.0
    tr.ready = True
    return tr


def _bar(t: int, o: float, c: float) -> dict:
    return {"time": t, "open": o, "high": max(o, c), "low": min(o, c),
            "close": c, "volume": 1_000_000.0}


def test_state_roundtrip_keeps_order_identity(tmp_path):
    """Restart must not downgrade a working order into a bare intent."""
    tr = _trader(tmp_path, _QueueBroker())
    tr.pending["AAA"] = {"desired": 1, "reason": "signal_1", "submitted": True,
                         "order_id": "ord-7", "target_qty": 29.0}
    tr._save_state()
    tr2 = _trader(tmp_path, _QueueBroker())
    state = tr2._load_state()
    p = (state.get("pending") or {}).get("AAA") or {}
    assert p.get("submitted") is True
    assert p.get("order_id") == "ord-7"
    assert p.get("target_qty") == 29.0


def test_resubmit_after_cancel_books_no_phantom_fill(tmp_path):
    """Canceled opg → resubmit keeps the pending alive; journal must not
    show a fill while the broker holds nothing."""
    br = _QueueBroker()
    tr = _trader(tmp_path, br)
    tr.pending["AAA"] = {"desired": 1, "reason": "signal_1", "submitted": True,
                         "order_id": "ord-dead", "target_qty": 29.0}
    br.queue_status("ord-dead", {"status": "canceled", "filled_qty": 0.0,
                                 "avg_price": 0.0})
    tr.hosts["AAA"] = _StubHost(desired=1)   # engine still wants long
    rec = tr.step_bar("AAA", _bar(1_700_000_000, 100.0, 101.0))
    assert not rec["fills"], "a submission must never be booked as a fill"
    assert len(br.submissions) == 1, "exactly one resubmission"
    p = tr.pending.get("AAA") or {}
    assert p.get("submitted") is True
    assert p.get("order_id") == br.submissions[0]["order_id"]
    assert tr.desired["AAA"] == 1
    assert br.equity() == 100_000.0, "broker cash must not move"


def test_fill_reconciles_from_order_status(tmp_path):
    """The real fill (fq>0, venue avg price) books on a later bar."""
    br = _QueueBroker()
    tr = _trader(tmp_path, br)
    tr.pending["AAA"] = {"desired": 1, "reason": "signal_1", "submitted": True,
                         "order_id": "ord-9", "target_qty": 29.0}
    br.queue_status("ord-9", {"status": "filled", "filled_qty": 29.0,
                              "avg_price": 244.25})
    tr.hosts["AAA"] = _StubHost(desired=1)   # engine still wants long
    rec = tr.step_bar("AAA", _bar(1_700_000_000, 244.25, 248.0))
    assert len(rec["fills"]) == 1
    f = rec["fills"][0]
    assert abs(f["price"] - 244.25) < 1e-9, "fill price is the venue print"
    assert "AAA" not in tr.pending, "resolved intent is consumed"


def test_stale_opg_canceled_when_market_open(tmp_path, monkeypatch):
    """An unfilled opg while the market trades is a relic — cancel it and
    resubmit at the market (day order) instead of deferring forever."""
    monkeypatch.setattr(trader_mod, "market_is_open", lambda: True)
    br = _QueueBroker()
    tr = _trader(tmp_path, br)
    tr.pending["AAA"] = {"desired": 1, "reason": "signal_1", "submitted": True,
                         "order_id": "ord-stale", "target_qty": 29.0}
    br.queue_status("ord-stale", {"status": "open", "filled_qty": 0.0,
                                  "avg_price": 0.0})
    tr.hosts["AAA"] = _StubHost(desired=1)
    tr.step_bar("AAA", _bar(1_700_000_000, 100.0, 101.0))
    assert br.cancels == ["ord-stale"]
    assert len(br.submissions) == 1, "resubmitted once at market"
    p = tr.pending.get("AAA") or {}
    assert p.get("submitted") is True and p.get("order_id") == "ord-1"


def test_open_opg_awaiting_auction_keeps_deferring(tmp_path, monkeypatch):
    """Pre-open run, order working, auction not reached yet → no cancels,
    no resubmits, intent intact."""
    monkeypatch.setattr(trader_mod, "market_is_open", lambda: False)
    br = _QueueBroker()
    tr = _trader(tmp_path, br)
    tr.pending["AAA"] = {"desired": 1, "reason": "signal_1", "submitted": True,
                         "order_id": "ord-live", "target_qty": 29.0}
    br.queue_status("ord-live", {"status": "open", "filled_qty": 0.0,
                                 "avg_price": 0.0})
    tr.hosts["AAA"] = _StubHost(desired=1)
    tr.step_bar("AAA", _bar(1_700_000_000, 100.0, 101.0))
    assert br.cancels == []
    assert br.submissions == []
    p = tr.pending.get("AAA") or {}
    assert p.get("order_id") == "ord-live"


def test_prepare_adopts_orphaned_open_order(tmp_path):
    """A working broker order with a lost pending record is adopted, not
    duplicated."""
    br = _QueueBroker()
    br.submissions.append({"symbol": "AAA", "qty": 29.0,
                           "order_id": "ord-orphan", "reason": "signal_1"})
    br.queue_status("ord-orphan", {"status": "open", "filled_qty": 0.0,
                                   "avg_price": 0.0})
    br._open.append({"id": "ord-orphan", "qty": "29.000000"})
    tr = _trader(tmp_path, br)
    tr.desired["AAA"] = 1
    tr._adopt_orphaned_entries()
    p = tr.pending.get("AAA") or {}
    assert p.get("submitted") is True and p.get("order_id") == "ord-orphan"
    assert p.get("target_qty") == 29.0


def test_flip_cancels_the_working_order(tmp_path):
    """Engine flips 1→0 while the entry order is still working → cancel it,
    arm the exit instead."""
    br = _QueueBroker()
    tr = _trader(tmp_path, br)

    class _FlipHost:
        def on_bar(self, candle):
            class _D:
                desired_position = 0
                fills: list = []

                def to_dict(self):
                    return {"desired_position": 0, "fills": []}

            return _D()

    tr.hosts["AAA"] = _FlipHost()
    tr.desired["AAA"] = 1
    tr.pending["AAA"] = {"desired": 1, "reason": "signal_1", "submitted": True,
                         "order_id": "ord-entry", "target_qty": 29.0}
    rec = tr.step_bar("AAA", _bar(1_700_000_000, 100.0, 101.0))
    assert br.cancels == ["ord-entry"], "stale entry order must be canceled"
    p = tr.pending.get("AAA") or {}
    assert p.get("desired") == 0, "exit intent armed"
    assert not rec["fills"]
