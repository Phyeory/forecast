"""test_prop_guard.py — the guard must enforce the firm's arithmetic exactly.

Run: cd backend && ./.venv/bin/python -m market_live.test_prop_guard
"""

from __future__ import annotations

import sys

from market_live.prop_guard import PropGuard, TEMPLATES


def _silent(_msg: str) -> None:
    pass


def test_daily_breach() -> None:
    g = PropGuard("ftmo_2step", 100_000, log=_silent)
    g.start_day(100_000, "d1")
    assert g.on_equity(96_000) == "ok"          # -4%, inside 5%
    g.close_day(96_000, "d1")
    g.start_day(96_000, "d2")
    assert g.on_equity(91_100) == "fail_daily"  # -5.1% of 96k
    assert g.status()["halted"]


def test_static_max_loss() -> None:
    g = PropGuard("ftmo_2step", 100_000, log=_silent)
    g.start_day(100_000, "d1")
    assert g.on_equity(96_000) == "ok"           # -4% day
    g.close_day(96_000, "d1")
    g.start_day(96_000, "d2")
    assert g.on_equity(92_000) == "ok"           # -4.2% day, total -8%
    g.close_day(92_000, "d2")
    g.start_day(92_000, "d3")
    # -11% total but only -3.8% on the day: the STATIC floor (90k) is what trips
    assert g.on_equity(89_000) == "fail_total"


def test_target_and_phases() -> None:
    g = PropGuard("ftmo_2step", 100_000, log=_silent)
    g.t["consistency"] = None          # isolate phase arithmetic
    g.start_day(100_000, "d1")
    assert g.on_equity(110_500) == "phase_pass"   # P1 at +10%
    assert g.s.phase == 1
    # phase 2 rebases to 110.5k, target +5% => 116.0k
    assert g.on_equity(116_100) == "pass"
    assert g.s.passed


def test_consistency_blocks_payout() -> None:
    g = PropGuard("ftmo_2step", 100_000, log=_silent)
    g.start_day(100_000, "d1")
    # one +12% day: target hit, but best day = 100% of total profit
    assert g.on_equity(112_000) == "consistency_hold"
    g.close_day(112_000, "d1")
    g.start_day(112_000, "d2")
    # +6k more: best day is still 66.7% of the 18k total -> hold
    assert g.on_equity(118_000) == "consistency_hold"
    g.close_day(118_000, "d2")
    g.start_day(118_000, "d3")
    # +6k more: 12k best / 24k total = 50% -> allowed
    assert g.on_equity(124_000) == "phase_pass"


def test_trailing_floor() -> None:
    trail = PropGuard("apex_funded", 100_000, log=_silent)   # trailing 4%
    trail.start_day(100_000, "d1")
    assert trail.on_equity(120_000) == "ok"
    trail.close_day(120_000, "d1")
    trail.start_day(120_000, "d2")
    # trailing floor = 120k * 0.96 = 115.2k, so 115.0k breaches
    assert trail.on_equity(115_000) == "fail_total"


def test_can_open_buffer() -> None:
    g = PropGuard("ftmo_2step", 100_000, log=_silent)
    g.start_day(100_000, "d1")
    g.on_equity(95_300)                     # 4.7% down: 0.5% buffer => blocked
    ok, why = g.can_open(95_300)
    assert not ok and "daily_floor" in why


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
        except AssertionError as exc:
            fails += 1
            print(f"  FAIL {t.__name__}: {exc}")
    print(f"\n[prop_guard] {len(tests) - fails}/{len(tests)} passed")
    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
