"""
AutoFeed market-condition auto-tune unit tests (2026-09-05).

Covers the pure tuning math (_compute_tuned_gates), the effective-gates
fallback ladder (disabled → user gates; stale → user gates), gate application
in _build_cli_args + _to_candidate, snapshot exposure, and the tune_now hook.
No network calls — conditions are injected directly.
Run:  cd backend && python -m pytest test_autofeed_tune.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import autofeed
from autofeed import AutoFeed, AutofeedConfig, Candidate


def make_feed(**over) -> AutoFeed:
    cfg = AutofeedConfig()
    cfg.min_mcap_usd = 20_000.0
    cfg.min_liquidity_usd = 5_000.0
    cfg.min_volume_usd = 25_000.0
    cfg.min_swaps = 900
    for k, v in over.items():
        setattr(cfg, k, v)
    return AutoFeed(cfg)


# ── _compute_tuned_gates ────────────────────────────────────────────────────

def test_compute_solid_conditions():
    cf = AutofeedConfig()
    cf.min_volume_usd = 25_000.0
    cf.min_swaps = 900
    cond = {"sol_usd": 168.0, "sol_chg_24h_pct": 0.0, "venue_ratio": 1.0}
    g = AutoFeed._compute_tuned_gates(cond, cf, now=time.time())
    # venue neutral, momentum flat → motion scale 1.0 → user gates unchanged
    assert g["min_volume_usd"] == 25_000.0
    assert g["min_swaps"] == 900
    # SOL=$168 → grad mcap 410.7×168 = $68,997 → ×0.5 discount = $34.5k > $20k floor
    assert g["min_mcap_usd"] == max(20_000.0, 410.7 * 168.0 * 0.5)


def test_compute_low_sol_raises_mcap_floor():
    cf = AutofeedConfig()          # min_mcap_usd default 20k
    cond = {"sol_usd": 103.0, "sol_chg_24h_pct": 0.0, "venue_ratio": 1.0}
    g = AutoFeed._compute_tuned_gates(cond, cf, now=time.time())
    assert g["min_mcap_usd"] == 410.7 * 103.0 * 0.5          # ≈ $21.2k
    # And at high SOL the floor rises with it
    cond["sol_usd"] = 240.0
    g2 = AutoFeed._compute_tuned_gates(cond, cf, now=time.time())
    assert g2["min_mcap_usd"] == 410.7 * 240.0 * 0.5          # ≈ $49.3k
    # User's own stricter floor still wins
    cf.min_mcap_usd = 60_000.0
    g3 = AutoFeed._compute_tuned_gates(cond, cf, now=time.time())
    assert g3["min_mcap_usd"] == 60_000.0


def test_compute_cold_venue_loosens_motion():
    cf = AutofeedConfig()
    cf.min_volume_usd = 50_000.0
    cf.min_swaps = 1_000
    cf.min_liquidity_usd = 20_000.0
    cond = {"sol_usd": 168.0, "sol_chg_24h_pct": -20.0, "venue_ratio": 0.3}
    g = AutoFeed._compute_tuned_gates(cond, cf, now=time.time())
    # ratio 0.3 × momentum_adj(1 + 0.3×clamp(-2)=0.7) = 0.21 → clamped to 0.5
    assert g["min_volume_usd"] == 25_000.0                    # 50k × 0.5
    assert g["min_swaps"] == 500
    assert g["min_liquidity_usd"] == 10_000.0


def test_compute_hot_venue_tightens_motion_but_caps():
    cf = AutofeedConfig()
    cf.min_volume_usd = 10_000.0
    cf.min_swaps = 100
    cf.min_liquidity_usd = 3_000.0
    cond = {"sol_usd": 168.0, "sol_chg_24h_pct": 40.0, "venue_ratio": 4.0}
    g = AutoFeed._compute_tuned_gates(cond, cf, now=time.time())
    # ratio 4 × momentum_adj(1.3) = 5.2 → clamped to 1.5
    assert g["min_volume_usd"] == 15_000.0                   # 10k × 1.5
    assert g["min_swaps"] == 150
    assert g["min_liquidity_usd"] == 4_500.0


def test_compute_absolute_floors_protect_tiny_user_gates():
    cf = AutofeedConfig()
    cf.min_volume_usd = 100.0
    cf.min_swaps = 3
    cf.min_liquidity_usd = 10.0
    cond = {"sol_usd": 168.0, "sol_chg_24h_pct": 0.0, "venue_ratio": 0.1}
    g = AutoFeed._compute_tuned_gates(cond, cf, now=time.time())
    # scale clamps to 0.5 but floors stop the gates collapsing to dust
    assert g["min_volume_usd"] == autofeed.AUTO_TUNE_VOL_FLOOR_USD
    assert g["min_swaps"] == autofeed.AUTO_TUNE_SWAPS_FLOOR
    assert g["min_liquidity_usd"] == autofeed.AUTO_TUNE_LIQ_FLOOR_USD


def test_compute_partial_conditions():
    cf = AutofeedConfig()
    # venue_ratio present, sol_usd missing → scale clamped to 1.5; user mcap kept
    g = AutoFeed._compute_tuned_gates({"venue_ratio": 2.0}, cf, now=time.time())
    assert g["min_volume_usd"] == cf.min_volume_usd * 1.5
    assert g["min_swaps"] == int(cf.min_swaps * 1.5)
    assert g["min_mcap_usd"] == cf.min_mcap_usd
    # sol_usd present, venue_ratio missing → mcap re-anchored, motion untouched
    g2 = AutoFeed._compute_tuned_gates({"sol_usd": 200.0}, cf, now=time.time())
    assert g2["min_mcap_usd"] == 410.7 * 200.0 * 0.5
    assert g2["min_volume_usd"] == cf.min_volume_usd
    # completely empty dict → all user gates
    g3 = AutoFeed._compute_tuned_gates({}, cf, now=time.time())
    assert g3 == {"min_mcap_usd": cf.min_mcap_usd, "min_volume_usd": cf.min_volume_usd,
                  "min_swaps": cf.min_swaps, "min_liquidity_usd": cf.min_liquidity_usd}


# ── effective_gates fallback ladder ──────────────────────────────────────────

def test_effective_gates_disabled_returns_user_gates():
    feed = make_feed(auto_tune_enabled=False)
    feed._market_conditions = {"sol_usd": 300.0, "fetched_at": time.time()}
    g = feed.effective_gates()
    assert g["min_mcap_usd"] == 20_000.0 and g["min_volume_usd"] == 25_000.0


def test_effective_gates_stale_returns_user_gates():
    feed = make_feed()
    feed._market_conditions = {
        "sol_usd": 300.0, "venue_ratio": 3.0,
        "fetched_at": time.time() - (autofeed.AUTO_TUNE_MAX_CONDITION_AGE + 60),
    }
    g = feed.effective_gates()
    assert g["min_mcap_usd"] == 20_000.0 and g["min_swaps"] == 900


def test_effective_gates_fresh_applies_tune():
    feed = make_feed()
    feed._market_conditions = {
        "sol_usd": 240.0, "sol_chg_24h_pct": 5.0, "venue_ratio": 1.5,
        "fetched_at": time.time(),
    }
    g = feed.effective_gates()
    assert g["min_mcap_usd"] == 410.7 * 240.0 * 0.5
    # scale = clamp(1.5 × (1 + 0.3×clamp(5/10)) , 0.5, 1.5) = 1.5
    assert g["min_swaps"] == 1350
    assert g["min_volume_usd"] == 25_000.0 * 1.5


def test_effective_gates_no_conditions_returns_user_gates():
    feed = make_feed()
    assert feed._market_conditions is None
    g = feed.effective_gates()
    assert g["min_mcap_usd"] == 20_000.0 and g["min_swaps"] == 900


# ── CLI args + local filter use the same effective gates ────────────────────

def test_build_cli_args_uses_effective_gates():
    feed = make_feed()
    feed._market_conditions = {
        "sol_usd": 240.0, "sol_chg_24h_pct": 0.0, "venue_ratio": 1.0,
        "fetched_at": time.time(),
    }
    args = feed._build_cli_args()
    joined = " ".join(args)
    assert "--min-marketcap" in args
    i = args.index("--min-marketcap")
    assert args[i + 1] == str(int(410.7 * 240.0 * 0.5))
    j = args.index("--min-volume")
    assert args[j + 1] == "25000"
    # user-owned levers pass through untouched
    k = args.index("--max-marketcap")
    assert args[k + 1] == str(int(AutofeedConfig().max_mcap_usd))
    h = args.index("--min-holder-count")
    assert args[h + 1] == str(int(AutofeedConfig().min_holders))


def test_build_cli_args_disabled_matches_user_gates():
    feed = make_feed(auto_tune_enabled=False)
    feed._market_conditions = {"sol_usd": 240.0, "fetched_at": time.time()}
    args = feed._build_cli_args()
    i = args.index("--min-marketcap")
    assert args[i + 1] == "20000"
    j = args.index("--min-swaps")
    assert args[j + 1] == "900"


def _row(**over):
    base = {
        "address": "Mint" + "x" * 32,
        "market_cap": 30_000.0, "liquidity": 6_000.0, "holder_count": 60,
        "smart_degen_count": 1, "swaps": 1_000, "volume": 30_000.0,
        "exchange": "pump_amm", "top_10_holder_rate": 0.3, "rug_ratio": 0.1,
        "bundler_rate": 0.1, "insider_rate": 0.1, "rat_trader_amount_rate": 0.1,
        "entrapment_ratio": 0.1, "bot_degen_rate": 0.2,
        "renounced_mint": True, "renounced_freeze_account": True,
    }
    base.update(over)
    return base


def test_to_candidate_applies_tuned_mcap_floor():
    feed = make_feed()
    # SOL=$300 → tuned floor ≈ $61.6k; a $40k token passes user floor but fails tuned
    feed._market_conditions = {"sol_usd": 300.0, "fetched_at": time.time()}
    assert feed._to_candidate(_row(market_cap=40_000.0)) is None
    cand = feed._to_candidate(_row(market_cap=70_000.0))
    assert isinstance(cand, Candidate)
    # Same feed, auto-tune off → $40k passes again (escape hatch)
    feed.config.auto_tune_enabled = False
    assert feed._to_candidate(_row(market_cap=40_000.0)) is not None


def test_to_candidate_applies_tuned_motion_gates():
    feed = make_feed()
    # SOL=$103, hot venue ratio 3.0 → motion scale clamped to 1.5 → vol gate 37.5k
    feed._market_conditions = {
        "sol_usd": 103.0, "sol_chg_24h_pct": 0.0, "venue_ratio": 3.0,
        "fetched_at": time.time(),
    }
    assert feed._to_candidate(_row(volume=30_000.0, swaps=1_000)) is None
    assert feed._to_candidate(_row(volume=40_000.0, swaps=1_400, liquidity=8_000.0)) is not None


def test_to_candidate_quality_gates_untouched_by_tune():
    feed = make_feed()
    feed._market_conditions = {
        "sol_usd": 240.0, "sol_chg_24h_pct": 0.0, "venue_ratio": 3.0,
        "fetched_at": time.time(),
    }
    # a rug-y token is rejected no matter how hot the venue is
    assert feed._to_candidate(_row(rug_ratio=0.6)) is None
    assert feed._to_candidate(_row(bundler_rate=0.9)) is None
    assert feed._to_candidate(_row(renounced_mint=False)) is None
    assert feed._to_candidate(_row(is_wash_trading=True)) is None


# ── DeFiLlama breakdown parser quirks ───────────────────────────────────────

def test_llama_parser_ignores_settled_echo():
    # REAL-WORLD regression (2026-09-05): the newest row was a byte-identical
    # echo of the previous SETTLED day ($310.67M on both 09-04 and 09-05,
    # frozen across 3 polls / 29+ min). The echo must NOT be projected (that
    # would fake a 4x hot day) and the pair counts once in the baseline.
    days = [1788307200 + i * 86400 for i in range(10)]       # 08-27 … 09-05
    vols = [1460.5e6, 576.3e6, 584.8e6, 732.1e6, 939.2e6,
            827.4e6, 1021.1e6, 838.7e6, 310.7e6, 310.7e6]   # echo pair
    now = days[-1] + 13 * 3600 + 21 * 60                    # 13:21 UTC 09-05
    out = AutoFeed._parse_llama_breakdown(list(zip(days, vols)), now)
    assert out is not None
    assert out["venue_vol_usd"] == 310.7e6                   # NOT projected
    # baseline = 08-27…09-03 median (echo counted once, deduped)
    assert abs(out["venue_ratio"] - 310.7e6 / 833.05e6) < 0.005


def test_llama_parser_projects_fresh_partial_day():
    # A genuinely FRESH partial day (value ≠ predecessor) IS filling intraday
    # → projected to a full-day estimate.
    days = [1788307200 + i * 86400 for i in range(10)]
    vols = [1460.5e6, 576.3e6, 584.8e6, 732.1e6, 939.2e6,
            827.4e6, 1021.1e6, 838.7e6, 700.0e6, 180.0e6]   # fresh partial
    now = days[-1] + 10 * 3600                              # 10:00 UTC 09-05
    out = AutoFeed._parse_llama_breakdown(list(zip(days, vols)), now)
    assert out is not None
    exp = 180.0e6 * (24 * 3600) / (10 * 3600)               # 180M → 432M
    assert abs(out["venue_vol_usd"] - exp) < 1e6
    assert abs(out["venue_ratio"] - exp / 833.05e6) < 0.01


def test_llama_parser_full_settled_day_no_projection():
    # Latest row 25h old → past projection window (<24h), before settle (≥26h):
    # used as-is and excluded from its own baseline.
    days = [1788307200 + i * 86400 for i in range(9)]       # 08-27 … 09-04
    vols = [1460.5e6, 576.3e6, 584.8e6, 732.1e6, 939.2e6,
            827.4e6, 1021.1e6, 838.7e6, 700.0e6]
    now = days[-1] + 25 * 3600
    out = AutoFeed._parse_llama_breakdown(list(zip(days, vols)), now)
    assert out["venue_vol_usd"] == 700.0e6                  # no projection
    assert abs(out["venue_ratio"] - 700.0e6 / 833.05e6) < 0.005


def test_llama_parser_midnight_clamp():
    # 30 min into the UTC day → elapsed clamped to ≥6h (avoid overprojection).
    days = [1788307200 + i * 86400 for i in range(10)]
    vols = [1460.5e6, 576.3e6, 584.8e6, 732.1e6, 939.2e6,
            827.4e6, 1021.1e6, 838.7e6, 310.7e6, 50.0e6]    # fresh partial
    now = days[-1] + 30 * 60
    out = AutoFeed._parse_llama_breakdown(list(zip(days, vols)), now)
    # 50M raw / (0.5h/24h) would be 2400M; clamped to 50M×4=200M
    assert out["venue_vol_usd"] == 50.0e6 * 4.0


def test_llama_parser_empty_and_garbage():
    assert AutoFeed._parse_llama_breakdown([], time.time()) is None
    assert AutoFeed._parse_llama_breakdown([(1, 0.0), (2, 0.0)], time.time()) is None
    # only a fresh in-progress row → no settled baseline exists → None
    now = time.time()
    assert AutoFeed._parse_llama_breakdown([(now - 3 * 3600, 100.0)], now) is None
    # single settled row → ratio is exactly 1.0 (neutral, safe default)
    ts = now - 30 * 3600
    out = AutoFeed._parse_llama_breakdown([(ts, 100.0)], now)
    assert out is not None and out["venue_ratio"] == 1.0


# ── tune_now / refresh / snapshot ───────────────────────────────────────────

def test_refresh_market_conditions_disabled_is_noop(monkeypatch):
    feed = make_feed(auto_tune_enabled=False)
    async def boom():
        raise AssertionError("must not fetch when disabled")
    monkeypatch.setattr(feed, "_fetch_market_conditions", boom)
    assert asyncio.run(feed.refresh_market_conditions()) is False


def test_refresh_populates_state_and_history(monkeypatch):
    feed = make_feed()
    cond = {"sol_usd": 150.0, "sol_chg_24h_pct": 2.0, "venue_ratio": 1.2,
            "fetched_at": time.time()}
    async def fake_fetch():
        return dict(cond)
    monkeypatch.setattr(feed, "_fetch_market_conditions", fake_fetch)
    assert asyncio.run(feed.refresh_market_conditions()) is True
    assert feed._market_conditions["sol_usd"] == 150.0
    assert feed._last_tune_at > 0
    assert len(feed._tune_history) == 1
    snap = feed.snapshot()
    assert snap["auto_tune_enabled"] is True
    assert snap["market_conditions"]["sol_usd"] == 150.0
    assert snap["effective_gates"]["min_mcap_usd"] == 410.7 * 150.0 * 0.5
    assert len(snap["tune_history"]) == 1
    # tune_now returns the refresh payload
    out = asyncio.run(feed.tune_now())
    assert out["refreshed"] is True
    assert out["effective_gates"]["min_mcap_usd"] == 410.7 * 150.0 * 0.5
    assert len(feed._tune_history) == 2


def test_refresh_failure_keeps_previous_conditions(monkeypatch):
    feed = make_feed()
    feed._market_conditions = {"sol_usd": 100.0, "fetched_at": time.time()}
    async def none_fetch():
        return None
    monkeypatch.setattr(feed, "_fetch_market_conditions", none_fetch)
    assert asyncio.run(feed.refresh_market_conditions()) is False
    assert feed._market_conditions["sol_usd"] == 100.0


def test_snapshot_disabled_shape():
    feed = make_feed(auto_tune_enabled=False)
    snap = feed.snapshot()
    assert snap["auto_tune_enabled"] is False
    assert snap["effective_gates"] == {
        "min_mcap_usd": 20_000.0, "min_volume_usd": 25_000.0,
        "min_swaps": 900, "min_liquidity_usd": 5_000.0,
    }
    assert snap["market_conditions"] is None


# ── Liveness + dead-mint quality gates (2026-09-15) ────────────────────────
# Sep 13-15: 51% of live sessions died no-motion, top mints re-fed 4-6× —
# GMGN trailing-1h stats pass while nothing trades NOW.

def _loose_feed(**over):
    # Gates wide open except motion presence, so tests isolate the new logic.
    cfg = AutofeedConfig()
    cfg.min_mcap_usd = 0.0
    cfg.min_liquidity_usd = 0.0
    cfg.min_holders = 0
    cfg.min_smart_degen_count = 0
    cfg.min_volume_usd = 0.0
    cfg.min_swaps = 0
    cfg.max_top10_holder_rate = 1.0
    cfg.max_rug_ratio = 1.0
    cfg.max_bundler_rate = 1.0
    cfg.max_insider_rate = 1.0
    cfg.max_rat_trader_rate = 1.0
    cfg.max_entrapment_ratio = 1.0
    cfg.max_bot_degen_rate = 1.0
    cfg.require_renounced_mint = False
    cfg.require_renounced_freeze = False
    cfg.reject_wash_trading = False
    cfg.reject_honeypot = False
    cfg.require_migration_exchange = False
    cfg.auto_tune_enabled = False
    for k, v in over.items():
        setattr(cfg, k, v)
    return AutoFeed(cfg)


def test_motion_presence_rejects_zero_stats():
    feed = _loose_feed()
    assert feed._to_candidate(_row(volume=0.0, swaps=100)) is None
    assert feed._to_candidate(_row(volume=100.0, swaps=0)) is None
    assert feed._to_candidate(_row(volume=100.0, swaps=100)) is not None
    # escape hatch: disabled → zero-stat rows pass again
    feed.config.require_motion_presence = False
    assert feed._to_candidate(_row(volume=0.0, swaps=0)) is not None


def test_liveness_first_sighting_parks_unless_roaring():
    feed = _loose_feed()
    now = time.time()
    # lukewarm debut parks (baseline recorded for next poll)
    assert feed._passes_liveness("Mint" + "a" * 32, 1_000, 30_000.0, 900, 25_000.0, now) is False
    # roaring debut (≥2× gates on both) forwards immediately
    assert feed._passes_liveness("Mint" + "b" * 32, 1_800, 50_000.0, 900, 25_000.0, now) is True
    # only one leg hot → still parks
    assert feed._passes_liveness("Mint" + "c" * 32, 5_000, 30_000.0, 900, 25_000.0, now) is False


def test_liveness_growth_forwards_flat_drops():
    feed = _loose_feed()
    now = time.time()
    m = "Mint" + "d" * 32
    assert feed._passes_liveness(m, 1_000, 30_000.0, 900, 25_000.0, now) is False
    # +30 swaps a poll later → alive
    assert feed._passes_liveness(m, 1_030, 30_100.0, 900, 25_000.0, now + 60.0) is True
    # flat numbers, lukewarm → dead now
    assert feed._passes_liveness(m, 1_030, 30_100.0, 900, 25_000.0, now + 120.0) is False
    # volume jump alone also proves life
    assert feed._passes_liveness(m, 1_030, 32_000.0, 900, 25_000.0, now + 180.0) is True


def test_liveness_sticky_hot_passes():
    feed = _loose_feed()
    now = time.time()
    m = "Mint" + "e" * 32
    assert feed._passes_liveness(m, 5_000, 200_000.0, 900, 25_000.0, now) is True
    # identical (cached) numbers next poll, still roaring → pass
    assert feed._passes_liveness(m, 5_000, 200_000.0, 900, 25_000.0, now + 60.0) is True


def test_dead_mint_blocks_until_expiry():
    feed = _loose_feed()
    now = time.time()
    row = _row(volume=100.0, swaps=100)
    mint = row["address"]
    assert feed._to_candidate(dict(row)) is not None
    feed.note_dead_mint(mint, now=now)
    assert feed._to_candidate(dict(row)) is None
    # expired cooldown → admitted again
    feed._dead_mints[mint] = now - (feed.config.dead_mint_cooldown_hours * 3600.0 + 1.0)
    assert feed._to_candidate(dict(row)) is not None
    assert mint not in feed._dead_mints  # lazy eviction
    snap = feed.snapshot()
    assert snap["dead_mint_count"] == 0


def test_dead_mint_note_empty_is_noop():
    feed = _loose_feed()
    feed.note_dead_mint("")
    assert feed.snapshot()["dead_mint_count"] == 0


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
