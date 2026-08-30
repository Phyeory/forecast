/* ──────────────────────────────────────────────────────────────────────────
   Mizuki Engine  ·  Price Action + Strategy Dashboard
   ────────────────────────────────────────────────────────────────────────── */

const WS_PROTO = location.protocol === "https:" ? "wss:" : "ws:";
const CANDLE_UP = "#26a69a";
const CANDLE_DOWN = "#ef5350";
const CANDLE_FLAT = "#5a6071";
let chartCurrency = "USD";

/* Strategy Engine Parameters — V1 (Physics-based regime detection) */
let engineParamsV1 = {
  ema_fast: 3, ema_slow: 7, atr_period: 7, roc_period: 3, warmup: 100,
  signal_strong: 4, signal_weak: 2, signal_noise: 1.1535714285714287,
  exhaustion_bars_limit: 3, delta_threshold: 0.3, kalman_gamma: 0.125,
  min_trend_bars: 2, reversal_confirm_bars: 2, chop_atr_pct: 0.3,
  chop_spread_pct: 0.05, reversal_exit_confirm_bars: 0,
  s_effective_threshold: 0.5, exhaustion_persist_bars: 6,
  regime_lookback: 6, persistence_threshold: 2, momentum_mean_threshold: 0.0,
  ema_min_spread_pct: 0.02, confidence_high: 0.79, confidence_low: 0.19,
  entry_confidence_high: 0.79, entry_confidence_low: 0.19,
  confidence_w1: 0.3, confidence_w2: 0.25, confidence_w3: 0.25, confidence_w4: 0.2,
  atr_floor_k: 0, ema_cross_persist_bars: 2, exhaustion_s_decay_bars: 1,
  local_range_bars: 80, local_range_threshold_pct: 10, sign_flip_threshold: 0,
  stability_bars: 5,
  spike_atr_multiplier: 1.2,
  spike_lookback_bars: 9,
  exhaustion_stall_bars: 6,
  exhaustion_stall_atr_pct: 3,
  body_baseline_bars: 160,
  overextension_k: 0.08,
  momentum_peak_bars: 1,
  consolidation_range_pct: 25,
  confidence_very_high: 0.86,
  ema_macro_period: 7,
  stoploss_pct: 0,
  takeprofit_pct: 0,
  // Confidence-scaled TP/SL (0 = use static value above)
  takeprofit_pct_low: 20,
  takeprofit_pct_high: 300,
  stoploss_pct_low: 12,
  stoploss_pct_high: 20,
  // Late-recording entry gate: refuse BUY when bar_count > this (0 = disabled)
  max_entry_bar_count: 5700,
  // Forbidden bar_count band: refuse BUY when bar_count in [lo, hi] (0,0 = disabled)
  forbidden_bc_lo: 2000,
  forbidden_bc_hi: 3000,
  // Trailing-stop floor: armed trail_stop never falls below entry * (1 + this%)
  trail_floor_pct: 13,
  reversal_exit_bars_max: 20,
  // LANGEVIN drift discriminator — price-level escape detector.
  // Increases PnL by 0.045 SOL on top of the regime-only baseline by
  // demoting TREND → REVERSAL once `p_hat` (or `c`) has been continuously
  // below `entry * (1 − langevin_drift_pct/100)` for `langevin_drift_stay`
  // consecutive state-updates.  A V-recovery signature (p_hat climbs back
  // above the tripwire) resets the counter — tuned at stay=94 to match
  // the corpus' natural V-recovery timescale.  Set `langevin_drift_stay`
  // very large (>1e6) to fully disable.
  langevin_drift_window: 28,
  langevin_drift_pct: 8.0,
  langevin_drift_stay: 94,
};

/* Strategy Engine Parameters — V2 (RBPF + UKF + KDE + Kramers escape) */
let engineParamsV2 = {
  // ── 16 core SDE free parameters (strategyV2.md §2) ──────────────────
  // Drift μ OU process
  lambda_mu:  0.15,    // μ mean-reversion rate  (larger = faster mean revert)
  kappa_mu:   0.05,    // order-flow coupling φ→μ  (larger = more OF influence)
  sigma_mu:   0.10,    // drift shock std per √s  (larger = noisier μ)
  // Log-variance h Heston-like OU process
  eta:        0.10,    // h mean-reversion rate   (larger = vol reverts faster)
  sigma_h:    0.20,    // log-var shock std       (larger = vol more volatile)
  // Order-flow pressure φ AR(1)
  alpha:      0.20,    // φ mean-reversion rate   (larger = quick decay)
  beta:       1.00,    // signed-delta coefficient (δ/v+ε influence on φ)
  sigma_phi:  0.15,    // φ shock std
  // Liquidity ℓ OU + jump dampener
  theta:      0.10,    // ℓ mean-reversion rate
  sigma_ell:  0.10,    // ℓ shock std
  zeta:       0.30,    // liquidity-jump decay magnitude on ℓ
  // KDE / volume profile decay  (iter16k: T_w = 14400 s structural KDE memory)
  lambda_0:   0.00006944, // KDE exponential decay rate (1/14400s ≈ full recording lifetime)
  lambda_1:   0.10,    // secondary slow-decay component
  // Jump-intensity (Poisson rate per second)
  kappa_J:    0.05,    // λ_J  — governs ℓ jump frequency
  // Execution cost model  s(n,ℓ) = s_0(ℓ) + s_1(ℓ)·n   (iter16h cost-calibration)
  s_0:        0.011,   // base slippage fraction (matched to ~1.11% one-way cost)
  s_1:        0.0005,  // marginal slippage per unit size
  // ── Regime topology tuning knobs ────────────────────────────────────
  regime_mu_star_scale:  0.10, // mu_star multiplier (0.1 = 10% of spec floor)
  regime_phi_star_scale: 1.00, // phi threshold scale factor
  // ── Meta / structural parameters ────────────────────────────────────
  n_particles:        200,   // RBPF particle count  (more = slower, accurate)
  n_grid:             200,   // spatial grid size for U(x,t)
  grid_sigma_extent:  5.0,   // grid half-width in units of σ_t·√T_w
  tw_window_seconds:  14400.0, // KDE memory window T_w (seconds) — iter16k structural KDE memory
  tau_min:            5.0,   // shortest prediction horizon (s)
  tau_max:            30.0,  // longest prediction horizon (s)
  tau_step:           5.0,   // horizon sweep step (s)
  eps_div:            1.0,   // ε in δ_k/(v_k+ε)
  fee_fraction:       0.0011, // fee fraction  (iter16h cost-cal ~0.11%)
  latency_seconds:    0.5,   // execution latency Δ_lat
  liquidity_cap_frac: 0.10,  // Kelly position cap  (0.10 = up to 10% of L_t)
  warmup_bars:        400,   // bars (engine update() intakes) before any decision is emitted (100 full candles = 400 sub-states)
                            // V1 parity: V1 suppresses signals through 400 sub-state updates (100 full candles)
  sigma_floor:        1e-6,  // numerical σ floor
  // ── Shared TP/SL parameters (V1 contract, adapter reads these) ──────
  stoploss_pct: 0,        // Hard-stop removed (iter18b): stoploss_pct=0 disables all price floors.
  takeprofit_pct: 0,
  takeprofit_pct_low: 20,
  takeprofit_pct_high: 300,
  stoploss_pct_low: 12,
  stoploss_pct_high: 20,
  trail_floor_pct: 13,
  // ── V1 confidence / regime-filter pass-through ───────────────────────
  // The V2 adapter inherits V1's well-tuned confidence gating on top of
  // Kramers / KDE checks.  These mirror the V1 defaults.
  warmup: 100,
  confidence_high: 0.79,
  confidence_low: 0.19,
  entry_confidence_high: 0.79,
  entry_confidence_low: 0.19,
  confidence_w1: 0.3, confidence_w2: 0.25, confidence_w3: 0.25, confidence_w4: 0.2,
  confidence_very_high: 0.86,
  regime_lookback: 6,
  persistence_threshold: 2,
  ema_fast: 3, ema_slow: 7, ema_macro_period: 7, atr_period: 7, roc_period: 3,
  max_entry_bar_count: 5700,
  forbidden_bc_lo: 2000,
  forbidden_bc_hi: 3000,
  // ── iter17/18: V2 entry gates + tail-preserving exit overlays ───────
  // Counterfactual-validated on the 404-trade logged-batch (see
  // RESEARCH_LOG.md iter17/18).
  v2_p_up_min:         0.62,   // Bayesian entry floor on P_up (was 0.35)
  v2_sigma_t_min:      0.021,  // entry gate on posterior σ_t (vol floor)
  v2_require_past_peak: 0,     // iter17b REJECTED — keep default off
  gain_retrace_arm_pct:  10,   // arm profit-lock at +10% peak gain (iter18b_opt)
  gain_retrace_give_frac: 0.5, // exit when gain retraces to peak_gain·(1−g) (iter19: 0.6→0.4)
  breakeven_arm_dd_pct:   25,  // arm scratch exit once low ≤ −25% offside (iter18b_opt)
  breakeven_buffer_pct:   2.5, // scratch exit level (entry·(1+buf))
  reversal_exit_bars:     2,   // consecutive REVERSAL bars before exit (iter18b_opt)
  // iter21 — sustained no-long-Kelly exit #7 ("kelly_flat").  Mirror of the
  // V2 entry gate: when direction != +1 AND E_star <= 0 has persisted for
  // `no_long_exit_bars` consecutive ticks AND trade is ≥ `no_long_offside_pct`
  // under water, EXIT (the engine itself asserts the long has no positive
  // Kelly utility).  Production tuning iter21_k60_offs40 (259 trades @ 77.2%
  // WR, +0.884 SOL, PF 1.55; all 5 paired-diff gates cleared vs
  // iter16_baseline_full).  K=20 + offs=-30 caught more losers at higher
  // W→L cost; K=60 + offs=-40 minimises winner cuts to ~1 while still saving
  // ~10 losers.
  no_long_exit_bars:     60,   // consecutive no-long-E_long ticks to fire kelly_flat
  no_long_offside_pct:   40,   // require ≥ 40% underwater to fire kelly_flat (winner-cut guard)
  no_long_mu_neg_frac:   0,    // iter21 hypothesis K REJECTED — μ-persistence guard (off)
  // ── iter45: Pre-entry taker order-flow imbalance gate (TAIL-ACCEPTED) ──
  // Blocks long entries when the trailing taker buy-volume ratio is below
  // `buy_ratio_min` over the last `window_seconds` (only when window volume
  // ≥ `volume_min_sol`; below that the gate passes — parity-safe on
  // low-volume recordings).  Validated r28_w10: 607-recording cohort cuts
  // big losers <-30% 33→23 (Wilcoxon p=0.0054), saves +0.465 SOL total loss
  // drag (p=0.0002), +0.181 SOL whole-cohort PnL, 0 added tail trades ≤-10%.
  // Standard whole-PnL paired_diff REJECTS (p=0.476) — tail-extermination
  // mechanism, judged by tail-focused tests (see RESEARCH_LOG.md Iter 45).
  v2_order_flow_imbalance_gate: 0.0,   // 1.0 = ON (iter45-validated default), 0.0 = OFF
  v2_order_flow_buy_ratio_min:  0.28,  // min taker buy-volume ratio in window
  v2_order_flow_window_seconds: 10,    // trailing window (s)
  v2_order_flow_volume_min_sol: 1.0,   // window volume floor (SOL); below → gate passes

  // Entry-Validation Response triage exit (iter48).  Post-entry evidence: the
  // market's taker-flow response to the engine's own entry.  Exits an
  // UNCONFIRMED (peak never reached entry·(1+confirm_pct/100)), flow-INVALIDATED
  // (trailing buy-ratio < buy_ratio_max), offside position after `eval_delay`
  // seconds.  evr9 production config — full-cohort validated (953 recordings):
  // catastrophics ≤−30% cut 87→76 (p=0.0038), kelly_flat PnL +1.142 SOL (p<0.0001).
  // Set v2_evr_enable=0.0 to disable and restore pre-iter48 behaviour.
  v2_evr_enable:          1.0,   // 1.0 = ON (production default); 0.0 = OFF
  v2_evr_confirm_pct:     10.0,  // confirmation threshold (% above entry); 0% of catastrophics hit +10%
  v2_evr_eval_delay:      120,   // seconds after fill before triage can fire
  v2_evr_grace_seconds:   0,     // >0: one-shot window [delay, delay+grace); 0 = continuous
  v2_evr_flow_window:     20,    // trailing taker-flow window (s)
  v2_evr_buy_ratio_max:   0.45,  // fire when trailing buy-ratio < this
  v2_evr_volume_min_sol:  1.0,   // window volume floor (SOL); below → no triage
  v2_evr_require_offside: 1.0,   // 1.0 = fire only when close < entry
  v2_evr_offside_min_pct: 20.0,  // require close ≤ entry*(1−20/100); winner MAE q10=−16.2%
  // iter50: sell-concentration veto.  When a qualifying EVR tick's trailing
  // sell volume is dominated by one second (whale-sweep print), veto the
  // triage permanently for that trade.  0.25 = ACCEPTED best config (thr=0.25).
  v2_evr_skip_sell_conc_min: 0.25,  // veto when maxsec sell share > this (0 = OFF)
  v2_evr_skip_conc_window:  60,    // trailing window (s) for the share

  // ── iter36/43/56/66: Holder-Flow & Dev Sell Monitoring ───────────────
  // Realtime on-chain trade stream monitoring and dev-wallet tracking.
  // v2_holder_flow_entry_block (1.0 = ON): block BUY entries if a dev or
  // whale sell occurred within `v2_holder_flow_entry_window_seconds`.
  // v2_holder_flow_exit_enable (1.0 = ON): fire immediate dev_sell_exit if
  // a dev or whale sell occurs while in position (checked within exit window).
  // v2_holder_flow_require_tag: 0.0 = any large sell ≥ min_usd qualifies
  // (gate 1.0); 1.0 = require verified insider tag (dev/sniper/bundler/rat_trader).
  v2_holder_flow_entry_block:          1.0,   // 1.0 = ON (production default), 0.0 = OFF
  v2_holder_flow_exit_enable:          1.0,   // 1.0 = ON (production default), 0.0 = OFF
  v2_holder_flow_require_tag:          0.0,   // 0.0 = all large sells (gate 1.0); 1.0 = verified tags only
  v2_holder_flow_min_usd:            100.0,   // Min sell USD threshold to filter dust (default $100)
  v2_holder_flow_entry_window_seconds:  30,   // Lookback window (s) before entry for dev/whale sell
  v2_holder_flow_exit_window_seconds:   15,   // Lookback window (s) in-position for dev/whale sell exit
  // iter56: holder-flow silence gate.  Block entry when tracked wallets have
  // been silent for >= this many seconds (2700.0 = ACCEPTED default; 0 = OFF).
  v2_hf_silence_gate_seconds:          0.0,

  // iter63/64: stationary Kramers rate-split early-harvest exit
  // ("rate_split_flip") — PRODUCTION ON (user decision 2026-08-25 after the
  // iter64 sweep battery: θ=0.55/K=12/arm=10 verified local optimum on all 7
  // perturbed directions; regime gate measured NET-NEGATIVE on the current
  // cohort → gate OFF: full-cohort +2.0739 SOL / 71.8% WR / 1,108 trades vs
  // gated +1.8596; paired Δ+0.155, Wilcoxon p=0.001, breadth 83%).  While
  // armed (peak ≥ entry·(1+arm)), fires when the stationary escape-rate split
  // s = k_down/(k_up+k_down) ≥ theta for `persist` consecutive 4-state ticks.
  // See RESEARCH_LOG.md Iters 63/64.
  v2_rate_split_enable:         1.0,   // 1.0 = ON (production default)
  v2_rate_split_regime_gate:    0.0,   // 0.0 = OFF (measured better than gated)
  v2_rate_split_q_max:          0.6,   // Q(today) >= this → normal day → inert when gated
  v2_rate_split_unknown_q_enable: 1.0, // unknown/missing Q dates → ON
  v2_rate_split_arm_pct:       10.0,   // arm profit-lock trigger at +10% peak
  v2_rate_split_offside_pct:    0.0,   // offside scope REJECTED by CF/screen (0)
  v2_rate_split_theta:          0.55,  // sustained stationary-split threshold
  v2_rate_split_persist:          12,  // consecutive 4-state ticks (≈3 s)
  v2_rate_split_min_peak_age_ticks: 0, // runner-immunity veto REJECTED (0 = off)
};

/* Strategy Engine Parameters — V3 (newborn-coin dump-bottom recovery).
   Mirrors backend/strategy_engineV3.py DEFAULT_CONFIG. */
let engineParamsV3 = {
  // ── V2 core passthrough (RBPF/UKF/KDE/Kramers — same math, newborn-tuned) ──
  lambda_mu:  0.30,   // μ mean-reversion rate (faster than V2 spot: newborns live on a 10-60s clock)
  kappa_mu:   0.05,   // order-flow coupling φ→μ
  sigma_mu:   0.10,   // drift shock std per √s
  eta:        0.10,   // h mean-reversion rate
  sigma_h:    0.20,   // log-var shock std
  alpha:      0.30,   // φ mean-reversion rate (faster: flow shifts in seconds)
  beta:       1.00,   // signed-delta coefficient
  sigma_phi:  0.15,   // φ shock std
  theta:      0.10,   // ℓ mean-reversion rate
  sigma_ell:  0.10,   // ℓ shock std
  zeta:       0.30,   // liquidity-jump decay magnitude
  lambda_0:   0.00027778, // KDE decay rate (1/3600s ≈ 1h newborn memory)
  lambda_1:   0.10,   // secondary slow-decay component
  kappa_J:    0.05,   // jump-intensity Poisson rate
  s_0:        0.011,  // base slippage fraction
  s_1:        0.0005, // marginal slippage per unit size
  n_particles:      200,    // RBPF particle count
  n_grid:           200,    // spatial grid size for U(x,t)
  grid_sigma_extent: 5.0,   // grid half-width in σ_t·√T_w units
  tw_window_seconds: 3600.0, // KDE memory window (s)
  tau_min:    5.0,   // shortest prediction horizon (s)
  tau_max:    30.0,  // longest prediction horizon (s) — V2-validated sweep (τ=10 starved P_zero decay)
  tau_step:   5.0,   // horizon sweep step (s)
  eps_div:    1.0,   // ε in δ_k/(v_k+ε)
  fee_fraction: 0.0011, // fee fraction
  latency_seconds: 0.5,  // execution latency Δ_lat
  liquidity_cap_frac: 0.10, // Kelly position cap
  warmup_bars: 60,    // engine intakes before decisions (15 full candles × 4 states — newborn tapes are short)
  sigma_floor: 1e-6,  // numerical σ floor
  // ── V3 lifecycle: LAUNCH PUMP ────────────────────────────────────────
  v3_launch_gain_min_pct: 20.0,  // launch high ≥ open·(1+20/100) arms the pump leg
  // ── V3 lifecycle: DUMP ──────────────────────────────────────────────
  v3_dump_retrace_pct:    50.0,  // dump low ≤ launch_high·(1−50/100)
  v3_dump_sell_ratio_min: 0.60, // trailing buy ratio ≤ 0.60 marks sell-dominated tape
  v3_dump_window_seconds: 60,   // flow window (s) the sell ratio is read over
  // ── V3 lifecycle: BOTTOM → ORGANIC entry gate ───────────────────────
  v3_p_up_min:               0.60,  // Bayesian floor on P_up (Kramers escape probability)
  v3_organic_buy_ratio_min:  0.60,  // min trailing taker buy-ratio (organic buyers present)
  v3_organic_window_seconds: 30,    // organic flow window (s)
  v3_organic_volume_min_sol: 0.05,  // window volume floor (SOL); below → silence ≠ demand
  v3_sigma_t_min:            0.010, // posterior σ_t floor (barrier geometry resolved)
  v3_mcap_entry_min_usd:     2000,  // entry band floor (user spec: ~2k mcap)
  v3_mcap_entry_max_usd:     4000,  // entry band ceiling (user spec: ~4k mcap)
  // ── V3 STRICT exits ──────────────────────────────────────────────────
  v3_takeprofit_pct: 250.0, // strict TP: bank at +250% (the 2k→8k band is a 3-4× move)
  v3_stoploss_pct:    30.0, // strict SL: cut the dead coin at -30% (survives newborn chop)
  v3_mcap_exit_usd:  7500,  // mcap band exit (user spec: sell ≈ 7-8k)
  // ── V3 supplementary posterior exits (offside-guarded, never cut winners) ──
  v3_kramers_down_persist:     12,    // consecutive down-dominant ticks (≈3 s)
  v3_kramers_offside_pct:      15.0,  // only on trades ≤ -15% offside
  v3_holder_flow_enable:       1.0,   // dev/insider sell exit + entry block
  v3_holder_flow_min_usd:      100.0,
  v3_holder_flow_window_seconds: 30,
};

/* Engine version: 1 = V1 (Physics), 2 = V2 (RBPF/UKF/KDE/Kramers),
   3 = V3 (newborn dump-bottom on the V2 math) */
let engineVersion = 1;

/* Live-trader engine version — independent from the chart engine toggle.
   Persisted across page loads; defaults to V2 (production engine) unless an
   explicit "1" is stored. */
let ltEngineVersion = (() => {
  const v = parseInt(localStorage.getItem("lt_engine_version") || "2", 10);
  return (v === 1 || v === 3) ? v : 2;
})();

/* Active params getter — returns the params for the current engine version */
function getEngineParams() {
  return engineVersion === 2 ? engineParamsV2
       : engineVersion === 3 ? engineParamsV3
       : engineParamsV1;
}
/* Params for live trader (based on its own engine version selector) */
function getLtEngineParams() {
  return ltEngineVersion === 2 ? engineParamsV2
       : ltEngineVersion === 3 ? engineParamsV3
       : engineParamsV1;
}
/* Legacy compat — direct references to `engineParams` throughout the file */
let engineParams = engineParamsV1;

const $ = id => document.getElementById(id);
const settingsModal = $("settings-modal");
const closeSettingsBtn = $("close-settings");
const applySettingsBtn = $("apply-settings-btn");
const settingsForm = $("settings-form");

function formatMcap(v) {
  if (!v || v <= 0) return "—";
  const prefix = chartCurrency === "USD" ? "$" : "";
  const suffix = chartCurrency === "SOL" ? " SOL" : "";
  return prefix + fmtLarge(v) + suffix;
}

function timeframeToSeconds(tf) {
  const m = { "1s": 1, "5s": 5, "15s": 15, "1m": 60, "5m": 300, "15m": 900, "1h": 3600 };
  return m[tf] || 60;
}

function fmtLarge(n) {
  if (n >= 1_000_000_000) return (n / 1_000_000_000).toFixed(2) + "B";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

/* ── Settings Logic ──────────────────────────────────────────────────── */

function renderSettings() {
  settingsForm.innerHTML = "";
  engineParams = getEngineParams();

  // Update engine badge in settings modal
  const badge = document.getElementById("settings-engine-badge");
  if (badge) {
    badge.textContent = "V" + engineVersion;
    badge.className = "engine-badge" + (engineVersion >= 2 ? ` v${engineVersion}` : "");
  }

  // ── Shared param hints (both engines) ──
  const sharedHints = {
    stoploss_pct: "0 = off  |  negative = hard stop (-10 exits at -10% from entry)  |  positive = true trailing stop (10 exits if price falls 10% from its absolute peak since entry)",
    takeprofit_pct: "Take profit at this % gain (0 = disabled, exits position when price hits entry * (1 + pct/100))",
    takeprofit_pct_low: "TP% used when confidence ≤ confidence_low — tighter exit at low conviction (0 = disabled)",
    takeprofit_pct_high: "TP% used when confidence ≥ confidence_high — let winners run at high conviction (0 = disabled)",
    stoploss_pct_low: "SL magnitude (%) at low confidence — wider stop when conviction is low (0 = disabled)",
    stoploss_pct_high: "SL magnitude (%) at high confidence — tighter stop when conviction is high (0 = disabled)",
    confidence_high: "EXIT: exit regime filter upper threshold — below this confidence the regime is 'ambiguous' and no new signals fire (also used as upper bound for TP/SL lerp)",
    confidence_low: "EXIT: exit regime filter lower threshold — below this confidence the engine forces EXHAUSTION and exits (also used as lower bound for TP/SL lerp)",
    entry_confidence_high: "ENTRY: minimum confidence required to open a new position (independent of exit thresholds)",
    entry_confidence_low: "ENTRY: lower confidence floor for entry — entries are blocked below this level (currently a hard gate, not a lerp)",
  };

  // ── V1-specific hints ──
  const v1Hints = {
    ...sharedHints,
    breakout_pct: "Buy when price > VWAP × (1 + breakout_pct/100)",
    vol_spike_mult: "Volume must exceed this × average volume to confirm entry",
    roc_min_pct: "Minimum Rate of Change % to trigger a buy signal",
    trailing_stop_pct: "Trail a stop this % below peak since entry (activates once in profit)",
    hard_stop_pct: "Fixed stop loss: exit if price drops this % from entry",
    max_hold_bars: "Maximum bars to hold a position (0 = disabled)",
    take_profit_pct: "Take profit at this % gain (0 = disabled, use trailing stop)",
    cooldown_bars: "After an exit, wait this many bars before re-entering",
    roc_exit_bars: "Exit if ROC stays negative for this many consecutive bars",
    rsi_overbought: "Block entries when RSI exceeds this threshold",
    langevin_drift_pct: "LANGEVIN drift discriminator tripwire. Demotes TREND → REVERSAL once `min(p_hat, c)` has been continuously below `entry × (1 − pct/100)` for `langevin_drift_stay` consecutive state-updates. Default 8.0 = 8% adverse excursion",
    langevin_drift_stay: "LANGEVIN drift discriminator consecutive-state trigger before regime demotion. Tuned at 94 against the corpus' natural V-recovery timescale (~23s). Lower = more aggressive exits, higher = more tolerant of dips. Set very large (>1e6) to fully disable",
    langevin_drift_window: "Window size (state-updates) for the auxiliary `_langevin_escape_score` analysis helper. Diagnostic-only — not a hard gate. Default 28",
    reversal_exit_bars_max: "Maximum bars the regime machine is allowed to sit in REVERSAL before forcing an exit (`reversal_exit_max` reason). Bounds the loss when the regime machine is slow to confirm a collapse",
  };

  // ── V2-specific hints and section groupings ──
  const v2Hints = {
    ...sharedHints,
    // Drift μ OU
    lambda_mu:  "μ mean-reversion rate — higher = drift decays to zero faster (more mean-reverting)",
    kappa_mu:   "Order-flow coupling φ→μ — higher = order flow imbalance pushes drift harder",
    sigma_mu:   "Drift shock std per √s — higher = drift is noisier / more volatile",
    // Log-variance h OU
    eta:        "h mean-reversion rate — higher = volatility reverts to long-run mean faster",
    sigma_h:    "Log-variance shock std — higher = volatility itself is more volatile (vol-of-vol)",
    // Order-flow φ AR(1)
    alpha:      "φ mean-reversion rate — higher = order-flow pressure dissipates faster",
    beta:       "Signed-delta coefficient — scales the δ_k/(v_k+ε) input into φ (order-flow sensitivity)",
    sigma_phi:  "φ shock std — higher = order-flow can jump more abruptly",
    // Liquidity ℓ
    theta:      "ℓ mean-reversion rate — how fast liquidity returns to normal after a shock",
    sigma_ell:  "ℓ shock std — continuous noise on liquidity",
    zeta:       "Liquidity-jump decay magnitude — size of ℓ drop on a detected jump (volume surge)",
    // KDE
    lambda_0:   "KDE exponential decay rate — 1/T_w where T_w is the memory window. Default 1/300s",
    lambda_1:   "Secondary slow-decay component for the two-timescale KDE",
    kappa_J:    "Jump-intensity Poisson rate per second — governs how often liquidity jumps occur",
    // Execution
    s_0:        "Base slippage fraction (paid on every trade regardless of size)",
    s_1:        "Marginal slippage per unit size — linear impact cost term",
    // Regime
    regime_mu_star_scale:  "mu_star multiplier: lowers the drift threshold below which the engine stays IDLE. 0.1 = 10% of the theoretical floor — increase to be more selective",
    regime_phi_star_scale: "phi threshold scale factor — multiplies the order-flow exit threshold. Higher = require stronger order-flow signal",
    // Meta
    n_particles:        "RBPF particle count — more particles = better posterior, but linearly slower. Minimum 50",
    n_grid:             "Spatial grid resolution for the potential landscape U(x,t)",
    grid_sigma_extent:  "Grid half-width in units of σ_t·√T_w — how far from current price to compute U",
    tw_window_seconds:  "KDE memory window T_w in seconds — price distribution is built from this recent history",
    tau_min:            "Shortest prediction horizon in seconds (minimum horizon swept for Kelly decision)",
    tau_max:            "Longest prediction horizon in seconds (maximum horizon swept)",
    tau_step:           "Horizon sweep step — horizons tau_min, tau_min+step, …, tau_max are all evaluated",
    eps_div:            "ε in δ_k/(v_k+ε) — small constant preventing division by zero in signed-delta ratio",
    fee_fraction:       "Jupiter DEX fee fraction (e.g. 0.001 = 0.1%)",
    latency_seconds:    "Execution latency Δ_lat in seconds — cost of delayed execution is baked into Kelly",
    liquidity_cap_frac: "Kelly position cap as fraction of estimated liquidity L_t (e.g. 0.10 = up to 10%)",
    warmup:             "Full candles to warmup indicators before emitting signals (100 full candles = 400 intra-candle states)",
    warmup_bars:        "Bars (sub-state intakes) before any signal is emitted — allows the RBPF state to stabilise (400 = 100 full candles)",
    sigma_floor:        "Numerical σ floor to prevent division-by-zero in degenerate low-vol regimes",
    // iter45 order-flow gate
    v2_order_flow_imbalance_gate: "iter45 taker order-flow gate — 1.0 = block long entries while taker buy-ratio is below the min (validated r28_w10: big losers <-30% cut 33→23, +0.465 SOL loss-drag saved, p<0.01)",
    v2_order_flow_buy_ratio_min:  "Minimum required taker buy-volume ratio over the trailing window (Σbuy/(Σbuy+Σsell)) — lower = gate fires less often",
    v2_order_flow_window_seconds: "Trailing window (s) over which the taker buy-ratio is computed",
    v2_order_flow_volume_min_sol: "Window volume floor (SOL) — below this the gate passes (parity-safe on low-volume recordings)",
    // iter48/50 EVR triage
    v2_evr_enable:                "1.0 = enable post-entry taker-flow triage (EVR); exits unconfirmed, flow-invalidated offside positions",
    v2_evr_confirm_pct:           "Gain confirmation threshold (% above entry); positions reaching this peak are immune to EVR",
    v2_evr_eval_delay:            "Seconds after fill before EVR triage evaluation begins",
    v2_evr_grace_seconds:         "One-shot evaluation window [delay, delay+grace); 0 = continuous evaluation after delay",
    v2_evr_flow_window:           "Trailing taker-flow window in seconds for computing the post-entry buy ratio",
    v2_evr_buy_ratio_max:         "Taker buy-ratio threshold — fire EVR triage when trailing buy-ratio falls below this value",
    v2_evr_volume_min_sol:        "Minimum window volume in SOL required to evaluate EVR (guard for thin liquidity)",
    v2_evr_require_offside:       "1.0 = fire EVR triage only when current price is below entry price",
    v2_evr_offside_min_pct:       "Minimum drawdown % below entry required for EVR triage to fire",
    v2_evr_skip_sell_conc_min:    "Veto EVR triage when max 1-second sell share exceeds this threshold (filters whale-sweep prints)",
    v2_evr_skip_conc_window:      "Trailing window in seconds for computing single-second sell concentration share",
    // iter36/43/56/66 holder-flow monitoring
    v2_holder_flow_entry_block:          "1.0 = block BUY entries if a dev/insider/whale sell occurred within the entry window (0.0 = disabled)",
    v2_holder_flow_exit_enable:          "1.0 = fire an immediate dev_sell_exit if a dev/insider/whale sell occurs while in position (0.0 = disabled)",
    v2_holder_flow_require_tag:          "0.0 = any large sell ≥ min_usd qualifies (gate 1.0); 1.0 = require verified insider tag (dev/sniper/bundler/rat_trader)",
    v2_holder_flow_min_usd:              "Minimum sell amount in USD to qualify as a significant sell (filters dust transactions)",
    v2_holder_flow_entry_window_seconds: "Pre-entry lookback window in seconds to check for dev/insider sell activity",
    v2_holder_flow_exit_window_seconds:  "In-position lookback window in seconds for dev/insider sell exit triggers",
    v2_hf_silence_gate_seconds:          "Silence entry gate (s): block entry if tracked wallets have been silent for ≥ this many seconds (e.g. 2700 = 45m; 0 = disabled)",
    // iter63/64 Kramers rate-split exit
    v2_rate_split_enable:                "1.0 = enable stationary Kramers rate-split early-harvest exit (rate_split_flip)",
    v2_rate_split_regime_gate:           "1.0 = gate rate-split exit to weak market regimes only (Q < q_max); 0.0 = always active",
    v2_rate_split_q_max:                 "Global regime Q threshold below which weak regime is declared when regime gate is active",
    v2_rate_split_unknown_q_enable:      "1.0 = keep rate-split active on dates with unknown or missing global regime Q",
    v2_rate_split_arm_pct:               "Peak gain % required to arm the rate-split profit-lock trigger",
    v2_rate_split_offside_pct:           "Offside threshold % (0.0 = disabled, profit-taking only)",
    v2_rate_split_theta:                 "Sustained downward escape rate split threshold s = k_down / (k_up + k_down)",
    v2_rate_split_persist:               "Required consecutive 4-state intra-candle ticks (≈ persist / 4 seconds) with split ≥ theta",
    v2_rate_split_min_peak_age_ticks:    "Minimum ticks elapsed since peak price before firing (0 = disabled)",
    v2_whale_dump_exit_enable:           "1.0 = enable whale-dump confirmed exit: candle sell print ≥ min_usd on a never-armed, offside trade, confirmed by price staying under the print close (iter72)",
    v2_whale_dump_min_usd:               "Whale-dump print size floor in USD (candle sell_volume × SOL/USD)",
    v2_whale_dump_offside_pct:            "Offside % at the print close required to arm the whale-dump exit",
    v2_whale_dump_max_peak_pct:           "Never-armed condition: peak gain since entry must stay ≤ this %",
    v2_whale_dump_confirm_s:              "Candles of price persistence below the print close required to confirm the dump",
    v2_whale_dump_confirm_g:              "Confirmation give-back %: confirm close ≤ print close × (1 − g/100)",
  };

  // ── V3 parameter hints (newborn dump-bottom engine) ──
  const v3Hints = {
    lambda_mu:              "μ mean-reversion rate — newborn-tuned (faster than V2 spot)",
    kappa_mu:               "Order-flow coupling (φ - φ̄) → μ",
    sigma_mu:               "Drift shock std per √s",
    eta:                    "h mean-reversion rate (log-variance OU)",
    sigma_h:                "Log-variance shock std",
    alpha:                  "φ mean-reversion rate — faster for newborn flow shifts",
    beta:                   "Signed-delta coefficient (δ_k / (v_k + ε))",
    sigma_phi:              "φ shock std",
    theta:                  "ℓ mean-reversion rate (liquidity OU)",
    sigma_ell:              "ℓ shock std",
    zeta:                   "Liquidity-jump decay magnitude on ℓ",
    lambda_0:               "KDE decay rate (1/3600s ≈ 1h newborn memory)",
    lambda_1:               "Secondary slow-decay component",
    kappa_J:                "Jump-intensity Poisson rate per second",
    s_0:                    "Base slippage fraction (cost model)",
    s_1:                    "Marginal slippage per unit size",
    n_particles:            "RBPF particle count (more = slower, accurate)",
    n_grid:                 "Spatial grid size for U(x,t)",
    grid_sigma_extent:      "Grid half-width in σ_t·√T_w units",
    tw_window_seconds:      "KDE memory window T_w in seconds (newborn: 3600)",
    tau_min:                "Shortest prediction horizon (s)",
    tau_max:                "Longest prediction horizon (s) — V2-validated sweep (τ=10 starved P_zero decay)",
    tau_step:               "Horizon sweep step (s)",
    eps_div:                "ε in δ_k/(v_k+ε)",
    fee_fraction:           "Fee fraction in the Kelly cost model",
    latency_seconds:        "Execution latency Δ_lat (s)",
    liquidity_cap_frac:     "Kelly position cap (fraction of L_t)",
    warmup_bars:            "Engine intakes before decisions (15 full candles × 4 states)",
    sigma_floor:            "Numerical σ floor",
    v3_launch_gain_min_pct:       "LAUNCH phase: pump registered when launch high ≥ open·(1+this%)",
    v3_dump_retrace_pct:          "DUMP phase: confirmed when low ≤ launch_high·(1−this%)",
    v3_dump_sell_ratio_min:       "Sell-side taker dominance: trailing buy-ratio ≤ (1−this)",
    v3_dump_window_seconds:       "Flow window (s) the dump sell ratio is read over",
    v3_p_up_min:                  "Bayesian entry floor on P_up (Kramers upward escape probability)",
    v3_organic_buy_ratio_min:     "Min trailing taker buy-ratio — organic buyers must be present",
    v3_organic_window_seconds:   "Organic flow window (s)",
    v3_organic_volume_min_sol:    "Window volume floor (SOL); below → silence ≠ demand",
    v3_sigma_t_min:               "Posterior σ_t floor (Kramers barrier geometry resolved)",
    v3_mcap_entry_min_usd:        "Entry band floor (USD mcap) — user spec ~2k",
    v3_mcap_entry_max_usd:        "Entry band ceiling (USD mcap) — user spec ~4k",
    v3_takeprofit_pct:            "STRICT take-profit: exit at +this% (fixed level, no posterior veto)",
    v3_stoploss_pct:              "STRICT stop-loss: exit at −this% (dead coin cut at a fixed price)",
    v3_mcap_exit_usd:             "Mcap band exit (USD) — user spec: sell ≈ 7-8k",
    v3_kramers_down_persist:     "Consecutive down-dominant ticks before the supplementary Kramers exit (≈ ticks/4 s)",
    v3_kramers_offside_pct:      "Offside % required before the supplementary Kramers exit may fire",
    v3_holder_flow_enable:       "1.0 = dev/insider sell blocks entry and exits an open position",
    v3_holder_flow_min_usd:      "Min dev/insider sell size (USD) to qualify",
    v3_holder_flow_window_seconds: "Lookback window (s) for dev/insider sell detection",
  };

  // ── V3 section headers ──
  const v3Sections = {
    lambda_mu:              "🌊 Drift μ  (OU process — newborn-tuned)",
    alpha:                  "⚡ Order-Flow Pressure φ  (AR-1)",
    theta:                  "💧 Liquidity ℓ  (OU + Jump)",
    lambda_0:               "📈 KDE  (1h newborn memory)",
    s_0:                   "💸 Execution Cost Model",
    n_particles:           "⚙️ Structural / Meta Parameters",
    tw_window_seconds:     "⏱️ Kramers Horizon (compressed)",
    v3_launch_gain_min_pct: "🚀 Lifecycle — LAUNCH PUMP Detection",
    v3_dump_retrace_pct:   "📉 Lifecycle — DUMP Detection",
    v3_p_up_min:           "🌱 Lifecycle — BOTTOM → ORGANIC Entry Gate",
    v3_takeprofit_pct:     "🛡️ STRICT Exits (TP / SL / Mcap Band)",
    v3_kramers_down_persist: "🧠 Supplementary Posterior Exits",
    v3_holder_flow_enable:  "👥 Holder-Flow (Dev/Insider Sells)",
  };

  // ── V2 section headers — label displayed above first key of each group ──
  const v2Sections = {
    lambda_mu:                    "🌊 Drift μ  (OU process)",
    eta:                          "📉 Log-Variance h  (Heston-like OU)",
    alpha:                        "⚡ Order-Flow Pressure φ  (AR-1)",
    theta:                        "💧 Liquidity ℓ  (OU + Jump)",
    lambda_0:                     "📈 KDE  (Volume Profile Decay)",
    kappa_J:                      "💥 Jump Intensity",
    s_0:                          "💸 Execution Cost Model",
    regime_mu_star_scale:         "🎯 Regime Topology Tuning",
    n_particles:                  "⚙️ Structural / Meta Parameters",
    stoploss_pct:                 "🛡️ Risk Management (TP / SL)",
    warmup:                       "🔗 Confidence Gate  (V1 pass-through)",
    confidence_high:              "🔗 Confidence Gate  (V1 pass-through)",
    ema_fast:                     "📀 EMA / ATR  (V1 indicator pass-through)",
    max_entry_bar_count:          "⏰ Bar-Count Gates",
    v2_p_up_min:                  "🎯 V2 Bayesian & Tail Exit Overlays",
    v2_order_flow_imbalance_gate: "⚡ Taker Order-Flow Gate (iter45)",
    v2_evr_enable:                "🩺 Entry-Validation Response (EVR, iter48/50)",
    v2_holder_flow_entry_block:   "👥 Holder-Flow & Dev Sell Monitoring (iter36/43/56/66)",
    v2_rate_split_enable:         "⚡ Kramers Rate-Split Exit (iter63/64)",
    v2_whale_dump_exit_enable:    "🐋 Whale-Dump Confirmed Exit (iter72)",
  };

  const paramHints = engineVersion === 2 ? v2Hints
                   : engineVersion === 3 ? v3Hints : v1Hints;
  const sectionMap  = engineVersion === 2 ? v2Sections
                   : engineVersion === 3 ? v3Sections : {};
  const renderedSections = new Set();

  for (const [key, val] of Object.entries(engineParams)) {
    // Insert section divider if this key opens a new section
    const sectionTitle = sectionMap[key];
    if (sectionTitle && !renderedSections.has(sectionTitle)) {
      renderedSections.add(sectionTitle);
      const divider = document.createElement("div");
      divider.className = "param-section-header";
      divider.textContent = sectionTitle;
      settingsForm.append(divider);
    }

    const group = document.createElement("div");
    group.className = "param-group";
    const label = document.createElement("label");
    label.className = "param-label";
    label.textContent = key;
    const input = document.createElement("input");
    input.className = "param-input";
    input.dataset.key = key;
    input.value = val;
    input.type = "number";
    input.step = "any";

    group.append(label, input);

    // Append hint if available, and span full width for readability
    if (paramHints[key]) {
      const hint = document.createElement("span");
      hint.className = "param-hint";
      hint.textContent = paramHints[key];
      group.append(hint);
      group.classList.add("full-width");
    }

    settingsForm.append(group);
  }
}

closeSettingsBtn.addEventListener("click", () => {
  settingsModal.classList.add("hidden");
});

settingsModal.addEventListener("click", (e) => {
  if (e.target === settingsModal) settingsModal.classList.add("hidden");
});

applySettingsBtn.addEventListener("click", () => {
  engineParams = getEngineParams();
  const inputs = settingsForm.querySelectorAll(".param-input");
  inputs.forEach(inp => {
    const key = inp.dataset.key;
    const rawVal = inp.value.trim();
    if (rawVal === "") return;
    const num = Number(rawVal);
    if (!isNaN(num)) {
      engineParams[key] = num;
    }
  });
  settingsModal.classList.add("hidden");
});

/* ── Engine version switching ─────────────────────────────────────────── */

function setEngineVersion(v) {
  engineVersion = v;

  // Sync settings-modal toggle
  for (const n of [1, 2, 3]) {
    const btn = document.getElementById(`settings-ver-v${n}`);
    if (btn) btn.classList.toggle("active", v === n);
  }

  // Re-render the settings form live if the modal is currently open
  if (settingsModal && !settingsModal.classList.contains("hidden")) {
    renderSettings();
  } else {
    // Still update the badge even when modal is closed
    const badge = document.getElementById("settings-engine-badge");
    if (badge) {
      badge.textContent = "V" + v;
      badge.className = "engine-badge" + (v >= 2 ? ` v${v}` : "");
    }
  }
}

// Settings-modal toggle buttons
document.querySelectorAll("#settings-engine-toggle .engine-ver-btn").forEach(btn => {
  btn.addEventListener("click", () => setEngineVersion(parseInt(btn.dataset.ver, 10)));
});

/* ── Live-trader engine version switching ─────────────────────────────── */

function setLtEngineVersion(v) {
  ltEngineVersion = v;
  localStorage.setItem("lt_engine_version", String(v));
  for (const n of [1, 2, 3]) {
    const btn = document.getElementById(`lt-engine-v${n}`);
    if (btn) btn.classList.toggle("active", v === n);
  }
}

document.querySelectorAll("#lt-engine-toggle .engine-ver-btn").forEach(btn => {
  btn.addEventListener("click", () => setLtEngineVersion(parseInt(btn.dataset.ver, 10)));
});

// Restore the persisted live-trader engine version selection (default V2).
// Re-applies the active classes so the toggle matches the stored value.
setLtEngineVersion(ltEngineVersion);

/* ════════════════════════════════════════════════════════════════════════
   NEW PAGES: Navigation + Recorder + Viewer + Backtest
   ════════════════════════════════════════════════════════════════════════ */

const API_BASE = `${location.protocol}//${location.host}`;

/* ── Page Navigation ─────────────────────────────────────────────────── */

const navTabs = document.querySelectorAll(".nav-tab");
const pages = document.querySelectorAll(".page");

function switchPage(pageId) {
  pages.forEach(p => p.classList.remove("active"));
  navTabs.forEach(t => t.classList.remove("active"));
  const target = document.getElementById(`page-${pageId}`);
  const tab = document.querySelector(`.nav-tab[data-page="${pageId}"]`);
  if (target) target.classList.add("active");
  if (tab) tab.classList.add("active");

  // Refresh data when switching to pages
  if (pageId === "portfolio") loadPortfolio();
  if (pageId === "recorder") { loadRecordingsList("recordings-list"); checkRecorderStatus(); }
  if (pageId === "viewer") loadRecordingsList("viewer-recordings-list", true);
  if (pageId === "backtest") { loadBacktestsList(); loadRecordingsDropdown(); }
  if (pageId === "new-pairs") { npRefreshPage(); }
}

navTabs.forEach(tab => tab.addEventListener("click", () => switchPage(tab.dataset.page)));

/* ── Shared helpers ──────────────────────────────────────────────────── */

async function apiFetch(path, opts = {}) {
  const res = await fetch(`${API_BASE}${path}`, { headers: { "Content-Type": "application/json" }, ...opts });
  return res.json();
}

function fmtTs(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function fmtDuration(start, end) {
  if (!start || !end) return "—";
  const s = Math.round(end - start);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  return (s / 3600).toFixed(1) + "h";
}

/* ── Recording card HTML ─────────────────────────────────────────────── */

function renderRecordingCard(rec, opts = {}) {
  const statusClass = rec.status === "recording" ? "status-recording" : "status-completed";
  let stopBtn = "";
  if (rec.status === "recording") {
    stopBtn = `<button class="btn btn-danger btn-xs" style="margin-right:4px;" onclick="stopRecording(${rec.id}, event)">⏹ Stop</button>`;
  }
  const actions = opts.viewerMode
    ? `${stopBtn}<button class="btn btn-primary btn-sm" onclick="loadViewer(${rec.id})">📊 View Chart</button>
       <button class="btn btn-danger btn-xs" style="margin-left:4px;" onclick="deleteRecording(${rec.id}, event)">🗑</button>`
    : `${stopBtn}<button class="btn btn-danger btn-xs" onclick="deleteRecording(${rec.id}, event)">🗑</button>`;
  return `
    <div class="recording-card" data-id="${rec.id}">
      <div class="rec-card-header">
        <div><span class="rec-card-name">${rec.token_name || 'Unknown'}</span> <span class="rec-card-symbol">${rec.token_symbol ? '$' + rec.token_symbol : ''}</span></div>
        <div class="rec-card-badges">
          <span class="rec-card-badge">${rec.timeframe}</span>
          <span class="rec-card-badge ${statusClass}">${rec.status}</span>
        </div>
      </div>
      <div class="rec-card-details">
        <span>🕐 ${fmtTs(rec.started_at)}</span>
        <span>📊 ${rec.candle_count} candles</span>
        ${rec.stopped_at ? `<span>⏱ ${fmtDuration(rec.started_at, rec.stopped_at)}</span>` : ''}
      </div>
      <div class="rec-card-mint">${rec.mint || ''}</div>
      <div class="rec-card-actions">${actions}</div>
    </div>`;
}

/* ── Recordings list ─────────────────────────────────────────────────── */

async function loadRecordingsList(containerId, viewerMode = false) {
  const list = await apiFetch("/api/recordings");
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!list.length) {
    el.innerHTML = `<div class="empty-state">No recordings yet.</div>`;
    return;
  }
  el.innerHTML = list.map(r => renderRecordingCard(r, { viewerMode })).join("");
}

async function stopRecording(id, e) {
  if (e) e.stopPropagation();
  await apiFetch("/api/recorder/stop", { method: "POST", body: JSON.stringify({ recording_id: id }) });
  checkRecorderStatus();
  loadRecordingsList("recordings-list");
  loadRecordingsList("viewer-recordings-list", true);
}

async function deleteRecording(id, e) {
  if (e) e.stopPropagation();
  if (!confirm("Delete this recording?")) return;
  await apiFetch(`/api/recordings/${id}`, { method: "DELETE" });
  loadRecordingsList("recordings-list");
  loadRecordingsList("viewer-recordings-list", true);
}

async function cleanupRecordings() {
  if (!confirm("Clean up all recordings with fewer than 100 candles?")) return;
  const res = await apiFetch("/api/recordings/cleanup", { method: "POST" });
  if (res.error) {
    alert(res.error);
    return;
  }
  alert(`Cleaned up ${res.deleted_count || 0} recording(s) with < 100 candles.`);
  loadRecordingsList("recordings-list");
  loadRecordingsList("viewer-recordings-list", true);
  if (typeof loadRecordingsDropdown === "function") {
    loadRecordingsDropdown();
  }
}

async function finishStaleRecordings() {
  if (!confirm("Mark stale recordings as completed? This will finish recordings with no recent candles.")) return;
  const btn = document.getElementById("viewer-fix-stale-btn");
  if (btn) { btn.disabled = true; btn.textContent = 'Working…'; }
  try {
    const res = await apiFetch('/api/recordings/finish_stale', { method: 'POST' });
    if (res && res.fixed) {
      alert(`Fixed ${res.fixed.length} stale recording(s).`);
    } else {
      alert('No stale recordings found.');
    }
  } catch (e) {
    alert('Error fixing stale recordings');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🛠 Fix Stale'; }
    loadRecordingsList("recordings-list");
    loadRecordingsList("viewer-recordings-list", true);
  }
}

/* ── Recorder ────────────────────────────────────────────────────────── */

let recPollTimer = null;

async function checkRecorderStatus() {
  const data = await apiFetch("/api/recorder/status");
  const statusEl = document.getElementById("rec-status");
  const startBtn = document.getElementById("rec-start-btn");
  if (data.active && data.recordings && data.recordings.length) {
    const rec = data.recordings[0];
    statusEl.classList.remove("hidden");
    startBtn.classList.remove("hidden");
    const nameStr = (rec.token_name || rec.mint?.slice(0, 8) || "—") + (rec.token_symbol ? ` $${rec.token_symbol}` : "");
    document.getElementById("rec-status-mint").textContent =
      data.count > 1 ? `${nameStr} +${data.count - 1} more` : nameStr;
    document.getElementById("rec-status-tf").textContent = rec.timeframe;
    document.getElementById("rec-status-candles").textContent = `${rec.candle_count} candles`;
    if (!recPollTimer) recPollTimer = setInterval(checkRecorderStatus, 3000);
  } else {
    statusEl.classList.add("hidden");
    startBtn.classList.remove("hidden");
    if (recPollTimer) { clearInterval(recPollTimer); recPollTimer = null; }
  }
}

document.getElementById("rec-start-btn").addEventListener("click", async () => {
  const mint = document.getElementById("rec-mint-input").value.trim();
  if (!mint) return alert("Enter a token address");
  const tf = document.getElementById("rec-tf-select").value;
  const data = await apiFetch("/api/recorder/start", { method: "POST", body: JSON.stringify({ mint, timeframe: tf }) });
  if (data.error) return alert(data.error);
  document.getElementById("rec-mint-input").value = "";
  checkRecorderStatus();
  loadRecordingsList("recordings-list");
});

/* ── Offline Chart Formatter ─────────── */

async function formatOfflineCandles(mint, rawCandles, timeframeStr) {
  if (!rawCandles || !rawCandles.length) return { candles: [], currency: "SOL" };

  // Find first non-zero open price across all candles to use as base
  let basePrice = 0;
  for (const c of rawCandles) {
    if (c.open > 0) { basePrice = c.open; break; }
    if (c.close > 0) { basePrice = c.close; break; }
  }
  if (!basePrice || basePrice <= 0) basePrice = 1; // last-resort fallback

  let baseMcap = 0;
  let ccy = "SOL";

  try {
    const tInfo = await apiFetch(`/api/token/${mint}`);
    if (tInfo && !tInfo.error) {
      // Determine SOL/USD price
      let solPrice = 160;
      if (tInfo.price_usd && tInfo.price_sol) {
        const pU = parseFloat(tInfo.price_usd);
        const pS = parseFloat(tInfo.price_sol);
        if (pU > 0 && pS > 0) solPrice = pU / pS;
      } else if (tInfo.usd_market_cap && tInfo.market_cap) {
        const sp = tInfo.usd_market_cap / tInfo.market_cap;
        if (sp > 50 && sp < 1000) solPrice = sp;
      }

      if (tInfo.usd_market_cap || tInfo.price_usd) {
        baseMcap = basePrice * 1_000_000_000 * solPrice;
        ccy = "USD";
      } else {
        baseMcap = basePrice * 1e9;
      }
    } else {
      baseMcap = basePrice * 1e9;
    }
  } catch (e) {
    baseMcap = basePrice * 1e9;
  }

  if (!baseMcap || isNaN(baseMcap) || baseMcap <= 0) baseMcap = basePrice * 1e9;

  const toMcap = (p, fallback) => {
    if (!p || p <= 0 || isNaN(p)) return fallback !== undefined ? fallback : 0;
    const v = baseMcap * (p / basePrice);
    if (!isFinite(v) || isNaN(v) || v <= 0) return fallback !== undefined ? fallback : 0;
    return v;
  };

  const tfSec = timeframeToSeconds(timeframeStr);
  const formatted = [];
  let lastTime = null;
  let lastClose = null;
  const seenTimes = new Set(); // deduplicate by time

  for (const c of rawCandles) {
    // Gap fill
    if (lastTime !== null && lastClose !== null && c.time > lastTime + tfSec) {
      const gap = Math.floor((c.time - lastTime) / tfSec) - 1;
      if (gap <= 15) {
        for (let t = lastTime + tfSec; t < c.time; t += tfSec) {
          if (!seenTimes.has(t)) {
            seenTimes.add(t);
            formatted.push({
              time: t,
              open: lastClose, high: lastClose, low: lastClose, close: lastClose,
              volume: 0,
              color: CANDLE_FLAT, borderColor: CANDLE_FLAT, wickColor: CANDLE_FLAT
            });
            lastTime = t;
          }
        }
      }
    }

    if (seenTimes.has(c.time)) continue; // skip duplicate timestamps
    seenTimes.add(c.time);

    const closeVal = toMcap(c.close, lastClose || toMcap(c.open, null));
    if (closeVal === null || closeVal <= 0) continue; // skip unrenderable candle

    let open = lastClose !== null ? lastClose : toMcap(c.open, closeVal);
    let high = toMcap(c.high, closeVal);
    let low = toMcap(c.low, closeVal);
    const close = closeVal;

    // Ensure OHLC is consistent
    high = Math.max(open, high, close);
    low = Math.min(open, low, close);

    let color = CANDLE_FLAT;
    if (close > open) color = CANDLE_UP;
    else if (close < open) color = CANDLE_DOWN;
    else if (lastClose !== null) {
      if (close > lastClose) color = CANDLE_UP;
      else if (close < lastClose) color = CANDLE_DOWN;
    }

    // Preserve backtest-specific fields (trade_action, trade_label, regime…) but only pass OHLCV + color to chart
    formatted.push({
      ...c,         // keep trade_action/trade_label for marker logic
      time: c.time,
      open, high, low, close,
      volume: c.volume || 0,
      color, borderColor: color, wickColor: color,
    });
    lastTime = c.time;
    lastClose = close;
  }

  return { candles: formatted, currency: ccy, baseMcap, basePrice, lastClose, lastTime };
}

/* ── Viewer ───────────────────────────────────────────────────────────── */

let viewerChart = null;

async function loadViewer(recordingId) {
  const rec = await apiFetch(`/api/recordings/${recordingId}`);
  const candles = await apiFetch(`/api/recordings/${recordingId}/candles`);
  if (!candles.length) return alert("No candles in this recording");

  document.getElementById("viewer-select-area").classList.add("hidden");
  document.getElementById("viewer-chart-area").classList.remove("hidden");
  document.getElementById("viewer-token-name").textContent = rec.token_name || "Unknown";
  document.getElementById("viewer-token-symbol").textContent = rec.token_symbol ? `$${rec.token_symbol}` : "";
  document.getElementById("viewer-meta-tf").textContent = rec.timeframe;
  document.getElementById("viewer-meta-candles").textContent = `${candles.length} candles`;

  const wrapper = document.getElementById("viewer-chart");
  wrapper.innerHTML = "";
  viewerChart = LightweightCharts.createChart(wrapper, {
    layout: { background: { color: "#0d0f12" }, textColor: "#5a6071" },
    grid: { vertLines: { color: "#1e2330" }, horzLines: { color: "#1e2330" } },
    timeScale: { borderColor: "#1e2330", timeVisible: true, secondsVisible: true },
    rightPriceScale: { borderColor: "#1e2330" },
    width: wrapper.clientWidth, height: wrapper.clientHeight,
  });

  const formattedData = await formatOfflineCandles(rec.mint, candles, rec.timeframe);
  chartCurrency = formattedData.currency;

  const cs = viewerChart.addCandlestickSeries({
    upColor: CANDLE_UP, downColor: CANDLE_DOWN,
    borderUpColor: CANDLE_UP, borderDownColor: CANDLE_DOWN,
    wickUpColor: CANDLE_UP, wickDownColor: CANDLE_DOWN,
    priceFormat: { type: 'custom', minMove: 1, formatter: p => formatMcap(p) }
  });
  cs.setData(formattedData.candles);

  const vs = viewerChart.addHistogramSeries({ color: "#5865f222", priceFormat: { type: "volume" }, priceScaleId: "vol" });
  viewerChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
  vs.setData(formattedData.candles.map(c => ({ time: c.time, value: c.volume || 0, color: c.close >= c.open ? "#26a69a33" : "#ef535033" })));

  viewerChart.timeScale().fitContent();
  new ResizeObserver(() => viewerChart.applyOptions({ width: wrapper.clientWidth, height: wrapper.clientHeight })).observe(wrapper);
}

document.getElementById("viewer-back-btn").addEventListener("click", () => {
  document.getElementById("viewer-select-area").classList.remove("hidden");
  document.getElementById("viewer-chart-area").classList.add("hidden");
  if (viewerChart) { viewerChart.remove(); viewerChart = null; }
});

/* ── Backtest ─────────────────────────────────────────────────────────── */

async function loadRecordingsDropdown(selectId = "bt-recording-select") {
  const list = await apiFetch("/api/recordings");
  const sel = document.getElementById(selectId);
  sel.innerHTML = `<option value="">— Choose a recording —</option>` +
    list.filter(r => r.status === "completed").map(r =>
      `<option value="${r.id}">${r.token_name || r.mint?.slice(0, 8)} ($${r.token_symbol || '?'}) — ${r.timeframe} — ${r.candle_count} candles</option>`
    ).join("");
}

/* ── Saved backtests list ────────────────────────────────────────────── */

async function loadBacktestsList() {
  const list = await apiFetch(`/api/backtests?market_type=spot`);
  const el = document.getElementById("backtests-list");
  if (!list.length) {
    el.innerHTML = `<div class="empty-state">No backtests yet. Select a recording and run a backtest.</div>`;
    return;
  }

  const singleTests = [];
  const batches = {};
  for (const bt of list) {
    if (bt.batch_id) {
      if (!batches[bt.batch_id]) batches[bt.batch_id] = [];
      batches[bt.batch_id].push(bt);
    } else {
      singleTests.push(bt);
    }
  }

  let html = "";
  const batchGroups = Object.entries(batches).sort((a, b) => b[1][0].created_at - a[1][0].created_at);
  for (const [bId, bItems] of batchGroups) {
    let totalTrades = 0, winningTrades = 0, totalPnl = 0;
    for (const bt of bItems) {
      const trades = bt.total_trades || 0;
      totalTrades += trades;
      winningTrades += Math.round(trades * (bt.win_rate || 0) / 100);
      totalPnl += (bt.total_pnl || 0);
    }
    const overallWinRate = totalTrades > 0 ? (winningTrades / totalTrades * 100) : 0;
    const pnlClass = totalPnl >= 0 ? "pos" : "neg";
    const pnlSign = totalPnl >= 0 ? "+" : "";
    html += `
    <div class="backtest-card batch-folder" onclick="toggleBatchFolder('${bId}')" style="border-left: 4px solid #5865f2; cursor: pointer;">
      <div class="bt-card-header">
        <div><span class="bt-card-name">📁 Batch Run</span> <span class="rec-card-symbol">${bItems.length} coins</span></div>
        <div class="rec-card-badges"><span class="rec-card-badge">Batch</span></div>
      </div>
      <div class="bt-card-stats">
        <div class="bt-stat"><span class="bt-stat-label">Total Trades</span><span class="bt-stat-value">${totalTrades}</span></div>
        <div class="bt-stat"><span class="bt-stat-label">Win Rate</span><span class="bt-stat-value">${overallWinRate.toFixed(1)}%</span></div>
        <div class="bt-stat"><span class="bt-stat-label">Total PnL</span><span class="bt-stat-value ${pnlClass}">${pnlSign}${totalPnl.toFixed(4)} SOL</span></div>
      </div>
      <div class="rec-card-details" style="margin-top:8px"><span>🕐 ${fmtTs(bItems[0].created_at)}</span></div>
      <div class="bt-card-actions"><button class="btn btn-danger btn-xs" onclick="deleteBatch('${bId}', event)">🗑 Delete Batch</button></div>
    </div>
    <div id="batch-items-${bId}" class="batch-items-container hidden" style="margin-left:20px; border-left: 2px dashed #30363d; padding-left:10px; margin-bottom: 10px; display: none;">
      ${bItems.map(bt => renderSingleBacktestCard(bt)).join("")}
    </div>`;
  }
  html += singleTests.map(bt => renderSingleBacktestCard(bt)).join("");
  el.innerHTML = html;
}

/** Single-run card. */
function renderSingleBacktestCard(bt) {
  const pnlClass = bt.total_pnl >= 0 ? "pos" : "neg";
  const pnlSign = bt.total_pnl >= 0 ? "+" : "";
  return `
    <div class="backtest-card" onclick="loadBacktestResult(${bt.id})">
      <div class="bt-card-header">
        <div><span class="bt-card-name">${bt.token_name || bt.mint?.slice(0, 8)}</span> <span class="rec-card-symbol">${bt.token_symbol ? '$' + bt.token_symbol : ''}</span></div>
        <div class="rec-card-badges"><span class="rec-card-badge">${bt.timeframe}</span></div>
      </div>
      <div class="bt-card-stats">
        <div class="bt-stat"><span class="bt-stat-label">Trades</span><span class="bt-stat-value">${bt.total_trades}</span></div>
        <div class="bt-stat"><span class="bt-stat-label">Win Rate</span><span class="bt-stat-value">${(bt.win_rate || 0).toFixed(1)}%</span></div>
        <div class="bt-stat"><span class="bt-stat-label">PnL</span><span class="bt-stat-value ${pnlClass}">${pnlSign}${(bt.total_pnl || 0).toFixed(4)}</span></div>
      </div>
      <div class="rec-card-details" style="margin-top:8px"><span>🕐 ${fmtTs(bt.created_at)}</span></div>
      <div class="bt-card-actions"><button class="btn btn-danger btn-xs" onclick="deleteBacktest(${bt.id}, event)">🗑</button></div>
    </div>`;
}


window.toggleBatchFolder = function (bId) {
  const el = document.getElementById(`batch-items-${bId}`);
  if (el) {
    if (el.style.display === "none" || el.classList.contains("hidden")) {
      el.style.display = "flex";
      el.style.flexDirection = "column";
      el.style.gap = "12px";
      el.classList.remove("hidden");
    } else {
      el.style.display = "none";
      el.classList.add("hidden");
    }
  }
};

async function deleteBatch(bId, e) {
  if (e) e.stopPropagation();
  if (!confirm("Delete this entire batch?")) return;
  await apiFetch(`/api/backtests/batch/${bId}`, { method: "DELETE" });
  loadBacktestsList();
}

async function deleteBacktest(id, e) {
  if (e) e.stopPropagation();
  await apiFetch(`/api/backtests/${id}`, { method: "DELETE" });
  loadBacktestsList();
}

async function deleteAllBacktests() {
  if (!confirm("Are you sure you want to delete ALL backtests?")) return;
  await apiFetch(`/api/backtests`, { method: "DELETE" });
  loadBacktestsList();
}

document.getElementById("bt-run-btn").addEventListener("click", async () => {
  const recId = document.getElementById("bt-recording-select").value;
  if (!recId) return alert("Select a recording first");
  const prog = document.getElementById("bt-progress");
  prog.classList.remove("hidden");
  document.getElementById("bt-run-btn").disabled = true;

  const testerConfig = {
    buy_size_sol: parseFloat(document.getElementById("tester-buy-size").value) || 0.1,
    slippage_pct: parseFloat(document.getElementById("tester-slippage").value) || 1.0,
  };

  try {
    const result = await apiFetch("/api/backtest", {
      method: "POST",
      body: JSON.stringify({
        recording_id: parseInt(recId),
        engine_params: getEngineParams(),
        engine_version: engineVersion,
        ...testerConfig
      })
    });
    if (result.error) { alert(result.error); return; }
    loadBacktestsList();
    loadBacktestResult(result.backtest_id);
  } finally {
    prog.classList.add("hidden");
    document.getElementById("bt-run-btn").disabled = false;
  }
});

document.getElementById("bt-run-all-btn").addEventListener("click", async () => {
  const recordings = await apiFetch("/api/recordings");
  const completed = recordings.filter(r => r.status === "completed");
  if (!completed.length) return alert("No completed recordings to backtest.");

  const prog = document.getElementById("bt-progress");
  const progLabel = document.getElementById("bt-progress-label");
  const runAllBtn = document.getElementById("bt-run-all-btn");
  const runBtn = document.getElementById("bt-run-btn");
  prog.classList.remove("hidden");
  runAllBtn.disabled = true;
  runBtn.disabled = true;
  progLabel.textContent = `Running all ${completed.length} recordings in parallel…`;

  try {
    const testerConfig = {
      buy_size_sol: parseFloat(document.getElementById("tester-buy-size").value) || 0.1,
      slippage_pct: parseFloat(document.getElementById("tester-slippage").value) || 1.0,
      priority_fee: parseFloat(document.getElementById("tester-priority-fee").value) || 0.0001,
      bribe_fee: parseFloat(document.getElementById("tester-bribe-fee").value) || 0.00001
    };

    const result = await apiFetch("/api/backtest/batch", {
      method: "POST",
      body: JSON.stringify({
        engine_params: getEngineParams(),
        engine_version: engineVersion,
        ...testerConfig
      }),
    });
    const msg = `Done: ${result.succeeded}/${result.total} backtests succeeded.`;
    if (result.failed > 0) alert(msg);
    loadBacktestsList();
  } catch (e) {
    alert(`Batch backtest failed: ${e.message || e}`);
  } finally {
    prog.classList.add("hidden");
    runAllBtn.disabled = false;
    runBtn.disabled = false;
    progLabel.textContent = "Running…";
  }
});

document.getElementById("bt-run-last-night-btn").addEventListener("click", async () => {
  // "Last night" window: 10:50 PM local time of the previous calendar day
  // through 12:00 PM (noon) local time of the current day. The backend
  // applies the same window against each recording's started_at timestamp;
  // we pre-compute it here only to preview which recordings will run.
  const now = new Date();
  const todayNoon = new Date(now); todayNoon.setHours(12, 0, 0, 0);
  const yesterday10_50pm = new Date(now); yesterday10_50pm.setDate(now.getDate() - 1); yesterday10_50pm.setHours(22, 50, 0, 0);
  const lo = yesterday10_50pm.getTime() / 1000;
  const hi = todayNoon.getTime() / 1000;

  const recordings = await apiFetch("/api/recordings");
  const lastNight = recordings.filter(r => r.status === "completed" && r.started_at >= lo && r.started_at <= hi);
  if (!lastNight.length) return alert("No completed recordings started last night (10:50 PM prev day – 12 PM today).");

  const prog = document.getElementById("bt-progress");
  const progLabel = document.getElementById("bt-progress-label");
  const lastNightBtn = document.getElementById("bt-run-last-night-btn");
  const runAllBtn = document.getElementById("bt-run-all-btn");
  const runBtn = document.getElementById("bt-run-btn");
  prog.classList.remove("hidden");
  lastNightBtn.disabled = true;
  runAllBtn.disabled = true;
  runBtn.disabled = true;
  progLabel.textContent = `Running last night's ${lastNight.length} recordings in parallel…`;

  try {
    const testerConfig = {
      buy_size_sol: parseFloat(document.getElementById("tester-buy-size").value) || 0.1,
      slippage_pct: parseFloat(document.getElementById("tester-slippage").value) || 1.0,
      priority_fee: parseFloat(document.getElementById("tester-priority-fee").value) || 0.0001,
      bribe_fee: parseFloat(document.getElementById("tester-bribe-fee").value) || 0.00001
    };

    const result = await apiFetch("/api/backtest/batch", {
      method: "POST",
      body: JSON.stringify({
        engine_params: getEngineParams(),
        engine_version: engineVersion,
        recording_ids: lastNight.map(r => r.id),
        last_night: true,
        ...testerConfig
      }),
    });
    const msg = `Done: ${result.succeeded}/${result.total} backtests succeeded.`;
    if (result.failed > 0) alert(msg);
    loadBacktestsList();
  } catch (e) {
    alert(`Last-night batch backtest failed: ${e.message || e}`);
  } finally {
    prog.classList.add("hidden");
    lastNightBtn.disabled = false;
    runAllBtn.disabled = false;
    runBtn.disabled = false;
    progLabel.textContent = "Running…";
  }
});

document.getElementById("bt-run-last-12h-btn").addEventListener("click", async () => {
  // Last 12h = a rolling 12-hour window ending at the moment the button is clicked.
  const hi = Math.floor(Date.now() / 1000);
  const lo = hi - 12 * 60 * 60;

  const recordings = await apiFetch("/api/recordings");
  const last12h = recordings.filter(r => r.status === "completed" && r.started_at >= lo && r.started_at <= hi);
  if (!last12h.length) return alert("No completed recordings started in the last 12 hours.");

  const prog = document.getElementById("bt-progress");
  const progLabel = document.getElementById("bt-progress-label");
  const last12hBtn = document.getElementById("bt-run-last-12h-btn");
  const runAllBtn = document.getElementById("bt-run-all-btn");
  const runBtn = document.getElementById("bt-run-btn");
  prog.classList.remove("hidden");
  last12hBtn.disabled = true;
  runAllBtn.disabled = true;
  runBtn.disabled = true;
  progLabel.textContent = `Running last 12h's ${last12h.length} recordings in parallel…`;

  try {
    const testerConfig = {
      buy_size_sol: parseFloat(document.getElementById("tester-buy-size").value) || 0.1,
      slippage_pct: parseFloat(document.getElementById("tester-slippage").value) || 1.0,
      priority_fee: parseFloat(document.getElementById("tester-priority-fee").value) || 0.0001,
      bribe_fee: parseFloat(document.getElementById("tester-bribe-fee").value) || 0.00001
    };

    const result = await apiFetch("/api/backtest/batch", {
      method: "POST",
      body: JSON.stringify({
        engine_params: getEngineParams(),
        engine_version: engineVersion,
        recording_ids: last12h.map(r => r.id),
        last_12h: true,
        ...testerConfig
      }),
    });
    const msg = `Done: ${result.succeeded}/${result.total} backtests succeeded.`;
    if (result.failed > 0) alert(msg);
    loadBacktestsList();
  } catch (e) {
    alert(`Last 12h batch backtest failed: ${e.message || e}`);
  } finally {
    prog.classList.add("hidden");
    last12hBtn.disabled = false;
    runAllBtn.disabled = false;
    runBtn.disabled = false;
    progLabel.textContent = "Running…";
  }
});

document.getElementById("bt-params-btn").addEventListener("click", () => {
  renderSettings();
  settingsModal.classList.remove("hidden");
});


/* ── Backtest result detail ──────────────────────────────────────────── */

let btChart = null;

async function loadBacktestResult(id) {
  const bt = await apiFetch(`/api/backtests/${id}`);
  if (!bt || bt.error) return alert("Failed to load backtest");

  document.getElementById("bt-controls").classList.add("hidden");
  document.querySelector("#page-backtest .backtests-section").classList.add("hidden");
  document.getElementById("bt-result-area").classList.remove("hidden");

  document.getElementById("bt-result-name").textContent = `${bt.token_name || bt.mint?.slice(0, 8)} ${bt.token_symbol ? '$' + bt.token_symbol : ''}`;
  document.getElementById("bt-result-tf").textContent = bt.timeframe;
  renderBacktestStatsGrid(bt);
  await renderBacktestChart(bt);
  renderBacktestTradesTable(bt);
}

function renderBacktestStatsGrid(bt) {
  const s = bt.summary_json || {};
  const el = document.getElementById("bt-stats-grid");
  const pnlC = s.total_pnl_sol >= 0 ? "pos" : "neg";
  const rows = [
    { l: "Total Trades", v: s.total_trades || 0 },
    { l: "Win Rate", v: `${(s.win_rate || 0).toFixed(1)}%` },
    { l: "Total PnL", v: `${s.total_pnl_sol >= 0 ? '+' : ''}${(s.total_pnl_sol || 0).toFixed(4)} SOL`, c: pnlC },
    { l: "Final Balance", v: `${(s.current_balance || 1).toFixed(4)} SOL` },
    { l: "Max Drawdown", v: `${(s.max_drawdown_pct || 0).toFixed(2)}%`, c: "neg" },
    { l: "Fees Paid", v: `${(s.total_fees_paid || 0).toFixed(4)} SOL` },
  ];
  el.innerHTML = rows.map(x =>
    `<div class="bt-stats-card"><div class="bt-stats-card-label">${x.l}</div><div class="bt-stats-card-value ${x.c || ''}">${x.v}</div></div>`
  ).join("");
}

async function renderBacktestChart(bt) {
  const wrapper = document.getElementById("bt-chart");
  wrapper.innerHTML = "";
  if (btChart) btChart.remove();
  btChart = LightweightCharts.createChart(wrapper, {
    layout: { background: { color: "#0d0f12" }, textColor: "#5a6071" },
    grid: { vertLines: { color: "#1e2330" }, horzLines: { color: "#1e2330" } },
    timeScale: { borderColor: "#1e2330", timeVisible: true, secondsVisible: true },
    rightPriceScale: { borderColor: "#1e2330" },
    width: wrapper.clientWidth, height: wrapper.clientHeight,
  });

  let candles = bt.candles || [];
  // Batch runs (Run All / Last Night / Last 12h) are persisted without
  // backtest_candles rows — fall back to the recording's candle stream.
  if (!candles.length && bt.recording_id) {
    try {
      candles = await apiFetch(`/api/recordings/${bt.recording_id}/candles`) || [];
    } catch (e) {
      console.warn("Failed to load recording candles for backtest", bt.recording_id, e);
    }
  }
  const formattedData = await formatOfflineCandles(bt.mint, candles, bt.timeframe);
  chartCurrency = formattedData.currency;

  const cs2 = btChart.addCandlestickSeries({
    upColor: CANDLE_UP, downColor: CANDLE_DOWN,
    borderUpColor: CANDLE_UP, borderDownColor: CANDLE_DOWN,
    wickUpColor: CANDLE_UP, wickDownColor: CANDLE_DOWN,
    priceFormat: { type: 'custom', minMove: 1, formatter: p => formatMcap(p) }
  });
  cs2.setData(formattedData.candles);

  const vs = btChart.addHistogramSeries({ color: "#5865f222", priceFormat: { type: "volume" }, priceScaleId: "vol" });
  btChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
  vs.setData(formattedData.candles.map(c => ({ time: c.time, value: c.volume || 0, color: c.close >= c.open ? "#26a69a33" : "#ef535033" })));

  // Markers
  const btMarkers = [];
  for (const c of formattedData.candles) {
    if (c.trade_action === "buy") {
      btMarkers.push({ time: c.time, position: "belowBar", color: CANDLE_UP, shape: "arrowUp", text: `BUY @ ${formatMcap(c.open)}` });
    } else if (c.trade_action === "exit") {
      btMarkers.push({ time: c.time, position: "aboveBar", color: CANDLE_DOWN, shape: "circle", text: `EXIT @ ${formatMcap(c.open)}` });
    }
  }
  // Batch runs (Run All / Last Night / Last 12h) persist no backtest_candles
  // rows, so the recording-candle fallback has no trade_action — build
  // markers from the always-present trades array instead.
  if (!btMarkers.length && bt.trades && bt.trades.length) {
    const toMcap = (p) => {
      if (!p || p <= 0 || isNaN(p)) return 0;
      if (formattedData.baseMcap && formattedData.basePrice) return formattedData.baseMcap * (p / formattedData.basePrice);
      return p;
    };
    for (const t of bt.trades) {
      if (t.entry_time) {
        btMarkers.push({ time: t.entry_time, position: "belowBar", color: CANDLE_UP, shape: "arrowUp", text: `BUY @ ${formatMcap(toMcap(t.entry_price))}` });
      }
      if (t.exit_time) {
        btMarkers.push({ time: t.exit_time, position: "aboveBar", color: CANDLE_DOWN, shape: "circle", text: `EXIT @ ${formatMcap(toMcap(t.exit_price))}` });
      }
    }
  }
  if (btMarkers.length) cs2.setMarkers(btMarkers.sort((a, b) => a.time - b.time));

  btChart.timeScale().fitContent();
  new ResizeObserver(() => btChart.applyOptions({ width: wrapper.clientWidth, height: wrapper.clientHeight })).observe(wrapper);
}

function renderBacktestTradesTable(bt) {
  const tbody = document.getElementById("bt-trades-tbody");
  const trades = bt.trades || [];
  tbody.innerHTML = trades.map((t, i) => {
    const pnlClass = t.pnl_sol >= 0 ? "trade-pnl-pos" : "trade-pnl-neg";
    return `<tr>
      <td>${i + 1}</td>
      <td>${fmtTs(t.entry_time)}</td>
      <td>${t.entry_price?.toExponential(4) || '—'}</td>
      <td>${fmtTs(t.exit_time)}</td>
      <td>${t.exit_price?.toExponential(4) || '—'}</td>
      <td class="${pnlClass}">${t.pnl_sol >= 0 ? '+' : ''}${t.pnl_sol?.toFixed(6) || '0'}</td>
      <td class="${pnlClass}">${t.pnl_pct >= 0 ? '+' : ''}${t.pnl_pct?.toFixed(2) || '0'}%</td>
      <td>${t.entry_reason || '—'}</td>
      <td>${t.exit_reason || '—'}</td>
    </tr>`;
  }).join("");
}

document.getElementById("bt-result-back-btn").addEventListener("click", () => {
  document.getElementById("bt-controls").classList.remove("hidden");
  document.querySelector("#page-backtest .backtests-section").classList.remove("hidden");
  document.getElementById("bt-result-area").classList.add("hidden");
  if (btChart) { btChart.remove(); btChart = null; }
});

window.btLoadBacktestResult = loadBacktestResult;

// Make functions globally available for onclick handlers
window.loadViewer = loadViewer;
window.deleteRecording = deleteRecording;
window.loadBacktestResult = loadBacktestResult;
window.deleteBacktest = deleteBacktest;
window.deleteAllBacktests = deleteAllBacktests;
window.deleteBatch = deleteBatch;

/* ════════════════════════════════════════════════════════════════════════
   LIVE TRADING — Real on-chain execution via Phantom + Jupiter
   ════════════════════════════════════════════════════════════════════════ */

const JUPITER_QUOTE = "https://lite-api.jup.ag/swap/v1/quote";
const JUPITER_SWAP = "https://lite-api.jup.ag/swap/v1/swap";
const WSOL = "So11111111111111111111111111111111111111112";
const SOL_DECIMALS = 9;
const LT_WS_BASE = `${WS_PROTO}//${location.host}/ws/live`;

/* ── State ────────────────────────────────────────────────────────────── */

let ltWalletPubkey = null;
let ltWalletConnected = false;
const ltActiveTraders = {};  // mint -> { ws, info, events[] }
let ltTradeCounter = 0;

/* ── DOM refs ─────────────────────────────────────────────────────────── */

const ltConnectBtn = $("lt-connect-btn");
const ltWalletDot = $("lt-wallet-dot");
const ltWalletLabel = $("lt-wallet-label");
const ltWalletAddr = $("lt-wallet-addr");
const ltWalletBal = $("lt-wallet-bal");
const ltAddBtn = $("lt-add-btn");
const ltStopAllBtn = $("lt-stop-all-btn");
const ltTokenInput = $("lt-token-input");
const ltTradersGrid = $("lt-traders-grid");
const ltTradesTbody = $("lt-trades-tbody");

/* ── Wallet Setup (Private Key) ────────────────────────────────────────── */

let _privateKey = localStorage.getItem("lt_private_key") || "";

async function connectWallet() {
  const pkInput = $("lt-private-key").value.trim();
  if (!pkInput) return alert("Please enter your base58 private key.");
  if (pkInput.length < 32) return alert("Private key seems too short. Expected a base58 string.");

  try {
    const res = await apiFetch("/api/live/private_key", {
      method: "POST",
      body: JSON.stringify({ private_key: pkInput, connected: true }),
    });
    if (res.error) return alert("Error saving private key: " + res.error);

    _privateKey = pkInput;
    localStorage.setItem("lt_private_key", pkInput);
    ltWalletPubkey = res.pubkey || "connected";
    ltWalletConnected = true;

    ltWalletDot.className = "dot connected";
    ltWalletLabel.textContent = "Key Set";
    ltWalletAddr.textContent = res.pubkey ? `${res.pubkey.slice(0, 6)}…${res.pubkey.slice(-4)}` : "(Server-side signing)";
    ltWalletBal.textContent = "";
    ltConnectBtn.textContent = "✅ Key Saved";
    ltAddBtn.disabled = false;

    $("lt-private-key").value = "";
    $("lt-private-key").placeholder = "Key securely saved in backend memory.";
    if (typeof afUpdateToggleGate === "function") afUpdateToggleGate();
  } catch (e) {
    alert("Failed to connect wallet: " + e);
  }
}

async function initWalletState() {
  try {
    const status = await apiFetch("/api/live/private_key");
    if (status && status.connected) {
      ltWalletConnected = true;
      ltWalletPubkey = status.pubkey || "connected";
      ltWalletDot.className = "dot connected";
      ltWalletLabel.textContent = "Key Set";
      ltWalletAddr.textContent = status.pubkey ? `${status.pubkey.slice(0, 6)}…${status.pubkey.slice(-4)}` : "(Server-side signing)";
      ltConnectBtn.textContent = "✅ Key Saved";
      ltAddBtn.disabled = false;
      if (typeof afRestoreState === "function") {
        afRestoreState();
      } else if (typeof afUpdateToggleGate === "function") {
        afUpdateToggleGate();
      }
    }
  } catch (e) { }
}
initWalletState();

ltConnectBtn.addEventListener("click", connectWallet);

/* ── Get config values ───────────────────────────────────────────────── */

function getLtConfig() {
  // Buy size comes ONLY from the input field.  An empty/invalid field yields
  // NaN — every consumer treats `buySize > 0` as the validity gate, and no
  // fallback default exists anywhere (frontend or backend).
  const buySizeRaw = parseFloat($("lt-buy-size").value);
  return {
    buySize: buySizeRaw > 0 ? buySizeRaw : NaN,
    slippagePct: parseFloat($("lt-slippage").value) || 10,
    slippageBps: Math.round((parseFloat($("lt-slippage").value) || 10) * 100),
    priorityFeeSol: 0.0001,               // fixed: 0.0001 SOL per transaction
    priorityFeeLamports: 100000,          // fixed: 100,000 micro-lamports
    timeframe: $("lt-timeframe").value,
  };
}

/* Push the input-field buy size to the backend store (POST /api/live/buy_size).
   This is how headless paths (autofeed server-side spawns) learn the size.
   An invalid field is never pushed — the backend has no default to fall back
   to, by design. */
function pushLtBuySize() {
  const v = parseFloat($("lt-buy-size").value);
  if (!(v > 0)) return;
  apiFetch("/api/live/buy_size", { method: "POST", body: JSON.stringify({ buy_size: v }) })
    .catch(() => { /* backend unreachable — retried on next change/init */ });
}

/* ── Legacy swap handler removed (moves to backend) ───────────────── */

/* ── Trader event log ────────────────────────────────────────────────── */

function addTraderEvent(ctx, type, msg) {
  const ts = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  ctx.events.unshift({ type, msg, ts });
  if (ctx.events.length > 50) ctx.events.pop();
  updateTraderCard(ctx.mint);
}

/* ── Trade history table ─────────────────────────────────────────────── */

function renderLtTradeTable(trades) {
  if (!ltTradesTbody || !Array.isArray(trades)) return;
  ltTradesTbody.innerHTML = "";
  ltTradeCounter = 0;
  for (const t of trades) {
    ltTradeCounter++;
    const tr = document.createElement("tr");
    const pnlSol = t.pnl_sol || 0;
    const pnlPct = t.pnl_pct || 0;
    const pnlClass = pnlSol >= 0 ? "trade-pnl-pos" : "trade-pnl-neg";
    const ts = t.timestamp ? new Date(t.timestamp * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
    const txHash = t.tx_hash || "";
    const txCell = txHash
      ? `<a href="https://solscan.io/tx/${txHash}" target="_blank" style="color:var(--accent);text-decoration:none">${txHash.slice(0, 8)}…</a>`
      : "—";
    const action = (t.action || "BUY").toUpperCase();
    const tokenSymbol = t.token_symbol ? "$" + t.token_symbol : (t.mint ? t.mint.slice(0, 6) + "…" : "—");
    const price = t.price || 0;
    tr.innerHTML = `
      <td>${ltTradeCounter}</td>
      <td>${tokenSymbol}</td>
      <td style="color:${action === "BUY" ? "var(--green)" : "var(--red)"}; font-weight:700">${action}</td>
      <td>${ts}</td>
      <td>${price ? price.toExponential(4) : "—"}</td>
      <td class="${pnlClass}">${action === "SELL" ? (pnlSol >= 0 ? "+" : "") + pnlSol.toFixed(6) : "—"}</td>
      <td class="${pnlClass}">${action === "SELL" ? (pnlPct >= 0 ? "+" : "") + pnlPct.toFixed(2) + "%" : "—"}</td>
      <td>${txCell}</td>
      <td>${t.status || "confirmed"}</td>
    `;
    ltTradesTbody.prepend(tr);
  }
}

function addLtTradeRow(ctx, action, price, pnlSol, pnlPct, txHash, status) {
  ltTradeCounter++;
  const tr = document.createElement("tr");
  const pnlClass = pnlSol >= 0 ? "trade-pnl-pos" : "trade-pnl-neg";
  const ts = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const txCell = txHash
    ? `<a href="https://solscan.io/tx/${txHash}" target="_blank" style="color:var(--accent);text-decoration:none">${txHash.slice(0, 8)}…</a>`
    : "—";
  tr.innerHTML = `
    <td>${ltTradeCounter}</td>
    <td>${ctx.info?.token_symbol ? "$" + ctx.info.token_symbol : (ctx.mint ? ctx.mint.slice(0, 6) + "…" : "—")}</td>
    <td style="color:${action === "BUY" ? "var(--green)" : "var(--red)"}; font-weight:700">${action}</td>
    <td>${ts}</td>
    <td>${price ? price.toExponential(4) : "—"}</td>
    <td class="${pnlClass}">${action === "SELL" ? (pnlSol ? (pnlSol >= 0 ? "+" : "") + pnlSol.toFixed(6) : "—") : "—"}</td>
    <td class="${pnlClass}">${action === "SELL" ? (pnlPct ? (pnlPct >= 0 ? "+" : "") + pnlPct.toFixed(2) + "%" : "—") : "—"}</td>
    <td>${txCell}</td>
    <td>${status}</td>
  `;
  ltTradesTbody.prepend(tr);
}

/* ── Session stats bar (aggregate across server sessions) ───────────────
   The server owns the ground-truth session_summary (in-memory: it survives
   page refreshes and resets when the program restarts).  The frontend caches
   the last summary and re-renders it on live events instead of recomputing
   from per-card stats — recomputing from the local cards reset the bar to 0
   right after a page refresh (freshly attached cards start with empty stats)
   and silently dropped completed (stopped) sessions. */
let _ltServerSummary = null;          // last session_summary from /api/live/status
let _summaryRefreshTimer = null;      // debounce for post-trade aggregate refresh

function scheduleSessionSummaryRefresh() {
  if (_summaryRefreshTimer) return;
  _summaryRefreshTimer = setTimeout(() => {
    _summaryRefreshTimer = null;
    apiFetch("/api/live/status")
      .then(st => { if (st && st.session_summary) updateSessionStats(st.session_summary); })
      .catch(() => { /* keep last cached summary */ });
  }, 1200);
}

function updateSessionStats(summary = null, serverTraders = null) {
  const el = $("lt-session-card");
  if (!el) return;

  if (summary) _ltServerSummary = summary;

  let pnl = 0, upnl = 0, wr = 0, trades = 0, tokens = 0;

  if (_ltServerSummary) {
    pnl = _ltServerSummary.total_pnl_sol || 0;
    upnl = _ltServerSummary.unrealized_pnl_sol || 0;
    wr = _ltServerSummary.win_rate || 0;
    trades = _ltServerSummary.total_trades || 0;
    tokens = _ltServerSummary.tokens_traded || 0;
  } else {
    // No server round-trip yet (very first page load) — approximate from the
    // attached cards so a just-started session isn't blank.
    const traders = serverTraders || Object.values(ltActiveTraders);
    let wins = 0;
    for (const t of traders) {
      const st = t.stats || {};
      pnl += st.total_pnl_sol || 0;
      upnl += t.unrealizedPnl || 0;
      wins += st.winning_trades || 0;
      trades += st.total_trades || 0;
    }
    wr = trades > 0 ? (wins / trades) * 100 : 0;
    tokens = traders.length;
  }

  // A live trade / detach / attach event means the aggregate may have moved —
  // refresh it from the server shortly (debounced).  Only the summary path
  // renders the authoritative numbers.
  if (!summary) scheduleSessionSummaryRefresh();

  if (trades === 0 && tokens === 0 && Object.keys(ltActiveTraders).length === 0) {
    el.style.display = "none";
    return;
  }
  el.style.display = "";

  const cls = v => (v >= 0 ? "pos" : "neg");
  const fmt = v => `${v >= 0 ? "+" : ""}${v.toFixed(4)} SOL`;

  const pnlEl = $("lts-pnl"), upnlEl = $("lts-upnl");
  pnlEl.textContent = fmt(pnl); pnlEl.className = "bt-stat-value " + cls(pnl);
  upnlEl.textContent = fmt(upnl); upnlEl.className = "bt-stat-value " + cls(upnl);
  $("lts-wr").textContent = `${wr.toFixed(1)}%`;
  $("lts-trades").textContent = trades;
  $("lts-tokens").textContent = tokens;
}

/* ── Trader card rendering ───────────────────────────────────────────── */

function updateTraderCard(mint) {
  const ctx = ltActiveTraders[mint];
  if (!ctx) return;

  let card = document.querySelector(`.lt-trader-card[data-mint="${mint}"]`);
  let isNew = false;
  if (!card) {
    card = document.createElement("div");
    card.className = "lt-trader-card";
    card.dataset.mint = mint;

    // Remove empty state if present
    const empty = ltTradersGrid.querySelector(".empty-state");
    if (empty) empty.remove();

    card.innerHTML = `
      <div class="lt-card-header">
        <div><span class="lt-card-name" id="lth-name-${mint}"></span><span class="lt-card-symbol" id="lth-sym-${mint}"></span></div>
        <div style="display:flex;gap:6px;align-items:center">
          <span class="engine-badge${ctx.engineVersion >= 2 ? ` v${ctx.engineVersion}` : ''}" title="Strategy engine">${ctx.engineVersion ? `V${ctx.engineVersion}` : 'V1'}</span>
          <div id="lth-trend-${mint}" class="direction-badge" style="font-size:10px; padding:2px 6px; display:none"></div>
          <div id="lth-regime-${mint}" class="regime-badge" style="font-size:10px; padding:2px 6px; display:none"></div>
          <div id="lth-status-${mint}"></div>
        </div>
      </div>
      <div class="lt-card-stats" id="lt-stats-${mint}"></div>
      <div id="lt-upnl-${mint}"></div>
      <div class="lt-card-chart-container" id="lt-chart-${mint}" style="height:250px; margin:10px 0; border:1px solid var(--border); border-radius:6px; background:#0d1117"></div>
      <div class="lt-event-log" id="lt-events-${mint}"></div>
      <div class="lt-card-actions" style="display:flex; gap:8px;">
        <button class="btn btn-primary btn-xs" style="background:#26a69a; border-color:#26a69a" onclick="manualTrade('${mint}', 'buy')">Buy</button>
        <button class="btn btn-primary btn-xs" style="background:#ef5350; border-color:#ef5350" onclick="manualTrade('${mint}', 'sell')">Sell</button>
        <button class="btn btn-danger btn-xs" onclick="stopLiveTrader('${mint}')" style="margin-left:auto;">⏹ Stop</button>
      </div>
    `;
    ltTradersGrid.appendChild(card);
    isNew = true;

    // Init lightweight charts
    const chart = LightweightCharts.createChart(document.getElementById(`lt-chart-${mint}`), {
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#8b949e', fontSize: 11 },
      grid: { vertLines: { color: '#30363d33' }, horzLines: { color: '#30363d33' } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: true, rightBarStaysOnScroll: true, shiftVisibleRangeOnNewBar: true, rightOffset: 5, barSpacing: 6 },
      crosshair: { mode: 0 }
    });
    const cSeries = chart.addCandlestickSeries({
      upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
      priceFormat: { type: 'custom', minMove: 1, formatter: p => formatMcap(p) }
    });
    const vSeries = chart.addHistogramSeries({
      color: '#5865f222', priceFormat: { type: 'volume' }, priceScaleId: 'vol'
    });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    ctx.chart = chart;
    ctx.candleSeries = cSeries;
    ctx.volSeries = vSeries;

    new ResizeObserver(() => {
      const el = document.getElementById(`lt-chart-${mint}`);
      if (el) chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    }).observe(document.getElementById(`lt-chart-${mint}`));
  }

  const st = ctx.stats || {};
  const ct = ctx.currentTrade;
  const hasPos = !!ct;
  card.className = `lt-trader-card${hasPos ? " has-position" : ""}`;

  const pnlClass = (st.total_pnl_sol || 0) >= 0 ? "pos" : "neg";
  const upnlClass = (ctx.unrealizedPnl || 0) >= 0 ? "pos" : "neg";

  const name = ctx.info?.token_name || mint.slice(0, 8);
  const symbol = ctx.info?.token_symbol ? "$" + ctx.info.token_symbol : "";
  document.getElementById(`lth-name-${mint}`).textContent = name;
  document.getElementById(`lth-sym-${mint}`).textContent = symbol;
  document.getElementById(`lth-status-${mint}`).innerHTML = hasPos ? '<span class="lt-card-status position-open">IN POSITION</span>' : '<span class="lt-card-status running"><span class="lt-live-dot"></span>MONITORING</span>';

  // Badges
  const tb = document.getElementById(`lth-trend-${mint}`);
  const rb = document.getElementById(`lth-regime-${mint}`);
  if (ctx.direction && ctx.direction !== "none") {
    tb.style.display = "block";
    const arrow = ctx.direction === "up" ? "▲" : "▼";
    const tColor = ctx.direction === "up" ? CANDLE_UP : CANDLE_DOWN;
    tb.style.color = tColor;
    tb.textContent = `${arrow} ${ctx.direction.toUpperCase()} S:${(ctx.sVal || 0).toFixed(2)}`;
  } else {
    tb.style.display = "none";
  }

  if (ctx.regime && ctx.regime !== "idle") {
    rb.style.display = "block";
    rb.textContent = ctx.regime.toUpperCase();
    rb.style.background = REGIME_COLORS[ctx.regime] || "#5a6071";
  } else {
    rb.style.display = "none";
  }

  document.getElementById(`lt-stats-${mint}`).innerHTML = `
    <div class="bt-stat"><span class="bt-stat-label">Trades</span><span class="bt-stat-value">${st.total_trades || 0}</span></div>
    <div class="bt-stat"><span class="bt-stat-label">Win Rate</span><span class="bt-stat-value">${(st.win_rate || 0).toFixed(1)}%</span></div>
    <div class="bt-stat"><span class="bt-stat-label">PnL</span><span class="bt-stat-value ${pnlClass}">${(st.total_pnl_sol || 0) >= 0 ? "+" : ""}${(st.total_pnl_sol || 0).toFixed(4)}</span></div>
  `;

  document.getElementById(`lt-upnl-${mint}`).innerHTML = hasPos ? `
    <div class="lt-card-unrealized">
      <span>Unrealized PnL</span>
      <span class="${upnlClass}" style="font-weight:700">${(ctx.unrealizedPnl || 0) >= 0 ? "+" : ""}${(ctx.unrealizedPnl || 0).toFixed(4)} SOL (${(ctx.unrealizedPnlPct || 0) >= 0 ? "+" : ""}${(ctx.unrealizedPnlPct || 0).toFixed(2)}%)</span>
    </div>
  ` : "";

  document.getElementById(`lt-events-${mint}`).innerHTML = ctx.events.slice(0, 5).map(e =>
    `<div class="${e.type}">${e.ts} — ${e.msg}</div>`
  ).join("");

  updateSessionStats();
}

function manualTrade(mint, action) {
  const ctx = ltActiveTraders[mint];
  if (!ctx || ctx.ws.readyState !== WebSocket.OPEN) return;
  ctx.ws.send(JSON.stringify({ type: "manual_trade", action: action }));
  addTraderEvent(ctx, "info", `Manual ${action.toUpperCase()} requested…`);
}
window.manualTrade = manualTrade;

/* ── Start live trading on a token ───────────────────────────────────── */

// Stagger consecutive startLiveTrader calls so N parallel tokens don't all
// open their WebSockets (and trigger resolve_input) at the exact same moment.
let _ltConnectCount = 0;
let _ltConnectResetTimer = null;
// Consecutive failed re-attach cycles per mint — bounds the onclose probe
// loop so a permanently-unreachable session can't re-attach forever.
let _attachFailCounts = {};

function startLiveTrader(mint, _delayOverride = null, opts = {}) {
  // opts.attach: re-attach to an already-running server-side session instead
  // of creating a new one — no private key sent, no new trader spawned.
  const attach = !!opts.attach;
  if (!attach) {
    if (!ltWalletPubkey) return alert("Connect wallet first");
    if (!/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(mint)) return alert("Invalid Solana address");
    // The backend now refuses to spawn a session without an explicit key
    // (a key-less connect is treated as attach-only).  Fail loudly here
    // instead of a silent WS 4001 close + card teardown.
    if (!_privateKey) return alert("Wallet key not available in this browser — re-enter it above (server-side key alone is not enough to start)");
  }
  if (ltActiveTraders[mint]) return attach ? undefined : alert("Already trading this token");

  // Stagger: each successive call within 2s adds 400ms extra delay
  const delayMs = _delayOverride !== null ? _delayOverride : _ltConnectCount * 400;
  if (!attach) {
    _ltConnectCount++;
    clearTimeout(_ltConnectResetTimer);
    _ltConnectResetTimer = setTimeout(() => { _ltConnectCount = 0; }, 2000);
  }

  const config = getLtConfig();
  // Buy size comes ONLY from the input field — there is no fallback default
  // in the frontend or the backend, so an invalid field must fail loudly
  // instead of silently trading a hard-coded size.
  if (!attach && !(config.buySize > 0)) {
    return alert("Enter a valid Buy Size (SOL) > 0 in Trading Configuration before starting");
  }
  const paramsStr = encodeURIComponent(JSON.stringify(getLtEngineParams()));
  // Attach mode NEVER sends the private key — it must not spawn a new session
  // if the existing one ended between the status check and this connect.
  // The dashboard config IS still sent on attach: it is ignored when a real
  // session exists, but it guarantees that any session created from this
  // connect inherits the dashboard's buy size / slippage / engine version.
  // The buy_size param is only included when the field holds a valid value —
  // the backend refuses to create a session without one (no default).
  const buySizeParam = config.buySize > 0 ? `&buy_size=${config.buySize}` : "";
  const wsUrl = attach
    ? `${LT_WS_BASE}/${mint}?timeframe=${encodeURIComponent(opts.timeframe || config.timeframe)}${buySizeParam}&slippage_bps=${config.slippageBps}&params=${paramsStr}&engine_version=${opts.engineVersion || ltEngineVersion}`
    : `${LT_WS_BASE}/${mint}?timeframe=${config.timeframe}&private_key=${encodeURIComponent(_privateKey)}${buySizeParam}&slippage_bps=${config.slippageBps}&params=${paramsStr}&engine_version=${ltEngineVersion}`;

  // Register the card immediately so the UI shows "Connecting…" right away
  const ctx = {
    mint,
    ws: null,  // filled in after delay
    info: null,
    stats: {},
    currentTrade: null,
    unrealizedPnl: 0,
    unrealizedPnlPct: 0,
    events: [],
    regime: "idle",
    direction: "none",
    sVal: 0,
    engineVersion: opts.engineVersion || ltEngineVersion,
    timeframe: opts.timeframe || config.timeframe,
    realMint: null,     // resolved on-chain mint (from session_info)
    manualStop: false,  // true once the user (or session_ended) closed this card — blocks reconnect
  };
  ltActiveTraders[mint] = ctx;
  ltStopAllBtn.style.display = "inline-flex";
  addTraderEvent(ctx, "info", attach ? "Re-attaching to running session…" : (delayMs > 0 ? `Connecting… (staggered ${delayMs}ms)` : "Connecting…"));
  updateTraderCard(mint);

  setTimeout(() => {
    if (!ltActiveTraders[mint]) return;  // was stopped before delay elapsed
    const ws = new WebSocket(wsUrl);
    ctx.ws = ws;

    // If the backend hasn't answered the handshake within 25s (resolver
    // contention, backend down), surface it instead of hanging forever on
    // "Re-attaching to running session…".  Closing the socket triggers the
    // onclose probe, which re-attaches cleanly once the backend is reachable.
    ctx.wsOpenTimer = setTimeout(() => {
      if (ctx.ws && ctx.ws.readyState === WebSocket.CONNECTING) {
        addTraderEvent(ctx, "error", "Connection timed out — retrying…");
        try { ctx.ws.close(); } catch (e) { /* ignore */ }
      }
    }, 25000);

    ws.onopen = () => {
      clearTimeout(ctx.wsOpenTimer);
      delete _attachFailCounts[mint];
      addTraderEvent(ctx, "info", "Connected — warming up indicators…");
    };

    ws.onmessage = async (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }

      if (msg.type === "session_info") {
        ctx.realMint = msg.real_mint || ctx.mint;
        if (msg.engine_version) ctx.engineVersion = msg.engine_version;
        if (!ctx.info && (msg.token_name || msg.token_symbol)) {
          ctx.info = { name: msg.token_name, symbol: msg.token_symbol };
        }
        updateTraderCard(mint);
      }

      if (msg.type === "session_ended") {
        // Server-side session finished (manual stop from any tab, mcap floor,
        // no-motion, stream end). Don't reconnect when the socket closes.
        ctx.manualStop = true;
        addTraderEvent(ctx, "error", `🛑 Session ended (${msg.reason || "stopped"})`);
        const card = document.querySelector(`.lt-trader-card[data-mint="${mint}"]`);
        if (card) { card.style.borderColor = "var(--red)"; card.style.opacity = "0.7"; }
        setTimeout(() => stopLiveTrader(mint), 8000);
      }

      if (msg.type === "token_info") {
        ctx.info = msg.data;
        addTraderEvent(ctx, "info", `Token: ${msg.data.name || mint.slice(0, 8)}`);
        updateTraderCard(mint);
      }

      if (msg.type === "historical" && msg.strategy) {
        addTraderEvent(ctx, "info", `Loaded ${msg.candles?.length || 0} historical candles`);
        if (ctx.candleSeries && msg.candles) {
          const res = await formatOfflineCandles(mint, msg.candles, ctx.timeframe);
          ctx.baseMcap = res.baseMcap;
          ctx.basePrice = res.basePrice;
          ctx.lastClose = res.lastClose;
          ctx.lastTime = res.lastTime;

          ctx.candleSeries.setData(res.candles);
          ctx.volSeries.setData(res.candles.map(c => ({ time: c.time, value: c.volume || 0, color: c.color })));
          if (ctx.chart) ctx.chart.timeScale().scrollToRealTime();
        }
        if (Array.isArray(msg.strategy) && msg.strategy.length > 0) {
          const lastS = msg.strategy[msg.strategy.length - 1];
          ctx.regime = lastS.regime || "idle";
          ctx.direction = lastS.direction || "none";
          ctx.sVal = lastS.s || 0;
          updateTraderCard(mint);
        }
      }

      if (msg.type === "candle" && msg.strategy) {
        const s = msg.strategy;
        if (ctx.candleSeries && msg.candle) {
          if (!ctx.baseMcap) {
            ctx.baseMcap = msg.market_cap_usd || (msg.candle.close * 1e9);
            ctx.basePrice = msg.candle.close;
            ctx.lastClose = null;
            ctx.lastTime = null;
          }

          let rawOpen = ctx.baseMcap * (msg.candle.open / ctx.basePrice);
          let high = ctx.baseMcap * (msg.candle.high / ctx.basePrice);
          let low = ctx.baseMcap * (msg.candle.low / ctx.basePrice);
          let close = ctx.baseMcap * (msg.candle.close / ctx.basePrice);

          // Gap filling and new candle bridging
          if (msg.is_new) {
            const tfSec = timeframeToSeconds(ctx.timeframe);
            if (ctx.lastTime && msg.candle.time > ctx.lastTime + tfSec) {
              const gap = Math.floor((msg.candle.time - ctx.lastTime) / tfSec) - 1;
              if (gap <= 15) {
                for (let t = ctx.lastTime + tfSec; t < msg.candle.time; t += tfSec) {
                  ctx.candleSeries.update({ time: t, open: ctx.lastClose, high: ctx.lastClose, low: ctx.lastClose, close: ctx.lastClose, color: CANDLE_FLAT, borderColor: CANDLE_FLAT, wickColor: CANDLE_FLAT });
                  ctx.volSeries.update({ time: t, value: 0, color: "#5865f222" });
                }
              }
            }
            ctx.currentOpen = ctx.lastClose !== null && ctx.lastClose !== undefined ? ctx.lastClose : rawOpen;
          }

          let open = ctx.currentOpen !== undefined ? ctx.currentOpen : rawOpen;
          low = Math.min(open, low, close);

          let color = CANDLE_FLAT;
          if (close > open) color = CANDLE_UP;
          else if (close < open) color = CANDLE_DOWN;
          else if (ctx.lastClose !== null && ctx.lastClose !== undefined) {
            if (close > ctx.lastClose) color = CANDLE_UP;
            else if (close < ctx.lastClose) color = CANDLE_DOWN;
          }

          ctx.candleSeries.update({ time: msg.candle.time, open, high, low, close, color, borderColor: color, wickColor: color });
          ctx.volSeries.update({ time: msg.candle.time, value: msg.candle.volume || 0, color: color });

          ctx.lastClose = close;
          ctx.lastTime = msg.candle.time;
          if (ctx.chart) ctx.chart.timeScale().scrollToRealTime();
        }

        // Update live_trade data
        const lt = s.live_trade || s.forward_test;
        if (lt) {
          ctx.stats = lt.stats || ctx.stats;
          ctx.currentTrade = lt.current_trade;
          ctx.unrealizedPnl = lt.unrealized_pnl || 0;
          ctx.unrealizedPnlPct = lt.unrealized_pnl_pct || 0;
        }
        ctx.regime = s.regime || "idle";
        ctx.direction = s.direction || "none";
        ctx.sVal = s.s || 0;
        updateTraderCard(mint);
      }

      if (msg.type === "trade_update") {
        ctx.stats = msg.stats || ctx.stats;
        ctx.currentTrade = msg.current_trade;
        if (msg.event === "buy_confirmed" || msg.event === "sell_confirmed") {
          const sig = msg.detail || "";
          const shortSig = sig.length > 10 ? sig.slice(0, 10) + "…" : sig;
          const action = msg.event.includes("buy") ? "buy" : "sell";
          addTraderEvent(ctx, action, `${msg.event.replace("_", " ").toUpperCase()} ✓ ${shortSig}`);
          if (msg.event === "buy_confirmed") {
            addLtTradeRow(ctx, "BUY", msg.current_trade?.entry_price || 0, 0, 0, sig, "confirmed");
          }
          if (msg.event === "sell_confirmed") {
            const ct = msg.closed_trade || msg.current_trade;
            if (ct) {
              addLtTradeRow(ctx, "SELL", ct.exit_price || 0, ct.pnl_sol || 0, ct.pnl_pct || 0, sig, "confirmed");
            }
            if (msg.sol_received) {
              addTraderEvent(ctx, "sell", `Received ${msg.sol_received.toFixed(6)} SOL`);
            }
          }
        } else if (msg.event === "buy_pending") {
          const sig = msg.detail || "";
          const shortSig = sig.length > 10 ? sig.slice(0, 10) + "…" : sig;
          addTraderEvent(ctx, "buy", `BUY BROADCAST ⏳ confirming on-chain… ${shortSig}`);
        } else if (msg.event === "sell_pending") {
          addTraderEvent(ctx, "sell", `⏳ ${msg.detail} — watchdog will retry`);
        } else if (msg.event === "buy_failed" || msg.event === "sell_failed") {
          addTraderEvent(ctx, "error", `❌ ${msg.event.replace("_", " ").toUpperCase()}: ${msg.detail}`);
        } else if (msg.event === "tx_simulation_failed") {
          addTraderEvent(ctx, "error", `⚠️ SIMULATION FAILED: ${msg.detail}`);
        } else if (msg.event === "mcap_stop") {
          addTraderEvent(ctx, "error", `🛑 MCAP FLOOR: ${msg.detail}`);
          const card = document.querySelector(`.lt-trader-card[data-mint="${mint}"]`);
          if (card) { card.style.borderColor = "var(--red)"; card.style.opacity = "0.7"; }
          setTimeout(() => stopLiveTrader(mint), 8000);
        } else if (msg.event === "mcap_floor_hold") {
          addTraderEvent(ctx, "error", `⚠️ MCAP FLOOR: ${msg.detail}`);
        } else if (msg.event === "mcap_floor_recovered") {
          addTraderEvent(ctx, "buy", `✅ MCAP FLOOR: ${msg.detail}`);
        } else if (msg.event === "no_motion_stop") {
          addTraderEvent(ctx, "error", `🕐 NO MOTION: ${msg.detail}`);
          const card = document.querySelector(`.lt-trader-card[data-mint="${mint}"]`);
          if (card) { card.style.borderColor = "var(--red)"; card.style.opacity = "0.7"; }
          setTimeout(() => stopLiveTrader(mint), 8000);
        }
        updateTraderCard(mint);
      }

      if (msg.type === "ping") {
        ws.send(JSON.stringify({ type: "pong" }));
      }
    };

    ws.onerror = () => { addTraderEvent(ctx, "error", "WebSocket error"); };
    ws.onclose = () => {
      clearTimeout(ctx.wsOpenTimer);
      addTraderEvent(ctx, "info", "Disconnected");
      updateTraderCard(mint);
      // The backend session keeps running independently of this tab.  Unless
      // this card was explicitly stopped, probe the backend and re-attach as
      // a viewer if the session is still alive (covers tab sleep, network
      // blips, and backend restarts that ended the session cleanly).
      if (!ctx.manualStop && ltActiveTraders[mint] === ctx) {
        setTimeout(() => {
          if (ctx.manualStop || ltActiveTraders[mint] !== ctx) return;
          const probeMint = ctx.realMint || mint;
          apiFetch("/api/live/status")
            .then(st => {
              if (ctx.manualStop || ltActiveTraders[mint] !== ctx) return;
              const t = st && Array.isArray(st.traders)
                ? st.traders.find(t => t.mint === probeMint && t.status === "running")
                : null;
              if (t) {
                // Drop this card and re-attach fresh (rebuilds the chart from
                // the session's recorded candles).  Bound the loop: if the
                // re-attach socket keeps failing (e.g. the resolver can't
                // agree on the mint), give up after 3 cycles instead of
                // re-attaching forever — the periodic status poll will
                // surface the session again if it becomes attachable.
                const fails = (_attachFailCounts[mint] || 0) + 1;
                if (fails > 3) {
                  delete _attachFailCounts[mint];
                  addTraderEvent(ctx, "error", "Re-attach failed repeatedly — dropping viewer (auto-refresh will retry)");
                  detachTraderCard(mint);
                  return;
                }
                _attachFailCounts[mint] = fails;
                detachTraderCard(mint);
                startLiveTrader(t.mint, 0, {
                  attach: true,
                  timeframe: t.timeframe,
                  engineVersion: t.engine_version,
                });
              } else {
                delete _attachFailCounts[mint];
                detachTraderCard(mint);
              }
            })
            .catch(() => {
              // Backend unreachable — don't leave the card parked silently.
              // After a few failed probes, drop it; the periodic status poll
              // brings it back the moment the backend is reachable again.
              const fails = (_attachFailCounts[mint] || 0) + 1;
              if (fails > 3) {
                delete _attachFailCounts[mint];
                addTraderEvent(ctx, "error", "Backend unreachable — dropping viewer (will auto-reconnect)");
                detachTraderCard(mint);
                return;
              }
              _attachFailCounts[mint] = fails;
              addTraderEvent(ctx, "error", "Backend unreachable — retrying…");
            });
        }, 3000);
      }
    };
  }, delayMs);
}

/* ── Stop trader ─────────────────────────────────────────────────────── */

function detachTraderCard(mint) {
  // Remove the local card only — the backend session (if any) is untouched.
  delete ltActiveTraders[mint];
  const card = document.querySelector(`.lt-trader-card[data-mint="${mint}"]`);
  if (card) card.remove();
  if (Object.keys(ltActiveTraders).length === 0) {
    ltTradersGrid.innerHTML = '<div class="empty-state">No active traders. Connect wallet and add tokens above.</div>';
    ltStopAllBtn.style.display = "none";
  }
  updateSessionStats();
}

function stopLiveTrader(mint) {
  const ctx = ltActiveTraders[mint];
  if (!ctx) return;
  ctx.manualStop = true;
  if (ctx.ws) ctx.ws.close();
  // Session key is the resolved on-chain mint (may differ from what the user
  // typed if a pair address / symbol was used to start it).
  apiFetch("/api/live/stop", { method: "POST", body: JSON.stringify({ mint: ctx.realMint || mint }) }).catch(() => { });
  detachTraderCard(mint);
}

function stopAllTraders() {
  for (const mint of Object.keys(ltActiveTraders)) {
    stopLiveTrader(mint);
  }
}

/* ── Re-attach to server-side sessions ───────────────────────────────── */

function refreshLiveSessions() {
  // Live sessions run server-side and survive tab closes. On page load (and
  // whenever the live-trading tab is opened), re-attach a viewer card for
  // every running session this tab isn't showing yet.
  apiFetch("/api/live/status")
    .then(st => {
      if (!st) return;
      if (st.session_summary) {
        updateSessionStats(st.session_summary, st.traders);
      } else if (Array.isArray(st.traders)) {
        updateSessionStats(null, st.traders);
      }
      if (Array.isArray(st.trades)) {
        renderLtTradeTable(st.trades);
      }
      if (Array.isArray(st.traders)) {
        // Sync the engine toggle from any running session — the server holds
        // the ground-truth engine version for live sessions.  This keeps the
        // toggle honest (e.g. after a page reload) so a NEW session spawned
        // later inherits the engine actually running, not a stale default.
        const runningEngine = st.traders.find(t => t.status === "running" && t.engine_version);
        if (runningEngine) setLtEngineVersion(Number(runningEngine.engine_version));
        for (const t of st.traders) {
          if (t.status !== "running") continue;
          if (ltActiveTraders[t.mint]) continue;
          startLiveTrader(t.mint, 0, {
            attach: true,
            timeframe: t.timeframe,
            engineVersion: t.engine_version,
          });
        }
      }
    })
    .catch(() => { });
}

/* ── Event listeners ─────────────────────────────────────────────────── */


ltAddBtn.addEventListener("click", () => {
  const mint = ltTokenInput.value.trim();
  if (!mint) return;
  startLiveTrader(mint);
  ltTokenInput.value = "";
});

ltTokenInput.addEventListener("keydown", e => { if (e.key === "Enter") ltAddBtn.click(); });
ltStopAllBtn.addEventListener("click", stopAllTraders);

/* ── Trading Configuration persistence ─────────────────────────────────
   Buy size / slippage / priority fee / timeframe survive page reloads,
   like the engine version, wallet key, and autofeed switch.  The buy size
   is the single source of truth for the live trader, so its persisted value
   is also pushed to the backend store (POST /api/live/buy_size) at load and
   on every change — headless paths (autofeed server-side spawns) read it
   from there. */
(function restoreLtConfig() {
  const savedBuySize = parseFloat(localStorage.getItem("lt_buy_size"));
  if (savedBuySize > 0) $("lt-buy-size").value = savedBuySize;
  const savedSlippage = parseFloat(localStorage.getItem("lt_slippage"));
  if (savedSlippage > 0) $("lt-slippage").value = savedSlippage;
  const savedPrioFee = parseFloat(localStorage.getItem("lt_priority_fee"));
  if (Number.isFinite(savedPrioFee) && savedPrioFee >= 0) $("lt-priority-fee").value = savedPrioFee;
  const savedTf = localStorage.getItem("lt_timeframe");
  if (savedTf && $("lt-timeframe").querySelector(`option[value="${savedTf}"]`)) {
    $("lt-timeframe").value = savedTf;
  }
  // Sync the backend store with the (restored) field value.
  pushLtBuySize();
})();

/* Config change listeners — persist, hot-update all active traders, and
   keep the backend buy-size store in sync */
["lt-buy-size", "lt-slippage"].forEach(id => {
  $(id).addEventListener("change", () => {
    const config = getLtConfig();
    // Persist the field values
    if (config.buySize > 0) localStorage.setItem("lt_buy_size", String(config.buySize));
    if (config.slippagePct > 0) localStorage.setItem("lt_slippage", String(config.slippagePct));
    // Keep the backend store (autofeed spawns) in sync with the field
    pushLtBuySize();
    for (const ctx of Object.values(ltActiveTraders)) {
      if (ctx.ws && ctx.ws.readyState === WebSocket.OPEN) {
        const msg = {
          type: "update_config",
          slippage_bps: config.slippageBps,
          // priority_fee is fixed at 0.0001 SOL — not sent
        };
        // Only push a valid buy size — an empty/invalid input must never
        // hot-set a running session to a fallback size.
        if (config.buySize > 0) msg.buy_size = config.buySize;
        ctx.ws.send(JSON.stringify(msg));
      }
    }
  });
});
$("lt-priority-fee").addEventListener("change", () => {
  const v = parseFloat($("lt-priority-fee").value);
  if (Number.isFinite(v) && v >= 0) localStorage.setItem("lt_priority_fee", String(v));
});
$("lt-timeframe").addEventListener("change", () => {
  localStorage.setItem("lt_timeframe", $("lt-timeframe").value);
});

// Page switch handler for live trading
const origSwitchPage = switchPage;
switchPage = function (pageId) {
  origSwitchPage(pageId);
  if (pageId === "live-trading") {
    // Pick up server-side sessions, stats, and trade log started from other tabs / before reload
    refreshLiveSessions();
  }
};
// Re-bind nav tabs with new switchPage
navTabs.forEach(tab => {
  tab.removeEventListener("click", () => { });
  tab.addEventListener("click", () => switchPage(tab.dataset.page));
});
// Sessions keep running with the tab closed — re-attach on first load too
refreshLiveSessions();

// Autofeed auto-starts sessions server-side without any browser action, so
// periodically re-sync while the Live Trading tab is visible.  Without this
// poll, a session created while the page is open stayed invisible until a
// reload or tab switch.
setInterval(() => {
  if (document.hidden) return;
  const livePage = document.getElementById("page-live-trading");
  if (!livePage || !livePage.classList.contains("active")) return;
  refreshLiveSessions();
}, 5000);


window.stopLiveTrader = stopLiveTrader;
window.stopAllTraders = stopAllTraders;


/* ══════════════════════════════════════════════════════════════════════════
   AUTOFEED — Auto-feed clean/organic pump.fun-migrated tokens from gmgn.ai
   ─────────────────────────────────────────────────────────────────────────
   Autofeed is discovery-only.  The backend polls gmgn-cli `market trending`
   with strict organic / non-bundled / mcap ≥ 15k gates.  Each accepted
   candidate is pushed over /ws/autofeed.  On each candidate we call
   startLiveTrader(mint) — exactly the same path the manual "Start Trading"
   button uses to open /ws/live/{mint}.

   Switch governance: the autofeed toggle cannot be turned on until the
   wallet key is set (`ltWalletConnected`).  We also force the backend to
   refuse if no private key is set, by sending `connected: true` on enable.
   ══════════════════════════════════════════════════════════════════════════ */

const AF_WS_BASE = `${WS_PROTO}//${location.host}/ws/autofeed`;

/* ── DOM refs ────────────────────────────────────────────────────────── */
const afToggle = $("af-toggle");
const afSettingsWrap = $("af-settings");
const afStatusDot = $("af-status-dot");
const afStatusText = $("af-status-text");
const afCliStatus = $("af-cli-status");
const afCandidates = $("af-candidates");
const afStatSeen = $("af-stat-seen");
const afStatFed = $("af-stat-fed");
const afStatTracked = $("af-stat-tracked");
const afStatLastPoll = $("af-stat-lastpoll");
const afSaveConfigBtn = $("af-save-config");
const afTestPollBtn = $("af-test-poll");
const afPreview = $("af-preview");

let afWS = null;
let afConfigCache = null;

/* ── Toggle governance ──────────────────────────────────────────────── */
function afUpdateToggleGate() {
  // Cannot be enabled until wallet (private key) is set
  if (!ltWalletConnected) {
    if (afToggle.checked) afToggle.checked = false;
    afToggle.disabled = true;
    afToggle.parentElement.title = "Connect wallet (set private key) first";
    if (!afToggle.checked) afSetStatus("off", "Wallet key required");
  } else {
    afToggle.disabled = false;
    afToggle.parentElement.title = "Turn on autofeed";
  }
}

/* ── WS lifecycle ───────────────────────────────────────────────────── */
function afConnectWS() {
  if (afWS && afWS.readyState === WebSocket.OPEN) return;
  try {
    afWS = new WebSocket(AF_WS_BASE);
  } catch (e) { console.warn("[AutoFeed WS] connect failed", e); return; }

  afWS.onopen = () => afSetStatus("connected", "Connected");
  afWS.onclose = () => {
    afSetStatus("off", "Disconnected");
    afWS = null;
    // Reconnect in 5s only if autofeed is supposed to be on
    if (afToggle.checked && ltWalletConnected) {
      setTimeout(afConnectWS, 5000);
    }
  };
  afWS.onerror = () => afSetStatus("off", "WS error");

  afWS.onmessage = async (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "autofeed_status") {
      afHandleStatus(msg.data || {});
    } else if (msg.type === "autofeed_candidate") {
      afHandleCandidate(msg.candidate || {});
    } else if (msg.type === "session_started") {
      // The backend auto-started a server-side session (it holds the private
      // key).  Surface it as a viewer card — no browser key required.
      refreshLiveSessions();
    } else if (msg.type === "ping") {
      try { afWS.send(JSON.stringify({ type: "pong" })); } catch { /* ignore */ }
    }
  };
}

function afDisconnectWS() {
  if (afWS) { try { afWS.close(); } catch { /* ignore */ } afWS = null; }
}

function afSetStatus(state, text) {
  afStatusDot.className = `dot ${state === "connected" ? "connected" : state === "running" ? "connected" : "error"}`;
  if (state === "running") afStatusDot.className = "dot connected";
  if (state === "off") afStatusDot.className = "dot error";
  if (text) afStatusText.textContent = text;
}

/* ── Incoming from backend ─────────────────────────────────────────── */
function afHandleStatus(snap) {
  afConfigCache = snap;
  // Ppopulate form fields from backend (only when local fields untouched -> just always)
  afPopulateForm(snap);

  // CLI configuration status
  if (snap.cli_configured) {
    afCliStatus.textContent = "gmgn-cli: ✅ configured";
    afCliStatus.style.color = "var(--green, #26a69a)";
  } else {
    afCliStatus.textContent = "gmgn-cli: ⚠ no API key";
    afCliStatus.style.color = "var(--red, #ef5350)";
  }

  // Stats
  afStatSeen.textContent = snap.total_seen || 0;
  afStatFed.textContent = snap.total_fed || 0;
  afStatTracked.textContent = snap.active_tracked || 0;
  if (snap.last_poll_at && snap.last_poll_at > 0) {
    afStatLastPoll.textContent = new Date(snap.last_poll_at * 1000).toLocaleTimeString();
  } else {
    afStatLastPoll.textContent = "—";
  }

  // Recent candidates → render
  afRenderCandidates(snap.recent_candidates || []);

  // Running state
  if (snap.is_running) {
    afSetStatus("running", "Running");
  } else {
    afSetStatus(snap.cli_configured ? "off" : "off", snap.cli_configured ? "Idle" : "gmgn-cli unconfigured");
  }
}

function afHandleCandidate(cand) {
  afAddCandidateRow(cand);
  // Feed into live trader — exactly the manual-button path.
  afFeedToLiveTrader(cand);
}

function afFeedToLiveTrader(cand) {
  if (!cand || !cand.mint) return;
  if (!ltWalletConnected) {
    console.warn("[AutoFeed] Received candidate but wallet not connected — skipping feed:", cand.mint);
    return;
  }
  // Don't double-start: skip if this mint is already an active trader
  if (ltActiveTraders[cand.mint]) {
    console.log("[AutoFeed] Skipping feed — already trading:", cand.mint);
    return;
  }
  try {
    if (_privateKey) {
      console.info(`[AutoFeed] Feeding ${cand.mint} (${cand.symbol || "?"}) mcap=$${Math.round(cand.market_cap || 0)} into live trader…`);
      startLiveTrader(cand.mint);
    } else {
      // Backend holds the key and auto-started the session server-side —
      // attach a viewer card instead of aborting (the old code required the
      // browser key here, so server-side sessions stayed invisible).
      console.info(`[AutoFeed] No browser key — surfacing server-side session for ${cand.mint} (${cand.symbol || "?"})`);
      refreshLiveSessions();
      setTimeout(refreshLiveSessions, 2000);
    }
  } catch (e) {
    console.error("[AutoFeed] Failed to start live trader for", cand.mint, e);
  }
}

function afRenderCandidates(list) {
  if (!Array.isArray(list) || list.length === 0) {
    afCandidates.innerHTML = `<div class="empty-state" style="font-size:11px;padding:14px">No candidates yet. Toggle AutoFeed on (requires wallet key).</div>`;
    return;
  }
  afCandidates.innerHTML = list.slice(0, 30).map(afRenderRowHTML).join("");
}

function afAddCandidateRow(cand) {
  // Prepend (removing empty state if present)
  const empty = afCandidates.querySelector(".empty-state");
  if (empty) empty.remove();
  const html = afRenderRowHTML(cand);
  afCandidates.insertAdjacentHTML("afterbegin", html);
  // Keep at most 30 rows
  const rows = afCandidates.querySelectorAll(".af-candidate");
  if (rows.length > 30) {
    rows[rows.length - 1].remove();
  }
}

function afRenderRowHTML(c) {
  const mint_short = (c.mint || "").slice(0, 8) + "…";
  const fmtUsd = (n) => {
    if (!n || n <= 0) return "—";
    if (n > 1e6) return `$${(n / 1e6).toFixed(2)}M`;
    if (n > 1e3) return `$${(n / 1e3).toFixed(1)}k`;
    return `$${n.toFixed(0)}`;
  };
  const fmtPct = (n) => (n != null && n > 0) ? (n * 100).toFixed(1) + "%" : "—";
  const fmtBool = (b, okWhenFalse = true) => {
    const cls = (b === false) ? (okWhenFalse ? "af-flag-ok" : "af-flag-bad") : "af-flag-bad";
    const lbl = (b === false) ? "✓" : "✗";
    return `<span class="${cls}">${lbl}</span>`;
  };
  return `
    <div class="af-candidate" data-mint="${c.mint}">
      <div>
        <span class="af-name">${c.symbol || mint_short}</span>
        <span class="af-sub"> / ${c.name || "—"}</span>
        <div class="af-sub">${c.mint}</div>
      </div>
      <div>
        <div class="af-sub">MCap</div>
        <div class="af-metric">${fmtUsd(c.market_cap)}</div>
        <div class="af-sub">Liq: ${fmtUsd(c.liquidity)}</div>
      </div>
      <div>
        <div class="af-sub">Smart / Holders</div>
        <div class="af-metric">${c.smart_degen_count || 0} / ${c.holders || 0}</div>
      </div>
      <div>
        <div class="af-sub">Rug / Bund</div>
        <div class="af-metric">${fmtPct(c.rug_ratio)} / ${fmtPct(c.bundler_rate)}</div>
      </div>
      <div>
        <div class="af-sub">Motion (vol / swaps)</div>
        <div class="af-metric" style="color:var(--green,#26a69a);font-weight:700">
          ${fmtUsd(c.volume)} / ${c.swaps || 0}
        </div>
        <div class="af-sub">1h: ${(c.price_change_1h != null && c.price_change_1h !== 0) ? ((c.price_change_1h >= 0 ? "+" : "") + Number(c.price_change_1h).toFixed(1) + "%") : "—"}</div>
      </div>
      <div>
        <div class="af-sub">Organic Gates</div>
        <div class="af-metric">
          Wash ${fmtBool(c.is_wash_trading)} ·
          RenMint ${fmtBool(c.renounced_mint, false)} ·
          RenFrz ${fmtBool(c.renounced_freeze, false)}
        </div>
        <button class="btn btn-xs btn-primary" style="margin-top:4px"
          onclick="afManualStart('${c.mint}')">⚡ Start</button>
      </div>
    </div>
  `;
}

function afManualStart(mint) {
  if (!ltWalletConnected) { alert("Connect wallet first."); return; }
  startLiveTrader(mint);
}
window.afManualStart = afManualStart;

/* ── Form read/write ───────────────────────────────────────────────── */
const AF_FIELD_MAP = [
  ["af-poll-seconds", "poll_seconds", "float"],
  ["af-interval", "interval", "str"],
  ["af-min-mcap", "min_mcap_usd", "float"],
  ["af-max-mcap", "max_mcap_usd", "float"],
  ["af-min-liq", "min_liquidity_usd", "float"],
  ["af-min-holders", "min_holders", "int"],
  ["af-min-smart", "min_smart_degen_count", "int"],
  ["af-min-volume", "min_volume_usd", "float"],
  ["af-min-swaps", "min_swaps", "int"],
  ["af-order-by", "order_by", "str"],
  ["af-migration-exchanges", "migration_exchanges", "str"],
  ["af-req-migration", "require_migration_exchange", "bool"],
  ["af-max-top10", "max_top10_holder_rate", "float"],
  ["af-max-rug", "max_rug_ratio", "float"],
  ["af-max-bundler", "max_bundler_rate", "float"],
  ["af-max-insider", "max_insider_rate", "float"],
  ["af-max-entrap", "max_entrapment_ratio", "float"],
  ["af-max-bot-degen", "max_bot_degen_rate", "float"],
  ["af-max-age", "max_created_age", "str"],
  ["af-platforms", "platforms", "str"],
  ["af-max-concurrent", "max_concurrent_feed", "int"],
  ["af-cooldown", "cooldown_after_feed_minutes", "float"],
  ["af-exclude", "exclude_mints", "str"],
  ["af-req-renounced-mint", "require_renounced_mint", "bool"],
  ["af-req-renounced-freeze", "require_renounced_freeze", "bool"],
  ["af-rej-wash", "reject_wash_trading", "bool"],
  ["af-rej-honeypot", "reject_honeypot", "bool"],
  ["af-req-social", "require_has_social", "bool"],
];

function afPopulateForm(snap) {
  for (const [id, key, type] of AF_FIELD_MAP) {
    const el = $(id);
    if (!el || snap[key] === undefined || snap[key] === null) continue;
    if (type === "bool") el.checked = !!snap[key];
    else el.value = snap[key];
  }
}

function afReadForm() {
  const out = {};
  for (const [id, key, type] of AF_FIELD_MAP) {
    const el = $(id);
    if (!el) continue;
    if (type === "bool") out[key] = !!el.checked;
    else if (type === "int") out[key] = parseInt(el.value, 10) || 0;
    else if (type === "float") out[key] = parseFloat(el.value) || 0;
    else out[key] = el.value.trim();
  }
  return out;
}

/* ── Button handlers ────────────────────────────────────────────────── */
afToggle.addEventListener("change", async () => {
  if (afToggle.checked) {
    if (!ltWalletConnected) {
      afToggle.checked = false;
      localStorage.setItem("lt_autofeed_enabled", "false");
      alert("⚠️  Cannot turn on autofeed without a private key set. Connect your wallet first.");
      afUpdateToggleGate();
      return;
    }
    localStorage.setItem("lt_autofeed_enabled", "true");
    // Tell backend the wallet-gate is satisfied (gate the autofeed loop)
    try {
      await apiFetch("/api/live/private_key", {
        method: "POST",
        body: JSON.stringify({ connected: true }),
      });
    } catch (e) { /* non-fatal; also reported to backend via /start below */ }
    // Send current config then start
    try {
      const cfg = afReadForm();
      await apiFetch("/api/autofeed/config", {
        method: "POST",
        body: JSON.stringify(cfg),
      });
      // Autofeed spawns sessions server-side — make sure the backend store
      // holds the input field's buy size before it starts.
      pushLtBuySize();
      await apiFetch("/api/autofeed/start", { method: "POST", body: "{}" });
      afSettingsWrap.style.display = "block";
      afConnectWS();
    } catch (e) {
      afToggle.checked = false;
      localStorage.setItem("lt_autofeed_enabled", "false");
      afSettingsWrap.style.display = "none";
      console.error("[AutoFeed] start failed", e);
      alert("AutoFeed start failed: " + e.message);
    }
  } else {
    localStorage.setItem("lt_autofeed_enabled", "false");
    afSettingsWrap.style.display = "none";
    try {
      await apiFetch("/api/autofeed/stop", { method: "POST" });
      afSetStatus("off", "Stopped");
    } catch (e) { /* ignore */ }
    afDisconnectWS();
  }
});

afSaveConfigBtn.addEventListener("click", async () => {
  const cfg = afReadForm();
  try {
    const r = await apiFetch("/api/autofeed/config", {
      method: "POST",
      body: JSON.stringify(cfg),
    });
    afConfigCache = r.snapshot || afConfigCache;
    afPreview.textContent = `Saved ${r.changed?.length || 0} field(s) ✓`;
    setTimeout(() => afPreview.textContent = "", 3000);
  } catch (e) {
    afPreview.textContent = "Save error: " + e.message;
    afPreview.style.color = "var(--red, #ef5350)";
  }
});

afTestPollBtn.addEventListener("click", async () => {
  if (!ltWalletConnected) return alert("Connect wallet first.");
  afTestPollBtn.disabled = true;
  afTestPollBtn.textContent = "Polling…";
  try {
    // Start then immediately stop — runs exactly one full poll since no candidate
    // will be processed safely.  Better: backend exposes "poll once" — we don't,
    // so fall back to fetch a status update.
    await apiFetch("/api/autofeed/config", {
      method: "POST",
      body: JSON.stringify(afReadForm()),
    });
    // A test poll can forward a candidate → server-side spawn; sync the
    // buy-size store first so it uses the input field's value.
    pushLtBuySize();
    await apiFetch("/api/autofeed/start", { method: "POST", body: "{}" });
    await new Promise(r => setTimeout(r, 2500));   // allow 1 poll cycle
    await apiFetch("/api/autofeed/stop", { method: "POST" });
    const snap = await apiFetch("/api/autofeed/status");
    afHandleStatus(snap);
  } catch (e) {
    console.warn("[AutoFeed poll-once] failed", e);
  } finally {
    afTestPollBtn.disabled = false;
    afTestPollBtn.textContent = "🧪 Poll Once";
  }
});

async function afRestoreState() {
  afUpdateToggleGate();
  try {
    const snap = await apiFetch("/api/autofeed/status");
    afHandleStatus(snap);
    const savedEnabled = localStorage.getItem("lt_autofeed_enabled") === "true";
    if (ltWalletConnected && (snap.is_running || savedEnabled)) {
      afToggle.checked = true;
      afSettingsWrap.style.display = "block";
      localStorage.setItem("lt_autofeed_enabled", "true");
      if (!snap.is_running) {
        try {
          const cfg = afReadForm();
          await apiFetch("/api/autofeed/config", {
            method: "POST",
            body: JSON.stringify(cfg),
          });
          // Ensure autofeed's server-side spawns use the input field's size
          pushLtBuySize();
          await apiFetch("/api/autofeed/start", { method: "POST", body: "{}" });
        } catch (err) {
          console.warn("[AutoFeed] Failed to auto-start backend autofeed", err);
        }
      }
      afConnectWS();
    } else if (!snap.is_running && !savedEnabled) {
      afToggle.checked = false;
      afSettingsWrap.style.display = "none";
    }
  } catch (e) {
    console.warn("[AutoFeed] Restore state failed", e);
  }
}

/* ── Wire into wallet connect lifecycle ────────────────────────────── */
const _origConnectWallet = connectWallet;
connectWallet = async function () {
  await _origConnectWallet();
  if (ltWalletConnected) {
    await afRestoreState();
  }
};
window.connectWallet = connectWallet;  // keep inline `onclick="connectWallet()"` working
ltConnectBtn.removeEventListener("click", _origConnectWallet);
ltConnectBtn.addEventListener("click", connectWallet);

/* ── Initial state ─────────────────────────────────────────────────── */
afRestoreState();

// When wallet disconnects or page is left, gracefully shutdown
window.addEventListener("beforeunload", () => {
  try { afDisconnectWS(); } catch { /* ignore */ }
});

/* ══════════════════════════════════════════════════════════════════════════
   PORTFOLIO — account overview driven by a single /api/portfolio call:
   wallet readout, summary cards, cumulative realized-PnL curve, open
   positions (with stop controls), and the durable closed-trade history.
   ══════════════════════════════════════════════════════════════════════════ */

let pfPnlChart = null;
let pfPnlSeries = null;

const pfStatsGrid = $("pf-stats-grid");
const pfWalletAddr = $("pf-wallet-addr");
const pfWalletBal = $("pf-wallet-bal");
const pfPositionsTbody = $("pf-positions-tbody");
const pfTradesTbody = $("pf-trades-tbody");
const pfStopAllBtn = $("pf-stop-all-btn");

function pfInitPnlChart() {
  const wrapper = $("pf-pnl-chart");
  if (pfPnlChart) return;   // create once; series data is replaced per load
  pfPnlChart = LightweightCharts.createChart(wrapper, {
    layout: { background: { color: "transparent" }, textColor: "#5a6071" },
    grid: { vertLines: { color: "#1e233088" }, horzLines: { color: "#1e233088" } },
    timeScale: { borderColor: "#1e2330", timeVisible: true, secondsVisible: false },
    rightPriceScale: { borderColor: "#1e2330" },
    width: wrapper.clientWidth, height: wrapper.clientHeight,
  });
  pfPnlSeries = pfPnlChart.addAreaSeries({
    lineColor: "#26a69a", topColor: "#26a69a44", bottomColor: "#26a69a05",
    lineWidth: 2,
    priceFormat: { type: "custom", minMove: 0.000001, formatter: p => `${p >= 0 ? "+" : ""}${p.toFixed(4)} SOL` },
  });
  new ResizeObserver(() =>
    pfPnlChart.applyOptions({ width: wrapper.clientWidth, height: wrapper.clientHeight })
  ).observe(wrapper);
}

function pfRenderWallet(wallet) {
  if (!wallet.pubkey) {
    pfWalletAddr.textContent = "Wallet not connected";
    pfWalletBal.textContent = "Set a key on the Execution page";
    return;
  }
  pfWalletAddr.textContent = `${wallet.pubkey.slice(0, 6)}…${wallet.pubkey.slice(-4)}`;
  pfWalletBal.textContent = wallet.sol_balance != null
    ? `${wallet.sol_balance.toFixed(4)} SOL`
    : "— SOL";
}

function pfRenderStats(summary) {
  const pnlC = summary.total_pnl_sol >= 0 ? "pos" : "neg";
  const cards = [
    { l: "Realized PnL", v: `${summary.realized_pnl_sol >= 0 ? "+" : ""}${summary.realized_pnl_sol.toFixed(4)} SOL`, c: summary.realized_pnl_sol >= 0 ? "pos" : "neg" },
    { l: "Unrealized PnL", v: `${summary.unrealized_pnl_sol >= 0 ? "+" : ""}${summary.unrealized_pnl_sol.toFixed(4)} SOL`, c: summary.unrealized_pnl_sol >= 0 ? "pos" : "neg" },
    { l: "Total PnL", v: `${summary.total_pnl_sol >= 0 ? "+" : ""}${summary.total_pnl_sol.toFixed(4)} SOL`, c: pnlC },
    { l: "Win Rate", v: `${(summary.win_rate || 0).toFixed(1)}%` },
    { l: "Closed Trades", v: summary.total_trades },
    { l: "Winning / Losing", v: `${summary.winning_trades} / ${summary.losing_trades}` },
    { l: "Tokens Traded", v: summary.tokens_traded },
  ];
  pfStatsGrid.innerHTML = cards.map(x =>
    `<div class="bt-stats-card"><div class="bt-stats-card-label">${x.l}</div><div class="bt-stats-card-value ${x.c || ""}">${x.v}</div></div>`
  ).join("");
}

function pfRenderPositions(positions) {
  pfStopAllBtn.style.display = positions.length ? "" : "none";
  if (!positions.length) {
    pfPositionsTbody.innerHTML = `<tr><td colspan="9" class="cell-empty">No open positions.</td></tr>`;
    return;
  }
  pfPositionsTbody.innerHTML = positions.map(p => {
    const upnlClass = p.unrealized_pnl_sol >= 0 ? "trade-pnl-pos" : "trade-pnl-neg";
    const symbol = p.token_symbol
      ? `<a href="https://solscan.io/token/${p.mint}" target="_blank" style="color:var(--text);text-decoration:none">$${p.token_symbol}</a>`
      : (p.mint ? p.mint.slice(0, 6) + "…" : "—");
    return `<tr>
      <td>${symbol}</td>
      <td>${(p.size_sol || 0).toFixed(4)}</td>
      <td>${(p.size_tokens || 0).toLocaleString([], { maximumFractionDigits: 0 })}</td>
      <td>${p.entry_price ? p.entry_price.toExponential(4) : "—"}</td>
      <td>${p.last_price ? p.last_price.toExponential(4) : "—"}</td>
      <td class="${upnlClass}">${p.unrealized_pnl_sol >= 0 ? "+" : ""}${p.unrealized_pnl_sol.toFixed(6)} (${p.unrealized_pnl_pct >= 0 ? "+" : ""}${p.unrealized_pnl_pct.toFixed(2)}%)</td>
      <td>${fmtDuration(p.entry_time, Date.now() / 1000)}</td>
      <td>${p.entry_reason || "—"}</td>
      <td><button class="btn btn-danger btn-xs" onclick="pfStopPosition('${p.mint}')">Stop</button></td>
    </tr>`;
  }).join("");
}

function pfRenderTrades(trades) {
  if (!trades.length) {
    pfTradesTbody.innerHTML = `<tr><td colspan="8" class="cell-empty">No closed trades yet.</td></tr>`;
    return;
  }
  pfTradesTbody.innerHTML = trades.map(t => {
    const pnlClass = (t.pnl_sol || 0) >= 0 ? "trade-pnl-pos" : "trade-pnl-neg";
    const symbol = t.token_symbol ? "$" + t.token_symbol : (t.mint ? t.mint.slice(0, 6) + "…" : "—");
    const txHash = t.tx_hash || "";
    const txCell = txHash
      ? `<a href="https://solscan.io/tx/${txHash}" target="_blank" style="color:var(--accent);text-decoration:none">${txHash.slice(0, 8)}…</a>`
      : "—";
    return `<tr>
      <td>${symbol}</td>
      <td>${fmtTs(t.ts)}</td>
      <td>${t.entry_price ? t.entry_price.toExponential(4) : "—"}</td>
      <td>${t.exit_price ? t.exit_price.toExponential(4) : "—"}</td>
      <td class="${pnlClass}">${(t.pnl_sol || 0) >= 0 ? "+" : ""}${(t.pnl_sol || 0).toFixed(6)}</td>
      <td class="${pnlClass}">${(t.pnl_pct || 0) >= 0 ? "+" : ""}${(t.pnl_pct || 0).toFixed(2)}%</td>
      <td>${t.exit_reason || "—"}</td>
      <td>${txCell}</td>
    </tr>`;
  }).join("");
}

async function loadPortfolio() {
  const data = await apiFetch("/api/portfolio");
  if (!data || data.error) return;
  pfInitPnlChart();
  pfRenderWallet(data.wallet);
  pfRenderStats(data.summary);
  pfRenderPositions(data.positions);
  pfRenderTrades(data.trades);

  // Cumulative realized PnL — dedupe same-second buckets (charts require
  // strictly ascending unique times; last trade of a bucket wins).
  const pts = [];
  for (const pt of data.equity_curve || []) {
    if (pts.length && pts[pts.length - 1].time === pt.time) pts[pts.length - 1] = pt;
    else pts.push(pt);
  }
  pfPnlSeries.setData(pts);
  if (pts.length) pfPnlChart.timeScale().fitContent();
}

async function pfStopPosition(mint) {
  await apiFetch("/api/live/stop", { method: "POST", body: JSON.stringify({ mint }) });
  loadPortfolio();
}
window.pfStopPosition = pfStopPosition;

pfStopAllBtn.addEventListener("click", async () => {
  await apiFetch("/api/live/stop_all", { method: "POST" });
  loadPortfolio();
});

// First paint + periodic refresh while the Portfolio tab is visible
loadPortfolio();
setInterval(() => {
  if (document.hidden) return;
  const page = document.getElementById("page-portfolio");
  if (!page || !page.classList.contains("active")) return;
  loadPortfolio();
}, 5000);


/* ══════════════════════════════════════════════════════════════════════════
   NEW PAIRS — auto-feed & record newly-born pump.fun tokens (pre-migration)
   ══════════════════════════════════════════════════════════════════════════
   Mirrors the Execution tab's session architecture (server-sided sessions
   survive tab closes; this tab attaches as viewers over /ws/newpairs/{mint})
   with three deliberate differences:
     1. NO strategy engine anywhere — sessions are pure price-action recorders.
     2. Recordings live in a SEPARATE database (newpairs_data.db).
     3. Termination is the NO-MOTION stop only (default 120 s) — there is no
        market-cap floor and no trading, so no wallet key is ever needed.

   Discovery: PumpPortal's subscribeNewToken feed (every pump.fun birth in
   real time) pushed over /ws/newpairs/feed.  Each accepted birth auto-opens
   a recording session server-side; this tab surfaces it as a viewer card —
   the exact same flow the gmgn AutoFeed uses for Execution.
   ══════════════════════════════════════════════════════════════════════════ */

const NP_FEED_WS_BASE = `${WS_PROTO}//${location.host}/ws/newpairs/feed`;
const NP_SESSION_WS_BASE = `${WS_PROTO}//${location.host}/ws/newpairs`;

/* ── State ────────────────────────────────────────────────────────────── */

const npActiveSessions = {};   // mint -> { ws, info, events[], candles, lastPrice, firstPrice }
let npWS = null;               // feed WS

/* ── DOM refs ─────────────────────────────────────────────────────────── */

const npToggle = $("np-toggle");
const npSettingsWrap = $("np-settings");
const npStatusDot = $("np-status-dot");
const npStatusText = $("np-status-text");
const npCandidates = $("np-candidates");
const npStatSeen = $("np-stat-seen");
const npStatFed = $("np-stat-fed");
const npStatActive = $("np-stat-active");
const npStatLastPoll = $("np-stat-lastpoll");
const npSaveConfigBtn = $("np-save-config");
const npPreview = $("np-preview");
const npTradersGrid = $("np-traders-grid");
const npAddBtn = $("np-add-btn");
const npTokenInput = $("np-token-input");
const npStopAllBtn = $("np-stop-all-btn");

/* ── Status helpers ───────────────────────────────────────────────────── */

function npSetStatus(state, text) {
  if (state === "running") npStatusDot.className = "dot connected";
  else if (state === "connected") npStatusDot.className = "dot connected";
  else npStatusDot.className = "dot error";
  if (text) npStatusText.textContent = text;
}

/* ── Feed WS lifecycle ─────────────────────────────────────────────────── */

function npConnectWS() {
  if (npWS && npWS.readyState === WebSocket.OPEN) return;
  try {
    npWS = new WebSocket(NP_FEED_WS_BASE);
  } catch (e) { console.warn("[NewPairs WS] connect failed", e); return; }

  npWS.onopen = () => npSetStatus("connected", "Connected");
  npWS.onclose = () => {
    npSetStatus("off", "Disconnected");
    npWS = null;
    // Reconnect in 5s only if the feed is supposed to be on
    if (npToggle.checked) setTimeout(npConnectWS, 5000);
  };
  npWS.onerror = () => npSetStatus("off", "WS error");

  npWS.onmessage = async (ev) => {
    let msg; try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "newpairs_status") {
      npHandleStatus(msg.data || {});
    } else if (msg.type === "newpair_candidate") {
      npHandleCandidate(msg.candidate || {});
    } else if (msg.type === "session_started") {
      // A recording session just started server-side — attach a viewer card.
      npAttachSession(msg.mint, { timeframe: msg.timeframe, tokenName: msg.token_name, tokenSymbol: msg.token_symbol });
    } else if (msg.type === "ping") {
      try { npWS.send(JSON.stringify({ type: "pong" })); } catch { /* ignore */ }
    }
  };
}

function npDisconnectWS() {
  if (npWS) { try { npWS.close(); } catch { /* ignore */ } npWS = null; }
}

/* ── Incoming from backend ────────────────────────────────────────────── */

function npHandleStatus(snap) {
  // Populate form fields from backend
  npPopulateForm(snap);

  // Cache the fees-gate threshold for candidate-row coloring
  if (snap.min_global_fees_sol !== undefined && snap.min_global_fees_sol !== null) {
    _npFeesThresholdCache = snap.min_global_fees_sol;
  }

  // Stats
  npStatSeen.textContent = snap.total_seen || 0;
  npStatFed.textContent = snap.total_fed || 0;
  npStatActive.textContent = snap.active_sessions ?? (snap.active_tracked || 0);
  if (snap.last_event_at && snap.last_event_at > 0) {
    npStatLastPoll.textContent = new Date(snap.last_event_at * 1000).toLocaleTimeString();
  } else {
    npStatLastPoll.textContent = "—";
  }

  npRenderCandidates(snap.recent_candidates || []);

  // Running state — no wallet gate on this tab (nothing is traded).
  if (snap.is_running) {
    npSetStatus("running", "Running — auto-recording newborns");
    const pend = snap.pending_qualification || 0;
    const feesGate = (snap.min_global_fees_sol || 0) > 0;
    $("np-source-status").textContent =
      "PumpPortal subscribeNewToken: ✅ live" +
      (feesGate ? ` · fees ≥ ${snap.min_global_fees_sol} SOL` : "") +
      (pend > 0 ? ` · ${pend} awaiting fees check` : "");
    $("np-source-status").style.color = "var(--green, #26a69a)";
  } else {
    npSetStatus("off", "Idle");
    $("np-source-status").textContent = "Feed off";
    $("np-source-status").style.color = "var(--dim)";
  }

  // Session summary card
  npUpdateSessionStats(snap);

  // Re-attach viewer cards for sessions already running (page reload, feed
  // spawned while another tab was open…).  The periodic poll also covers this.
  if (Array.isArray(snap.sessions)) {
    for (const s of snap.sessions) {
      if (s.status !== "recording") continue;
      if (npActiveSessions[s.mint]) continue;
      npAttachSession(s.mint, {
        attach: true,
        timeframe: s.timeframe,
        tokenName: s.token_name,
        tokenSymbol: s.token_symbol,
      });
    }
  }
}

function npHandleCandidate(cand) {
  npAddCandidateRow(cand);
}

/* Current global-fees gate threshold (SOL) for coloring candidate rows.
   Falls back to the last status snapshot, then the field's default. */
let _npFeesThresholdCache = 0.5;
function npFeesThreshold() {
  return _npFeesThresholdCache;
}

/* ── Session stats bar (deck-zone aggregate) ──────────────────────────── */

function npUpdateSessionStats(snap) {
  const el = $("np-session-card");
  if (!el) return;
  const active = snap.active_sessions || 0;
  const seen = snap.total_seen || 0;
  const fed = snap.total_fed || 0;
  if (active === 0 && seen === 0 && fed === 0 && Object.keys(npActiveSessions).length === 0) {
    el.style.display = "none";
    return;
  }
  el.style.display = "";
  $("nps-recording").textContent = active;
  $("nps-seen").textContent = seen;
  $("nps-fed").textContent = fed;
  let candles = 0;
  for (const ctx of Object.values(npActiveSessions)) candles += ctx.candleCount || 0;
  $("nps-candles").textContent = candles;
}

/* ── Birth candidate rows ─────────────────────────────────────────────── */

function npRenderCandidates(list) {
  if (!Array.isArray(list) || list.length === 0) {
    npCandidates.innerHTML = `<div class="empty-state" style="font-size:11px;padding:14px">No births yet. Toggle the feed on to auto-record new pump.fun tokens.</div>`;
    return;
  }
  npCandidates.innerHTML = list.slice(0, 30).map(npRenderRowHTML).join("");
}

function npAddCandidateRow(cand) {
  const empty = npCandidates.querySelector(".empty-state");
  if (empty) empty.remove();
  npCandidates.insertAdjacentHTML("afterbegin", npRenderRowHTML(cand));
  const rows = npCandidates.querySelectorAll(".np-candidate");
  if (rows.length > 30) rows[rows.length - 1].remove();
}

function npRenderRowHTML(c) {
  const mintShort = (c.mint || "").slice(0, 8) + "…";
  const fmtSol = (n) => (n != null && n > 0) ? n.toFixed(2) + " SOL" : "—";
  const fmtUsd = (n) => {
    if (!n || n <= 0) return "—";
    if (n > 1e6) return `$${(n / 1e6).toFixed(2)}M`;
    if (n > 1e3) return `$${(n / 1e3).toFixed(1)}k`;
    return `$${n.toFixed(0)}`;
  };
  const social = [c.twitter && "𝕏", c.telegram && "✈", c.website && "🌐"].filter(Boolean).join(" ") || "—";
  return `
    <div class="np-candidate af-candidate" data-mint="${c.mint}">
      <div>
        <span class="af-name">${c.symbol || mintShort}</span>
        <span class="af-sub"> / ${c.name || "—"}</span>
        <div class="af-sub">${c.mint}</div>
      </div>
      <div>
        <div class="af-sub">Dev Buy</div>
        <div class="af-metric">${fmtSol(c.initial_sol)}</div>
        <div class="af-sub">MCap: ${fmtUsd(c.market_cap_usd)}</div>
      </div>
      <div>
        <div class="af-sub">Social</div>
        <div class="af-metric">${social}</div>
      </div>
      <div>
        <div class="af-sub">Fees Paid</div>
        <div class="af-metric">${(c.global_fees_sol || 0) > 0
          ? `<span style="color:${c.global_fees_sol >= npFeesThreshold() ? "var(--green,#26a69a)" : "inherit"}">${c.global_fees_sol.toFixed(3)} SOL</span>`
          : "—"}</div>
        <div class="af-sub">Curve: ${fmtSol(c.pool_sol)}</div>
      </div>
      <div>
        <div class="af-sub">Age</div>
        <div class="af-metric">${c.first_seen ? Math.max(0, Math.round((Date.now() / 1000 - c.first_seen))) + "s" : "—"}</div>
      </div>
      <div>
        <div class="af-sub">Action</div>
        <button class="btn btn-xs btn-primary" style="margin-top:4px"
          onclick="npManualStart('${c.mint}')">● Record</button>
      </div>
    </div>
  `;
}

function npManualStart(mint) {
  const tf = $("np-timeframe").value || "1s";
  apiFetch("/api/newpairs/session/start", { method: "POST", body: JSON.stringify({ mint, timeframe: tf }) })
    .then(r => {
      if (r.error) return alert(r.error);
      npAttachSession(r.mint, { timeframe: r.timeframe });
    })
    .catch(e => alert("Failed to start recording: " + e));
}
window.npManualStart = npManualStart;

/* ── Session viewer cards (mirror updateTraderCard, minus trading) ────── */

function npUpdateSessionCard(mint) {
  const ctx = npActiveSessions[mint];
  if (!ctx) return;

  let card = document.querySelector(`.np-session-card[data-mint="${mint}"]`);
  if (!card) {
    card = document.createElement("div");
    card.className = "np-session-card";
    card.dataset.mint = mint;

    const empty = npTradersGrid.querySelector(".empty-state");
    if (empty) empty.remove();

    card.innerHTML = `
      <div class="lt-card-header">
        <div><span class="lt-card-name" id="nph-name-${mint}"></span><span class="lt-card-symbol" id="nph-sym-${mint}"></span></div>
        <div style="display:flex;gap:6px;align-items:center">
          <span class="engine-badge" title="Recording mode (no engine)">REC</span>
          <div id="nph-change-${mint}" class="direction-badge" style="font-size:10px; padding:2px 6px;"></div>
          <div id="nph-status-${mint}"></div>
        </div>
      </div>
      <div class="lt-card-stats" id="np-stats-${mint}"></div>
      <div class="np-card-chart-container" id="np-chart-${mint}" style="height:250px; margin:10px 0; border:1px solid var(--border); border-radius:6px; background:#0d1117"></div>
      <div class="lt-event-log" id="np-events-${mint}"></div>
      <div class="lt-card-actions" style="display:flex; gap:8px;">
        <button class="btn btn-danger btn-xs" onclick="npStopSession('${mint}')" style="margin-left:auto;">⏹ Stop</button>
      </div>
    `;
    npTradersGrid.appendChild(card);

    const chart = LightweightCharts.createChart(document.getElementById(`np-chart-${mint}`), {
      layout: { background: { type: 'solid', color: 'transparent' }, textColor: '#8b949e', fontSize: 11 },
      grid: { vertLines: { color: '#30363d33' }, horzLines: { color: '#30363d33' } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: true, rightBarStaysOnScroll: true, shiftVisibleRangeOnNewBar: true, rightOffset: 5, barSpacing: 6 },
      crosshair: { mode: 0 }
    });
    const cSeries = chart.addCandlestickSeries({
      upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
      wickUpColor: '#26a69a', wickDownColor: '#ef5350',
      priceFormat: { type: 'custom', minMove: 1, formatter: p => formatMcap(p) }
    });
    const vSeries = chart.addHistogramSeries({
      color: '#5865f222', priceFormat: { type: 'volume' }, priceScaleId: 'vol'
    });
    chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });

    ctx.chart = chart;
    ctx.candleSeries = cSeries;
    ctx.volSeries = vSeries;

    new ResizeObserver(() => {
      const el = document.getElementById(`np-chart-${mint}`);
      if (el) chart.applyOptions({ width: el.clientWidth, height: el.clientHeight });
    }).observe(document.getElementById(`np-chart-${mint}`));
  }

  const name = ctx.info?.token_name || ctx.tokenName || mint.slice(0, 8);
  const symbol = (ctx.info?.token_symbol || ctx.tokenSymbol) ? "$" + (ctx.info?.token_symbol || ctx.tokenSymbol) : "";
  document.getElementById(`nph-name-${mint}`).textContent = name;
  document.getElementById(`nph-sym-${mint}`).textContent = symbol;

  const changePct = ctx.firstPrice > 0 && ctx.lastPrice > 0
    ? (ctx.lastPrice - ctx.firstPrice) / ctx.firstPrice * 100 : 0;
  const changeEl = document.getElementById(`nph-change-${mint}`);
  changeEl.textContent = `${changePct >= 0 ? "+" : ""}${changePct.toFixed(2)}%`;
  changeEl.style.color = changePct >= 0 ? CANDLE_UP : CANDLE_DOWN;

  document.getElementById(`nph-status-${mint}`).innerHTML =
    `<span class="lt-card-status running"><span class="lt-live-dot"></span>RECORDING</span>`;

  document.getElementById(`np-stats-${mint}`).innerHTML = `
    <div class="bt-stat"><span class="bt-stat-label">Trades</span><span class="bt-stat-value">${ctx.tradeCount || 0}</span></div>
    <div class="bt-stat"><span class="bt-stat-label">Candles</span><span class="bt-stat-value">${ctx.candleCount || 0}</span></div>
    <div class="bt-stat"><span class="bt-stat-label">Last Price</span><span class="bt-stat-value">${ctx.lastPrice ? ctx.lastPrice.toExponential(3) : "—"}</span></div>
  `;

  document.getElementById(`np-events-${mint}`).innerHTML = ctx.events.slice(0, 5).map(e =>
    `<div class="${e.type}">${e.ts} — ${e.msg}</div>`).join("");
}

function npAddSessionEvent(ctx, type, msg) {
  const ts = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  ctx.events.unshift({ type, msg, ts });
  if (ctx.events.length > 50) ctx.events.pop();
  npUpdateSessionCard(ctx.mint);
}

/* ── Attach to a server-side recording session (viewer card) ───────────── */

function npAttachSession(mint, opts = {}) {
  if (!mint) return;
  if (npActiveSessions[mint]) return;
  const timeframe = opts.timeframe || $("np-timeframe").value || "1s";

  const ctx = {
    mint,
    ws: null,
    info: null,
    tokenName: opts.tokenName || null,
    tokenSymbol: opts.tokenSymbol || null,
    timeframe,
    events: [],
    tradeCount: 0,
    candleCount: 0,
    lastPrice: 0,
    firstPrice: 0,
    baseMcap: null,
    basePrice: null,
    lastClose: null,
    lastTime: null,
    manualStop: false,
  };
  npActiveSessions[mint] = ctx;
  npStopAllBtn.style.display = "inline-flex";
  npAddSessionEvent(ctx, "info", opts.attach ? "Re-attaching to recording session…" : "Connecting…");
  npUpdateSessionCard(mint);

  const ws = new WebSocket(`${NP_SESSION_WS_BASE}/${mint}?timeframe=${encodeURIComponent(timeframe)}`);
  ctx.ws = ws;

  ctx.wsOpenTimer = setTimeout(() => {
    if (ctx.ws && ctx.ws.readyState === WebSocket.CONNECTING) {
      npAddSessionEvent(ctx, "error", "Connection timed out — retrying…");
      try { ctx.ws.close(); } catch (e) { /* ignore */ }
    }
  }, 25000);

  ws.onopen = () => {
    clearTimeout(ctx.wsOpenTimer);
    npAddSessionEvent(ctx, "info", "Connected — recording price action…");
  };

  ws.onmessage = async (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }

    if (msg.type === "session_info") {
      if (msg.token_name && !ctx.tokenName) ctx.tokenName = msg.token_name;
      if (msg.token_symbol && !ctx.tokenSymbol) ctx.tokenSymbol = msg.token_symbol;
      if (msg.no_motion_stop_seconds) ctx.noMotionStop = msg.no_motion_stop_seconds;
      npUpdateSessionCard(mint);
    }

    if (msg.type === "session_ended") {
      ctx.manualStop = true;
      npAddSessionEvent(ctx, "error", `🛑 Session ended (${msg.reason || "stopped"})`);
      const card = document.querySelector(`.np-session-card[data-mint="${mint}"]`);
      if (card) { card.style.borderColor = "var(--red)"; card.style.opacity = "0.7"; }
      setTimeout(() => npDetachSession(mint), 8000);
    }

    if (msg.type === "token_info") {
      ctx.info = msg.data;
      npAddSessionEvent(ctx, "info", `Token: ${msg.data.name || mint.slice(0, 8)}`);
      npUpdateSessionCard(mint);
    }

    if (msg.type === "historical") {
      npAddSessionEvent(ctx, "info", `Loaded ${msg.candles?.length || 0} recorded candles`);
      if (ctx.candleSeries && msg.candles) {
        const res = await npFormatCandles(msg.candles, ctx);
        ctx.candleSeries.setData(res.candles);
        ctx.volSeries.setData(res.candles.map(c => ({ time: c.time, value: c.volume || 0, color: c.color })));
        if (ctx.chart) ctx.chart.timeScale().scrollToRealTime();
      }
    }

    if (msg.type === "candle" && msg.candle) {
      const candle = msg.candle;
      if (ctx.candleSeries) {
        if (!ctx.baseMcap) {
          ctx.baseMcap = msg.market_cap_usd || (candle.close * 1e9 * 150);
          ctx.basePrice = candle.close;
        }
        if (msg.is_new) ctx.candleCount++;
        if (ctx.firstPrice <= 0) ctx.firstPrice = candle.close;
        ctx.lastPrice = candle.close;

        const close = ctx.baseMcap * (candle.close / ctx.basePrice);
        let open = ctx.lastClose !== null && ctx.lastClose !== undefined
          ? ctx.lastClose : ctx.baseMcap * (candle.open / ctx.basePrice);
        let high = ctx.baseMcap * (candle.high / ctx.basePrice);
        let low = ctx.baseMcap * (candle.low / ctx.basePrice);
        low = Math.min(open, low, close);
        high = Math.max(open, high, close);

        let color = CANDLE_FLAT;
        if (close > open) color = CANDLE_UP;
        else if (close < open) color = CANDLE_DOWN;
        else if (ctx.lastClose !== null && ctx.lastClose !== undefined) {
          if (close > ctx.lastClose) color = CANDLE_UP;
          else if (close < ctx.lastClose) color = CANDLE_DOWN;
        }

        ctx.candleSeries.update({ time: candle.time, open, high, low, close, color, borderColor: color, wickColor: color });
        ctx.volSeries.update({ time: candle.time, value: candle.volume || 0, color });
        ctx.lastClose = close;
        ctx.lastTime = candle.time;
        if (ctx.chart) ctx.chart.timeScale().scrollToRealTime();
      }
      if (msg.trade) ctx.tradeCount++;
      npUpdateSessionCard(mint);
    }

    if (msg.type === "ping") {
      ws.send(JSON.stringify({ type: "pong" }));
    }
  };

  ws.onerror = () => { npAddSessionEvent(ctx, "error", "WebSocket error"); };
  ws.onclose = () => {
    clearTimeout(ctx.wsOpenTimer);
    npAddSessionEvent(ctx, "info", "Disconnected");
    // The backend session keeps recording independently of this tab.  Probe
    // and re-attach if it is still alive (mirrors the live-tab contract).
    if (!ctx.manualStop && npActiveSessions[mint] === ctx) {
      setTimeout(async () => {
        if (ctx.manualStop || npActiveSessions[mint] !== ctx) return;
        try {
          const st = await apiFetch("/api/newpairs/status");
          const t = (st.sessions || []).find(s => s.mint === mint && s.status === "recording");
          if (t) {
            npDetachSession(mint);
            npAttachSession(mint, { attach: true, timeframe: t.timeframe, tokenName: t.token_name, tokenSymbol: t.token_symbol });
          } else {
            npDetachSession(mint);
          }
        } catch (e) {
          npDetachSession(mint);
        }
      }, 3000);
    }
  };
}

/* ── Candle formatting for newborn charts ──────────────────────────────── */

async function npFormatCandles(rawCandles, ctx) {
  // Newborn tokens have no USD price history — chart in SOL mcap space
  // (price SOL × 1e9 supply), which is the natural scale for bonding-curve
  // tokens and matches formatOfflineCandles' SOL fallback.
  if (!rawCandles || !rawCandles.length) return { candles: [] };
  let basePrice = 0;
  for (const c of rawCandles) {
    if (c.open > 0) { basePrice = c.open; break; }
    if (c.close > 0) { basePrice = c.close; break; }
  }
  if (!basePrice || basePrice <= 0) basePrice = 1;
  const baseMcap = basePrice * 1e9;
  const tfSec = timeframeToSeconds(ctx.timeframe);
  const formatted = [];
  let lastTime = null, lastClose = null;
  const seenTimes = new Set();
  for (const c of rawCandles) {
    if (lastTime !== null && lastClose !== null && c.time > lastTime + tfSec) {
      const gap = Math.floor((c.time - lastTime) / tfSec) - 1;
      if (gap <= 15) {
        for (let t = lastTime + tfSec; t < c.time; t += tfSec) {
          if (!seenTimes.has(t)) {
            seenTimes.add(t);
            formatted.push({ time: t, open: lastClose, high: lastClose, low: lastClose, close: lastClose, volume: 0, color: CANDLE_FLAT, borderColor: CANDLE_FLAT, wickColor: CANDLE_FLAT });
            lastTime = t;
          }
        }
      }
    }
    if (seenTimes.has(c.time)) continue;
    seenTimes.add(c.time);
    const closeVal = baseMcap * (c.close / basePrice);
    if (!closeVal || closeVal <= 0) continue;
    let open = lastClose !== null ? lastClose : baseMcap * (c.open / basePrice);
    let high = baseMcap * (c.high / basePrice);
    let low = baseMcap * (c.low / basePrice);
    high = Math.max(open, high, closeVal);
    low = Math.min(open, low, closeVal);
    let color = CANDLE_FLAT;
    if (closeVal > open) color = CANDLE_UP;
    else if (closeVal < open) color = CANDLE_DOWN;
    formatted.push({ time: c.time, open, high, low, close: closeVal, volume: c.volume || 0, color, borderColor: color, wickColor: color });
    lastTime = c.time;
    lastClose = closeVal;
  }
  return { candles: formatted, baseMcap, basePrice, lastClose, lastTime };
}

/* ── Stop / detach sessions ────────────────────────────────────────────── */

function npDetachSession(mint) {
  delete npActiveSessions[mint];
  const card = document.querySelector(`.np-session-card[data-mint="${mint}"]`);
  if (card) card.remove();
  if (Object.keys(npActiveSessions).length === 0) {
    npTradersGrid.innerHTML = '<div class="empty-state">No active recordings. Toggle the feed on or add a newborn mint above.</div>';
    npStopAllBtn.style.display = "none";
  }
}

function npStopSession(mint) {
  const ctx = npActiveSessions[mint];
  if (!ctx) return;
  ctx.manualStop = true;
  if (ctx.ws) ctx.ws.close();
  apiFetch("/api/newpairs/session/stop", { method: "POST", body: JSON.stringify({ mint }) }).catch(() => { });
  npDetachSession(mint);
}

function npStopAllSessions() {
  for (const mint of Object.keys(npActiveSessions)) npStopSession(mint);
  apiFetch("/api/newpairs/session/stop_all", { method: "POST" }).catch(() => { });
}

window.npStopSession = npStopSession;
window.npStopAllSessions = npStopAllSessions;

/* ── Form read/write ──────────────────────────────────────────────────── */

const NP_FIELD_MAP = [
  ["np-min-dev-buy", "min_initial_buy_sol", "float"],
  ["np-max-dev-buy", "max_initial_buy_sol", "float"],
  ["np-min-mcap", "min_mcap_usd", "float"],
  ["np-max-mcap", "max_mcap_usd", "float"],
  ["np-min-fees", "min_global_fees_sol", "float"],
  ["np-qual-timeout", "qualification_timeout_seconds", "float"],
  ["np-req-social", "require_social", "bool"],
  ["np-exclude", "exclude_mints", "str"],
  ["np-max-concurrent", "max_concurrent_sessions", "int"],
  ["np-timeframe", "session_timeframe", "str"],
  ["np-no-motion", "no_motion_stop_seconds", "float"],
  ["np-cooldown", "cooldown_seconds", "float"],
];

function npPopulateForm(snap) {
  for (const [id, key, type] of NP_FIELD_MAP) {
    const el = $(id);
    if (!el || snap[key] === undefined || snap[key] === null) continue;
    if (type === "bool") el.checked = !!snap[key];
    else el.value = snap[key];
  }
}

function npReadForm() {
  const out = {};
  for (const [id, key, type] of NP_FIELD_MAP) {
    const el = $(id);
    if (!el) continue;
    if (type === "bool") out[key] = !!el.checked;
    else if (type === "int") out[key] = parseInt(el.value, 10) || 0;
    else if (type === "float") out[key] = parseFloat(el.value) || 0;
    else out[key] = el.value.trim();
  }
  return out;
}

/* ── Toggle / buttons ─────────────────────────────────────────────────── */

npToggle.addEventListener("change", async () => {
  if (npToggle.checked) {
    localStorage.setItem("np_feed_enabled", "true");
    try {
      await apiFetch("/api/newpairs/config", {
        method: "POST",
        body: JSON.stringify(npReadForm()),
      });
      await apiFetch("/api/newpairs/start", { method: "POST", body: "{}" });
      npSettingsWrap.style.display = "block";
      npConnectWS();
    } catch (e) {
      npToggle.checked = false;
      localStorage.setItem("np_feed_enabled", "false");
      npSettingsWrap.style.display = "none";
      console.error("[NewPairs] start failed", e);
      alert("New Pairs feed start failed: " + e.message);
    }
  } else {
    localStorage.setItem("np_feed_enabled", "false");
    npSettingsWrap.style.display = "none";
    try {
      await apiFetch("/api/newpairs/stop", { method: "POST" });
      npSetStatus("off", "Stopped");
    } catch (e) { /* ignore */ }
    npDisconnectWS();
  }
});

npSaveConfigBtn.addEventListener("click", async () => {
  const cfg = npReadForm();
  try {
    const r = await apiFetch("/api/newpairs/config", {
      method: "POST",
      body: JSON.stringify(cfg),
    });
    npPreview.textContent = `Saved ${r.changed?.length || 0} field(s) ✓`;
    setTimeout(() => npPreview.textContent = "", 3000);
  } catch (e) {
    npPreview.textContent = "Save error: " + e.message;
    npPreview.style.color = "var(--red, #ef5350)";
  }
});

npAddBtn.addEventListener("click", () => {
  const mint = npTokenInput.value.trim();
  if (!mint) return alert("Enter a newborn token address");
  npManualStart(mint);
  npTokenInput.value = "";
});
npTokenInput.addEventListener("keydown", e => { if (e.key === "Enter") npAddBtn.click(); });
npStopAllBtn.addEventListener("click", npStopAllSessions);

/* ── Recordings list (separate newpairs DB) ────────────────────────────── */

function npRenderRecordingCard(rec) {
  const statusClass = rec.status === "recording" ? "status-recording" : "status-completed";
  const stopBtn = rec.status === "recording"
    ? `<button class="btn btn-danger btn-xs" style="margin-right:4px;" onclick="npStopRecordingById(${rec.id}, event)">⏹ Stop</button>`
    : "";
  return `
    <div class="recording-card" data-id="${rec.id}">
      <div class="rec-card-header">
        <div><span class="rec-card-name">${rec.token_name || 'Unknown'}</span> <span class="rec-card-symbol">${rec.token_symbol ? '$' + rec.token_symbol : ''}</span></div>
        <div class="rec-card-badges">
          <span class="rec-card-badge">AGE 0</span>
          <span class="rec-card-badge">${rec.timeframe}</span>
          <span class="rec-card-badge ${statusClass}">${rec.status}</span>
        </div>
      </div>
      <div class="rec-card-details">
        <span>🕐 ${fmtTs(rec.started_at)}</span>
        <span>📊 ${rec.candle_count} candles</span>
        ${rec.stopped_at ? `<span>⏱ ${fmtDuration(rec.started_at, rec.stopped_at)}</span>` : ''}
      </div>
      <div class="rec-card-mint">${rec.mint || ''}</div>
      <div class="rec-card-actions">${stopBtn}
        <button class="btn btn-primary btn-sm" onclick="npLoadViewer(${rec.id})">📊 View Chart</button>
        <button class="btn btn-danger btn-xs" style="margin-left:4px;" onclick="npDeleteRecording(${rec.id}, event)">🗑</button>
      </div>
    </div>`;
}

async function npLoadRecordingsList() {
  const list = await apiFetch("/api/newpairs/recordings");
  const el = document.getElementById("np-recordings-list");
  if (!el) return;
  if (!list.length) {
    el.innerHTML = `<div class="empty-state">No newborn-token recordings yet.</div>`;
    return;
  }
  // New-pair recordings accumulate at ~1k+/day — cap the rendered cards
  // (newest first, matching the backend's ORDER BY) so the DOM stays light.
  const MAX_CARDS = 150;
  const shown = list.slice(0, MAX_CARDS);
  el.innerHTML = shown.map(npRenderRecordingCard).join("") +
    (list.length > MAX_CARDS
      ? `<div class="empty-state" style="grid-column: 1/-1">Showing newest ${MAX_CARDS} of ${list.length} recordings — use Clean Up to prune dead stillborns.</div>`
      : "");
}

async function npStopRecordingById(id, e) {
  if (e) e.stopPropagation();
  const rec = await apiFetch(`/api/newpairs/recordings/${id}`);
  if (rec && rec.mint) {
    await apiFetch("/api/newpairs/session/stop", { method: "POST", body: JSON.stringify({ mint: rec.mint }) });
    const ctx = npActiveSessions[rec.mint];
    if (ctx) { ctx.manualStop = true; npDetachSession(rec.mint); }
  }
  npLoadRecordingsList();
}

async function npDeleteRecording(id, e) {
  if (e) e.stopPropagation();
  if (!confirm("Delete this newborn recording?")) return;
  await apiFetch(`/api/newpairs/recordings/${id}`, { method: "DELETE" });
  npLoadRecordingsList();
}

async function npCleanupRecordings() {
  if (!confirm("Clean up all new-pair recordings with fewer than 30 candles?")) return;
  const res = await apiFetch("/api/newpairs/recordings/cleanup", { method: "POST" });
  if (res.error) return alert(res.error);
  alert(`Cleaned up ${res.deleted_count || 0} recording(s) with < 30 candles.`);
  npLoadRecordingsList();
}

window.npStopRecordingById = npStopRecordingById;
window.npDeleteRecording = npDeleteRecording;
window.npCleanupRecordings = npCleanupRecordings;

/* ── New-pair recording viewer (chart from the separate DB) ───────────── */

let npViewerChart = null;

async function npLoadViewer(recordingId) {
  const rec = await apiFetch(`/api/newpairs/recordings/${recordingId}`);
  const candles = await apiFetch(`/api/newpairs/recordings/${recordingId}/candles`);
  if (!candles.length) return alert("No candles in this recording");

  document.getElementById("np-recordings-list").classList.add("hidden");
  document.getElementById("np-viewer-area").classList.remove("hidden");
  document.getElementById("np-viewer-token-name").textContent = rec.token_name || "Unknown";
  document.getElementById("np-viewer-token-symbol").textContent = rec.token_symbol ? `$${rec.token_symbol}` : "";
  document.getElementById("np-viewer-meta-tf").textContent = rec.timeframe;
  document.getElementById("np-viewer-meta-candles").textContent = `${candles.length} candles`;

  const wrapper = document.getElementById("np-viewer-chart");
  wrapper.innerHTML = "";
  if (npViewerChart) { npViewerChart.remove(); npViewerChart = null; }
  npViewerChart = LightweightCharts.createChart(wrapper, {
    layout: { background: { color: "#0d0f12" }, textColor: "#5a6071" },
    grid: { vertLines: { color: "#1e2330" }, horzLines: { color: "#1e2330" } },
    timeScale: { borderColor: "#1e2330", timeVisible: true, secondsVisible: true },
    rightPriceScale: { borderColor: "#1e2330" },
    width: wrapper.clientWidth, height: wrapper.clientHeight,
  });

  const res = await npFormatCandles(candles, { timeframe: rec.timeframe });

  const cs = npViewerChart.addCandlestickSeries({
    upColor: CANDLE_UP, downColor: CANDLE_DOWN,
    borderUpColor: CANDLE_UP, borderDownColor: CANDLE_DOWN,
    wickUpColor: CANDLE_UP, wickDownColor: CANDLE_DOWN,
    priceFormat: { type: 'custom', minMove: 1, formatter: p => formatMcap(p) }
  });
  cs.setData(res.candles);

  const vs = npViewerChart.addHistogramSeries({ color: "#5865f222", priceFormat: { type: "volume" }, priceScaleId: "vol" });
  npViewerChart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
  vs.setData(res.candles.map(c => ({ time: c.time, value: c.volume || 0, color: c.close >= c.open ? "#26a69a33" : "#ef535033" })));

  npViewerChart.timeScale().fitContent();
  new ResizeObserver(() => npViewerChart.applyOptions({ width: wrapper.clientWidth, height: wrapper.clientHeight })).observe(wrapper);
}

document.getElementById("np-viewer-back-btn").addEventListener("click", () => {
  document.getElementById("np-recordings-list").classList.remove("hidden");
  document.getElementById("np-viewer-area").classList.add("hidden");
  if (npViewerChart) { npViewerChart.remove(); npViewerChart = null; }
});

window.npLoadViewer = npLoadViewer;

/* ── Page-level refresh & lifecycle ───────────────────────────────────── */

function npRefreshPage() {
  // Pull latest status (attaches viewer cards for feed-spawned sessions)
  apiFetch("/api/newpairs/status")
    .then(snap => { if (snap && !snap.error) npHandleStatus(snap); })
    .catch(() => { });
  npLoadRecordingsList();
  // Keep the feed WS alive while the tab is open
  if (npToggle.checked) npConnectWS();
}

// Periodic status poll while the New Pairs tab is visible (sessions spawn
// server-side with no browser involvement — this is how the grid finds them).
setInterval(() => {
  if (document.hidden) return;
  const page = document.getElementById("page-new-pairs");
  if (!page || !page.classList.contains("active")) return;
  apiFetch("/api/newpairs/status")
    .then(snap => { if (snap && !snap.error) npHandleStatus(snap); })
    .catch(() => { });
}, 5000);

// Restore persisted feed toggle state on load (no wallet gate — recording
// never trades).
(async function npRestoreState() {
  try {
    const snap = await apiFetch("/api/newpairs/status");
    if (snap && !snap.error) npHandleStatus(snap);
    const savedEnabled = localStorage.getItem("np_feed_enabled") === "true";
    if (snap.is_running || savedEnabled) {
      npToggle.checked = true;
      npSettingsWrap.style.display = "block";
      localStorage.setItem("np_feed_enabled", "true");
      if (!snap.is_running) {
        try {
          await apiFetch("/api/newpairs/config", {
            method: "POST",
            body: JSON.stringify(npReadForm()),
          });
          await apiFetch("/api/newpairs/start", { method: "POST", body: "{}" });
        } catch (err) {
          console.warn("[NewPairs] Failed to auto-start backend feed", err);
        }
      }
      npConnectWS();
    } else {
      npToggle.checked = false;
      npSettingsWrap.style.display = "none";
    }
  } catch (e) {
    console.warn("[NewPairs] Restore state failed", e);
  }
})();

window.addEventListener("beforeunload", () => {
  try { npDisconnectWS(); } catch { /* ignore */ }
});
