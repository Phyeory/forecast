# Fable 5 — Independent Diagnosis of the Pump-Chart V2 Strategy Engine

## 1. Executive summary

**Verdict: UNPROVEN.** The repository available for this audit does not contain the claimed source-of-truth database, iter31/37/38/40/42 per-recording result files, or live-session ledgers, so the central empirical claim cannot be independently re-derived. More importantly, the acceptance rule is demonstrably misaligned with the stated objective: the implementation accepts only changes improving at least 50% of recordings, while a left-tail-only intervention can be valuable even when fewer than 50% of recordings contain a tail loss (`backend/analysis/paired_diff.py:1-28, 272-286`). The highest-value immediate change is therefore **not an engine parameter change**, but a user-approved, pre-registered tail-risk acceptance protocol evaluated alongside—not instead of—the existing anti-overfit PnL gate.

Two code findings also invalidate important premises of the requested audit. First, current production code defaults `v2_holder_flow_require_tag` to **1.0**, not 0.0 (`backend/strategy_engineV2.py:2933-2938`), so current HEAD is the verified-insider gate, not the “any $100 seller” gate described in the prompt. Second, `recording_ended` does **not** fill at the final close: the backtester passes the final OHLC candle to `_close_long` (`backend/backtester.py:356-364`), which interpolates through that candle and applies slippage (`backend/forward_tester.py:670-698`). That makes the force-close claim and any arithmetic based on “final close less 1%” unverified for current HEAD.

No production engine, execution, database, or parameter file was modified.

## 2. Per-rejection audit table

| Research item | Verdict | Reason and evidence |
|---|---|---|
| iter16b–16o Kramers/KDE sweeps | **UNPROVEN** | The log describes outcomes, but the corresponding full result files are absent. Current code confirms extensive changes to KDE potential and Kramers scaling (`strategy_engineV2.py:1888-1917, 1937-2082`), but not the reported outcome numbers. |
| iter22 left-tail anatomy | **UNPROVEN** | No iter22 result corpus or price database is present. The force-close execution premise differs from current code (`backtester.py:356-364`; `forward_tester.py:670-698`). |
| iter26 breadth “impossibility” | **FLAWED** | At most it bounds acceptance under the chosen per-recording breadth gate. It does not establish that tail-risk reduction is impossible. The code requires 50% improvement for full acceptance (`paired_diff.py:272-286`), structurally disadvantaging sparse-tail interventions. |
| iter30/32 pool-liquidity tests | **UNPROVEN** | Data and result artifacts needed to verify pool coverage, calibration, and outcomes are absent. Current backtester does pass `pool_sol` (`backtester.py:273-277, 318-325`), but that does not validate historic coverage. |
| iter31 microstructure | **UNPROVEN** | The claimed 49-feature tests and iter31 per-token logs are absent. Current recorder schema supports buy/sell volume (`data_store.py:47-60`), but coverage and correctness cannot be measured. |
| iter33 P_down blindness | **UNPROVEN** | The code can mathematically produce `P_down > 0.5` (`strategy_engineV2.py:2073-2082, 2148-2153, 3614-3638`). Whether it fails on actual dumps requires missing traces. The claim is not a theorem of the implementation. |
| iter34 structural angles | **UNPROVEN** | No candidate artifacts or scripts were available to verify feature definitions, replacement dynamics, or statistical outputs. |
| iter35 provenance/static token proof | **FLAWED** | Static same-token inseparability cannot rule out dynamic token-state features. The current engine already carries dynamic state (`bar_count`, entry state, peak and posterior), so the scope of the claimed impossibility is narrower than “entry selection.” |
| iter37 PSE/oracle bound | **FLAWED** | An exit-timing oracle over OHLCV cannot bound entry gating, position sizing, holder-flow information, or on-chain execution. The requested `iter37_vs_iter31.json` and `iter37_pse.json` are absent, so even the narrow arithmetic is unverified. |
| iter38/40 holder-flow and organicity gates | **UNPROVEN** | The full A/B/C result corpus is absent. Current HEAD uses tag-required gating by default (`strategy_engineV2.py:2933-2993`), contrary to the prompt’s stated default. |
| iter42 futures | **UNPROVEN** | Only code and tests are present, not the iter42 result corpus. Futures parameters are explicitly a separate preset (`strategy_engineV2.py:232-329`) and do not establish anything about the spot left tail. |

## 3. Hypothesis adjudication

### 1. P_down blindness is software miscalibration

**INSUFFICIENT EVIDENCE, with a credible calibration concern.** The probability equations are internally capable of generating a down majority: rates are positive, probabilities sum to one, and direction becomes negative when `P_down` dominates (`strategy_engineV2.py:2061-2082, 2148-2153`). Therefore `P_down ≡ 0` is not structurally hard-coded.

However, the prefactor uses curvature at `idx_t`, even though the barrier search separately returns `idx_basin` (`strategy_engineV2.py:1964-1969, 2009-2018`). If current price is not at the local minimum, `U''(x_t)` is not the Kramers basin curvature promised by the docstring. Non-positive curvature is then silently replaced by `1e-6`, which can suppress both rates and inflate `P_zero`. This is a concrete mathematical implementation defect worth instrumenting. The absent decision traces prevent quantifying its impact on dumps.

The KDE grid also scales as `sigma_t * sqrt(T_w)` (`strategy_engineV2.py:1832-1840`) while the potential buffer uses event/bar-count time in parts of the implementation; the source itself documents a unit mismatch for futures (`strategy_engineV2.py:283-300`). Spot uses one-second bars, so the mismatch may be numerically hidden rather than conceptually resolved.

### 2. Acceptance objective is mis-specified

**CONFIRMED.** Full acceptance requires positive Wilcoxon, positive bootstrap CI, and at least 50% of common recordings improved (`paired_diff.py:272-286`). That is a broad PnL-improvement objective, not a survivability objective. It can reject a treatment that materially reduces expected shortfall or maximum drawdown on a minority tail cohort.

Recommended pre-registration, subject to user approval:

1. Freeze a development cohort and untouched temporal holdout.
2. Keep the existing aggregate-PnL test as a non-inferiority constraint: candidate total expectancy must not be worse than baseline by more than a pre-declared margin (suggestion: 10% of baseline expectancy, finalized before running candidates).
3. Add primary tail endpoints: per-trade 95% expected shortfall, aggregate SOL loss among trades below -20%, and sequential-account maximum drawdown.
4. Use paired recording-level bootstrap confidence intervals for all three; accept only if the expected-shortfall and max-drawdown intervals are strictly improved on development and retain direction on holdout.
5. Report turnover, fees, worst trade, and replacement-entry effects. Do not tune thresholds on the holdout.

### 3. iter37 oracle bound is over-cited

**CONFIRMED as a scope error; arithmetic unverified.** Current code consumes holder-flow events before replay (`backtester.py:233-258`) and evaluates them as entry and exit information (`strategy_engineV2.py:2978-3015, 3405-3414`). An OHLCV exit-timing oracle cannot bound that information set. Missing iter37 artifacts prevent checking the numeric bound.

### 4. Position sizing is the actual bug

**PARTLY CONFIRMED.** Executed spot size is fixed by `buy_size_sol` and available balance (`forward_tester.py:419-446`); computed `n_star` is logged but not used for size (`forward_tester.py:69-88`). The backtest applies a constant percentage slippage independent of `pool_sol` (`forward_tester.py:283-301, 374-384, 687-698`) even though pool liquidity is passed to the engine. Thus execution cost is not liquidity- or size-sensitive.

Whether this explains the observed tail is **INSUFFICIENT EVIDENCE** because the database needed to calculate order-size/pool ratios is absent. The live trader uses Jupiter quotes and actual settlement (`live_trader.py:60-67, 119-132, 1868-1876`), so live/backtest price impact differs by construction.

### 5. recording_ended is pessimistic or biased

**CONFIRMED that it is mischaracterized; bias is INSUFFICIENT EVIDENCE.** The schema stores no completion reason (`data_store.py:35-45`), and `stop_recording` records only time/count/status (`data_store.py:129-137`). Stale recordings can be marked completed when their last candle is older than the scanner threshold (`main.py:366-380`). Therefore completion reasons cannot be stratified after the fact, and dead-tape correlation cannot be ruled out.

The force-close fill is synthetic and not the final close: it reuses the whole final OHLC candle and the ordinary delayed intrabar fill model (`backtester.py:356-364`; `forward_tester.py:687-698`). This can be above or below the close depending on candle geometry.

### 6. Lookahead or replay bug

**No direct lookahead found; parity remains UNPROVEN.** Backtest states are emitted in causal order and volume arrives only in state 4 (`backtester.py:281-325`). Signals are queued after engine update and executed on the next update (`forward_tester.py:883-942`). Live code contains a pending-signal model intended to mirror that behavior (`live_trader.py:348-359`).

Byte parity could not be tested because recordings 1019/951/878 and `price_data.db` are absent. A documentation inconsistency remains: `live_trader.py:5-12` says immediate same-candle execution while the implementation comments describe next-boundary execution (`live_trader.py:348-359`). Documentation should not be treated as evidence of runtime parity.

### 7. Entry signal is structurally bad at micro-caps

**INSUFFICIENT EVIDENCE.** The requested current-dataset mcap counterfactual cannot be re-derived. The implementation does support static market-cap bounds (`strategy_engineV2.py:2939-2944, 3392-3404`), but no replacement-aware state-machine experiment is present in the available artifacts.

### 8. Production holder-flow gate 1.0 is net-negative

**REFUTED as a statement about current defaults; outcome unknown.** Current HEAD defaults entry/exit enabled and `require_tag=1.0` (`strategy_engineV2.py:2933-2938`). Untagged and `whale` events do not qualify (`strategy_engineV2.py:2972-2993`). The prompt’s “require_tag=0.0 production default” is stale relative to code. Net effect remains unmeasurable without holder-flow data and A/B/C runs.

### 9. Breadth proofs conflate token breadth and objective

**CONFIRMED.** The paired script measures per-recording PnL breadth (`paired_diff.py:204-286`). It does not test dynamic within-token state separation. Static dual-outcome token evidence therefore cannot rule out token age, prior failed entries, drawdown-from-peak at entry, or trade ordinal.

### 10. Live losses differ from backtests

**CONFIRMED by construction; magnitude unknown.** Backtests use deterministic fixed slippage and two fixed transaction fees (`forward_tester.py:685-771`). Live uses Jupiter route quotes, transaction confirmation, authoritative token balances, unlimited sell retries, and watchdog recovery (`live_trader.py:60-67, 96-132, 1868-1876, 2268-2353`). Session ledgers are designed for `backend/data/live_logs/<session>` (`live_trader.py:280-295`) but none are present in this checkout.

### Additional hypotheses

1. **Kramers basin curvature index defect — CONFIRMED in code.** `idx_basin` is computed but curvature uses `idx_t` (`strategy_engineV2.py:1964-1969, 2009-2018`).
2. **Force-close fill semantics defect — CONFIRMED in code.** “Recording ended at close” is not what is simulated (`backtester.py:356-364`; `forward_tester.py:687-698`).
3. **Completion-cause observability defect — CONFIRMED in schema.** No stop reason is persisted (`data_store.py:35-45, 129-137`).
4. **Current holder-flow experiment identity drift — CONFIRMED.** Code comments say defaults were off and describe migration states, while executable defaults are enabled/tag-required (`strategy_engineV2.py:2912-2938`). Batch metadata must capture resolved defaults, not just user overrides.

## 4. Code-quality findings

1. **Wrong Kramers curvature location.** The function locates `idx_basin` but computes basin curvature at `idx_t`; this violates its own equation and docstring (`strategy_engineV2.py:1951-1956, 1964-1969, 2009-2018`).
2. **Silent curvature fallback.** Any non-positive current-point curvature becomes `1e-6` (`strategy_engineV2.py:2014-2022`), masking geometry failure rather than exposing a diagnostic reason.
3. **Force-close semantics do not match comments/research premise.** The final OHLC path is interpolated rather than closing at `c_last` (`backtester.py:356-364`; `forward_tester.py:687-698`).
4. **Backtest slippage ignores liquidity and size.** `pool_sol` reaches the engine but not execution pricing; percentage slippage remains constant (`backtester.py:273-325`; `forward_tester.py:283-301, 374-384, 687-698`).
5. **Kelly size is disconnected from execution.** `n_star` is captured for diagnostics (`forward_tester.py:69-88`) while spot sizing remains fixed (`forward_tester.py:419-446`).
6. **Recording completion lacks provenance.** Manual stop, task termination, stale scanner, and source failure all collapse into `status='completed'` (`data_store.py:129-137`; `main.py:253-258, 366-380`).
7. **Resolved configuration is not reliably self-describing.** Backtests save the passed `engine_params`, not necessarily every resolved adapter default (`backtester.py:370-379`). A default change can therefore alter behavior without appearing in old/new parameter metadata.
8. **Streak semantics are sub-tick, not candle.** `_no_long_streak` increments on every adapter update (`strategy_engineV2.py:3346-3356`), and the backtester calls four updates per candle (`backtester.py:292-325`). Comments alternately call the threshold “bars,” “ticks,” and “candles” (`strategy_engineV2.py:2731-2744, 3655-3657`). Threshold interpretation must be normalized.
9. **Broad exception suppression exists in schema migration and stale cleanup.** Migration catches every exception as “column already exists” (`data_store.py:81-95`), and stale cleanup silently continues on all exceptions (`main.py:366-378`). These paths can hide real database corruption or lock failures.

## 5. Prioritized fix proposals

### A. Fixes supported strongly enough to implement after approval

#### 1. Correct and instrument Kramers basin geometry

- **Defect:** curvature is evaluated at `idx_t`, not `idx_basin` (`strategy_engineV2.py:1964-1969, 2009-2018`).
- **Change:** use `d2[idx_basin]`; return diagnostic flags for missing barriers, boundary barriers, non-positive basin curvature, clamp use, grid span, bandwidth, and rate components.
- **Test:** freeze a recording cohort; first run a diagnostic-only batch proving outputs remain unchanged except logging. Then run the corrected candidate against the frozen baseline. Require improved P_down calibration on pre-labelled dump windows and no degradation under both the existing PnL gate and the new tail protocol.
- **Expected effect:** unknown; if curvature clamping drives down-blindness, potentially large. If `idx_t≈idx_basin` in practice, negligible.

#### 2. Define force-close semantics explicitly

- **Defect:** `recording_ended` uses an invented intrabar point rather than final close (`backtester.py:356-364`; `forward_tester.py:687-698`).
- **Change:** add a dedicated close-at-observed-final-price path for accounting, and separately flag the trade as non-executable/mark-to-market. Do not pretend this is a realizable fill when liquidity is unknown.
- **Test:** rerun baseline with three reports: current synthetic fill, final-close mark, and liquidity-stressed liquidation. Compare only force-closed trades and total tail expected shortfall.
- **Expected effect:** bounded to force-closed trades, but potentially material to the reported left tail.

#### 3. Persist recording stop reason and feed health

- **Defect:** completed recordings cannot be distinguished by cause (`data_store.py:35-45, 129-137`).
- **Change:** add `stop_reason`, `source_last_event_at`, and `source_error` fields; write explicit values for manual stop, stale scanner, connection failure, shutdown, and normal policy stop.
- **Test:** collect a fresh untouched cohort and compare return/liquidity distributions by stop reason before admitting it to strategy research.
- **Expected effect:** no direct PnL change; potentially large reduction in measurement bias.

#### 4. Adopt a dual PnL-and-tail acceptance protocol

- **Defect:** the 50% breadth rule cannot validate sparse-tail improvements (`paired_diff.py:272-286`).
- **Change:** pre-register the protocol in Section 3.2 while retaining PnL non-inferiority and holdout requirements.
- **Test:** apply once to frozen candidate families, without retuning thresholds after seeing results.
- **Expected effect:** no mechanical strategy gain; prevents false rejection of drawdown improvements.

### B. Cheap experiments worth running

#### 5. Full holder-flow A/B/C on one frozen cohort

- **Variants:** no gate; any-large-seller (`require_tag=0`); verified insider (`require_tag=1`). Keep event latency realistic.
- **Test:** run identical recordings and report PnL, tail expected shortfall, maximum drawdown, blocked entries, repeated exit triggers, tag coverage, and event latency. Use resolved configs in every artifact.
- **Expected effect:** potentially large if provenance is predictive; otherwise likely opportunity loss for the untagged circuit breaker.

#### 6. Liquidity-aware execution stress test before adaptive sizing

- **Change:** do not immediately wire Kelly size. First replay all fills under constant 1%, conservative reserve-curve impact, and Jupiter/live-derived empirical impact buckets by size/pool ratio.
- **Test:** compare left-tail attribution and rank stability across models. Only then test `min(fixed_cap, Kelly_fraction × equity, liquidity_cap)` with strict aggregate exposure limits.
- **Expected effect:** likely worsens estimated tail realism; adaptive sizing may then reduce live drawdown materially.

#### 7. Dynamic token-state entry study

- **Features:** trade ordinal, token age, time since prior exit, count and severity of prior failed trades, drawdown from recording peak, and liquidity trend at entry.
- **Test:** use walk-forward splits by time and rerun the engine to capture replacement entries; static masks are insufficient.
- **Expected effect:** unknown, but this hypothesis was not ruled out by static token identity analysis.

#### 8. Backtest/live reconciliation harness

- **Change:** replay exact recorded live candles and holder-flow delivery times, then match signals, intended fills, actual quote/fill, fees, retries, and ledger PnL.
- **Test:** reconcile every live session by transaction signature and classify divergence into signal timing, quote impact, confirmation delay, partial fill, or emergency exit.
- **Expected effect:** diagnostic; likely the highest-value route for explaining reported real drawdown.

### C. Mechanisms not worth re-litigating without new evidence

- Static entry masks treated as if they predicted replacement-aware engine behavior.
- Unconditional tighter hard stops optimized solely on aggregate PnL.
- Drift-work exponent restoration without a new calibration argument.
- Futures short logic used as evidence about the spot left tail.
- Any claim that a broad OHLCV exit oracle proves entry-side or external-information impossibility.

## 6. What I did not find

The following required artifacts are absent from this checkout:

- `backend/data/price_data.db` and `backend/data/backtest_data.db`;
- `backend/v2_results/` entirely;
- `backend/analysis/iter31_baseline.json`, `iter37_vs_iter31.json`, `iter37_pse.json`, and the later iteration analyses named in the request;
- `trade_report.md`;
- `logs/` and `backend/data/live_logs/` session ledgers;
- the Python virtual environment and test dependencies.

Accordingly, I could not independently verify the 427 trades, 75.6% win rate, +0.96465 SOL, 63 big losers, feature-test p-values, mcap AUC, holder-flow coverage, volume coverage, pool-size ratios, oracle arithmetic, byte parity on recordings 1019/951/878, or live-vs-replay divergence. `python -m pytest test_futures.py -q` failed because `pytest` is not installed; direct runtime imports failed because `numpy` is not installed. Static compilation of `strategy_engineV2.py`, `backtester.py`, `forward_tester.py`, and `live_trader.py` succeeded.

## 7. Explicit asks for the user

1. Provide the omitted research artifact set and a read-only snapshot of both SQLite databases, including the live-session ledger directories. Without them, the empirical conclusion remains unproven.
2. Decide whether survivable drawdown is a co-primary objective. If yes, approve a pre-registered dual protocol before any new candidate is run.
3. Approve a separate implementation pass for the basin-curvature correction, force-close accounting clarification, and recording stop-reason schema. Those production files were intentionally not changed during this diagnosis.
