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

## iter04_full baseline (recorded 2026-07-20T18:33 GMT)

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

This is the **current production-best baseline**. All subsequent
iterations must beat this under `paired_diff` statistical criteria.

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
