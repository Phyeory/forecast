# Fable 5 — Independent Static Diagnosis

## 1. Executive summary

**Verdict: the central claim is UNPROVEN and materially overstated.** The repository supports a narrower conclusion: many specific, causal OHLCV gates and stop rules failed on the iter31 cohort under replacement-aware replay. It does **not** prove that the left tail is unfixable from current data, and it does not establish the iter37 claimed universal exit-only oracle bound.

The highest-value defect is the Kramers geometry implementation. The barrier routine says it locates a basin, but returns the current grid index as the basin without searching for a local minimum; the decision routine then evaluates basin curvature at that same current-price index. Monotone/open sides are represented by the grid boundary as a barrier despite comments claiming “open escape at attempt rate.” These choices can structurally suppress or misdirect `P_down`, so the observed `P_down ≈ 0` is evidence of the implementation’s geometry, not evidence that crashes contain no Bayesian downside signal (`backend/strategy_engineV2.py:848-901`, `1964-2022`).

Two independent protocol problems also prevent an impossibility conclusion. First, the acceptance objective is total PnL plus majority breadth, although the stated problem is catastrophic downside; the log itself proves that only 30.9% of traded tokens contain a big loser, making the 50% breadth requirement unreachable for a targeted tail fix (`RESEARCH_LOG.md:3370-3377`). Second, `paired_diff.py` pairs only recordings for which both batches wrote JSON files, while `backtester.py` writes a JSON file only when a recording traded. A gate that removes every trade from a recording therefore removes that recording from the paired sample instead of assigning candidate PnL zero (`backend/analysis/paired_diff.py:64-82,225-244`; `backend/backtester.py:395-405`). This is a material missing-not-at-random defect.

The report therefore recommends, in order: (1) freeze the present production response to unvalidated holder-flow defaults until the actual defaults and A/B/C cohort are reconciled; (2) repair and unit-test the Kramers basin/open-boundary construction; (3) replace the comparison loader with a union-of-recordings zero-fill design; and (4) pre-register a downside objective alongside, not instead of, the anti-overfit PnL objective.

## 2. Per-rejection audit

| Iteration / claim | Verdict | Audit reason |
|---|---|---|
| 16b–16j sign, Kramers, horizon, variance repairs | **SOUND as local mechanism failures; UNPROVEN as global evidence** | The log records severe mechanism engagement and adverse outcomes, but the modern barrier routine still does not implement its claimed basin/open-boundary semantics (`RESEARCH_LOG.md:43-48`; `backend/strategy_engineV2.py:848-901`). |
| 22 stop/offside family | **SOUND for the tested thresholds** | Both sides of offside 40 were run replacement-aware and lost PnL (`RESEARCH_LOG.md:3231-3257`). It establishes a local optimum, not a theorem over all exits. |
| 26 entry-feature and breadth “impossibility” | **FLAWED** | Entry-feature non-separability is credible for tested features (`RESEARCH_LOG.md:3312-3320`). The breadth proof correctly derives 30.9%, but this proves the acceptance gate is misaligned with a tail-only objective, not that tail reduction is impossible (`RESEARCH_LOG.md:3370-3377`). |
| 30/32 pool-liquidity level gates | **SOUND for reserve level/slope; UNPROVEN for event telemetry** | Reserve level is empirically a price mirror and trailing pool features did not lead entries. The log itself finds rare invariant breaks that can lead crashes, but overlap was only 24 trades/7 big losers (`RESEARCH_LOG.md:3754-3789`). |
| 31 microstructure / volcollapse | **SOUND for the implemented gate; UNPROVEN globally** | The 49-feature multiple-testing result and replacement-aware vc90 loss are credible (`RESEARCH_LOG.md:3658-3713`). “Any causal OHLCV feature” exceeds the tested family and is not proved. |
| 33a velocity exit | **SOUND for the pre-registered grid** | The rule’s premise fails at action time: 80% of fast-dipping winners were also unarmed (`RESEARCH_LOG.md:3855-3895`). |
| 33b blind-regime sizing | **UNPROVEN** | Static half-sizing of nearly all trades was negative, but executed size was never wired to `n_star`, so no replacement-aware sizing strategy was tested (`RESEARCH_LOG.md:3908-3935`). |
| 33c dual KDE / `P_down` | **FLAWED** | It correctly shows that one splice failed. It does not validate the underlying barrier algorithm, whose current-index “basin” and boundary treatment contradict the claimed physics (`backend/strategy_engineV2.py:848-901,1968-2022`). |
| 34 structural angles | **SOUND for tested static/replay rules; UNPROVEN as exhaustive** | The tested cross-token, token-memory, ordinal, reflection, floor, and arm rules were negative (`RESEARCH_LOG.md:4063-4098`). Dynamic token-state space was sampled, not exhausted. |
| 35 static provenance | **SOUND only for static token-level provenance** | Dual-outcome mints refute a perfect static mint classifier, but do not refute dynamic state features or partial risk reduction (`RESEARCH_LOG.md:4215-4234`). Current snapshots also contain acknowledged hindsight bias (`RESEARCH_LOG.md:4250-4265`). |
| 37 PSE | **SOUND for PSE; FLAWED oracle theorem** | The full PSE batch lost (`RESEARCH_LOG.md:4501-4525`). The “oracle bound” is computed from one candidate’s displaced/re-entry set and cannot upper-bound every possible exit policy (`RESEARCH_LOG.md:4552-4594`). |
| 38 holder-flow | **UNPROVEN and internally inconsistent** | The authoritative result is only 12 recordings and no paired A/B/C comparison (`RESEARCH_LOG.md:4788-4807`). The log says production `require_tag=0`, but current code defaults to `1.0`; comments also claim gate defaults OFF while assignments are ON (`backend/strategy_engineV2.py:2912-2938`). |
| 40 variance-ratio/Hurst gate | **SOUND for tested VR/Hurst families** | Point-in-time features failed corrected significance and replacement-aware configurations failed (`RESEARCH_LOG.md:5032-5102,5125-5143`). This does not imply all dynamic-state classifiers fail. |
| 42 futures convergence | **UNPROVEN as a convergence ceiling** | Forty-seven trades across four symbols and one reported converged setting cannot prove no profitable macro calibration exists (`RESEARCH_LOG.md:5227-5245`). It is separate from the spot left-tail question. |

## 3. Hypothesis adjudication

### 3.1 `P_down ≡ 0` is software miscalibration

**CONFIRMED as a credible code defect; empirical effect unverified.**

The implemented potential is $$U(x)=-T\log\rho(x)$$ (`backend/strategy_engineV2.py:1888-1917`). The barrier function’s contract says it locates the nearest basin and barriers, but it returns `x_idx` as `idx_basin` and returns `U[x_idx]` as `U_basin`; the discovered `idx_*_min` values are never used (`backend/strategy_engineV2.py:848-901`). The Kramers routine then computes `omega0_sq` from `d2[idx_t]`, not from a located local minimum (`backend/strategy_engineV2.py:1964-2022`).

For a Kramers rate, the required geometry is a local well $$x_0: U'(x_0)=0,\ U''(x_0)>0$$ and a saddle/barrier $$x_b: U'(x_b)=0,\ U''(x_b)<0.$$ At an arbitrary current point, $$U''(x_t)$$ is not a basin frequency. Clamping a nonpositive curvature to $$10^{-6}$$ turns model failure into an extremely small prefactor, directly suppressing both rates.

The open-boundary claim is also not implemented. A monotone side is assigned the edge index (`backend/strategy_engineV2.py:879-898`), after which its edge potential is treated as a finite barrier (`backend/strategy_engineV2.py:1968-2071`). An absorbing/open side should be represented explicitly, for example by a first-passage boundary rate or at minimum by a no-barrier attempt-rate branch, not by the maximum KDE vacuum at the grid edge. This is particularly important because the grid spans $$5\sigma_t\sqrt{T_w}$$ (`backend/strategy_engineV2.py:1832-1840`): with $$T_w=14{,}400$$, the edge is deliberately far into low density, making $$-\log\rho$$ large.

Finally, the implementation does not match the specification’s expected-return equation. The spec integrates drift/order flow (`strategyV2.md:193-205`), while code replaces that with barrier-distance outcomes (`backend/strategy_engineV2.py:2084-2110`). That may be a legitimate model revision, but claims that the routine “mirrors the spec exactly” are false (`backend/strategy_engineV2.py:1947-1956`).

### 3.2 Acceptance objective is mis-specified

**CONFIRMED.** The production comparison accepts on positive Wilcoxon, positive mean-PnL bootstrap interval, and at least 50% of common tokens improved (`backend/analysis/paired_diff.py:19-29,293-302`). The log proves targeted big-loss interventions can touch at most 30.9% of traded tokens (`RESEARCH_LOG.md:3370-3377`). Thus a perfect loss reduction restricted to affected tokens fails breadth by construction.

The objective should not be silently replaced. The user should pre-register a dual mandate:

1. **Non-inferiority of total expectancy:** lower 95% paired-bootstrap bound for total PnL difference above a chosen margin $$-\delta$$.
2. **Superiority of downside:** upper 95% confidence bound for candidate expected shortfall below baseline by a chosen minimum.

For loss variable $$L=-R$$ and tail level $$\alpha$$, use

$$ES_\alpha(L)=E[L\mid L\ge VaR_\alpha(L)].$$

Also compare chronological account maximum drawdown and downside deviation. Pair/bootstrap at the recording or mint-cluster level and reserve a final untouched temporal holdout.

### 3.3 Iter37 oracle bound is narrower than claimed

**CONFIRMED.** Its oracle operates on the cand-only re-entries and displaced entries generated by PSE (`RESEARCH_LOG.md:4552-4588`). Another exit policy changes the stopping times, candidate-only entries, and displaced winners. Therefore PSE’s oracle is an upper bound only over modifications to that realized PSE candidate path, not over all adapted stopping policies.

Formally, if policy $$\pi$$ changes stopping time $$\tau_\pi$$, it changes the future state and admissible-entry sequence. A bound calculated on $$\mathcal T_{PSE}$$ cannot bound $$\sup_\pi E[W_T^\pi]$$ unless all policies share the same transition and opportunity sets, which replacement-entry dynamics explicitly violate.

It also cannot cover holder-flow, which is new information outside the OHLCV filtration. The planned no-gate/big-seller/verified-insider A/B/C comparison remains undone in the cited log (`RESEARCH_LOG.md:4755-4762,4788-4807`).

### 3.4 Position sizing is the actual bug

**INSUFFICIENT EVIDENCE, but the risk-model mismatch is confirmed.** Backtests execute fixed `buy_size_sol` (default 0.1) and never consume `n_star` (`backend/backtester.py:198-205`; `backend/forward_tester.py:419-446`). `n_star` is only returned/logged (`backend/strategy_engineV2.py:2114-2185`). The backtest slippage is a fixed percentage plus a heuristic within-candle delay and has no dependence on pool depth or order size (`backend/forward_tester.py:307-384,670-753`).

The live path instead uses Jupiter quotes, actual wallet balances, partial-fill recovery, escalating slippage up to 9000 bps, and measured on-chain proceeds (`backend/live_trader.py:1868-1885,1962-2116`). This confirms that live loss severity can exceed modeled backtest loss. No static artifact read here supplies the entry-size/pool-depth distribution required to quantify impact.

### 3.5 `recording_ended` bias

**CONFIRMED as unidentifiable from current schema; direction insufficient.** Recordings store status and stop timestamp but no stop reason (`backend/data_store.py:35-45,129-136`). A recorder is completed in `finally` after any stream termination (`backend/main.py:218-258`), and inactive stale recordings are auto-completed after 60 seconds without a recent candle (`backend/main.py:344-380`). Thus manual stop, network failure, process failure, and dead-tape timeout are observationally conflated.

Force-close uses the last OHLC candle and the same fixed-slippage fill model (`backend/backtester.py:354-364`; `backend/forward_tester.py:670-759`). On an illiquid dead tape, this can be a fictional executable price. The baseline tail is therefore measured with unknown censoring and execution bias.

### 3.6 Lookahead/state replay

**Backtester causality REFUTED as the primary defect; full live parity INSUFFICIENT.** Backtest candles are expanded open → first extreme → second extreme → close, with volume delivered only at state 4 (`backend/backtester.py:281-325`). Signals are queued and executed on the next update’s open step (`backend/forward_tester.py:883-942`). KDE receives only updates with positive volume (`backend/strategy_engineV2.py:2375-2384`), which means stored full-candle volume enters at close.

However, live is not identical by construction. It launches swaps when a completed-candle signal is detected, records signal-candle prices, faces confirmation latency, and can receive holder-flow late (`backend/live_trader.py:2735-2841`; `RESEARCH_LOG.md:5155-5168`). Current code comments are internally contradictory about immediate notification versus next-candle semantics (`backend/live_trader.py:2524-2540,2747-2760`). Logical parity may be close, but economic fill parity is impossible.

### 3.7 Entry is structurally bad at micro-caps / mcap gate

**INSUFFICIENT EVIDENCE.** The static mcap report explicitly warns that blocked entries are treated as vanished and are only an optimistic upper bound (`trade_report.md:160-168`). No tested contiguous static gate was significant (`trade_report.md:256-330`). This supports rejection of simple static mcap bands, but not a replacement-aware dynamic state such as token age × drawdown-from-peak × prior-trade state. Iter34 tested several such variables statically/replay-wise, but no in-engine model of the combined token state is documented (`RESEARCH_LOG.md:4063-4098`).

### 3.8 Holder-flow gate may be net-negative

**CONFIRMED as unvalidated production risk.** The authoritative gate result is only 30 trades across 12 recordings (`RESEARCH_LOG.md:4788-4807`), while the generic large-seller behavior previously blocked thousands of bars and exhibited repeated exit signals (`RESEARCH_LOG.md:4669-4696`). Current code has a serious documentation/default conflict: comments say entry/exit defaults are OFF, assignments set them ON, comments describe `require_tag` default 1, and the assignments indeed set 1 (`backend/strategy_engineV2.py:2912-2938`). This differs from the log’s claimed gate-1.0 production default `require_tag=0` (`RESEARCH_LOG.md:4770-4776`).

The exit is last in `_check_exit_v2`, after all internal exits (`backend/strategy_engineV2.py:3694-3713`), while the live pump can execute it immediately. That is another backtest/live ordering difference when two triggers coincide.

### 3.9 Breadth proofs conflate token breadth and objective

**CONFIRMED.** The 41 dual-outcome mints refute perfect classification by immutable mint-level features, not dynamic token-state features (`RESEARCH_LOG.md:4215-4234`). It does not constrain a policy using stopping time, token age, prior failures, path state, or holder-flow. The 50% breadth rule also counts the wrong unit for a rare-tail objective.

### 3.10 Live-only losses

**CONFIRMED.** Live quotes and retries are liquidity dependent and can fail or partially fill; backtests use a fixed slip independent of depth (`backend/live_trader.py:1868-2116`; `backend/forward_tester.py:374-446,687-759`). Live holder-flow can arrive after entry; the log documents a live trade blocked only when backtest was given zero-latency future event visibility and restored by a 40-second latency shift (`RESEARCH_LOG.md:5162-5168`). Backtests therefore estimate strategy logic under idealized observability and execution, not realized live drawdown.

### Additional finding: paired samples drop zero-trade candidates

**CONFIRMED critical methodology bug.** `_files_for` only discovers files that exist, `_compare` uses set intersection, and missing candidate recordings are reported but excluded from all differences (`backend/analysis/paired_diff.py:64-82,225-244`). `_write_trade_log` is called only if `trades` is nonempty (`backend/backtester.py:395-405`). A gate that blocks all trades on a recording creates no candidate file and silently removes that observation. The correct comparison domain is the union of baseline and candidate recording IDs, with missing side represented by zero trades and zero PnL.

## 4. Code-quality findings

1. **Basin finder does not find a basin.** `idx_up_min` and `idx_down_min` are computed then discarded; `x_idx` is returned as basin (`backend/strategy_engineV2.py:848-901`).
2. **Open boundary is not open.** Monotone sides become grid-edge barriers, contrary to comments claiming attempt-rate escape (`backend/strategy_engineV2.py:879-901,1907-1911`).
3. **Curvature is evaluated at arbitrary current price.** Negative/non-well curvature is hidden by a `1e-6` clamp (`backend/strategy_engineV2.py:2009-2022`).
4. **Specification drift is undocumented in API claims.** Spec mean uses integrated drift/flow, while code uses barrier distances, despite saying it mirrors the spec exactly (`strategyV2.md:193-205`; `backend/strategy_engineV2.py:1947-1956,2084-2110`).
5. **KDE “seconds” are update counts.** Trade timestamps are `_bar_count`, which advances on each intra-candle state, so a `14400 seconds` window is 3,600 physical one-second candles under four-state expansion (`backend/strategy_engineV2.py:2294,2375-2384`). The log sometimes acknowledges four-state scaling, but the primary documentation labels these seconds.
6. **Conditional precedence is ambiguous.** `if _build_full_result and last_potential or buy_volume + sell_volume > 0` recomputes on volume regardless of `_build_full_result`; this may be intended but should be parenthesized (`backend/strategy_engineV2.py:3290-3293`).
7. **Holder-flow defaults contradict comments and research log.** Current effective defaults are ON/ON/tag-required, not the logged gate-1.0 ON/ON/no-tag (`backend/strategy_engineV2.py:2912-2938`; `RESEARCH_LOG.md:4770-4776`).
8. **Recording stop reason is absent.** Censoring cannot be audited (`backend/data_store.py:35-45,129-136`).
9. **Database migrations swallow every exception.** `except Exception: pass` treats real migration failures as “column exists” (`backend/data_store.py:81-95`).
10. **Holder-flow loss is silent under pressure.** DB insert errors are debug-only and full event queues drop events (`backend/holder_flow.py:472-492`).
11. **Paired-difference selection is missing-not-at-random.** Zero-trade candidates disappear from statistical tests (`backend/analysis/paired_diff.py:64-82,225-244`; `backend/backtester.py:395-405`).
12. **Reported median is not a median for even samples.** It selects the upper middle value (`backend/analysis/paired_diff.py:269-270`). This is minor but avoidable.
13. **No multiplicity control across sequential iterations.** Individual feature families often use Bonferroni, but repeated adaptive iteration-level searches and reuse of iter31 as both discovery and evaluation make reported p-values exploratory, not confirmatory.

## 5. Prioritized fix proposals

### A. Confident fixes

#### A1. Correct the comparison population

**Defect.** Missing zero-trade candidate files are excluded.

**Mathematical requirement.** For every recording $$i$$ in the pre-registered cohort, define

$$\Delta_i=P_i^{candidate}-P_i^{baseline},$$

with $$P_i=0$$ when a strategy makes no trade. Estimating over $$\{i:B_i\cap C_i\}$$ instead of $$\{i:B_i\cup C_i\}$$ is selection on the treatment outcome and generally biases $$E[\Delta]$$.

**Change.** Make the batch manifest authoritative, load the union of IDs, and synthesize zero-trade rows for missing logs. Better: always write one result file per recording, including zero-trade results.

**Follow-up protocol.** Re-run `paired_diff.py` only after repairing the loader, against every existing baseline/candidate pair whose gate can eliminate all trades (especially iter31_vc90, iter37_pse, iter38 A/B/C, iter40). Compare old and corrected sample counts and deltas.

**Expected effect.** Direction is candidate-specific; the important result is valid inclusion. The maximum count correction equals `only_in_baseline + only_in_candidate` currently reported but omitted.

#### A2. Add recording-end provenance and censoring analysis

**Defect.** No stop reason and no executable-liquidity observation at force-close.

**Mathematical requirement.** Recording termination is right censoring. Kaplan–Meier-style non-informative censoring requires $$T_{stop}\perp Y_{future}\mid X$$; current dead-tape/stale completion plausibly violates this.

**Change.** Persist `stop_reason` (`manual`, `stream_ended`, `stale_timeout`, `process_shutdown`, `error`) and last observed pool depth/quoteability. Mark `recording_ended` PnL as model-valued, not executable, unless a quote/depth model supports it.

**Follow-up protocol.** Stratify recording-ended trades by stop reason on a newly collected cohort. Report tail PnL under (i) last-close fixed slip, (ii) constant-product depth fill, and (iii) unliquidatable/worst-case bounds.

**Expected effect.** Unquantified on existing schema. It can move the 14 big recording-ended losses reported in `trade_report.md:134-145` in either direction.

#### A3. Align acceptance with downside without abandoning anti-overfit controls

**Defect.** The 50% breadth gate structurally rejects targeted tail improvements.

**Change.** Before any new result, pre-register two co-primary conditions: total-PnL non-inferiority and expected-shortfall/max-drawdown superiority. Keep family-wise correction and add a temporal holdout.

**Follow-up protocol.** Freeze 60% chronological development, 20% validation, 20% untouched test. Tune only on development; lock policy; require validation; report test once. Cluster-bootstrap by mint to account for repeated trades on the same token.

**Expected effect.** A reduction of the reported big-loss pool from −2.722 to −1.5 SOL would save about 1.222 SOL gross, but acceptance must account for sacrificed winners and replacement entries.

### B. High-value experiments because prior dismissal was flawed/unproven

#### B1. Repair Kramers geometry before interpreting `P_down`

**Defect.** No actual basin; no explicit open boundary; arbitrary-point curvature.

**Mathematical mechanism.** Locate a local minimum $$x_0$$ around $$x_t$$, then the first local maximum on each side. For no interior maximum before an absorbing edge, use an explicit first-passage/open-boundary model rather than edge KDE vacuum. In the overdamped regime,

$$k\approx\frac{\sqrt{U''(x_0)|U''(x_b)|}}{2\pi\gamma}e^{-\Delta U/T}.$$

This expression is valid only for a metastable well and a locally quadratic saddle with $$\Delta U/T\gg1$$. It fails on monotone landscapes, which is exactly why an open-boundary branch is needed.

**Concrete change.** Return actual basin and barrier indices plus geometry status (`well`, `open_up`, `open_down`, `flat`). Compute curvature at the located basin. Refuse to manufacture a barrier from the grid edge.

**Follow-up protocol.** First use analytic unit potentials without market data: symmetric double well, tilted well, monotone up/down, flat potential. Verify rate ordering and limiting behavior. Then mechanism-trace the fixed engine on the pre-registered iter33 worst trades; only if `P_down` responds causally run a full batch with paired union-zero-fill analysis.

**Expected effect.** If the current curvature floor is binding, rates can increase by the ratio $$\sqrt{U''(x_0)/10^{-6}}$$. Magnitude on real trades is unverified-by-execution.

#### B2. Wire risk sizing to executable liquidity, not latent `L_t`

**Defect.** Fixed size and fixed slip ignore pool impact; latent `L_t=e^\ell` is not SOL depth.

**Mathematical mechanism.** For a constant-product pool with SOL reserve $$R$$ and order $$q$$, price impact is nonlinear and approximately $$q/R$$ only for small $$q$$. Constrain order size by

$$q\le \eta R$$

and by fractional Kelly

$$q=W\,c\,\max\left(0,\frac{\hat\mu-C(q,R)}{\hat\sigma^2}\right),\quad 0<c<1,$$

where $$C$$ includes round-trip AMM impact, fees, and latency.

**Follow-up protocol.** Use only recordings with real `pool_sol`. Compare fixed size, pool-cap only, and fractional-Kelly-plus-pool-cap under a constant-product fill model. Pre-register tail ES and PnL non-inferiority. Do not extrapolate from the sparse 10-overlap iter32 cohort.

**Expected effect.** When $$q/R$$ is large, modeled losses should become worse but size caps should reduce SOL tail roughly proportional to reduced notional; percentage return is not expected to improve.

#### B3. Complete holder-flow A/B/C with latency

**Defect.** Generic big-seller and verified-insider signals are conflated; current defaults conflict with the log; zero-latency backtest sees events earlier than live.

**Mechanism.** Compare filtrations: no event, all large sells, verified insider sells. Shift each event by observed polling/delivery delay $$D$$ and use only events with $$t_{event}+D\le t_{decision}$$.

**Follow-up protocol.** On a fresh pre-specified cohort with adequate tagged-event counts, run A/B/C through full backtester semantics, union-zero-fill paired analysis, and latency sensitivity at empirical p50/p90 delivery delays. Report entry blocks, unique exits, repeated signals, opportunity loss, tail ES, and total PnL.

**Expected effect.** The only authoritative existing magnitude is +0.1736 SOL on 12 recordings for gate 1.0 without a paired comparator (`RESEARCH_LOG.md:4788-4807`); no defensible population expectation exists.

#### B4. Dynamic token-state model

**Defect.** Static mint proofs are over-cited against dynamic state.

**Mechanism.** Define a causal state vector at entry containing token age, drawdown from prior peak, prior engine entries/outcomes, time since last crash, and holder-flow. Use shrinkage or a small Bayesian survival/hazard model, not a large threshold sweep.

**Follow-up protocol.** Train on chronological development only, calibrate probabilities on validation, then evaluate one locked policy on untouched test with mint clustering and replacement-aware engine replay.

**Failure condition.** Reject if calibration fails, test ES does not improve, or total PnL breaches non-inferiority.

### C. Mechanisms correctly killed

1. Re-tuning `kelly_flat` offside immediately around 40: both 35 and 45 were inferior (`RESEARCH_LOG.md:3231-3257`).
2. The specific PSE rule: full replacement-aware batch was materially worse (`RESEARCH_LOG.md:4501-4525`).
3. Simple static mcap bands: non-significant across several historical baselines and explicitly subject to replacement bias (`trade_report.md:160-168,290-330`).
4. The tested VR/Hurst “organic regime” gates: broad static and in-engine sweeps did not produce stable positive paired results (`RESEARCH_LOG.md:5032-5102,5125-5143`).
5. Reserve-level pool drawdown as a substitute for price: the reserve is mostly a CPMM price mirror (`RESEARCH_LOG.md:3754-3789`).

## 6. What I did not find or could not verify

- The prompt-named `backend/analysis/iter31_baseline.json`, `iter37_vs_iter31.json`, `iter37_pse.json`, and matching `backend/v2_results` files were not present in the repository paths exposed to this audit. Their numbers were therefore cross-checked only against `trade_report.md` and `RESEARCH_LOG.md`, not the raw artifacts.
- No `logs/` artifacts were present at the requested path. Live-only conclusions rely on source code and the research log’s quoted parity records.
- The SQLite database is binary and the mandate prohibited DB queries or executable inspection. I could not verify current candle-volume coverage, holder-flow tag counts, stop populations, or size/pool ratios directly. The 89.7% buy-volume coverage remains a log claim (`RESEARCH_LOG.md:3661-3668`).
- I did not execute tests, scripts, backtests, database queries, or numerical spot checks, per mandate. All proposed verification commands/protocols are follow-up work.
- I could not validate stochastic determinism from static code alone. Stored parity artifacts cited by the log were absent.
- The repository changed after several log entries. In particular, holder-flow effective defaults in current code do not match the final iter38 prose.

Artifacts that would close the gaps: restore the complete iter31/37/38/40 result directories, include batch manifests listing zero-trade recordings, persist live `trades.jsonl`, add recording stop reasons, and export a static data-quality summary containing nonzero buy/sell-volume coverage, holder tags, event latency, and order-size/pool-depth ratios.

## 7. Explicit asks for the user

1. Decide and pre-register whether the primary goal is total PnL maximization or survivable downside with PnL non-inferiority. The current protocol cannot accept a purely targeted tail improvement.
2. Decide which holder-flow policy production is intended to run: no gate, generic large-seller gate, or verified-insider gate. Current code and the research log disagree.
3. Authorize a follow-up executable phase only after the Kramers and paired-sample defects are fixed. The first runs should be analytic unit tests and corrected re-analysis, not another broad parameter sweep.
