# NEXT AGENT BRIEF — Eliminate the Post-Peak Give-Back (target: beat +2.07 SOL baseline)

You are taking over quantitative strategy work on this repo's V2 engine (`backend/strategy_engineV2.py`, memecoin 1s tape, long-only). Read this entire brief before writing code. It encodes ~40 iterations of hard-won negative results — violating any item in it will waste days and produce false positives.

## Mission

The latest production full-cohort backtest ends at **+2.0739 SOL (1,108 trades / 71.8% WR)** — but the cumulative PnL peaked near **+3 SOL** mid-run before decaying back to +2. Your mission: engineer and validate a mechanism that eliminates a material part of that post-peak give-back and the catastrophic-loss left tail, without sacrificing the win rate. Success bar: clear the paired-difference acceptance gates vs the current baseline AND push the full-cohort total toward +4 SOL. Be honest if the data says a direction is dead — this codebase's value is half built from rigorous negative results.

## Current production state (adopted 2026-08-25 — do not regress these)

| knob | value | provenance |
|---|---|---|
| `v2_rate_split_enable` | **1.0** | iter63/64 — stationary Kramers split s=k_down/(k_up+k_down) ≥ 0.55 sustained 12 ticks while armed (peak ≥ +10%); PRODUCTION ON |
| `v2_rate_split_regime_gate` | 0.0 | measured better ungated (gating cost −0.155 paired, p=0.001, breadth 83%) |
| `theta/persist/arm/offside/mpa` | 0.55 / 12 / 10% / 0 / 0 | verified local optimum — ALL 14 sweep directions null-or-worse |
| `v2_hf_silence_gate_seconds` | 0.0 | matches user's production profile |
| holder-flow entry gate / dev-sell exit / regime adaptation / participation floor | REMOVED | iter62 ablation + iter64 surgery (user decisions) |
| EVR triage + sell-conc veto | ON | iter48/50 |

Reference baselines (per-token logs in `backend/v2_results/`):
- `iter64_userbase_1787616977` — gated variant, 1,101 trades / 71.84% / +1.8596 (user's own UI run reproduced byte-exact)
- `iter64sw_ungated_1787631207` — **current production**, 1,108 / 71.8% / **+2.0739**
Battery runner: `cd backend && .venv/bin/python analysis/iter63_battery.py <CAND_LABEL> iter64_userbase_` (acceptance = Wilcoxon p<0.05 one-sided + bootstrap CI>0 + breadth ≥50% + favorable McNemar).

Exit-reason PnL decomposition (current cohort): gain_retrace +4.77, rate_split_flip +3.64, tp_v2 +0.73, breakeven +0.39, kramers_down +0.28, reversal +0.14, bayesian −0.04, **kelly_flat −4.28 (92 trades, avg −46%)**, **evr_triage −1.69 (the fires' own cost)**, **recording_ended −1.86**. Tail ≤−15%: 228 trades dragging −8.18 SOL.

## STEP ZERO — forensic decomposition BEFORE designing anything (mandatory)

Reconstruct the equity curve of `iter64sw_ungated` in time order (per-trade logs carry entry/exit timestamps). Decompose the +3→+2 give-back segment by: UTC date, hour-of-day, token, exit_reason, and trade age. Answer precisely: is the give-back (a) a handful of catastrophic losers, (b) an afternoon/session-wide cluster of small losses, (c) winner give-back after the peak day, or (d) recording_ended force-closes stacking up? Every prior iteration that skipped this step designed against a phantom. Scripts to adapt: `analysis/iter63_forensics.py` (MFE reconstruction from candle tape), `analysis/iter63_reentry_autopsy.py` (paired stream diff), `analysis/iter63_giveback.py`.

Known loss structure (verify, don't assume): tail losers (≤−15%) never reach +10% MFE (99.5% on the older snapshot) — they are entry-selection errors that no exit timing can rescue; EVR already covers the ≥120s flow-invalidated lane; kelly_flat fires at median 355s vs −20%-cross at ~56s.

## The graveyard — do NOT re-test without a genuinely NEW information source

Exit-side (all REJECTED, multiple independently confirmed): hard stops (it07), tighter trails g<0.5 (it27: LOOSER was +31.7%; BLP family −1.34; CF g-sweep negative), kelly_flat variants incl. μ-persistence guard (it21k), persistent-submersion exit (it37), SODT/WCCB timeouts (it55), confirmation-staged sizing (it60), exit-side τ amplification (prior it63 session, CI-failed), offside scope ×3 (latest: −0.032 on production config), peak-age veto ×2 (−0.105), θ/K/arm grid beyond optimum. **Oracle bound (it37 addendum)**: pure exit-timing/re-entry-gating on OHLCV alone was mathematically bounded below baseline — the escapes since then (EVR, rate-split) used NEW evidence axes (taker flow, escape-rate geometry), so your idea needs a comparably new axis OR portfolio-level scope.

Entry-side (11+ rejections): local regime/microstructure gates (it31/45), on-chain provenance (it35), cross-token breadth & token memory (it34A), model ensembles (it56), regime-conditioned SDE coefficients (it59), participation floors/daily cutoffs/per-token caps (diagnosed dead, it60/61), market-condition scaling (it52). Pool liquidity (it32) and holder-flow pre-entry stats (it56) also null.

Replacement-entry dynamics: static same-path counterfactuals OVERSTATE gains — the freed capital re-enters and bled again in it37; post-flip re-entries were net-POSITIVE (+0.19) in it63 — never gate re-entries blindly; always real-engine screen before believing a counterfactual.

## Directions that remain genuinely open (ranked by expected value / novelty)

1. **Portfolio-level risk management (untested class)** — every prior mechanism was per-trade. Concurrent open-position count, fleet heat (Σ open notional vs rolling balance), per-hour entry throttling after N consecutive losses *across tokens*, daily account-level stop after −X drawn (note: "daily-loss cutoffs" were killed at DIAGNOSIS in it60/61 on the old cohort — re-derive on the current cohort before dismissing; the cohort and stack have changed materially).
2. **Time-of-day structure** — entry quality by hour has never been tested anywhere in the log. If the Step-Zero decomposition shows the give-back clusters in specific hours, a causal hour-aware throttle is cheap to validate.
3. **The 20–120s early-bleed lane** — between entry disaster onset (−10%@20s median) and EVR's 120s delay sits an uncovered window. it49 proved contemporaneous price/flow features can't separate there (AUC≈0.5) — so only a genuinely new observable (e.g., liquidity-pool depth trajectory `pool_sol` slope, spread widening series, or sub-second volume texture) could crack it; it32 showed k-jump leads crashes 5–15s but never appears at entry time.
4. **Position sizing** — engine Kelly `n_star` is computed but NOT wired to executed size (noted it33b). Wiring executed size ∝ min(n_star, cap) with a floor is untested end-to-end; it53's execution-adaptive sizing failed, but raw posterior-confidence sizing did not.
5. **Winner-side second-leg capture** — the runner-saturation artifact (stationary split pins s→1 during price discovery above KDE mass; rec952 lost −0.127 to a premature flip) is documented and irreducible on OHLCV alone per the veto experiments — BUT a flow-confirmed flip (rate-split ∧ trailing buy-ratio collapse) was explicitly listed as future work in it63 §6 and never tested.

## Protocol mandates (every one has bitten a previous agent)

1. **Baseline at battery time**: never inherit baselines across dataset boundaries — recordings accumulate daily (1,623 as of 08-25). Re-run or pair strictly within one dataset generation.
2. **Default-OFF development**: every new knob ships default-OFF (or parity-default), gated behind explicit `engine_params`. Bare-{} must byte-match production until adoption. Prove it: bare-{} vs explicit params on recs {1810, 431} must be trade-for-trade identical (see the adoption probe pattern in RESEARCH_LOG.md Iter 64 §7).
3. **Three-pipeline parity**: Backtester/ForwardTester/LiveTrader must evolve engine state identically. Any per-trade state resets go in BOTH `notify_trade_opened` and `notify_trade_closed`. Futures engines hard-off for spot-scoped mechanisms.
4. **Write-only diagnostics only** during exploration: the tick-capture hook (`v2_debug_tick_log`) appends JSONL per in-position tick carrying [t,o,h,l,c,entry,peak,k_up,k_down,P_up,P_down,P_zero,direction,E_star,tau,exit_reason,no_long_streak] — extend rather than reinvent. Capture harness: `analysis/iter63_capture.py`; CF scorer: `analysis/iter63_cfscore.py`.
5. **Screen → full batch → battery**: screen cells on the 260-rec stratified subset (`analysis/iter64_screen.py` pattern, ~8 min/cell), full-batch only survivors via `run_iteration.py --label X --params f.json --max-workers 8` (~30-50 min), then `analysis/iter63_battery.py X iter64_userbase_`.
6. **Suites must stay green**: `test_futures.py` (18), `-m pytest analysis/test_live_parity.py` (10), `analysis/test_regime_adapt.py` (10), `analysis/test_iter63_rate_split.py` (7), `analysis/test_hf_silence.py` (5). If you change defaults, update pinned assertions AND prove bare-{} ≡ intended config.

## Landmines (each one detonated on a real agent)

- **Filename collision**: `run_iteration.py --label X` OVERWRITES `backend/analysis/X.json` with the aggregate. Never name a params file the same as a future label (this silently corrupted a whole sweep on 2026-08-25). Use `*_params.json`.
- **Unknown engine_params are silently ignored** (merged into cfg, never erroring) — a typo'd knob runs the baseline and looks like a null result. Always verify the override took effect: check `engine_params` inside the per-token logs, and confirm cell deltas differ from baseline.
- `BACKTEST_RESULTS_DIR=backend/v2_results` env must be set for every batch entry point.
- Custom process pools need `guard_parent()` (from `process_watchdog.py`) — orphaned CPU-burning pools are a documented 17-hour incident.
- `python -m pytest analysis/test_live_parity.py` (pytest mode works; script mode misreports).
- Analysis dir is gitignored; v2_results too. The durable record is RESEARCH_LOG.md + AGENTS.md — **write those BEFORE launching long batches** (an earlier session lost its engine code in a tree reset and orphaned 3,800 result files with no writeup).
- Do not commit; the user commits.

## Deliverables

1. Step-Zero decomposition report (equity-curve anatomy of the give-back).
2. Mechanism implemented default-OFF + unit tests + parity proofs + green suites.
3. Screen results table → full-cohort batches of survivors → acceptance battery vs `iter64_userbase_`/`iter64sw_ungated` logs.
4. RESEARCH_LOG.md entry (next Iter number, house style: Status line, numbered sections, tables, lessons) + AGENTS.md summary block updated to the post-change state of the codebase.
5. Explicit statement of which knobs changed default and why, with the bare-{} parity proof.

Remember: the fastest way to respect the user's time is to let the data kill your idea cheaply (CF/screen) instead of expensively (full batches). The second fastest is reading the graveyard before digging.
