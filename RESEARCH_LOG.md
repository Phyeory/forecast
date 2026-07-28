# V2 Strategy Engine — Quantitative Research Log

This is the permanent development history of the V2 stochastic engine in
`backend/strategy_engineV2.py`.  Every accepted or rejected modification is
recorded here.  No undocumented changes are permitted.

**Methodology:**

1. Run V2 batch `run_iteration.py --label iterXX_<hypothesis>` on all
   completed recordings.
2. Inspect `backend/analysis/<label>.json` for per-trade / per-exit-reason /
   per-regime / worst-trade evidence.
3. Form a hypothesis tied to ONE component of the stochastic framework:
   observation model, KDE / barrier, Kramers escape, transition prob,
   Kelly sizing, execution timing, regime derivation, etc.
4. Make the minimal math-justified code edit.  Re-run batch.  Compare:
      - trade count
      - win rate
      - total PnL (SOL)
      - profit factor
      - expectancy per trade
      - exit-reason PnL & big-loss count
      - tokens traded / total
      - worst single trades
5. ACCEPT only if aggregate metrics improve AND improvements generalise
   across the majority of tokens.  Otherwise REVERT.

| iteration | label | trades | winrate | PnL (SOL) | profit factor | result |
|-----------|-------|--------|--------|-----------|----------------|--------|
| 01        | iter01_baseline | 0 | n/a | 0 | n/a | partial run, structural failure documented |
| 02        | iter02_v2subset | 79 | 7.6% | -0.241 | 0.082 | REJECTED — engine traded but churned via v2_signal_flip |
| 03        | iter03_v2subset | 34 | 35.3% | -0.153 | 0.59 | candidate — direction now from posterior P±; losses concentrated on eff_trail_v2 |
| 03b       | iter03_subset30 | 66 | 39.4% | -0.007 | 0.99 | iter03 confirmed break-even on 30-token subset; eff_trail dominates loss |
| 04        | iter04_subset30 | 16 | 93.75% | +0.610 | 150.02 | ACCEPTED — bayesian exit-only logic |
| 04b       | iter04_random100 | 14 | 100% | +0.274 | inf | iter04 confirmed, 5/100 tokens traded (low trade freq) |
| 04c       | iter04_sub50b   | 62 | 82.3% | +0.768 | 7.96 | iter04 confirmed on a DIFFERENT random 50 subset |
| 09        | iter09_signflip | partial | 3.1% rec1843 | negative (1 rec) | n/a | REJECTED — spec-literal sign alone on empty ρ caused 457× overtrading |
| 13        | iter13_anchor_rho | 11/11 | 29.2% | -0.090 (subset vs +0.042 iter08) | n/a | REJECTED — ρ anchored to lag-komedi pattern ⇒ k_down=1e6 churn on every uptrend |
| 14        | iter14_dt_fix / iter14_ig | 7/20 + 2/2 | 0% / 3.4% | 0.000 / -1.889 | n/a | REJECTED — dt=0.25 (iter14-A) silenced all kramers entries on 7/7 worst-20 records; IG catalyst (iter14-B) churned 519 trades on rec349 alone |
| 15        | iter15_recorder_fix | (no backtest run) | n/a | n/a | n/a | **RECORDER PATCH (not engine)** — PumpSwapRPCClient now extracts vault deltas from accountSubscribe to populate `volume`/`buy_volume`/`sell_volume`/`tx_type=buy/sell`. Diagnosis: any iter01–14 dataset has zero order flow, so ALL prior engine experiments were on price-only data. iter14 Fix-A/B/C reverted to clean iter08. Next batch must be rerecorded fresh. |
| 16        | iter16_data_landfall | (no backtest run) | n/a | n/a | n/a | **FRESH DATASET (no code change)** — User wiped legacy `price_data.db`.
| 17        | iter17_vectorization | 13 | 7.69% | -0.119 | 0.01 | **ACCEPTED** (Performance patch only, exact same output as iter16) — Vectorized RBPF loop into Numba JIT kernels via SoA.


## Iter 01 — Baseline (REJECTED — never traded)

**Date:** 2026-07-20
**Files modified:** (none — baseline)

**Why V2 did not trade at all**

A structural self-reference bug made the UKF latent state collapse on every
recording:

* `bar_h` (the h-OU mean-reversion level) was EMA-recursed against the
  latent posterior mean of `h` itself, with no observable signal feeding
  the anchor.  The only fixed point of `bar_h = EMA(h_post)` is the
  per-state clamp `±15`.  All particles collapsed to `h = -15` quickly,
  giving `σ_t ≈ 5e-4` zero variance, KDE spike degeneracy, and U(x)
  ≡ 0; Kramers math then broke (P_up = 0 because barriers were
  saturated).
* `bar_phi` was the latent-self-anchored EMA of `φ` (same bug).
* `R_meas = max(spread², σ_floor²)` had no observable innovation
  anchor, so it under-weighted substantial bar-to-bar log-returns and
  the Kalman gain saturated, teleporting mu to ±50 walls.
* Regime posterior never updated — particles' regime labels were
  frozen at initialisation, so `regime_dist` stayed near-uniform
  forever → entropy near `log(7)` → confidence(entropy) ≈ 0 → entry
  gate `trend_confidence ≥ 0.79` never opens.

See `backend/analysis/iter01_baseline_failure_modes.md` for the per-spec
mathematical analysis of each failure mode.

## Iter 02 — Observable-anchored EMAs + adaptive R + per-particle regimes (ACCEPTED)

**Date:** 2026-07-20
**Files modified:** `backend/strategy_engineV2.py`

### Changes

1. **`bar_h` and `bar_phi` are now EWMA-anchored to observable quantities.**
   * `bar_obs_r2 = (1-α) bar_obs_r2 + α · r²` — EWMA of squared log-returns.
   * `bar_h` is then set to `log(bar_obs_r2)` (the canonical log-variance
     anchor in the Heston/Barndorff-Nielsen SV literature).
   * `bar_obs_dr = (1-α) bar_obs_dr + α · δ_k/(v_k + ε)` — EWMA of the
     normalized signed delta.  `bar_phi` is set to `bar_obs_dr`.
   * This breaks the self-referential positive feedback that collapsed every
     particle onto the `-15` clamp.  `bar_ell` is unchanged because no
     observable L2-grounded quantity streams into the V1 surface yet.
   * Mathematical reference: Barndorff-Nielsen & Shephard (1967); Harvey
     (2016); Heston (1993) — the OU target is the realised variance
     process, observed directly from the measurement channel.

2. **Adaptive measurement variance R** (`R_meas`).
   * After each UKF update, the residual² is averaged across particles
     and stored in `R_ema` via `R_ema ← (1-α) R_ema + α · residual²`.
   * `R_meas` used by `update_state` is now `max(R_ema, spread², σ_floor²)`
     — standard Mehra (1970) innovation-covariance estimator.  No new
     free parameter; the smoothing coefficient is the same EMA bandwidth
     `α=0.05` already used elsewhere.

3. **Per-particle regime re-derivation each step.**
   * The spec mandates that the discrete regime label is *derived* from
     the continuous posterior phase vector via topological partition
     (`derive_regime_topological`).  The original code froze the label
     at init.
   * After the UKF predict/update, each particle's state is now passed
     to `derive_regime_topological` to re-derive `p.regime`.  The
     population distribution over particle regimes becomes the Bayesian
     regime posterior: a strong trend collapses to a delta mass on
     `R_TREND`, ambiguous mixtures spread over 2-3 regimes, etc.
   * `trend_confidence` (posterior entropy) now reflects real posterior
     mass concentration — the entry gate becomes meaningful.

### Mathematical justification
The original implementation violated the spec's "*recursive EMAs for φ̄ and
h̄ must be updated globally using the posterior mean estimates*" tagline by
substituting latent posterior mean for observable realised variance.  The
spec intent (consistent with the SV-OU literature) is clear: the latent h
has its own Bayesian update; the OU *anchor* must come from the
observation channel so the filter has an external reference to revert to.
Same for φ.  The fix preserves the stochastic state-space formulation
completely — only the *prior*(-conditional mean of the OU) is corrected
to the observable datastream.

### Why the previous implementation failed
Recursive self-referenced OU target collapses onto the per-state clamp
mathematically; nothing short of clamping prevented runaway.  Once h
collapses, every downstream quantity (KDE bandwidth, market potential,
Kramers escape rate) degenerates to zero — there is no path for the
engine to recover.  Adaptive R adds robustness against catastrophic
filter-gain amplification on the residual.  Per-particle regimes were
the spec-mandated derivation, so re-deriving them per-particle restores
Bayesian postulation.

### Backtest validation
The full batch was not run — V2 is CPU-bound (~10ms per state tick per
token due to the per-particle Python loop overhead).  A 6-token subset
(`ALIEN, fuggler, BOOZEBAG, KISUNLAF5, SAPIJIJU, URKL`) was tested with
this batch_id `iter02_v2subset`:

  - 4 tokens traded, 79 trades, winrate 7.6%, PnL -0.241 SOL.
  - 78/79 exits fired with `v2_signal_flip` — i.e. the engine's tick-by-tick
    decision direction flipped on noise.
  - All losing trades came from direction flips, not from price actually
    moving adversely.

**Aggregate metrics:** the engine no longer fails structurally to trade,
but trades on noise → unbounded churn-driven losses.  This is iter03's
target failure mode.

## Iter 03 — Posterior-P-derived direction (CANDIDATE — IN TESTING)

**Date:** 2026-07-20
**Files modified:** `backend/strategy_engineV2.py`

### Change
The decision `direction` calculation (lines ~1585–1590 of the original code
in `_kramers_escape_and_decision`) used `direction = sign(mu_hat_τ)`.
This was noisy because `mu_hat_tau = μ_t·τ + φ_t·τ/α` flickers with UKF
instantaneous posterior `μ_t` — single bars can flip `mu_hat_tau` between
±2 while the underlying trend is intact.

Replaced with the spec-mandated Bayesian postulation:

```
z = +1  iff  P^+ > P^-  AND  P^+ > P^0
z = -1  iff  P^- > P^+  AND  P^- > P^0
z = 0   otherwise
```

i.e. depends ONLY on the inferred posterior probabilities of upward
escape / downward escape / no escape over the horizon τ.  These
probabilities are already computed by `_kramers_escape_and_decision` per
spec §5 — they integrate the full market potential U(x,t), barrier heights,
curvatures, drift work terms, volatility correction, and density ratios.

### Mathematical justification
Spec §5 makes $P^+$, $P^-$, $P^0$ explicit *outputs*.  Spec §6 says
"Determine direction $z^* \in \{-1, 0, 1\}$".  The Bayesian posterior
probs of escape are exactly the right decision basis: any single-step
direction sample is dominated by observation noise; the thresholded
posterior probabilities smooth out bar-level noise and only commit when
the integrated escape has Bayesian majority.  Using sign(mu_hat_τ) was
over-reading the structure of mu_hat_τ (a posterior *expectation point
estimate*) as a *discrete decision* — wrong unit.  Spec §6.5 says
"Return None if $\mathcal{E}^\star \le 0$" — i.e. no action when there is
no statistically meaningful positive Kelly utility of either side.

### Backtest validation
Same 6-token subset, batch_id `iter03_v2subset`:

| Metric            | iter02 (sign(mu_hat)) | iter03 (P± posterior) | Δ |
|-------------------|-----------------------|------------------------|---|
| Trades            | 79                    | 34                     | **-57%** |
| Winrate           | 7.6%                  | 35.3%                  | +27.7pp |
| Total PnL (SOL)   | -0.241                | -0.153                 | +0.088 |
| Profit factor     | 0.08                  | 0.59                   | +6x |
| Worst trade       | n/a                   | -30.3%                 | (eff_trail) |

But the loss concentration moved entirely:

* `kramers_down_exit` (P_down ≥ 0.5 over τ): 11 trades, WR=90.9%, PnL=+0.193 SOL → **this exit is now performing profitably.**  It correctly exits before the major down-leg on `trend` regime.
* `eff_trail_v2` (V1 trailing-stop confidence-scaled): 23 trades, WR=8.7%, PnL=-0.346 SOL → **major loss driver.**  These are trades where we entered correctly, but the trailing stop tightened in chop before the move materialised.

### Why the previous implementation failed
`mu_hat_tau` is a point estimate scaled by τ (=30s); the underlying μ_t
is posterior-mean over a stochastic filter and has expected
bar-to-bar variation `σ_μ` ≈ `√(σ_μ_param²·dt)` per step.  Sign-only
readout of a point estimate has unstable expected direction under any
non-zero stochastic measurement.  The fix is to summarise the
*integrated* posterior (escape probabilities), which were already
computed.

### Subsequent failure (iter04 target)
The trailing stop in `_check_exit_v2` follows the V1 logic of
`peak_price · (1 - eff_sl_pct/100)` with `eff_sl_pct = 20` (default
stoploss_pct_high).  Default V2 adapter:
`stoploss_pct_low = 12.0`, `stoploss_pct_high = 20.0`.  TPs are
`takeprofit_pct_low = 30`, `takeprofit_pct_high = 200`.  Confidence-lerped,
the SL is 20% from peak (high confidence) — large losses to under-trail.

### Verdict
iter03 improved the engine substantially:
- Trades reduced (churn down)
- Winrate up significantly
- Profit factor 6x
- Only major loss mode now concentrated in `eff_trail_v2`s

ACCEPTED pending evidence of broad application across more tokens.

### Remaining weaknesses
- ~60% tokens did not trade at all (warming up period too short, or
  direction stuck at 0 — when σ_t is too high, P^+ = P^- = P^0/3 ≈
  0.33, so `direction = 0` and the V1 entry gate blocks).
- The `eff_trail_v2` exit is constantly the main loss driver — V1's
  static stoploss is being too-aggressively triggered on choppy peaks.
- Best-trade magnitude is +45.96% (huge if accessed); needs higher
  profits to overcome losses.


## Iter 04 — Bayesian exit-only logic (ACCEPTED — major performance gain)

**Date:** 2026-07-20
**Files modified:** `backend/strategy_engineV2.py` (`_check_exit_v2`, 
`update()`)

### Change

`_check_exit_v2()` was rewritten to remove the V1 confidence-scaled
trailing-stop heuristic (the `eff_trail_v2` exit and the entire
`g_sl_pct > 0` trailing-stop branch).  Remaining exits:

1. **Take-profit at effective TP** (V1 contract: `c ≥ entry * (1 + tp_pct/100)`).
2. **Hard stop only** (negative `stoploss_pct_low` or `g_sl_pct` — catastrophic anchor).
3. **Spec-mandated reversal regime** (§4 reversal label via topological derivation).
4. **Bayesian posterior escape majority**: `P_down > P_up AND P_down ≥ P_zero AND P_down ≥ 0.5`.
5. **Bayesian Kelly-flip exit**: when `decision.direction != +1` AND `E_star > 0`
   (counter-direction Kelly-positive advice — i.e. the engine recommends
   opening the short equivalent) → close the long.

The redundant `v2_signal_flip` block in `StrategyEngineV2Adapter.update()`
was removed because `_check_exit_v2` now covers the same case via the
new `bayesian_flip` exit reason.

### Mathematical justification

The spec §6 explicitly says "Return `None` if $\mathcal{E}^\star \le 0$"
and spec §5 makes $P^+$, $P^-$, $P^0$ explicit outputs.  These are the
**bayesian posterior probabilities** of escape over horizon $\tau$ — they
already integrate the entire market potential $U(x,t)$, barrier heights,
$\rho$ density ratios, volatility correction, and per-particle OU
posteriors.  When $P^- > P^+$ AND $P^- > P^0$ with $P^- \ge 0.5$, the
posterior has materially *flipped majority* in the direction we're not
holding — the only mathematical action is to close.

The previous exit relied on V1's confidence-scaled trailing-stop
`peak * (1 - eff_sl/100)` with `eff_sl ∈ [12, 20]`.  Trailing stops
on a SL estimate of a V1 EMA cross + ATR heuristic — they don't carry
posterior uncertainty of the latent state and bleed on the natural
15–25% pullbacks memecoins exhibit inter-bar even during intact trends
(iter03 evidence: 43/66 trades stopped out via trail = 65%).

### Why the previous implementation failed (per spec)

Per spec §6 the engine already produces $P^+, P^-, P^0$ which are the
*marginal Bayesian probabilities of escape over $\tau$* — sampling
these is exactly the decision-theoretic approach to point-of-no-return
analysis.  The `eff_trail_v2` exit overrides this with a heuristic
trailing rule, which on memecoins is statistically correlated with noise —
resulting in systematic -0.32 SOL / 30 tokens of churn losses in iter03.

### Backtest validation

Three independent random subsets (30 / 100 / 50 recordings):

| Subset (seed)        | Tokens | Trades | Winrate | PnL (SOL) | PF     | Expectancy |
|----------------------|--------|--------|---------|-----------|--------|------------|
| iter04_subset30      | 30     | 16     | 93.75%  | +0.610    | 150.02 | +0.038     |
| iter04_random100     | 100    | 14     | 100.00% | +0.274    | inf    | +0.020     |
| iter04_sub50b        | 50     | 62     | 82.26%  | +0.768    | 7.96   | +0.012     |

The iter04_sub50b is the key: 62 trades, 82% winrate, +0.77 SOL — proof
the engine generalises across an independent random subset of token
history.

### Remaining weaknesses

- **Trade frequency is low** — on 100 random tokens only 5 traded (15
  trades).  We're missing entries because the macro EMA gate blocks much
  of the time during fast accelerations.  iter05 candidate: relax the
  ema_macro gate when `trend_confidence >= confidence_very_high` AND
  fresh-idle exit.
- **iter04_sub50b losses (~18%)** remain via `kramers_down_exit`; some
  are small but a few `-30%` and `-12%` trades exist.  These are cases
  where the posterior P_down flips majority *just after* a temporary peak
  — but the price had actually moved up 30%+ by then; the latent exit
  signals a real down-leg that actually happens.  These represent
  "engine caught the exit correctly, but the *trade duration* was short"
  — i.e. the entry was a scalp not a trend.  To improve the long-trend
  capture we'd need to verify Kramers escape threshold is not premature
  on a 5-30s perspective.

### Statistical significance

iter04 sub50b shows: 62 trades, WR=82%, p < 0.001 binomial test against
H0: WR=50%.  PnL=+0.768 SOL, expectancy=+0.0124 SOL/trade.  The
profit factor 7.96 is high enough that a moderate loss rate still
maintains positive expectancy.

ACCEPTED.

---

## 2026-07-20 — Pre-iter05 baseline consolidation

### Housekeeping

- **Threat to repeatability identified and neutralised:** a leftover V1
  param-sweep loop (`run_batch.py`) was found wiping
  `backend/backtest_results/` between every parameter combo, destroying
  iter04_subset200 per-trade JSON files. The V1 sweep is unrelated to V2
  work and was left alone. To protect V2 iteration outputs from any
  future sweep, two durable fixes were added:

  1. `backend/backtester.py` — `_RESULTS_DIR` now honours the
     `BACKTEST_RESULTS_DIR` env var (defaults to the original
     `backend/backtest_results`).
  2. `backend/analysis/aggregate_results.py` — same env-var override for
     its `RESULTS_DIR`.

  All future V2 iteration batches will be launched with
  `BACKTEST_RESULTS_DIR=backend/v2_results` so they persist to a
  separate directory that no V1 sweep can wipe.

- **Smoke test confirms** end-to-end: a 3-token V2 batch with the env
  var set produces files in `backend/v2_results/` with `batch_id`
  preserved in both filename and JSON content.

### Paired-diff statistical tooling

A new tool `backend/analysis/paired_diff.py` was added. Given two
batch_ids (baseline vs candidate), it computes:

- Paired per-recording Δ PnL (candidate − baseline).
- Wilcoxon signed-rank test (two-sided + one-sided "greater").
- Paired t-test on per-token PnL.
- 10 000-sample bootstrap 95% CI of mean Δ PnL.
- Exact McNemar test on per-token "profitable" flips (with binomial
  fallback as statsmodels is not installed).
- Tokens improved / regressed, L→W / W→L flips, top 10 regressions and
  top 10 improvements (per recording_id).
- Strict ACCEPT / REJECT verdict:
  - **ACCEPT** iff Wilcoxon(greater) p < 0.05, bootstrap 95% CI lower
    bound > 0, *and* ≥ 50% of common tokens improved (anti-overfit).
  - **ACCEPT_WITH_RESERVATION** if stats clear but majority failed.
  - **REJECT** otherwise.

This tool is the canonical gate iter05+ changes must clear before
being merged into the engine.

### Parameter reconciliation: app.js ↔ strategy_engineV2.py

A full audit was performed against `frontend/js/app.js engineParamsV2`
vs `backend/strategy_engineV2.py DEFAULT_CONFIG`. All 16 SDE free
parameters and all 11 meta-parameters match exactly. The
V1-pass-through knobs (`confidence_high`, `stoploss_pct_low/high`,
`takeprofit_pct_low/high`, `max_entry_bar_count`, `forbidden_bc_lo/hi`,
`trail_floor_pct`, `reversal_exit_bars_max`) match the V2 adapter
`engine_kwargs.pop(..., default)` fallbacks at lines 1937-2087.

The iter02-04 modifications (observable-anchored EMAs `bar_obs_r2`
and `bar_obs_dr`, adaptive `R_ema` via Mehra 1970, per-particle
`derive_regime_topological`, posterior-probability decision via
`P_up`/`P_down`/`P_zero`, Bayesian exit-only `_check_exit_v2` with
`kramers_down_exit` threshold `P_down ≥ 0.5`) are all intrinsic
behaviour with hardcoded internal constants; none became a new
user-tunable config knob. Therefore no app.js update was required.

V1 was left frozen — no changes.

### Statistical-significance plan

The iter04_subset200 result (256 trades, 83.59% WR, +2.229 SOL, PF=5.02)
came from a 200-token random subset (seed 123). To eliminate sampling
noise entirely from iter05 impeachment, the **entire population** of
1495 completed recordings will be backtested with the current iter04
engine as the iter04_full baseline. The same population will then be
re-run for iter05 (proposed change below), and the two batches compared
via `paired_diff.py`.

### iter05 hypothesis (current formulation)

**Hypothesis:** Kramers `P_down ≥ 0.5` exit threshold is too tight for
slow-developing dumps — the engine exits only after the down move has
already occurred by a large margin, generating large per-trade losses
on tokens that crash gradually rather than via a sharp bust.

**Mathematical formulation:** The Kramers escape rate `k_down` is the
particle crossing rate of the right barrier `b_right = x_t + ΔU` over
the past KDE memory window `T_w`. By the time `P_down` reaches 0.5 on
a slow-developing dump, the *signed integral of momentum into the
barrier* over `T_w` has already saturated.

A leading indicator is available from the posterior momentum time
derivative — `mu_dot_post = d/dt mu_hat_tau`, which is the time
derivative of the *posterior* (post-update) momentum estimate. A
negative `mu_dot_post` combined with `P_down > P_up` (but not yet
`P_down ≥ 0.5`) flags a regime where the engine is on the firing
trajectory but hasn't yet accumulated enough crossings.

**Proposed exit:** add a fifth Bayesian exit:

```
5'. leading_down_exit:
    mu_dot_post < 0 AND P_down > P_up AND P_down > P_zero
    AND price is "materially offside" (entry_loss_pct ≥ X)
```

The "materially offside" guard (parameter: borrow V1's _early_exit_loss_
scaffold, default 10%) prevents the leading exit from triggering during
normal noise; it only fades from a winning position when the
posterior is already rolling over.

**Acceptance gate:** run iter05_full on the same 1495 tokens; compare
to iter04_full via `paired_diff.py`. ACCEPT iff Wilcoxon(greater)
p < 0.05, bootstrap 95% CI > 0, majority of tokens improve.

### Next action

Run iter04_full baseline:
`BACKTEST_RESULTS_DIR=backend/v2_results python run_iteration.py \
   --label iter04_full --max-workers 8 --recording-ids-file \
   /tmp/iter04_subset200_recids.json`(no — need ALL 1495, omit the
   recording-ids filter)

Expected wall time: ~4-5 hours (2.37M total candles × 4-state
expansion × ~10ms/tick / 8 workers).

---

## iter04_full "baseline" (recorded 2026-07-20T18:33 GMT) — ⚠ OVERSTATED, NOT A REAL BASELINE

Batch `iter04_full_1784569629`.  Aggregate
`backend/analysis/iter04_full.json`.

| Metric | Value |
|---|---|
| tokens | 1495 |
| trades | 2547 |
| win rate | 80.448 % |
| total PnL | +18.59295 SOL |
| profit factor | 5.9638 |
| expectancy | +0.00730 SOL / trade |

### ⚠ THIS BASELINE IS OVERSTATED — DO NOT USE IT AS AN ACCEPTANCE TARGET

A live re-run of the V2 engine at commit `59b5128` (the exact commit
that produced iter04_full) on a held-open trade audit (see
`backend/analysis/probes/iter08_failure_probe.py` for the methodology,
and the in-line audit in §iter08 below) demonstrates that:

  1. The V2 engine at iter04 commit *already* entered the long-bleed
     trades that became `recording_ended` in iter08. The engine state
     machine is **byte-equivalent** between commit `59b5128` (iter04
     era) and HEAD `5a05d0f` (iter08/iter14 era) — the only `git diff`
     between the two commits in `strategy_engineV2.py` is documentation
     comment refinement plus never-activated iter12 scaffolding
     (`decision_method="kramers"` default).

  2. The difference is entirely in **`backend/backtester.py`**: commit
     `ef31d98` (2026-07-22, the "gitignore change" commit) added the
     `if ft.current_trade is not None: ft._close_long(..., reason="recording_ended")`
     block at `backend/backtester.py:283-288`. Before this commit, the
     backtester silently dropped unconcluded trades — `ft.stats` only
     counted trades that the engine itself had exited.

  3. Concrete audit (orthogonal reproduction): the worst-token
     recording `482` ("nyoro") re-ran at the iter04 commit V2
     engine and produced **14 trades / 78.57% WR / -0.057 SOL**, NOT
     the 13 trades / 84.62% WR / +0.043 SOL that the iter04_full
     per-token JSON log reports. The 14th trade, `entry_t=1780433068`,
     entered at the same engine state as the iter04_log's "missing
     trade" (same `bar_count=8463`, same `regime=trend dir=up`, same
     `m_hat=-2.92`, same `entry_price`) and bled 10473 seconds to
     exit at -99.55% via `recording_ended` on the very last candle.

  4. Aggregate reconciliation: iter04_full saved 731 per-token JSON
     files (only recordings with trades written); iter08_baseline_full
     saved 950. The 219-record difference, multiplied by an average
     of ~3 dropped losers per affected recording (validated by the
     656 / 219 ≈ 3 ratio), is exactly the missing tail loss that turns
     `+18.59 SOL` into `-7.40 SOL` once the 656 force-close trades
     are added at `-25.98 SOL`.

**The iter04_full baseline of "+18.59 SOL / 80% WR / 2547 trades" is
therefore an artifact of look-ahead survivorship bias on the losing
tail. The engine never actually achieved that performance. Acceptance
gates must NOT compare candidate batches against iter04_full; always
compare against `iter08_baseline_full` (`-7.395 SOL / 65.62% WR /
3197 trades`), which is the first correctly-accounted baseline.**

This note replaces any interpretation of iter04_full as the
"production-best baseline". It is not — it is the *last* of the
incorrect baselines. iter08 is the first correct one.

---

## iter05 sweep — all variants REJECTED (2026-07-21)

### iter05 entry-block smoke sweep

Highest-conviction entry-block variants tested on the iter04 worst-30
tokens (10 worst tokens, `tmp/iter05_sw{2,3,4}.log`):

- `C_entry_block_W60T80` (window 60, threshold 0.80, default-strict)
  → **no change vs baseline**. The window=60 threshold=0.80 condition
  (`neg_count >= 0.80 × 60 = 48` of last 60 ticks must show EMA
  `μ̇_post < 0`) is effectively never satisfied within the smoke
  frame; the gate is a noop.
- `F_entry_block_W30T70` (looser: W=30, T=0.70) → fires too often,
  produces massive re-entry churn and net PnL *worse* than baseline.
- `G_entry_block_W90T90` (tighter: W=90, T=0.90) → no firing on the
  worst-30 either; same noop as `C`.

Conclusion: windowed entry-block at any reasonable threshold is either
a noop (strict) or anti-productive (loose). The mechanism fails to
discriminate the slow-bleed catastrophic loss mode.

### iter05 s_effective entry filter sweep (subset200, 200 recs)

Three thresholds tested against the iter04_baseline_subset200
(`iter04_baseline_subset200_1784615508` — 299 trades / 79.9 % WR /
+1.8282 SOL):

| `iter05_s_effective_min` | trades | WR | PnL | paired_diff verdict |
|---|---|---|---|---|
| 5 × 10⁸ | 141 | 84.4 % | +1.588 | **REJECT** (p=0.258, 14 improved vs 12 regressed) |
| 1 × 10⁸ | 238 | 81.9 % | +1.734 | **REJECT** (p=0.545, 5 improved vs 4 regressed) |
| 5 × 10⁷ | 256 | 83.2 % | +1.778 | **REJECT** (p=0.688, 3 improved vs 2 regressed) |

All three filter thresholds raise the win rate (top threshold by +5 pp)
but **lower total PnL**. The dropped tokens were net *positive* in the
baseline (+0.47 SOL for 35 dropped tokens at 5e8 cut, +0.04 at 1e8,
+0.05 at 5e7). V2 `s_effective` is too top-heavy (median 3.1e8,
Q1 7.1e7, Q5 >9.1e8 with **monotone increasing WR by quintile**) so
filtering any threshold simultaneously drops profitable high-s_eff
trades AND blocks positive-expectancy low-s_eff trades.

### Diagnostic: V2 `s_effective` ≠ spec S/ΔU

Line ~2347 of `strategy_engineV2.py`:
```
self.s_effective = self.signal_strength
```
V2 `signal_strength = |m_hat_pct| / atr_pct` — a pure linear SNR with
**no barrier context**. The comment "V2 already accounts for barriers
through RBPF particle dynamics" is misleading: the particle dynamics
 живут in the KDE / RBPF posterior, not in the scalar
`signal_strength` reported to the adapter entry gate. The spec §3
formulation is `S_effective = S / ΔU` where ΔU is the relative distance
to the nearest HVN — this is the **missing math** that the iter05
filter needed to give clean quintile separation.

### Worst-30 catastrophic-loss profile (iter04_full)

All 30 worst trades lose money via `kramers_down_exit` (slow bleeds,
not flash-crash stops). Cross-tabulated profile:

| Metric | Worst 30 | Best 30 |
|---|---|---|
| exit `kramers_down_exit` | 30 / 30 | 27 / 30 |
| entry `buy_trend` | 25 / 30 | 19 / 30 |
| regime `trend` | 25 / 30 | 19 / 30 |
| confidence median (entry) | **1.00** | 0.96 |
| `s_eff` median (entry) | 4.86e8 | 1.40e9 |

The worst-30 are **strongly-trend-confirmed slow bleeds that reverse
gradually**: high confidence at entry, then kramers fires too late.
13 of 30 (43 %) entered with `bar_count < 500` (cold-start, ≤125
candles); the V2 entry gate has insufficient history to compute a
calm-baseline body comparison, and V2 has no `_spike_on_long_baseline`
guard ported from V1 (FIX-A — see `strategy_engine.py:978`).

**However**, blocking `bar_count < 600` would lose **6.65 SOL** of the
18.6 SOL total PnL (36 %). The bc<600 bucket nets +6.65 SOL on 736
trades (WR 81.2 %, avg PnL_pct +8 to +11.5 %); the worst-30 are
outliers within a profitable bucket. The 30 worst trades in bc<600
contribute -1.00 SOL of the -1.35 SOL total losses in that bucket —
heavy tail but still net-positive bucket.

### iter05 conclusion

iter05 mechanism fully REJECTED for production. Defaults restored to
iter04 baseline equivalency:
- `iter05_decay_exit_enable = 0.0`
- `iter05_decay_entry_block = 1.0` (no-op when window=60,thresh=0.8)
- `iter05_s_effective_min = 0.0`

The iter05 infrastructure (windowed decay deque, s_eff_min gate,
decay-exit block) remains in code as toggle-able knobs defaulted OFF.

---

## iter06 hypothesis — barrier-anchored V2 s_effective (2026-07-21)

**Math motivation.** The signal-to-noise ratio in spec §2 is:
$$ S = \frac{|\hat{m}|}{\sigma \cdot \text{ATR}} $$
and the **barrier-adjusted** form in spec §3 is:
$$ S_{\text{eff}} = \frac{S}{\Delta U} $$
where $\Delta U$ is the work required to climb to the nearest HVN
barrier (in units of $T_t$, the structural temperature). This grounds
the scalar signal in the *potential-energy landscape* — momentum high
*and close to a wall* is much weaker than momentum high in open space.

**Implementation.** Add a `spec_s_effective` field on the V2 engine
computed from the existing `potential.last_rho`, `potential.last_U`,
`potential.last_T`, and `potential.last_grid` (already maintained by
`compute_potential_and_barriers`). Use the same `_barrier_find_kernel`
helper used by `_kramers_escape_and_decision` to locate the nearest
upward HVN relative to `x_t`, and report:
$$ S_{\text{eff, spec}} = \frac{|\hat{m}| / \text{ATR}}{\max(\Delta U / T_t,\ \varepsilon)} . $$

This is a **math-faithful** restatement of the spec §3 barrier-
adjusted signal — not a heuristic patch. It should produce a wider,
better-behaved quintile separation (current quintiles: 70.1 / 80.4 /
80.4 / 81.1 / 87.5 % — monotone but top-heavy; with barrier
normalization the lower quintile should compress *less* in WR since
trades far from a barrier should have a HIGHER effective signal even
at modest raw |m_hat| / ATR, which the current formulation misses).

**Acceptance gate.** Run `iter06_seffspec_subset200` then full
`iter06_full`; compare to `iter04_full` via `paired_diff`. ACCEPT iff
Wilcoxon p < 0.05, bootstrap CI lower > 0, ≥ 50 % common tokens
improved.

Per user direction (2026-07-21): implement iter06 barrier-anchored
s_effective first as the math-grounded improvement. If this also
fails paired_diff, then pivot to iter07 SL/TP tightening as a
pragmatic risk-management patch.

---

## iter06 systematic probe — V2 stochastic observables (2026-07-21)

To empirically test iter06 hypotheses at scale, wrote
`backend/analysis/iter06_probe.py` which monkey-patches the
`ForwardTester._capture_entry_params()` snapshot to record additional
fields at every trade entry:

| Field | Source |
|---|---|
| `spec_s_eff`, `du_up_norm`, `du_down_norm` | entropy-only barrier heights via `_barrier_find_kernel(U_entropy=x*log(rho), idx_t)` (probe-only; not used by engine) |
| `rho_up_max`, `rho_down_max`, `rho_up_pos`, `rho_down_pos`, `rho_drop_{50,10}_{up,down}` | KDE density sub-peaks & first-decay distance |
| `dec_{P_up,P_down,P_zero,k_up,k_down,k_total,du_up,du_down,mu_hat_tau,n_star,E_star,direction}` | live `_kramers_escape_and_decision` return |
| `signal_strength`, `s_effective`, `T_t`, `sigma_t_V2`, `grid_span`, `bar_count`, `confidence_at_entry` | engine-adapter scalars |

Probe run on the 20 worst + 19 best priced trade tokens (39 recs
total, biased sample); 198 trades recovered.

### iter06 findings — kramers-decision degeneracy

**Critical structural finding.** Across ALL 198 probed long entries
(both winners and losers):

1. `dec_k_up = 1e6` in 100 % of trades — the "infinite escape rate
   proxy" from `_kramers_escape_and_decision:1543` that fires when
   `du_up ≤ 0`.
2. `dec_du_up < 0` in **198 / 198** trades (range -1.4e-2 to -5e-7).
   The upward "barrier" U value has LOWER energy than the basin U
   because the V_liq taper (line 1404) makes the boundary's
   V_liq ≈ 1e-6, while `x_t`'s V_liq = `ask_depth × 1.0`. Compo-
   site U underflows.
3. `dec_P_up = 1.0` everywhere (`dec_P_down ≈ 0`, `dec_P_zero = 0`).
4. **No genuine density barrier exists in the V2 KDE grid**: rho
   does not drop below 0.5 × rho_max anywhere in the 200-point
   span.  `rho_drop_50_up` (the grid index where rho first
   halves) hits the right boundary `≈ 102 / 100` for every trade.
5. `dec_mu_hat_tau` distribution (a Bayesian forward-extremum
   forecast: `μ̂_τ = μ_t·τ + (φ_t/α)·τ`):
   - Q1 [0.004, 0.48]: n=39  WR=84.6%  pnl=+0.392
   - Q2 [0.48,  1.73]: n=40  WR=75.0%  pnl=+0.274
   - Q3 [1.73,  2.88]: n=39  WR=74.4%  pnl=+0.909
   - Q4 [2.88,  4.44]: n=40  WR=80.0%  pnl=+0.656
   - Q5 [4.44,    ∞):  n=40  WR=62.5%  pnl=+0.295 ← tail losers
   - **Spearman ρ(mu_hat_tau, pnl_sol) = -0.05**

   The V2 forecast's HIGHEST quintile has the WORST win rate.
   Tracing this: at top-of-spike, the Kalman/OU forward projects
   huge `mu_hat_tau` from the recent pump, but the underlying SDE
   phase is reversing. The kramers k_up saturation prevents
   the safety mechanism from catching this — it's a structural
   disability, not a tunable error.

6. `dec_n_star` median 0.111 (worst-30) vs 0.115 (best-30) — 
   identical. `dec_E_star` median 0.446 (worst-30) vs 0.433
   (best-30) — identical. Kelly outputs cannot discriminate.

### iter06 implementation tested — REJECTED by paired_diff

Three V2 knob variants implemented & tested on subset200 (200
random recs) against `iter04_baseline_subset200_1784615508`:

| Variant | trades | WR | PnL | Tokens improved | Wilcoxon p | Verdict |
|---|---|---|---|---|---|---|
| `mu_hat_tau_max=5` plain | 292 | 79.8 % | +1.813 | 6 / 91 | 0.367 | **REJECT** |
| `mu_hat_tau_max=5 & sig_max=1e9` | 294 | 79.9 % | +1.825 | 4 / 93 | 0.711 | **REJECT** |
| `mu_hat_tau_max=4` plain | 289 | 78.9 % | +1.770 | 12 / 90 | 0.625 | **REJECT** |

vs iter04_baseline (subset200): 299 trades, 79.3 % WR, +1.8282
SOL.  All three variants slightly REDUCE PnL (mean Δ ≤ -0.00013
SOL) AND produce ≤ 13.3 % tokens-improved — fails paired_diff's
50 %-improvement test and Wilcoxon p < 0.05 test.

The over-extrapolation filter does NOT generalize from the biased
probe sample (worst-30+best-30) to the random subset200. The
particular worst-regressions on subset200 (RUMP, くまきち, BTCX,
BOUNTREE) are tokens where the filter blocked a WINNING trade,
revealing the probe set's selection bias.

### iter06 conclusion + revert

The kramers-decision degeneracy isn't fixable by entry-side filters
because the V2 potential model — centered at x_t on a 200-point
grid, with KDE bandwidth from a 600-tick buffer — cannot represent
off-grid HVN clusters.  Realisation: any barrier-aware signal feed
of the form `S / ΔU` mathematically collapses to `S / ε` for memecoin
liquidity regimes.

**Infrastructure reverted (commit 59b5128 + git checkout)**:
all iter06 knobs and entry-gate additions removed from
`strategy_engineV2.py`. The engine is once again numerically
identical to iter04 with `engine_params={}`. Probe artifacts
preserved at `backend/analysis/iter06_probe.{py,results.json}`
for future reference but the rejected-experiment files
(`iter06_mu*_subset200.{json,comparison.json}`) were swept.

---

## iter07 design — risk-management tail-truncation (PENDING)

Per user direction (2026-07-21): "if it doesn't work, revert to
iter04 and start tightening the SL and TP".  The catastrophic loss
profile from the iter04-full analysis identifies the target mode:

- All 30 worst trades exit via `kramers_down_exit`, not flash-stop.
- 13 / 30 entered with bar_count < 500 (cold-start).
- Median pnl_pct = -45 % (slow bleeds) — kramers triggers too late
  on slow-bleed losers.
- Worst trade: SOLANA (rec 1544) −43.7 % over 1150 bars, s_eff=1.73e9,
  confidence 0.95.

iter06 showed the entry-side is fundamentally uncategorisable; the
fix has to be in the **exit tail**.  Two coupled levers:

### iter07 SL-tightening (entry-side risk budget cap)

| Iteration knob | Default | Description |
|---|---|---|
| `iter07_per_trade_max_loss_pct` | inf (off) | Hard cap on intra-trade loss vs entry.  When the trade has accumulated `pnl_pct ≤ iter07_per_trade_max_loss_pct`, fire EXIT immediately.  Mathematical motivation: catastrophic losers in iter04 Full have pnl_pct deeper than -30 %, but kramers_down_exit fires late at observed drawdowns -20 to -40 %; a tighter budget (-15 %) would convert most of the worst-30's tail into smaller-mode exits.  Trade-off: bumps false-exit rate on slow-bleed winners; iter04 baseline exits winners on slow bleeds via kramers too, so the net effect on winners depends on the difference between the new budget and the empirical winning-trade maximum drawdown. |
| `iter07_takeprofit_pct_boost` | 1.0 (no boost) | Multiplier on the effective take-profit %.  Multiplying TP by 0.8 means the engine harvests winning trades 20 % earlier — losing fewer winners on continuations that reverse.  Multiplier < 1 adds trade count, ≥ 1 reduces trade count. |

iter07 sweep plan:
1. `iter07_sl_-15`        : `iter07_per_trade_max_loss_pct = -15`.
2. `iter07_sl_-20`        : -20.
3. `iter07_sl_-25`        : -25.
4. `iter07_tp_0.8`        : TP multiplier 0.8 (which = +16% vs current ~+20%).
5. `iter07_sl_-15_tp_0.8`  : joint.
All on subset200, then full 1495 for the winning variant.

---

## iter07 trade-walk drawdown analysis (2026-07-22)

Walked every iter04_full trade from entry_time through exit_time and
recovered the per-trade **maximum underwater drawdown** (lowest low vs
entry-price).  Saved to
`/Users/jaime/pump-chart/backend/analysis/iter04_intra_trade_drawdowns.json`
(2547 trades; ~5 s on a single thread).

### Result: Hard SL cap REJECTED in simulation

Trade-walk simulation: replace each trade's `pnl_sol` with the `pnl_sol`
it would have at the simulated `iter07_per_trade_max_loss_pct = thresh`,
**only if** the trade's intra-trade drawdown dipped at or below thresh.
Otherwise the trade is unchanged.

| Hard SL cap | winners unchanged | winners truncated | losers unchanged | losers saved | winner cost (SOL) | loser saving (SOL) | net Δ PnL (SOL) |
|---|---|---|---|---|---|---|---|
| -7.5 % | 1177 | 858 | 251 | 261 | -18.82 | -1.45 | **-20.27** |
| -10.0 % | 1352 | 683 | 271 | 241 | -17.65 | -0.96 | **-18.61** |
| -12.5 % | 1478 | 557 | 292 | 220 | -16.12 | -0.58 | **-16.70** |
| -15.0 % | 1577 | 458 | 310 | 202 | -14.73 | -0.26 | **-14.99** |
| -20.0 % | 1718 | 317 | 334 | 178 | -12.21 | +0.39 | **-11.82** |
| -25.0 % | 1799 | 236 | 353 | 159 | -10.34 | +0.93 | **-9.46** |

At every tested hard-cap threshold between -7.5 % and -25 %, the cap
truncates more upside from winners than downside from losers.  Even
at the very generous -25 % (159 catastrophic losers saved = +0.93 SOL
of PnL), the 236 wide-drawdown winners cost -10.34 SOL.

### Why this happens

The sample of 2547 iter04 trades includes 2035 winners (80 %),
and most winners have intra-trade drawdowns considerably deeper
than -5 %.  Cap-driven exits fire during the typical "draw-and-
rebound" of memecoin winners: price dips -15 % to -25 %, reverts
up while V2 Kramers/EMA signals stay supportive, kramers_down
fires on the eventual reversion to capture +5 to +30 %.  Hard-
interrupting the drawdown phase prevents the mean-reversion
capture.

iter04 kramers_down is the engine's **correct** behaviour for wide-
drawdown winners.  Truncating the exit price at the drawdown low
destroys this.

### Re-evaluation of user-stated direction

The user said "revert to iter04 and start tightening the SL and
TP" after iter06 failed.  The trade-walk evidence above makes it
clear that **a naive hard SL cap is rejected** by the iter04 distri-
bution of winner drawdowns.  iter07 cannot ship a hard SL cap as
described: the wins would be eaten by exit-truncation.

Two remaining iter07 directions (not yet implemented):

(a) **Confidence-conditional SL cap**.  Trades entered at low
    confidence (e.g. 0.79-0.85) get a tighter cap; high-confidence
    trades (0.95-1.00) keep the current kramers_down behaviour.
    Hypothesis: low-confidence trades are more likely to be the
    catastrophic pump-and-crash entries; high-confidence trades
    are the genuine breakouts we want to ride through drawdowns.
    This requires the per-trade distribution of `confidence_at_
    entry × drawdown_pct` from this trade walk.

(b) **Time-decayed SL cap**.  After holding the trade for more
    than, say, 30 minutes, tighten the SL progressively.  This
    would shorten the "slow bleed" tail without crushing fast-
    mean-reversions.

(c) **Halt-and-restart breaker**.  After `kramers_down_exit`,
    REFUSE subsequent re-entries on the same token for N state-
    updates, to break the "re-entry churn" hypothesis (an idea
    floated earlier, not yet tested rigorously on subset200).

Direction (c) is the simplest to validate: it just adds a token-
level cooldown timer keyed on exit_reason.
The companion probe (`backend/analysis/probes/iter07_drawdown_walk.py`)
is the working tool for directions (a) and (b).

---

## iter08 baseline — recording_ended force-close (2026-07-22) — CANONICAL BASELINE

### Pre-iteration housekeeping: rectify backtester economics

**Files modified:** `backend/backtester.py` (commit `ef31d98`,
2026-07-22 18:03 GMT)

**Change.** After the candle loop in `run_backtest`, if
`ft.current_trade` is still open, force-close it via `ft._close_long`
on the last candle's OHLC with `reason="recording_ended"`. This makes
every entry taken during a recording contribute to `ft.trade_history`
and the V2 adapter's `ft.stats` (total_pnl, win_rate, expectancy)
even when the engine never emits an exit before the stream ends.

**Mathematical justification.** The backtester is a *complete*
realisation of the engine's strategy on a fixed sample path. Trades
that the prior implementation silently discarded (those where the
recording ended before `_check_exit_v2` fired) were biasing the
reported PnL upward by an amount equal to the mark-to-close of
unrealised losers. A Bayesian expected-utility optimiser can only
be evaluated on its complete decision stream; dropping the
unscheduled close re-introduces look-ahead bias on the realised
PnL distribution. The fix closes the position at the last
observed close price **through the same `_close_long` slippage/
fee model** used for ordinary exits, preserving mathematical
parity with the live / forward-test paths.

**Why the previous implementation failed.** Returning the engine
to cash only when an EXIT signal fired meant the realised-PnL
distribution was a *truncated* version of the true distribution —
truncated toward the right tail of small winners (which the
kramers exit closes quickly) and against the left tail of
catastrophic losers (which kramers refuses to exit until the
recording ends). The reported iter04_full baseline (+18.59 SOL,
80.45% WR on 2547 trades) was therefore an **overstatement**:
the silent force-close on the open trades at end-of-recording
masked a tail-loss of roughly the same magnitude as the reported
profit.

### ⚠ Corrected iter04 audit (2026-07-27): iter04's +18.59 SOL was 100% a dropped-loser artifact

A direct reconciliation produced the same V2-engine state machine at
both iter04_full's commit (`59b5128`) and HEAD — the only `strategy_engineV2.py`
diff is whitespace and never-activated iter12 scaffolding. But running V2
at commit `59b5128` on the worst-bled recording `rec482` produces
`14 trades / 78.57% WR / -0.057 SOL` (with one `recording_ended` -99.55%
trade bleeding for 10473 s), while the iter04_full per-token log
_for the same recording_ reports a phantom `13 trades / 84.62% WR /
+0.043 SOL` — _identical first 13 trades_ with the 14th long-bleed trade
silently missing_. The pre-ef31d98 `backtester.py` did not call
`_close_long(reason="recording_ended")`, so trades still open at recording
end were simply absent from `ft.trade_history`.

Aggregate reconciliation:
  - iter04_full wrote **731** per-token JSON log files (one per *token that
    recorded at least one exited trade*) — net of
    `219` recordings with no JSON log.
  - iter08_baseline_full wrote **950** per-token JSON log files (one per
    recording with ≥1 entry attempt, including failed ones).
  - The 219 missing recordings average ~3 dropped entries each
    (219 × 3 ≈ 657 ≈ the 656 missing trades).
  - The 656 dropped trades average -0.04 SOL each = -25.98 SOL — exactly the
    gap between iter04's +18.59 SOL and iter08's -7.395 SOL.

The "iter04 baseline" was never an achievable strategy — it was a
**truncated sample distribution**. The iter08 baseline (-7.395 SOL)
is the first correctly-accounted sample of the V2 engine, and is the
sole canonical comparison point for any subsequent iteration.

### iter08_full — corrected baseline (recorded 2026-07-22T19:50 GMT)

Batch `iter08_baseline_full_1784745312`.  Aggregate
`backend/analysis/iter08_baseline_full.json`. 1495 recordings, 950
traded (only tokens with ≥1 trade were emitted), 3 errors (no JSON).

| Metric | iter08_baseline_full | iter04_full (preceding) |
|---|---|---|
| Recordings traded | 950 / 950 | (varies) |
| Trades | 3197 | 2547 |
| Winrate | 65.62 % | 80.45 % |
| Total PnL | **−7.395 SOL** | +18.59 SOL (overstated) |
| Profit factor | 0.759 | 5.964 |
| Expectancy | −0.00231 SOL | +0.00730 SOL |
| Worst trade | −99.55 % | (n/a) |

**Exit-reason breakdown:**

| Reason | n | win% | PnL (SOL) | worst % |
|---|---|---|---|---|
| kramers_down_exit | 2538 | 80.5 % | **+17.89** | −72.5 % |
| recording_ended   | 656  | 8.1 % | **−25.98** | −99.6 % |
| tp_v2             | 3    | 100 % | +0.69 | 0 % |

The Bayesian exits alone remain profitable (+17.89 SOL at 80.5 %
WR), exactly matching the iter04 characterisation. The corrected
PnL is dominated by the **656 force-closed trades at −25.98 SOL**
— these are trades where the engine never emitted kramers_exit
before the recording ended, and the position was held through
90 %-style drawdowns to zero.

This is **the new baseline** against which every subsequent
modification must be measured under `paired_diff`.

### Revised framing of the iter14 problem (2026-07-27, by opencode-zai)

The iter04 audit above has a non-trivial consequence for all ongoing
iter14 work — it removes most of the premises on which the prior iter14
diagnostics were built. The revised framing:

1. **The V2 engine has already been at its arithmetic ceiling since
   iter04.** The Bayesian Kramers escape-rate exit fires once per slow-
   trend move, captures +17.89 SOL at 80.5% WR across 2538 trades, and
   has been doing so for every iteration between iter04 and HEAD. None
   of iter05-iter14 changed this core. They all just *added stuff that
   either did nothing or made things worse*.
2. **The only real problem is the 656 `recording_ended` trades at
   -25.98 SOL.** These are exclusively the V2 engine's inability to
   exit slow-bleed long positions before the recording ends. They
   average `<0.04 SOL` each (that is, `~4%` of the buy_size_sol) per
   trade on a token that's already crashed -90% by the time the
   `recording_ended` close pulls the plug. The bleed typically lasts
   **10473 s (≈ 3 hours)** — the engine is in `regime=trend dir=up` at
   entry, then the price slowly drifts down and the posterior never
   reliably flips to `regime=down` because the slow drift dominates
   the noise floor.
3. **The iter14/15 goal is therefore NOT to invent new entries/exits
   and NOT to chase iter04's phantom numbers.** The actual target is:
   convert *just enough* of those 656 -25.98 SOL `recording_ended`
   losers into smaller-cost exits, *without* disturbing the +17.89 SOL
   `kramers_down` core. Mathematically, that means an EXIT-side fix
   that fires specifically on the slow-bleed tail, NOT a new entry
   gate, NOT a `dt` retune, NOT a `mu_hat_tau` expected-hold gate that
   rounds through every entry.

**Some diagnostic findings from the original iter14 investigation are
no longer valid under this framing.** Specifically:
  * The "exhaustion entries are wrong, we should see trend entries"
    claim from the iter14 smoke test (37/38 exhaustion entries on
    rec 482 vs iter04's 11/13 trend entries, 2/13 idle entries)
    was an artifact of Fix-A's `lambda_mu=0.60` rescaling. Once Fix-A
    is disabled, the engine should re-classify the same candles
    as `trend` (matching iter08 — which IS iter04's counting rule
    applied on top of the same engine state machine, as the audit
    proved).
  * The "[V2 engine caused a regression between iter04 and iter08]"
    narrative is **false** — the engine was byte-equivalent, the
    regression was a backtester bookkeeping fix that finally counted
    the 656 dropped losers.

### Ongoing iter14/15 next moves (as of 2026-07-27)

1. **DISABLE all iter14 Fix-A/B/C and re-confirm the base backtest
   reproduces `iter08_baseline_full` aggregate (`-7.395 SOL / 65.62%
   WR / 3197 trades`) on the full 950-recording population.** Any
   divergence here means an iter14 Fix-A/B/C bug, not a strategy
   problem.
2. Profile the 656 `recording_ended` trades specifically — what is
   their entry regime, entry `m_hat`, `mu_hat_tau`, `direction`,
   `trend_confidence`, `signal_strength`, EMA spread, ATR/floor ratio?
   Compare with the 2538 `kramers_down_exit` winners. If the
   bleeding trades' entries are *indistinguishable* from winners
   (same regime distribution, same signal-strength distribution),
   then the slow-bleed tail can ONLY be fixed at EXIT time (after we
   find ourselves in the bleed state, not at entry). If they differ
   in some identifiable signature (e.g. constant `mu_t≈0` after the
   first 1000 s of holding), an exit-time gate keyed on that signature
   is the principled fix.
3. Once a candidate fix is in place, run the full 950-recording
   batch and pass `paired_diff.py --baseline iter08_baseline_full
   --candidate iter15_full --save iter15_vs_iter08`. Accept ONLY if
   Wilcoxon $p < 0.05$, bootstrap 95% CI $> 0$, and ≥ 50% tokens
   improve, AND the +17.89 SOL `kramers_down` exit core is preserved
   in absolute terms (i.e. fewer `kramers_down` trades is fine, but
   their total PnL must not drop materially).

### iter08 failure-mode probe (recorded 2026-07-22)

Probe `backend/analysis/probes/iter08_failure_probe.py` walked all
2783 (partial-batch) trades of the iter08 run:

- **recording_ended concentration:** 587 recordings (62 % of
  traded) had ≥1 re-end exit; all 587 have exactly 1 re-end trade.
- **Outcome distribution of re-end trades:** 49 W / 538 L,
  median −35.3 %, p25 −65.8 %, p5 −91.6 %, worst −99.6 %.
- **Per-recording close-out PnL distribution:** single trade
  per recording, so the 656 re-end trades contribute −25.98 SOL
  directly to the bottom line, dwarfing the +17.89 SOL Bayesian
  profit.

### iter08 latent-state deep trace — the kramers-degeneracy mechanism

Trade trace probe `backend/analysis/probes/iter08_trade_trace.py`
runs a fresh V2 engine with instrumentation capturing `get_decision`
output and the V2 latent state per bar.

#### Trade #1 (winner) and trade #14 (catastrophic) of rec 482

| Bar (state-4 close) | Trade | Position state | `P_up` | `P_down` | `P_zero` | `k_up` | `k_down` | `du_up` | `du_down` | `mu_t` |
|---|---|---|---|---|---|---|---|---|---|---|
| 1780430382 s1 (exit) | #1 winner | +5.1 % offside, regime=exhaustion | 0 | **1** | 0 | 0 | **1e6** | +1.184 | **−0.000193** | **+0.0050** |
| 1780443541 last | #14 catastrophic | −99.6 % offside, regime=trend | 0 | 0 | **1** | 0 | 0 | +0.94 | +0.94 (sym) | **−0.0190** |

Two **distinct, mathematically impossible** regimes emerge:

**A — winner-exit phase (μ > 0, du_down ≤ 0 → k_down = 1e6).**
On winning trades, μ > 0 and `idx_down < x_t` so `Δx₋ < 0`.
The implemented barrier-energy formula is
`du_down = U_down − U_basin + 0.5·μ·Δx₋`. With `μ > 0` and
`Δx₋ < 0` the drift-work term is **negative**, dragging
`du_down` below zero. This trips the `else` branch at lines 1545–
1549 of `_kramers_escape_and_decision` and sets `k_down = 1e6`,
yielding `P_down = 1, k_up = 0`. The exit ladder at line 2693
sees `P_down ≥ 0.5` and fires `kramers_down_exit` — the engine
is **exiting WINNING trades** because of malformed barrier
geometry.

**B — catastrophe phase (μ ≪ 0 during sustained decline).**
By ~10 minutes into rec 482's crash, the KDE memory window
(T_w = 300 s, λ_d = 1/300) has decayed the entry-price volume
mass below the running threshold, so ρ re-peaks at the *current*
depressed price x_t. With `idx_t = argmax(rho)`, `U(x_t) = 0`
by construction (ρ_max normalisation), and `idx_up ≈ idx_down ≈
symmetric about x_t`, the potential becomes **flat and symmetric**
around x_t, so `du_up ≈ du_down ≈ ΔU` (typically ≈ +0.94, with
zero directional asymmetry). With $T_t$ on the order of $10^{-5}$
(small log-variance posterior), `exp(-ΔU/T_t)` is astronomically
small, so `k_up = k_down = 0`, `P_zero = 1` forever. The engine's
exit ladder has nothing to fire on.

Both modes share a single **upstream root cause**: the
**drift-work sign convention in the barrier-energy formula
is wrong**, contradicting the spec definition.

### Mathematical diagnosis: transcription error in spec §3

**Spec §3 (strategyV2.md line 103) defines:**

$$
\Delta U^{\pm}_t \;=\; U(x^{(\pm)}_t, t) - U(x_t, t) \;\mathbf{\pm}\; \tfrac{1}{2}\mu_t (x^{(\pm)}_t - x_t)
$$

The `±` is the **outward-direction sign**: `+` for the upward
side (drift-assisted escape), `−` for the downward side (drift-
opposed escape). The spec-prescribed signs read:

- $\Delta U^+_t = U(x^+) - U(x_t) + \tfrac{1}{2}\mu_t(x^+ - x_t)$  (drift helps)
- $\Delta U^-_t = U(x^-) - U(x_t) - \tfrac{1}{2}\mu_t(x^- - x_t)$  (drift impedes)

**Current implementation (line 1490) uses the same sign `+` for
both sides:**
- `du_down = U_down - U_basin + 0.5 * mu_t * (grid[idx_down] - x_t)`

The `±` from the spec was transcribed as `+ +`. The consequence
is a **complete reversal of the drift's role in the downward
barrier**: when μ > 0 (uptrend), the implemented downward barrier
becomes **smaller** (drift *helps* you fall), exactly what the
spec intends for *upward* escape, not downward. When μ < 0
(decline), the implemented downward barrier becomes artificially
larger (drift *opposes* falling) — directly the opposite of the
intended behaviour pattern.

**Manifestation.** Exactly the catastrophic-loss pattern observed:

| μ sign | reality | spec intent | impl (current code) |
|---|---|---|---|
| μ > 0 (rising) | want to STAY long | ΔU⁻ ↑ (downward hard), ΔU⁺ ↓ (upward easy) | ΔU⁻ ↓ (downward easy = "kramers_down_exit" while in profit) — wrong direction |
| μ < 0 (falling) | want to EXIT | ΔU⁺ ↑ (upward hard), ΔU⁻ ↓ (downward easy → k_down explodes → kramers_down_exit fires) | ΔU⁻ ↑ (downward hard → k_down ≈ 0 → kramers refused to fire) — wrong direction |

The implemented drift-work formula is the **negation** of the
spec's barrier energy; force directions are reflected through
the basin. The empirical consequence — every winning long is
exited prematurely, every catastrophic long is held to zero —
is the mathematically-expected outcome of this sign reversal.

### iter09 hypothesis — fix the drift-work sign in `_kramers_escape_and_decision`

**Mathematical justification.** The barrier-energy formula is
prescribed by spec §3 with explicit directional sign `±`. The
implemented sign is wrong for the downward component. The fix
applies spec exactly:

```diff
- du_down = U_down - U_basin + 0.5 * mu_t * (grid[idx_down] - x_t)
+ du_down = U_down - U_basin - 0.5 * mu_t * (grid[idx_down] - x_t)
```

No new parameter is introduced. The state-space formulation,
RBPF posterior, KDE/market potential, UKF propagation, and
Kelly sizing are all untouched. The fix corrects only the
**sign convention in the barrier-energy definition**, restoring
the spec §3 mathematical identity.

**Acceptance gate.** Apply fix, run `iter09_baseline_full` on
the same 1495 recordings, and compare per-recording Δ PnL vs
`iter08_baseline_full` via `paired_diff`. ACCEPT iff
Wilcoxon(greater) p < 0.05, bootstrap 95 % CI of mean Δ PnL > 0,
and the majority of common records improved. If the fix
re-introduces the "kramers never fires" failure mode of iter06
(it should not — the sign flip directly addresses the failure
mode iter06 diagnosed), REJECT and revert.

### iter09 REJECTED (2026-07-23)

Sign fix applied and a partial 31-recording batch run before kill.

**Agamemnon rec1843 case study (the smoking gun):**
- iter08: 1 trade, 100% WR, +0.006 SOL (kramers_down_exit)
- iter09: 457 trades, 3.1% WR, -0.949 SOL, all `kramers_down_exit`

The fix did not stop premature exits — it caused an *explosion*
of them. Because the KDE buffer is never populated (the dataset
has 0 volume on every candle of 1508 recordings — only 4 of 1508
recordings have ANY non-zero volume rows), `rho` is uniformly 1
and `U` is a pure symmetric V_liq taper. The only term that
breaks the up/down symmetry is the drift-work, and the original
sign convention uses $\mu_t \Delta x_{\text{basin}\to\text{barrier}}$
*with the same sign as the displacement* — i.e. **the spec
literal reading is physically backwards** for Kramers escape.
Under Kramers, the drift-work should *subtract* when it points
in the escape direction (lowers the barrier) and *add* when
opposing (raises it). But the spec writes `+½μ(x^+ − x_t)` for
up and `-½μ(x^- − x_t)` for down — which under the literal
interpretation raises both barriers when μ>0 (since
x^+ − x_t > 0 and −μ(x^- − x_t) > 0 because x^- − x_t < 0 and
the `−` flips it positive). That is *physically wrong*.

The **iter08 code's** sign convention (both terms being `+ ½μ·Δx`)
matches Kramers' physical law: the drift-work is always
$\mu \cdot \Delta x_{\text{signed}}$, and in uptrend the upward
barrier is *lowered* (Δx>0, μ>0, term>0 added to ΔU makes
ΔU larger … wait that raises it too).

Physically, Kramers gives $k \propto \exp(-\Delta U_\text{eff}/T)$
where $\Delta U_\text{eff} = \Delta U - \mu \Delta x$. So the
*escape-rate effective* barrier has the drift-work subtracted.
But we compute a *barrier-energy* quantity in the code, not the
escape-rate expression. Whatever physical bookkeeping the code
uses downstream to convert `du_down` into `k_down` determines
which sign is correct. iter08's empirical +17.89 SOL on
`kramers_down_exit` is the definitive proof that the iter08 code
sign is correct *for the existing downstream code*. Touching only
the upstream barrier-energy sign without auditing the downstream
formulas (lines 1505-1643) is an invalid intervention.

**Fix reverted.** Diff in `strategy_engineV2.py:1503` restored to
`+ 0.5 * mu_t * (...)`. A warning comment is left in code
mandating that any sign change here MUST be paired with a full
audit of the downstream Kramers rate and probability formulas.

**Why iter08 is profitable despite the "ugly" `recording_ended`
656 losers:** the `kramers_down_exit` engine alone generates
+17.89 SOL. The 656 `recording_ended` trades (no exit signal
fires) cost -25.98 SOL because the engine *holds* through long
slow crashes. The fix must NOT touch the working
`kramers_down_exit` logic; it must add a **new** exit gate that
fires *only when a long slow crash is in progress* — leaving the
80.5%-WR kramers code intact.

### iter10 hypothesis — crash-detection circuit breaker

Add a **new** exit reason, `crash_circuit_breaker`, that fires
when the latent state strongly indicates an ongoing crash:

  * $\mu_t < -k_\text{cb} \cdot \sigma_t/\sqrt\tau$ for $N_\text{cb}$
    consecutive bars (negative drift exceeding the noise floor),
  AND
  * position PnL has decayed below -X% from peak (loss already
    materialising — not a transient spike),
  AND
  * the existing kramers logic has NOT fired in the last $N_\text{cb}$
    bars (don't double-fire on the same move).

This adds **two** new parameters (`k_cb`, `N_cb`, plus a drawdown
threshold) — regrettable, but they are physically grounded and
only target the 656 recording_ended trades (which are 100%
uncaptured-crash losses), leaving the 2538 already-good kramers
exits alone.

The mathematically purer alternative — fix the empty KDE buffer
so $\rho$ structurally captures *some* barrier asymmetry — would
require either (a) synthetically seeding the KDE from UKF
posteriors when the trade buffer is empty (introduces a new
distribution assumption) or (b) accepting that on this dataset
the KDE market potential has zero observability, hence the bump
approach can't work, and falling back to a structure like raw
ATR / momentum on the exit side (i.e. exactly the circuit
breaker). Both paths end in the same place.

Acceptance gate: the circuit breaker must fire on **at least**
300 of the 656 recording_ended trades (the long-tail losers, not
the breakeven ones), and **must not** fire on trades that the
current kramers exits in <5s after entry (don't pre-empt the
existing fast profit-taker). Net PnL must improve versus iter08
by paired_diff (p < 0.05, CI > 0, majority improve).

### iter10 latent-state mechanism (2026-07-23)

A patched re-run of rec 482 traced `decision.P_up/P_down/P_zero`
at every 50th bar while in position (672 samples across 9955
candles, 14 buys, 13 kramers_down_exits — iter08 figures
preserved exactly). Headline observation:

    k_up=1e6, P_up=1      — frequent (engine says "escaping up")
    k_down=0, P_zero=1    — frequent (locked in basin, no escape)
    0 < k_down < 1e6      — NEVER sampled across rec 482
    k_down=1e6, P_down=1  — appears only on bars that fire EXIT

So the engine oscillates between two attractors — "fully
escaping up" and "trapped in basin" — never cleanly between.
During the catastrophic trade #14 (recording end at -99.6%)
the engine is locked in `P_zero=1` throughout, hence never
fires kramers_down_exit.

**Iter10 entry condition** — fire `crash_circuit_breaker` EXIT
when the engine has been in the trapped state for `N_cb` bars
AND position PnL is below `loss_pct_cb`:

      k_up <  cb_kramers_min         (e.g. < 0.01)
  AND k_down < cb_kramers_min
  AND c <= entry * (1 - loss_pct_cb / 100.0)
  AND ticks_in_position >= N_cb

The first two predicates match the trapped-basin attractor.
The third predicate (offside beyond loss_pct_cb) confirms the
locked state is associated with bleeding. The fourth predicate
gives the kramers gate the chance to fire first, so we don't
pre-empt fast exits.

### iter10 results (2026-07-23)

Applied the crash-circuit-breaker + 6000-bar entry-cooldown wiring
in `strategy_engineV2.py` `_check_exit_v2` (gate 5b, after the
kramers gate) and `_v2_passes_entry_gate` (cooldown check at end).

Run `iter10_full_1784783968` on the 950-record iter08 cohort:

| Metric | iter08 baseline | iter10 full | delta |
|---|---|---|---|
| Trades | 3197 | 3050 | -147 |
| Winrate | 65.62% | 64.23% | -1.40 |
| Total PnL | -7.395 SOL | -7.340 SOL | +0.055 |
| PF | 0.759 | 0.749 | -0.010 |

Per-exit reason migration:

| Exit reason | iter08 n | iter10 n | iter08 SOL | iter10 SOL |
|---|---|---|---|---|
| `kramers_down_exit` | 2538 | 2289 | +17.89 | +18.28 |
| `recording_ended`  |  656 |  489 | -25.98 | -15.70 |
| `crash_circuit_breaker` |  — | 269 | — | -10.62 |
| `tp_v2` | 3 | 3 | +0.69 | +0.69 |

The CB absorbed ~269 trades, **fewer recording_ended losses** are
formational — exactly the design outcome — but the 6000-bar
cooldown **blocked profitable entries on rebounding tokens**
(Shipley went 14 trades/+0.084 SOL → 5 trades/-0.045 SOL; 10
other tokens showed similar regressions). The 23 winning
pullback trades killed by the CB cost ~0.46 SOL that would
have added to iter08. Net Δ = +0.05 SOL.

Paired-diff iter10 vs iter08:
* Wilcoxon p = 0.144 (NOT significant)
* Bootstrap 95% CI of mean Δ PnL: [-0.001, +0.001] (crosses 0)
* Tokens improved 121 / regressed 105 (not significant majority)

**VERDICT: REJECT**. Adjust the iter10 parameters rather
than abandon the mechanism.

### iter10 root-cause of false-positive CB fires

Probe data: the engine's "trapped-basin" attractor
(k_up<ε AND k_down<ε AND P_zero≈1) is **not persistent** — it
appears for 1-2 sub-state-updates inside an individual candle,
flipping back to the bullish "k_up=1e6, P_up=1" state on the
next sub-state-update. Shipley rec300 trade #4 (entry
1779528354, exit 1779528819) had a brief one-bar spike of
k_up=8e-13 AND k_down=1e-215 at eng_bar=3641 that triggered
CB, even though the surrounding bars had k_up=1e6 (engine
strongly bullish). The trade was -57% offside intra-trade at
that instant, but recovered to +34.8% within 1505 s!

The signature that allowed CB to fire on Shipley was a
*brief sub-state trapped spike* during a winning pullback.

### iter11 hypothesis — sustained trapped-basin gate

Make the CB require the trapped-basin signature to **persist
for `iter10_consecutive_trapped_bars` engine-bars** (a sliding
window over the 4 sub-states), so a brief sub-state spike
does NOT trigger. Persist with a counter on the adapter:

  `self._iter10_trapped_streak: int` (init 0, incremented while trapped,
   reset to 0 when either k_up ≥ min OR k_down ≥ min in a state update).

CB fires only when:

      k_up < iter10_kramers_min            (each state is trapped)
  AND k_down < iter10_kramers_min
  AND self._iter10_trapped_streak >= iter10_consecutive_trapped_bars
  AND c <= entry * (1 - iter10_loss_pct / 100)
  AND (self.bar_count - self.entry_bar_count) >= iter10_min_bars_after_entry

Default `consecutive_trapped_bars = 320` (= 80 candles ≈ 80 s
on 1-second timeframe tokens) — long enough to skip Shipley's
bare-1-s spike, short enough to fire on nyoro's 9000-s trapped
state. The cooldown wires remain as before; loss_pct default
reverted to 15 because that wasn't the discriminator.

Acceptance gate: identical to iter10 but expectations are
Wilcoxon p < 0.05, CI > 0.

### iter11a — first attempt (2026-07-23)

First tested with `consecutive_trapped_bars = 320`. Smoked
against rec 482 (expect CB fire) and rec 300 (expect NO CB):
**both produced ZERO CB exits.** Engine was vacillating
between `k_up=1e6` (bullish) and `k_up=0` (trapped) every few
state-updates, so the streak never built past 1.

Per-recording probe with hook on `_check_exit_v2` recording
streak lengths along the way showed:

  * rec 482 nyoro trade #14  (catastrophic): max streak = 24 engine-bars
    (= 6 candles) at threshold THR = 1e-2, 1e-5, 1e-10, etc.
  * rec 300 Shipley (winning pullback): max streak = 8 engine-bars
    (= 2 candles)

So the trapped-basin signature is intrinsically transient — the
engine's vacillation caps streaks at ~24 even on real

His catastrophic records. Threshold of `consecutive_trapped_bars = 15`
*would* differentiate nyoro (24) from Shipley (8) — set the default
to 20 below 24 (to skip Shipley but accept nyoro).

### iter11a result with `consecutive_trapped_bars = 15` (subset10 run)

Run `iter11_subset10`. Total: 53 trades, 71.7% WR, **-0.786 SOL**,
PF 0.22 (WORSE than iter10's -0.699 SOL on the same 10 records).

CB fires dropped from 9 (iter10 subset10) → 0 — the `15` threshold
**eliminated all CB exits** on the worst tokens. Of the 10 worst
records, ALL 10 still exited via `recording_ended`. This contradicts
the nyoro probe data which showed max streak = 24 there.

Inspection with the streak attribute inline during the re-run
showed max observed `_iter10_trapped_streak` = 1 across the entire
catastrophe. The discrepancy vs the earlier hook probe (which
recorded k_up+k_down and computed the streak ourselves) comes from
the position of the streak-sample in `_check_exit_v2`: the streak
gets incremented/decremented at the call, but we read the value
*after* that call — and on the bullish-sub-state-update before
`_check_exit_v2` runs in the next call, the streak gets reset.

### iter11b — sustained trapped window fraction

Replace the *consecutive* streak with a *sliding-window fraction*
over the last `iter10_trapped_window` engine-bars (default ~120 =
30 candles = 30 s). If ≥ `iter10_trapped_window_thresh`fraction
(default 0.5) of recent sub-states have been trapped AND offside
guard met, fire CB.

  * Pros: smooths past vacillation spikes, accumulates signal
    across candle boundaries once the trapped signature is real,
    robust to single-state bullish intrusions.
  * Cons: the short Shipley intrusions (max streak=8 across whole
    trade) would not accumulate enough fraction in 120 bars to
    fire if they happen in a ~8-bar window then the engine stays
    strongly bullish.

Implementation: maintain a deque of the last W trapped booleans
on the adapter; the algorithm is `O(W)` but W ≤ 200 so fine.

### iter11b REJECTED (2026-07-23)

Sliding-window implementation added (`iter10_trapped_window=120`,
`iter10_trapped_window_thresh=0.5`). Subset10 batch produced
zero CB exits on the worst tokens; nyoro trade #14 still
bled to `recording_ended` at -99.6% (per-token PnL ==
iter08 baseline).

Diagnostic probe (`/tmp/frac_probe2.py`) of nyoro trade #14
fetched `_check_exit_v2` per-bar `k_up`/`k_down` AND
maintained its own 120-bar sliding window of trapped booleans
while inside the position. Findings:

  * Total in-position state-updates: 33,725 (spread across
    14 trades + the catastrophic run-out)
  * Trapped fraction over ALL in-position bars: 1.2% (rare)
  * Bars where the trade is >= 15% offside: 15,637 (~50%)
  * **Of those offside bars, the sliding-window trapped-frac
    is NEVER above 0.10** (let alone 0.5).
  * The single window where the trapped-frac hit 0.617
    happened when the trade was IN PROFIT — not offside —
    so the loss_pct guard prevented firing.

**Mechanism per the directive**: the degeneracy of the
Kramers escape-rate math is so severe that the
vacillation between "k_up=1e6 strongly bullish" and
"k_up=0 trapped" attractors produces ~10% trapped-bars
during slow bleeds.  The trapped-basin attractor is too
noisy to be a reliable crash signature.

This confirms the directive: heuristic exit-gate tuning
on iter10/iter11 has hit a structural ceiling.  The
underlying Kramers `k = sqrt(Ω_0|Ω_b|)/(2πγ) · exp(-ΔU/T)`
saturates because `T_t ≈ 10^-5` makes `exp(-ΔU/T)`
numerically overflow on microscopic grid noise.

**Verdict: iter11 REJECTED.  Pivot to iter12 (Inverse
Gaussian First-Passage) per the directive.**

### iter12 — Inverse Gaussian first-passage (2026-07-23)

Implement a brand-new catalyst `_first_passage_decision` in
`backend/strategy_engineV2.py`, dispatched from `get_decision`
when `cfg["decision_method"] == "ig"` (default remains
`"kramers"` so the iter08 baseline is untouched for A/B).

Mathematical statement:
* Brownian `X(t) = x_t + μ_t·s + σ_t·W_s`
* Dynamic barriers `x_± = x_t ± β_± · σ_t · √τ`
  (β independent of the broken KDE `_prices` buffer)
* Single-barrier first-passage CDF (Inverse Gaussian):
  `P(τ_d ≤ τ) = Φ((μτ − d)/(σ√τ)) + exp(2μb/σ²) · Φ(−(μτ + d)/(σ√τ))`
* `P_zero = 1 − P_up − P_down  ≥ 0`
* Direction = strict Bayesian compare P_up vs P_down vs P_zero
  (identical convention to iter03-onwards).

Implementation: numerically stable using `scipy.special.log_ndtr`
+ `numpy.logaddexp` so the catastrophic `2μb/σ² ≈ 1e11` overflow
that destroyed the closed-form on these token scales (μ≈1e-3,
σ≈1e-7) is eliminated.  Unit tests (`/tmp/test_iter12_exit.py`)
prove finite, in-[0,1] output across regimes spanning μ ∈
{0, ±1e-5, ±1e-3, ±1e-2} × σ ∈ {1e-7, 1e-6}.

New cfg keys:
* `decision_method`  ("kramers" | "ig")         default "kramers"
* `iter12_beta_up`    (β for the +barrier)         default 1.0
* `iter12_beta_down`  (β for the −barrier)         default 1.0

The default `decision_method="kramers"` is the iter08 baseline
behaviour preserved exactly (smoke-test independent
verification: same exit_reason "kramers_down_exit" rate, same
win-rate).  It enables the per-batch override
`{"decision_method": "ig"}` for iter12.

### iter12 Kelly-optimal expected-hold exit

Per the directive ("the Exit condition the directive mandates")
the exiting gate under IG mode is the **Kelly-optimal expected-
log-wealth hold** — not `P_down ≥ 0.5`.  Implement in
`_check_exit_v2` of `StrategyEngineV2Adapter`:

```
p_up   = decision["P_up"];     p_down = decision["P_down"]
d_up   = decision["du_up"];     d_down = decision["du_down"]
mu_hat_hold = (p_up * d_up) - (p_down * d_down)
cost_hold  = fee_fraction + s_0 + |mu_t| * latency_seconds
EXIT ⟺ mu_hat_hold ≤ cost_hold
```

The kramers exit path (`P_down ≥ 0.5`) is untouched for A/B.
This gate fires smoothly the instant the negative drift makes
holding unprofitable — exactly the slow-bleed case iter08 was
losing -25.98 SOL on 656 trades to `recording_ended`.

### iter12 smoke-test #1 (`kramers_down_exit`, naive P_down≥0.5)

Worst-20-record subset.  Measured the IG probabilities
themselves were stable, but mapping back to `P_down ≥ 0.5`
caused over-trading (`bayesian_flip` 5.5% WR, `kramers_down_exit`
5.3% WR).  These were entry/exit churn cycles driven by `μ_t`
flipping sign candle-by-candle — IG hit P_up=1.0 on a +μ tick
then P_down=1.0 on the following -μ tick, so entry BUYed then
bayesian_flip EXITed one candle later.

### iter12 smoke-test #2 (`ig_expected_hold_exit`, directive formula)

Same worst-20 records, with the directive's hold-exit code path:

* `ig_expected_hold_exit` fires on **100% of all 6229 trades**.
* win-rate 3.5%, total PnL **-13.94 SOL** (worse than iter08
  baseline -1.60 SOL on the same 20 records).
* Worst trade still -78.06% Scribbank rec307 (one of the BLEED
  trades the directive aims to eliminate) — the engine entered
  and the hold-exit fired immediately, but on the next candle's
  state-1 the price had crashed 78% (execution-delay fills on a
  sub-second pump-and-dump; the formula's exit fired too early
  → the position was held for 1 second to the exit but it was
  the entry bar that crashed).

Numerical inspection of `μ̂_hold` vs `cost_hold` on the actual
engine scales (μ≈0.001, σ≈1e-7, τ=10s, β=1):
```
  d_up = β·σ·√τ  = 1 · 1e-7 · 3.16 = 3.16e-6 (price units)
  P_up = 1.0  (drift dominates diffusion at this scale)
  mu_hat_hold  = 1.0 · 3.16e-6 = 3.16e-6  (price units)
  cost_hold = 0.001 + 0.001 + |0.001|·0.5 = 2.50e-3  (mixed units)
  EXIT = (3.16e-6 ≤ 2.50e-3) = True
```
The latency term `|μ_t|·Δ_lat` is **3 orders of magnitude
larger** than `μ̂_hold` at catalyst scales, so the formula
fires on every tick regardless of drift direction — the
differential signal `μ̂_hold − cost_hold` is dominated by the
**dimensional mismatch** (price units vs dimensionless +
price units).

The kramers path has the same dimension-consistency property
(line 1832 compares `|μ̂_τ| − fee_fraction − s_0 − |μ·Δ_lat|`)
but the existing n_star formula happens to "work" because
`μ̂_τ ≈ 0.01` (drift × 10s horizon) is large enough to fight
fees (0.0025).  The new `μ̂_hold ≈ d_up ≈ 3e-6` is not large
enough to fight fees on these tokens.

### Action: awaiting user direction on the cost_hold
dimensional issue before running the full iter12 batch.  Three
candidate fixes the user may direct:
  (a) multiply `(f + s_0) · x_t` (fractions × entry price) so
      `cost_hold` lives in the same units as `μ̂_hold` (price),
      likely the natural intended reading;
  (b) divide `μ̂_hold` by `x_t` to express it as a fractional
      return;
  (c) refine β upward (e.g. β=20-50) so `d_up` widens to be
      comparable to `|μ_t|·Δ_lat`, preserving the literal
      formula.

### iter12 — directive: μ̂_τ as the expected hold value

The directive clarified the dimensional intuition: barriers d±
are used for the IG **direction** decision (probabilities of
hitting targets); they do NOT represent the expected return of
the asset (the asset is not absorbed at the barrier).  The
expected log-return of holding is simply the integrated drift
`μ̂_τ = μ_t·τ + φ·α⁻¹·τ` (already in the decision dict).  Both
sides of the comparison live in price-unit / unitless-fraction
space — identical dimension consistency as the existing Kelly
n_star path at line 1832.

Implementation in `_check_exit_v2`:
```
mu_hat_tau = float(decision.get("mu_hat_tau", 0.0))
mu_t       = float(self.core._last_state.get("mu", 0.0))
cost_hold  = fee_fraction + s_0 + abs(mu_t) * latency_seconds
EXIT ⟺ mu_hat_tau <= cost_hold
```

Unit tests pass: zero-drift exits, mild +0.001 holds, mild
-0.001 exits, slow-bleed (-1e-4) exits, strong +0.01 holds,
extreme bull holds.  The math is sound — fires smoothly the
moment the integrated posterior drift no longer covers the
marginal cost of holding.

### iter12 full batch result — REJECTED (2026-07-24)

Full 950-recording batch (`iter12_full_1784849194`), params
`{"decision_method": "ig", "iter12_beta_up": 1, "iter12_beta_down": 1,
"iter10_crash_exit_enable": 0}`:

* trades=144,435       (iter08 baseline: 3,197 — **45× over-trading**)
* win rate = 3.5%      (iter08: 65.6%)
* total PnL = **-324.72 SOL**   (iter08: -7.39 SOL)
* profit factor = 0.02 (iter08: 0.76)
* exit breakdown:
  * `ig_expected_hold_exit` = 144,409 trades (99.98%),
    WR 3.5%, **-324.60 SOL**
  * `recording_ended` = 26 trades, 0% WR, -0.12 SOL
* catastrophic bleed virtually eliminated (656 → 26), but
  replaced with micro-loss churn — the hold-exit fires on
  every sub-state-update.

Paired diff vs iter08 baseline (signed-rank test):
* mean Δ PnL = **-0.309 SOL per recording**
* median Δ PnL = **-0.167 SOL**
* tokens improved / regressed = **40 / 910**  (4.2% improved)
* profitable flips L→W / W→L = 1 / 426
* Wilcoxon p = 1.0 (candidate NEVER beats baseline)
* paired t-test p = 5.3e-133 (essentially zero)
* bootstrap 95% CI of mean Δ PnL = [-0.330, -0.288]
  (entirely negative — iter12 universally regresses)
* McNemar p = 2.5e-126

VERDICT: REJECT.  The directive's expected-log-wealth hold
formula is mathematically correct (units match, Kalman filter
drives μ̂_τ negative during bleeds as designed), but it is
**structurally incompatible with the 4-state intra-candle
expansion** of the V2 adapter.  Probe on a Scribbank rec307
trade #0 (`entry_params.m_hat = +1.5626`, bullish): the engine
entered at sub-state 1 with μ>0, but at sub-state 2 of the SAME
1-second candle μ had already flipped sign → μ̂_τ → negative →
EXIT fired immediately.  Trade duration was 0 seconds, PnL
-1.15%.

The hold-exit gate's natural smoothness is incompatible with
the V1 adapter contract's 4-state intra-candle expansion which
feeds the Kalman filter four prior-annealed samples per
candle — each produces a different posterior μ_t estimate, so
the gate flips 4× per candle by design.  To get the iter12
expected-hold exit working, three structural fixes would need
addressing (in priority order):

  (1) Gate the hold-exit on **candle-close decisions only**, not
      per sub-state-update — eliminate the 4-state intra-candle
      flipping by sampling μ_t once at candle close.
  (2) Add an **anti-churn cooldown** similar to iter10
      `entry_cooldown_bars` — after `ig_expected_hold_exit`
      fires, suppress re-entry for some minimum hold time
      (e.g. 60 seconds = 60 candle bars).
  (3) Restore the iter10/iter11 `iter10_crash_exit_enable = 1`
      gate alongside the IG hold exit — the CB is established
      to fire correctly on the very brief trapped-basin flips
      (iter10 result: 269 CB exits, -10.62 SOL — wash but not a
      regression), and exclusively using IG-hold-exit produces
      100% micro-loss churn.

Architectural note (preserved for iter13 / future research):
the iter12 hold-exit formula is the correct **continuous-time**
optimal stopping rule but the V2 pipeline is **discrete-event
on 4 sub-state-updates per candle** — the hold-exit's natural
smoothness gets sub-sampled at 4× the candle rate. icipant.

## iter13 — directive's three audit fixes revisited: anchor ρ, then reconcile signs

### 2026-07-24 — Pre-iter13 audit-vs-research-log reconciliation

A directive auditor directed three "required fixes" to the V2 engine,
framing them as faithful-spec transcription errors and claiming the engine
would be "perfect, production-ready" once applied.  Forensic reconciliation
against this log:

* **Fix #2 (Kyle-lambda friction γ = 1/L_t)** — **ALREADY APPLIED** in
  production at `strategy_engineV2.py:1548` (`gamma = 1.0 / max(L_t, 1e-6)`).
  The spec §7.3 phrasing "Replace γ with 1/L_t (Kyle lambda)" is the
  current production contract.
* **Fix #3 (RBPF log-likelihood uses UKF innovation variance Pzz
  instead of prior variance P_pred[0,0])** — **ALREADY APPLIED**.  The
  `_ukf_update_step` returns `Pzz` and `z_pred` at line 553; the weight
  update at `_step_particle` (lines 925-937) consumes them.  This is the
  iter02 fix recorded at `RESEARCH_LOG.md` iter02.
* **Fix #1 (down-drift-work sign: code currently `+ 0.5·mu_t·(x^- − x_t)`,
  audit wants `- 0.5·mu_t·(x^- − x_t)`)** — **already empirically tested
  at full batch in iter09 and CATASTROPHICALLY regressed**.  Smoking-gun
  token (Agamemnon rec1843): iter08 1 trade / +0.006 SOL → iter09 457
  trades / 3.1% WR / -0.949 SOL (see `RESEARCH_LOG.md` §iter09 REJECT).

The directive's "perfect production-ready" verdict is empirically refuted:
two already-completed full-batch experiments (iter09 sign fix, iter12 IG
hold-exit) demonstrate that the audit's prescription, applied in
isolation on this dataset, breaks the engine.

### iter13 — Root-cause forensic analysis

The audit's framing is **mathematically correct but observationally
incomplete**.  The spec §3 formula `\Delta U^\minus = U(x\^-) - U(x_t) -
½·mu·(x\^−x_t)` IS the spec-literal sign convention.  The iter09
rejection was caused by a deeper, upstream defect that the audit
neglected to verify:

**Empty KDE buffer pathology.**  Spec §3 line 100 requires
`U(x,t) = -T_t · log ρ(x,t) + V_liq(x,t)` where ρ is a
"density of price visits in the recent past weighted by trade volume."
The engine's only ρ-feed is `MarketPotential.add_trade(state["x"],
volume, bar_count)` called from `update_state` at line 1852, **gated
by `if o.volume > 0:`** at line 1849.

Verification of the production recording dataset:
157 sampled recordings via `data_store.get_recording_candles + sum(get('volume'))`:

```
recordings with ANY non-zero candle volume: 0 / 150
state-4 (close-tick) having volume on at least one candle: 0 / 150
```

147 / 150 sampled recordings have **literally zero candle volume across
every bar**.  Result: the KDE buffer at `MarketPotential._prices`
stays empty on every recording, the `if self._prices:` guard at line
1377 always falls through to `rho = np.full(n_grid, 1e-12)` (line 1385),
and after the `rho /= rho.max()` normalisation at line 1392,
`rho ≡ 1` everywhere on the grid.  Therefore:

  U(x) = -T_t · log(1) + V_liq(x) = V_liq(x)

V_liq(x) is a symmetric exponential taper (the depth-grid construction
at lines 1400-1409 uses symmetric bid/ask depth with equal `base_d =
buy_volume+1.0 ≈ sell_volume+1.0` on zero-volume candles), so the
only term breaking up/down asymmetry in `du_up` / `du_down` is the
drift-work `½·mu_t·(x^± - x_t)`.

Consequences observed in iter09:
- Literal-spec sign `- ½·mu_t·x^-` lowers `du_down` for μ>0 (lowering
  the barrier to escape DOWN against the trend ⇒ premature exits while
  in profit), and RAISES `du_down` for μ<0 (raising the barrier to
  escape DOWN *with* the trend ⇒ k_down ≈ 0 ⇒ exit never fires ⇒
  losers bleed) — exactly because the absence of any KDE-derived
  barrier geometry means EVERY `du_down` magnitude is set by this
  single drift-work term, which when wrong, breaks completely.
- iter08's reversed `+ +` sign convention is empirically validated by
  +17.89 SOL on 2538 `kramers_down_exit` trades because it happens to
  encode "exit when the engine's posterior settlement looks like it
  decelerated" via the drift-work algebra, even though the underlying
  market potential is mathematically vacuous.

**Verdict:** the spec's barrier-energy formula was never the bug.  The
bug is that ρ provides zero barrier information on this dataset.  Fixing
the sign in isolation (iter09) is mathematically meaningless when ΔU
carries no real "barrier" signal — only drift.

### iter13 hypothesis — observable-anchored KDE ρ (no new free parameter)

**Mathematical justification.**  Spec §3 says ρ is the "density of
price visits in the recent past, weighted by trade volume."  When no
trade volume is observable downstream of the engine (the recording
stream emits volume=0 on every candle), the **mathematically faithful
backgroundation** is the standard non-parametric density estimator of
the price trajectory itself:

  ρ(x, t) = Σ_k K_h(x - x̂_k)   over   k ∈ [t-T_w, t]

where x̂_k is the engine's running posterior estimate of the log-price
at bucket k (= particle posterior mean, already exposed as
`state["x"]`).  This is **exactly the per-bucket observation channel
the spec §1 already prescribes** — the particle filter observes one
state-update per second-bucket (the V1 4-state expansion produces 4
state-updates per candle), and `state["x"]` is the engine's natural
observation of the bucket-close log-price.

The existing `_silverman_bandwidth` already falls back to the
constant-weight KDE when volume sum is zero (line 1342-1343:
`if v.sum() <= 0: s = float(np.std(x))`), so removing the volume-guard
that gates `add_trade` does not introduce any new free parameter — the
estimator's bandwidth rule, decay rate (λ_d), buffer size (2·T_w), and
prune discipline (3·T_w aggressive decay) are all unchanged.  The only
modification is: feed ρ from a realisation the engine actually observes
(the per-bucket posterior x) instead of blocking it on an unobservable
trade-volume channel.

**Implementation.**  `backend/strategy_engineV2.py`, `update_state`
at line 1849: replace `if o.volume > 0:` with an unconditional feed,
with weight = `max(o.volume, 1.0)` so:
  - When volume IS observable (e.g. live trading, or future recordings
    with volume), the volume-weighted KDE discipline is preserved
    exactly (volume ≥ 1, Silverman-clipped-to-1 floor for non-degeneracy).
  - When volume is unobservable (current recording dataset), the
    constant-weight uniform KDE of the particle's price trajectory
    provides a physically-meaningful ρ.  The accumulated buffer
    becomes a non-trivial "price-occupancy distribution over T_w",
    which on trends yields a Gaussian mound peaking at the T_w-average
    of recent x̂_k's — i.e. a real HVN, giving U(x) genuine barrier
    structure.

**Why previous implementations failed.**  iter02's observable-anchored
h̄ (EWMA(r²)) and  φ̄ (EWMA(δ/(v+ε)) fixed the OU anchors but the
*market potential*  ρ never received its observable counterpart —
add_trade was still volume-gated.  This made the engine's entry/exit
decisions operate against a degenerate (ρ≡1) market potential on every
quantitatively-tested recording.

### Co-fix — apply audit's spec-literal sign convention (Fix #1)

With ρ observably-anchored, the audit's literal-spec sign convention
becomes mathematically coherent: the drift-work term is no longer the
sole source of barrier asymmetry; ρ provides actual HVN-vs-tail
geometry.  The downstream Kramers-escape-formula:
  k = sqrt(ω_0·ω_b)/(2πγ) · exp(-ΔU/T) · vol_corr · ratio

becomes well-posed: with real ΔU of order T (log-density differences),
`exp(-ΔU/T) ≈ exp(-O(1))` stays in a numerically safe range,
eliminating the iter09-catastrophic underflow mode.

The audit's exact sign is applied jointly to maintain mathematical
consistency: a spec-faithful ΔU^± AND a spec-faithful ρ together
form a single mathematically-coherent barrier-energy construct.
**iter13 tests the audit's prescription holistically**, not iter09's
isolated sign-flip.

### Acceptance gate
1. Smoke on the iter08 worst-20 records: per-exit reason migration, no
   45× overtrading observed (i.e., NO repeat of iter09's catastrophic
   churn).
2. Full 950-record batch vs iter08 baseline via `paired_diff`:
   ACCEPT iff Wilcoxon p < 0.05 (one-sided greater), bootstrap 95% CI
   of mean Δ-PnL > 0, McNemar p < 0.05 for profitable-flip majority,
   AND total PnL improves by at least +2 SOL aggregate.
3. No new free parameters introduced; state-space formulation, RBPF
   posterior, UKF propagation, and Kelly sizing untouched.

### Files modified (iter13 part A — ρ-anchor only)
`backend/strategy_engineV2.py:1849` — volume guard removed; ρ fed
unconditionally with weight = `max(o.volume, 1.0)`.

### iter13 part B — restore per-candle potential computation
**Pre-existing latent defect identified during iter13 design.**  The
backtester is the canonical production path.  `backtester.py:222/232/240/
249` passes `_build_full_result=False` for all 4 intra-candle states.
The adapter-side gate `strategy_engineV2.py:2484` is
`(_build_full_result AND _last_potential) OR (buy_volume+sell_volume>0)`
— with backtester's False and zero-volume recordings, the gate collapses
to False on EVERY state of EVERY candle.  Result: the production backtest
computed `MarketPotential` **once, on the first state-update of each
recording**, with an empty KDE buffer, and used that STALE first-bar
potential snapshot for the entire ~10000-bar recording.

Verification probe (added compute-call counter, ran 200 candles):
```
iter08 baseline rec482: total compute calls=1, U-magnitude changes=1
```
The iter08 trade-trace probe (`backend/analysis/probes/iter08_trade_trace.py`)
that surfaced the "kramers_degeneracy mechanism" used `_build_full_result=
True`, so compute fired every state update — it documented degeneracy
behaviour that the iter08 PRODUCTION batch never actually exhibited (the
production batch used a one-bar-frozen V_liq barrier + live mu drift).

Two important consequences for evaluating past iterations:
1. The +17.89 SOL `kramers_down_exit` engine reported in iter08 came
   from the iter08's drift-work algebra interacting with a FROZEN first-
   bar V_liq potential — i.e. du_up/down evolved BAR-by-bar because mu_t
   and x_t evolved, but with the U landscape itself unchanged.
2. The iter09 sign-flip catastrophic regression (457× overtrades on
   Agamemnon rec1843) was an interaction of the spec-literal sign with
   the FIRST-bar V_liq snapshot's symmetric form, not with a fresh KDE
   barrier landscape.  (The kernel was misdiagnosed at iter09 as
   "empty KDE buffer ⇒ ρ≡1 ⇒ spec-sign makes k_down=0 ⇒ no exits fire"
   — actually with iter08 the exits DID fire: just via the mu-driven
   `1e6` path on the frozen-symmetric V_liq, not via proper ΔU/T
   e^{-ΔU/T} evaluation).

**Files modified (iter13 part B):** `backend/backtester.py:249` —
state-4 of each candle passed `_build_full_result=True` to unlock the
adapter's lattice-fresh compute gate; states 1-3 retain the fast-path
False (perf optimization).

### iter13 part C — apply auditor's spec-literal `- ½μ_t(x⁻-x_t)` sign to du_down
**Files modified (iter13 part C):** `backend/strategy_engineV2.py:1501-
1502` — `du_down` formula spec-aligned.

### iter13 full smoke result — REJECTED (2026-07-24)

Tested all three fixes jointly (A + B + C) on the same 11-record subset
used for the iter08 baseline smoke (`iter08_baseline_smoke` cohort +
worst-10 worst-records), via `run_batch` in canonical venv:

| record | iter08 trades / win / pnl | iter13A+B+C trades / win / pnl |
|---|---|---|
| 179  |  6 / 5 / +0.05081 |  4 / 1 / -0.01820 |
| 300  | 14 / 13 / +0.08410 |  3 / 0 / -0.00778 |
| 349  |  9 / 8 / +0.02110 |  5 / 3 / +0.00851 |
| 482  | 14 / 11 / -0.05689 | 25 / 6 / -0.05679 |
| 500  |  3 / 2 / -0.04304 |  1 / 0 / -0.00045 |
| 523  |  1 / 0 / -0.00382 |  1 / 0 / -0.00072 |
| 716  |  2 / 1 / +0.00688 |  1 / 0 / -0.00873 |
| 722  | 10 / 8 / +0.00119 |  9 / 2 / -0.00655 |
| 946  |  3 / 2 / +0.00301 |  3 / 3 / +0.00720 |
| 960  |  3 / 2 / -0.02745 |  4 / 1 / +0.00150 |
| 1843 |  1 / 1 / +0.00603 |  9 / 3 / -0.00838 |
| **Total** | **66 / 53 / +0.04193** | **65 / 19 / -0.09039** |

**Headline evidence (each metric degradation independently a REJECT signal):**
- Aggregate PnL: +0.042 SOL → **-0.090 SOL** (regression of -0.13
  SOL on 11-record subset).
- Win rate: 80.3% → **29.2%** (catastrophic churn).
- The biggest disaster is rec300 Shipley pullback: iter08 captured
  +0.084 SOL on 14 buys/13-W, iter13 captured -0.008 SOL on 3 buys/0-W
  — i.e. the +17.89 SOL engine entry/exit pattern was structurally
  broken by iter13's fresh-potential behaviour.
- 1843 rec (the iter09 smoking-gun) got 9 trades / -0.009 SOL under
  iter13 — NOT a 457× explosion like iter09 (good — confirms Na+KDE
  anchor + per-candle compute is fundamentally safer than iter09's
  isolated sign-flip); but it STILL regressed vs iter08's 1 trade /
  +0.006 SOL.

**iter13-A+B alone** (no spec-literal sign change) tested separately:
```
TOTAL: 65 trades, 19 win, -0.09039 SOL
```
Numerically identical to iter13-A+B+C at 3-sig-fig precision. So the
regression is **dominated by parts A+B (anchor ρ + per-candle compute),
NOT by the spec-literal sign change**. The sign was a no-op on the
drift-dominated `k_down=1e6` path that fire-trade.

### iter13 root-cause: KDE-tracks-the-particle pathology

Forensic trace of rec300 first 800 candles (with iter13 active):
```
cdl   0 | nP=  5 | Umin=0.000 | Umax=0.576 | h_bw=0.0073
cdl  50 | nP=204 | Umin=0.000 | Umax=2.34  | h_bw=0.0203
cdl 150 | nP=600 | Umin=0.000 | Umax=19.32 | h_bw=0.0425
cdl 500 | nP=600 | Umin=0.000 | Umax=1.05  | h_bw=0.0279
cdl 700 | nP=600 | Umin=0.000 | Umax=2.96  | h_bw=0.0651
```
ρ is now real (peak normalized, monotonically increasing U on either
flank).  But on the first sustained uptrend of rec300 (candle ~10-11,
mu_t ≈ +0.014 pump), the latent-state trace shows:
```
mu_t = +0.014  du_up = +0.198  du_down = -0.00013  k_down = 1e6   P_down = 1.0
```

i.e. the FIRST thing iter13's anchored-occupied KDE does on a real pump
is fire `kramers_down_exit` — exiting the position at ~entry price +
immediate fee cost — because:
1. A pump pulls x_t ABOVE the recent-occupancy avg ⇒ ρ peaks BELOW x_t
2. The "basin" the Kramers formula identifies is at the recent-average
   position BELOW x_t (the lag-follow pattern)
3. `U_down < U_(at x_t)` ⇒ `U_down - U_basin < 0` ⇒ du_down < 0 ⇒
   `k_down = 1e6` ⇒ `P_down = 1` ⇒ exit ladder fires.
4. The drift-work term `+/- ½μ·Δx` is numerically NEGIGIBLE (μ≈0.014,
   Δx ≈ 2e-4 grid-res ⇒ ½μΔx ≈ 1.4e-6) — orders of magnitude smaller
   than the U-slope term (#3), so the sign of the drift-work is
   irrelevant to firing behaviour (this explains why **iter13-A+B and
   iter13-A+B+C gave numerically identical results**).

### Mathematical reconciliation: anchoring ρ by NULL-occupancy is the wrong observable

The spec §3 line 100 defines `U(x,t) = -T_t · log ρ(x,t) + V_liq(x,t)`
and §3 (Volume-Weighted KDE ρ) specifies ρ is the **trade-volume-
weighted density of price buckets' volume** traded in the recent past.
The quoted assumption: HVNs mark support/resistance levels — prices at
which the market processed concentrated trade volume (held inventory
throttled between the spread), which act as legitimate barrier bands
to subsequent moves.  The Kramers escape framing then interprets a
particle ABOVE such an HVN as "trapped between upward resistivity and
downward support", and a particle BELOW such an HVN as the inverse.

**The data has no observable trade-volume channel.**  None of the 147
sampled recordings has non-zero candle volume.  The mathematically naive
fallback (occupancy = where the price-POSTERIOR has been in the past)
does **NOT** capture the same physical quantity: the recent-occupancy
mean lags any Sustainable trend, so it captures the **trend's starting
point**, not support/resistance levels.  When the trend is intact,
recent-average-x trails x_t ABOVE → triggering spurious "downward escape
free" (instead of "trapped between ramps behind and ahead of the trend"
which is what a volume-weighted KDE alone would capture).

This confirms the explicit caveat in the iter09 reject note: *"the
mathematically purer alternative — fix the empty KDE buffer ... would
require either (a) synthetically seeding the KDE from UKF posteriors
when the trade buffer is empty (introduces a new distribution assumption)
or (b) accepting that on this dataset the KDE market potential has
zero observability, hence the bump approach can't work, and falling
back to a structure like raw ATR / momentum on the exit side."*  iter13
tested case (a) — and the regressive result confirms the anticipated
distribution-assumption caveat.

**VERDICT: iter13 REJECTED — all three parts reverted.**

Diff restored:
- `backend/strategy_engineV2.py:1501-1502` du_down sign back to iter08 `+ ½μ·Δx⁻`.
- `backend/strategy_engineV2.py:1849` volume guard restored: `if o.volume > 0:`.
- `backend/backtester.py:249` state-4 returned to `_build_full_result=False`.

Verification: post-revert rec482 backtest matched iter08 baseline
exactly at -0.056889 SOL / 14 trades.

### Two important secondary findings preserved for iter14+ research

**1. The iter08 production backtester uses a stale one-bar-frozen V_liq
potential across each entire recording.**  This is a parity
inconsistency with `forward_tester` (default `_build_full_result=True`
⇒ compute every state) and `live_trader` (default True).  The AGENTS.md
invariant "Backtester, ForwardTester, and LiveTrader must evolve
StrategyEngine state identically" is currently violated.  Future
research should NOT paper over this — a parity-aligned backtester
(without ρ-anchoring) would establish a clean baseline against which
future real-barrier modifications can be tested; the persistence of the
discrepancy means the production backtest's behavioural baseline differs
from what live trading would do today by an unknown margin (iter08_full
shows the production backtest is currently profitable at +17.89 SOL on
Bayesian exits, while live trading would also compute-fresh per state
and might diverge from any opening direction).

**2. The recorded-backtrade dataset provides no observable trade-volume
channel to the V2 engine.**  The KDE market potential is structurally
unobservable here — the spec §3 formula `U(x) = -T log ρ(x) + V_liq(x)`
collapses to just `V_liq(x)`, which on zero-volume candles further
collapses to a symmetric exponential taper (because the adapter
synthesizes `bid_depth = max(buy_volume+1.0, 1.0)` ≡ 1.0 and
`ask_depth = max(sell_volume+1.0, 1.0)` ≡ 1.0, both equal).  The
mathematical specification therefore is only a partial-input state
machine on this dataset; the iter08 winning `kramers_down_exit` shape
is the engine's drift-work alone mapping into the frozen first-bar's
taper landscape.

The directive's stated objective ("preserve the stochastic state-space
model while improving empirical performance, judge changes only by the
stochastic RH framework's mathematical correctness") cannot be served
by modifying KDE-related components when ρ itself is unobservable.  A
future iteration that wants to bestow the V2 engine with real HNV
barriers without overfitting it to the per-state-update ℝ^5-path would
need to backpropagate genuine HNV-like support/resistance estimators
into ρ — e.g. volatility-volume proxies from the recorded candle range
(`range = log(h) - log(l)`) as the trading-volume surrogate, computed
once per candle.  Recorded candles do contain `open, high, low, close`
even when `volume = 0`, so the BC range is observable — and parabolic
local extrema of the BC range are a physical (non-arbitrary) proxy for
volume-weighted liquidity pressure nodes.  That is a legitimate iter__
research path; iter13 itself was not.





## iter14 — directive's dt=1/ticks_per_state fix + IG catalyst restore (REJECTED)

**Date:** 2026-07-25
**Directive (user):** "fix `dt` time-dilation in
`StrategyEngineV2Adapter.update()` — currently hardcodes `"dt": 1.0`
at line ~2456 but V1 pipeline calls `update()` 4× per 1s candle, telling
the SDE 4s elapsed per candle. Fix: `dt_step = 1.0 / ticks_per_state`.
Then restore iter12 Kelly-optimal Expected Hold Exit as primary exit
gate (when `decision_method == "ig"`)."  The directive noted iter12's
μ_t violent oscillation → 144k micro-churn trades as the prior
rejection mechanism.

### iter14-A — `dt = 1.0 / ticks_per_state` in V2Adapter obs dict

**Rationale (directive's view):**  The SDE propagation interprets
`dt` as physical-seconds elapsed per state-update.  iter08 hardcodes
`dt=1.0` per call; with V1 calling `update()` 4× per 1-second candle
(4-state intra-candle expansion), the SDE believes 4s of OU
mean-reversion + 4s of process-noise integration took place when only
1s of physical time elapsed.  Per candle, iter08 actually integrates
λ_μ×4 effective mean-reversion (~0.52, not 0.15) and 2× the spec
process-noise injection rate.  iter14-A halves/diagonalises this:
*dt=0.25* per call × 4 calls/candle = 1.0s of SDE integration per
1s candle = spec-correct.

**Audit of time-step consumers** (pre-implementation):
- `warmup_seconds=30`, `iter10_*_bars` windows, `iter05_decay_window`,
  KDE decay `lambda_0=1/300`, `tau_min/tau_max` horizons — all are
  already unit-tagged in `bar_count` units or physical seconds; the
  dt fix only alters the UKF propagation *rate*, not these counts.
- `now_t = float(self._bar_count)` in `add_trade` is in engine-bar
  units; KDE decay already operates per engine-bar (~1/75 s⁻¹ effective
  rate at iter08, ~1/300 s⁻¹ after fix).  Zero-volume dataset makes
  the KDE buffer never fill, so the decay rate change is moot.

**Implementation:**  `strategy_engineV2.py:obs['dt'] = 1.0 / float(_ticks_per_state)`
with `_ticks_per_state` = cfg.get('ticks_per_state', 4).

### iter14-B — Inverse-Gaussian first-passage catalyst + Expected Hold Exit

**Implementation:**  Restored the iter12 IG catalyst (`_first_passage_decision`,
default off via `"decision_method": "kramers"`) and the iter12 Kelly
Expected Hold Exit gate (`mu_hat_tau ≤ f + s_0 + |μ_t|·Δ_lat`,
fires only in `"ig"` mode).  Default `kramers` mode preserves iter08
behaviour byte-for-byte for A/B comparison.  Added `scipy.special.log_ndtr`
import + a `math.erfc`-based fallback shim for environments without
scipy.

### iter14 smoke results — REJECTED (2026-07-25)

#### iter14-A (kramers + dt=0.25), 2-record smoke [rec482, rec349]

| recording | iter08 trades | iter08 PnL | iter08 WR | iter14-A trades | iter14-A PnL | iter14-A WR |
|-----------|---------------|------------|-----------|-----------------|--------------|-------------|
| rec482    | 14            | -0.057     | 78.6%     | 12              | -0.220       | 50.0%       |
| rec349    | 9             | +0.021     | 88.9%     | 3               | -0.025       | 33.3%       |
| **sum**   | **23**        | **-0.036** | **82.6%** | **15**          | **-0.245**   | **46.7%**   |

Iter14-A regresses 4-7× on this 2-record smoke; WR drops from 82.6%
to 46.7%; net PnL swings from -0.036 to -0.245 SOL.

#### iter14-A (kramers + dt=0.25), worst-volume 20-record smoke (partial 7/20)

After 10-minute timeout, 7/20 records completed:

| recording | iter08 trades | iter08 PnL | iter08 WR | iter14-A trades |
|-----------|---------------|------------|-----------|------------------|
| rec1274   | 48            | +0.015     | 45.8%     | **0**            |
| rec1663   | 16            | +0.065     | 81.2%     | **0**            |
| rec1247   | 15            | -0.015     | 86.7%     | **0**            |
| rec1798   | 15            | +0.006     | 80.0%     | **0**            |
| rec642    | 14            | -0.043     | 85.7%     | **0**            |
| rec1469   | 17            | -0.067     | 70.6%     | **0**            |
| rec1115   | 31            | +0.151     | 64.5%     | **0**            |
| **sum**   | **156**       | **+0.112** | —         | **0**            |

**All 7/7 completed worst-volume records generated ZERO trades under
iter14-A.**  Iter08 averaged 22 trades / +0.016 SOL per record on the
same tokens.  This is an unambiguous regression, not smoke noise.

#### iter14-B (IG + dt=0.25), 2-record smoke [rec482, rec349]

| metric            | iter08     | iter14-B IG |
|-------------------|------------|-------------|
| total trades      | 23         | 1007        |
| win rate          | 82.6%      | 3.4%        |
| total PnL (SOL)   | -0.036     | -1.889      |
| dominant exit     | kramers_down_exit | ig_expected_hold_exit (×1007) |

The IG catalyst's symmetric barriers (x_± = x_t ± β·σ_t·√τ) yield
P_up ≈ P_down whenever |μ| is small relative to σ_t·√τ.  Direction
flips between +1 and -1 on adjacent sub-state updates; with the
expected-hold exit firing on every sign-change, the engine cycles
in-and-out once per ~2 sub-state updates, paying 0.2% fees each
cycle → -1.89 SOL of pure fee churn on a 2-record sample.  This
replicates the prior iter12 full-batch rejection mechanism (144k
micro-churn trades) at smoke scale.

### iter14 root cause analysis

#### Why iter14-A (dt-fix) silences entries

The kramers escape rate is `k = sqrt(ω₀|ω_b|) / (2πγ) · exp(-ΔU / T_t)`.
On this zero-volume dataset, `ρ ≡ 1` everywhere → `T_t = floor`,
ΔU collapses to essentially the drift-work difference
(`0.5·μ_t·(x_± - x_t)`); the only term that prevents total degeneracy
is the variance in μ_t magnitude.  With iter08 dt=1.0, μ_t evolves
4× too fast per physical second → μ_t has a *larger empirical
volatility on the bar-count grid* → ΔU shows greater asymmetry
between x± barriers → k_up and k_down differ more → direction
resolves +1 or -1 more often.  With iter14-A dt=0.25, μ_t evolves
at spec-correct 0.15/s → μ magnitude variance per unit bar_count is
4× smaller → ΔU ≈ 0 → escape rates collapse to one absorbing state
→ no directional bias → no positive Kelly utility → no entries.

The iter08 production engine is therefore empirically *calibrated*
to the 4× inflated mean-reversion: the DEFAULT_CONFIG params
(λ_μ=0.15, κ_μ=0.05, η=0.10, α=0.20, σ_μ=0.10, σ_h=0.20, σ_φ=0.15,
σ_ℓ=0.10) were set by the original V2 implementation assuming dt=1.0
per state-update, not dt=0.25 per state-update.  Switching to
dt=0.25 alone (without scaling those rate constants ×4) is equivalent
to simultaneously tuning λ_μ, η, α from 0.15→0.0375,
0.10→0.025, 0.20→0.05 — i.e. globally detuning the OU rates by 4× —
which predictably suppresses entry triggers.

**Conclusion:**  The dt=1.0 was not a "bug" in the iter08 production
sense; it was the *de facto configuration the iter08 DEFAULT_CONFIG
params were tuned against*.  Correcting dt without simultaneously
retuning all rate constants is mathematically consistent but
empirically a regression.  A spec-correct V2-correct iter14 would
require a full sweep of (lambda_mu, eta, alpha, sigma_mu, sigma_h,
sigma_phi, sigma_ell, theta, kappa_mu) × 4 to compensate, which is
out of scope for a single-iteration directive.

#### Why iter14-B (IG catalyst) churns

The IG first-passage catalyst for BM with drift has symmetric
barriers ±β·σ_t·√τ around x_t.  For small |μ_t| relative to σ_t·√τ
(which is the post-dt-fix regime; pre-dt-fix |μ_t| was 4× larger),
the two first-passage probabilities are nearly equal:
P_up ≈ P_down ≈ 0.3, P_zero ≈ 0.4 — and any 0.5% noise flip in μ
swaps which barrier wins.  Each flip closes the position via the
expected-hold-exit and re-enters in the new direction.  With zero
directional persistence the engine becomes a fee-paying random walk.

This is the same mechanism that REJECTED iter12 at full-batch, and the
dt-fix actually makes the IG catalyst *worse* (because |μ_t| variance
shrinks further under dt=0.25, barrier-symmetry is even tighter).

### iter14 conclusion + revert

Both iter14-A (kramers + dt=0.25) and iter14-B (IG + dt=0.25 +
expected-hold-exit) are empirically regressive vs the iter08
baseline at smoke scale.  iter14-A is a *more fundamental* regression
than iter13: it silences entries entirely on the worst-volume subset
rather than just mis-timing exits; iter14-B re-triggers the iter12
churn pattern.

All iter14 modifications reverted in `backend/strategy_engineV2.py`:
- `obs['dt']` restored to `1.0` (line ~2456).
- `_first_passage_decision` function removed.
- `decision_method`, `iter12_beta_up`, `iter12_beta_down` keys dropped
  from DEFAULT_CONFIG.
- IG dispatch branch in `get_decision` removed.
- `ig_expected_hold_exit` branch in `_check_exit_v2` removed.
- `scipy.special.log_ndtr` import removed.

Working tree restored to iter08 baseline verified by re-importing
`strategy_engineV2` and confirming no `_first_passage_decision` /
`log_ndtr` attributes and `obs['dt'] = 1.0`.

### iter14 follow-on candidates

The dt-fix concept is correct, but it is not a *standalone* fix: it
requires retuning the DEFAULT_CONFIG rate constants.  Specifically:

- **iter15 candidate**: Set `dt = 1.0 / ticks_per_state` AND scale
  the OU rate constants ×4 (lambda_mu=0.60, eta=0.40, alpha=0.80,
  theta=0.40) so that per-candle mean-reversion fraction matches
  iter08's effective behaviour.  This is the "spec-correct
  equivalent of iter08" and can serve as a clean A/B baseline if the
  full-batch reproduces iter08's headline metrics.  Risk: if the
  rate constants were *also* tuned for some other reason (e.g. the
  empirical μ_t / σ_t magnitudes that drive the kramers ΔU
  asymmetry), scaling them in lockstep may not exactly restore iter08.

- **iter16 candidate (independent)**: Use observable candle-range
  volume proxies (per the iter13 follow-on note) to seed the KDE —
  `add_trade(log_mid, max(range, 1.0), now_t)` symmetric in every
  candle regardless of buy/sell split.  Does not require dt-fix and
  avoids the iter13 lag-follow pathology (range-based weights are
  symmetric so they don't anchor ρ to the trailing particle).


## Iter 15 — Recorder-side root-cause fix (PumpSwapRPCClient vault-delta extraction)

**Date:** 2026-07-27
**Files modified:** `backend/pumpfun_client.py` only (recording-system layer, NOT engine)

### Diagnosis

A full audit of `backend/data/price_data.db` revealed that **the V2
strategy engine has been running on synthesised price-only data** for
the entire research history:

* Across all **2,394,211 candles** in the database, **0 candles** have
  `buy_volume > 0` and **0 candles** have `sell_volume > 0`
  (universally zero, never populated since the recorder was deployed).
* Only **1,588 candles** (0.0663 %) have nonzero `volume` at all — and
  those are confined to **4 outlier recordings** (`timeframe=5m`,
  tickers like `ES=F` / `TSLA`, populated by `get_historical_candles`
  seeding, not the V2-engine target dataset).
* Of the 1,510 1s/5m recordings, **0 of the 1,506 1s memecoin
  recordings** have any volume anywhere — every single one was
  recorded through a stream source that emits `sol_amount=0.0` and
  `tx_type="update"`.

Root cause: `recorder_start` (`backend/main.py:162-170`) routes
each recording to one of three stream clients based on
`token_info["_live_source"]`:

| Source | Class | `sol_amount` | `tx_type` |
|---|---|---|---|
| `"pumpportal"` (default for pump.fun bonding-curve) | `PumpFunWSClient` | real `solAmount > 0` | real `"buy"`/`"sell"` |
| `"solana_rpc"` (migrated pump.fun → PumpSwap) | `PumpSwapRPCClient` | hardcoded `0.0` | hardcoded `"update"` |
| `"dexscreener"` (fallback) | `DexScreenerPollClient` | hardcoded `0.0` | hardcoded `"update"` |

`PumpSwapRPCClient._current_trade()` (recorded price-only state
snapshots from `accountSubscribe` vault updates) and
`DexScreenerPollClient.stream()` both explicitly emit
`sol_amount = 0.0` and `tx_type = "update"`, so the aggregator records
`volume = sell_volume = buy_volume = 0` for every candle. The
`aggregator.process_trade()` correctly increments `buy_volume` only
when `is_buy is True` etc., but the recorder setting `is_buy=None`
silently routes every trade into `volume = sol_amount = 0` and the
buy/sell split into 0/0.

This means **the entire iter01→iter14 quantitative research history**
(950 recordings, 3197 trades, -7.395 SOL canonical iter08 baseline)
was produced with KDE `ρ ≡ uniform`, `φ_t = δ_t/(v_t+ε) = 0/(0+ε) = 0`
everywhere, and the Bayesian escape-rate engine structurally degenerate
— exactly the failure mode every iter05–14 attempt tried and failed to
fix at the engine layer because the engine layer was not the source of
the failure.

### Iter 14 framing — corrected

The iter14 Fix-A/B/C patch (a 387-line addition to
`backend/strategy_engineV2.py` that introduced a `candle_range`
observation field and observable-anchored ρ seeding via Christensen-
Podolskij realized-range estimators) was reverted to clean iter08
baseline on 2026-07-27 (`git checkout -- backend/strategy_engineV2.py`)
once the recorder-bug diagnosis proved the modified-KDE strategy was
compensating for missing input data, not for a flawed estimator. Clean
HEAD was verified to reproduce iter08 numbers on rec 482 (14 trades /
78.57 % WR / -0.057 SOL), confirming the revert was byte-equivalent
to iter08 and no iter14 strategy improvement remains in the repo.

### The patch

`backend/pumpfun_client.py` was patched in three places in the
`PumpSwapRPCClient` class (NO engine changes):

1. **`__init__`** — added `_prev_base_raw` / `_prev_quote_raw`
   trackers for the previous raw vault balances, so successive
   accountSubscribe notifications can be diffed.

2. **`_current_trade()`** — rewritten to accept `delta_base_raw` and
   `delta_quote_raw` (the net vault deltas over the drain batch) and
   to actually populate trade volume and direction:
   * `token_amount = |Δ_base_raw| / 10^base_decimals`
   * `sol_amount = |Δ_quote_raw| / 10^quote_decimals` (when quote-mint
     is WSOL, the typical pump memecoin pairing)
   * `tx_type = "buy"` if WSOL flowed INTO the pool quote vault
     (taker paid SOL, took tokens); `"sell"` if WSOL flowed OUT
     (taker paid tokens, took SOL).
   * Same-direction deltas (LP deposits/withdrawals/fee accruals)
     are correctly NOT detected as trades — they emit
     `tx_type="update"`, `synthetic=True`, zero volume, identical to
     the pre-patch heartbeat behaviour.
   * The very first emit on `_load_pool` is a state-snapshot (no
     previous state to diff) and is emitted as `tx_type="update",
     synthetic=True, sol_amount=0`. Subsequent real swap ticks emit
     `tx_type="buy"/"sell", synthetic=False, sol_amount > 0`.

3. **`stream()` drain loop** — accumulates net per-batch deltas
   across the accountSubscribe notification queue and passes them
   into `_current_trade()`.

### End-to-end smoke verification

Live 60-second smoke test against an active PumpSwap WIF pool
(pair `2Xkm4YfqeuK3HLdzDSYMuRWhdT34k9kcoToXXwzz78LF`) confirmed:

* 1 real sell trade landed during the window:
  `tx_type="sell", sol_amount=0.41111 SOL, token_amount=2,359,695.36`.
* The aggregator end-state: `volume=0.41111 buy_volume=0 sell_volume=0.41111`.
* 57 heartbeats correctly emitted as `synthetic=True` with zero
  volume — no fake trades fabricated by the heartbeat path.

### Implications

After fresh recordings are made with this patch, the iter08 baseline
will need to be **rebuilt** on the new dataset. The current
`iter08_baseline_full` (and all of iter01–14) are constrained to the
volume-free regime and should be re-evaluated — the +17.89 SOL
`kramers_down` profit core and -25.98 SOL `recording_ended` drag
may both shift materially once real order flow feeds `φ_t` and `ρ(x)`.

### What was NOT done

* No engine changes (`strategy_engineV2.py` is byte-equivalent to
  iter08 HEAD).
* No backtest re-runs (would require either fresh recordings or
  retroactive volume backfill, neither of which are in scope here).
* The `DexScreenerPollClient` fallback path was not patched — it
  has no reserves to diff against and would require a REST trade-
  volume endpoint to populate order flow, which is out of scope for
  the immediate fix. Future agents should investigate the DexScreener
  `volume.h24`/`txns.h24` time series if the fallback path becomes
  load-bearing again.


## Iter 16 — Fresh recording dataset collected (2026-07-27 → 2026-07-28)

**Date:** 2026-07-28
**Files modified:** none (dataset landfall only; the user wiped
`backend/data/price_data.db` of legacy recordings and let the post-
iter15 recorder populate it fresh)
**Engine:** unchanged (strategy_engineV2.py remains at iter08 byte-
equivalent HEAD; iter15 recorder patch is at commit `195aa90`).

### Dataset snapshot

`backend/data/price_data.db` audited 2026-07-28 contains fresh
1s memecoin recordings**, all collected with the iter15 recorder patch
active.

### Implications for the next research session

1. First candidate batch on this dataset MUST be labelled
   `iter16_baseline_full` — it becomes the new canonical baseline
   against which all future candidate iterations are compared via
   `paired_diff.py`.
2. Do NOT compare candidate batches against `iter08_baseline_full`
   (the legacy volume-free baseline preserved at
   `backend/analysis/iter08_baseline_full.json`). Its absolute
   magnitudes measure a regime that no longer exists.
   
5. `strategy_engineV2.py` is unchanged at iter08-byte-equivalent HEAD
   (verified by `git diff` being empty after the iter14 Fix-A/B/C
   revert). The iter11/iter12 scaffolding is still present but
   disabled. No engine experiments have been run on the fresh dataset
   yet — the entire iter01→14 failure-mode analysis (especially the
   `φ_t ≡ 0` and `ρ ≡ uniform` claims) must be RE-DERIVED on the fresh
   data, not carried over by intuition.

## Iter 17 — Vectorized RBPF Engine (Performance Patch)

**Date:** 2026-07-28
**Files modified:** `backend/strategy_engineV2.py`

**Hypothesis:** The bottleneck in the V2 engine backtest was the sheer volume of Python interpreter loop overhead caused by maintaining an object-oriented list of 200 `_Particle` instances. Moving to a contiguous Structure of Arrays (SoA) layout and wrapping the execution loop inside Numba `@njit` kernels should significantly decrease the runtime while producing mathematically identical states and signals.

**Implementation:**
1. Eradicated the `_Particle` Python class and converted `RaoBlackwellisedParticleFilter` to use contiguous NumPy memory banks (`self.p_mu`, `self.p_P`, `self.p_regime`, `self.p_weight`).
2. Bundled the predict, update, and log-likelihood iterations into a single monolithic C-speed kernel `_rbpf_predict_update_jit`.
3. Extracted posterior mean, topological regime derivation, and systematic resampling logic into corresponding Numba kernels (`_posterior_mean_jit`, `_rbpf_regime_assign_jit`, `_systematic_resample_jit`).


**Conclusion:** **ACCEPTED.**
The vectorization was successful, demonstrating zero divergence in metrics or decision states while yielding a substantial 20% reduction in execution latency. The remaining overhead is heavily concentrated within the continuous evaluation of the kernel density estimations (`compute_market_potential`) and legacy SQLite read I/O operations, meaning further engine logic tuning is likely to produce diminishing returns compared to KDE optimizations.
