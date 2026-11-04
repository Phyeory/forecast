"""
Signal-time engine-parameter capture for the LiveTrader.

At the instant a BUY or EXIT signal fires, the live trader records every
decision-relevant engine value together with the threshold it was compared
against (the dashboard-set parameter), so a live session's trades can be
audited against the exact gate arithmetic that admitted or rejected them.

Physical output: one JSON-lines file per live session directory:
``backend/data/live_logs/<session>/signals.jsonl``, one record per signal
event.  Each record is::

    {
      "ts": 1757...,                       # wall-clock unix seconds
      "iso": "2026-09-04T...Z",            # ISO-8601
      "event": "buy_signal" | "exit_signal",
      "signal": "buy" | "exit",
      "engine_version": 2,
      "regime": "trend",
      "reason": "buy_trend" | "gain_retrace" | ...,
      "candle_time": 1789...,              # engine candle clock (s)
      "price": 0.0000123,                  # signal-state close
      "fields": {                          # THE payload: one entry per
        "trend_confidence": {              # decision value, keyed by name
          "live_value": 0.8421,            # what the engine computed NOW
          "threshold": 0.79,               # the dashboard-set parameter
          "comparison": ">=",              # how the gate compares them
          "passed": true                   # live gate outcome
        },
        "v2_P_up": {"live_value": 0.71, "threshold": 0.62, ...},
        ...
      },
      "coefficients": {                    # equation coefficients (all of
        "lambda_mu": 0.15, ...             # them, straight off the engine)
      },
      "engine_state": {                    # supporting latent state at the
        "mu": ..., "h": ..., ...           # signal instant (V2 posterior)
      }
    }

Design invariants:
  * The engine owns its decision state: every value and threshold is read
    from ``engine.snapshot()`` (a flat dict keyed by attribute name).  Any
    key the snapshot does not carry falls back to reading the live engine
    attribute exactly as this module did before snapshots existed, so no
    field can go missing and no engine family is left uncovered.
  * Read-only on the engine.  Capture must never perturb trading state
    (same contract as ForwardTester's ``_capture_entry_params``).
  * Strictly guarded so every engine family (V1 / V2 / V3 / V4 / V6, whose
    knob surfaces differ) captures everything it has and never crashes on
    what it lacks.
  * Write failures are swallowed (logging must never break trading).
  * The record is written once per SIGNAL event (not per retry or launch):
    the signal state is frozen at detection time in
    LiveTrader._queue_signal_from_state, mirroring the backtester's
    signal-instant snapshot semantics.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _f(obj, attr, default=None):
    """Safe float read of an engine attribute (getattr-guarded)."""
    try:
        v = getattr(obj, attr, default)
        if v is None:
            return None
        return round(float(v), 10)
    except (TypeError, ValueError, AttributeError):
        return None


def _fr(v):
    """Round an already-extracted raw engine value the way _f would."""
    if v is None:
        return None
    try:
        return round(float(v), 10)
    except (TypeError, ValueError):
        return None


def _r(v, nd=10):
    """Round a numeric value, pass through everything else."""
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return round(f, nd)
    except (TypeError, ValueError):
        return v


def _field(live_value, threshold=None, comparison=None, passed=None):
    """One {live_value, threshold, comparison, passed} entry."""
    return {
        "live_value": _r(live_value),
        "threshold": _r(threshold),
        "comparison": comparison,
        "passed": passed,
    }


def _snapshot(eng) -> Optional[dict]:
    """The engine's decision-state dict, or None when it offers none."""
    fn = getattr(eng, "snapshot", None)
    if not callable(fn):
        return None
    try:
        snap = fn()
    except Exception:
        return None
    return snap if isinstance(snap, dict) else None


def _val(snap, eng, name, default=None):
    """Snapshot value for ``name``, else the live engine attribute."""
    if snap is not None and name in snap:
        return snap[name]
    return getattr(eng, name, default)


def _v2_decision(snap, eng) -> dict:
    """The latest V2 Kramers/Kelly decision snapshot (getattr-guarded)."""
    dec = _val(snap, eng, "_v2_last_decision", None)
    return dec if isinstance(dec, dict) and dec else {}


def _has(snap, eng, attr) -> bool:
    """Threshold knob exists and is enabled (non-zero / positive)."""
    v = _val(snap, eng, attr, None)
    try:
        return v is not None and float(v) != 0.0
    except (TypeError, ValueError):
        return False


def _knob(snap, eng, attr, default=None):
    """Read a threshold knob without popping / mutating anything."""
    return _val(snap, eng, attr, default)


# --- Coefficient / full-config collectors ------------------------------------

# Internal adapter knob name -> dashboard parameter name, so a record can be
# diffed against the Engine Parameters modal key-for-key.
_DASH_NAMES = {
    "_v2_p_up_min": "v2_p_up_min",
    "_v2_sigma_t_min": "v2_sigma_t_min",
    "_gain_retrace_arm_pct": "gain_retrace_arm_pct",
    "_gain_retrace_give_frac": "gain_retrace_give_frac",
    "_breakeven_arm_dd_pct": "breakeven_arm_dd_pct",
    "_breakeven_buffer_pct": "breakeven_buffer_pct",
    "_reversal_exit_bars": "reversal_exit_bars",
    "_no_long_exit_bars": "no_long_exit_bars",
    "_no_long_offside_pct": "no_long_offside_pct",
    "_no_long_mu_neg_frac": "no_long_mu_neg_frac",
    "_v2_evr_enable": "v2_evr_enable",
    "_v2_evr_confirm_pct": "v2_evr_confirm_pct",
    "_v2_evr_eval_delay": "v2_evr_eval_delay",
    "_v2_evr_grace_seconds": "v2_evr_grace_seconds",
    "_v2_evr_flow_window": "v2_evr_flow_window",
    "_v2_evr_buy_ratio_max": "v2_evr_buy_ratio_max",
    "_v2_evr_volume_min_sol": "v2_evr_volume_min_sol",
    "_v2_evr_require_offside": "v2_evr_require_offside",
    "_v2_evr_offside_min_pct": "v2_evr_offside_min_pct",
    "_v2_evr_skip_sell_conc_min": "v2_evr_skip_sell_conc_min",
    "_v2_evr_skip_conc_window": "v2_evr_skip_conc_window",
    "_v2_rate_split_enable": "v2_rate_split_enable",
    "_v2_rate_split_arm_pct": "v2_rate_split_arm_pct",
    "_v2_rate_split_offside_pct": "v2_rate_split_offside_pct",
    "_v2_rate_split_theta": "v2_rate_split_theta",
    "_v2_rate_split_persist": "v2_rate_split_persist",
    "_v2_rate_split_min_peak_age_ticks": "v2_rate_split_min_peak_age_ticks",
    "_v2_aoe_enable": "v2_aoe_enable",
    "_v2_aoe_offside_pct": "v2_aoe_offside_pct",
    "_v2_aoe_theta": "v2_aoe_theta",
    "_v2_aoe_p_up_max": "v2_aoe_p_up_max",
    "_v2_aoe_persist": "v2_aoe_persist",
    "_v2_aoe_max_peak_pct": "v2_aoe_max_peak_pct",
    "_v2_holder_flow_entry_block": "v2_holder_flow_entry_block",
    "_v2_holder_flow_entry_window_seconds": "v2_holder_flow_entry_window_seconds",
    "_v2_holder_flow_exit_enable": "v2_holder_flow_exit_enable",
    "_v2_holder_flow_exit_window_seconds": "v2_holder_flow_exit_window_seconds",
    "_v2_holder_flow_min_usd": "v2_holder_flow_min_usd",
    "_v2_holder_flow_require_tag": "v2_holder_flow_require_tag",
    "_v2_msm_enable": "v2_msm_enable",
    "_mcap_low_usd": "mcap_low_usd",
    "_mcap_high_usd": "mcap_high_usd",
}


def collect_coefficients(eng, snap=None) -> dict:
    """ALL equation coefficients plus every remaining threshold knob.

    V2: the SDE / filter coefficient vector (lambda_mu ...) from the core
    cfg, every adapter knob, then the FULL engine cfg dict, so nothing
    dashboard-settable is ever missing from the record.  V1 (and any engine
    with a plain-attribute surface): the cfg_* attrs the backtester's
    entry_params snapshot enumerates, plus everything in a ``cfg`` dict
    attribute if present.
    """
    if snap is None:
        snap = _snapshot(eng)
    out: dict = {}

    # V2 core SDE coefficients (the adapter mirrors them as plain attrs).
    for k in ("lambda_mu", "kappa_mu", "sigma_mu", "eta", "sigma_h",
              "alpha", "beta", "sigma_phi", "theta", "sigma_ell",
              "zeta", "lambda_0", "lambda_1", "kappa_J", "s_0", "s_1",
              "eps_div", "sigma_floor", "N_p", "n_particles", "n_grid"):
        v = _val(snap, eng, k, None)
        if v is None:
            continue
        out[k] = _r(v)

    # Full cfg dicts (V2 adapter: self.cfg = core.cfg; V1: none).
    cfg = getattr(eng, "cfg", None)
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            try:
                if isinstance(v, (int, float, bool, str)) or v is None:
                    out.setdefault(f"cfg.{k}", _r(v) if isinstance(v, (int, float)) else v)
            except Exception:
                pass

    # V1-style cfg_* plain attributes: the exhaustive list the backtester's
    # _capture_entry_params enumerates.  V1 and the adapters echo them for
    # the pipeline layers.
    for attr in (
        "confidence_high", "confidence_low", "entry_confidence_high",
        "entry_confidence_low", "confidence_very_high",
        "confidence_w1", "confidence_w2", "confidence_w3", "confidence_w4",
        "ema_fast_p", "ema_slow_p", "ema_macro_period", "atr_period",
        "roc_period", "warmup",
        "S_strong", "S_weak", "S_noise", "exhaustion_bars_limit",
        "delta_threshold", "min_trend_bars", "reversal_confirm_bars",
        "chop_atr_pct", "chop_spread_pct", "reversal_exit_confirm_bars",
        "s_effective_threshold", "exhaustion_persist_bars",
        "exhaustion_s_decay_bars", "exhaustion_stall_bars",
        "exhaustion_stall_atr_pct", "regime_lookback",
        "persistence_threshold", "momentum_mean_threshold",
        "ema_min_spread_pct", "atr_floor_k", "ema_cross_persist_bars",
        "local_range_bars", "local_range_threshold_pct",
        "sign_flip_threshold", "stability_bars", "spike_atr_multiplier",
        "spike_lookback_bars", "body_baseline_bars", "overextension_k",
        "momentum_peak_bars", "consolidation_range_pct",
        "stoploss_pct", "takeprofit_pct",
        "stoploss_pct_low", "stoploss_pct_high",
        "takeprofit_pct_low", "takeprofit_pct_high",
        "max_entry_bar_count", "forbidden_bc_lo", "forbidden_bc_hi",
        "trail_floor_pct", "reversal_exit_bars_max",
        "reversal_exit_bars",
        # Adapter exit-overlay knobs.
        "gain_retrace_arm_pct", "gain_retrace_give_frac",
        "breakeven_arm_dd_pct", "breakeven_buffer_pct",
        "no_long_exit_bars", "no_long_offside_pct", "no_long_mu_neg_frac",
    ):
        v = _val(snap, eng, attr, None)
        if v is None:
            continue
        out[attr] = _r(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

    # Every remaining v2_* knob exposed by the engine family: both the
    # public `v2_*` names and the internal `_v2_*` threshold knobs the
    # gates compare against.  dir() order is the key order of the record.
    try:
        for name in dir(eng):
            if not (name.startswith("v2_") or (name.startswith("_v2_")
                    and not name.startswith("_v2_last"))):
                continue
            if name in out:
                continue
            if snap is not None and name in snap:
                v = snap[name]
            else:
                v = getattr(eng, name)
            if callable(v) or isinstance(v, (dict, list, tuple, frozenset, set)):
                continue
            out[name] = _r(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
    except Exception:
        pass

    # V1-family leading-underscore threshold knobs (strategy_engine).
    for attr in ("_max_entry_bar_count", "_forbidden_bc_lo", "_forbidden_bc_hi"):
        v = _val(snap, eng, attr, None)
        if v is not None:
            out[attr] = _r(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

    # Mirror internal knob names under their DASHBOARD parameter names.
    for internal, dash in _DASH_NAMES.items():
        v = _val(snap, eng, internal, None)
        if v is not None:
            out[internal] = _r(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
            out.setdefault(dash, out[internal])

    return out


def _core_trio(snap, eng) -> dict:
    """V2 core observables {v2_sigma_t, v2_mu_dot, v2_L_t} (present values)."""
    out: dict = {}
    core = getattr(eng, "core", None)
    for k, attr in (("v2_sigma_t", "_last_sigma_t"),
                    ("v2_mu_dot", "_mu_dot"),
                    ("v2_L_t", "_last_L_t")):
        if snap is not None and k in snap:
            v = _fr(snap[k])
        elif core is not None:
            v = _f(core, attr)
        else:
            v = None
        if v is not None:
            out[k] = v
    return out


def collect_engine_state(eng, snap=None) -> dict:
    """Latent state plus supporting observables at the signal instant."""
    if snap is None:
        snap = _snapshot(eng)
    out: dict = {}

    # V2 posterior state snapshot (mirrors forward_tester._v2_entry_snapshot).
    st = _v2_decision(snap, eng).get("state") or {}
    for k in ("x", "mu", "h", "phi", "ell", "var_phi", "regime"):
        if k in st:
            out[f"v2_{k}"] = _r(st[k])
    out.update(_core_trio(snap, eng))

    # Indicator surface shared by every engine family (V1 real, the adapters
    # mirror the V1 names: same values the dashboard displays).
    for attr in ("m_hat", "prev_m_hat", "p_hat", "momentum_acceleration",
                 "signal_strength", "s_effective", "trend_confidence",
                 "ema_fast_val", "ema_slow_val", "ema_macro_val",
                 "ema_spread", "prev_ema_spread", "spread_expanding",
                 "atr_val", "atr_floor", "is_trending",
                 "in_local_chop", "pre_entry_stable"):
        v = _val(snap, eng, attr, None)
        out[attr] = _r(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

    # In-position exit geometry (only meaningful on exit records).
    entry = _fr(_val(snap, eng, "entry_price", None))
    if entry is not None:
        out["entry_price"] = entry
        peak = _fr(_val(snap, eng, "_peak_price", None))
        if peak is not None and entry > 0:
            out["peak_price"] = peak
            out["peak_gain_pct"] = _r((peak / entry - 1.0) * 100.0)

    # Bar / warmup counters.
    for attr in ("bar_count", "trend_bar_count", "exhaustion_bar_count",
                 "reversal_bar_count", "no_motion_count",
                 "_no_long_streak", "_mu_post_neg_count",
                 "_mu_dot_post_sign_neg_count", "_rate_split_streak",
                 "_aoe_streak"):
        v = _val(snap, eng, attr, None)
        if v is not None:
            try:
                out[attr] = int(v)
            except (TypeError, ValueError):
                pass

    # Momentum-over-peak / overextension informational flags.
    for attr in ("_momentum_past_peak_flag", "_price_overextended_flag"):
        v = _val(snap, eng, attr, None)
        if v is not None:
            out[attr] = bool(v)

    # V1-only trend anchor bookkeeping.
    for attr in ("trend_start_bar", "trend_start_price", "trend_start_atr"):
        v = _val(snap, eng, attr, None)
        if v is not None:
            out[attr] = _r(v)

    return out


def _warmup_floor(snap, eng):
    """The warmup gate the record reports (engine rule, fallback re-derived)."""
    if snap is not None and "warmup_floor" in snap:
        return snap["warmup_floor"]
    warmup_bars = _val(snap, eng, "_warmup_bars", None)
    if warmup_bars is not None:
        return max(int(warmup_bars), 400)
    if _val(snap, eng, "warmup", None) is not None:
        w = int(_val(snap, eng, "warmup", 100) or 100)
        return w * 4 if w <= 100 else w
    return None


def _recent_dev_sell(snap, eng) -> bool:
    """Whether a qualifying dev/insider sell sits in the entry window."""
    if snap is not None and "holder_flow_recent_dev_sell" in snap:
        return bool(snap["holder_flow_recent_dev_sell"])
    has_dev = False
    if hasattr(eng, "_has_recent_dev_sell"):
        try:
            has_dev = bool(eng._has_recent_dev_sell(
                int(_val(snap, eng, "_current_time", 0)),
                int(_knob(snap, eng, "_v2_holder_flow_entry_window_seconds", 30) or 30),
                float(_knob(snap, eng, "_v2_holder_flow_min_usd", 100) or 100),
            ))
        except Exception:
            has_dev = False
    return has_dev


def _method_field(snap, eng, key, method_name, threshold, comparison):
    """A field whose live value is an engine method result, if available."""
    if snap is not None and key in snap:
        return _field(snap[key], threshold, comparison)
    if hasattr(eng, method_name):
        try:
            return _field(getattr(eng, method_name)(), threshold, comparison)
        except Exception:
            return None
    return None


def _mu_post_neg_frac(snap, eng):
    """Fraction of the post-entry mu window that is negative (None = no window)."""
    if snap is not None and "mu_post_neg_window_maxlen" in snap:
        maxlen = snap["mu_post_neg_window_maxlen"]
        if maxlen:
            return _val(snap, eng, "_mu_post_neg_count", 0) / maxlen
        return None
    win = getattr(eng, "_mu_post_neg_window", None)
    if win is not None and win.maxlen:
        return getattr(eng, "_mu_post_neg_count", 0) / win.maxlen
    return None


def collect_signal_fields(eng, signal: str, price: float, snap=None) -> dict:
    """Every decision value vs its threshold, in gate-evaluation order.

    BUY records carry the entry-gate ladder; EXIT records carry every
    exit cascade rule that was live-evaluated at the signal instant with
    the values each rule compared against its threshold.
    """
    if snap is None:
        snap = _snapshot(eng)
    fields: dict = {}
    dec = _v2_decision(snap, eng)
    regime_attr = _val(snap, eng, "regime", None)
    regime = getattr(regime_attr, "value", regime_attr)
    c = price

    # Shared observables the entry gate consumes.
    if signal == "buy":
        warmup_floor = _warmup_floor(snap, eng)
        if warmup_floor is not None:
            fields["bar_count"] = _field(
                _val(snap, eng, "bar_count", 0), warmup_floor, ">", None)

        # V1-family entry gates (strategy_engine _passes_entry_filters).
        fields["trend_confidence"] = _field(
            _val(snap, eng, "trend_confidence", None),
            _knob(snap, eng, "entry_confidence_high"), ">=",
        )
        # Macro trend gate: close vs slow EMA.
        fields["ema_macro_gate_price_vs_ema_macro"] = _field(
            c, _knob(snap, eng, "ema_macro_val"), ">=",
        )
        # V1 blow-off guard: overextension AND signal_strength > S_strong.
        fields["signal_strength_vs_S_strong"] = _field(
            _val(snap, eng, "signal_strength", None),
            _knob(snap, eng, "S_strong"), "<=", "blow_off_guard",
        )
        # V1 HVN/VA gate: inside HVN requires signal_strength >= 1.2 x S_strong.
        fields["signal_strength_vs_S_strong_x1.2"] = _field(
            _val(snap, eng, "signal_strength", None),
            None if _knob(snap, eng, "S_strong") is None
            else float(_knob(snap, eng, "S_strong")) * 1.2, ">=",
        )

        # V2 Bayesian entry gate (_v2_passes_entry_gate).
        if "P_up" in dec:
            fields["v2_P_up"] = _field(
                dec.get("P_up"), _knob(snap, eng, "_v2_p_up_min"), ">=",
            )
            fields["v2_E_star"] = _field(dec.get("E_star"), 0.0, ">",
                                        bool(int(dec.get("direction", 0) or 0) == 1
                                             and float(dec.get("E_star", -1) or -1) > 0))
            _dir_v = dec.get("direction")
            fields["v2_direction"] = _field(
                int(_dir_v) if isinstance(_dir_v, (int, float)) else _dir_v,
                1, "==")
        if snap is not None and "v2_sigma_t" in snap:
            sigma_t = _fr(snap["v2_sigma_t"])
        else:
            core = getattr(eng, "core", None)
            sigma_t = _f(core, "_last_sigma_t") if core is not None else None
        if sigma_t is not None or _has(snap, eng, "_v2_sigma_t_min"):
            fields["v2_sigma_t"] = _field(
                sigma_t, _knob(snap, eng, "_v2_sigma_t_min"), ">=",
            )
        # Leading-decay block: neg mu-dot fraction of window.
        window = _knob(snap, eng, "iter05_decay_window")
        thresh = _knob(snap, eng, "iter05_decay_window_thresh")
        if window and thresh is not None:
            if snap is not None and "mu_dot_post_sign_neg_count" in snap:
                neg = snap["mu_dot_post_sign_neg_count"]
            else:
                neg = getattr(getattr(eng, "core", None), "_mu_dot_post_sign_neg_count", 0)
            fields["iter05_mu_dot_neg_frac"] = _field(
                (neg / window) if window else None,
                float(thresh), "<",
            )
        # Market-cap bound (dashboard min/max mcap).
        mcap = _fr(_val(snap, eng, "_market_cap_usd", None))
        if mcap is not None:
            fields["market_cap_usd"] = _field(mcap, None, None)
            lo = _knob(snap, eng, "_mcap_low_usd")
            hi = _knob(snap, eng, "_mcap_high_usd")
            if lo and float(lo) > 0:
                fields["mcap_low_gate"] = _field(mcap, float(lo), ">=")
            if hi and float(hi) > 0:
                fields["mcap_high_gate"] = _field(mcap, float(hi), "<=")

        # Holder-flow entry block: dev-sell presence inside the clear window.
        if _has(snap, eng, "_v2_holder_flow_entry_block"):
            has_dev = _recent_dev_sell(snap, eng)
            fields["holder_flow_entry_window"] = _field(
                "recent_dev_sell_present" if has_dev else "no_recent_dev_sell",
                float(_knob(snap, eng, "_v2_holder_flow_entry_window_seconds", 30) or 30),
                "s_clear",
            )

    elif signal == "exit":
        entry = _fr(_val(snap, eng, "entry_price", None))
        fields["exit_trigger_price"] = _field(c, None, None)
        if entry is not None and entry > 0:
            # 1. Take-profit: c >= entry * (1 + tp/100).
            if snap is not None and "effective_takeprofit_pct" in snap:
                tp_pct = snap["effective_takeprofit_pct"]
            else:
                tp = getattr(eng, "_effective_takeprofit_pct", None)
                tp_pct = tp() if callable(tp) else None
            if tp_pct is not None and float(tp_pct) > 0:
                fields["tp_v2_exit_level"] = _field(
                    c, entry * (1.0 + float(tp_pct) / 100.0), ">=",
                )
            # 2b. Gain-retrace: armed when peak_gain >= arm; exit when
            #     c <= entry * (1 + peak_gain * (1-g)).
            arm = _knob(snap, eng, "_gain_retrace_arm_pct")
            give = _knob(snap, eng, "_gain_retrace_give_frac")
            peak = _fr(_val(snap, eng, "_peak_price", None))
            if peak is not None and arm is not None and give is not None:
                peak_gain = peak / entry - 1.0
                floor_gain = peak_gain * (1.0 - float(give))
                fields["gain_retrace_arm_peak_gain_pct"] = _field(
                    peak_gain * 100.0, float(arm), ">=",
                )
                fields["gain_retrace_exit_level"] = _field(
                    c, entry * (1.0 + floor_gain), "<=",
                )
            # 2c. Breakeven scratch: armed when low <= entry * (1-X/100);
            #     fires when c >= entry * (1+buf/100).
            be_dd = _knob(snap, eng, "_breakeven_arm_dd_pct")
            be_buf = _knob(snap, eng, "_breakeven_buffer_pct")
            if be_dd is not None and be_buf is not None:
                fields["breakeven_scratch_exit_level"] = _field(
                    c, entry * (1.0 + float(be_buf) / 100.0), ">=",
                )
                fields["breakeven_scratch_arm_level"] = _field(
                    _val(snap, eng, "_be_armed", None) and "armed"
                    or "not_armed",
                    float(be_dd), "% dd",
                )
            # 2d. Rate-split: s = kd/(ku+kd) >= theta sustained K.
            if _has(snap, eng, "_v2_rate_split_enable"):
                ku = float(dec.get("k_up", 0.0) or 0.0)
                kd = float(dec.get("k_down", 0.0) or 0.0)
                kt = ku + kd
                split = (kd / kt) if kt > 0.0 else 0.0
                fields["v2_rate_split_s"] = _field(
                    split, _knob(snap, eng, "_v2_rate_split_theta"), ">=",
                )
                fields["v2_rate_split_streak"] = _field(
                    _val(snap, eng, "_rate_split_streak", 0),
                    int(_knob(snap, eng, "_v2_rate_split_persist", 12) or 12), ">=",
                )
            # 5. Kramers down: P_down > P_up, > P_zero, >= 0.5.
            if "P_down" in dec or "P_up" in dec:
                fields["v2_P_down"] = _field(
                    dec.get("P_down"), 0.5, ">=",
                )
                fields["v2_P_down_vs_P_up"] = _field(
                    dec.get("P_down"), dec.get("P_up"), ">",
                )
                fields["v2_P_down_vs_P_zero"] = _field(
                    dec.get("P_down"), dec.get("P_zero"), ">",
                )
            # 6. Bayesian flip: direction != 1 AND E_star > 0.
            _bf_e = dec.get("E_star")
            _bf_e = None if _bf_e is None or float(_bf_e) <= -900.0 else _bf_e
            fields["v2_bayesian_flip_E_star"] = _field(
                _bf_e, 0.0, ">",
                bool(int(dec.get("direction", 0) or 0) != 1
                     and _bf_e is not None and float(_bf_e) > 0),
            )
            # 7. kelly_flat: no-long streak >= K AND >= offside% under water.
            if _has(snap, eng, "_no_long_exit_bars"):
                streak = _val(snap, eng, "_no_long_streak", 0)
                offs_pct = float(_knob(snap, eng, "_no_long_offside_pct", 40) or 40)
                fields["kelly_flat_streak"] = _field(
                    streak, int(_knob(snap, eng, "_no_long_exit_bars", 60) or 60), ">=",
                )
                fields["kelly_flat_offside_level"] = _field(
                    c, entry * (1.0 - offs_pct / 100.0), "<=",
                )
                # mu-persistence guard (default OFF: threshold 0 always passes).
                fields["kelly_flat_mu_neg_frac"] = _field(
                    _mu_post_neg_frac(snap, eng),
                    _knob(snap, eng, "_no_long_mu_neg_frac", 0.0), ">=",
                )
            # 7b. EVR triage: unconfirmed + flow-invalidated + offside.
            if _has(snap, eng, "_v2_evr_enable"):
                peak2 = _fr(_val(snap, eng, "_peak_price", None))
                fields["evr_confirm_level"] = _field(
                    peak2, entry * (1.0 + float(_knob(snap, eng, "_v2_evr_confirm_pct", 10) or 10) / 100.0),
                    "<",
                )
                fields["evr_offside_level"] = _field(
                    c, entry * (1.0 - float(_knob(snap, eng, "_v2_evr_offside_min_pct", 20) or 20) / 100.0),
                    "<=",
                )
                f = _method_field(snap, eng, "evr_trailing_buy_ratio",
                                  "_evr_trailing_buy_ratio",
                                  _knob(snap, eng, "_v2_evr_buy_ratio_max", 0.45), "<")
                if f is not None:
                    fields["evr_trailing_buy_ratio"] = f
                f = _method_field(snap, eng, "evr_maxsec_sell_share",
                                  "_evr_maxsec_sell_share",
                                  _knob(snap, eng, "_v2_evr_skip_sell_conc_min", 0.25), "<=")
                if f is not None:
                    fields["evr_maxsec_sell_share"] = f
            # 6b. AOE (default OFF): captured when enabled.
            if _has(snap, eng, "_v2_aoe_enable"):
                peak3 = _fr(_val(snap, eng, "_peak_price", None))
                pg = ((peak3 / entry - 1.0) * 100.0) if peak3 else None
                fields["aoe_offside_level"] = _field(
                    c, entry * (1.0 - float(_knob(snap, eng, "_v2_aoe_offside_pct", 25) or 25) / 100.0),
                    "<=",
                )
                fields["aoe_unconfirmed_max_peak_pct"] = _field(
                    pg, _knob(snap, eng, "_v2_aoe_max_peak_pct", 5.0), "<",
                )
                ku = float(dec.get("k_up", 0.0) or 0.0)
                kd = float(dec.get("k_down", 0.0) or 0.0)
                kt = ku + kd
                fields["aoe_rate_split_s"] = _field(
                    (kd / kt) if kt > 0.0 else None,
                    _knob(snap, eng, "_v2_aoe_theta", 0.65), ">=",
                )
                fields["aoe_streak"] = _field(
                    _val(snap, eng, "_aoe_streak", 0),
                    int(_knob(snap, eng, "_v2_aoe_persist", 16) or 16), ">=",
                )
            # 3. Reversal-exit persistence: reversal_bar_count >= K.
            rev_k = _knob(snap, eng, "_reversal_exit_bars")
            if rev_k is not None:
                fields["reversal_bar_count"] = _field(
                    _val(snap, eng, "reversal_bar_count", 0), int(rev_k), ">=",
                )

        # 8. Holder-flow dev-sell exit (default OFF).
        if _has(snap, eng, "_v2_holder_flow_exit_enable"):
            fields["holder_flow_exit_window"] = _field(
                "dev_sell_scanned", float(_knob(
                    snap, eng, "_v2_holder_flow_exit_window_seconds", 30) or 30),
                "s_window",
            )

        # Regime at the exit instant (V1-family regime-labeled exits).
        fields["exit_regime"] = _field(str(regime or ""), None, None)

    # V4 post-nuke reversion engine (StrategyEngineV4Adapter).
    cfg = getattr(eng, "cfg", None)
    if isinstance(cfg, dict) and any(k.startswith("v4_") for k in cfg):
        if snap is not None and "drawdown" in snap:
            dd = snap["drawdown"]
        else:
            dd = None
            if hasattr(eng, "_drawdown"):
                try:
                    dd = eng._drawdown()
                except Exception:
                    dd = None
        if signal == "buy":
            if dd is not None:
                fields["v4_drawdown"] = _field(
                    dd, cfg.get("v4_dd_min"), "in_band",
                    bool(float(cfg.get("v4_dd_min", 0)) <= dd <= float(cfg.get("v4_dd_max", 1))))
                fields["v4_dd_band_max"] = _field(dd, cfg.get("v4_dd_max"), "<=")
            fields["v4_peak_mcap"] = _field(
                _val(snap, eng, "_peak_mcap", None), cfg.get("v4_min_peak_mcap"), ">=")
            fields["v4_entries_used"] = _field(
                _val(snap, eng, "_entries_used", 0), cfg.get("v4_max_entries"), "<")
            f = _method_field(snap, eng, "trailing_flow_ratio",
                              "_trailing_flow_ratio", cfg.get("v4_flow_ratio"), ">=")
            if f is not None:
                fields["v4_trailing_flow_ratio"] = f
            fields["v4_bounce_trigger_dd"] = _field(
                dd, None if dd is None
                else float(cfg.get("v4_dd_min", 0)) - float(cfg.get("v4_bounce_depth", 0)),
                "<=")
        else:
            entry = _fr(_val(snap, eng, "entry_price", None))
            if entry is not None and entry > 0:
                fields["v4_tp_exit_level"] = _field(
                    c, entry * (1.0 + float(cfg.get("v4_tp_pct", 0)) / 100.0), ">=")
                fields["v4_jeet_scratch_level"] = _field(
                    c, entry * (1.0 - float(cfg.get("v4_jeet_tol", 0)) / 100.0), "<=")
                trade_age = None
                if _val(snap, eng, "_trade_open_time", None):
                    trade_age = int(_val(snap, eng, "_current_time", 0)) - int(
                        _val(snap, eng, "_trade_open_time"))
                if trade_age is not None:
                    fields["v4_trade_age_vs_jeet_grace_s"] = _field(
                        trade_age, cfg.get("v4_jeet_grace_s"), ">=")
                    fields["v4_trade_age_vs_max_hold_s"] = _field(
                        trade_age, cfg.get("v4_max_hold_s"), ">")

    # V6 dev-sell absorption engine (StrategyEngineV6Adapter).
    if isinstance(cfg, dict) and any(k.startswith("v6_") for k in cfg):
        if signal == "buy":
            p0 = _val(snap, eng, "_event_p0", 0)
            fields["v6_reclaim_price"] = _field(
                c, None if not p0
                else p0 * (1.0 + float(cfg.get("v6_reclaim_pct", 0)) / 100.0),
                ">=")
            fields["v6_window_low_vs_breakdown"] = _field(
                _val(snap, eng, "_window_low", None),
                None if not p0
                else p0 * (1.0 - float(cfg.get("v6_absorb_dd", 0))),
                ">=")
            es = _val(snap, eng, "_event_sol", 0)
            fields["v6_post_sell_vol"] = _field(
                _val(snap, eng, "_post_sell_vol", None),
                None if not es
                else float(cfg.get("v6_post_sell_mult", 0)) * es,
                "<=")
        else:
            entry = _fr(_val(snap, eng, "entry_price", None))
            if entry is not None and entry > 0:
                fields["v6_tp_exit_level"] = _field(
                    c, entry * (1.0 + float(cfg.get("v6_tp_pct", 0)) / 100.0), ">=")
                fields["v6_scratch_exit_level"] = _field(
                    c, entry * (1.0 - float(cfg.get("v6_scratch_tol", 0)) / 100.0), "<=")
                trade_age = None
                if _val(snap, eng, "_trade_open_time", None):
                    trade_age = int(_val(snap, eng, "_current_time", 0)) - int(
                        _val(snap, eng, "_trade_open_time"))
                if trade_age is not None:
                    fields["v6_trade_age_vs_scratch_grace_s"] = _field(
                        trade_age, cfg.get("v6_scratch_grace_s"), ">=")
                    fields["v6_trade_age_vs_max_hold_s"] = _field(
                        trade_age, cfg.get("v6_max_hold_s"), ">")

    return fields


# --- Writer -------------------------------------------------------------------

class SignalCaptureJournal:
    """Appends one JSON record per BUY/EXIT signal to ``signals.jsonl``.

    Owned by LiveTrader; the per-session directory already exists (the
    SessionJournal created it).  All failures are swallowed: signal
    capture must never break trading.
    """

    def __init__(self, session_dir, engine_version: int):
        self.path = Path(session_dir) / "signals.jsonl"
        self.engine_version = int(engine_version)
        self._lock = threading.Lock()

    def record_signal(self, eng, signal: str, reason: str,
                      candle_time: Optional[int] = None,
                      price: Optional[float] = None) -> None:
        now = datetime.now(timezone.utc)
        c = float(price) if price is not None else 0.0
        snap = _snapshot(eng)
        rec = {
            "ts": now.timestamp(),
            "iso": now.isoformat(timespec="milliseconds"),
            "event": f"{signal}_signal",
            "signal": signal,
            "engine_version": self.engine_version,
            "reason": reason,
            "candle_time": candle_time,
            "price": _r(c),
            "fields": collect_signal_fields(eng, signal, c, snap),
            "coefficients": collect_coefficients(eng, snap),
            "engine_state": collect_engine_state(eng, snap),
        }
        line = json.dumps(rec, default=str)
        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            return rec
        except Exception:
            # Logging must never break trading.
            return None


def collect_signal_record(eng, signal: str, reason: str,
                          engine_version: int,
                          candle_time: Optional[int] = None,
                          price: Optional[float] = None) -> dict:
    """Build (but do not persist) one signal record, for tests."""
    c = float(price) if price is not None else 0.0
    snap = _snapshot(eng)
    return {
        "event": f"{signal}_signal",
        "signal": signal,
        "engine_version": engine_version,
        "reason": reason,
        "candle_time": candle_time,
        "price": _r(c),
        "fields": collect_signal_fields(eng, signal, c, snap),
        "coefficients": collect_coefficients(eng, snap),
        "engine_state": collect_engine_state(eng, snap),
    }
