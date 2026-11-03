"""Single source of truth for every engine knob and its default.

One table per engine family.  Each engine module derives its DEFAULT_CONFIG
from its table, so a default value exists exactly once in the repository.
Adapter-only knobs (consumed by engine_kwargs.pop in the V2 adapter, absent
from the engine's own config) live in their own section.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Knob:
    """One configuration knob: its default value and its type."""
    name: str
    default: "float | int | str"
    kind: str  # "float" | "int" | "str"


# V2 engine config: 76 knobs, original key order preserved.
V2_ENGINE_KNOBS: dict[str, Knob] = {
    "lambda_mu": Knob("lambda_mu", 0.15, "float"),
    "kappa_mu": Knob("kappa_mu", 0.05, "float"),
    "sigma_mu": Knob("sigma_mu", 0.1, "float"),
    "eta": Knob("eta", 0.1, "float"),
    "sigma_h": Knob("sigma_h", 0.2, "float"),
    "alpha": Knob("alpha", 0.2, "float"),
    "beta": Knob("beta", 1.0, "float"),
    "sigma_phi": Knob("sigma_phi", 0.15, "float"),
    "theta": Knob("theta", 0.1, "float"),
    "sigma_ell": Knob("sigma_ell", 0.1, "float"),
    "zeta": Knob("zeta", 0.3, "float"),
    "lambda_0": Knob("lambda_0", 6.944444444444444e-05, "float"),
    "lambda_1": Knob("lambda_1", 0.1, "float"),
    "kappa_J": Knob("kappa_J", 0.05, "float"),
    "s_0": Knob("s_0", 0.011, "float"),
    "s_1": Knob("s_1", 0.0005, "float"),
    "n_particles": Knob("n_particles", 200, "int"),
    "n_grid": Knob("n_grid", 200, "int"),
    "grid_sigma_extent": Knob("grid_sigma_extent", 5.0, "float"),
    "tw_window_seconds": Knob("tw_window_seconds", 14400.0, "float"),
    "tau_min": Knob("tau_min", 5.0, "float"),
    "tau_max": Knob("tau_max", 30.0, "float"),
    "tau_step": Knob("tau_step", 5.0, "float"),
    "eps_div": Knob("eps_div", 1.0, "float"),
    "fee_fraction": Knob("fee_fraction", 0.0011, "float"),
    "latency_seconds": Knob("latency_seconds", 0.5, "float"),
    "liquidity_cap_frac": Knob("liquidity_cap_frac", 0.1, "float"),
    "warmup_bars": Knob("warmup_bars", 400, "int"),
    "rng_seed": Knob("rng_seed", 42, "int"),
    "ticks_per_state": Knob("ticks_per_state", 4, "int"),
    "sigma_floor": Knob("sigma_floor", 1e-06, "float"),
    "logprob_floor": Knob("logprob_floor", -50.0, "float"),
    "v2_drift_work_fraction": Knob("v2_drift_work_fraction", 0.0, "float"),
    "v2_evr_enable": Knob("v2_evr_enable", 1.0, "float"),
    "v2_evr_confirm_pct": Knob("v2_evr_confirm_pct", 10.0, "float"),
    "v2_evr_eval_delay": Knob("v2_evr_eval_delay", 120, "int"),
    "v2_evr_grace_seconds": Knob("v2_evr_grace_seconds", 0, "int"),
    "v2_evr_flow_window": Knob("v2_evr_flow_window", 20, "int"),
    "v2_evr_buy_ratio_max": Knob("v2_evr_buy_ratio_max", 0.45, "float"),
    "v2_evr_volume_min_sol": Knob("v2_evr_volume_min_sol", 1.0, "float"),
    "v2_evr_require_offside": Knob("v2_evr_require_offside", 1.0, "float"),
    "v2_evr_offside_min_pct": Knob("v2_evr_offside_min_pct", 20.0, "float"),
    "v2_evr_skip_sell_conc_min": Knob("v2_evr_skip_sell_conc_min", 0.25, "float"),
    "v2_evr_skip_conc_window": Knob("v2_evr_skip_conc_window", 60, "int"),
    "v2_holder_flow_entry_block": Knob("v2_holder_flow_entry_block", 0.0, "float"),
    "v2_holder_flow_exit_enable": Knob("v2_holder_flow_exit_enable", 0.0, "float"),
    "v2_holder_flow_require_tag": Knob("v2_holder_flow_require_tag", 0.0, "float"),
    "v2_holder_flow_min_usd": Knob("v2_holder_flow_min_usd", 100.0, "float"),
    "v2_holder_flow_entry_window_seconds": Knob("v2_holder_flow_entry_window_seconds", 30, "int"),
    "v2_holder_flow_exit_window_seconds": Knob("v2_holder_flow_exit_window_seconds", 15, "int"),
    "v2_rate_split_arm_pct": Knob("v2_rate_split_arm_pct", 10.0, "float"),
    "v2_rate_split_offside_pct": Knob("v2_rate_split_offside_pct", 0.0, "float"),
    "v2_rate_split_theta": Knob("v2_rate_split_theta", 0.55, "float"),
    "v2_rate_split_persist": Knob("v2_rate_split_persist", 12, "int"),
    "v2_rate_split_min_peak_age_ticks": Knob("v2_rate_split_min_peak_age_ticks", 0, "int"),
    "v2_entry_delay_seconds": Knob("v2_entry_delay_seconds", 0.0, "float"),
    "v2_exit_delay_seconds": Knob("v2_exit_delay_seconds", 20.0, "float"),
    "v2_exit_delay_armed_only": Knob("v2_exit_delay_armed_only", 1.0, "float"),
    "v2_percoin_cal_enable": Knob("v2_percoin_cal_enable", 1.0, "float"),
    "v2_percoin_requires_mintcal": Knob("v2_percoin_requires_mintcal", 1.0, "float"),
    "v2_percoin_cal_min_candles": Knob("v2_percoin_cal_min_candles", 120, "int"),
    "v2_percoin_cal_every": Knob("v2_percoin_cal_every", 100, "int"),
    "v2_percoin_cal_interval_seconds": Knob("v2_percoin_cal_interval_seconds", 100.0, "float"),
    "v2_percoin_cal_blend": Knob("v2_percoin_cal_blend", 1.0, "float"),
    "v2_percoin_cal_window": Knob("v2_percoin_cal_window", 600, "int"),
    "v2_harvestcal_enable": Knob("v2_harvestcal_enable", 0.0, "float"),
    "v2_harvest_arm_scale": Knob("v2_harvest_arm_scale", 1.0, "float"),
    "v2_harvest_arm_min": Knob("v2_harvest_arm_min", 4.0, "float"),
    "v2_harvest_arm_max": Knob("v2_harvest_arm_max", 14.0, "float"),
    "v2_percoin_geo_enable": Knob("v2_percoin_geo_enable", 1.0, "float"),
    "v2_percoin_geo_requires_mintcal": Knob("v2_percoin_geo_requires_mintcal", 0.0, "float"),
    "v2_tau_vr_enable": Knob("v2_tau_vr_enable", 1.0, "float"),
    "v2_tau_vr_scale": Knob("v2_tau_vr_scale", 1.0, "float"),
    "v2_popcal_enable": Knob("v2_popcal_enable", 1.0, "float"),
    "v2_mintcal_enable": Knob("v2_mintcal_enable", 1.0, "float"),
    "v2_chain_fetch_enable": Knob("v2_chain_fetch_enable", 1.0, "float"),
}

# V2 adapter-only knobs: consumed via engine_kwargs.pop in the adapter.
V2_ADAPTER_KNOBS: dict[str, Knob] = {
    "takeprofit_pct_low": Knob("takeprofit_pct_low", 30.0, "float"),
    "takeprofit_pct_high": Knob("takeprofit_pct_high", 300.0, "float"),
    "stoploss_pct_low": Knob("stoploss_pct_low", 12.0, "float"),
    "stoploss_pct_high": Knob("stoploss_pct_high", 25.0, "float"),
    "stoploss_pct": Knob("stoploss_pct", 0.0, "float"),
    "takeprofit_pct": Knob("takeprofit_pct", 0.0, "float"),
    "warmup": Knob("warmup", 100, "int"),
    "confidence_high": Knob("confidence_high", 0.79, "float"),
    "confidence_low": Knob("confidence_low", 0.19, "float"),
    "entry_confidence_high": Knob("entry_confidence_high", 0.79, "float"),
    "entry_confidence_low": Knob("entry_confidence_low", 0.19, "float"),
    "confidence_very_high": Knob("confidence_very_high", 0.86, "float"),
    "v2_p_up_min": Knob("v2_p_up_min", 0.62, "float"),
    "v2_sigma_t_min": Knob("v2_sigma_t_min", 0.021, "float"),
    "gain_retrace_arm_pct": Knob("gain_retrace_arm_pct", 10.0, "float"),
    "gain_retrace_give_frac": Knob("gain_retrace_give_frac", 0.5, "float"),
    "v2_debug_tick_log": Knob("v2_debug_tick_log", "", "str"),
    "breakeven_arm_dd_pct": Knob("breakeven_arm_dd_pct", 25.0, "float"),
    "breakeven_buffer_pct": Knob("breakeven_buffer_pct", 2.5, "float"),
    "reversal_exit_bars": Knob("reversal_exit_bars", 2, "int"),
    "no_long_exit_bars": Knob("no_long_exit_bars", 60, "int"),
    "no_long_offside_pct": Knob("no_long_offside_pct", 40.0, "float"),
    "no_long_mu_neg_frac": Knob("no_long_mu_neg_frac", 0.0, "float"),
    "max_entry_bar_count": Knob("max_entry_bar_count", 5700, "int"),
    "forbidden_bc_lo": Knob("forbidden_bc_lo", 2000, "int"),
    "forbidden_bc_hi": Knob("forbidden_bc_hi", 3000, "int"),
    "trail_floor_pct": Knob("trail_floor_pct", 13.0, "float"),
    "reversal_exit_bars_max": Knob("reversal_exit_bars_max", 20, "int"),
    "iter05_decay_window": Knob("iter05_decay_window", 60, "int"),
    "iter05_decay_window_thresh": Knob("iter05_decay_window_thresh", 0.8, "float"),
    "iter05_decay_entry_block": Knob("iter05_decay_entry_block", 1.0, "float"),
    "v2_rate_split_enable": Knob("v2_rate_split_enable", 1.0, "float"),
    "mcap_low_usd": Knob("mcap_low_usd", 0.0, "float"),
    "mcap_high_usd": Knob("mcap_high_usd", 0.0, "float"),
}

# V3 engine config: 54 knobs, original key order preserved.
V3_KNOBS: dict[str, Knob] = {
    "lambda_mu": Knob("lambda_mu", 0.3, "float"),
    "kappa_mu": Knob("kappa_mu", 0.05, "float"),
    "sigma_mu": Knob("sigma_mu", 0.1, "float"),
    "eta": Knob("eta", 0.1, "float"),
    "sigma_h": Knob("sigma_h", 0.2, "float"),
    "alpha": Knob("alpha", 0.3, "float"),
    "beta": Knob("beta", 1.0, "float"),
    "sigma_phi": Knob("sigma_phi", 0.15, "float"),
    "theta": Knob("theta", 0.1, "float"),
    "sigma_ell": Knob("sigma_ell", 0.1, "float"),
    "zeta": Knob("zeta", 0.3, "float"),
    "lambda_0": Knob("lambda_0", 0.0002777777777777778, "float"),
    "lambda_1": Knob("lambda_1", 0.1, "float"),
    "kappa_J": Knob("kappa_J", 0.05, "float"),
    "s_0": Knob("s_0", 0.011, "float"),
    "s_1": Knob("s_1", 0.0005, "float"),
    "n_particles": Knob("n_particles", 200, "int"),
    "n_grid": Knob("n_grid", 200, "int"),
    "grid_sigma_extent": Knob("grid_sigma_extent", 5.0, "float"),
    "tw_window_seconds": Knob("tw_window_seconds", 3600.0, "float"),
    "tau_min": Knob("tau_min", 5.0, "float"),
    "tau_max": Knob("tau_max", 30.0, "float"),
    "tau_step": Knob("tau_step", 5.0, "float"),
    "eps_div": Knob("eps_div", 1.0, "float"),
    "fee_fraction": Knob("fee_fraction", 0.0011, "float"),
    "latency_seconds": Knob("latency_seconds", 0.5, "float"),
    "liquidity_cap_frac": Knob("liquidity_cap_frac", 0.1, "float"),
    "warmup_bars": Knob("warmup_bars", 60, "int"),
    "rng_seed": Knob("rng_seed", 42, "int"),
    "v2_drift_work_fraction": Knob("v2_drift_work_fraction", 0.0, "float"),
    "v3_launch_gain_min_pct": Knob("v3_launch_gain_min_pct", 20.0, "float"),
    "v3_dump_retrace_pct": Knob("v3_dump_retrace_pct", 50.0, "float"),
    "v3_dump_sell_ratio_min": Knob("v3_dump_sell_ratio_min", 0.6, "float"),
    "v3_dump_window_seconds": Knob("v3_dump_window_seconds", 60, "int"),
    "v3_p_up_min": Knob("v3_p_up_min", 0.6, "float"),
    "v3_organic_buy_ratio_min": Knob("v3_organic_buy_ratio_min", 0.6, "float"),
    "v3_organic_window_seconds": Knob("v3_organic_window_seconds", 30, "int"),
    "v3_organic_volume_min_sol": Knob("v3_organic_volume_min_sol", 0.05, "float"),
    "v3_sigma_t_min": Knob("v3_sigma_t_min", 0.01, "float"),
    "v3_mcap_entry_min_usd": Knob("v3_mcap_entry_min_usd", 2000.0, "float"),
    "v3_mcap_entry_max_usd": Knob("v3_mcap_entry_max_usd", 4000.0, "float"),
    "v3_stoploss_pct": Knob("v3_stoploss_pct", 30.0, "float"),
    "v3_takeprofit_pct": Knob("v3_takeprofit_pct", 250.0, "float"),
    "v3_mcap_exit_usd": Knob("v3_mcap_exit_usd", 7500.0, "float"),
    "v3_kramers_down_persist": Knob("v3_kramers_down_persist", 12, "int"),
    "v3_kramers_offside_pct": Knob("v3_kramers_offside_pct", 15.0, "float"),
    "v3_holder_flow_enable": Knob("v3_holder_flow_enable", 1.0, "float"),
    "v3_holder_flow_min_usd": Knob("v3_holder_flow_min_usd", 100.0, "float"),
    "v3_holder_flow_window_seconds": Knob("v3_holder_flow_window_seconds", 30, "int"),
    "confidence_high": Knob("confidence_high", 0.79, "float"),
    "confidence_low": Knob("confidence_low", 0.19, "float"),
    "entry_confidence_high": Knob("entry_confidence_high", 0.79, "float"),
    "entry_confidence_low": Knob("entry_confidence_low", 0.19, "float"),
    "confidence_very_high": Knob("confidence_very_high", 0.86, "float"),
}

# V4 engine config: 24 knobs, original key order preserved.
V4_KNOBS: dict[str, Knob] = {
    "v4_warmup_s": Knob("v4_warmup_s", 120, "int"),
    "v4_min_age_s": Knob("v4_min_age_s", 300, "int"),
    "v4_min_peak_mcap": Knob("v4_min_peak_mcap", 20000, "int"),
    "v4_dd_min": Knob("v4_dd_min", 0.4, "float"),
    "v4_dd_max": Knob("v4_dd_max", 1.0, "float"),
    "v4_bounce_depth": Knob("v4_bounce_depth", 0.03, "float"),
    "v4_flow_window_states": Knob("v4_flow_window_states", 10, "int"),
    "v4_flow_ratio": Knob("v4_flow_ratio", 0.9, "float"),
    "v4_flat_window_s": Knob("v4_flat_window_s", 240, "int"),
    "v4_flat_range_pct": Knob("v4_flat_range_pct", 0.15, "float"),
    "v4_flat_no_new_low_s": Knob("v4_flat_no_new_low_s", 120, "int"),
    "v4_mode": Knob("v4_mode", 'bounce', "str"),
    "v4_tp_pct": Knob("v4_tp_pct", 45.0, "float"),
    "v4_jeet_tol": Knob("v4_jeet_tol", 2.0, "float"),
    "v4_jeet_grace_s": Knob("v4_jeet_grace_s", 20, "int"),
    "v4_max_hold_s": Knob("v4_max_hold_s", 900, "int"),
    "v4_max_entries": Knob("v4_max_entries", 3, "int"),
    "v4_two_stage": Knob("v4_two_stage", 0.0, "float"),
    "confidence_high": Knob("confidence_high", 0.79, "float"),
    "confidence_low": Knob("confidence_low", 0.19, "float"),
    "entry_confidence_high": Knob("entry_confidence_high", 0.79, "float"),
    "entry_confidence_low": Knob("entry_confidence_low", 0.19, "float"),
    "confidence_very_high": Knob("confidence_very_high", 0.86, "float"),
    "warmup_bars": Knob("warmup_bars", 120, "int"),
}

# V6 engine config: 15 knobs, original key order preserved.
V6_KNOBS: dict[str, Knob] = {
    "v6_min_event_usd": Knob("v6_min_event_usd", 100.0, "float"),
    "v6_absorb_window_s": Knob("v6_absorb_window_s", 45, "int"),
    "v6_absorb_dd": Knob("v6_absorb_dd", 0.08, "float"),
    "v6_post_sell_mult": Knob("v6_post_sell_mult", 10.0, "float"),
    "v6_reclaim_pct": Knob("v6_reclaim_pct", 1.0, "float"),
    "v6_tp_pct": Knob("v6_tp_pct", 25.0, "float"),
    "v6_scratch_tol": Knob("v6_scratch_tol", 2.0, "float"),
    "v6_scratch_grace_s": Knob("v6_scratch_grace_s", 20, "int"),
    "v6_max_hold_s": Knob("v6_max_hold_s", 600, "int"),
    "confidence_high": Knob("confidence_high", 0.79, "float"),
    "confidence_low": Knob("confidence_low", 0.19, "float"),
    "entry_confidence_high": Knob("entry_confidence_high", 0.79, "float"),
    "entry_confidence_low": Knob("entry_confidence_low", 0.19, "float"),
    "confidence_very_high": Knob("confidence_very_high", 0.86, "float"),
    "warmup_bars": Knob("warmup_bars", 60, "int"),
}

ENGINE_KNOBS: dict = {
    "v2": V2_ENGINE_KNOBS,
    "v2_adapter": V2_ADAPTER_KNOBS,
    "v3": V3_KNOBS,
    "v4": V4_KNOBS,
    "v6": V6_KNOBS,
}


def family_defaults(family: str) -> dict:
    """The DEFAULT_CONFIG dict for an engine family, in declaration order."""
    return {name: knob.default for name, knob in ENGINE_KNOBS[family].items()}


def v2_default(name: str) -> "float | int | str":
    """Default for a V2 knob, engine config first, adapter section second."""
    knob = V2_ENGINE_KNOBS.get(name) or V2_ADAPTER_KNOBS.get(name)
    if knob is None:
        raise KeyError(f"unknown V2 knob: {name}")
    return knob.default
