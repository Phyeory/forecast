"""Reflex tier: nuke→momentum trap machine (deterministic, no network)."""

import time

from degen.feed import TapeTracker
from degen.traps import TrapMachine, TrapSpec, replay_trap


MINT = "TestMint1111111111111111111111111111111111"


def trade(ts, price, sol=1.0, buy=True, mcap_sol=None):
    return {
        "mint": MINT, "tx_type": "buy" if buy else "sell",
        "sol_amount": sol, "token_amount": sol / max(price, 1e-12),
        "price": price, "timestamp": ts,
        "market_cap_sol": mcap_sol if mcap_sol is not None else price * 1000,
        "pool_sol": price * 1000 / 410.7,   # not exact — irrelevant to the trap
    }


def spec(**kw):
    base = dict(mint=MINT, size_sol=0.05, nuke_min_pct=0.50, max_drawdown_pct=0.75,
                up_ticks=3, vol_flip=True, reclaim_pct=0.02, expires_in_s=900,
                min_mcap_sol=0.0)
    base.update(kw)
    return TrapSpec(**base)


def test_fires_on_nuke_then_momentum():
    """Setuh: never buy the knife — buy the momentum after the nuke."""
    t0 = time.time()
    trades = []
    # run-up to a peak
    p = 1.0
    for i in range(20):
        p *= 1.01
        trades.append(trade(t0 + i, p))
    peak = p
    # the nuke: ~57% drawdown
    for i in range(20, 44):
        p *= 0.965
        trades.append(trade(t0 + i, p, buy=False))
    assert p <= peak * 0.50, "setup must constitute a ≥50% nuke"
    # momentum resumes: buys drive consecutive up-ticks with buy pressure
    for i in range(40, 50):
        p *= 1.01
        trades.append(trade(t0 + i, p, sol=3.0, buy=True))

    fire = replay_trap(spec(), trades)
    assert fire is not None, "trap must fire on nuke→momentum"
    assert fire["kind"] == "nuke_momentum"
    assert fire["size_sol"] == 0.05
    assert fire["trigger"]["drawdown"] >= 0.50
    assert fire["trigger"]["nuke_low"] < fire["trigger"]["peak_price"] * 0.5


def test_never_fires_without_nuke():
    t0 = time.time()
    trades = [trade(t0 + i, 1.0 * (1.01 ** i)) for i in range(60)]  # straight up
    assert replay_trap(spec(), trades) is None


def test_never_fires_on_falling_knife_alone():
    """Nuke without the turn → no fire (that's the whole point)."""
    t0 = time.time()
    p = 1.0
    trades = [trade(t0 + i, p) for i in range(10)]
    for i in range(10, 70):
        p *= 0.97
        trades.append(trade(t0 + i, p, buy=False))
    assert replay_trap(spec(), trades) is None


def test_aborts_beyond_authorized_drawdown():
    t0 = time.time()
    p = 1.0
    trades = [trade(t0 + i, p) for i in range(10)]
    for i in range(10, 60):
        p *= 0.94                      # way past the 75% authorization
        trades.append(trade(t0 + i, p, buy=False))
    fire = replay_trap(spec(max_drawdown_pct=0.75), trades)
    assert fire is None


def test_expires():
    tr = TrapMachine(spec(expires_in_s=900))
    tr.armed_ts = time.time() - 1000              # armed long ago
    tracker = TapeTracker(MINT, first_seen=time.time())
    fire = tr.on_trade(trade(time.time(), 1.0), tracker)
    assert fire is None and tr.state == "expired"


def test_mcap_floor_abort():
    t0 = time.time()
    p = 1.0
    trades = [trade(t0 + i, p, mcap_sol=30.0) for i in range(10)]
    for i in range(10, 40):
        p *= 0.96
        trades.append(trade(t0 + i, p, buy=False, mcap_sol=30.0 * (0.96 ** (i - 10))))
    fire = replay_trap(spec(min_mcap_sol=5.0), trades)
    assert fire is None or fire["trigger"]["mcap_sol"] >= 5.0


def test_trap_state_transitions_after_fire():
    tr = TrapMachine(spec())
    tracker = TapeTracker(MINT, first_seen=time.time())
    now = time.time()
    for i, pr in enumerate([1.0, 1.2, 1.4, 0.9, 0.5, 0.45, 0.44, 0.45, 0.46, 0.47, 0.48]):
        tr.on_trade(trade(now + i, pr, sol=2.0 if pr > 0.44 else 0.5), tracker)
        tracker.update(trade(now + i, pr, sol=2.0 if pr > 0.44 else 0.5))
    assert tr.state == "fired"
    # second fire must not happen
    assert tr.on_trade(trade(now + 99, 0.5), tracker) is None
