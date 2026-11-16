"""
new-feed source tests (2026-11-16).

Covers feed_sources pure functions (GT pool parsing, motion gates, DexScreener
enrichment, RPC mint-account / largest-accounts parsing, concentration math,
composite risk score, vetting gates incl. fail-closed policy), feed_store
persistence round-trip + tape-outcome math, and the AutoFeed gecko fetch
pipeline with a stubbed vetting client (no network).

Run:  cd backend && python -m pytest test_feed_sources.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import feed_sources as fs
import feed_store
from autofeed import AutoFeed, AutofeedConfig, Candidate


# ── GT pool parsing ──────────────────────────────────────────────────────────

def _gt_node(**over) -> dict:
    mint = "8Ma4i7fFDqukxpLeoTpPHfhVVuqHeZbfAPyUq7wbnkZn"
    pool = "Ew8AC5hZubTq8MYvdLBZv2MpSvRb9tyPLWW1gJZZk46B"
    node = {
        "id": f"solana_{pool}",
        "type": "pool",
        "relationships": {
            "base_token": {"data": {"id": f"solana_{mint}", "type": "token"}},
            "dex": {"data": {"id": "pumpswap", "type": "dex"}},
        },
        "attributes": {
            "name": "SNUT / SOL",
            "address": pool,
            "pool_created_at": "2026-11-16T15:34:29Z",
            "reserve_in_usd": "10488.92",
            "fdv_usd": None,
            "market_cap_usd": None,
            "base_token_price_usd": "0.0000012",
            "volume_usd": {"h1": "9322.09", "h24": "21000.5"},
            "transactions": {"h1": {"buys": 133, "sells": 62, "buyers": 110,
                                    "sellers": 47, "unique_traders": 120}},
            "price_change_percentage": {"h1": "31.4"},
        },
    }
    if over:
        node.update(over)
    return node


def test_parse_gecko_pool_full():
    p = fs.parse_gecko_pool(_gt_node())
    assert p is not None
    assert p.mint == "8Ma4i7fFDqukxpLeoTpPHfhVVuqHeZbfAPyUq7wbnkZn"
    assert p.dex == "pumpswap" and p.reserve_in_usd == 10488.92
    assert p.vol_h1 == 9322.09 and p.txns_h1_buyers == 110
    assert p.age_seconds > 0 and p.fdv_usd is None
    assert p.wash_proxy_usd > 0


def test_parse_gecko_pool_bad_nodes():
    assert fs.parse_gecko_pool({}) is None
    n = _gt_node()
    n["id"] = "eth_0xabc"
    assert fs.parse_gecko_pool(n) is None          # non-solana pool
    n = _gt_node()
    n["relationships"]["base_token"]["data"]["id"] = "solana_short"
    assert fs.parse_gecko_pool(n) is None          # unusable mint


def test_parse_iso_ts_formats():
    assert fs.parse_iso_ts("2026-11-16T15:34:29Z") > 0
    assert fs.parse_iso_ts("2026-11-16T15:34:29.123Z") > 0
    assert fs.parse_iso_ts("garbage") == 0.0
    assert fs.parse_iso_ts(None) == 0.0


# ── motion gates (pure) ──────────────────────────────────────────────────────

def _pool(**over) -> fs.GeckoPool:
    p = fs.parse_gecko_pool(_gt_node())
    assert p is not None
    for k, v in over.items():
        setattr(p, k, v)
    return p


BASE_GATES = dict(
    dex_allowlist={"pumpswap"},
    min_age_s=600.0, max_age_s=0.0,
    min_liquidity_usd=5000.0, min_volume_usd=25000.0,
    min_swaps=900, min_buyers_h1=10,
    max_volume_per_buyer_usd=0.0,
)


def test_motion_gate_defaults_pass_probe_pool():
    # The real probe pool: vol_h1 9322 < 25k floor → motion gate must reject it
    tally: dict = {}
    passing, rejected = fs.filter_gecko_motion([_pool()], reject_tally=tally, **BASE_GATES)
    assert not passing and len(rejected) == 1
    assert tally.get("min_volume_usd") == 1


def test_motion_gates_all_reasons():
    young = _pool(age_seconds=60.0)
    old = _pool(age_seconds=90_000.0)
    lowliq = _pool(reserve_in_usd=100.0)
    fewbuyers = _pool(txns_h1_buyers=3)
    wash = _pool(vol_h1=900_000.0, txns_h1_buyers=50)   # proxy 18k, buyers pass
    other_dex = _pool(dex="meteora-dbc")
    gates = dict(BASE_GATES, min_volume_usd=0.0, min_swaps=0)
    gates["max_volume_per_buyer_usd"] = 10_000.0
    gates["min_age_s"] = 600.0
    gates["max_age_s"] = 7200.0
    gates["min_liquidity_usd"] = 5000.0
    gates["min_buyers_h1"] = 10
    tallies: list[dict] = []
    for pool in (young, old, lowliq, fewbuyers, wash, other_dex):
        t: dict = {}
        passing, _ = fs.filter_gecko_motion([pool], reject_tally=t, **gates)
        assert not passing
        tallies.append(t)
    assert "min_pool_age" in tallies[0]
    assert "max_pool_age" in tallies[1]
    assert "min_liquidity_usd" in tallies[2]
    assert "min_buyers_h1" in tallies[3]
    assert "max_volume_per_buyer_usd" in tallies[4]
    assert "dex_not_allowed" in tallies[5]


def test_motion_gate_passes_healthy_pool():
    p = _pool(reserve_in_usd=20_000.0, vol_h1=40_000.0,
              txns_h1_buys=500, txns_h1_sells=400,
              txns_h1_buyers=300, txns_h1_sellers=250,
              created_at_unix=time.time() - 1800.0)
    gates = dict(BASE_GATES, min_volume_usd=25_000.0)
    passing, rejected = fs.filter_gecko_motion([p], **gates)
    assert len(passing) == 1 and not rejected


# ── DexScreener enrichment ───────────────────────────────────────────────────

def test_ds_enrich_picks_deepest_pair():
    pairs = [
        {"liquidity": {"usd": 100}, "marketCap": 10_000, "pairCreatedAt": int((time.time() - 60) * 1000)},
        {"liquidity": {"usd": 9_000}, "marketCap": 45_000, "pairCreatedAt": int((time.time() - 1200) * 1000)},
    ]
    e = fs.ds_enrich_mint(pairs)
    assert e["mcap_usd"] == 45_000 and e["liquidity_usd"] == 9_000.0
    assert e["pair_age_s"] is not None and e["pair_age_s"] >= 1200.0


def test_ds_enrich_empty_is_none_not_zero():
    assert fs.ds_enrich_mint(None)["mcap_usd"] is None
    assert fs.ds_enrich_mint([])["liquidity_usd"] is None


def test_ds_enrich_falls_back_to_fdv():
    e = fs.ds_enrich_mint([{"liquidity": {"usd": 5000}, "fdv": 33_000}])
    assert e["mcap_usd"] == 33_000


# ── RPC parse helpers + concentration ────────────────────────────────────────

def test_parse_mint_account():
    info = {"data": {"program": "spl-token", "parsed": {"info": {
        "mintAuthority": None, "freezeAuthority": None,
        "supply": "1000000000", "decimals": 6}, "type": "mint"}}}
    d = fs.parse_mint_account(info)
    assert d["mint_authority"] is None and d["freeze_authority"] is None
    assert d["supply"] == 1000.0


def test_parse_mint_account_non_renounced():
    info = {"data": {"parsed": {"info": {"mintAuthority": "SomeAuth",
                                         "freezeAuthority": "SomeFreeze",
                                         "supply": "500", "decimals": 0}}}}
    d = fs.parse_mint_account(info)
    assert d["mint_authority"] is not None and d["supply"] == 500.0


def test_concentration_shares():
    supply = 1_000.0
    amounts = [50.0] * 20         # top10 = 500/1000 = 0.5, top20 = 1.0
    t10, t20 = fs.concentration_shares(supply, amounts)
    assert t10 == 0.5 and t20 == 1.0
    amounts = [10.0] * 20         # top10 = 0.1
    t10, t20 = fs.concentration_shares(supply, amounts)
    assert t10 == 0.1 and t20 == 0.2
    assert fs.concentration_shares(0.0, amounts) == (None, None)
    assert fs.concentration_shares(supply, []) == (None, None)


def test_parse_largest_accounts_tolerant():
    vals = fs.parse_largest_accounts([{"uiAmount": 5.0}, {"uiAmount": None},
                                      {"uiAmount": "7"}, "junk"])
    assert vals == [5.0, 0.0, 7.0]


# ── composite risk score ─────────────────────────────────────────────────────

def _vet(**over) -> fs.VettingResult:
    v = fs.VettingResult(mint="x" * 40)
    for k, val in over.items():
        setattr(v, k, val)
    return v


def test_risk_score_clean_and_concentrated():
    clean = fs.composite_risk_score(_vet(top10_share=0.10, top20_share=0.15))
    assert clean == 0.0
    bundled = fs.composite_risk_score(_vet(top10_share=0.90, top20_share=0.95))
    assert bundled > 2.0
    assert bundled > clean


def test_risk_score_components():
    rugged = fs.composite_risk_score(_vet(top10_share=0.10, rugcheck_rugged=True))
    assert rugged >= 2.0
    insider = fs.composite_risk_score(_vet(top10_share=0.10, insider_graphs=True))
    assert insider >= 1.0
    scored = fs.composite_risk_score(_vet(top10_share=0.10, rugcheck_score=100.0))
    assert 1.4 < scored <= 1.5
    unknown = fs.composite_risk_score(_vet(top10_share=None))
    assert unknown == 0.0   # unknown vector must not fabricate risk


# ── vetting gates (fail-closed) ──────────────────────────────────────────────

def test_vetting_gates_fail_closed_on_rpc_unavailable():
    p = _pool()
    v = _vet(rpc_ok=False)
    reason = fs.apply_gecko_quality_gates(
        p, v, require_renounced_mint=True, require_renounced_freeze=True,
        max_top20_holder_rate=0.7, max_rugcheck_score=75.0,
        vetting_fail_policy="reject")
    assert reason == "vetting_rpc_unavailable"
    # explicit allow policy passes it through (escape hatch)
    reason = fs.apply_gecko_quality_gates(
        p, v, require_renounced_mint=True, require_renounced_freeze=True,
        max_top20_holder_rate=0.7, max_rugcheck_score=75.0,
        vetting_fail_policy="allow")
    assert reason is None


def test_vetting_gates_renounced_and_concentration():
    p = _pool()
    base = dict(require_renounced_mint=True, require_renounced_freeze=True,
                max_top20_holder_rate=0.7, max_rugcheck_score=75.0,
                vetting_fail_policy="reject")
    assert fs.apply_gecko_quality_gates(p, _vet(rpc_ok=True, renounced_mint=False,
                                                renounced_freeze=True), **base) == "not_renounced_mint_onchain"
    assert fs.apply_gecko_quality_gates(p, _vet(rpc_ok=True, renounced_mint=True,
                                                renounced_freeze=False), **base) == "not_renounced_freeze_onchain"
    assert fs.apply_gecko_quality_gates(p, _vet(rpc_ok=True, renounced_mint=True,
                                                renounced_freeze=True, top20_share=0.9), **base) == "max_top20_holder_rate"
    # unknown concentration under a configured ceiling ⇒ fail-closed
    assert fs.apply_gecko_quality_gates(p, _vet(rpc_ok=True, renounced_mint=True,
                                                renounced_freeze=True, top20_share=None), **base) == "vetting_concentration_unknown"
    assert fs.apply_gecko_quality_gates(p, _vet(rpc_ok=True, renounced_mint=True,
                                                renounced_freeze=True, top20_share=0.5,
                                                rugcheck_score=90.0), **base) == "max_rugcheck_score"
    assert fs.apply_gecko_quality_gates(p, _vet(rpc_ok=True, renounced_mint=True,
                                                renounced_freeze=True, top20_share=0.5,
                                                rugcheck_rugged=True), **base) == "rugcheck_rugged"
    assert fs.apply_gecko_quality_gates(p, _vet(rpc_ok=True, renounced_mint=True,
                                                renounced_freeze=True, top20_share=0.5,
                                                rugcheck_score=20.0), **base) is None


# ── RugCheck parse ───────────────────────────────────────────────────────────

def test_rugcheck_parse_shapes():
    d = fs.VettingClient._parse_rugcheck({"score_normalised": 33, "rugged": False,
                                          "risks": [{"name": "mint_authority"}, "free_risk"],
                                          "creator": {"tokens": 7},
                                          "graphInsidersDetected": True})
    assert d["score"] == 33.0 and d["creator_tokens"] == 7
    assert "mint_authority" in d["risks"] and "free_risk" in d["risks"]
    assert d["insider_graphs"] is True
    d2 = fs.VettingClient._parse_rugcheck(None)
    assert d2["score"] is None and not d2["rugged"]


# ── feed_store persistence + outcome math ────────────────────────────────────

def test_feed_store_roundtrip(tmp_path=None):
    # uses the real feed_data.db (telemetry db — safe to write)
    feed_store.record_event("TestMint" + "1" * 32, "gecko", "fed",
                            symbol="TST", market_cap=42_000.0, risk_score=0.7,
                            vector={"k": "v"})
    feed_store.record_event("TestMint" + "1" * 32, "gecko", "rejected",
                            reason="max_top20_holder_rate")
    s = feed_store.stats()
    assert s["events"] >= 2 and s["events_fed"] >= 1


def _candle(t, o, h, l, c, v=10.0) -> dict:
    return {"time": int(t), "open": o, "high": h, "low": l, "close": c, "volume": v}


def test_compute_tape_outcome_metrics():
    t0 = 1794840000
    candles = [
        _candle(t0 - 5, 100, 101, 99, 100),            # BEFORE event — must be ignored
        _candle(t0, 100, 102, 100, 101),
        _candle(t0 + 1, 101, 101, 60, 62),             # -38.6% dump in 1s
        _candle(t0 + 2, 62, 63, 55, 56),               # deeper low 55 → drawdown -45%
        _candle(t0 + 1800, 56, 60, 58, 59),            # forward return +? vs base 100 → -41%
    ]
    m = feed_store.compute_tape_outcome(candles, t0)
    assert m["candle_count_window"] == 4
    assert m["max_1s_dump_pct"] == round((62 / 101 - 1) * 100, 2)
    assert m["max_drawdown_pct_30m"] == round((55 / 100 - 1) * 100, 2)
    assert m["forward_return_pct_30m"] == round((59 / 100 - 1) * 100, 2)
    assert m["price_death"] is False


def test_compute_tape_outcome_price_death():
    t0 = 1794840000
    candles = [_candle(t0 + i, 50, 50, 50, 50, v=0.0) for i in range(80)]
    m = feed_store.compute_tape_outcome(candles, t0)
    assert m["price_death"] is True
    assert m["max_drawdown_pct_30m"] == 0.0


def test_compute_tape_outcome_empty_window():
    m = feed_store.compute_tape_outcome([], 1794840000)
    assert m["candle_count_window"] == 0 and m["max_1s_dump_pct"] is None


# ── AutoFeed gecko pipeline (stubbed vetting — no network) ───────────────────

def _gecko_feed(**cfg_over) -> AutoFeed:
    cfg = AutofeedConfig(feed_source="gecko")
    cfg.min_mcap_usd = 20_000.0
    cfg.max_mcap_usd = 10_000_000.0
    cfg.min_liquidity_usd = 5_000.0
    cfg.min_volume_usd = 25_000.0
    cfg.min_swaps = 900
    cfg.min_pool_age_minutes = 0.0        # tests drive age explicitly
    cfg.auto_tune_enabled = False         # static gates in tests
    for k, v in cfg_over.items():
        setattr(cfg, k, v)
    return AutoFeed(cfg)


def test_gecko_fetch_rejects_unknown_mcap_fail_closed(monkeypatch):
    feed = _gecko_feed()
    pool = _pool(reserve_in_usd=20_000.0, vol_h1=40_000.0,
                 txns_h1_buys=500, txns_h1_sells=400,
                 txns_h1_buyers=300, txns_h1_sellers=250)
    monkeypatch.setattr(feed._vetting, "poll_universe", async_return([pool]))
    monkeypatch.setattr("autofeed.get_ds_pairs_by_mints", async_return({pool.mint: []}))
    tally: dict = {}
    cands, raw = asyncio.run(feed._fetch_gecko_candidates(tally))
    assert cands == [] and raw == 1
    assert tally.get("mcap_unknown") == 1


def test_gecko_fetch_rejects_rpc_vetting_unavailable(monkeypatch):
    feed = _gecko_feed()
    pool = _pool(reserve_in_usd=20_000.0, vol_h1=40_000.0,
                 txns_h1_buys=500, txns_h1_sells=400,
                 txns_h1_buyers=300, txns_h1_sellers=250)

    class _BadVet:
        async def vet(self, mint):
            v = fs.VettingResult(mint=mint, rpc_ok=False)
            v.detail["rpc_error"] = "boom"
            return v

    monkeypatch.setattr(feed._vetting, "poll_universe", async_return([pool]))
    monkeypatch.setattr(feed._vetting, "vet", _BadVet().vet)
    monkeypatch.setattr("autofeed.get_ds_pairs_by_mints",
                        async_return({pool.mint: [{"liquidity": {"usd": 20_000},
                                                   "marketCap": 40_000}]}))
    tally: dict = {}
    cands, _ = asyncio.run(feed._fetch_gecko_candidates(tally))
    assert cands == []
    assert tally.get("vetting_rpc_unavailable") == 1


def test_gecko_fetch_happy_path_builds_candidate(monkeypatch):
    feed = _gecko_feed()
    pool = _pool(reserve_in_usd=20_000.0, vol_h1=40_000.0,
                 txns_h1_buys=500, txns_h1_sells=400,
                 txns_h1_buyers=300, txns_h1_sellers=250)

    class _GoodVet:
        async def vet(self, mint):
            v = fs.VettingResult(mint=mint, rpc_ok=True, renounced_mint=True,
                                 renounced_freeze=True, supply=1_000_000.0,
                                 top10_share=0.15, top20_share=0.22,
                                 rugcheck_score=10.0)
            v.detail["score"] = fs.composite_risk_score(v)
            return v

    monkeypatch.setattr(feed._vetting, "poll_universe", async_return([pool]))
    monkeypatch.setattr(feed._vetting, "vet", _GoodVet().vet)
    monkeypatch.setattr("autofeed.get_ds_pairs_by_mints",
                        async_return({pool.mint: [{"liquidity": {"usd": 20_000},
                                                   "marketCap": 40_000}]}))
    tally: dict = {}
    cands, _ = asyncio.run(feed._fetch_gecko_candidates(tally))
    assert len(cands) == 1
    c = cands[0]
    assert c.source == "gecko"
    assert c.market_cap == 40_000 and c.top20_pct == 0.22
    assert c.buyers_h1 == 300 and c.wash_proxy_usd > 0
    assert c.renounced_onchain is True and c.rugcheck_score == 10.0
    assert feed._seen.get(c.mint) is c


def test_gecko_fetch_skips_recently_seen(monkeypatch):
    feed = _gecko_feed()
    pool = _pool(reserve_in_usd=20_000.0, vol_h1=40_000.0,
                 txns_h1_buys=500, txns_h1_sells=400,
                 txns_h1_buyers=300, txns_h1_sellers=250)
    seen = Candidate(mint=pool.mint, source="gecko")
    feed._seen[pool.mint] = seen          # just seen (age ~0 < cooldown)
    monkeypatch.setattr(feed._vetting, "poll_universe", async_return([pool]))
    monkeypatch.setattr("autofeed.get_ds_pairs_by_mints", async_return({}))
    tally: dict = {}
    cands, _ = asyncio.run(feed._fetch_gecko_candidates(tally))
    assert cands == []
    assert tally.get("_dedup") == 1


def test_gecko_dex_allowlist_filters(monkeypatch):
    feed = _gecko_feed()
    pool = _pool(dex="meteora-dbc")
    monkeypatch.setattr(feed._vetting, "poll_universe", async_return([pool]))
    tally: dict = {}
    cands, _ = asyncio.run(feed._fetch_gecko_candidates(tally))
    assert cands == [] and tally.get("dex_not_allowed") == 1


def test_poll_universe_dedupes_trending_wins(monkeypatch):
    vc = fs.VettingClient()
    mint = "8Ma4i7fFDqukxpLeoTpPHfhVVuqHeZbfAPyUq7wbnkZn"
    t_pool = _pool(mint=mint, vol_h1=99_999.0)
    n_pool = _pool(mint=mint, vol_h1=1.0)
    fresh = _pool(mint="Fresh" + "3" * 34, vol_h1=5_000.0)

    async def fake_fetch(url, params, pages, label):
        if "trending" in url:
            return [t_pool]
        return [n_pool, fresh]

    monkeypatch.setattr(vc, "_fetch_gt", fake_fetch)
    uni = asyncio.run(vc.poll_universe(pages_new=1, pages_trending=1))
    mints = [p.mint for p in uni]
    assert mints.count(mint) == 1
    assert any(p.mint == "Fresh" + "3" * 34 for p in uni)
    got = next(p for p in uni if p.mint == mint)
    assert got.vol_h1 == 99_999.0   # trending entry wins


def test_gmgn_source_still_dispatches(monkeypatch):
    """feed_source=gmgn must use the CLI path, not gecko."""
    feed = _gecko_feed(feed_source="gmgn")
    called = {"gecko": 0, "gmgn": 0}

    async def fake_gecko(tally):
        called["gecko"] += 1
        return [], 0

    async def fake_gmgn():
        called["gmgn"] += 1
        return []

    monkeypatch.setattr(feed, "_fetch_gecko_candidates", fake_gecko)
    monkeypatch.setattr(feed, "_fetch_candidates", fake_gmgn)
    asyncio.run(feed._tick())
    assert called["gecko"] == 0 and called["gmgn"] == 1


def test_snapshot_gecko_fields():
    snap = _gecko_feed().snapshot()
    assert snap["feed_source"] == "gecko"
    assert snap["vetting_fail_policy"] == "reject"
    assert "feed_store" in snap and "events" in snap["feed_store"]
    assert "max_top20_holder_rate" in snap and "max_rugcheck_score" in snap


def test_forward_candidates_persists_decisions():
    feed = _gecko_feed()
    fed = []

    async def fwd(cand: Candidate):
        fed.append(cand)

    cand = Candidate(mint="FwdTest" + "2" * 32, source="gecko",
                     market_cap=50_000.0, risk_score=0.4)
    feed._forward_fn = fwd
    asyncio.run(feed._forward_candidates([cand]))
    assert len(fed) == 1
    s = feed_store.stats()
    assert s["events_fed"] >= 1


async def _run_forward(feed, cands, fwd):
    feed._forward_fn = fwd
    await feed._forward_candidates(cands)


# ── helper ───────────────────────────────────────────────────────────────────

def async_return(value):
    async def _fn(*args, **kwargs):
        return value
    return _fn
