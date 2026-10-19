"""recalibrate_market.py — adaptive, causal recalibrator for the V2M stock stack.

Why this file exists
--------------------
The campaign's calibration was a ONE-SHOT fit on the 2016-2023 train prefix
plus a constant `gain_retrace_arm_pct=14.0` override (`arm14`).  That is a
static policy.  This module builds the *calibrator as a first-class
algorithm*: at any as-of date it re-derives per-symbol engine parameters from
a trailing window, searches a small set of behaviour knobs on an in-window
fit segment, gates the choice on an untouched holdout, and writes a versioned
snapshot.  A separate retrospective replayer measures whether the adaptive
policy beats the frozen one out of sample.

Two silent bugs in the incumbent calibrator are fixed HERE (not in
`market_calibration.py`, so the campaign baseline stays byte-comparable):

1. `v2_sigma_t_min` — the incumbent's sigma-gate replay reads
   `p["warmup_bars"]` before it is assigned, raises KeyError, and the blanket
   `except` returns 1e-6.  Every campaign tape therefore ran with the entry
   noise gate effectively OFF.  Reproduced: all 67 arm14 result files carry
   `v2_sigma_t_min == 1e-06`.
2. `_forward_runup_dd` — returns `-percentile(drawdowns, 40)` where the
   drawdown list is already non-negative and >60% zeros on a bull tape.  All
   67 tapes report `dd_med_pct == -0.0`; `max(dd_med, 1.0)` then pins
   `breakeven_arm_dd_pct=15`, `no_long_offside_pct=12` and
   `v2_evr_offside_min_pct=8` at their floors on every symbol.

Everything else is the campaign's causal discipline:
  * calibration sees only candles strictly before the as-of timestamp;
  * the fit segment is the first (1-holdout) of the trailing window; the
    holdout is the most recent holdout fraction and is NEVER used for
    selection;
  * candidate overrides are constant knobs on top of the per-symbol base —
    no new engine code paths.

CLI
---
    # one snapshot
    python -m market_backtest.recalibrate_market snapshot --asof 2025-07-01

    # retrospective: monthly snapshots + forward OOS path vs frozen arm14
    python -m market_backtest.recalibrate_market replay --label recal252 \
        --start 2024-01-01 --end 2025-07-01 --window 252 --holdout 0.2

    # calibrator-knob sweep (panel + quarterly dates, cheap screening)
    python -m market_backtest.recalibrate_market sweep --panel 23 \
        --start 2024-01-01 --end 2025-07-01 --cadence quarterly

    # full-book snapshot batch in runner format (for mc_firms / live)
    python -m market_backtest.recalibrate_market batch --snapshot <file> \
        --label recal_arm14 --split 2023-10-16
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import json
import math
import os
import random
import subprocess
import time
from bisect import bisect_left
from pathlib import Path
from statistics import median
from typing import Iterable, Optional

import numpy as np

from .market_calibration import (
    BAR_SPEC,
    calibrate_market_params,
)
from .market_store import (
    get_market_recording,
    get_market_recording_candles,
    list_market_recordings,
)
from .runner import COSTS, market_kind, replay_candles

RESULTS_ROOT = Path(__file__).resolve().parent / "results"
RECAL_ROOT = RESULTS_ROOT / "recal"
SECONDS_DAY = 86400.0

# ─────────────────────────────────────────────────────────────────────────
# calibrator defaults (all knobs are explicit; cells must set them all)
# ─────────────────────────────────────────────────────────────────────────
DEFAULT_CFG: dict = {
    "window_bars": 252,          # trailing calibration/selection window
    "holdout_frac": 0.20,        # most-recent share, never used for selection
    "base_lookback_bars": 505,   # causal estimator lookback (>=300 required)
    # 0 = warm on ALL history before the scored window.  The engine has long
    # memory (KDE 375 bars + OU/RBPF state); a 260-bar warm start silently
    # suppresses entries for quarters at a time.  The live host warms on the
    # full feed history, so full-history warm is the decision-identical
    # choice.  Costs ~5.5 s per symbol/window — the price of parity.
    "warmup_bars": 0,
    "min_fit_trades": 8,         # pooled trades required to trust a cell
    "holdout_tolerance": -0.02,  # account-unit degradation allowed on holdout
    "selection": "pooled",       # pooled | per_symbol | stats_only
    # base stats: "trailing" = re-derive the causal statistical layers from
    # the last 505 bars; "frozen" = keep the campaign's calibrated stats and
    # only search the behaviour knob.  The 2x2 (base x search) separates
    # "refresh" damage from "search" value.
    "base_mode": "trailing",     # trailing | frozen
    # Search surface = the arm ladder only.  conf 0.84/0.90/0.92 and tau
    # variants were already falsified by the campaign's 146-cell sweep; the
    # adaptive question is which arm dose the trailing window supports.
    "arm_floors": [3.0, 14.0, 20.0],
    "conf_grid": [0.79],
    "tau_scales": [1.0],
    "bootstrap_n": 10000,
    "gate_p": 0.05,
    "breadth_min": 0.50,
    "seed": 20261019,
    "sigma_gate_fix": 1.0,       # 1 = corrected causal gate, 0 = incumbent 1e-6
    # The corrected drawdown ladder is measured WORSE (-0.0415 test Σ at the
    # campaign split), so it is available but NOT adopted.  Fix the code,
    # keep the campaign floors.
    "dd_fix": 0.0,
}

# Candidate overlays.  `arm` is the arm ladder's LOWER clip (the campaign's
# finding was that the formula's 3.0 floor exits winners at the first
# pullback); `conf` is the 4-pillar entry confidence floor; `tau_scale`
# multiplies the interval-class tau_max (40d -> 60d at 1.5).
CANDIDATES: list[dict] = [
    {"name": "arm3",        "arm": 3.0,  "conf": 0.79, "tau_scale": 1.0},
    {"name": "arm8",        "arm": 8.0,  "conf": 0.79, "tau_scale": 1.0},
    {"name": "arm14",       "arm": 14.0, "conf": 0.79, "tau_scale": 1.0},
    {"name": "arm14_c84",   "arm": 14.0, "conf": 0.84, "tau_scale": 1.0},
    {"name": "arm14_t60",   "arm": 14.0, "conf": 0.79, "tau_scale": 1.5},
    {"name": "arm20",       "arm": 20.0, "conf": 0.79, "tau_scale": 1.0},
]


# ─────────────────────────────────────────────────────────────────────────
# time / tape helpers
# ─────────────────────────────────────────────────────────────────────────

def iso_to_ts(s: str) -> int:
    """'YYYY-MM-DD' -> unix midnight UTC."""
    import datetime as dt
    d = dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def ts_to_iso(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(int(t)))


def asof_index(candles: list[dict], asof_ts: int) -> int:
    """Index of the first bar at/after asof (bars strictly before are seen)."""
    times = [int(c["time"]) for c in candles]
    return bisect_left(times, int(asof_ts))


def month_starts(start: str, end: str) -> list[str]:
    """First calendar day of each month in [start, end)."""
    import datetime as dt
    a = dt.date.fromisoformat(start)
    b = dt.date.fromisoformat(end)
    out = []
    y, m = a.year, a.month
    while (y, m) < (b.year, b.month):
        out.append(dt.date(y, m, 1).isoformat())
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def quarter_starts(start: str, end: str) -> list[str]:
    import datetime as dt
    a = dt.date.fromisoformat(start)
    b = dt.date.fromisoformat(end)
    out = []
    y, m = a.year, ((a.month - 1) // 3) * 3 + 1
    while (y, m) < (b.year, b.month):
        out.append(dt.date(y, m, 1).isoformat())
        m += 3
        if m > 12:
            y, m = y + 1, 1
    return out


def next_refresh(dates: list[str], d: str) -> Optional[str]:
    for x in dates:
        if x > d:
            return x
    return None


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"],
                              cwd=Path(__file__).resolve().parents[2],
                              capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


def _tape(symbol: str) -> list[dict]:
    rec = get_market_recording("stocks", symbol, "1d")
    if not rec:
        raise ValueError(f"no stocks/1d tape for {symbol}")
    return get_market_recording_candles(rec["id"])


def stock_symbols() -> list[str]:
    return [r["symbol"] for r in list_market_recordings("stocks")
            if r["interval"] == "1d" and r["status"] == "completed"]


def ensure_bars(symbol: str) -> list[dict]:
    """Worker-local tape cache (one DB read per symbol per process)."""
    cache = ensure_bars.__dict__.setdefault("_cache", {})
    if symbol not in cache:
        cache[symbol] = _tape(symbol)
    return cache[symbol]


def _frozen_one(symbol: str) -> dict:
    """Worker-local copy of the campaign's frozen arm14 params."""
    cache = ensure_bars.__dict__.setdefault("_frozen_params", {})
    if symbol not in cache:
        cache[symbol] = frozen_params([symbol])[symbol]
    return dict(cache[symbol])


# ─────────────────────────────────────────────────────────────────────────
# corrected calibration layers
# ─────────────────────────────────────────────────────────────────────────

def forward_stats(closes: np.ndarray, highs: np.ndarray,
                  window: int = 5) -> tuple[float, float]:
    """Median forward max-up (%) and a POSITIVE typical drawdown (%).

    The incumbent returned the negative of a clipped non-negative percentile
    (always ~0 on a bull tape).  Here the drawdown side is the 75th
    percentile of the loss magnitude, which is positive by construction.
    """
    n = len(closes)
    if n < window + 20:
        return (float("nan"), float("nan"))
    m = n - window
    # rolling forward max without a scipy dependency
    fmax = np.empty(m, dtype=float)
    for i in range(m):
        fmax[i] = float(highs[i + 1:i + 1 + window].max())
    run = (fmax / closes[:m] - 1.0) * 100.0
    ret = (closes[window:] / closes[:m] - 1.0) * 100.0
    loss = np.maximum(0.0, -ret)
    return (float(np.median(run)), float(np.percentile(loss, 75)))


def sigma_gate(prefix: list[dict], params: dict) -> float:
    """Causal entry noise gate = 0.35 x p10(posterior sigma_t).

    Byte-for-byte the incumbent's replay, with the `warmup_bars` KeyError
    fixed (the value is read from the params dict instead of an unassigned
    key).  Bounded to the last 3000 bars exactly as the incumbent.
    """
    replay = prefix[-3000:]
    warm = int(params.get("warmup_bars", 800))
    try:
        from .strategy_engineV2M import StrategyEngineV2MAdapter
        eng = StrategyEngineV2MAdapter(**params)
        sig: list[float] = []
        for i, cand in enumerate(replay):
            t0 = int(cand["time"])
            o = float(cand["open"]); hi = float(cand["high"])
            lo = float(cand["low"]); cl = float(cand["close"])
            vv = float(cand.get("volume", 0.0))
            bv = float(cand.get("buy_volume", 0.0))
            sv = float(cand.get("sell_volume", 0.0))
            bull = cl >= o
            mf, ms = (hi, lo) if bull else (lo, hi)
            eng.update(time=t0, o=o, h=o, l=o, c=o, volume=0.0,
                       _build_full_result=False)
            eng.update(time=t0, o=o, h=max(o, mf), l=min(o, mf), c=mf,
                       volume=0.0, _build_full_result=False)
            eng.update(time=t0, o=o, h=hi, l=lo, c=ms, volume=0.0,
                       _build_full_result=False)
            eng.update(time=t0, o=o, h=hi, l=lo, c=cl, volume=vv,
                       buy_volume=bv, sell_volume=sv,
                       _build_full_result=False)
            if (i + 1) * 4 > warm:
                # NOTE: the posterior sigma lives on the engine CORE.  The
                # incumbent read `eng._last_sigma_t` on the ADAPTER (always
                # None), which is the second half of the silent-gate bug.
                core = getattr(eng, "core", eng)
                st = getattr(core, "_last_sigma_t", None)
                if st is not None and math.isfinite(st):
                    sig.append(float(st))
        if len(sig) >= 50:
            return round(max(0.35 * float(np.percentile(sig, 10)), 1e-6), 8)
    except Exception:
        pass
    return 1e-6


def fixed_base_params(prefix: list[dict], kind: str,
                      cfg: dict) -> tuple[dict, dict]:
    """Per-symbol causal calibration with the two silent layers repaired."""
    n_look = int(cfg["base_lookback_bars"])
    look = prefix if n_look <= 0 else prefix[-n_look:]
    if len(look) < 300:
        raise ValueError(f"base lookback too thin: {len(look)}")
    calib = calibrate_market_params(look, kind, len(look))
    p = dict(calib["engine_params"])

    closes = np.array([c["close"] for c in look], dtype=float)
    highs = np.array([c["high"] for c in look], dtype=float)
    runup, dd = forward_stats(closes, highs,
                              window=10 if "1d" not in kind else 5)

    if cfg.get("sigma_gate_fix", 1):
        p["v2_sigma_t_min"] = sigma_gate(look, p)
    if cfg.get("dd_fix", 1) and math.isfinite(dd):
        p["breakeven_arm_dd_pct"] = round(float(np.clip(6.0 * dd, 15.0, 40.0)), 1)
        p["no_long_offside_pct"] = round(float(np.clip(1.3 * dd, 12.0, 45.0)), 1)
        p["v2_evr_offside_min_pct"] = round(float(np.clip(1.2 * dd, 8.0, 30.0)), 1)

    audit = {
        "runup_med_pct": runup,
        "dd_med_pct": dd,
        "sigma_t_min": p["v2_sigma_t_min"],
        "arm_formula": p["gain_retrace_arm_pct"],
        "breakeven_arm_dd_pct": p["breakeven_arm_dd_pct"],
        "no_long_offside_pct": p["no_long_offside_pct"],
        "evr_offside_min_pct": p["v2_evr_offside_min_pct"],
        "n_lookback": len(look),
    }
    return p, audit


def apply_candidate(base: dict, cand: dict, cfg: dict) -> dict:
    p = dict(base)
    p["gain_retrace_arm_pct"] = float(cand["arm"])
    p["confidence_high"] = float(cand["conf"])
    p["entry_confidence_high"] = float(cand["conf"])
    tau0 = float(base.get("tau_max", 40.0))
    tau = min(60.0, tau0 * float(cand.get("tau_scale", 1.0)))
    p["tau_max"] = tau
    p["tau_min"] = max(5.0, tau / 8.0)
    return p


def candidate_grid(cfg: dict) -> list[dict]:
    """Explicit grid; every cell is a constant-override on top of the base."""
    out = []
    for arm in cfg["arm_floors"]:
        for conf in cfg["conf_grid"]:
            for ts in cfg["tau_scales"]:
                # keep the grid small & interpretable: the full cross would be
                # 16 cells per date; the campaign already showed arm dominates
                # conf/tau, so use a nested design around arm=14.
                if arm != 14.0 and (conf != 0.79 or ts != 1.0):
                    continue
                out.append({"name": f"arm{arm:g}_c{int(conf*100)}_t{ts:g}",
                            "arm": float(arm), "conf": float(conf),
                            "tau_scale": float(ts)})
    if not out:
        out = [dict(c) for c in CANDIDATES]
    return out


# ─────────────────────────────────────────────────────────────────────────
# window accounting
# ─────────────────────────────────────────────────────────────────────────

def _pnl_contribution(trades: list[dict], open_trade: Optional[dict],
                      t: float, price: float, at_end: bool) -> float:
    """Realized exits <= t + mark of the position open at t."""
    realized = sum(float(tr.get("pnl_sol") or 0.0) for tr in trades
                   if float(tr.get("exit_time") or 0.0) <= t)
    held = None
    for tr in trades:
        et = float(tr.get("entry_time") or 0.0)
        xt = float(tr.get("exit_time") or 0.0)
        if et <= t < xt:
            held = tr
            break
    if held is None and open_trade is not None:
        # The replay's still-open position is not in trade_history.  It may
        # also have been open at an EARLIER boundary (long hold), so use it
        # at any t on/after its entry — not only at the replay end.
        if float(open_trade.get("entry_time") or 0.0) <= t:
            held = open_trade
    mark = 0.0
    if held:
        entry = float(held.get("entry_price") or 0.0)
        size = float(held.get("size_sol") or 0.0)
        if entry > 0:
            mark = size * (price / entry - 1.0)
    return realized + mark


def window_pnl(replay: dict, start_ts: float, end_ts: float,
               start_price: float, end_price: float) -> float:
    """Period PnL of one symbol (account units) over [start, end]."""
    trades = replay["trades"]
    c0 = _pnl_contribution(trades, replay.get("open_trade"), start_ts,
                           start_price, at_end=False)
    c1 = _pnl_contribution(trades, replay.get("open_trade"), end_ts,
                           end_price, at_end=True)
    return c1 - c0


def _slice(tape: list[dict], lo: int, hi: int) -> list[dict]:
    return tape[max(0, lo):max(0, hi)]


# ─────────────────────────────────────────────────────────────────────────
# snapshot construction
# ─────────────────────────────────────────────────────────────────────────

def _snapshot_path(cfg: dict, asof: str, tag: str) -> Path:
    h = hashlib.sha1(json.dumps({"cfg": cfg, "asof": asof},
                                sort_keys=True, default=str).encode()).hexdigest()[:10]
    name = f"snapshot_{asof}_{h}.json"
    path = RECAL_ROOT / tag / name
    if not path.exists():
        # reuse an identical snapshot from another cadence/tag (same asof+cfg)
        hits = sorted(RECAL_ROOT.glob(f"*/{name}"))
        if hits:
            return hits[0]
    return path


def build_snapshot(symbols: list[str], asof: str, cfg: dict,
                   tag: str = "default", verbose: bool = True,
                   workers: int = 8) -> dict:
    """Build (or load) one versioned snapshot at `asof`."""
    asof_ts = iso_to_ts(asof)
    path = _snapshot_path(cfg, asof, tag)
    if path.exists():
        return json.loads(path.read_text())

    cands = candidate_grid(cfg)
    cfg_hash = hashlib.sha1(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:10]

    # ── phase 1: per symbol/date fit + holdout scores for every candidate ──
    tasks = [{"symbol": s, "asof": asof, "asof_ts": asof_ts,
              "cfg": cfg, "cands": cands, "phase": 1} for s in symbols]
    t0 = time.time()
    fit_rows = _run_tasks(tasks, workers)
    if verbose:
        print(f"[recal] {asof}: phase1 {len(symbols)} symbols in "
              f"{time.time() - t0:.1f}s")

    by_symbol: dict[str, dict] = {}
    for r in fit_rows:
        if "error" in r:
            if verbose:
                print(f"[recal] {r.get('symbol')} ERROR {r['error']}")
            continue
        by_symbol[r["symbol"]] = r

    # ── selection ────────────────────────────────────────────────────────
    n_c = len(cands)
    pooled_fit = [sum(by_symbol[s]["fit"][i] for s in by_symbol) for i in range(n_c)]
    pooled_hold = [sum(by_symbol[s]["holdout"][i] for s in by_symbol) for i in range(n_c)]
    trades_fit = [sum(by_symbol[s]["trades_fit"][i] for s in by_symbol) for i in range(n_c)]

    if cfg["selection"] == "stats_only":
        # Conservative policy: refresh the causal per-symbol statistical
        # layers only; keep the campaign's behaviour knobs (arm14/conf .79/
        # tau 40) fixed.  No thin-window knob search.
        chosen = next((i for i, c in enumerate(cands)
                       if c["arm"] == 14.0 and c["conf"] == 0.79
                       and c["tau_scale"] == 1.0), 0)
        selection_diag = {"best_cell": cands[chosen]["name"],
                          "best_fit": 0.0, "se_fit": 0.0, "n_near": 1,
                          "n_symbols_traded": [0] * n_c,
                          "holdout_delta_raw": 0.0,
                          "holdout_fallback_to_arm14": False,
                          "top_trade_share": 0.0}
        hold_delta_raw = 0.0
        fell_back = False
        hold_delta = 0.0
    elif cfg["selection"] == "pooled":
        n_traded = [sum(1 for s in by_symbol if by_symbol[s]["trades_fit"][i] > 0)
                    for i in range(n_c)]
        eligible = [i for i in range(n_c) if trades_fit[i] >= cfg["min_fit_trades"]]
        if not eligible:                      # never reject every cell
            eligible = list(range(n_c))
        best = max(eligible, key=lambda i: pooled_fit[i])
        # One-standard-error rule.  The fit window is thin (~1-3 account
        # trades/month across 67 names), so a raw argmax is usually one
        # symbol's outlier.  Compute the SE of the pooled sum from the
        # cross-sectional dispersion and treat every cell within 1 SE of the
        # leader as statistically indistinguishable; among those, keep the
        # incumbent arm14 cell.  A broad-based improvement (small SE) still
        # switches.
        scores_best = [by_symbol[s]["fit"][best] for s in by_symbol]
        se = float(np.std(scores_best, ddof=1) * math.sqrt(len(scores_best))) \
            if len(scores_best) > 1 else 0.0
        near = [i for i in eligible if pooled_fit[i] >= pooled_fit[best] - se]
        inc_cell = next((i for i, c in enumerate(cands)
                         if c["arm"] == 14.0 and c["conf"] == 0.79
                         and c["tau_scale"] == 1.0), None)
        if inc_cell in near:
            chosen = inc_cell
        else:
            chosen = min(near, key=lambda i: abs(cands[i]["arm"] - 14.0))
        selection_diag = {"best_cell": cands[best]["name"], "best_fit": pooled_fit[best],
                          "se_fit": se, "n_near": len(near),
                          "n_symbols_traded": n_traded}
    else:  # per_symbol (diagnostic; 252d is too thin for this — expected to lose)
        chosen = None
        selection_diag = {"best_cell": "per_symbol", "best_fit": float("nan"),
                          "se_fit": 0.0, "n_near": n_c,
                          "n_symbols_traded": [0] * n_c}

    # holdout sanity vs the incumbent arm14 cell
    inc = next((i for i, c in enumerate(cands)
                if c["arm"] == 14.0 and c["conf"] == 0.79 and c["tau_scale"] == 1.0), 0)
    hold_delta_raw = 0.0
    fell_back = False
    if chosen is not None:
        hold_delta_raw = pooled_hold[chosen] - pooled_hold[inc]
        if hold_delta_raw < cfg["holdout_tolerance"]:
            chosen = inc
            fell_back = True
    hold_delta = 0.0 if fell_back else hold_delta_raw
    selection_diag.update({"holdout_delta_raw": hold_delta_raw,
                           "holdout_fallback_to_arm14": fell_back})
    if chosen is not None and cfg["selection"] != "stats_only":
        all_pnls = [x for s in by_symbol
                    for x in by_symbol[s].get("fit_pnls", [[]] * n_c)[chosen]]
        tot = sum(all_pnls)
        selection_diag["top_trade_share"] = (max(all_pnls) / tot
                                             if tot > 0 and all_pnls else 1.0)

    # ── materialise per-symbol params ────────────────────────────────────
    params_map: dict[str, dict] = {}
    selection: dict[str, dict] = {}
    for s, r in by_symbol.items():
        base = r["base_params"]
        if chosen is not None:
            ci = chosen
            p = apply_candidate(base, cands[ci], cfg)
            selection[s] = {"candidate": cands[ci]["name"], "index": ci}
        else:
            # per-symbol: pick each symbol's own best fit cell
            ci = max(range(n_c), key=lambda i: r["fit"][i])
            p = apply_candidate(base, cands[ci], cfg)
            selection[s] = {"candidate": cands[ci]["name"], "index": ci}
        p.pop("_kind", None)
        params_map[s] = p

    snap = {
        "version": f"{asof}-{cfg_hash}",
        "asof": asof,
        "asof_ts": asof_ts,
        "created_at": time.time(),
        "git_commit": git_commit(),
        "seed": cfg["seed"],
        "config": cfg,
        "candidates": cands,
        "selection": selection,
        "selection_pooled": {
            "fit_scores": pooled_fit,
            "holdout_scores": pooled_hold,
            "fit_trades": trades_fit,
            "chosen_index": chosen,
            "chosen": cands[chosen]["name"] if chosen is not None else "per_symbol",
            "holdout_delta_vs_arm14": hold_delta,
            **selection_diag,
        },
        "audit": {s: by_symbol[s].get("audit", {}) for s in by_symbol},
        "params": params_map,
        "n_symbols": len(params_map),
        "elapsed_s": time.time() - t0,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, indent=1, default=str))
    tmp.replace(path)

    manifest = path.parent / "manifest.json"
    mans = json.loads(manifest.read_text()) if manifest.exists() else {"versions": []}
    mans["latest"] = path.name
    mans.setdefault("versions", []).append(
        {"file": path.name, "asof": asof, "created_at": snap["created_at"],
         "chosen": snap["selection_pooled"]["chosen"]})
    manifest.write_text(json.dumps(mans, indent=1))
    if verbose:
        sp = snap["selection_pooled"]
        print(f"[recal] {asof}: chose {sp['chosen']} "
              f"(fit Σ {sp['fit_scores'][sp['chosen_index']]:+.4f}, "
              f"hold Δ {sp['holdout_delta_vs_arm14']:+.4f}, "
              f"{snap['n_symbols']} symbols)")
    return snap


# ─────────────────────────────────────────────────────────────────────────
# worker tasks
# ─────────────────────────────────────────────────────────────────────────

def _task(row: dict) -> dict:
    try:
        from process_watchdog import guard_parent
        guard_parent()
    except Exception:
        pass
    try:
        cfg = row["cfg"]
        sym = row["symbol"]
        tape = ensure_bars(sym)
        asof_ts = int(row["asof_ts"])
        idx = asof_index(tape, asof_ts)
        if idx < 320:
            return {"symbol": sym, "error": f"asof too early ({idx} bars)"}

        kind = market_kind("stocks", "1d")
        if cfg.get("base_mode") == "frozen":
            base = _frozen_one(sym)
            audit = {"base_mode": "frozen"}
        else:
            base, audit = fixed_base_params(tape[:idx], kind, cfg)

        w = int(cfg["window_bars"])
        warm = int(cfg["warmup_bars"])
        warm = idx if warm <= 0 else warm          # 0 = full-history warm
        lo = idx - w
        fit_end = lo + int(round(w * (1.0 - float(cfg["holdout_frac"]))))
        if lo < 305:
            return {"symbol": sym, "error": "window too early"}

        start_ts = float(tape[lo]["time"])
        fit_end_ts = float(tape[fit_end]["time"])
        end_ts = float(tape[idx - 1]["time"])
        start_px = float(tape[lo - 1]["close"])
        fit_px = float(tape[fit_end - 1]["close"])
        end_px = float(tape[idx - 1]["close"])

        sl = _slice(tape, lo - warm, idx)
        cands = row["cands"]
        if cfg["selection"] == "stats_only":
            n = len(cands)
            return {"symbol": sym, "asof": row["asof"], "fit": [0.0] * n,
                    "holdout": [0.0] * n, "trades_fit": [0] * n,
                    "trades_holdout": [0] * n, "fit_pnls": [[] for _ in range(n)],
                    "base_params": base, "audit": audit}

        fit, hold, nfit, nhold, pnls = [], [], [], [], []
        for c in cands:
            p = apply_candidate(base, c, cfg)
            rp = replay_candles("stocks", sym, "1d", sl, p,
                                force_close=False)
            fit.append(window_pnl(rp, start_ts, fit_end_ts, start_px, fit_px))
            hold.append(window_pnl(rp, fit_end_ts, end_ts, fit_px, end_px))
            nfit.append(sum(1 for t in rp["trades"]
                            if start_ts <= float(t.get("exit_time") or 0) <= fit_end_ts))
            nhold.append(sum(1 for t in rp["trades"]
                             if fit_end_ts < float(t.get("exit_time") or 0) <= end_ts))
            pnls.append([float(t.get("pnl_sol") or 0.0) for t in rp["trades"]
                         if start_ts <= float(t.get("exit_time") or 0) <= fit_end_ts])

        return {"symbol": sym, "asof": row["asof"], "fit": fit,
                "holdout": hold, "trades_fit": nfit, "trades_holdout": nhold,
                "fit_pnls": pnls, "base_params": base, "audit": audit}
    except Exception as exc:  # noqa: BLE001
        return {"symbol": row.get("symbol"),
                "error": f"{type(exc).__name__}: {exc}"}


def _frozen_full_task(row: dict) -> dict:
    """One full-tape frozen replay per symbol, reused across all months."""
    try:
        from process_watchdog import guard_parent
        guard_parent()
    except Exception:
        pass
    try:
        sym = row["symbol"]
        rp = replay_candles("stocks", sym, "1d", ensure_bars(sym),
                            row["params"], force_close=False)
        return {"symbol": sym, "trades": rp["trades"],
                "open_trade": rp.get("open_trade")}
    except Exception as exc:  # noqa: BLE001
        return {"symbol": row.get("symbol"),
                "error": f"{type(exc).__name__}: {exc}"}


def _forward_task(row: dict) -> dict:
    """Replay [start - warm, end) for one symbol with snapshot + frozen."""
    try:
        from process_watchdog import guard_parent
        guard_parent()
    except Exception:
        pass
    try:
        sym = row["symbol"]
        tape = ensure_bars(sym)
        start_ts = int(row["start_ts"]); end_ts = int(row["end_ts"])
        i0 = asof_index(tape, start_ts)
        i1 = asof_index(tape, end_ts)
        warm = int(row["warmup_bars"])
        warm = i0 if warm <= 0 else warm           # 0 = full-history warm
        if i0 < 305 or i1 <= i0:
            return {"symbol": sym, "error": "forward slice too thin"}
        sl = _slice(tape, i0 - warm, i1)
        s_px = float(tape[i0 - 1]["close"])
        e_px = float(tape[i1 - 1]["close"])
        out = {"symbol": sym, "start_ts": start_ts, "end_ts": end_ts}
        rp = replay_candles("stocks", sym, "1d", sl, row["cand_params"],
                            force_close=False)
        out["cand_pnl"] = window_pnl(rp, start_ts, end_ts, s_px, e_px)
        out["cand_trades"] = [t for t in rp["trades"]
                              if start_ts <= float(t.get("exit_time") or 0) <= end_ts]
        out["cand_open"] = rp.get("open_trade")
        if row.get("frozen_params") is not None:
            frp = replay_candles("stocks", sym, "1d", sl, row["frozen_params"],
                                 force_close=False)
        else:  # reuse the once-per-symbol full-tape frozen path
            frp = {"trades": row.get("frozen_trades") or [],
                   "open_trade": row.get("frozen_open")}
        out["frozen_pnl"] = window_pnl(frp, start_ts, end_ts, s_px, e_px)
        out["frozen_trades"] = [t for t in frp["trades"]
                                if start_ts <= float(t.get("exit_time") or 0) <= end_ts]
        return out
    except Exception as exc:  # noqa: BLE001
        return {"symbol": row.get("symbol"),
                "error": f"{type(exc).__name__}: {exc}"}


def _run_tasks(tasks: list[dict], workers: int, fn=None) -> list[dict]:
    fn = fn or _task
    if workers <= 1 or len(tasks) <= 1:
        return [fn(t) for t in tasks]
    with futures.ProcessPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, tasks))


# ─────────────────────────────────────────────────────────────────────────
# forward path + stitched evaluation
# ─────────────────────────────────────────────────────────────────────────

def frozen_params(symbols: list[str], results_dir: Optional[Path] = None,
                  arm: float = 14.0) -> dict[str, dict]:
    """The campaign's frozen arm14 params (from stored results if present)."""
    out = {}
    d = Path(results_dir) if results_dir else RESULTS_ROOT / "arm14"
    if d.exists():
        for f in sorted(d.glob("*.json")):
            if f.name.startswith(("batch_", "summary", "mc_", "stats_")):
                continue
            try:
                data = json.loads(f.read_text())
            except Exception:
                continue
            if isinstance(data, dict) and data.get("engine_params") \
                    and data.get("symbol") and data.get("interval") == "1d":
                p = dict(data["engine_params"])
                p["gain_retrace_arm_pct"] = float(arm)
                out[str(data["symbol"])] = p
    if not out:
        for s in symbols:
            tape = ensure_bars(s)
            idx = int(len(tape) * 0.7)
            p, _ = fixed_base_params(tape[:idx], "stocks", DEFAULT_CFG)
            p["gain_retrace_arm_pct"] = float(arm)
            out[s] = p
    return {s: out[s] for s in symbols if s in out}


def forward_month(snapshot: dict, start: str, end: str,
                  frozen: dict[str, dict], workers: int = 8,
                  warmup: int = 0,
                  frozen_cache: Optional[dict] = None) -> dict:
    """Per-symbol forward PnL for the snapshot and the frozen baseline."""
    tasks = []
    for sym, p in snapshot["params"].items():
        if sym not in frozen:
            continue
        task = {"symbol": sym, "start_ts": iso_to_ts(start),
                "end_ts": iso_to_ts(end), "warmup_bars": warmup,
                "cand_params": p}
        if frozen_cache and sym in frozen_cache:
            task["frozen_trades"] = frozen_cache[sym]["trades"]
            task["frozen_open"] = frozen_cache[sym].get("open_trade")
        else:
            task["frozen_params"] = frozen[sym]
        tasks.append(task)
    rows = _run_tasks(tasks, workers, _forward_task)
    ok = [r for r in rows if "error" not in r]
    return {
        "start": start, "end": end,
        "n_symbols": len(ok),
        "cand_total": sum(r["cand_pnl"] for r in ok),
        "frozen_total": sum(r["frozen_pnl"] for r in ok),
        "per_symbol": {r["symbol"]: {"cand": r["cand_pnl"],
                                     "frozen": r["frozen_pnl"]} for r in ok},
        "cand_trades": [t for r in ok for t in r["cand_trades"]],
        "frozen_trades": [t for r in ok for t in r["frozen_trades"]],
        "errors": [r for r in rows if "error" in r],
    }


def replay_policy(dates: list[str], symbols: list[str], cfg: dict,
                  tag: str, frozen: dict[str, dict],
                  workers: int = 8, verbose: bool = True) -> dict:
    """Monthly snapshots + forward OOS paths, stitched into one account."""
    # The frozen baseline is constant: replay it ONCE per symbol over the
    # full tape and slice every window from that path.  Halves the forward
    # cost and guarantees the same baseline across months.
    frows = _run_tasks(
        [{"symbol": s, "params": frozen[s]}
         for s in symbols if s in frozen],
        workers, _frozen_full_task)
    frozen_cache = {r["symbol"]: r for r in frows if "error" not in r}
    if verbose:
        print(f"[recal] frozen path cached for {len(frozen_cache)} symbols")
    months = []
    for i, d in enumerate(dates):
        end = dates[i + 1] if i + 1 < len(dates) else None
        if end is None:
            # last date: one extra month
            import datetime as dt
            dd = dt.date.fromisoformat(d) + dt.timedelta(days=31)
            end = dd.replace(day=1).isoformat()
        snap = build_snapshot(symbols, d, cfg, tag=tag, verbose=verbose,
                              workers=workers)
        m = forward_month(snap, d, end, frozen, workers=workers,
                          warmup=int(cfg["warmup_bars"]),
                          frozen_cache=frozen_cache)
        m["asof"] = d
        m["chosen"] = snap["selection_pooled"]["chosen"]
        months.append(m)
        if verbose:
            print(f"[recal] {d} → {end}: cand {m['cand_total']:+.4f} "
                  f"frozen {m['frozen_total']:+.4f} "
                  f"Δ {m['cand_total'] - m['frozen_total']:+.4f} "
                  f"({m['chosen']})")

    out = {
        "tag": tag, "config": cfg, "dates": dates, "months": months,
        "total_cand": sum(m["cand_total"] for m in months),
        "total_frozen": sum(m["frozen_total"] for m in months),
        "n_symbols": len(symbols),
    }
    out["delta"] = out["total_cand"] - out["total_frozen"]
    out["gate"] = paired_gate(
        [m["cand_total"] - m["frozen_total"] for m in months],
        {s: sum(m["per_symbol"].get(s, {}).get("cand", 0.0)
                - m["per_symbol"].get(s, {}).get("frozen", 0.0)
                for m in months)
         for s in symbols},
        cfg)
    return out


# ─────────────────────────────────────────────────────────────────────────
# statistics
# ─────────────────────────────────────────────────────────────────────────

def _wilcoxon(xs: list[float]) -> tuple[float, float]:
    nz = [x for x in xs if x != 0.0]
    if len(nz) < 10:
        return (float("nan"), float("nan"))
    try:
        from scipy.stats import wilcoxon
        r = wilcoxon(nz)
        return (float(r.statistic), float(r.pvalue))
    except Exception:
        return (float("nan"), float("nan"))


def _bootstrap_ci(xs: list[float], n: int = 10000, seed: int = 7,
                  alpha: float = 0.05) -> tuple[float, float]:
    if not xs:
        return (0.0, 0.0)
    rng = random.Random(seed)
    k = len(xs)
    means = []
    for _ in range(n):
        s = 0.0
        for _ in range(k):
            s += xs[rng.randrange(k)]
        means.append(s / k)
    means.sort()
    return (means[int(alpha / 2 * n)],
            means[int((1 - alpha / 2) * n) - 1])


def paired_gate(month_deltas: list[float], symbol_deltas: dict[str, float],
                cfg: dict) -> dict:
    """Campaign-grade gate on the stitched OOS path.

    Units: months for the time-series test, symbols for breadth/CI.
    """
    sym_vals = list(symbol_deltas.values())
    traded = [v for v in sym_vals if v != 0.0]
    p_month = _wilcoxon(month_deltas)[1]
    p_sym = _wilcoxon(sym_vals)[1]
    ci_m = _bootstrap_ci(month_deltas, cfg["bootstrap_n"], cfg["seed"])
    ci_s = _bootstrap_ci(sym_vals, cfg["bootstrap_n"], cfg["seed"] + 1)
    breadth_all = (sum(1 for v in sym_vals if v > 0) / len(sym_vals)) if sym_vals else 0.0
    breadth_traded = (sum(1 for v in traded if v > 0) / len(traded)) if traded else 0.0
    total = sum(month_deltas)
    passed = (
        total > 0
        and (p_month < cfg["gate_p"] or p_sym < cfg["gate_p"])
        and ci_s[0] > 0
        and breadth_all >= cfg["breadth_min"]
    )
    return {
        "total_delta": total,
        "month_deltas": month_deltas,
        "wilcoxon_p_month": p_month,
        "wilcoxon_p_symbol": p_sym,
        "bootstrap_ci_month": ci_m,
        "bootstrap_ci_symbol": ci_s,
        "breadth_all": breadth_all,
        "breadth_traded": breadth_traded,
        "n_months": len(month_deltas),
        "n_symbols": len(sym_vals),
        "n_traded": len(traded),
        "passed": bool(passed),
        "gate_p": cfg["gate_p"],
        "breadth_min": cfg["breadth_min"],
    }


def stitched_stats(trades: list[dict], start_ts: int, symbol: str = "ALL") -> dict:
    """Sharpe / maxDD / worst day from the candidate's stitched trade path."""
    if not trades:
        return {}
    days: dict[int, float] = {}
    for t in trades:
        xt = int(t.get("exit_time") or 0)
        if xt <= 0:
            continue
        days[xt // 86400] = days.get(xt // 86400, 0.0) + float(t.get("pnl_sol") or 0.0)
    if not days:
        return {}
    vals = [v for _, v in sorted(days.items())]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for v in vals:
        eq += v
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / max(1, len(vals) - 1)
    sd = math.sqrt(var)
    return {
        "days": len(vals),
        "total_pnl": sum(vals),
        "mean_pct": mean * 100,
        "sd_pct": sd * 100,
        "sharpe": (mean / sd * math.sqrt(252)) if sd > 0 else 0.0,
        "maxdd_pct": mdd * 100,
        "worst_day_pct": min(vals) * 100,
    }


# ─────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────

def _panel(symbols: list[str], n: int) -> list[str]:
    if n <= 0 or n >= len(symbols):
        return list(symbols)
    step = max(1, len(symbols) // n)
    return symbols[::step][:n]


def cmd_snapshot(args) -> None:
    cfg = dict(DEFAULT_CFG)
    cfg["window_bars"] = args.window
    cfg["holdout_frac"] = args.holdout
    cfg["selection"] = args.selection
    cfg["base_mode"] = args.base_mode
    cfg["base_lookback_bars"] = args.base_lookback
    cfg["sigma_gate_fix"] = 0.0 if args.no_sigma_fix else 1.0
    cfg["dd_fix"] = 1.0 if args.dd_fix else 0.0
    syms = stock_symbols()
    snap = build_snapshot(syms, args.asof, cfg, tag=args.tag, workers=args.workers)
    print(json.dumps({k: snap[k] for k in
                      ("version", "asof", "n_symbols", "selection_pooled")},
                     indent=1))


def cmd_replay(args) -> None:
    cfg = dict(DEFAULT_CFG)
    cfg["window_bars"] = args.window
    cfg["holdout_frac"] = args.holdout
    cfg["selection"] = args.selection
    cfg["base_mode"] = args.base_mode
    cfg["base_lookback_bars"] = args.base_lookback
    cfg["gate_p"] = args.gate_p
    syms = _panel(stock_symbols(), args.panel)
    frozen = frozen_params(syms)
    dates = (month_starts(args.start, args.end) if args.cadence == "monthly"
             else quarter_starts(args.start, args.end))
    out = replay_policy(dates, syms, cfg, args.label, frozen,
                        workers=args.workers)
    out_dir = RECAL_ROOT / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "replay.json").write_text(json.dumps(out, indent=1, default=str))
    g = out["gate"]
    print(f"\n[recal] {args.label}: Σ cand {out['total_cand']:+.4f} "
          f"frozen {out['total_frozen']:+.4f} Δ {out['delta']:+.4f}")
    print(f"[recal] gate: p_month {g['wilcoxon_p_month']:.3g} "
          f"p_symbol {g['wilcoxon_p_symbol']:.3g} "
          f"CI_symbol [{g['bootstrap_ci_symbol'][0]:+.4f}, {g['bootstrap_ci_symbol'][1]:+.4f}] "
          f"breadth {g['breadth_all']:.0%} → {'PASS' if g['passed'] else 'FAIL'}")


def cmd_sweep(args) -> None:
    """Cheap calibrator-knob screening: panel + quarterly dates."""
    syms = _panel(stock_symbols(), args.panel)
    frozen = frozen_params(syms)
    dates = quarter_starts(args.start, args.end)
    rows = []
    base_cfg = dict(DEFAULT_CFG)
    for selection in ("stats_only", "pooled"):
        # stats_only is cheap (no candidate replays); it is the conservative
        # "refresh the causal layers only" policy and the honest baseline.
        for window in (126, 252, 378):
            for holdout in (0.20, 0.33):
                cfg = dict(base_cfg)
                cfg["window_bars"] = window
                cfg["holdout_frac"] = holdout
                cfg["selection"] = selection
                tag = f"sw_{selection}_w{window}_h{int(holdout*100)}"
                t0 = time.time()
                out = replay_policy(dates, syms, cfg, tag, frozen,
                                    workers=args.workers, verbose=False)
                g = out["gate"]
                row = {
                    "window": window, "holdout": holdout, "selection": selection,
                    "delta": out["delta"], "total_cand": out["total_cand"],
                    "total_frozen": out["total_frozen"],
                    "p_month": g["wilcoxon_p_month"], "p_symbol": g["wilcoxon_p_symbol"],
                    "ci_symbol": g["bootstrap_ci_symbol"],
                    "breadth": g["breadth_all"], "passed": g["passed"],
                    "elapsed_s": time.time() - t0,
                    "monthly_delta": [m["cand_total"] - m["frozen_total"]
                                      for m in out["months"]],
                    "chosen": [m["chosen"] for m in out["months"]],
                }
                rows.append(row)
                print(f"[sweep] {selection:<10} w{window} h{holdout:.0%}: "
                      f"Δ {row['delta']:+.4f} p_sym {row['p_symbol']:.3g} "
                      f"breadth {row['breadth']:.0%} "
                      f"{'PASS' if row['passed'] else 'fail'} "
                      f"({row['elapsed_s']:.0f}s)")
    out_dir = RECAL_ROOT / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sweep.json").write_text(json.dumps(rows, indent=1))
    best = max(rows, key=lambda r: r["delta"])
    print(f"\n[sweep] best Δ: w{best['window']} h{best['holdout']:.0%} "
          f"Δ {best['delta']:+.4f} → {out_dir / 'sweep.json'}")


def campaign_params(prefix: list[dict], kind: str, arm: float = 14.0,
                    sigma_fix: bool = False, dd_fix: bool = False) -> dict:
    """Exact campaign base (FULL train prefix), optionally with each fix."""
    calib = calibrate_market_params(prefix, kind, len(prefix))
    p = dict(calib["engine_params"])
    p["gain_retrace_arm_pct"] = float(arm)
    if sigma_fix:
        p["v2_sigma_t_min"] = sigma_gate(prefix, p)
    if dd_fix:
        closes = np.array([c["close"] for c in prefix], dtype=float)
        highs = np.array([c["high"] for c in prefix], dtype=float)
        runup, dd = forward_stats(closes, highs,
                                  window=10 if "1d" not in kind else 5)
        if math.isfinite(dd):
            p["breakeven_arm_dd_pct"] = round(float(np.clip(6.0 * dd, 15.0, 40.0)), 1)
            p["no_long_offside_pct"] = round(float(np.clip(1.3 * dd, 12.0, 45.0)), 1)
            p["v2_evr_offside_min_pct"] = round(float(np.clip(1.2 * dd, 8.0, 30.0)), 1)
    return p


def _fixprobe_task(row: dict) -> dict:
    try:
        from process_watchdog import guard_parent
        guard_parent()
    except Exception:
        pass
    try:
        sym = row["symbol"]
        tape = ensure_bars(sym)
        split_ts = int(row["split_ts"])
        idx = asof_index(tape, split_ts)
        p = campaign_params(tape[:idx], "stocks", arm=14.0,
                            sigma_fix=row["sigma_fix"], dd_fix=row["dd_fix"])
        rp = replay_candles("stocks", sym, "1d", tape, p, force_close=True)
        tr = [t for t in rp["trades"] if int(t.get("entry_time") or 0) < split_ts]
        te = [t for t in rp["trades"] if int(t.get("entry_time") or 0) >= split_ts]
        from .runner import _partition_stats
        return {"symbol": sym, "trades": rp["trades"], "stats": rp["stats"],
                "stats_train": _partition_stats(tr),
                "stats_test": _partition_stats(te),
                "engine_params": p, "n_candles": len(tape),
                "n_candles_train": idx, "split_ts": split_ts}
    except Exception as exc:  # noqa: BLE001
        return {"symbol": row.get("symbol"), "error": f"{type(exc).__name__}: {exc}"}


def cmd_fixprobe(args) -> None:
    """Full-book batch with the campaign base + each repaired layer."""
    syms = stock_symbols()
    split_ts = iso_to_ts(args.split)
    tag = args.label
    out_dir = RESULTS_ROOT / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    tasks = [{"symbol": s, "split_ts": split_ts,
              "sigma_fix": args.mode in ("sigma", "both"),
              "dd_fix": args.mode in ("dd", "both")} for s in syms]
    t0 = time.time()
    rows = _run_tasks(tasks, args.workers, _fixprobe_task)
    ok = [r for r in rows if "error" not in r]
    for r in ok:
        sym = r["symbol"]
        out = {
            "market": "stocks", "symbol": sym, "interval": "1d",
            "kind": "stocks", "label": tag,
            "split_frac": round(r["n_candles_train"] / r["n_candles"], 6),
            "split_ts": r["split_ts"], "split_iso": ts_to_iso(r["split_ts"]),
            "n_candles": r["n_candles"], "n_candles_train": r["n_candles_train"],
            "calibration": {"mode": args.mode},
            "engine_params": r["engine_params"],
            "stats": r["stats"], "stats_train": r["stats_train"],
            "stats_test": r["stats_test"], "starting_balance": 1.0,
            "buy_size": 0.1,
            "slippage_pct": COSTS["stocks"]["slippage_pct"],
            "priority_fee": COSTS["stocks"]["priority_fee"],
            "ran_at": time.time(), "trades": r["trades"],
        }
        (out_dir / f"stocks_{sym}_1d_{tag}.json").write_text(
            json.dumps(out, default=str))
    te = sum(r["stats_test"]["total_pnl"] for r in ok)
    tr = sum(r["stats_train"]["total_pnl"] for r in ok)
    nt = sum(r["stats_test"]["trades"] for r in ok)
    pos = sum(1 for r in ok if r["stats_test"]["total_pnl"] > 0)
    print(f"[fixprobe:{args.mode}] {len(ok)}/{len(syms)} symbols  "
          f"test Σ {te:+.4f} ({nt} trades, {pos} pos)  train Σ {tr:+.4f}  "
          f"{time.time() - t0:.0f}s → {out_dir}")
    for r in rows:
        if "error" in r:
            print(f"  ERROR {r['symbol']}: {r['error']}")


def cmd_adopt(args) -> None:
    """Manually mark one snapshot as adopted (live picks it up at open)."""
    p = Path(args.snapshot)
    if p.is_dir():
        mf = p / "manifest.json"
        p = p / (json.loads(mf.read_text())["latest"] if mf.exists()
                else sorted(p.glob("snapshot_*.json"))[-1].name)
    snap = json.loads(p.read_text())
    if not snap.get("params"):
        raise SystemExit(f"{p} has no params")
    rec = {"file": p.name, "adopted_at": time.time(),
           "adopted_by": args.by, "note": args.note,
           "version": snap.get("version"), "asof": snap.get("asof"),
           "n_symbols": len(snap["params"])}
    (p.parent / "adopted.json").write_text(json.dumps(rec, indent=1))
    print(f"[adopt] {p.name} → {p.parent / 'adopted.json'}")


def cmd_batch(args) -> None:
    """Write runner-format result JSONs for a snapshot over full tapes."""
    snap = json.loads(Path(args.snapshot).read_text())
    params = snap["params"]
    split_ts = iso_to_ts(args.split)
    out_dir = Path(args.out_dir) if args.out_dir else RESULTS_ROOT / args.label
    out_dir.mkdir(parents=True, exist_ok=True)
    from .runner import _partition_stats
    for sym, p in params.items():
        tape = ensure_bars(sym)
        idx = asof_index(tape, split_ts)
        replay = replay_candles("stocks", sym, "1d", tape, p,
                                force_close=True)
        trades = replay["trades"]
        tr = [t for t in trades if int(t.get("entry_time") or 0) < split_ts]
        te = [t for t in trades if int(t.get("entry_time") or 0) >= split_ts]
        out = {
            "market": "stocks", "symbol": sym, "interval": "1d",
            "kind": "stocks", "label": args.label,
            "split_frac": round(idx / len(tape), 6), "split_ts": split_ts,
            "split_iso": ts_to_iso(split_ts),
            "n_candles": len(tape), "n_candles_train": idx,
            "calibration": snap.get("audit", {}).get(sym, {}),
            "engine_params": p, "recal_snapshot": snap.get("version"),
            "stats": replay["stats"],
            "stats_train": _partition_stats(tr),
            "stats_test": _partition_stats(te),
            "starting_balance": 1.0, "buy_size": 0.1,
            "slippage_pct": COSTS["stocks"]["slippage_pct"],
            "priority_fee": COSTS["stocks"]["priority_fee"],
            "ran_at": time.time(), "trades": trades,
        }
        (out_dir / f"stocks_{sym}_1d_{args.label}.json").write_text(
            json.dumps(out, default=str))
    print(f"[batch] {len(params)} symbols → {out_dir}")


def cmd_report(args) -> None:
    p = Path(args.run)
    out = json.loads(p.read_text())
    months = out["months"]
    g = out["gate"]
    cand_trades = [t for m in months for t in m.get("cand_trades", [])]
    frozen_trades = [t for m in months for t in m.get("frozen_trades", [])]
    st_c = stitched_stats(cand_trades, iso_to_ts(months[0]["start"]))
    st_f = stitched_stats(frozen_trades, iso_to_ts(months[0]["start"]))
    print(f"\n[{out['tag']}] {len(months)} periods, "
          f"{out['n_symbols']} symbols, window {out['config']['window_bars']}d, "
          f"selection {out['config']['selection']}")
    print(f"{'period':<12}{'cand':>10}{'frozen':>10}{'Δ':>10}  chosen")
    for m in months:
        print(f"{m['start']:<12}{m['cand_total']:>10.4f}"
              f"{m['frozen_total']:>10.4f}"
              f"{m['cand_total'] - m['frozen_total']:>10.4f}  {m['chosen']}")
    print(f"{'TOTAL':<12}{out['total_cand']:>10.4f}"
          f"{out['total_frozen']:>10.4f}{out['delta']:>10.4f}")
    print(f"\n  candidate: trades {len(cand_trades)}  "
          f"Sharpe {st_c.get('sharpe', 0):.2f}  "
          f"maxDD {st_c.get('maxdd_pct', 0):.1f}%  "
          f"worst day {st_c.get('worst_day_pct', 0):.2f}%")
    print(f"  frozen:    trades {len(frozen_trades)}  "
          f"Sharpe {st_f.get('sharpe', 0):.2f}  "
          f"maxDD {st_f.get('maxdd_pct', 0):.1f}%  "
          f"worst day {st_f.get('worst_day_pct', 0):.2f}%")
    print(f"  gate: p_month {g['wilcoxon_p_month']:.3g}  "
          f"p_symbol {g['wilcoxon_p_symbol']:.3g}  "
          f"CI_symbol [{g['bootstrap_ci_symbol'][0]:+.4f}, "
          f"{g['bootstrap_ci_symbol'][1]:+.4f}]  "
          f"breadth {g['breadth_all']:.0%} (traded {g['breadth_traded']:.0%}) "
          f"→ {'PASS' if g['passed'] else 'FAIL'}")
    out["summary"] = {"candidate": st_c, "frozen": st_f}
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, indent=1))
    else:
        (p.parent / "report.json").write_text(json.dumps(out, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot")
    s.add_argument("--asof", required=True)
    s.add_argument("--window", type=int, default=252)
    s.add_argument("--holdout", type=float, default=0.20)
    s.add_argument("--selection", default="pooled", choices=["pooled", "per_symbol", "stats_only"])
    s.add_argument("--base-mode", default="trailing", choices=["trailing", "frozen"])
    s.add_argument("--base-lookback", type=int, default=505,
                   help="bars for the causal statistical estimators; 0 = full history")
    s.add_argument("--tag", default="latest")
    s.add_argument("--no-sigma-fix", action="store_true",
                   help="keep the incumbent 1e-6 entry gate (diagnostic)")
    s.add_argument("--dd-fix", action="store_true",
                   help="enable the repaired drawdown ladder (measured worse)")
    s.add_argument("--workers", type=int, default=8)
    s.set_defaults(fn=cmd_snapshot)

    r = sub.add_parser("replay")
    r.add_argument("--label", required=True)
    r.add_argument("--start", default="2024-01-01")
    r.add_argument("--end", default="2025-07-01")
    r.add_argument("--cadence", default="monthly", choices=["monthly", "quarterly"])
    r.add_argument("--window", type=int, default=252)
    r.add_argument("--holdout", type=float, default=0.20)
    r.add_argument("--selection", default="pooled", choices=["pooled", "per_symbol", "stats_only"])
    r.add_argument("--base-mode", default="trailing", choices=["trailing", "frozen"])
    r.add_argument("--base-lookback", type=int, default=505)
    r.add_argument("--gate-p", type=float, default=0.05)
    r.add_argument("--panel", type=int, default=0, help="0 = all 67")
    r.add_argument("--workers", type=int, default=8)
    r.set_defaults(fn=cmd_replay)

    w = sub.add_parser("sweep")
    w.add_argument("--label", default="sweep")
    w.add_argument("--start", default="2024-01-01")
    w.add_argument("--end", default="2025-07-01")
    w.add_argument("--panel", type=int, default=23)
    w.add_argument("--workers", type=int, default=8)
    w.set_defaults(fn=cmd_sweep)

    b = sub.add_parser("batch")
    b.add_argument("--snapshot", required=True)
    b.add_argument("--label", required=True)
    b.add_argument("--split", default="2023-10-16")
    b.add_argument("--out-dir", default="")
    b.set_defaults(fn=cmd_batch)

    rp = sub.add_parser("report")
    rp.add_argument("--run", required=True)
    rp.add_argument("--json-out", default="")
    rp.set_defaults(fn=cmd_report)

    a = sub.add_parser("adopt")
    a.add_argument("--snapshot", required=True)
    a.add_argument("--by", default="manual")
    a.add_argument("--note", default="")
    a.set_defaults(fn=cmd_adopt)

    f = sub.add_parser("fixprobe")
    f.add_argument("--mode", required=True,
                   choices=["none", "sigma", "dd", "both"])
    f.add_argument("--label", required=True)
    f.add_argument("--split", default="2023-10-16")
    f.add_argument("--workers", type=int, default=8)
    f.set_defaults(fn=cmd_fixprobe)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
