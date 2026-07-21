# V2 Baseline Failure Analysis (iter01_baseline)

**Date:** 2026-07-20
**Engine file:** `backend/strategy_engineV2.py`
**Run:** partial batch on 1495 completed recordings (timed out at ~50%).
**Result:** 0 trades placed on every token inspected.  Per-trade JSON files
were therefore NOT written (backtester.py:291 only writes logs when trades
are non-empty).  Direct numeric metrics cannot be extracted — the engine is
not trading at all.

This file collects the **latent-state evidence** showing *why* V2 never
trades.  Each failure mode is tied to a specific component of the
stochastic framework and a mathematically justified fix is proposed for
the next iteration.

---

## Failure Mode A — h collapses to the −15 clamp (volatility collapse)

**Symptom:** After 300 random-Gaussian log-return steps simulating memecoin
data, particle `h` values are uniformly at `−15.0` (the lower clamp).
`exp(h/2) ≈ exp(−7.5) ≈ 5.5e-4`.  As soon as this happens `σ_t ≈ 0`,
the KDE grid collapses to a delta, the potential U is identically 0, and
every Kramers barrier computation degenerates.

**Root cause (mathematical):** The h SDE is:

    h_k = h_{k-1} − η · (h_{k-1} − h̄) · Δt + σ_h · √Δt · ε_h

with `h̄` defined as a **recursive EMA of the posterior mean of h**:

    bar_h ← (1-α_e) · bar_h + α_e · h_posterior_mean

When `h̄` starts at `-4.0` and the OU pulls all particles toward `h̄`,
the posterior mean drifts down each step.  The EMA, being a *mean* of that
same posterior mean, is dragged down with it.  This creates a positive
feedback loop:

    bar_h ↓ ⟹ OU pulls h ↓ ⟹ posterior mean(h) ↓ ⟹ bar_h ↓ again

until every particle is at the `−15` clamp.  The process has no anchor
to external price variance.

**Component:** Volatility estimation (h SDE in §2.3, EMA bar_h in §2.5).

**Why it broke:** The spec says "*recursive EMAs for φ̄ and h̄ must be
updated globally*".  But the spec's implicit anchor for h̄ is the
*stationary level* of the h-OU, which is the realised variance of the
observations themselves — not the recursive mean of the latent h.  Using a
posterior-self reference for the OU mean-reversion level creates an
unobservable self-reference (the only stable point of `h̄ = mean(h)` under
the OU is the clamp).  There is **no ground-truth signal feeding h̄**.

**Mathematically justified fix:** Replace the latent self-reference
`bar_h = EMA(h_post)` with the observation-grounded realised log-variance
running estimate:

    bar_h_obs = log(ema(w·r²) + ε)   where r = log_return,  w=1

Equivalently a weighted EWMA of squared log-returns, mapped to log-space.
This explicitly ties the OU anchor to the observable variance process and
is the standard form for a Heston/SV-OU "target" (cf. Barndorff-Nielsen
1967).  The information is the same magnitude (variance of x), but it
comes from the observation channel rather than the latent posterior, so
the OU is well-defined.  This **preserves the state-space formulation**
(it is still recursive, still SR-OU), and the latent h still does its job
via the kernel-pulled posterior estimate.

Note: an analogous argument applies to `bar_phi`: it should track the
observation-grounded rolling mean of order-flow imbalance
`(signed_delta / (volume+ε))`, NOT the latent posterior mean of φ.  Fix
the same way.

---

## Failure Mode B — Measurement variance R is dominated by a dead σ floor

**Symptom:** `R_meas = max(spread², sigma_floor²)`  with `sigma_floor = 1e-6`
makes R tiny when spreads are tiny.  When σ_t is at the clamp (failure A),
the UKF Q block `Q[0,0] = σ_eff² · Δt` is also tiny, so any meaningful
log_return creates an enormous residual / Pzz ratio, the Kalman gain is
clamped to 100, and the next x measurement-dominated update teleports
the state across 6 orders of magnitude (we observed `x = 1e6` clamped,
`mu = 50.0` clamped, all 200 particles with **identical weights = 0.005**
— i.e. ESS = N).

**Component:** Observation model (UKF measurement variance R).

**Why it broke:** The fused measurement variance should be the variance
of the actual measurement residual stream — i.e. heterogeneous quarticity
(style of Harvey 2016).  Tying R only to the spread² (and the `sigma_floor`
constant) means the filter has no idea of typical `Δx` magnitude.  When
the latent σ_t is wrong (which it is, see failure A), the gain
miscalibration swings wildly.

**Mathematically justified fix:** Compute R recursively from the
EWMA of squared residuals of the measurement outcome — *i.e. a real-time
estimate of the innovation variance*.  This is the standard Adaptive Kalman
trick (Mehra 1970; the innovation-based covariance estimator).  We replace

    R_t = max(spread², sigma_floor²)

with

    R_t = max(R_ema · pt_scale, sigma_floor²)
    R_ema ← (1-α_R) R_ema + α_R · (residual²)         # once posterior z_pred is known

This is a direct Bayesian *innovation covariance* estimate, fully
recursive, parameter-free beyond the smoothing EMA constant (which is not
a new free parameter — it's an arbitrary estimation bandwidth like `η`).

---

## Failure Mode C — Kramers P_up ≡ 0 / P_down ≡ 1 saturation

**Symptom:** For every decision we observed, the posterior escape rate
satisfies `k_total · τ ≫ 5` so `exp(−k_total · τ) ≈ 0` and the slow
variation formula gives `P_down = (k_down/k_total) = 1` (because the
"barrier downward" degenerated: `du_down < 0` clauses force `k_down=1e6`,
the numerical stand-in for `+inf`).

**Root cause:** Two latent issues.  First, because `σ_t ≈ 0` (failure A)
the KDE has zero bandwidth — the grid KDE is literally a spike at `x_t`,
so on either side of `x_t` the `ρ` is identically 0, meaning `U = -T log ρ`
is `+∞`. This produces a "basin only at x_t" and "barriers on both sides"
of infinite height.  The `_barrier_find_kernel` then reports the *grid
boundary* as the barrier, and because `U_grid[idx_down] = U_grid[idx_t]`
(mono behaviour), `du_down = U_down − U_basin + 0.5·mu·(grid_down − x_t)`
becomes negative — invoking the `1e6` "barrier off" path.

Second, after fixing failure A, the `k_total` values are still likely to
be in mHz range (=1e-3) so `k_total · τ = 1e-3 · 30s = 0.03`,
`exp(−0.03) = 0.97` — which means `P0 ≈ 0.97, P_up ≈ P_down ≈ 0.015`,
i.e. even on a clean trend the engine only assigns ~3% probability of
any trans-barrier move at the 30-second horizon.  This means the gate
`P_up < 0.35` (line 2318 of V2 adapter) is essentially *never* passed.

**Component:** Transition probability estimation (Kramers escape).

**Why it broke:** The spec's modified Kramers formula `k = (ω₀ω_b)/(2πγ)
exp(−ΔU/T)` describes the **thermodynamic transition rate over many
independent realisations** — it has units `1/s` for *particle* dynamics
where `γ` is properly dimensioned.  When `γ = 1/L_t` (§7.3) and `L_t` is
memecoin-scale (`~1e2`–`1e4`), γ ≈ 1e-2 to 1e-4, and ω₀·ω_b ~ small, you
get rates around 1e-3 to 1e-6 per second.  This is correct physics for
"how often a single particle escapes a deep well" but is **not calibrated
to the trade decision horizon**.

**Mathematically justified fix:** The escape rate `k` is dimensionally a
rate per second.  For a trading decision, the relevant question is not
"what is the rate at which one particle *eventually* escapes" but "what is
the probability of escape over horizon τ integrated against the
**forward Kolmogorov semigroup** of the SDE"?  The current engine
approximates it via the slow-variation formula
`P±(τ) = (k±/k_tot) (1 - exp(-k_tot τ))`.  This is fine *if* k is
correctly calibrated — but it produces `P_up ~ 0.01` in normal regimes.

The minimum-cost necessary change: **renormalise k to absorb the horizon
τ directly** by introducing a dimensionless escape time `τ̄ = τ / τ₀` where
`τ₀ = h^{1/2} / ω₀` is the natural basin oscillation period; then
`P±(τ) = (k±/k_tot) (1 - exp(-k_tot · τ / τ₀))`.  This is still the same
slow-variation formula but **using the OU basin timescale** as the natural
time unit instead of seconds.  Mathematically, this is exactly what the
harmonic approximation of Kramers requires — `τ₀ = 2π / ω₀` is the natural
period.

Implementation: replace `exp(-k_total · tau)` with
`exp(-k_total · tau · omega0) ≈ slow-variation in dimensionless time`.

---

## Failure Mode D — Regime posterior never evolves (uniform prior preserved)

**Symptom:** After 300 observations, particle weights are still uniform
`(min=0.005, max=0.005)` and listed particle regimes are exactly the
initial spread `Counter(0:29, 1:29, 2:29, 3:29, 4:28, 5:28, 6:28)`.
This means `_last_state["regime_dist"]` has near-uniform mass across all
7 regimes → entropy ≈ log(7) → confidence(entropy) ≈ 0 → the V2 adapter's
_entry gate_ requires `trend_confidence ≥ 0.79` (entry_confidence_high).
**Gate never opens.**

**Root cause (mathematical):** The spec mandates "regime transitions are
driven by the topological partition logic (Section 4)".  The spec's intent
is that the **discrete regime label is a function of the continuous
posterior state** — `r_t = f(μ_t, μ̇_t, φ_t, h_t, ℓ_t, d_t)` — computed
deterministically each step.  This is the so-called **minimisation
principle** of the topological partition: one state → one regime.

In the current code (`RaoBlackwellisedParticleFilter.step`), particles'
**regime labels are never re-derived** after the initialisation.  They
are preserved across resampling.  Since each particle's UKF state evolves
roughly the same in expectation (the same observation likelihood),
particle weights stay near-uniform and so does the label distribution.

**Why it broke:** The implementation reads `Counter[(p.regime for p in
particles)]` as the "regime posterior", but a particle's regime label
`p.regime` carries **no information content** beyond the initial draw —
it never propagates the topological derivation done in
`derive_regime_topological()`.  That function *is* called (line 1577 of
`update_state`), but its return value is assigned to `state["regime"]`
only — the **per-particle label** is not updated, and the particle
population's regime distribution (`regime_counts` computed inside
`step()`) just enumerates the old frozen labels.

**Mathematically justified fix:** The regime label is a *derived* function
of the continuous state, not a *sampled* latent variable.  The
topological derivation is the exact result.  Therefore:

  * Each particle's regime label must be re-derived **per step** from
    that particle's continuous state (μ, μ̇, φ, h, ℓ, spread).  This makes
    the regime a deterministic re-defined quantity of the continuous
    posterior, matching the spec's intent (per Section 4 of
    `strategyV2.md`).

  * The regime distribution returned to callers is then a proper
    posterior over the *evolving* topological readings of each particle
    — collapses to a delta mass in a strong trend, spreads across 2-3
    regimes when particles disagree about μ-vel.

  * The argmax regime is the modal topology over the particle set —
    matching the spec's "regime $r_t = \arg\max_r P(r \mid \Psi_t)$".

This is **purely a correction of the spec-mandated derivation and is
already called by the engine** — we just need to apply it per-particle
inside `_step_particle` (or after step) instead of only on the posterior
mean.

---

## Aggregate summary

All four failure modes stem from the same root cause — **recursive
structures that reference the latent posterior itself instead of observable
inputs** (`bar_h = EMA(h_post)`, `bar_phi = EMA(phi_post)`) plus an
**unupdated regime posterior** that violates §4 of the spec.

Plan for iter02+:

1. **iter02** — Fix `bar_h` and `bar_phi` to be EMA of observable
   realised variance / delta ratio, not latent self-reference.  Also fix
   measurement R to innovation-based adaptive estimate.  These two
   belong together because they are both side-effects of the same
   "self-reference instead of observable-anchor" bug, and neither can
   be validated without the other.
2. **iter03** — Fix regime posterior (apply topological derivation
   per-particle each step).  This is an independent fix.
3. **iter04** — Fix Kramers slow-variation for OU basin timescale if
   after iter02-03 the escape-rate calibration is still off.
