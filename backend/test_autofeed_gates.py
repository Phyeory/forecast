"""
AutoFeed fail-closed gate repair tests (2026-11-16).

The old `_to_candidate` parsed risk fields with tolerant coercion and gated
with `if 0 < value > limit` — a missing/renamed GMGN field coerced to 0.0 and
PASSED silently.  Empirically confirmed regression: `insider_rate` was retired
from trending responses (absent 5/5 probed rows) and its gate died unnoticed.
The repair makes the filter FAIL-CLOSED: a row missing/unparseable for any
required field is rejected with a tallyable `shape_missing` reason, never
passed; present-and-zero still passes (genuinely clean).  These tests pin
that contract.

Run:  cd backend && python -m pytest test_autofeed_gates.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from autofeed import AutoFeed, AutofeedConfig, Candidate


def make_feed(**over) -> AutoFeed:
    cfg = AutofeedConfig()
    cfg.min_mcap_usd = 20_000.0
    cfg.min_liquidity_usd = 5_000.0
    cfg.min_volume_usd = 25_000.0
    cfg.min_swaps = 900
    # Tick tests only exercise shape/dedup telemetry; the holder screen is
    # covered separately (test_organic_screen.py) with a fake CLI runner.
    cfg.organic_screen_enabled = False
    for k, v in over.items():
        setattr(cfg, k, v)
    return AutoFeed(cfg)


MINT = "Mint" + "x" * 32


def _row(**over) -> dict:
    """A verifiably-organic GMGN trending row (matches the 2026-11-16 probe shape)."""
    base = {
        "address": MINT,
        "name": "Test Token", "symbol": "TST",
        "market_cap": 30_000.0, "liquidity": 6_000.0, "holder_count": 60,
        "smart_degen_count": 1, "swaps": 1_000, "volume": 30_000.0,
        "exchange": "pump_amm", "top_10_holder_rate": 0.3, "rug_ratio": 0.1,
        "bundler_rate": 0.1, "insider_rate": 0.1, "rat_trader_amount_rate": 0.1,
        "entrapment_ratio": 0.1, "bot_degen_rate": 0.2,
        "is_wash_trading": False, "is_honeypot": False,
        "renounced_mint": True, "renounced_freeze_account": True,
        # Recently migrated (the lifecycle band rejects hours-old chases).
        "creation_timestamp": int(time.time()) - 600,
        "open_timestamp": int(time.time()) - 600,
        "twitter_username": "https://x.com/test",
    }
    base.update(over)
    return base


def _tally(feed: AutoFeed, row: dict) -> tuple[object, dict]:
    tally: dict = {}
    cand = feed._to_candidate(row, tally)
    return cand, tally


# ── baseline: a clean row passes and carries the supplementary vector ───────

def test_clean_row_passes():
    feed = make_feed()
    cand, tally = _tally(feed, _row())
    assert isinstance(cand, Candidate)
    assert tally == {}
    assert cand.mint == MINT


def test_clean_row_with_absent_supplementary_fields_still_passes():
    feed = make_feed()
    row = _row()
    for k in ("sniper_count", "dev_team_hold_rate", "cto_flag", "buy_tax",
              "sell_tax", "history_highest_market_cap", "twitter_create_token_count"):
        row.pop(k, None)
    cand, tally = _tally(feed, row)
    assert isinstance(cand, Candidate)
    assert "shape_missing" not in tally   # supplementary fields are optional
    assert cand.sniper_count == 0 and cand.dev_team_hold_rate == 0.0


def test_genuinely_zero_risk_values_pass():
    """Present-and-0 is CLEAN, not missing — must not reject."""
    feed = make_feed()
    cand, tally = _tally(feed, _row(bundler_rate=0, rug_ratio=0,
                                    rat_trader_amount_rate=0, entrapment_ratio=0.0,
                                    bot_degen_rate=0.0, top_10_holder_rate=0.0))
    assert isinstance(cand, Candidate)
    assert tally == {}


# ── fail-closed: every required field missing ⇒ reject ──────────────────────

def test_each_required_field_missing_rejects():
    feed = make_feed()
    for field in AutoFeed.REQUIRED_FIELDS:
        row = _row()
        del row[field]
        cand, tally = _tally(feed, row)
        assert cand is None, f"missing required field {field!r} must reject"
        assert tally.get("shape_missing") == 1, f"field {field!r}: {tally}"
        assert tally.get(f"_field_missing:{field}") == 1


def test_unparseable_required_numeric_field_rejects():
    feed = make_feed()
    cand, tally = _tally(feed, _row(market_cap="not-a-number"))
    assert cand is None
    assert tally.get("shape_missing:market_cap") == 1


def test_unparseable_required_boolean_field_rejects():
    feed = make_feed()
    cand, tally = _tally(feed, _row(is_honeypot="maybe"))
    assert cand is None
    assert tally.get("shape_missing:is_honeypot") == 1


def test_null_required_field_rejects():
    feed = make_feed()
    cand, tally = _tally(feed, _row(is_honeypot=None))
    assert cand is None
    assert tally.get("shape_missing") == 1


def test_indicator_fields_are_telemetry_only():
    """2026-11-28: GMGN's manipulation indicators no longer gate — a missing
    or absurd value must not reject a market-verifiable row."""
    feed = make_feed()
    cand, tally = _tally(feed, _row(bundler_rate=None, rug_ratio=None,
                                    top_10_holder_rate=None,
                                    is_wash_trading=None))
    assert isinstance(cand, Candidate)
    assert tally == {}


def test_bad_mint_rejects():
    feed = make_feed()
    cand, _ = _tally(feed, _row(address="short"))
    assert cand is None


# ── retired field: insider_rate optional, gate applies only when present ────

def test_insider_rate_absent_still_passes():
    """insider_rate was retired upstream (absent 5/5 rows 2026-11-16)."""
    feed = make_feed()
    cand, tally = _tally(feed, _row(insider_rate=None))
    assert isinstance(cand, Candidate)
    assert "shape_missing" not in tally
    assert cand.insider_rate == 0.0


def test_insider_rate_present_but_high_rejects():
    feed = make_feed(max_insider_rate=0.50)
    cand, tally = _tally(feed, _row(insider_rate=0.7))
    assert cand is None
    assert tally.get("max_insider_rate") == 1


# ── classic risk gates still fire on present values ─────────────────────────

def test_indicator_gates_off_by_default_but_configurable():
    """The gates are retired; when explicitly configured they still bind."""
    feed = make_feed()
    assert feed._to_candidate(_row(bundler_rate=0.9)) is not None
    feed = make_feed(max_bundler_rate=0.5, max_rug_ratio=0.3,
                     max_top10_holder_rate=0.4)
    assert feed._to_candidate(_row(bundler_rate=0.51)) is None
    assert feed._to_candidate(_row(top_10_holder_rate=0.45)) is None
    assert feed._to_candidate(_row(rug_ratio=0.31)) is None
    assert feed._to_candidate(_row(bundler_rate=0.49)) is not None


def test_boolean_gates_fire():
    feed = make_feed()
    # wash-trading is GMGN's own classification — gate only when configured
    assert feed._to_candidate(_row(is_wash_trading=True)) is not None
    assert make_feed(reject_wash_trading=True)._to_candidate(
        _row(is_wash_trading=True)) is None
    assert feed._to_candidate(_row(is_honeypot=True)) is None
    assert feed._to_candidate(_row(renounced_mint=False)) is None
    assert feed._to_candidate(_row(renounced_freeze_account=False)) is None
    # GMGN ints 0/1 parse as bools
    assert feed._to_candidate(_row(renounced_mint=1)) is not None


def test_motion_gates_fire_on_present_values():
    """Zero liquidity/volume is no longer skipped (`> 0` guard removed)."""
    feed = make_feed()
    assert feed._to_candidate(_row(liquidity=0.0)) is None
    assert feed._to_candidate(_row(volume=0.0)) is None
    assert feed._to_candidate(_row(swaps=0)) is None
    assert feed._to_candidate(_row(holder_count=0)) is None
    assert feed._to_candidate(_row(liquidity=4_999.0)) is None
    assert feed._to_candidate(_row(liquidity=5_001.0)) is not None


# ── optional extended gates: default OFF, enforced only when set ────────────

def test_dev_and_sniper_supply_gates_explicit_only():
    """2026-11-28: dev/sniper gates are off by default — the on-chain screen
    computes dev hold from the creator wallet; explicit config still binds."""
    feed = make_feed()
    assert feed._to_candidate(_row(dev_team_hold_rate=0.05)) is not None
    feed = make_feed(max_dev_team_hold_rate=0.03, max_top70_sniper_hold_rate=0.15)
    assert feed._to_candidate(_row(dev_team_hold_rate=0.05)) is None
    assert feed._to_candidate(_row(top70_sniper_hold_rate=0.20)) is None
    assert feed._to_candidate(_row(dev_team_hold_rate=0.02,
                                   top70_sniper_hold_rate=0.10)) is not None


def test_remaining_extended_gates_default_off():
    feed = make_feed()
    row = _row(twitter_create_token_count=50, cto_flag=True,
               buy_tax=0.25, sell_tax=0.25)
    cand, tally = _tally(feed, row)
    assert isinstance(cand, Candidate)
    assert tally == {}
    assert cand.cto_flag is True
    assert cand.dup_count == 0


def test_extended_gates_enforce_when_configured():
    feed = make_feed(max_dev_team_hold_rate=0.5, max_top70_sniper_hold_rate=0.5,
                     max_twitter_create_token_count=10, reject_cto_flag=True,
                     max_buy_sell_tax_pct=0.10)
    assert feed._to_candidate(_row(dev_team_hold_rate=0.6)) is None
    assert feed._to_candidate(_row(top70_sniper_hold_rate=0.6)) is None
    assert feed._to_candidate(_row(twitter_create_token_count=11)) is None
    assert feed._to_candidate(_row(cto_flag=True)) is None
    assert feed._to_candidate(_row(buy_tax=0.25)) is None
    assert feed._to_candidate(_row(sell_tax=0.05)) is not None


def test_dup_count_accumulates():
    feed = make_feed()
    cand = feed._to_candidate(_row(image_dup=True, telegram_dup=1, website_dup=False))
    assert cand is not None and cand.dup_count == 2


# ── tick-level shape telemetry ───────────────────────────────────────────────

def test_tick_records_shape_report_on_drift():
    feed = make_feed()
    rows = [_row(address="Mint" + "y" * 30 + str(i)) for i in range(4)]
    # 2 of 4 rows drift: drop a required field
    del rows[2]["swaps"]
    del rows[3]["holder_count"]
    asyncio.run(feed._process_rows(rows))
    rep = feed._shape_report
    assert rep["raw_rows"] == 4
    assert rep["rows_missing_required"] == 2
    assert set(rep["field_missing_counts"]) == {"swaps", "holder_count"}
    assert rep["rows_rejected"] == 2
    assert feed._shape_totals["raw_rows"] == 4
    assert feed._shape_totals["rows_missing_required"] == 2
    assert feed._reject_tally_total.get("shape_missing") == 2


def test_tick_shape_report_clean_tick():
    feed = make_feed()
    rows = [_row(address="Mint" + "z" * 30 + str(i)) for i in range(3)]
    asyncio.run(feed._process_rows(rows))
    rep = feed._shape_report
    assert rep["rows_missing_required"] == 0
    assert rep["field_missing_counts"] == {}
    assert rep["rows_rejected"] == 0


def test_snapshot_exposes_shape_fields():
    feed = make_feed()
    snap = feed.snapshot()
    assert "last_shape_report" in snap
    assert "shape_totals" in snap
    assert "reject_tally_total" in snap
    assert "max_dev_team_hold_rate" in snap


# ── lifecycle band + pump-chasing guard (2026-11-27 quality fix) ────────────

def test_age_band_rejects_too_fresh_and_bounds_old_when_configured():
    feed = make_feed()
    assert feed._to_candidate(_row(open_timestamp=int(time.time()) - 60)) is None
    assert feed._to_candidate(_row(open_timestamp=int(time.time()) - 600)) is not None
    # 0 = no upper cap (the default); an explicit cap still binds.
    assert feed._to_candidate(_row(open_timestamp=int(time.time()) - 86400)) is not None
    feed.config.max_migrated_age_minutes = 60
    assert feed._to_candidate(_row(open_timestamp=int(time.time()) - 86400)) is None


def test_already_pumped_token_rejects():
    feed = make_feed()
    assert feed._to_candidate(_row(price_change_percent1h=400.0)) is None
    assert feed._to_candidate(_row(price_change_percent1h=200.0)) is not None
