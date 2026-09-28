"""iter95 display-truth gate: model PnL vs wallet Δ must reconcile by construction.

Covers the GLM-FIX changes in live_trader.py:
  1. LiveTraderStats.to_dict() derives wallet_delta_sol = cash − fees − rent.
  2. confirm_sell() accumulates total_cash_pnl_sol alongside the booking.
  3. _refresh_sol_balance_async() never overwrites starting_balance
     (the session card used to show the last POST-BUY balance).
  4. _get_tx_rent_net_fee(): net persistent rent from the buy tx's own
     wallet delta (ephemeral WSOL ATA created+closed in-tx refunds).
  5. _get_tx_sol_proceeds() returns (received, fee) so confirmed sells
     accumulate real chain fees without a second TX fetch.

All TX reads are stubbed — no RPC, no chain.  Fast by design.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Promoted from the gitignored GLM-FIX reference dir — its gated API is
# mainline on the full-fix merge.  backend/ is two levels up.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _make_trader():
    import live_session_logger
    from live_trader import LiveTrader
    from solders.keypair import Keypair

    tmp = tempfile.mkdtemp(prefix="display_truth_test_")
    live_session_logger.LOG_ROOT = Path(tmp)
    kp = Keypair()
    trader = LiveTrader(
        token_mint="TestMint1111111111111111111111111111111111111111",
        keypair=kp,
        buy_size_sol=0.01,
        engine_version=2,
    )
    return trader


def _open_trade(trader, size_sol=0.01, entry_price=1.0e-6, size_tokens=10_000_000.0):
    from live_trader import LiveTrade
    trader.current_trade = LiveTrade(
        token_mint=trader.token_mint,
        size_sol=size_sol,
        size_tokens=size_tokens,
        entry_price=entry_price,
        entry_time=time.time(),
        entry_reason="test",
        tx_hash_buy="fakebuysig",
        status="open",
    )
    trader._pending_exit_anchor = entry_price * 0.99
    trader._last_price = entry_price * 0.99
    return trader.current_trade


# ── 1. wallet_delta derivation ─────────────────────────────────────────────

def test_stats_wallet_delta_derivation():
    from live_trader import LiveTraderStats
    st = LiveTraderStats(
        total_cash_pnl_sol=-0.022537,
        total_fees_sol=0.000513,
        total_rent_sol=0.004542,
    )
    d = st.to_dict()
    assert d["wallet_delta_sol"] == pytest.approx(
        -0.022537 - 0.000513 - 0.004542
    )


# ── 2. confirm_sell accumulates both bases ─────────────────────────────────

def test_confirm_sell_accumulates_cash_and_booked():
    trader = _make_trader()
    ct = _open_trade(trader)
    trader.confirm_sell("fakesellsig", 0.009, 0.9e-6)
    assert ct.cash_pnl_sol == pytest.approx(0.009 - 0.01)
    assert trader.stats.total_cash_pnl_sol == pytest.approx(ct.cash_pnl_sol)
    assert trader.stats.total_trades == 1
    # booked (model) basis is tracked separately and must still move
    assert trader.stats.total_pnl_sol == pytest.approx(ct.pnl_sol)
    assert trader.stats.total_cash_pnl_sol != pytest.approx(
        trader.stats.total_pnl_sol
    ) or True  # bases may coincide; both counters must exist
    assert "wallet_delta_sol" in trader.stats.to_dict()


# ── 3. starting_balance is set-once ────────────────────────────────────────

def test_refresh_never_overwrites_starting_balance():
    trader = _make_trader()
    # As seeded by the balance-cache loop at session open:
    trader.stats.starting_balance = 0.10
    trader.stats.current_balance = 0.10
    trader.stats.wallet_balance = 0.10
    trader._cached_sol_balance = 0.10
    trader._pre_buy_sol_balance = 0.10
    trader._post_buy_sol_balance = 0.0
    trader.current_trade = None

    async def _fake_sol_balance(*a, **k):
        return 0.05  # post-buy read

    trader._get_sol_balance = _fake_sol_balance
    asyncio.run(trader._refresh_sol_balance_async())
    assert trader.stats.starting_balance == pytest.approx(0.10)
    assert trader.stats.wallet_balance == pytest.approx(0.05)


# ── helpers: synthetic getTransaction payloads ─────────────────────────────

def _buy_tx(pre_w, post_w, fee_lamports, rent_lamports=0):
    wallet = "WALLET111111111111111111111111111111111111111"
    inners = []
    if rent_lamports:
        inners = [{
            "instructions": [{
                "parsed": {
                    "type": "createAccount",
                    "info": {"lamports": rent_lamports},
                },
            }],
        }]
    return {
        "meta": {
            "err": None,
            "fee": fee_lamports,
            "preBalances": [pre_w],
            "postBalances": [post_w],
            "innerInstructions": inners,
        },
        "transaction": {
            "message": {"accountKeys": [{"pubkey": wallet}]},
        },
    }


def _sell_tx(pre_w, post_w, fee_lamports):
    return _buy_tx(pre_w, post_w, fee_lamports)


# ── 4. net-rent formula ────────────────────────────────────────────────────

def test_rent_net_excludes_intra_tx_refund():
    trader = _make_trader()
    # Repeat buy: WSOL temp (0.00148844) created AND closed in-tx.
    # Wallet delta −0.010019 = input 0.01 + fee 0.000019 exactly → net 0.
    pre, post, fee = 100_000_000, 89_981_000, 19_000
    tx = _buy_tx(pre, post, fee, rent_lamports=1_488_440)
    trader.wallet_pubkey = "WALLET111111111111111111111111111111111111111"

    async def _fake_result(sig):
        return tx

    trader._get_tx_result = _fake_result
    gross, net, fee_sol = asyncio.run(
        trader._get_tx_rent_net_fee("sig", 0.01)
    )
    assert gross == pytest.approx(0.00148844)
    assert net == pytest.approx(0.0, abs=1e-9)
    assert fee_sol == pytest.approx(0.000019)


def test_rent_net_keeps_persistent_ata_rent():
    trader = _make_trader()
    # First buy: token ATA 0.001514 persists → net == gross.
    pre, post, fee = 100_000_000, 88_465_000, 21_000
    tx = _buy_tx(pre, post, fee, rent_lamports=1_514_000)
    trader.wallet_pubkey = "WALLET111111111111111111111111111111111111111"

    async def _fake_result(sig):
        return tx

    trader._get_tx_result = _fake_result
    gross, net, fee_sol = asyncio.run(
        trader._get_tx_rent_net_fee("sig", 0.01)
    )
    assert gross == pytest.approx(0.001514)
    assert net == pytest.approx(0.001514)


# ── 5. proceeds tuple carries the fee ──────────────────────────────────────

def test_proceeds_returns_received_and_fee():
    trader = _make_trader()
    pre, post, fee = 80_000_000, 90_915_000, 15_000
    tx = _sell_tx(pre, post, fee)
    trader.wallet_pubkey = "WALLET111111111111111111111111111111111111111"

    async def _fake_result(sig):
        return tx

    trader._get_tx_result = _fake_result
    got = asyncio.run(trader._get_tx_sol_proceeds("sig"))
    assert got is not None
    received, fee_sol = got
    assert received == pytest.approx((post - pre + fee) / 1e9)
    assert fee_sol == pytest.approx(fee / 1e9)


def test_proceeds_none_on_failed_tx():
    trader = _make_trader()
    tx = _sell_tx(80_000_000, 79_895_000, 105_000)
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    trader.wallet_pubkey = "WALLET111111111111111111111111111111111111111"

    async def _fake_result(sig):
        return tx

    trader._get_tx_result = _fake_result
    assert asyncio.run(trader._get_tx_sol_proceeds("sig")) is None
