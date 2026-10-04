"""Feed: Setuh filter table, tape tracker math, curve lanes, trap registry."""

import asyncio
import time

import pytest

from degen.feed import DegenConfig, DegenFeed, RecentBirth, TapeTracker, CURVE_SOL_AT_GRAD

MINT = "FeedTestMint11111111111111111111111111111111"


# ── Setuh table gates ────────────────────────────────────────────────────────

def test_new_pairs_gate_mcap_floor():
    feed = DegenFeed(DegenConfig(new_pairs_min_mcap_usd=3_500.0))
    feed._sol_usd = 170.0
    now = time.time()
    ok, _ = feed._new_pairs_pass(RecentBirth(mint=MINT, ts=now, birth_mcap_sol=25.0), now)
    assert ok, "25 SOL × $170 = $4,250 ≥ $3,500 must pass"
    ok, _ = feed._new_pairs_pass(RecentBirth(mint=MINT, ts=now, birth_mcap_sol=15.0), now)
    assert not ok, "15 SOL × $170 = $2,550 < $3,500 must fail"


def test_new_pairs_gate_age():
    feed = DegenFeed(DegenConfig())
    feed._sol_usd = 170.0
    now = time.time()
    ok, _ = feed._new_pairs_pass(RecentBirth(mint=MINT, ts=now - 999, birth_mcap_sol=30.0), now)
    assert not ok, "discovery older than the 6-minute window must fail"


def test_soon_gate_curve_band_and_mcap():
    cfg = DegenConfig(soon_min_mcap_usd=6_000.0, soon_curve_min=0.85, soon_curve_max=0.97)
    feed = DegenFeed(cfg)
    feed._sol_usd = 170.0
    now = time.time()

    tr = TapeTracker(MINT, first_seen=now - 600)          # 10 min old
    tr.curve_frac = 0.90                                   # in band
    tr.last_mcap_sol = 40.0                                # $6,800
    ok, _ = feed._soon_pass(tr, now)
    assert ok

    tr2 = TapeTracker(MINT, first_seen=now - 600)
    tr2.curve_frac = 0.50                                  # mid-curve, not "soon"
    tr2.last_mcap_sol = 40.0
    ok, _ = feed._soon_pass(tr2, now)
    assert not ok

    tr3 = TapeTracker(MINT, first_seen=now - 600)
    tr3.curve_frac = 0.90
    tr3.last_mcap_sol = 20.0                               # $3,400 < $6k floor
    ok, _ = feed._soon_pass(tr3, now)
    assert not ok

    tr4 = TapeTracker(MINT, first_seen=now - 3600)         # too old for the 30-min window
    tr4.curve_frac = 0.90
    tr4.last_mcap_sol = 40.0
    ok, _ = feed._soon_pass(tr4, now)
    assert not ok


# ── tape tracker math ────────────────────────────────────────────────────────

def test_tracker_drawdown_and_peak():
    now = time.time()
    tr = TapeTracker(MINT, first_seen=now)
    for i, p in enumerate([1.0, 1.5, 2.0, 1.4, 0.8]):
        tr.update({"mint": MINT, "tx_type": "buy", "sol_amount": 1.0,
                   "price": p, "timestamp": now + i, "market_cap_sol": p * 100})
    assert tr.peak_price == 2.0
    assert tr.in_drawdown
    assert abs(tr.drawdown - 0.60) < 1e-9
    assert tr.low_since_peak == 0.8


def test_momentum_resume_requires_vol_flip():
    now = time.time()
    tr = TapeTracker(MINT, first_seen=now)
    # three up-ticks but sell pressure dominates → no momentum
    seq = [(1.0, True, 1.0), (0.5, False, 5.0), (0.5, False, 5.0), (0.5, False, 5.0),
           (0.52, True, 1.0), (0.55, True, 1.0), (0.58, True, 1.0)]
    ts = now
    for p, buy, sol in seq:
        tr.update({"mint": MINT, "tx_type": "buy" if buy else "sell", "sol_amount": sol,
                   "price": p, "timestamp": ts, "market_cap_sol": p * 100})
        ts += 1
    assert tr.up_tick_streak >= 3
    assert not tr.momentum_resume()

    tr2 = TapeTracker(MINT, first_seen=now)
    ts = now
    for p, buy, _sol in seq:
        sol = 5.0 if buy else 0.5               # pressure flipped: buys dominate
        tr2.update({"mint": MINT, "tx_type": "buy" if buy else "sell", "sol_amount": sol,
                    "price": p, "timestamp": ts, "market_cap_sol": p * 100})
        ts += 1
    assert tr2.momentum_resume()


def test_curve_frac_from_pool_sol():
    now = time.time()
    tr = TapeTracker(MINT, first_seen=now)
    tr.update({"mint": MINT, "tx_type": "buy", "sol_amount": 1.0, "price": 1.0,
               "timestamp": now, "market_cap_sol": 100.0,
               "pool_sol": CURVE_SOL_AT_GRAD * 0.9})
    assert abs(tr.curve_frac - 0.90) < 1e-9


# ── feed lifecycle (event loop driven directly — no pytest-asyncio dep) ─────

def _run(coro):
    return asyncio.run(coro)


def test_handle_birth_emits_new_pair_and_watches():
    async def scenario():
        feed = DegenFeed(DegenConfig(new_pairs_min_mcap_usd=1.0))
        feed._sol_usd = 170.0
        now = time.time()
        feed._handle_birth({"mint": MINT, "name": "Test", "symbol": "TST",
                            "marketCapSol": 30.0, "timestamp": now, "solAmount": 0.5})
        assert MINT in feed.trackers
        cand = await asyncio.wait_for(feed.candidates.get(), timeout=2.0)
        feed._stopped = True
        return cand

    cand = _run(scenario())
    assert cand["event"] == "new_pair"


def test_trap_register_dispatches_sync():
    from degen.traps import TrapMachine, TrapSpec

    async def scenario():
        fired = []

        async def on_trigger(fire):
            fired.append(fire)

        feed = DegenFeed(DegenConfig())
        feed.on_trap_trigger = on_trigger
        spec = TrapSpec(mint=MINT, size_sol=0.05, nuke_min_pct=0.10, up_ticks=1,
                        vol_flip=False, reclaim_pct=0.0, expires_in_s=60)
        trap = TrapMachine(spec)
        feed.register_trap(trap)

        # simulate the tape loop path: trades flow through trap.on_trade first
        now = time.time()
        for i, p in enumerate([1.0, 0.85, 0.83, 0.84, 0.86]):
            tr = feed.trackers[MINT]
            fire = trap.on_trade(
                {"mint": MINT, "tx_type": "buy", "sol_amount": 1.0, "price": p,
                 "timestamp": now + i, "market_cap_sol": p * 100}, tr)
            tr.update({"mint": MINT, "tx_type": "buy", "sol_amount": 1.0, "price": p,
                       "timestamp": now + i, "market_cap_sol": p * 100})
            if fire:
                await feed._dispatch_trigger(fire)
                break
        feed._stopped = True
        return fired

    fired = _run(scenario())
    assert fired and fired[0]["kind"] == "nuke_momentum"
    assert fired[0]["trap_id"].startswith("trap-")
