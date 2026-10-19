# TASK: Full recalibration system for the market (stock) stack — calibrator
#        engineering at the same rigor as the memecoin campaign, then re-prove
#        prop-firm economics end to end.

Worktree: /Users/jaime/pump-chart-market (branch `market-backtest-systems`).
Read, in order: AGENTS.md (repo root), backend/market_backtest/MARKET_RESEARCH_LOG.md
(canonical campaign record), backend/market_backtest/PROP_FIRMS.md, GO_LIVE.md,
RESULTS.md, and the memecoin RESEARCH_LOG.md sections on calibration + acceptance
gates (git show 3a33e47:AGENTS.md has the old condensed log; the acceptance-gate
philosophy is in "Benchmarking").

## Mission in one line
Build an auto-recalibrator for the US-equity V2 forward test whose parameters are
FIRST-CLASS (they fundamentally shape strategy behaviour — arm thresholds, τ
program, confidence floors, sizing k — not cosmetics), sweep/tune THE CALIBRATOR
ITSELF for correct sensitivity using the full battery (backtests + Monte Carlo +
hypothesis testing), and then re-run the prop-firm proofs (FTMO MC + deterministic
pass + economics) on the recalibrated stack to confirm it still makes thousands
per month and passes evaluations for real payouts.

## Hard invariants (violating any = failed task)
1. **Memecoin stack untouched.** ZERO changes to backend/strategy_engine*.py,
   backtester.py, forward_tester.py, engine_factory.py, live_trader.py, or any
   file the memecoin stack imports. shasum these before/after and compare with
   /Users/jaime/pump-chart (main repo); byte-identical is the gate. All new code in
   backend/market_backtest/recalibrate_market.py + analysis/ + tests.
2. **Frozen-params default unchanged.** Recalibration is a separate offline
   system; the live trader only ever picks up a calibration snapshot at session
   start, and only one that PASSED the gate. Never auto-adopt mid-session, never
   silently re-fit on the live path (see the Luna 11-vs-8 trap in AGENTS.md).
3. **Determinism**: same inputs → same params. Seed everything; no wall-clock in
   math; batch-style cell runs so results are reproducible.
4. **Acceptance gates, not vibes**: every adoption/claims requires paired ΔPnL
   Wilcoxon p<0.05 + 10k-bootstrap 95% CI > 0 + ≥50% breadth — the campaign's
   standard gate. Tail statistics need tail-appropriate tests.
5. **Re-baseline before comparing** (campaign trap): results measured on older
   stacks don't compare against new ones. Every headline number in your final
   report must come from runs on YOUR stack.

## Phase A — understand what calibration means here
- Read how the memecoin trader calibrates (calibrate_market_params; what params:
  adaptive measurement variance anchors, OU drift/vol anchors, KDE bandwidth /
  potential U layers, entropy confidence, τ/VR-horizon program) and how those
  params CHANGE BEHAVIOR (not just execution details).
- Read how the stock campaign froze params per symbol (frozen_params store,
  arm14 = gain_retrace_arm_pct 14.0, τ program, k=0.75). Enumerate the complete
  calibratable surface for stocks: every param that would change the algorithm's
  behaviour fundamentally (entry/exit thresholds, regime topology mapping,
  confidence floors, barrier placement, τ scale, per-symbol vs pooled layers).
- Decide and document (in a new section of MARKET_RESEARCH_LOG.md): which layers
  recalibrate per-symbol, which pool across the 67, and which stay frozen forever
  (e.g. anything the MC dossed as structural).

## Phase B — build the calibrator (engine-grade, not a wrapper script)
- backend/market_backtest/recalibrate_market.py:
  - Per symbol / pooled layers as documented in Phase A.
  - Trailing-window calibration (default 252d), most-recent 20% excluded as
    holdout; calibration NEVER sees the holdout.
  - Search over the calibratable surface with the same discipline the memecoin
    campaign used for its sweeps (explicit param grids in cell runs, all knobs
    set explicitly — the "sweep cells pass only --params" trap).
  - Incumbent params as the baseline arm in every comparison.
  - Versioned snapshot output (never overwrite; candidates land in a new dir
    with a manifest: window, seed, git commit, gate stats).

## Phase C — sweep and tune THE CALIBRATOR ITSELF (this is the core deliverable)
The calibrator is an algorithm too; its knobs (window length, holdout fraction,
search breadth, gate thresholds, per-symbol vs pooled choice, refresh cadence)
need their own evidence:
1. **Retrospective replay of the calibrator on history**: at multiple past dates
   (e.g. monthly from 2024-01 to 2025-06), run the calibrator on data up to that
   date, then BACKTEST the adopted params on the following out-of-sample window.
   This measures: does adaptive recalibration beat the static campaign params
   out of sample, and at what cadence/sensitivity?
2. **Sensitivity sweep of calibrator knobs**: window ∈ {126, 189, 252, 378}d,
   holdout ∈ {20%, 33%}, cadence ∈ {monthly, quarterly, yearly}, gate thresholds
   ∈ {p<0.05, p<0.01}, per-symbol vs pooled for each layer. For each cell, run
   the full backtest battery over the 67-tape universe (BACKTEST_RESULTS_DIR=
   backend/v2_results convention, batch runner, 8 workers; ~60–70 min full-DB
   cell budget — plan cells accordingly, use --recording-ids-file subsets for
   cheap screening then confirm winners full-DB + shasum the engine file around
   long burns).
3. **Hypothesis testing on every headline claim**: paired Wilcoxon + bootstrap CI
   + breadth (backend/analysis/paired_diff.py, aggregate_results.py). Also test
   the null that recalibration is no better than frozen (equivalence-style
   reporting, not just "we found a winner in 1 of 12 cells" — apply the
   multiple-comparisons discipline: cells are a search; winners must survive on
   untouched holdout data + holdout-confirm like the campaign's iter90e gate).
4. **Monte Carlo**: re-run backend/market_backtest/mc_firms.py (and its
   extended variants) with recalibrated params, 10k paths, over ALL evaluation
   periods and account sizes the campaign used ($25k/$50k/$100k/$200k; FTMO
   2-Step + free trial + 1-step comparisons where the campaign has them),
   INCLUDING sequence/efficiency stressors (drawdown placement, fee escalation,
   slipped entries — whatever mc_firms implements). Report full-pass
   probability, median profit/attempt, EV after fees, ensuring-cost curves at
   each k in {0.5, 0.75, 1.0}.

## Phase D — re-prove prop-firm economics on the recalibrated stack
- Deterministic pass proof: replicate the campaign's deterministic single-run
  proof methodology (day-certain pass; the original was day 442) for the
  recalibrated parameters — the date will differ; what must hold is "a full
  2-Step pass exists on the replayed equity with the guard enforced live".
- Economics vs campaign baseline: pass rates, EV/attempt, months-to-first-payout
  (payout cadence every 14d — verified facts in PROP_FIRMS.md), Σ run stats.
- If recalibrated params are WORSE for prop economics (real risk: calibration
  optimized for raw PnL may worsen drawdown shape), say so plainly and keep the
  incumbent — "no change" is an acceptable, honest outcome IF backed by the
  battery. The gate exists precisely so mediocre recalibrations die.

## Phase E — integration + tests + docs
- `daily_alpaca.sh` gains a WEEKLY section (guarded to not run before the live
  tape has ≥4 weeks recorded; the cron calls it Sunday) that computes candidates
  + gate report; adoption stays manual until you recommend otherwise explicitly.
- Live integration mirror of memecoin: `recalibration` / `recalibration_rejected`
  journal events (same shape as memecoin's), snapshot picked up at next
  session_open, engine_heartbeat records which calibration version is live.
- Tests: gate math on hand fixtures; determinism (two runs identical);
  calibrator replay on a fixed window reproduces itself; "incumbent never
  mutated without gate pass"; memecoin parity + full pump-chart test suite still
  green (the standard command block in AGENTS.md); market_live.test_live_parity
  PASS; test_prop_guard 6/6.
- Docs: new MARKET_RESEARCH_LOG.md section (numbered iteration chain, honest
  findings incl. negatives), GO_LIVE.md ops update, PROP_FIRMS.md append with
  the new MC numbers.
- Commit on market-backtest-systems (no push). Finish with a report: what was
  built, the calibrator-knob sensitivity map, the recalibrate-vs-frozen verdict
  with its p-values/CIs, the new prop-firm battery numbers (pass prob, EV, £/mo
  expected at 2–3 parallel attempts), and the recommended cadence + defaults.

## Operational notes
- venv: /Users/jaime/pump-chart/backend/.venv/bin/python (only venv).
- Batch runs: BACKTEST_RESULTS_DIR=backend/v2_results; no resume (restart re-runs
  the cell); pool workers freeze code at spawn — restart main.py after any
  deploy and shasum engines around long burns.
- Live account is PAUSED (cron removed, account flat, orders cancelled). Do NOT
  reinstall cron or run live sessions as part of this task; it is a research
  build. If you need a live smoke of a calibration snapshot, use
  path-replay/ForwardTester, not the broker.
- Alpaca keys live in (gitignored) backend/market_live/config/keys.env — never
  commit, never fund the account.
- Do not modify anything under /Users/jaime/pump-chart (main repo) except reading.
