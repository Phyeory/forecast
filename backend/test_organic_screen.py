"""
Organic holder-forensics screen tests (Setuh blueprint Day 2: Bundles).

Every check is a pure function of the holder rows, so these tests pin the
thresholds and the fail behaviour without touching GMGN or the chain.

Run:  cd backend && python -m pytest test_organic_screen.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import feed_store
import organic_screen as oscreen
from organic_screen import (OrganicScreen, ScreenConfig, evaluate_holders,
                            normalise_holders)


def _h(owner, pct, *, sol=2.0, funder="", funded_at=0.0, start=1000.0,
       is_new=False, tags=None, creator=False):
    """Canonical holder row (what normalise_holders emits)."""
    return {
        "owner": owner, "pct": pct, "sol": sol, "is_new": is_new,
        "tags": list(tags or []), "funder": funder, "funded_at": funded_at,
        "start_holding_at": start, "buy_count": 1,
        "is_creator": creator,
    }


def _clean(n=10):
    """Diverse percentages / balances / funders / entry times → clean tape."""
    pcts = [0.030, 0.025, 0.020, 0.018, 0.015, 0.012, 0.010, 0.009, 0.008, 0.006]
    out = []
    for i, p in enumerate(pcts[:n]):
        out.append(_h(f"W{i}", p, sol=1.0 + i * 0.37,
                      funder=f"F{i}", funded_at=1_000_000 * (i + 1),
                      start=1000.0 + i * 900))
    return out


# ── normalisation: pool rows never count as holders ─────────────────────────

def test_normalise_excludes_pool_and_vault_rows():
    rows = [
        # LP / pool vault: GMGN tags the DEX, or addr_type == 2
        {"address": "POOL", "amount_percentage": 0.98, "exchange": "pump_amm"},
        {"address": "VAULT", "amount_percentage": 0.01, "addr_type": 2},
        {"address": "WALLET", "amount_percentage": 0.03, "native_balance": "2000000000",
         "native_transfer": {"from_address": "F1", "timestamp": 123},
         "start_holding_at": 1000, "is_new": False, "tags": []},
    ]
    hs = normalise_holders(rows)
    assert [h["owner"] for h in hs] == ["WALLET"]
    assert abs(hs[0]["sol"] - 2.0) < 1e-9
    assert hs[0]["funder"] == "F1"


def test_normalise_accepts_percent_or_fraction():
    hs = normalise_holders([{"address": "W", "amount_percentage": 3.0}])
    assert abs(hs[0]["pct"] - 0.03) < 1e-9


def test_normalise_empty_is_unavailable():
    verdict, reasons, metrics, _ = evaluate_holders([], creator="")
    assert verdict == "unavailable" and reasons == ["no_holders"]


# ── Setuh check 1: holder distribution ─────────────────────────────────────

def test_clean_holder_table_passes():
    verdict, reasons, metrics, score = evaluate_holders(_clean(), creator="")
    assert verdict == "pass", reasons
    assert score < 70


def test_top1_above_four_percent_rejects():
    hs = _clean() + [_h("WHALE", 0.08, sol=9.9)]
    verdict, reasons, metrics, _ = evaluate_holders(hs)
    assert verdict == "reject"
    assert any(r.startswith("top1_") for r in reasons)


def test_top10_concentration_rejects():
    hs = [_h(f"W{i}", 0.034, sol=1 + i, funder=f"F{i}", start=1000 + i * 900)
          for i in range(10)]
    verdict, reasons, _, _ = evaluate_holders(hs)
    assert verdict == "reject"
    assert any(r.startswith("top10_") for r in reasons)


def test_identical_holdings_reject():
    # Setuh: 2.5 / 2.5 / 2.5 / 2.5 is the classic bundle fingerprint.
    hs = [_h(f"W{i}", 0.025, sol=1 + i, funder=f"F{i}", start=1000 + i * 900)
          for i in range(4)]
    verdict, reasons, metrics, _ = evaluate_holders(hs)
    assert verdict == "reject"
    assert any(r.startswith("identical_holdings_x") for r in reasons)
    assert metrics["identical_cluster"] >= 4


# ── Setuh check 2: funding source ──────────────────────────────────────────

def test_shared_funder_cluster_rejects():
    hs = [
        _h("A", 0.015, funder="SAME"),
        _h("B", 0.014, funder="SAME"),
        _h("C", 0.016, funder="SAME"),
        _h("D", 0.010, funder="OTHER", start=5000),
    ]
    verdict, reasons, metrics, _ = evaluate_holders(hs)
    assert verdict == "reject"
    assert any(r.startswith("shared_funder_x3") for r in reasons)
    assert metrics["funder_cluster_max"] == 3


def test_funding_burst_rejects():
    now = 1_800_000_000
    hs = [_h(f"W{i}", 0.005 + i * 0.001, funder=f"F{i}",
             funded_at=now + i * 30, start=1000 + i * 900)
          for i in range(4)]
    verdict, reasons, metrics, _ = evaluate_holders(hs)
    assert verdict == "reject"
    assert any(r.startswith("funding_burst_x") for r in reasons)


# ── Setuh check 3: identical SOL balances ──────────────────────────────────

def test_similar_sol_balances_reject():
    hs = [
        _h("A", 0.012, sol=1.000),
        _h("B", 0.014, sol=1.001),
        _h("C", 0.016, sol=0.999),
        _h("D", 0.020, sol=4.20),
    ]
    verdict, reasons, metrics, _ = evaluate_holders(hs)
    assert verdict == "reject"
    assert any(r.startswith("similar_sol_balances_x") for r in reasons)


# ── Setuh check 4 / launch bundle ──────────────────────────────────────────

def test_launch_window_cobuy_rejects():
    # 4 wallets bought inside the first 30 s holding 8.8% total.
    hs = [
        _h("A", 0.020, sol=1.0, start=1000.0),
        _h("B", 0.022, sol=1.5, start=1010.0),
        _h("C", 0.024, sol=2.0, start=1030.0),
        _h("D", 0.022, sol=2.5, start=1035.0),
        _h("E", 0.010, sol=3.0, start=9000.0),
    ]
    verdict, reasons, metrics, _ = evaluate_holders(hs)
    assert verdict == "reject"
    assert any(r.startswith("launch_bundle_x4") for r in reasons)


# ── fresh wallets / dev / flags ────────────────────────────────────────────

def test_fresh_wallet_cluster_rejects():
    hs = [
        _h("A", 0.016, sol=1.0, is_new=True, start=1000),
        _h("B", 0.017, sol=1.5, is_new=True, start=1001),
        _h("C", 0.018, sol=2.0, is_new=True, start=1002),
        _h("D", 0.019, sol=2.5, is_new=True, start=1003),
        _h("E", 0.020, sol=3.0, start=9000),
    ]
    verdict, reasons, metrics, _ = evaluate_holders(hs)
    assert verdict == "reject"
    assert any(r.startswith("fresh_wallets_x4") for r in reasons)


def test_dev_holding_rejects():
    hs = _clean() + [_h("DEV", 0.05, creator=True, sol=2.2, funder="FDEV")]
    verdict, reasons, metrics, _ = evaluate_holders(hs, creator="DEV")
    assert verdict == "reject"
    assert any(r.startswith("dev_hold_") for r in reasons)


def test_flagged_bundler_supply_rejects():
    hs = [
        _h("A", 0.031, sol=1.0, tags=["bundler"]),
        _h("B", 0.032, sol=1.5, tags=["maker_token_tags"]),   # no flag
        _h("C", 0.031, sol=2.0, tags=["rat_trader"]),
        _h("D", 0.010, sol=2.5),
    ]
    verdict, reasons, metrics, _ = evaluate_holders(hs)
    assert verdict == "reject"
    assert any(r.startswith("flagged_supply_") for r in reasons)


def test_sniper_tag_alone_is_score_only():
    hs = [
        _h("A", 0.031, sol=1.0, tags=["sniper"], start=1000),
        _h("B", 0.010, sol=1.5, start=9000),
    ]
    verdict, reasons, _, _ = evaluate_holders(hs)
    assert verdict == "pass", reasons


# ── score shadow vs enforce ────────────────────────────────────────────────

def test_score_is_shadow_by_default_and_enforces_when_asked():
    hs = _clean()
    cfg = ScreenConfig(score_threshold=10.0)
    v_shadow, reasons, metrics, score = evaluate_holders(hs, cfg=cfg)
    assert score > 10 and v_shadow == "pass"       # shadow: logged, not enforced
    cfg2 = ScreenConfig(score_threshold=10.0, enforce_score=True)
    v_enforced, reasons2, _, _ = evaluate_holders(hs, cfg=cfg2)
    assert v_enforced == "reject"
    assert any(r.startswith("score_") for r in reasons2)


def test_enforce_hard_false_downgrades_to_pass():
    cfg = ScreenConfig(enforce_hard=False)
    v, reasons, _, _ = evaluate_holders([_h("A", 0.20)], cfg=cfg)
    assert v == "pass" and any(r.startswith("top1_") for r in reasons)


# ── fetch path: one call, cached afterwards ────────────────────────────────

_CHAIN_ROWS = [
    {"owner": f"W{i}", "pct": 0.02 + i * 0.001, "sol": 1.0 + i * 0.3,
     "is_new": False, "tags": [], "funder": f"F{i}", "funder_name": "",
     "funded_at": 1000 + i, "start_holding_at": 1000 + i * 900,
     "buy_count": 1, "is_creator": False}
    for i in range(6)
]


def test_screen_uses_chain_first_and_caches(monkeypatch, tmp_path):
    """2026-11-28: the chain reader is PRIMARY — GMGN holders must not be
    called while our own on-chain read succeeds."""
    monkeypatch.setattr(feed_store, "FEED_DB", tmp_path / "feed.db")
    feed_store.init_feed_db()
    monkeypatch.setattr(oscreen.rl, "gmgn_banned", lambda: False)

    gmgn_calls = {"n": 0}

    async def runner(args):
        gmgn_calls["n"] += 1
        return {"list": []}

    chain_calls = {"n": 0}

    async def chain(mint, creator=""):
        chain_calls["n"] += 1
        return [dict(r) for r in _CHAIN_ROWS]

    scr = OrganicScreen(cfg=ScreenConfig(), cli_runner=runner, rpc_reader=chain)
    v1 = asyncio.run(scr.screen("MINT1", creator=""))
    assert v1.verdict in ("pass", "reject") and v1.source == "chain"
    assert chain_calls["n"] == 1 and gmgn_calls["n"] == 0
    v2 = asyncio.run(scr.screen("MINT1", creator=""))
    assert chain_calls["n"] == 1 and gmgn_calls["n"] == 0
    assert v2.source == "cache" and scr.cache_hits == 1


def test_gmgn_fallback_only_when_chain_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(feed_store, "FEED_DB", tmp_path / "feed.db")
    feed_store.init_feed_db()
    monkeypatch.setattr(oscreen.rl, "gmgn_banned", lambda: False)

    gmgn_calls = {"n": 0}

    async def runner(args):
        gmgn_calls["n"] += 1
        return {"list": [
            {"address": f"W{i}", "amount_percentage": 0.02 + i * 0.001,
             "native_balance": str((1 + i) * 1_000_000_000),
             "native_transfer": {"from_address": f"F{i}", "timestamp": 1000 + i},
             "start_holding_at": 1000 + i * 900} for i in range(6)],
        }

    async def empty_chain(mint, creator=""):
        return []

    async def noop_augment(rows):
        return rows

    scr = OrganicScreen(cfg=ScreenConfig(), cli_runner=runner,
                        rpc_reader=empty_chain, chain_augmenter=noop_augment)
    v = asyncio.run(scr.screen("MINT2", creator=""))
    assert v.source == "gmgn" and gmgn_calls["n"] == 1

    # fallback disabled → no GMGN call at all
    gmgn_calls["n"] = 0
    scr2 = OrganicScreen(cfg=ScreenConfig(allow_gmgn_fallback=False),
                         cli_runner=runner, rpc_reader=empty_chain,
                         chain_augmenter=noop_augment)
    v2 = asyncio.run(scr2.screen("MINT3", creator=""))
    assert v2.verdict == "unavailable" and gmgn_calls["n"] == 0


def test_screen_unavailable_when_no_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(feed_store, "FEED_DB", tmp_path / "feed.db")
    feed_store.init_feed_db()
    monkeypatch.setattr(oscreen.rl, "gmgn_banned", lambda: False)

    async def runner(args):
        return {"list": []}

    async def no_rpc(mint, creator=""):
        return []

    scr = OrganicScreen(cfg=ScreenConfig(require_holder_data=True,
                                         allow_gmgn_fallback=False),
                        cli_runner=runner, rpc_reader=no_rpc)
    v = asyncio.run(scr.screen("MINT2", creator=""))
    assert v.verdict == "reject"                 # fail-closed when required
    assert v.reasons == ["holder_data_unavailable"]

    scr2 = OrganicScreen(cfg=ScreenConfig(require_holder_data=False),
                         cli_runner=runner, rpc_reader=no_rpc)
    v2 = asyncio.run(scr2.screen("MINT3", creator=""))
    assert v2.verdict == "unavailable"           # default: don't starve feed


# ── Tier-2 RPC helper ──────────────────────────────────────────────────────

def test_first_funder_picks_largest_outflow():
    tx = {
        "transaction": {"message": {"accountKeys": [
            {"pubkey": "OWNER"}, {"pubkey": "FUNDER"}, {"pubkey": "FEEPAYER"}]}},
        "meta": {"preBalances": [0, 5_000_000_000, 1_000_000_000],
                 "postBalances": [1_000_000_000, 3_900_000_000, 1_099_000_000]},
    }
    assert oscreen._first_funder(tx, "OWNER") == "FUNDER"


def test_first_funder_returns_empty_on_bad_shape():
    assert oscreen._first_funder({}, "OWNER") == ""


# ── GMGN indications are wiped and recomputed from the chain ────────────────

def test_gmgn_rows_augmented_from_chain(monkeypatch, tmp_path):
    """GMGN's is_new/tags/funding must NOT reach the verdict: the augmenter
    wipes them and self-computes the same primitives from chain signatures."""
    import time as _t

    now = _t.time()
    holders = [
        {"owner": "FRESH", "token_account": "TA_FRESH", "pct": 0.03,
         "sol": 1.0, "is_new": True, "tags": ["bundler"], "funder": "GMGN_F",
         "funded_at": 123, "start_holding_at": 1, "buy_count": 1,
         "is_creator": False},
        {"owner": "OLD", "token_account": "TA_OLD", "pct": 0.02,
         "sol": 9.0, "is_new": False, "tags": ["sniper"], "funder": "GMGN_F2",
         "funded_at": 5, "start_holding_at": 2, "buy_count": 1,
         "is_creator": False},
    ]

    async def fake_call(session, method, params):
        if method == "getSignaturesForAddress":
            acct = params[0]
            if acct == "TA_FRESH":
                return [{"blockTime": int(now) - 30}, {"blockTime": int(now) - 60}]
            if acct == "TA_OLD":
                return [{"blockTime": int(now) - 100}]
            if acct == "FRESH":                       # wallet born yesterday
                return [{"blockTime": int(now) - 86400},
                        {"blockTime": int(now) - 90000}]
            if acct == "OLD":                          # ancient wallet, >1000 sigs
                return [{"blockTime": int(now) - 500}] * 1001
        if method == "getTransaction":
            return {"blockTime": int(now) - 90000,
                    "transaction": {"message": {"accountKeys": [
                        {"pubkey": "FRESH"}, {"pubkey": "FUNDING_W"}]}},
                    "meta": {"preBalances": [0, 5_000_000_000],
                             "postBalances": [1_000_000_000, 3_900_000_000]}}
        return None

    out = asyncio.run(oscreen.augment_rows_from_chain(
        holders, call=fake_call))
    fresh, old = out[0], out[1]
    # indications wiped
    assert fresh["tags"] == [] and old["tags"] == []
    assert fresh["funder"] != "GMGN_F" and old["funder"] != "GMGN_F2"
    # self-computed
    assert fresh["is_new"] is True and old["is_new"] is False
    assert fresh["funder"] == "FUNDING_W" and fresh["funded_at"] > 0
    assert fresh["start_holding_at"] == int(now) - 60
    assert old["start_holding_at"] == int(now) - 100
