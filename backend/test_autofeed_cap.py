"""
Autofeed capacity / accounting regression tests (2026-11-27).

The bug: `_autofeed_active_count()` includes `_autofeed_pending_spawns`, and
the autofeed increments that counter BEFORE the spawn task runs — so a spawn
fed at the cap was refused by its OWN reservation.  Live evidence: 26
candidates fed vs 7 recordings; the refused mints were kept in `_seen` and
never retried.  Also pins that a forward returning False is NOT counted as
fed and is released for a later tick.

Run:  cd backend && python -m pytest test_autofeed_cap.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import autofeed
from autofeed import AutoFeed, AutofeedConfig
from main import _spawn_slot_available


MINT = "Mint" + "x" * 32


def _row(**over) -> dict:
    base = {
        "address": MINT,
        "name": "Test Token", "symbol": "TST",
        "market_cap": 200_000.0, "liquidity": 6_000.0, "holder_count": 60,
        "smart_degen_count": 1, "swaps": 1_000, "volume": 30_000.0,
        "exchange": "pump_amm", "top_10_holder_rate": 0.2, "rug_ratio": 0.05,
        "bundler_rate": 0.05, "insider_rate": 0.1, "rat_trader_amount_rate": 0.1,
        "entrapment_ratio": 0.1, "bot_degen_rate": 0.2,
        "is_wash_trading": False, "is_honeypot": False,
        "renounced_mint": True, "renounced_freeze_account": True,
        "creator": "Creator" + "c" * 30,
        "creation_timestamp": int(time.time()) - 600,
        "open_timestamp": int(time.time()) - 600,
    }
    base.update(over)
    return base


# ── the cap arithmetic (pure) ───────────────────────────────────────────────

def test_reserved_spawn_is_not_double_counted():
    # 5 running + THIS spawn already counted as pending → it may start.
    assert _spawn_slot_available(6, 5, 1, already_reserved=True) is True
    # A non-reserved caller (browser) sees 5+1 and is refused.
    assert _spawn_slot_available(6, 5, 1, already_reserved=False) is False


def test_cap_allows_start_below_limit_and_unlimited():
    assert _spawn_slot_available(20, 19, 1, already_reserved=True) is True
    assert _spawn_slot_available(20, 20, 1, already_reserved=True) is False
    assert _spawn_slot_available(0, 999, 0) is True          # 0 = unlimited


# ── fed accounting: a refused forward must not count as fed ─────────────────

def _make_feed(monkeypatch, **over) -> AutoFeed:
    cfg = AutofeedConfig()
    cfg.organic_screen_enabled = False       # no GMGN CLI in unit tests
    for k, v in over.items():
        setattr(cfg, k, v)
    monkeypatch.setattr(autofeed.feed_store, "record_event",
                        lambda *a, **k: None)
    return AutoFeed(cfg)


def test_forward_false_is_counted_as_not_started_and_released(monkeypatch):
    feed = _make_feed(monkeypatch)

    async def forward(cand):
        # first candidate refused by the spawn queue, second accepted
        return cand.symbol != "REFUSED"

    feed._forward_fn = forward
    rows = [
        _row(address="Mint" + "a" * 31, symbol="REFUSED"),
        _row(address="Mint" + "b" * 31, symbol="OK"),
    ]
    asyncio.run(feed._process_rows(rows))
    assert feed.total_fed == 1
    assert feed.total_fed_not_started == 1
    assert feed._fed_not_started_tally.get("not_started") == 1
    # the refused mint must be retryable next tick
    assert "Mint" + "a" * 31 not in feed._seen
    assert "Mint" + "b" * 31 in feed._seen


def test_per_tick_budget_caps_feeds(monkeypatch):
    feed = _make_feed(monkeypatch, max_feeds_per_tick=1)
    fed: list[str] = []

    async def forward(cand):
        fed.append(cand.mint)
        return True

    feed._forward_fn = forward
    # `_to_candidate` validates address length only; unique 35-char mints.
    rows = [_row(address=("Mint" + str(i) + "x" * 40)[:44], symbol=f"T{i}")
            for i in range(5)]
    asyncio.run(feed._process_rows(rows))
    assert len(fed) == 1 and feed.total_fed == 1


def test_screen_reject_is_not_forwarded_or_counted(monkeypatch):
    feed = _make_feed(monkeypatch)
    feed.config.organic_screen_enabled = True
    feed.config.organic_screen_max_fetches_per_tick = 2
    fed: list[str] = []

    async def forward(cand):
        fed.append(cand.mint)
        return True

    class V:
        verdict = "reject"
        risk_score = 88.0
        reasons = ["top1_9.0pct"]
        metrics = {"top1_pct": 0.09}
        source = "gmgn"

    async def fake_screen(cand, creator=""):
        return V()

    feed.screen_candidate = fake_screen
    feed._forward_fn = forward
    asyncio.run(feed._process_rows([_row(address="Mint" + "q" * 31, symbol="BAD")]))
    assert fed == []
    assert feed.total_fed == 0
    assert feed.total_rejected_screen == 1
    # rejected mints stay in `_seen` (not retried every tick)
    assert "Mint" + "q" * 31 in feed._seen


def test_unscreened_candidate_survives_trending_rotation(monkeypatch):
    """A candidate beyond the per-tick screen budget must stay queued and be
    screened on a later tick even if it drops off the trending list."""
    feed = _make_feed(monkeypatch, organic_screen_enabled=True,
                      organic_screen_max_fetches_per_tick=1)
    calls: list[str] = []

    class V:
        verdict = "pass"
        risk_score = 10.0
        reasons: list = []
        metrics: dict = {}
        source = "gmgn"

    async def fake_screen(cand, creator=""):
        calls.append(cand.symbol)
        return V()

    forwarded: list[str] = []

    async def forward(c):
        forwarded.append(c.symbol)
        return True

    feed.screen_candidate = fake_screen
    feed._forward_fn = forward
    a = _row(address=("MintA" + "x" * 40)[:44], symbol="A")
    b = _row(address=("MintB" + "x" * 40)[:44], symbol="B")
    asyncio.run(feed._process_rows([a, b]))
    assert calls == ["A"] and forwarded == ["A"]
    assert any(c.symbol == "B" for c in feed._pending_screen.values())

    # Next poll: the trending list no longer contains B.  It must still be
    # screened from the pending queue (cooldown reset to isolate the test).
    feed._last_fed_at = 0.0
    asyncio.run(feed._process_rows([]))
    assert calls == ["A", "B"] and forwarded == ["A", "B"]
    assert feed._pending_screen == {}
