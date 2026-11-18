"""
Organic gate tests (2026-11-18) — per-wallet bundle evidence.

Fixtures mirror the real payload shape captured from
`/v1/market/token_top_holders` (gmgn-cli 1.5.2 / openapi.gmgn.ai, 2026-11-18):
`amount_percentage`, `native_balance` (lamports), `created_at` (wallet age),
`start_holding_at` (first buy), `native_transfer.{from_address,timestamp,name}`,
`maker_token_tags`, `tags`, `is_suspicious`.

Each test isolates ONE rule so a regression names the evidence it broke.

Run:  cd backend && python -m pytest test_organic_gate.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import organic_gate
from organic_gate import GateConfig, OrganicGate, evaluate

CREATED = 1_800_000_000.0
MINT = "Mint" + "x" * 38


def holder(pct: float, *, bought_at: float = None, wallet_created: float = 0.0,
           sol: float = 2.0, funder: str = "", funder_ts: float = 0.0,
           funder_name: str = "", maker_tags=None, tags=None,
           suspicious: bool = False, addr: str = "") -> dict:
    """One holder row: supply share `pct`, first buy at `bought_at`."""
    return {
        "address": addr or ("H" + str(int(pct * 1e6)).zfill(6) + str(abs(hash((pct, bought_at, funder))) % 1000)),
        "account_address": "A" + str(abs(hash((pct, addr))) % 10 ** 8),
        "amount_percentage": pct,
        "native_balance": str(int(sol * 1e9)),
        "created_at": int(wallet_created),
        "start_holding_at": int(bought_at if bought_at is not None else CREATED + 3600),
        "native_transfer": {"from_address": funder,
                            "timestamp": int(funder_ts),
                            "name": funder_name},
        "maker_token_tags": list(maker_tags or ["top_holder"]),
        "tags": list(tags or []),
        "is_suspicious": suspicious,
        "buy_tx_count_cur": 1,
        "sell_tx_count_cur": 0,
    }


def clean_table(n: int = 20, each: float = 0.01) -> list[dict]:
    """A boring, healthy holder table: varied lots, old wallets, varied SOL."""
    rows = []
    for i in range(n):
        # Deliberately irregular lot sizes — identical lots are their own signal.
        pct = each * (1.0 + ((i * 37) % 11 - 5) / 25.0)
        rows.append(holder(
            pct,
            bought_at=CREATED + 600 + i * 337,
            wallet_created=CREATED - 400 * 86400,          # years old
            sol=0.4 + i * 0.37,                            # varied balances
            funder=f"F{i}",                                # all different funders
            funder_ts=CREATED - 400 * 86400 + i * 1000,
            tags=["axiom"] if i % 3 == 0 else ([] if i % 3 == 1 else ["photon"]),
        ))
    return rows


def cfg(**over) -> GateConfig:
    c = GateConfig()
    for k, v in over.items():
        setattr(c, k, v)
    return c


def ev(rows, **over):
    return evaluate(rows, created_ts=CREATED, cfg=cfg(**over), mint=MINT, symbol="TST")


# ── baseline ────────────────────────────────────────────────────────────────

def test_clean_table_passes():
    res = ev(clean_table())
    assert res.passed, res.reasons
    assert res.critical == [] and res.soft == []
    assert res.metrics["top10_pct"] <= 0.30
    assert res.metrics["max_single_pct"] <= 0.05


def test_pool_row_is_excluded_from_concentration():
    """The LP row must not count as a holder (it routinely owns 40-60%)."""
    rows = clean_table()
    pool = holder(0.55, bought_at=CREATED, wallet_created=0, sol=0.0,
                  addr="POOL", maker_tags=[])
    pool["native_transfer"] = {"from_address": "", "timestamp": 0, "name": None}
    rows.append(pool)
    res = ev(rows)
    assert res.metrics["pool_pct"] == 0.55
    assert res.metrics["max_single_pct"] <= 0.05
    assert res.passed, res.reasons


# ── Setuh check 1: distribution ─────────────────────────────────────────────

def test_single_holder_above_threshold_rejects():
    rows = clean_table()
    rows[3]["amount_percentage"] = 0.09
    res = ev(rows, max_single_holder_pct=0.05)
    assert not res.passed
    assert "single_holder_concentration" in res.critical
    assert res.metrics["max_single_pct"] == 0.09


def test_top10_concentration_rejects():
    rows = clean_table(20, each=0.001)
    for i in range(10):
        rows[i]["amount_percentage"] = 0.04
    res = ev(rows)
    assert not res.passed
    assert "top10_concentration" in res.critical


# ── launch-window buying: the bundle set-up ─────────────────────────────────

def test_launch_window_bundle_rejects():
    """8 wallets buying inside the first 2 minutes holding 12% ⇒ bundle."""
    rows = clean_table(24, each=0.001)
    for i in range(8):
        rows[i]["amount_percentage"] = 0.015
        rows[i]["start_holding_at"] = int(CREATED + 5 + i)
    res = ev(rows, max_launch_bundle_pct=0.10)
    assert not res.passed
    assert "launch_bundle_supply" in res.critical
    assert res.metrics["launch_rows"] == 8


def test_single_launch_wallet_rejects():
    rows = clean_table()
    rows[0]["amount_percentage"] = 0.045
    rows[0]["start_holding_at"] = int(CREATED + 1)
    res = ev(rows, max_launch_single_pct=0.03)
    assert "launch_wallet_concentration" in res.critical


def test_late_buys_are_not_launch_supply():
    rows = clean_table(20, each=0.004)
    for r in rows:
        r["start_holding_at"] = int(CREATED + 3600)     # an hour later
    res = ev(rows)
    assert res.metrics["launch_bundle_pct"] == 0.0
    assert res.passed, res.reasons


# ── Setuh check 3: funded from the same place at the same time ─────────────

def test_funder_cluster_rejects():
    rows = clean_table(24, each=0.001)
    for i in range(6):
        rows[i]["amount_percentage"] = 0.02
        rows[i]["native_transfer"] = {"from_address": "SharedFund",
                                      "timestamp": int(CREATED - 3600 + i),
                                      "name": None}
    res = ev(rows, max_funder_cluster_pct=0.10)
    assert not res.passed
    assert "funder_cluster" in res.critical
    assert res.metrics["funder_cluster_n"] == 6


def test_cex_funder_is_not_a_cluster():
    """Six wallets funded by Coinbase are organic; a CEX hot wallet is not a bundle."""
    rows = clean_table(24, each=0.001)
    for i in range(6):
        rows[i]["amount_percentage"] = 0.02
        rows[i]["native_transfer"] = {"from_address": "CoinbaseHot",
                                      "timestamp": int(CREATED - 3600 + i * 900),
                                      "name": "Coinbase"}
    res = ev(rows)
    assert res.metrics["funder_cluster_n"] <= 1
    assert "funder_cluster" not in res.reasons


def test_same_funder_spread_over_days_is_not_a_cluster():
    rows = clean_table(24, each=0.001)
    for i in range(6):
        rows[i]["amount_percentage"] = 0.02
        rows[i]["native_transfer"] = {"from_address": "SharedFund",
                                      "timestamp": int(CREATED - (i + 1) * 5 * 86400),
                                      "name": None}
    res = ev(rows)
    assert res.metrics["funder_cluster_n"] == 1     # every wallet its own block
    assert "funder_cluster" not in res.reasons


# ── fresh-wallet cohorts ────────────────────────────────────────────────────

def test_prelaunch_wallet_cohort_rejects():
    rows = clean_table(24, each=0.001)
    for i in range(5):
        rows[i]["amount_percentage"] = 0.02
        rows[i]["created_at"] = int(CREATED - 3600)
    res = ev(rows, max_created_cohort_pct=0.08)
    assert not res.passed
    assert "prelaunch_wallet_cohort" in res.critical
    assert res.metrics["created_cohort_n"] == 5


def test_fresh_tag_share_is_soft():
    rows = clean_table(20, each=0.015)
    for i in range(18):
        rows[i]["tags"] = ["fresh_wallet"]
    res = ev(rows, max_fresh_tag_pct=0.25)
    assert "fresh_wallet_share" in res.soft
    assert res.score == 1


# ── Setuh check 2/4: dormant wallets, identical lots, one terminal ─────────

def test_dust_top_holders_soft_signal():
    rows = clean_table(20, each=0.01)
    for r in rows[:12]:
        r["native_balance"] = str(int(0.001 * 1e9))
    res = ev(rows)
    assert "dust_top_holders" in res.soft
    assert res.metrics["median_top10_sol"] < 0.02


def test_matched_lot_sizes_soft_signal():
    rows = clean_table(20, each=0.01)
    for i in range(6):
        rows[i]["amount_percentage"] = 0.025          # 2.5/2.5/2.5/2.5
    res = ev(rows)
    assert "matched_lot_sizes" in res.soft
    assert res.metrics["matched_lot_pairs"] >= 4


def test_terminal_dominance_soft_signal():
    rows = clean_table(20, each=0.01)
    for r in rows:
        r["tags"] = ["terminal"]
    res = ev(rows)
    assert "terminal_dominance" in res.soft
    assert res.metrics["terminal_tag"] == "terminal"


def test_stacked_soft_signals_reject():
    """Three weak tells converge ⇒ reject (the stack is what matters)."""
    rows = clean_table(20, each=0.015)
    for i, r in enumerate(rows[:18]):
        r["tags"] = ["fresh_wallet"]
        r["native_balance"] = "1000"
    for i in range(6):
        rows[i]["amount_percentage"] = 0.025
    res = ev(rows, max_fresh_tag_pct=0.25, max_matched_lot_pairs=4)
    assert not res.passed
    assert "stacked_risk_signals" in res.reasons


def test_two_soft_signals_still_pass():
    rows = clean_table(20, each=0.01)
    for r in rows[:12]:
        r["native_balance"] = "1000"                  # dust
    res = ev(rows)
    assert res.soft == ["dust_top_holders"]
    assert res.passed, res.reasons


# ── GMGN-tagged supply ──────────────────────────────────────────────────────

def test_bundler_tagged_supply_rejects():
    rows = clean_table(20, each=0.02)
    for i in range(6):
        rows[i]["maker_token_tags"] = ["bundler"]
    res = ev(rows, max_bundler_tag_pct=0.10)
    assert "bundler_tagged_supply" in res.critical
    assert res.metrics["bundler_tag_pct"] > 0.10


def test_sniper_tagged_supply_rejects():
    rows = clean_table(20, each=0.015)
    for i in range(20):
        rows[i]["maker_token_tags"] = ["sniper"]
    res = ev(rows, max_sniper_holder_pct=0.20)
    assert "sniper_tagged_supply" in res.critical


# ── deployer / social hygiene (row-level) ──────────────────────────────────

def test_dev_still_holding_is_soft():
    """`creator_hold` also fires on successful tokens whose dev kept a bag, so
    it is a weighted tell — the quantified dev_hold_rate is the hard gate."""
    res = evaluate(clean_table(), created_ts=CREATED, cfg=cfg(), mint=MINT,
                   row={"creator_token_status": "creator_hold"})
    assert "dev_still_holding" in res.soft
    assert res.passed


def test_quantified_dev_hold_rejects():
    res = evaluate(clean_table(), created_ts=CREATED, cfg=cfg(), mint=MINT,
                   row={"creator_hold_rate": 0.12})
    assert "dev_hold_rate" in res.critical


def test_dev_team_hold_rejects():
    res = evaluate(clean_table(), created_ts=CREATED, cfg=cfg(), mint=MINT,
                   row={"dev_team_hold_rate": 0.20})
    assert "dev_team_hold" in res.critical


def test_sniper_hold_rate_rejects():
    res = evaluate(clean_table(), created_ts=CREATED, cfg=cfg(), mint=MINT,
                   row={"top70_sniper_hold_rate": 0.30})
    assert "sniper_hold_rate" in res.critical


def test_dev_closed_passes():
    res = evaluate(clean_table(), created_ts=CREATED, cfg=cfg(), mint=MINT,
                   row={"creator_token_status": "creator_close"})
    assert res.passed, res.reasons


def test_serial_creator_rejects():
    res = evaluate(clean_table(), created_ts=CREATED, cfg=cfg(), mint=MINT,
                   row={"creator_created_count": 9})
    assert "serial_creator" in res.critical


def test_creator_field_absent_does_not_reject():
    """Absent supplementary row fields must never fail-close a clean table."""
    res = evaluate(clean_table(), created_ts=CREATED, cfg=cfg(), mint=MINT, row={})
    assert res.passed, res.reasons


def test_copycat_ambiguity_gates_are_off_by_default():
    """image/website/twitter counts also fire on the *original* of a copied meme
    (real GTA VI token measured website_dup=801), so they must not gate by
    default — only when a threshold is explicitly configured."""
    res = evaluate(clean_table(), created_ts=CREATED, cfg=cfg(), mint=MINT,
                   row={"twitter_create_token_count": 4000, "image_dup": 7,
                         "website_dup": 900})
    assert res.passed, res.reasons


def test_copycat_gates_enforce_when_configured():
    res = evaluate(clean_table(), created_ts=CREATED,
                   cfg=cfg(max_twitter_create_token_count=5, max_image_dup=2,
                           max_website_dup=5), mint=MINT,
                   row={"twitter_create_token_count": 4000, "image_dup": 7,
                         "website_dup": 900})
    assert {"twitter_token_spam", "image_reuse", "website_reuse"} <= set(res.critical)


def test_unburned_lp_rejects():
    res = evaluate(clean_table(), created_ts=CREATED, cfg=cfg(), mint=MINT,
                   row={"burn_status": "not_burn"})
    assert "lp_not_burned" in res.critical


def test_round_tripped_token_rejects():
    """A token already -80% from its ATH is a distribution in progress."""
    res = evaluate(clean_table(), created_ts=CREATED, cfg=cfg(), mint=MINT,
                   row={"history_highest_market_cap": 1_000_000.0,
                        "market_cap": 200_000.0})
    assert "round_tripped" in res.critical
    assert res.metrics["ath_drawdown_pct"] == 0.8


# ── float sanity ───────────────────────────────────────────────────────────

def test_pool_holding_almost_everything_rejects():
    """A "pool" at 99.5% of supply leaves no float (live GTASZN case)."""
    rows = clean_table(20, each=0.0001)
    pool = holder(0.99, wallet_created=0, sol=0.0, addr="POOL")
    pool["native_transfer"] = {"from_address": "", "timestamp": 0, "name": None}
    res = evaluate(rows + [pool], created_ts=CREATED, cfg=cfg(), mint=MINT,
                   row={"market_cap": 168_000.0})
    assert "supply_in_pool" in res.critical
    assert "thin_float" in res.critical
    assert res.metrics["float_usd"] < 25_000.0


def test_healthy_pool_share_is_fine():
    rows = clean_table(20, each=0.02)
    pool = holder(0.50, wallet_created=0, sol=0.0, addr="POOL")
    pool["native_transfer"] = {"from_address": "", "timestamp": 0, "name": None}
    res = evaluate(rows + [pool], created_ts=CREATED, cfg=cfg(), mint=MINT,
                   row={"market_cap": 500_000.0})
    assert res.passed, res.reasons
    assert 0.49 < res.metrics["float_pct"] < 0.51


# ── fail-closed ────────────────────────────────────────────────────────────

def test_empty_table_is_unverified_not_clean():
    res = ev([])
    assert not res.passed and res.error == "empty_holder_table"
    assert res.blocked


def test_pool_only_table_is_unverified():
    pool = holder(0.99, wallet_created=0, sol=0.0)
    pool["native_transfer"] = {"from_address": "", "timestamp": 0, "name": None}
    res = ev([pool])
    assert res.error == "no_holders_after_pool"


# ── I/O shell: caching, rate limits, modes ────────────────────────────────

class FakeGate(OrganicGate):
    def __init__(self, rows=None, err="", **kw):
        super().__init__(GateConfig(**kw), api_key="test-key")
        self._rows = rows
        self._err = err
        self.calls = 0

    async def _fetch_holders(self, mint):
        self.calls += 1
        return self._rows, self._err


class _Cand:
    def __init__(self, mint=MINT, symbol="TST", created=CREATED):
        self.mint = mint
        self.symbol = symbol
        self.creation_timestamp = created


def test_verify_passes_and_caches():
    g = FakeGate(rows=clean_table())
    c = _Cand()
    r1 = asyncio.run(g.verify(c))
    r2 = asyncio.run(g.verify(c))
    assert r1.passed and r2.passed
    assert g.calls == 1                      # second call served from cache
    assert g.total_passed == 1


def test_verify_rejects_on_evidence():
    rows = clean_table(20, each=0.003)
    rows[0]["amount_percentage"] = 0.12
    g = FakeGate(rows=rows)
    r = asyncio.run(g.verify(_Cand()))
    assert not r.passed and "single_holder_concentration" in r.reasons
    assert g.total_rejected == 1
    assert g.last_reject["symbol"] == "TST"
    assert r.blocked


def test_verify_fetch_error_is_fail_closed():
    g = FakeGate(rows=None, err="timeout")
    r = asyncio.run(g.verify(_Cand()))
    assert not r.passed and r.error == "timeout"
    assert r.blocked
    assert g.total_unverified == 1


def test_shadow_mode_records_but_does_not_block():
    rows = clean_table(20, each=0.003)
    rows[0]["amount_percentage"] = 0.12
    g = FakeGate(rows=rows, mode="shadow")
    r = asyncio.run(g.verify(_Cand()))
    assert not r.passed
    assert not r.blocked                      # shadow never blocks a session


def test_off_mode_skips_fetch():
    g = FakeGate(rows=clean_table(), mode="off")
    r = asyncio.run(g.verify(_Cand()))
    assert r.passed and r.metrics.get("skipped")
    assert g.calls == 0


def test_rate_limited_is_deferred_not_scored():
    """A ban window is waited out once (shared key), then reported as deferred."""
    class BanGate(FakeGate):
        async def _fetch_holders(self, mint):
            self.calls += 1
            return None, "rate_limited"

    g = BanGate(rows=None)
    g.config.fetch_wait_budget = 0          # don't sleep in a unit test
    organic_gate.rl.note_gmgn_429(time.time() + 30, source="test_organic_gate")
    try:
        r = asyncio.run(g.verify(_Cand()))
        assert r.error == "rate_limited"
        assert g.calls == 0                 # no fetch while banned (budget 0)
    finally:
        organic_gate.rl._gmgn_banned_until = 0.0


def test_build_config_maps_flat_attributes():
    class CF:
        organic_gate_mode = "shadow"
        organic_max_single_holder_pct = 0.07
        organic_max_fetch_per_tick = 3

    gc = organic_gate.build_config(CF())
    assert gc.mode == "shadow"
    assert gc.max_single_holder_pct == 0.07
    assert gc.max_fetch_per_tick == 3
    assert gc.max_top10_holder_pct == GateConfig().max_top10_holder_pct
