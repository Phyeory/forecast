# Fable 5 — Independent Static Diagnosis

## 1. Executive summary

**Verdict: FLAWED.** The narrower conclusion—many tested OHLCV-only entry and exit rules failed on the iter31 cohort—is supported by the research log, but the stronger conclusion that the current left tail is “unfixable from current data” does not follow. The acceptance test is mathematically incapable of accepting a tail-only improvement on the reported cohort, selectively excludes recordings on which a candidate suppresses every trade, and optimizes mean PnL rather than the drawdown problem posed by the user (`RESEARCH_LOG.md:3370-3391`; `backend/analysis/paired_diff.py:225-230,276-301`). The iter37 “oracle bound” is not a bound on all exit rules; it is an attribution of one PSE candidate and its candidate-only re-entries (`RESEARCH_LOG.md:4552-4594`). The Kramers downside channel is also structurally miscalibrated: it treats the current grid point as the basin, substitutes local grid boundaries for absent barriers, and then uses curvature at the current point rather than at a located minimum (`backend/strategy_engineV2.py:848-901,1964-2022`). My committed fix is therefore **a pre-registered downside-risk acceptance protocol, evaluated on the union of baseline and candidate recordings with zero-trade outcomes represented as zero PnL**, while retaining mean-PnL non-inferiority. This is the minimum valid change that permits the project to select a mechanism that actually reduces the −2.72 SOL left tail instead of structurally rejecting it.

This was a static audit. I executed no program, backtest, database query, or analysis script. Several artifacts named by the research log are absent from this checkout: the available `backend/analysis/` inventory ends at iter12 JSON outputs, and no `backend/v2_results/` or live-log artifacts are present. Consequently, numerical claims from later iterations are verified only as claims recorded in `RESEARCH_LOG.md`, not independently against their advertised result files.

## 2. Per-rejection audit

| Iteration / claim | Verdict | Reason and evidence |
|---|---|---|
| 16b–16o decision/KDE sweep | **SOUND narrowly; UNPROVEN broadly** | The log records genuine formula defects and targeted corrections: synthetic-liquidity domination, missing grid-spacing in the second derivative, wrong use of the posterior mean square in the variance, and too-short KDE memory (`RESEARCH_LOG.md:2547-2581`). The surviving code contains those corrections (`backend/strategy_engineV2.py:904-921,1888-1917,2093-2110`). But the failed boundary experiment was reverted, and the production barrier finder still maps an absent side barrier to the grid boundary (`backend/strategy_engineV2.py:879-901`); it does not implement the advertised “open escape at attempt rate.” Thus those sweeps do not validate the present P_down calibration.
| Iter22 left-tail anatomy and stop sweeps | **SOUND for tested rules** | The log reports exact candidate runs on both sides of the kelly-flat threshold and negative static sweeps (`RESEARCH_LOG.md:3208-3258`). This supports a local optimum for that rule family, not an all-exit ceiling.
| Iter26 breadth impossibility | **FLAWED as an acceptance theorem; SOUND arithmetic** | If only 46 of 149 traded tokens can change, breadth cannot exceed 30.9%; the arithmetic is correct (`RESEARCH_LOG.md:3370-3388`). That proves the acceptance rule is infeasible for a tail-only intervention—not that the intervention lacks risk value. It exposes objective misspecification.
| Iter30 CPMM pool level | **SOUND narrowly** | The CPMM relation makes reserve level largely price-derived (`RESEARCH_LOG.md:3592-3604`). It does not rule out independent liquidity events, execution impact, or holder flow.
| Iter31 microstructure gate | **SOUND for `v2_volcollapse_max=0.90`; UNPROVEN for the whole feature class** | The replacement-aware run was negative (`RESEARCH_LOG.md:3694-3713`). The conclusion that every causal microstructure mechanism is impossible relies on marginal univariate tests and one in-engine threshold; it is broader than the experiment.
| Iter32 pool-liquidity signal | **UNPROVEN** | Only 10 of 159 traded recordings carried pool data and only 24 overlap trades were replayed (`RESEARCH_LOG.md:3754-3759,3792-3806`). This is useful negative evidence, not a conclusive population result.
| Iter33a velocity/unarmed exit | **SOUND** | Its causal premise failed: 80% of fast-dipping winners were still unarmed at the intervention time (`RESEARCH_LOG.md:3855-3898`).
| Iter33b blind-regime adaptive sizing | **SOUND for the proposed discriminator; UNPROVEN for risk sizing** | P_down blindness was nearly universal and therefore not outcome-selective (`RESEARCH_LOG.md:3916-3935`). That rejects P_down-conditioned halving, not account-level drawdown-constrained sizing.
| Iter33c dual KDE | **SOUND for that splice; UNPROVEN as a Kramers verdict** | The fast-KDE splice engaged but did not produce P_down ≥ 0.5 (`RESEARCH_LOG.md:3953-3977`). The production barrier algorithm remains geometrically suspect, so failure of this splice cannot establish that downside passage probability is market-impossible.
| Iter34 structural angles | **SOUND for the listed static rules; UNPROVEN broadly** | Cross-token breadth, prior crash, ordinal, reflection, and structural floors were negative in recorded counterfactuals (`RESEARCH_LOG.md:4063-4098`). Static replay does not model every dynamic token-state policy, as the log itself acknowledges elsewhere through replacement-entry effects.
| Iter35 provenance | **SOUND for static provenance; FLAWED as a dynamic-state impossibility proof** | Dual-outcome mints prove static mint features cannot perfectly separate all trades (`RESEARCH_LOG.md:4215-4234`). They do not rule out time-varying holder flow, token age/state, concentration at entry, or post-entry events; current snapshots were explicitly hindsight-biased (`RESEARCH_LOG.md:4250-4265`).
| Iter37 PSE | **SOUND rejection of PSE; FLAWED oracle theorem** | The full candidate lost (`RESEARCH_LOG.md:4499-4525`). But the “oracle” is constructed from PSE’s changed exits and candidate-only re-entries, and the four displaced winners are treated as unavoidable (`RESEARCH_LOG.md:4552-4588`). This does not upper-bound a different exit rule with different intervention times and state trajectories.
| Iter38 holder-flow | **UNPROVEN and incomplete** | The first manual replay omitted force-closes and was corrected (`RESEARCH_LOG.md:4635-4649`). The authoritative result covered only 12 recordings and no completed A/B/C comparison is recorded (`RESEARCH_LOG.md:4788-4807`). Current code defaults also conflict with the log: entry/exit are ON but `require_tag` defaults to 1, not the documented gate-1.0 value 0 (`backend/strategy_engineV2.py:2933-2938`).
| Iter40 VR/Hurst | **SOUND for the tested gate surface** | No feature survived multiplicity correction; the tuned in-engine configurations remained negative (`RESEARCH_LOG.md:5032-5070,5125-5143`). This rejects that organicity gate, not all dynamic state features.
| Iter42 futures convergence | **UNPROVEN as a ceiling and irrelevant to the spot tail** | Forty-seven trades across four symbols and one converged configuration cannot bound “any non-memecoin timeframe” (`RESEARCH_LOG.md:5227-5255`). It has no evidentiary force on spot dead-coin losses.

## 3. Hypothesis adjudication

1. **P_down ≡ 0 is software miscalibration — CONFIRMED.** The potential is $$U=-T\log\rho$$ (`backend/strategy_engineV2.py:1888-1917`). The barrier kernel starts at the current-price index, labels that point the “basin,” and substitutes the boundary whenever no turning peak is found (`backend/strategy_engineV2.py:848-901`). Kramers then uses $$\omega_0^2=U''(x_t)$$ rather than curvature at a separately located local minimum (`backend/strategy_engineV2.py:1964-2022`). On a monotone thin-density downside, the boundary has very low density and therefore high $$U$$, so $$\Delta U_-/T$$ is large and $$k_-\propto e^{-\Delta U_-/T}$$ collapses (`backend/strategy_engineV2.py:2061-2082`). This is exactly the logged blindness (`RESEARCH_LOG.md:2685-2690`). It is an algorithmic consequence, not evidence of market truth.

2. **Acceptance objective is mis-specified — CONFIRMED.** The code requires one-sided Wilcoxon significance, a positive bootstrap mean-PnL CI, and at least 50% common-token breadth (`backend/analysis/paired_diff.py:293-301`). The log proves only 30.9% of tokens carry a big loss (`RESEARCH_LOG.md:3370-3388`). A tail-only fix therefore fails by construction even if it materially reduces drawdown.

3. **Iter37 oracle is narrower than claimed — CONFIRMED.** It bounds one PSE trajectory decomposition, not the set of all exit policies (`RESEARCH_LOG.md:4552-4594`). It also expressly excludes new information, while holder-flow data is now ingested by the backtester (`backend/backtester.py:233-250`).

4. **Position sizing is an actual defect — CONFIRMED.** The engine computes $$n^*$$ and caps it by liquidity (`backend/strategy_engineV2.py:2084-2129`), but ForwardTester executes `buy_size_sol` directly (`backend/forward_tester.py:419-446`). The fixed-size model is therefore inconsistent with the engine’s claimed Kelly-optimal contract. Whether wiring n* improves this cohort remains UNVERIFIED-BY-EXECUTION.

5. **`recording_ended` is pessimistic or biased — INSUFFICIENT EVIDENCE.** Force-close inclusion is correct for complete decision streams (`backend/backtester.py:356-364`), but the code calls the ordinary timed intra-bar fill over the final candle rather than filling explicitly at the final close (`backend/forward_tester.py:670-697`). Recorder end-reason population and executable liquidity could not be checked from absent DB artifacts.

6. **Lookahead/state replay bug — PARTLY REFUTED, parity claim still INSUFFICIENT.** Backtest states are causal in order and volume arrives only at state 4 (`backend/backtester.py:267-325`); pending execution is documented for the next state-1 boundary. However, live parity was historically broken in five ways, including delayed holder flow and deferred trade notification (`RESEARCH_LOG.md:4812-4887`). The log later reports repairs, but no parity artifacts are present here for independent verification.

7. **Entry is structurally bad at micro-caps — INSUFFICIENT EVIDENCE.** The engine is demonstrably a bounce catcher and iter40 reports median entry VR below one (`RESEARCH_LOG.md:5098-5103`). The same regime contains profitable entries, so this alone does not prove a bad entry. Static mcap effects do not establish a replacement-aware mechanism.

8. **Production holder-flow can be net-negative — CONFIRMED as an unresolved production risk.** The only corrected positive result covers 12 recordings (`RESEARCH_LOG.md:4788-4807`). A/B/C was planned but not completed in the log. Current code has contradictory comments/defaults and actually defaults to verified tags (`backend/strategy_engineV2.py:2912-2938`), while the repository guidance says gate 1.0. Production behavior is therefore configuration-ambiguous.

9. **Breadth proofs conflate token breadth with objective — CONFIRMED.** Paired comparison uses only the intersection of traded recordings (`backend/analysis/paired_diff.py:225-230`), so a candidate that correctly suppresses every trade on a losing recording is omitted rather than assigned candidate PnL zero. Static provenance’s dual-outcome result also does not rule out dynamic token state (`RESEARCH_LOG.md:4215-4234`).

10. **Live losses differ by construction — CONFIRMED.** Live defaults are 0.01 SOL and 1500 bps slippage (`backend/live_trader.py:252-304`), whereas batch backtests default to 0.1 SOL and 1% slippage (`backend/backtester.py:88-93`). Live uses real Jupiter execution and asynchronous confirmation (`backend/live_trader.py:96-132,340-359`). The log itself reports matched logic but differing PnL from slippage and sizing (`RESEARCH_LOG.md:5155-5168`). Backtest profitability therefore does not establish live profitability.

### Additional failure hypotheses

- **Common-set selection bias — CONFIRMED.** `paired_diff.py` computes all tests and breadth only on `set(base) & set(candidate)` (`backend/analysis/paired_diff.py:225-280`). This directly biases evaluation against selective entry blockers.
- **Kelly derivation is internally inconsistent — CONFIRMED.** The optimizer uses `abs(mu_hat_tau)` in the numerator (`backend/strategy_engineV2.py:2114-2125`) but later evaluates signed utility using `direction * n_star * mu_hat_tau` (`backend/strategy_engineV2.py:2168-2178`). Direction is chosen from probability ordering rather than the sign of expected payoff (`backend/strategy_engineV2.py:2131-2153`). Therefore the displayed $$n^*$$ is not generally the argmax of the displayed signed objective.
- **Holder-flow default drift — CONFIRMED.** Comments say OFF and log says gate 1.0/untagged, while executable defaults are ON and tag-required (`backend/strategy_engineV2.py:2912-2938`).
- **Final-candle execution semantics are mislabeled — CONFIRMED.** “Force-close on final close” is not what the code does: it reuses delayed intrabar interpolation and slippage (`backend/backtester.py:356-364`; `backend/forward_tester.py:687-697`).

## 4. Code-quality findings

1. **Barrier geometry does not locate a basin.** `_barrier_find_kernel` returns `x_idx` as the basin without descending to a local minimum (`backend/strategy_engineV2.py:848-901`).
2. **Absent barriers become maximum-distance barriers.** Monotone sides terminate at a grid boundary, contrary to the “open escape” interpretation documented in comments (`backend/strategy_engineV2.py:879-901,1903-1911`).
3. **Kramers curvature is sampled at current price, not a minimum.** Negative curvature is replaced by a numerical constant, potentially silencing or arbitrarily scaling rates (`backend/strategy_engineV2.py:2009-2022`).
4. **Kelly sizing is dead with respect to execution.** $$n^*$$ is logged, but fixed `buy_size_sol` determines exposure (`backend/strategy_engineV2.py:2124-2129`; `backend/forward_tester.py:419-446`).
5. **Kelly optimization and utility disagree.** Absolute expected return sizes both directions, but signed expected return scores utility (`backend/strategy_engineV2.py:2118-2125,2168-2178`).
6. **Selective candidates are removed from inference.** Baseline-only/candidate-only recordings are counted but excluded from tests (`backend/analysis/paired_diff.py:225-230,238-280`).
7. **Holder-flow defaults and comments disagree.** This is operationally dangerous because an omitted kwarg changes whether generic whales or verified insiders can affect trades (`backend/strategy_engineV2.py:2912-2938`).
8. **Holder-flow has silent-loss paths.** DB insertion failures are debug-only and queue overflow silently drops events (`backend/holder_flow.py:472-492`).
9. **Backtest and live execution models differ materially.** Different default size/slippage and simulated versus real routing make PnL non-parity expected (`backend/backtester.py:88-93`; `backend/live_trader.py:252-304`).
10. **Later research artifacts are not present in this checkout.** Therefore byte-identity, per-trade, and paired-result claims after iter12 remain UNVERIFIED-BY-EXECUTION/ARTIFACT in this audit.

## 5. The fix

### 5.1 Defect addressed

The project asks to reduce catastrophic drawdown but accepts changes only when mean per-recording PnL improves on common traded recordings and at least half those recordings improve (`backend/analysis/paired_diff.py:225-301`). Since big losses occur on only 30.9% of traded tokens, no tail-only rule can satisfy breadth (`RESEARCH_LOG.md:3370-3388`). The current protocol also drops baseline-only recordings, precisely where a successful entry gate may suppress a losing trade.

### 5.2 Mathematical argument

Let $$X_i^B$$ and $$X_i^C$$ be baseline and candidate PnL on every recording in the **union** cohort $$\mathcal U$$. Define $$X_i=0$$ when a strategy makes no trade. Let $$\Delta_i=X_i^C-X_i^B$$. Define loss $$L_i=-X_i$$ and lower-tail expected shortfall at pre-registered level $$\alpha=0.10$$:

$$\operatorname{ES}_{0.10}(L)=\mathbb E[L\mid L\ge \operatorname{VaR}_{0.90}(L)].$$

The user’s loss objective is

$$H_R:\operatorname{ES}_{0.10}(L^C)<\operatorname{ES}_{0.10}(L^B),$$

subject to mean-PnL non-inferiority

$$H_N:\mathbb E[\Delta]\ge -\varepsilon,$$

with pre-registered $$\varepsilon=0.00025\ \text{SOL per recording}$$ (one quarter of one percent of the 0.1 SOL stake).

**Proposition.** A rule that changes only a subset $$S\subset\mathcal U$$ with $$|S|/|\mathcal U|<1/2$$ can be accepted by this criterion if and only if its lower-tail reduction is statistically positive and its mean cost is bounded by $$\varepsilon$$; it is not rejected merely because $$|S|<|\mathcal U|/2$$.

**Proof.** For $$i\notin S$$, $$\Delta_i=0$$. The current breadth rule rejects whenever $$|S|/|\mathcal U|<1/2$$ regardless of the values of $$\Delta_i$$. Under the proposed criterion, zeros outside $$S$$ enter both the paired bootstrap and ES order statistics. If losses in the worst decile are reduced by amounts $$a_i\ge0$$, then, absent rank crossings,

$$\operatorname{ES}_{0.10}(L^B)-\operatorname{ES}_{0.10}(L^C)=\frac1m\sum_{i\in T}a_i,$$

where $$T$$ is the baseline worst-decile set and $$m=|T|$$. With rank crossings, recomputing candidate ES is conservative because a formerly non-tail candidate can enter the tail. Mean cost is independently bounded by the non-inferiority condition. Hence acceptance corresponds to the stated drawdown objective while preserving an anti-overfit economic guard. ∎

This proof establishes validity of the criterion, not profitability of any candidate. Statistical generalization still requires blocked, time-ordered validation.

### 5.3 Concrete change

Change only the analysis/acceptance layer in the follow-up:

1. Modify `backend/analysis/paired_diff.py::_compare` so inference uses $$\mathcal U=\text{baseline IDs}\cup\text{candidate IDs}$$, assigning a zero-trade metric with PnL zero to a missing side instead of discarding it. The defect is at `backend/analysis/paired_diff.py:225-230`.
2. Add paired recording-level metrics: ES at 10%, worst-decile total PnL, maximum sequential drawdown using recordings ordered by recording start time, and loss-side semideviation.
3. Replace the 50% breadth requirement for a **pre-declared tail-risk experiment** with two co-primary gates: (a) one-sided paired block-bootstrap 95% CI for ES improvement strictly above zero; (b) mean-PnL non-inferiority lower CI above $$-0.00025$$ SOL/recording. Keep a descriptive breadth field, but do not make it a feasibility condition.
4. Freeze these gates before selecting the next candidate. Do not retroactively re-score the historical sweep and cherry-pick a winner.

### 5.4 Falsifiable follow-up protocol

1. Freeze the canonical baseline batch and one candidate before inspecting validation outcomes.
2. Split recordings chronologically: earliest 60% development, next 20% validation, final 20% untouched test. Preserve every recording, including zero-trade recordings.
3. Use `run_iteration.py` for baseline and candidate with identical data, size, slippage, and holder-flow latency. For holder flow, pre-register the candidate as gate 1.0 or gate 2.0; do not compare both on the test split.
4. Extend `paired_diff.py` as above and run 10,000 **moving-block** bootstrap resamples over chronologically adjacent recordings (block length pre-registered as 20 recordings) to preserve session dependence.
5. Accept only if, on the untouched test split: $$CI_{95\%,lower}(ES_B-ES_C)>0$$, $$CI_{95\%,lower}(\mathbb E[\Delta])>-0.00025$$, candidate maximum drawdown is lower, and no execution-error rate increases. Report conventional mean-PnL Wilcoxon and token breadth descriptively.
6. Independently report real-live fill deltas; do not mix simulated 1% fills with Jupiter 15% tolerance.

### 5.5 Expected effect magnitude

Using the canonical values recorded in the log, 63 big losers contribute $$-2.7224$$ SOL (`RESEARCH_LOG.md:3670-3676`). If a candidate saves a fraction $$r$$ of those losses while incurring winner cost $$C$$, then

$$\Delta\operatorname{PnL}=2.7224r-C.$$

For the concrete target $$r=0.25$$ and the non-inferiority budget over 652 recordings $$C\le 652(0.00025)=0.163$$ SOL,

$$\Delta\operatorname{PnL}\ge 2.7224(0.25)-0.163=0.5176\ \text{SOL}.$$

The follow-up must therefore demonstrate at least about **0.68 SOL gross tail savings** to meet the 25% target, and no more than **0.163 SOL** aggregate mean cost. These are derived targets, not asserted outcomes.

## 6. What the fix depends on

- Recording order must reflect real time; otherwise maximum drawdown and block bootstrap are invalid.
- Missing result files must mean “zero trades,” not execution failure. Failed recordings must be classified separately and fail the run if asymmetric.
- Baseline and candidate must share identical execution assumptions and data availability.
- ES level, block length, non-inferiority margin, candidate mechanism, and data split must be frozen before the untouched test is inspected.
- The follow-up must restore the advertised iter31/iter37/iter38 artifacts or regenerate them under a new immutable batch ID. In this checkout, those artifacts could not be independently read.
- The Kramers geometry and Kelly sizing defects remain separate engineering issues. They should not be changed in the same confirmatory run, because doing so would make attribution impossible.

## 7. Explicit ask

Approve the pre-registered tail-risk objective before any further strategy experiment, and restore or regenerate the missing iter31/37/38 result artifacts for independent verification. Then nominate exactly one holder-flow configuration for the untouched test split; the report recommends verified-insider gate 2.0 if tag coverage is adequate, otherwise the test should stop rather than silently fall back to generic whale flow.

## Decision

**Adopt the union-cohort expected-shortfall/non-inferiority acceptance protocol now.** The existing research supports rejection of many individual mechanisms, but its headline impossibility claim is not established, and its current statistical gate cannot answer the drawdown question it was asked to answer.
