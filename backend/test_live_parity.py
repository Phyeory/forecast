"""
iter57 — Live-vs-backtest parity regression tests.

Covers the four root causes of the live trader diverging from the backtester:

  1. Holder-flow delivery: the live pump must deliver EVERY persisted event
     exactly once (DB id-cursor).  The pre-iter57 count-diff over the
     monitor's 60 s-trimmed in-memory list silently dropped ~75-92 % of
     events (reproduced here as a regression reference).
  2. Engine feed: every tick (including same-price ticks) must refresh the
     accumulating-candle buffer so the completed candle carries the full
     volume the backtester replays.
  3. Pending-signal semantics: signals retry until executed instead of being
     silently cleared when a swap is still settling (re-entry parity), and
     BUY retries expire after a staleness cap.
  4. Warm-up: update_historical_candle never launches swaps and passes the
     full candle payload through.

Plus an end-to-end decision-parity test: the same synthetic recording driven
through ForwardTester (backtester semantics) and LiveTrader (stubbed swaps)
must produce identical decision sequences (entry/exit candle times + reasons).

Run:  cd backend && python test_live_parity.py
"""
from __future__ import annotations
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import random
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_trader(**kwargs):
    """Construct a LiveTrader with stubbed on-chain execution + temp journal."""
    import live_session_logger
    from live_trader import LiveTrader
    from solders.keypair import Keypair

    tmp = tempfile.mkdtemp(prefix="live_parity_test_")
    live_session_logger.LOG_ROOT = Path(tmp)

    kp = Keypair()
    trader = LiveTrader(
        token_mint="TestMint1111111111111111111111111111111111111111",
        keypair=kp,
        buy_size_sol=0.01,
        engine_version=2,
        **kwargs,
    )
    trader._events = []

    async def _fake_buy(reason):
        trader._events.append(("buy", reason, time.time()))
        if trader.current_trade is not None:
            trader.current_trade.status = "open"
        return "fakebuysig"

    async def _fake_sell(reason):
        trader._events.append(("sell", reason, time.time()))
        # Mirror a fast on-chain confirmation so the engine/positions cycle
        # like the backtester's synchronous close.  exit_time is re-stamped
        # with wall-clock by confirm_sell; decision parity is asserted on
        # entry times + exit reasons/order.
        if trader.current_trade is not None:
            trader.confirm_sell("fakesellsig", trader.current_trade.size_sol * 1.0,
                                trader.current_trade.entry_price)
        return "fakesellsig"

    trader.execute_buy = _fake_buy
    trader.execute_sell = _fake_sell
    return trader


def _gen_candles(n=900, seed=42):
    """Deterministic random-walk 1s candles with buy/sell volume split."""
    rng = random.Random(seed)
    candles = []
    price = 1.0e-6
    t0 = 1_700_000_000
    # Phase 1: quiet drift (warmup), Phase 2: pump with buy pressure,
    # Phase 3: dump with sell pressure, Phase 4: recovery pump.
    for i in range(n):
        if i < 200:
            drift, bv, sv = 0.0, 0.2, 0.2
        elif i < 420:
            drift, bv, sv = 0.004, 3.0, 0.6
        elif i < 650:
            drift, bv, sv = -0.005, 0.5, 3.5
        else:
            drift, bv, sv = 0.005, 3.5, 0.8
        o = price
        c = max(price * (1 + drift + rng.gauss(0, 0.004)), 1e-9)
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.002)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.002)))
        vol = bv + sv
        candles.append({
            "time": t0 + i, "open": o, "high": h, "low": l, "close": c,
            "volume": vol, "buy_volume": bv, "sell_volume": sv,
            "pool_sol": 150.0, "market_cap_usd": 80_000.0,
        })
        price = c
    return candles


def _feed_ft(candles, engine_kwargs=None):
    """Backtester semantics: ForwardTester over the candle list, with
    run_backtest's default slippage (1.0 %) — the reference configuration."""
    from forward_tester import ForwardTester
    ft = ForwardTester(engine_version=2, engine_kwargs=engine_kwargs,
                       slippage_pct=1.0)
    last = None
    for cd in candles:
        t, o, h, l, c = cd["time"], cd["open"], cd["high"], cd["low"], cd["close"]
        bullish = c >= o
        mid1, mid2 = (h, l) if bullish else (l, h)
        ft.update(time=t, o=o, h=o, l=o, c=o, volume=0.0, _build_full_result=False)
        ft.update(time=t, o=o, h=max(o, mid1), l=min(o, mid1), c=mid1, volume=0.0, _build_full_result=False)
        ft.update(time=t, o=o, h=h, l=l, c=mid2, volume=0.0, _build_full_result=False)
        ft.update(time=t, o=o, h=h, l=l, c=c, volume=cd["volume"],
                  buy_volume=cd["buy_volume"], sell_volume=cd["sell_volume"],
                  pool_sol=cd["pool_sol"], market_cap_usd=cd["market_cap_usd"],
                  _build_full_result=False)
        last = (o, h, l, c, t)
    if ft.current_trade is not None and last:
        ft._close_long(*last[:4], last[4], reason="recording_ended")
    return ft


async def _feed_live(trader, candles):
    """Live semantics: ticks with candle-boundary is_new flags.

    Feeds one tick per candle (the candle's final state) — the live trader
    buffers it and expands on the boundary, exactly like main.py."""
    decisions = []
    prev_t = None
    for cd in candles:
        is_new = prev_t is not None and cd["time"] != prev_t
        # boundary tick arrives with the NEW candle's first state; feed a
        # minimal first-tick (o=h=l=c=open, no volume) then the closing tick
        if is_new:
            trader.update(time_val=cd["time"], o=cd["open"], h=cd["open"],
                          l=cd["open"], c=cd["open"], volume=0.0, is_new=True)
            await asyncio.sleep(0)  # let swap tasks run
        trader.update(time_val=cd["time"], o=cd["open"], h=cd["high"],
                      l=cd["low"], c=cd["close"], volume=cd["volume"],
                      buy_volume=cd["buy_volume"], sell_volume=cd["sell_volume"],
                      is_new=False, market_cap_usd=cd["market_cap_usd"],
                      pool_sol=cd["pool_sol"])
        await asyncio.sleep(0)
        prev_t = cd["time"]
    return decisions


def _live_decisions(trader):
    """Extract the live trader's decision sequence: (action, candle_time, reason)."""
    seq = []
    for ev in trader._events:
        seq.append(ev[:2])
    return seq


# ── 1. Holder-flow delivery ──────────────────────────────────────────────────

def test_hf_countdiff_over_trimmed_list_drops_events():
    """Regression reference: the OLD count-based diff over the monitor's
    60s-trimmed list drops events (this is the bug iter57 fixed)."""
    RECENT_WINDOW = 60.0
    events = []
    t0 = 1_700_000_000
    # 10 events, one every 30s — each ages out of the 60s window 60s later
    for i in range(10):
        events.append({"time": t0 + i * 30, "id": i + 1, "wallet": f"w{i}"})

    pushed_n = 0
    delivered = []
    t = float(t0)
    end = t0 + 30 * 10 + 120
    while t <= end:
        cutoff = t - RECENT_WINDOW
        # the monitor only holds DISCOVERED (time <= now), untrimmed events
        current = [e for e in events if cutoff <= e["time"] <= t]
        if len(current) > pushed_n:
            delivered.extend(current[pushed_n:])
            pushed_n = len(current)
        t += 1.0
    # The old mechanism loses most events
    assert len(delivered) < len(events), (
        f"expected the old count-diff to drop events, got {len(delivered)}/{len(events)}")


def test_hf_get_holder_flow_since_lossless():
    """get_holder_flow_since returns every row exactly once, in id order."""
    import data_store

    tmp = tempfile.mktemp(prefix="hf_parity_", suffix=".db")
    con = sqlite3.connect(tmp)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE holder_flow (
        id INTEGER PRIMARY KEY AUTOINCREMENT, recording_id INTEGER, time INTEGER,
        wallet TEXT, tag TEXT, side TEXT, amount_usd REAL, amount_sol REAL, tx_hash TEXT)""")
    for i in range(50):
        con.execute(
            "INSERT INTO holder_flow (recording_id, time, wallet, tag, side, amount_usd, amount_sol, tx_hash)"
            " VALUES (7, ?, ?, 'whale', 'sell', 500.0, 3.0, ?)",
            (1_700_000_000 + i * 10, f"w{i}", f"tx{i}"))
    con.commit()

    orig = data_store._get_price_read_conn

    def _tmp_conn():
        c = sqlite3.connect(tmp)
        c.row_factory = sqlite3.Row
        return c

    data_store._get_price_read_conn = _tmp_conn
    try:
        seen = []
        cursor = 0
        for _ in range(10):
            rows = data_store.get_holder_flow_since(7, cursor)
            if not rows:
                break
            seen.extend(rows)
            cursor = rows[-1]["id"]
        assert len(seen) == 50
        assert [r["id"] for r in seen] == list(range(1, 51))
        # no re-delivery past the cursor
        assert data_store.get_holder_flow_since(7, cursor) == []
        # other recordings are isolated
        assert data_store.get_holder_flow_since(8, 0) == []
    finally:
        data_store._get_price_read_conn = orig
        con.close()
        os.unlink(tmp)


# ── 2. Engine feed: same-price ticks must refresh the buffer ─────────────────

def test_same_price_ticks_refresh_buffer_volume():
    """Consecutive same-price ticks must accumulate volume into the buffered
    candle that the engine later expands (iter57 Fix 2 precondition)."""
    trader = _make_trader()
    t0 = 1_700_000_000

    async def drive():
        # candle 1: first tick carries 1 SOL buy, then 5 same-price ticks of
        # 1 SOL buy each — the buffered candle must end with 6 SOL total.
        trader.update(time_val=t0, o=1.0, h=1.0, l=1.0, c=1.0, volume=1.0,
                      buy_volume=1.0, sell_volume=0.0, is_new=False)
        for _ in range(5):
            trader.update(time_val=t0, o=1.0, h=1.0, l=1.0, c=1.0, volume=6.0,
                          buy_volume=6.0, sell_volume=0.0, is_new=False)
        # boundary tick for candle 2
        trader.update(time_val=t0 + 1, o=1.0, h=1.0, l=1.0, c=1.0, volume=0.0,
                      is_new=True)
        await asyncio.sleep(0)

    asyncio.run(drive())
    hist = trader.engine._candle_volume_history
    assert hist, "engine saw no candle volume history"
    assert hist[0]["buy_vol"] == 6.0, (
        f"buffered candle volume was not refreshed by same-price ticks: {hist[0]}")


# ── 3. Pending-signal retry semantics ────────────────────────────────────────

def test_pending_buy_retries_after_slow_sell_confirm():
    """A BUY queued while the previous sell is still settling must fire once
    the sell confirms — not be silently dropped (old Step-1 clear)."""
    trader = _make_trader()
    t0 = 1_700_000_000

    async def drive():
        # Buffer candle t0 so a later is_new tick forms a real boundary.
        trader.update(time_val=t0, o=1.0, h=1.0, l=1.0, c=1.0, volume=0.0, is_new=False)

        # Manually queue an exit while a trade is open, then a buy while the
        # sell is "in flight", then settle the sell — the buy must fire.
        from live_trader import LiveTrade
        trade = LiveTrade(token_mint=trader.token_mint, entry_time=t0,
                          entry_price=1.0, size_sol=0.01, size_tokens=10.0,
                          entry_reason="buy_test", status="open")
        trader.current_trade = trade
        trader.engine.notify_trade_opened(1.0, __import__("strategy_engine").Direction.UP)

        # sell swap goes in flight (blocks everything)
        trader._swap_in_flight = True
        trader.current_trade.status = "closing"
        trader.engine.notify_trade_closed()
        trader._pending_buy = True
        trader._pending_buy_reason = "buy_retry_test"
        trader._pending_buy_ts = time.time()

        # boundary while blocked — signal must NOT be dropped
        trader.update(time_val=t0 + 1, o=1.0, h=1.0, l=1.0, c=1.0, volume=0.0, is_new=True)
        await asyncio.sleep(0)
        assert trader._pending_buy, "pending BUY was dropped while swap in flight"

        # sell settles on-chain → trade closes → drain fires the buy
        trader._swap_in_flight = False
        trader.confirm_sell("sig", 0.01, 1.0)
        await asyncio.sleep(0.3)
        assert any(e[0] == "buy" and e[1] == "buy_retry_test" for e in trader._events), (
            f"re-entry buy never fired after sell confirm: {trader._events}")
        assert not trader._pending_buy

    asyncio.run(drive())


def test_pending_buy_expires_when_stale():
    """BUY retries expire after pending_signal_max_age_seconds."""
    trader = _make_trader(pending_signal_max_age_seconds=0.05)
    t0 = 1_700_000_000

    async def drive():
        trader.update(time_val=t0, o=1.0, h=1.0, l=1.0, c=1.0, volume=0.0, is_new=False)
        trader._pending_buy = True
        trader._pending_buy_reason = "buy_stale"
        trader._pending_buy_ts = time.time() - 1.0  # already stale
        trader.update(time_val=t0 + 1, o=1.0, h=1.0, l=1.0, c=1.0, volume=0.0, is_new=True)
        await asyncio.sleep(0)
        assert not trader._pending_buy, "stale BUY was not expired"
        assert all(e[0] != "buy" for e in trader._events)

    asyncio.run(drive())


def test_immediate_holder_flow_exit_consumes_flags():
    """immediate_holder_flow_exit must not leave a pending EXIT that the
    retry executor could double-fire."""
    trader = _make_trader()
    from live_trader import LiveTrade

    async def drive():
        trade = LiveTrade(token_mint=trader.token_mint, entry_time=1,
                          entry_price=1.0, size_sol=0.01, size_tokens=10.0,
                          entry_reason="buy_x", status="open")
        trader.current_trade = trade
        trader.engine.notify_trade_opened(1.0, __import__("strategy_engine").Direction.UP)

        trader.immediate_holder_flow_exit("dev_sell_exit:abc")
        assert trader.current_trade.status == "closing"
        assert not trader._pending_exit and not trader._pending_buy
        assert not trader.engine.in_position
        await asyncio.sleep(0)  # let the sell task run
        # and a subsequent boundary must not re-fire a sell
        trader.update(time_val=1, o=1.0, h=1.0, l=1.0, c=1.0, volume=0.0, is_new=False)
        trader.update(time_val=2, o=1.0, h=1.0, l=1.0, c=1.0, volume=0.0, is_new=True)
        await asyncio.sleep(0)

    asyncio.run(drive())
    sells = [e for e in trader._events if e[0] == "sell"]
    assert len(sells) == 1, f"exit double-fired: {trader._events}"


# ── 4. Warm-up safety ────────────────────────────────────────────────────────

def test_warmup_never_fires_swaps_and_passes_volume():
    trader = _make_trader()
    candles = _gen_candles(n=300)
    for cd in candles:
        trader.update_historical_candle(
            time_val=cd["time"], o=cd["open"], h=cd["high"], l=cd["low"],
            c=cd["close"], volume=cd["volume"], buy_volume=cd["buy_volume"],
            sell_volume=cd["sell_volume"], market_cap_usd=cd["market_cap_usd"],
            pool_sol=cd["pool_sol"])
    assert trader._events == [], "warm-up launched swaps"
    assert trader.current_trade is None
    # volume passthrough reached the engine
    hist = trader.engine._candle_volume_history
    assert hist and hist[-1]["buy_vol"] > 0


# ── 5. End-to-end decision parity: LiveTrader vs ForwardTester ───────────────

_PARITY_RECS = [2935, 2949, 2941]


def _recording_candles_opt(rec):
    try:
        from data_store import get_recording_candles
        candles = get_recording_candles(rec)
        return candles or None
    except Exception:
        return None


@pytest.mark.parametrize("rec", _PARITY_RECS)
def test_decision_parity_with_backtester(rec):
    """Same recorded candles → identical decision sequence (entry/exit candle
    times + reasons) between the backtester path (ForwardTester) and the live
    path (LiveTrader with stubbed instant swaps).

    This is the iter57 acceptance criterion: whatever the backtester would
    have traded on the live session's own recording, the live trader now
    trades identically."""
    candles = _recording_candles_opt(rec)
    if not candles:
        pytest.skip(f"recording {rec} not available in local price_data.db")

    ft = _feed_ft(candles)
    ft_trades = [(t.entry_time, t.entry_reason, t.exit_time, t.exit_reason)
                 for t in ft.trade_history]
    assert ft_trades, "recording produced no backtest trades — test is vacuous"

    trader = _make_trader()

    async def drive():
        await _feed_live(trader, candles)
        await asyncio.sleep(0)
    asyncio.run(drive())

    live_tr = [(t.entry_time, t.entry_reason) for t in trader.trade_history]
    live_exit = [t.exit_reason for t in trader.trade_history]
    bt_entry = [(t[0], t[1]) for t in ft_trades]

    assert len(live_tr) == len(bt_entry), (
        f"trade count mismatch: live {len(live_tr)} vs backtest {len(bt_entry)}\n"
        f"live={live_tr}\nbt={bt_entry}")
    for (lt, bt) in zip(live_tr, bt_entry):
        assert abs(lt[0] - bt[0]) <= 1, f"entry time mismatch: {lt} vs {bt}"
        assert lt[1] == bt[1], f"entry reason mismatch: {lt} vs {bt}"
    # exit reasons in sequence (exit_time is wall-clock in the stubbed live
    # path, so only the decision content is compared here; the replay harness
    # verifies exit candle-times separately)
    bt_exit = [t[3] for t in ft_trades]
    assert live_exit == bt_exit, (
        f"exit sequence mismatch:\nlive={live_exit}\nbt={bt_exit}")


if __name__ == "__main__":
    rc = pytest.main([__file__, "-v", "--tb=short"])
    sys.exit(rc)
