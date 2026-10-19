"""Tests for the market recalibrator (market_backtest/recalibrate_market.py).

Fast by design: no full-book runs.  The expensive parity checks are done
once on a single tape and the stored arm14 result.

Run:
    cd backend && ../backend/.venv/bin/python -m pytest \
        market_backtest/test_recalibrate.py -q
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from market_backtest import recalibrate_market as R
from market_backtest.runner import replay_candles


# ─────────────────────────────────────────────────────────────────────────
# statistics / gate
# ─────────────────────────────────────────────────────────────────────────

def test_bootstrap_ci_sign_and_width():
    xs = [0.1, 0.2, 0.0, 0.15, 0.05, 0.3]
    lo, hi = R._bootstrap_ci(xs, n=2000, seed=3)
    assert lo < sum(xs) / len(xs) < hi
    assert lo > 0


def test_paired_gate_passes_on_broad_positive_deltas():
    cfg = dict(R.DEFAULT_CFG)
    months = [0.02, 0.03, 0.01, 0.04, 0.02, 0.05, 0.01, 0.03, 0.02, 0.04,
              0.01, 0.02]
    syms = {f"S{i}": 0.01 for i in range(20)}
    g = R.paired_gate(months, syms, cfg)
    assert g["passed"] is True
    assert g["bootstrap_ci_symbol"][0] > 0
    assert g["breadth_all"] == 1.0


def test_paired_gate_fails_on_cross_sectional_noise():
    cfg = dict(R.DEFAULT_CFG)
    months = [0.05, -0.04, 0.03, -0.03, 0.02, -0.02, 0.01, -0.01, 0.0, 0.0]
    syms = {f"S{i}": (0.01 if i % 2 else -0.01) for i in range(20)}
    g = R.paired_gate(months, syms, cfg)
    assert g["passed"] is False
    assert g["breadth_all"] <= 0.5


def test_wilcoxon_requires_enough_units():
    assert math.isnan(R._wilcoxon([0.1, 0.2, 0.3])[1])


# ─────────────────────────────────────────────────────────────────────────
# calibration fixes
# ─────────────────────────────────────────────────────────────────────────

def test_forward_stats_drawdown_is_positive():
    import numpy as np
    # 40 up bars, then 20 down bars -> a real drawdown
    closes = np.concatenate([np.linspace(100, 140, 40), np.linspace(140, 100, 20)])
    highs = closes * 1.01
    runup, dd = R.forward_stats(closes, highs, window=5)
    assert math.isfinite(runup) and math.isfinite(dd)
    assert dd > 0.0, "drawdown must not be sign-flipped"


def test_sigma_gate_above_incumbent_floor():
    tape = R.ensure_bars("AAPL")
    p, audit = R.fixed_base_params(tape[:1200], "stocks", R.DEFAULT_CFG)
    assert p["v2_sigma_t_min"] > 1e-5
    assert audit["sigma_t_min"] == p["v2_sigma_t_min"]


def test_sigma_gate_is_causal():
    tape = R.ensure_bars("AAPL")
    prefix = tape[:1200]
    p1, _ = R.fixed_base_params(prefix, "stocks", R.DEFAULT_CFG)
    # mutate the FUTURE (not in prefix) and re-calibrate on the same prefix
    future = [dict(c) for c in prefix]
    future[-1]["close"] *= 3.0
    p2, _ = R.fixed_base_params(future, "stocks", R.DEFAULT_CFG)
    assert p1["v2_sigma_t_min"] == p2["v2_sigma_t_min"]


# ─────────────────────────────────────────────────────────────────────────
# replay parity with the campaign baseline
# ─────────────────────────────────────────────────────────────────────────

def test_replay_slice_matches_stored_arm14():
    ref_path = (Path(__file__).resolve().parent / "results" / "arm14" /
                "stocks_AAPL_1d_arm14.json")
    if not ref_path.exists():
        pytest.skip("arm14 AAPL result not present")
    ref = json.loads(ref_path.read_text())
    tape = R.ensure_bars("AAPL")
    rp = replay_candles("stocks", "AAPL", "1d", tape,
                        ref["engine_params"], force_close=True)
    assert rp["trades"] == ref["trades"]
    assert rp["stats"] == ref["stats"]


def test_window_pnl_marks_a_long_hold_at_BOTH_boundaries():
    """Regression: an open position entered before the window must have its
    start mark subtracted, not just its end mark added."""
    trade = {"entry_time": 10, "exit_time": 0, "entry_price": 90.0,
             "size_sol": 0.1, "pnl_sol": 0.0}
    rp = {"trades": [], "open_trade": trade}
    pnl = R.window_pnl(rp, start_ts=20, end_ts=30, start_price=100.0,
                       end_price=110.0)
    expect = 0.1 * (110.0 / 90.0 - 1.0) - 0.1 * (100.0 / 90.0 - 1.0)
    assert pnl == pytest.approx(expect)


def test_window_pnl_accounts_for_open_marks():
    # one open trade entered at 100, marked at 110 -> +0.1 * 10% = +0.01
    trade = {"entry_time": 10, "exit_time": 0, "entry_price": 100.0,
             "size_sol": 0.1, "pnl_sol": 0.0}
    rp = {"trades": [], "open_trade": trade}
    pnl = R.window_pnl(rp, start_ts=5, end_ts=20, start_price=100.0,
                       end_price=110.0)
    assert pnl == pytest.approx(0.01)


# ─────────────────────────────────────────────────────────────────────────
# determinism / immutability
# ─────────────────────────────────────────────────────────────────────────

def _tiny_cfg():
    cfg = dict(R.DEFAULT_CFG)
    cfg.update({"window_bars": 126, "warmup_bars": 260,
                "arm_floors": [14.0], "conf_grid": [0.79],
                "tau_scales": [1.0], "min_fit_trades": 0,
                "selection": "stats_only"})
    return cfg


def test_fixed_base_is_deterministic_and_does_not_mutate_cfg():
    tape = R.ensure_bars("MSFT")
    cfg = _tiny_cfg()
    before = json.dumps(cfg, sort_keys=True)
    p1, a1 = R.fixed_base_params(tape[:900], "stocks", cfg)
    p2, a2 = R.fixed_base_params(tape[:900], "stocks", cfg)
    assert p1 == p2 and a1 == a2
    assert json.dumps(cfg, sort_keys=True) == before


def test_snapshot_determinism_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RECAL_ROOT", tmp_path)
    cfg = _tiny_cfg()
    s1 = R.build_snapshot(["AAPL", "MSFT"], "2025-01-01", cfg,
                          tag="t", verbose=False, workers=1)
    s2 = R.build_snapshot(["AAPL", "MSFT"], "2025-01-01", cfg,
                          tag="t", verbose=False, workers=1)
    for k in ("params", "selection", "selection_pooled", "version"):
        assert s1[k] == s2[k]
    mf = json.loads((tmp_path / "t" / "manifest.json").read_text())
    assert mf["latest"].startswith("snapshot_2025-01-01")
    assert len(mf["versions"]) == 1, "second build must load, not append"


def test_frozen_base_stats_only_is_identity(tmp_path, monkeypatch):
    """The control cell: frozen base + stats_only == the campaign params."""
    monkeypatch.setattr(R, "RECAL_ROOT", tmp_path)
    cfg = _tiny_cfg()
    cfg["base_mode"] = "frozen"
    cfg["selection"] = "stats_only"
    snap = R.build_snapshot(["AAPL"], "2025-01-01", cfg, tag="t2",
                            verbose=False, workers=1)
    fr = R.frozen_params(["AAPL"])["AAPL"]
    for k in ("gain_retrace_arm_pct", "confidence_high", "tau_max",
              "v2_sigma_t_min", "breakeven_arm_dd_pct", "no_long_offside_pct"):
        assert snap["params"]["AAPL"][k] == fr[k]


def test_frozen_params_are_not_mutated_by_candidates(tmp_path):
    frozen = R.frozen_params(["AAPL", "MSFT"])
    before = json.dumps(frozen, sort_keys=True)
    base = dict(next(iter(frozen.values())))
    R.apply_candidate(base, {"arm": 3.0, "conf": 0.79, "tau_scale": 1.0},
                      R.DEFAULT_CFG)
    assert json.dumps(frozen, sort_keys=True) == before


# ─────────────────────────────────────────────────────────────────────────
# live adoption semantics
# ─────────────────────────────────────────────────────────────────────────

def test_live_snapshot_requires_manual_adoption(tmp_path):
    from market_live.trader import LiveConfig, MarketLiveTrader

    snap = {"version": "v-test", "asof": "2025-01-01",
            "params": {"AAPL": {"gain_retrace_arm_pct": 14.0}}}
    f = tmp_path / "snapshot_2025-01-01_abc.json"
    f.write_text(json.dumps(snap))
    (tmp_path / "manifest.json").write_text(json.dumps({"latest": f.name,
                                                        "versions": []}))

    cfg = LiveConfig(symbols=("AAPL",), calibration_snapshot=str(tmp_path))
    tr = MarketLiveTrader(cfg, journal=None, verbose=False)
    params, meta = tr._load_calibration_snapshot()
    assert params == {} and "adopted.json" in meta.get("error", "")

    (tmp_path / "adopted.json").write_text(json.dumps(
        {"file": f.name, "adopted_at": 1.0, "adopted_by": "test"}))
    params, meta = tr._load_calibration_snapshot()
    assert params["AAPL"]["gain_retrace_arm_pct"] == 14.0
    assert meta["version"] == "v-test"


def test_snapshot_params_are_authoritative_over_config_overrides(tmp_path):
    from market_live.trader import LiveConfig, MarketLiveTrader
    cfg = LiveConfig(symbols=("AAPL", "MSFT"),
                     calibration_snapshot=str(tmp_path / "x.json"),
                     overrides={"gain_retrace_arm_pct": 3.0})
    tr = MarketLiveTrader(cfg, journal=None, verbose=False)
    tr._frozen = {"AAPL": {"gain_retrace_arm_pct": 20.0},
                  "MSFT": {"gain_retrace_arm_pct": 8.0}}
    tr._snap_symbols = {"AAPL"}
    assert tr._resolve_params("AAPL", [])["gain_retrace_arm_pct"] == 20.0
    assert tr._resolve_params("MSFT", [])["gain_retrace_arm_pct"] == 3.0


def test_live_snapshot_file_path_is_explicit_adoption(tmp_path):
    from market_live.trader import LiveConfig, MarketLiveTrader
    snap = {"version": "v2", "asof": "2025-01-01",
            "params": {"AAPL": {"confidence_high": 0.79}}}
    f = tmp_path / "s.json"
    f.write_text(json.dumps(snap))
    cfg = LiveConfig(symbols=("AAPL",), calibration_snapshot=str(f))
    tr = MarketLiveTrader(cfg, journal=None, verbose=False)
    params, meta = tr._load_calibration_snapshot()
    assert params["AAPL"]["confidence_high"] == 0.79
    assert meta["source"] == str(f)
