# V2 Strategy Engine — Quantitative Research Log

> Condensed 2026-09-26 from the 1,313-line log (full text: git history + `backend/analysis/`
> batch artifacts). One row per iteration: mechanism → verdict → production effect.

Accept gate (paired per-recording ΔPnL): Wilcoxon p<0.05 + 10k-bootstrap 95% CI>0 +
≥50% breadth; tail tools need tail tests. History measured on older stacks — **re-baseline
before comparing** (MSM/gates/delays reset 2026-09-11; calibration made causal 2026-09-17).

---

## Current production state (2026-09-22)

Authoritative: `backend/strategy_engineV2.py::DEFAULT_CONFIG` (+ `app.js::engineParamsV2` mirror).

| surface | state | origin |
|---|---|---|
| Engine | V2 RBPF/UKF/Kramers, long-only, spot-only | iter02-04 |
| Exec | signal-instant (`exec_model="instant"`); `legacy` escape hatch; latency overlay | iter73 |
| Entry delay | **0.0 = OFF** (iter83 REJECTED — era-inverting) | iter83 2026-09-12 |
| Exit delay | **20.0 armed-only = ON** (full-DB Δ+5.06 p=5.6e-17, both eras, holdout) | iter83 2026-09-12 |
| EVR triage + veto | ON (120 s / 20% / 0.45 / veto 0.25) | iter48/50 |
| HF entry gate / dev-sell exit | **OFF** (iter62 policy; zero marginal once exit delay ON) | iter43/62/83b |
| Rate-split harvest | ON (arm 10% / θ 0.55 / 12 ticks) | iter63/64 |
| kelly_flat | ON (60 ticks / 40% offside) | iter21 |
| Calibration stack | popcal + mintcal + per-coin(gated, requires_mintcal) + **τ=VR horizon** ON; harvestcal OFF | iter86c/90e |
| τ program | `tau_max=clip(H*×scale,10,60)`, scale 1.0 (+3.935 p=0.00014, holdout p=0.017) | iter90e 2026-09-19 |
| Live booking | fill-anchor: books BT-identical fills, journals wallet truth as `cash_*` | iter91b 2026-09-22 |
| Tape basis | **effective basis ON** (pool_virtual_reserves.py): live + replay prices on `vault+V`, phantom hatch `effective_basis=False` / `PUMPCHART_DISABLE_VR_FIX=1` | iter94 2026-09-26 |
| Removed | futures/sniper (08-29), MSM gate (reverted 09-11), Q-layer, whale-dump/SPE/pool-drain exits, V1 trailing stop, mayhem V7, HF silence gate | — |
| Live safety | mcap-floor breach = emergency sell + block + terminate | — |

---

## Era 0 — Legacy volume-free dataset (iters 01–15, DELETED 2026-07-27)

Recorder bug (`sol_amount=0` → KDE uniform, φ≡0); wiped after iter15 fix.

| Iter | Verdict | Lesson |
|---|---|---|
| 01 | REJECTED | Self-referential EWMA anchors collapsed UKF variance. |
| 02 | ACCEPTED | Observable EWMA anchors (r̄², φ̄) restored state. |
| 03 | ACCEPTED | Bayesian P± replaced sign; trades −57%, PF ×6. |
| 04 | ACCEPTED | Removed V1 trailing stop; Bayesian-only WR 82–93%. `iter04_full` (+18.6) was a dropped-`recording_ended` artifact; true baseline = iter08. |
| 05–06 | REJECTED | S_eff/barrier filters killed +EV trades; empty-KDE pathology. |
| 07 | REJECTED | Hard stops truncate rebound winners; costs ≫ savings. |
| 08 | CANONICAL | `recording_ended` force-close: **−7.395** true baseline. |
| 09–14 | REJECTED | Sign flip (457× churn), breakers, streaks, IG hold (144k trades), KDE lag-follow, dt=0.25 detune. |
| 15 | RECORDER FIX | Vault-diff extraction patched; dataset wiped. |

## Era 1 — Fresh dataset, OHLCV ceiling (iters 16–42, 07-27 → 08-28)

Exits stabilize; 10+ orthogonal entry-side negatives (microstructure, breadth, provenance, pool, geometry, flow).

| Iter | Verdict | Lesson |
|---|---|---|
| 16–20 | ACCEPTED | Anchors, counterfactual gates, gain_retrace overlays; no-hard-stop + reversal guard. |
| 21 | ACCEPTED | `kelly_flat` exit (no-long E*≤0 × 60 ticks, ≥40% offside). |
| 22/26/28 | NEGATIVE | Big losers inseparable from winners by entry features (exhaustive). |
| 27 | ACCEPTED | `give_frac` 0.4→0.5 (+31.7% PnL). |
| 29 | CONFIRMED | arm=10 optimal. |
| 30–32 | NEGATIVE | `pool_sol` = price mirror; LP-pull leads crashes but never at entry. |
| 33 | NEGATIVE | P_down ≡ 0 blindness is universal; 0/3 survive. |
| 34–35 | NEGATIVE | 41/155 mints dual-outcome ⇒ static token filtering capped; graduation filter already optimal. |
| 36–38, 43–44 | ITERATIVE | HF instrumentation (dev ATA + GMGN); gate 1.0 ACCEPTED (+163%, p=0.0095); require_tag=1 REJECTED (sparsity 12/44); causality audit. |
| 37 | NEGATIVE | **Oracle bound**: exit-only/re-entry changes bounded below baseline on this stack. |
| 39/41 | PARITY FIX | 5 live-vs-BT divergences fixed; immediate HF exit on pump task. |
| 40 | NEGATIVE | Variance-ratio/Hurst organicity gate. |
| 42 | NEGATIVE | Futures deleted 08-29 (long-only 1h majors −11.4 USDC/47 trades). |

## Era 2 — Flow triage + tail batteries (iters 45–56)

| Iter | Verdict | Lesson |
|---|---|---|
| 45 | ACCEPTED (tail lens) | Pre-entry taker-imbalance gate; whole-PnL gate structurally rejects tail tools. |
| 46/47 | REJECTED | CWSE staging; drift-completed down-channel. |
| 48 | ACCEPTED | **EVR triage** (120 s unconfirmed + invalidated + ≥20% offside): catas 87→76 (p=0.0038). |
| 49 | BOUND | FP/TP inseparable at fire (AUC ≤0.62); evr9 Pareto-optimal. |
| 50 | ACCEPTED | **Sell-concentration veto 0.25**: WR 70.2→71.45%, CI+. |
| 52–55 | REJECTED | Market adapt, adaptive sizing, SODT, WCCB — net-negative/non-engaging. |
| 56 | ACCEPTED→REMOVED | HF silence gate 2700 s (−0.44 drag); removed Sep-2; re-admission REJECTED iter68. |

## Era 3 — Regime adaptation (iters 57–66)

| Iter | Verdict | Lesson |
|---|---|---|
| 57/58 | ACCEPTED→REMOVED | Q give-back adapt; whole Q-layer removed iter64+cleanup. Entry/exit/Kelly/SDE regime batteries ALL REJECTED. |
| 59–61 | REJECTED | SDE conditioning, staged sizing, participation floor — bleed is the never-confirmed entry-rate channel. |
| 62 | USER POLICY | HF gate + dev exit OFF (ablation −0.74 but policy prevails; never committed — resurrected by Sep-11 surgery, re-applied OFF). |
| 63/64 | ACCEPTED | **Rate-split harvest** (armed θ=0.55 × 12); regime gate OFF (ungated better). |
| 65 | REJECTED | Pool-drain exit: harvests losses, capital re-bleeds. |
| 66 | PARITY | Realtime HF source (stream whale + dev-ATA); exec-offset knobs (analysis-layer). |

## Era 4 — Tail mandate + execution (iters 68–73)

| Iter | Verdict | Lesson |
|---|---|---|
| 68–70 | ALL KILLED | Tail walls bound (W3 veto / W1 fast / W0 EVR); silence/insider/concentration/EVR-recal/flow-sizing/vol-collapse none beat them. |
| 73 | SHIPPED | **Signal-instant parity**: live swaps at signal tick; BT `exec_model="instant"`; latency overlay (buy ~10 s, sell ~2.3 s). |

## Era 5 — MSM fleet regime (iters 74–76) — REVERTED 2026-09-11

3-state HMM over 5-min fleet bins; B′ adopted 09-01 (+1.32, p=0.034) then **user-reverted** —
code + panel deleted. Sizing/conservation REJECTED (Kelly n* anti-information). σ² switch
neutral; 16-config sweep confirmed B′ was the family optimum. Revisit only with new data.

## Era 6 — Consistency + delays (iters 77–82)

| Iter | Verdict | Lesson |
|---|---|---|
| 77 | REJECTED engines / SHIPPED fleet | V4 one-token lottery, V6 full-pop negative (both non-default); **live multi-engine fleet** shipped. |
| 78 | DISCOVERY | **5 s deferred entry** +1.18 (p=1.4e-4): buys the signal micro-dip; 10 s+ buys the bounce (era-inverts). Whale-dump re-REJECTED. |
| 79 | REJECTED+REMOVED | SPE/P_zero: P_zero≥0.85 on 85.6% of ALL in-position ticks — no discrimination. |
| 80 | ADOPTED→OFF | 20 s armed exit Δ+1.04 (p=0.0038); adopted 09-03, set OFF 09-11 reset. |
| 81 | RESEARCH | Mayhem mandate: 9 surfaces falsified; V7 deleted. |
| 82 | AUDIT | Delay plateau 3≡5, 10≡20≡45 (mechanism, not overfit). |

## Missions

- **Live-monitor 2026-09-04→05, COMPLETE** (`notes/live_monitor_*`). Exit-hold reset fixed (`171e715`).

---

## Iter 83 — Delay re-tune, post-cleanup stack (2026-09-11/12, PREREGISTERED)

Cohort 2,396 recs (pre/post-Aug-20 cut 1787184000); train/holdout 50/50 seed 20260911.
Baseline `iter83_base_full` −0.661/67.2%. Mid-session external edit (entry 0→2.0, exit
0→20) contaminated 4 cells → quarantined as `drift{1..4}`, re-burned with fully explicit params.

| Cell | config (entry/exit armed) | boot mean Δ | p> | CI low | breadth | era Δ pre/post |
|---|---|---|---|---|---|---|
| C1 | 5.0 / 0.0 | +0.00242 | 0.023 | −0.00043 | 53.1% | +0.78 / **−0.081 FAIL** (CI+era-invert) |
| C2 | 3.0 / 0.0 | +0.00198 | 0.011 | −0.00064 | 52.9% | +0.62 / **−0.041 FAIL** |
| **C3** | **0.0 / 20.0 armed** | **+0.00958** | **7.1e-10** | **+0.00607** | **59.7%** | **+1.97/+0.85 SELECTED** |
| C4 | 5.0 / 20.0 armed | +0.00368 | 0.0039 | +0.00062 | 54.8% | pass, weaker |
| C5 | 2.0 / 20.0 armed | +0.00330 | 0.0014 | +0.00039 | 55.0% | pass, weaker |

Every entry-delay config degrades the stack (iter78 effect dead post-cleanup); C4≡drift twin
290/290, C5≡drift twin 291/291 (determinism ✓).
**VERDICT: ACCEPT — `v2_exit_delay_seconds` 20.0 armed-only ADOPTED, entry stays 0.0.** Full-DB
`iter83_win_x20_full` vs base (571 paired): **Δ+5.064**, p=**5.6e-17**, CI[+0.0065,+0.0114],
breadth 59.5%; per-era Δ +2.82/+2.22 (each p<3e-9); holdout Δ p=**6.8e-9** CI+ breadth 59.4%;
catas 207→187 (drag −9.39→−8.30). Mechanism: armed harvests fill ~20 s deeper into their
bounce (exp/trade 0.00045→0.00338, 7.5×); loss book untouched; decisions byte-identical.
Headline WR now ~63–64% (was 66%) — WR-for-expectancy trade.

## Iter 83b — Holder-flow on the adopted delay stack (2026-09-12, PREREGISTERED)

2×2 (delays × HF) on same 2,396 cohort. C6 = delays+HF ON vs adopted, train 271 paired:
Δ+0.00126 (p=0.037) but CI[−0.00095,+0.00328]∋0, breadth 16.6% → indeterminate → HF OFF.

| delays | HF | full-DB PnL | WR | note |
|---|---|---|---|---|
| off | off | −0.661 | 67.2% | baseline |
| off | ON | +0.536 | 64.9% | +1.20 here (p=0.004 CI+, sparse coverage) |
| 20a | off | +4.402 | 63.6% | **ADOPTED** |
| 20a | ON | +2.771 (train) | 63.3% | ≈0 marginal |

Read: armed deferral already captures the post-washout protection dev-exit bought. Caveat:
pre-iter72 recs carry ~6.7% whale coverage — re-litigate on full-coverage cohort only.
**VERDICT: delays ON + HF OFF; no code change.**

## Iter 84 — Global-regime calibration (2026-09-12, PREREGISTERED) → REJECTED (verdict unsound, see iter84b)

REGIME_B (`sigma_mu` .10→.15, `lambda_mu` .15→.20, `tau_max` 30→20, conf .79→.82/.80) on
post-Aug-20 train (510): CAL 65 trades/−0.234 vs BASE 73/−0.149; breadth 0%; symptoms worse.
**H1 REJECT, mechanism CLOSED.** (Null later found degenerate — BASE cells ran REGIME_B via
the sentinel leak; see below.) Infra (`session_calibrator.py`, SOL history) kept as instrument.

## Iter 84b — Population SDE calibration + sentinel-leak correction (2026-09-12)

Physics population estimator (last-50-DB window → all 13 SDE coeffs via stationarity
inversion). Load-bearing stability finding: DEFAULT UKF collapses (h→+15, σ→1808) on
8/30 post-era smoke; fixes = EWMA+IQR sigma_h, sigma_phi ceiling 0.15, noise floors ≥
DEFAULT/2. Calibrated: 0 collapses vs DEFAULT 2 (strictly more stable). 4-state feed
mandatory for diagnostics (1-update/candle overstates collapses 5/30).
**Bug**: `run_backtest` popped the sentinel from the shared chunk dict → tasks 2..N
silently calibrated (default True). Invalidated iter84 BASEs + iter84b screen (+1.98 "H1
PASS" = CAL-vs-contaminated). **Fix**: copy-before-pop, default → False (explicit opt-in).
Regression `TestBatchSentinelLeak` 35/35.
Definitive screen (clean `base_full_v2` vs valid `cal_full`, 2,437): full Δ+0.40 CI∋0
p=0.098; post +1.30 CI∋0 p=0.031 breadth 52%; holdout null. **VERDICT: REJECTED.**
Survives: stability + WR/expectancy (68.3% vs 63.5%, +0.0063 vs +0.0034) at starvation cost.
Graveyard: per-session 13-coeff calibration unless starvation is fixed (kappa_mu floor,
lambda_mu ceiling). Sentinel default FALSE thereafter.
**User adoption 09-12 anyway** (`iter84b_cal_full`: 777/68.3%/+4.86 vs 1311/63.5%/+4.47):
`v2_popcal_enable`=1.0 DEFAULT; OFF hatch one knob away at every layer. Recorded over the
failed CI gate on per-trade quality + stability.

## Iter 85 — Per-coin online recalibration (2026-09-12, preregistered) → REJECTED

Engine-native: this coin's rolling tape (≥120, window 600) → 13 SDE coeffs every 100 candles
(`recalibrate()` repacks cfg/KDE; user keys protected). Smoke: 29/30 diverge (11/13 coeffs
typical), 0 collapses, 18/18 tests, parity 10/10. Full-DB vs clean base: Δ−0.070 CI∋0
p=0.205; post +0.368 p=0.137; WR 63.5→69.3% and exp ×2 but trades 1,311→642, big-win rate
16.1→13.9% — same failure signature as iter84b (better quality, halved tail exposure, flat
PnL). **Entry gate near information limit; SDE recalibration any flavor → graveyard**
without a non-tape data channel. Infra shipped opt-in (OFF = byte-parity).

## Iter 86 — Mint-history + combined stack (2026-09-13)

User proposal: calibrate from the token's own prior tape at tick 0
(`calibrate_from_mint_history`; same-mint candles before start, no lookahead). Coverage
702/2,437 (29%). `iter86_mintcal` on adopted popcal stack: affected Δ+0.16 p=0.37 breadth
48% (gates fail) but exp −0.0012→+0.0016, PnL −0.079→+0.084; mid-tapes (600–1999) best.
**iter86b combined** (mint at tick 0 + per-coin refining it; `_calibration_sourced`
sentinel lets calibration keys refine while user keys stay protected, 26/26 tests):
affected Δ**+0.71** p=**0.0057** CI[+0.0002,+0.0019] breadth 62%; full recon +0.68 p=0.0027;
holdout +0.26 p=0.0033; WR 63.8→**76.8%**, exp →**+0.0091** at identical trade count (69=69).
**ADOPTED** (`v2_mintcal_enable` + `v2_percoin_cal_enable` = 1.0; adapter fallbacks from
DEFAULT_CONFIG; NONCAL restores pure-DEFAULT byte-parity; app.js v135; parity 10/10).
**iter86c correction**: recon assumed thin tapes inert — false (per-coin noise there:
true ungated full-DB CI∋0, post p=0.36, train/holdout ±1.3 imbalance). Fix:
`v2_percoin_requires_mintcal`=1.0 (online arms only on mint-sourced sessions; explicit knob
bypasses). Gated burn = affected values + rest byte-identical. Lesson: never reconstruct an
engine-native layer from a subset burn.
**iter86d live chain fetch** (2026-09-14): thin-mint sessions fetch full on-chain history
(curve PDA + PumpSwap sigs → 1 s candles → `mint_history_candles`, 90 s bound, warmup-sized;
failure → population fallback). RPC retains ~2 days sigs (recent-only; tape deepens across
sessions). Incidental: fixed wrong pump.fun program const (native curve-watcher was dead).
Smoke + suite 204 / parity 10/10 green.

## Iter 89 — Causal re-baseline (2026-09-17) → correctness, not alpha

Backtester calibrated from future data. Three leaks fixed + tested: (1) population needs
`stopped_at<=cutoff` + `time<cutoff` (rec 4320: 10/50 window recs ended 2.5–6 ks after
cutoff); (2) mint layer capped to `time<cutoff`, 6000 s lookback; (3) mint row-layout bug
(timestamp kept as "open"). Per-coin now t-scheduled (100 s, blend 1.0) with per-attempt
cutoffs; `calibration_startup.py` frozen-cutoff init (mint→chain→population) shared
live+BT; chain fetch paginates backward from cutoff. Knob re-tune (gentle/responsive/stable
on frozen mint-disjoint 96/96): 0/3 move any screen trade; gentle holdout +0.0027 →
REJECTED. Full 2,648 (rec 5022 excluded): Δ+0.177 p=0.489 CI∋0, eras null. **NOT ADOPTED;
causal defaults stand.** Honest stack: 887 trades / 68.2% / +5.31 / +0.00598. All pre-iter89
popcal baselines incomparable. Suite 267/8-foreign/1-skip; parity 10/10.

## Iter 90 — Neutrality autopsy + harvest geometry (2026-09-18) → both CLOSED

Why causal calibration is neutral: (1) estimators floor-pin on ~100% sessions (same vector
everywhere — pre-89 "signal" was bug+leak garbage); (2) refinement gated to mint-sourced
11–12% that rarely trade (0/20 screen traded); (3) Kramers direction ≈ KDE density ratio
(T cancels) — geometry-driven, coefficient-free in bounds (ON/OFF diverge 9/16, Δ−0.068).
Candidate: per-coin `gain_retrace_arm_pct` calibration (dominant harvest path: 525/887,
+5.18; estimator + init/mint/online layers + `_geometry_sourced`; `v2_harvestcal_enable`
default 0 = byte-parity; 23 tests). Smoke: layers fire, config-active, causal, no collapse —
but only 3/16 traded diverge. Campaign (frozen snapshot, geo/geo_s08/geo_s13/geo_gate):
best (geo_gate +0.0224, 2 changed) → holdout 0/96 changed → **REJECTED**; full-DB −0.142
p=0.211, 3.2% changed. Counterfactual grid (arm 4–14 × give .35–.70, 887 trades): EVERY
cell ≤ production (arm 10/give 0.5); marginal 4–10% arming trades are net winners unlocked.
**Harvest-lock surface CLOSED; production unchanged** (68.2%/+5.31 stands). Tape-drift note:
rec-4320 goldens re-measured on HEAD/current data (data drift, verified clean-worktree).

## Iter 90d/e — VR decision-horizon calibration: ADOPTED (2026-09-19)

Leverage sweep: only τ moves decisions positively (tau60 +76/+0.258 on 16 recs; gate
scalars noise). Root cause: legacy τ (=1/λ_μ inversion) floor-pins at 10 on ~100% sessions
→ P⁰-heavy, over-conservative. Estimator: H* = argmax Var(ret_H)/H over {15,30,60,120,240,
480} (chop H*=15 vs trend ≥30, bimodal); `tau_max=clip(scale×H*,10,60)` in all layers
(`v2_tau_vr_enable`, OFF=legacy parity; 9 tests; smoke 9/16 diverge at scale 1.0).
Campaign (96→96→2,647): static-tau60 diffuse (+3.07, p=0.83, 52% of 1,302); **per-coin
tauvr10 (scale 1.0) clears everything**: full-DB **+3.935** (5.31→9.24, +74%) p=**0.00014**
CI[+0.0007,+0.0023], 565 changed 61% improved, token breadth 60%; holdout **+0.462
p=0.017** CI[+0.0010,+0.0102] 82%; eras +1.28/+2.65; day-permutation p=**0.0005**; trades
887→1,991 (+124%, added ≈62% WR), exp +0.0060→+0.0046; tail balanced (38 vs 55).
**ADOPTED: `v2_tau_vr_enable/scale` 1.0/1.0** (static NOT adopted — diffuse). Goldens
re-measured; suite 276/parity 10/10; restart + hard-refresh to deploy.

## Iter 90g — UI wiring fix (2026-09-19)

User 7-day UI batch (−0.16) vs adopted re-run on same cohort (+1.305/234/59.8%):
(1) stale mirror sent popcal/mintcal/percoin/tau_vr/exit-delay all 0 (pre-iter83 physics);
(2) deeper: mirror broadcast 13 SDE defaults that overwrote calibrator coeffs and killed the
sourced sentinel — **every UI BT/live session ran uncalibrated since the program began**
(campaign burns used minimal params, unaffected). Fix: `_strip_default_sde` (default-valued
SDE keys dropped pre-merge; non-defaults still win), mirror exit 0→20, bust v139→140.
Fixed mirror+pipeline reproduces adopted byte-exactly. 132 calibration tests green.

## Iter 90k — Overnight parity audit (2026-09-21)

75 sessions (22 traded): live 61/29/47.5%/+0.0035 @0.01 vs like-for-like BT 65/56.9%/+0.0625
(user UI batch 59/62.7%/+0.5365 @10× notional, fresh fit). Forensics (26 trades/12 sessions):
(1) wall-vs-tape phantom (tool bug — candle clock trails wall ≤30 s on thin tapes; BT stamps
armed exits at target second inside gaps; `verify_session.py` now compares decision tape-s);
(2) settle-wait exits (physical — sell waits for buy landing; fill-price class, unfixable);
(3) **post-launch re-arm bug (REAL, FIXED)**: exit launch cleared `_pending_exit` mid-second,
remaining states re-armed hold+boundary; `confirm_sell` left it standing → ~20 s extra
in-position, 3 missed re-entries (rec5343/5356/5400). Fix: detection requires
`status!="closing"` (+ regression test); (4) tau re-init leak (replay, not live): refit on
params equal to today's default diverges (rec5400 τ30→45, +1 trade); forced-kwargs replay
exact 7/7, 11,148-state scan zero-divergence — **pipelines decision-identical; replay
wasn't**. OPEN (needs user OK): `run_backtest` honor session snapshot (skip re-fit), else UI
batches always re-fit. Accepted: 3 manual sells + downstream, mcap stop (rec5381 t2), 1
insufficient_sol (rec5401). Arithmetic closes: 61+3+1=65. Cliff-exit edits lost (uncommitted)
→ abandoned per user. BACKTEST_RESULTS_DIR pinned for verify tool.

## Iter 91 — Execution audit: tape price is the phantom (2026-09-22)

53-rec same-trades batch (23 trades/69.6%/+0.0677 @0.1) vs live (23/52%/−0.00825 @0.01):
(1) **decisions 22/23 exact** (entry s + reasons; holds match; 17 armed targets = BT ±1 s;
knobs identical); (2) rec5429 mismatch = replay refit artifact (session-forced replay exact;
`recording_ended` = live's mcap-stop, label only); (3) fills: BT-vs-tape@signal −2.3%,
signal→land ≈0, **cash-vs-tape@land +21.6% median** (in +29.6%/out +20.1% — the PnL tax);
cash≈Jupiter quote (+0.00%) — zero slippage; (4) venue test decisive: rec5429 slot
449209749 + 5 neighbor same-pool fills 7.36–7.54e-08 (ours mid-range) vs tape 3.85–3.91e-08
(**~1.9× below every real fill**; candle self-consistent but off-market) — BT books at
prices the market never offered (thin-pool extreme of the known ~25% basis); (5) landing
fast (broadcast→blockTime ~0.6 s buys/~0.8 s sells; 8–20 s `elapsed_s` is reconcile
bookkeeping; signal→broadcast 0.2–1.0 s). Fixes: buy-settled opens at confirm (was 8–20 s
`pending` blocking sub-15 s exits/re-entries); sell skips redundant balance read (~0.5–1 s).
**Direction: make BT fill at executable levels, not live toward phantom.** Restart to deploy.

## Iter 91b — Fill-anchor booking (2026-09-22)

User directive: live ≈ BT winrate/PnL, live-trader-only change. **Result-booking parity**:
live books exactly as `forward_tester._open_long/_close_long` for same decisions; wallet
truth journaled as `cash_*`. Contract (23/23 ≤1e-9 on batch `1790060527972`): entry =
`path_price_at(entry_ts)`×1.01 (frac 0 ⇒ signal-candle open); armed exit =
`path_price_at(sig+20)`×0.99 (gap seconds via prior-candle interp — 9/23 sat in gaps);
instant exit = `_intrabar_price(frac 0.505)`×0.99 (NOT state close); pnl =
`size/entry×exit−fees−size`, fee 0.2%/RT (notional-invariant vs BT 0.1 ref) — win iff >0.
Impl: `_pending_exit_anchor` frozen at detection/target via FT byte-mirror
(`_exit_anchor_target_t` survives retries); `confirm_sell` books anchors. 24/24
parity/exit-delay/signal green. Replay of 23: 22 decision-matched book **exactly batch:
68.2% / +61.7% notional** (target ✓); 23rd (rec5429) is decision divergence
(live `buy_exhaustion@…517` −36.5% vs refit `buy_trend@…502` +5.98%). Full live book:
65.2%/+25.2% (was 52%/−82% cash). rec5429 race = calibration one-rec accepted-set lag
(28/54 exact; divergent blocks share median-lattice fingerprints; rows/candles pristine —
last mile unrecoverable from disk). Fixes need user OK (outside live-only): (A)
deterministic fitter window, or (B) batches honor stored kwargs (verify already does).
Restart to deploy.

## Iter 94 — THE PHANTOM WAS THE TAPE: PumpSwap virtual-quote-reserves basis fix (2026-09-26) — SHIPPED

User report: the live "fill premium" is fundamentally impossible as slippage vs the charted
price action. **Correct — root cause is a price-basis error, not execution.** Forensics
(on-chain, `analysis/iter94_wedge_study.py`, 94 fills / 24 sessions):

1. Post-graduation PumpSwap pools price swaps on **effective_quote_reserves =
   vault + virtual_quote_reserves**, where **V = 17.5845 SOL** is a protocol constant
   (measured identical to 1e-7 SOL on every pool decoded, creations back to 2026-08-20;
   appended i128 at pool-account offset 245). PumpPortal's pumpswap trade events
   (and `PumpSwapRPCClient`'s vault-diff feed) report the RAW VAULT ratio — so every
   recorded price for a graduated token sat **below the executable price by
   (vault+V)/vault** (median 1.29× on the studied sessions; grows to 2.3×+ as pools
   drain; iter91's rec5429 "1.9× thin-pool" case = same wedge). Curve-era tapes are
   correct (CPMM prices on virtual reserves natively).
2. **Fills were always fair**: at the fill instant (pool vaults decoded from our own
   swap txs) every buy = spot_eff×1.0126 and every sell = spot_eff×0.9877 — the
   PumpSwap 1.25%/side fee and nothing else. The iter91 "+21.6% median cash-vs-tape"
   and iter93's k_buy/k_sell "premiums" were 100% this wedge. The iter93
   realistic-exec campaign (tape → realistic Δ −1.42 SOL, WR −13.6pp) was testing a
   phantom wedge as if it were an execution cost.
3. **First-buy rent**: a session's first buy creates the WSOL ATA (+0.00149 SOL once
   per wallet) and token ATA (+0.00151 SOL once per mint) inside the swap tx = +15%/+30%
   of a 0.01 SOL notional in wallet deltas (the 1.1653/1.3022 residual clusters). Real
   but one-time; excluded from trade PnL (basis = size_sol), now journaled as
   `rent_sol` + `buy_rent` events.
4. `exit_price_actual` was fiction: `confirm_sell` was passed `self._last_price` (the
   tape) — now = `sol_received / tokens actually sold` (ledger truth).

**Fix (all layers, shipped):**
- `pool_virtual_reserves.py` — V resolver (DexScreener pumpswap pair → pool-account
  decode), in-process + `pool_virtual_resolves` sidecar table in price_data.db for
  pool-worker determinism; `prewarm_mints()` (30-mint DexScreener batches +
  getMultipleAccounts) hooked into `run_backtest_batch` so batches never network in
  workers; negative rows are permanent-for-replay (pool created later ⇒ post-fix
  recording ⇒ no loader correction anyway).
- Live feed: hub resolves V per mint on register, re-polls `_VQ_POLL_S`=120 s until the
  pool appears (mid-session graduation; migration-event trigger best-effort),
  `subscribeMigration` attempted; `_normalise` lifts pool-era prints to
  `(vSol+V)/vTok` (graduation-gated by pairCreatedAt; curve prints untouched);
  mcap scaled by the same wedge; `PumpSwapRPCClient` prices `(quote+V)/base`.
- Replay: `get_recording_candles(effective_basis=True)` (default) corrects recordings
  started before `PUMPSWAP_VR_FIX_EPOCH` per candle via `(pool_sol+V)/pool_sol`,
  pairCreatedAt-gated; post-epoch recordings were stored corrected (no double-fix).
  Phantom escape: `effective_basis=False` (tests) / `PUMPCHART_DISABLE_VR_FIX=1` (global).
- Live journal: `rent_sol` on trades + `buy_rent` events; `exit_price_actual` = ledger.

**Validation:** `test_live_parity` 10/10 on corrected candles; new
`analysis/test_pool_virtual_reserves.py` 20/20 (decode, graduation gating, loader
boundary, kill-switch, hub wiring, prewarm). `analysis/iter94_validate_fix.py`:
post-fix live fills vs corrected tape = ×0.984 sells (fee signature — wedge gone),
buys ×1.068 (fee + first-buy rent + signal→fill adverse timing on pumps); live cash
vs like-for-like corrected replay now the same basis and ballpark. Stale iter92/93
tests: `test_exec_model.py` realistic-band section skipped (machinery never committed —
stash@{0}; premise falsified), rec-4320 goldens re-measured (iter89 precedent).
**Era note: every pre-iter94 v2_results baseline was measured on the phantom tape —
re-baseline before comparing anything; graduated-token PnL/WR shift ~proportional to
the wedge's covariance with hold windows.** Deploy: restart `main.py` + hard-refresh
(chart/anchors/bookings all on the executable basis now).

**iter94b incident + fix (2026-09-27 01:10):** "all buys failing" with pump `Custom:1` —
decoded on-chain: instruction-5 Token-2022 ATA creation, `Transfer: insufficient lamports
1507032, need 1513840`. Wallet at 0.0131 SOL vs a **0.01311 SOL hard first-buy requirement**
(0.01 swap + 0.001514 token-2022 ATA + 0.001488 WSOL-ATA re-open + 0.00011 fees) — the
pre-flight (`buy + 50k lamports`) passed, the tx failed on-chain and burned 0.000105 SOL
priority fee per attempt (69 attempts / 8 confirmed / 15 broadcast-died overnight). Root:
wallet exhausted by session churn's rent+fees+losses; the check never modeled the
first-buy rent that iter94 exposed. Fix: rent-aware pre-flight in `execute_buy` —
first buy of a session requires `buy + 0.0034 (ATA rents) + 0.00025 (fees)` (flag flips
on confirm/adoption); repeat buys require `buy + 0.00025`. Clean `insufficient_sol`
rejection BEFORE broadcast (no fee burn), `required_sol` journaled.
`analysis/test_first_buy_rent_preflight.py` 4/4. Operations: a 0.01-SOL-notional wallet
trading fresh mints needs ≳0.0137 SOL per session start — **fund the wallet**.

---

## Graveyard — do NOT re-test without a new data channel

P_zero exits (79: P_zero≡1 on 1 s tapes) · whale-dump (72/78: replacements eat savings) ·
pool-drain (65) · exit-only/re-entry family (37 oracle bound) · P_down-blind sizing/gates
(33b/c) · static provenance/token features (34/35/56) · pool_sol as signal (30/32) · regime
entry-side anything (52/58/59/60/61) · Kelly-coupled sizing (76) · silence re-admission (68) ·
EVR delay/ratio extension (49/69/70) · creator-rug QC (09-04 RugCheck) · V4/V6 as defaults
(77) · SDE-coeff recalibration any flavor (84b/85: quality↑, tail-exposure↓, PnL flat) ·
harvest-lock geometry (90: production at surface optimum) · entry-gate scalars (leverage-2:
noise) · sigma_h from raw std(Δlog r²) (ceiling-clip explosion; use EWMA+IQR) · sigma_phi >
DEFAULT (saturation cascade) · noise floors < DEFAULT/2 (overconfident-UKF collapse) ·
single-update/candle diagnostics (overstates collapse; invariant 2) · `iter66_exec_calibration.
json` INVALID (model-anchor vs real-fill comparison).

## Benchmark baseline history (fresh dataset; pre-causal rows incomparable post-iter89)

| Baseline | Cohort | PnL | WR | Notes |
|---|---|---|---|---|
| iter16_baseline_full | first fresh | — | — | post-iter15 |
| iter31_baseline_full | 652 | +0.965 | 75.6% | post-ceiling |
| iter74d_base_full | 1,525 | +1.124 | 65.9% | 08-31 canonical |
| iter75sw B′ | 1,525 | +1.320 | 66.0% | with MSM (reverted) |
| iter83_base_full | 2,396 | −0.661 | 67.2% | post-cleanup: gates/delays/MSM off |
| iter83_win_x20_full | 2,411 | +4.402 | 63.6% | + exit 20 armed (ADOPTED) |
| iter89 causal base | 2,647 | +5.306 | 68.2% | honest (causal calibration) |
| iter90e tauvr10 | 2,647 | +9.241 | 64.6% | + VR τ program (ADOPTED) |

**All rows above were measured on the pre-iter94 phantom tape** (pool-era prices =
raw vault ratio, understated by (vault+17.58)/vault vs executable). Re-baseline on the
effective basis before any future comparison — graduated-token PnL/WR shift with the
wedge's covariance with hold windows (rec-4320 goldens moved 4/−0.0746 → 6/−0.0240).
