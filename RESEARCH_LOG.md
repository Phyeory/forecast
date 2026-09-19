# V2 Strategy Engine — Quantitative Research Log

> **Condensed 2026-09-11** from the original 8,977-line log (full text recoverable from git
> history: `git show fde6abc:RESEARCH_LOG.md` or any earlier commit). One entry per iteration:
> mechanism → verdict → production effect. Full per-iteration protocols, sweeps, and autopsies
> live in the git history and in `backend/analysis/` batch artifacts.

All candidate changes are evaluated on historical recordings with the **paired-difference
protocol** (see AGENTS.md): Wilcoxon signed-rank p < 0.05 on per-recording ΔPnL, bootstrap 95%
CI strictly positive, ≥50% token-improvement breadth — with tail-focused tests for
tail-extermination mechanisms (whole-PnL gates structurally reject them).

---

## Current production state (2026-09-11)

Authoritative source: `backend/strategy_engineV2.py::DEFAULT_CONFIG`. Historical baseline
numbers below were measured on stacks that differ from today's (MSM gate reverted, delays off,
holder-flow gates off) — **re-baseline before comparing new candidates**.

| surface | state | origin |
|---|---|---|
| Engine | V2 RBPF/UKF/Kramers, long-only, spot-only | iter02-04 |
| Exec model | signal-instant fills (`exec_model="instant"`); `legacy` escape hatch; measured-latency overlay available | iter73 |
| Entry delay | **0.0 = OFF** (iter83 REJECTED on post-cleanup stack — era-inverting) | iter83 verdict 2026-09-12 |
| Exit delay | **20.0 armed-only = ON** (iter83 ADOPTED: full-DB Δ+5.06 SOL p=5.6e-17, both eras, holdout p=6.8e-9) | iter83 verdict 2026-09-12 |
| EVR triage + sell-concentration veto | ON (eval 120 s / offside 20% / ratio 0.45 / veto 0.25) | iter48/50 |
| Holder-flow entry gate | **OFF** (iter62 user policy; briefly resurrected by git resets, restored OFF) | iter43/62 |
| Holder-flow dev-sell exit | **OFF** (same) | iter43/62 |
| HF stream silence gate | removed from code (Sep-2 dead-code cleanup; re-admission REJECTED iter68) | iter56/68 |
| Rate-split early harvest (`rate_split_flip`) | ON (arm 10% / θ 0.55 / persist 12) | iter63/64 |
| kelly_flat exit | ON (K=60 ticks, ≥40% offside) | iter21 |
| MSM fleet-regime gate (B′) | **REVERTED 2026-09-11** (`fleet_regime_online.py` + panel deleted) | iter74/75 |
| iter57/58 Q-regime give-back, iter61 floor | removed from code | iter64 + cleanup |
| Whale-dump exit, SPE (P_zero), pool_drain exit | removed from code (graveyard) | iter72/78/79/65 |
| Futures & sniper layers | deleted 2026-08-29 | — |
| Mayhem engine (V7) | deleted | iter81 |
| Live safety | mcap-floor breach = emergency sell + entry block + session terminate (the orphaned `3ec53f9` HOLD policy was never applied) | — |

---

## Era 0 — Legacy volume-free dataset (iters 01–15, dataset DELETED 2026-07-27)

Every metric from this era is bounded by a recorder bug: `PumpSwapRPCClient`/DexScreener
recordings carried `sol_amount=0`, so KDE ρ ≡ uniform and φ ≡ 0 — the engine ran order-flow-blind.
The dataset was wiped after the iter15 fix; artifacts survive only in `backend/analysis/` JSONs.

| Iter | Verdict | Mechanism / lesson |
|---|---|---|
| 01 | REJECTED | Self-referential EWMA anchors collapsed UKF variance (−15 clamp). |
| 02 | ACCEPTED | Observable EWMA anchors (r̄², φ̄) restored state; still churned on sign noise. |
| 03 | ACCEPTED | Integrated Bayesian posterior P± replaced point-estimate sign; trades −57%, PF ×6. |
| 04 | ACCEPTED | Removed V1 trailing stop; Bayesian-exit-only raised WR to 82–93%. `iter04_full` (+18.6 SOL) later proven a bookkeeping artifact (dropped `recording_ended` losers); true baseline is iter08. |
| 05–06 | REJECTED | S_eff filters / barrier anchoring — killed positive-expectancy trades; empty-KDE pathology. |
| 07 | REJECTED | Hard stop-loss caps truncate drawdown-and-rebound winners; costs ≫ savings. |
| 08 | CANONICAL (legacy) | `recording_ended` force-close (ef31d98) completed decision streams: **−7.395 SOL** true baseline (+17.89 from kramers_down exits, −25.98 rec_ended drag). |
| 09–14 | REJECTED | Spec-literal sign flip (457× churn), circuit breakers, trapped-basin streaks, IG hold-exit (144k trades), unconditional KDE occupancy lag-follow, SDE dt=0.25 detuning. |
| 15 | RECORDER FIX | Root cause was missing order-flow input, not the engine. Vault-diff extraction patched; dataset wiped; fresh era begins. |

## Era 1 — Fresh dataset, the OHLCV ceiling (iters 16–42, 2026-07-27 → 08-28)

Post-fix recordings. Theme: exits stabilize, entries prove inseparable — 10+ orthogonal entry-side
negative results (microstructure, breadth, provenance, pool liquidity, geometry, order-flow).

| Iter | Verdict | Mechanism / lesson |
|---|---|---|
| 16–20 | 16b/17/18b/19 ACCEPTED | Observation anchors, counterfactual entry gates, gain_retrace overlays; no-hard-stoploss + reversal-persistence guard. |
| 21 | ACCEPTED | `kelly_flat` exit #7 (no-long E*≤0 sustained 60 ticks, ≥40% offside). |
| 22/26/28 | NEGATIVE | Big losers inseparable from winners by entry-time engine features (exhaustive). |
| 27 | ACCEPTED | `gain_retrace_give_frac` 0.4→0.5 (+31.7% PnL). |
| 29 | CONFIRMED | arm=10 optimal. |
| 30–32 | NEGATIVE | `pool_sol` = 0.99-corr price mirror; LP-pull k-jumps lead crashes but never appear at entry; not exploitable (theory + real vault data). |
| 33 | NEGATIVE | Crash-velocity exit, blind-regime sizing, dual-KDE — P_down ≡ 0 blindness is universal; 0/3 survive. |
| 34–35 | NEGATIVE | Cross-token breadth, token memory, reflection shape, on-chain provenance: 41/155 mints dual-outcome ⇒ static token-level filtering mathematically capped; pump.fun graduation filter already optimal. |
| 36–38, 43–44 | ITERATIVE | Holder-flow instrumentation (dev ATA subscribe + GMGN); gate 1.0 (any ≥$100 sell) ACCEPTED (+163% PnL, p=0.0095); require_tag=1 REJECTED (registry sparsity); iter44 causality audit. |
| 37 | NEGATIVE | Persistent-submersion exit: correct exit, swamped by replacement-entry churn. **Oracle bound: exit-only/re-entry-gate changes are bounded below baseline on this stack.** |
| 39/41 | PARITY FIX | 5 root causes of live-vs-BT divergence fixed (holder-flow delivery loss, tick skip, signal drops, notify timing, pool_sol); immediate holder-flow exit on the pump task. |
| 40 | NEGATIVE | Variance-ratio/Hurst organicity gate. |
| 42 | NEGATIVE (spot intact) | Futures layer shipped, then **deleted 2026-08-29**: long-only V2 on 1h majors is break-even at best (−11.4 USDC / 47 trades). |

## Era 2 — Post-entry flow triage + tail batteries (iters 45–56)

| Iter | Verdict | Mechanism / lesson |
|---|---|---|
| 45 | ACCEPTED (tail lens) | Pre-entry taker-flow imbalance gate: tail-focused stats significant; whole-PnL gate structurally rejects tail tools. |
| 46 | REJECTED | CWSE staged exposure — tail compressed, PnL negative. |
| 47 | REJECTED | Drift-completed down-escape channel. |
| 48 | ACCEPTED | **EVR post-entry taker-flow triage** (fire at 120 s when never-confirmed + flow-invalidated + ≥20% offside): catas ≤−30% 87→76 (p=0.0038); loss reclassification, not elimination. |
| 49 | BOUND | FP/TP inseparable at fire time (AUC ≤0.62); delayed filters pay more than lookahead earns; evr9 Pareto-optimal. |
| 50 | ACCEPTED | **Sell-concentration veto 0.25**: FP scratches are bursty single-second sweeps; WR 70.2→71.45%, CI strictly positive. |
| 52–55 | REJECTED | Market-condition adaptation, execution-adaptive sizing, SODT timeout, WCCB — all net-negative or non-engaging; shipped default-OFF (later removed). |
| 56 | ACCEPTED | **HF stream silence gate 2700 s** (tail loss drag −0.44 SOL). *Removed from code in the Sep-2 cleanup; re-admission REJECTED at iter68 — do not restore.* |

## Era 3 — Regime adaptation program (iters 57–66)

| Iter | Verdict | Mechanism / lesson |
|---|---|---|
| 57/58 | ACCEPTED then REMOVED | Q_gr_lag3 give-back adaptation (thr 0.6 / adapt 0.2) — cleared the strict gate; later removed with the whole Q-layer (iter64 + cleanup). Entry/exit/Kelly/SDE regime batteries: ALL REJECTED. |
| 59–61 | REJECTED | SDE coefficient conditioning, CSS staged sizing, participation floor — the regime bleed is the never-confirmed entry-rate channel; floors are allocation policy, not alpha. |
| 62 | USER POLICY | Holder-flow entry gate + dev-sell exit turned OFF in the working tree (ablation said NET-NEGATIVE −0.74, but user policy prevails; re-gate criteria recorded). **Never committed — resurrected by the Sep-11 git repairs; re-applied OFF 2026-09-11.** |
| 63/64 | ACCEPTED | **Rate-split early harvest** (`rate_split_flip`, armed θ=0.55 × 12 ticks) — harvests armed winners before the give-back floor; regime gate default OFF (ungated measured better). |
| 65 | REJECTED | Pool-drain exit: fires harvest losses at the drain price; freed capital re-bleeds. |
| 66 | PARITY | Realtime rate-limit-free holder-flow source (session-stream whale dispatch + dev-ATA watcher); iter66 exec-offset knobs (analysis-layer). |

## Era 4 — Left-tail mandate + execution model (iters 68–73)

| Iter | Verdict | Mechanism / lesson |
|---|---|---|
| 68–70 | ALL KILLED | Fresh baseline (warmup 400); tail anatomy: W3 veto-latched / W1 fast / W0 EVR walls fully bound the tail; silence re-admission, insider-history, concentration, EVR recalibration, flow sizing, vol-collapse — none beat the walls. |
| 73 | SHIPPED | **Signal-instant execution parity**: live fires swaps at the signal tick; backtester default `exec_model="instant"` (legacy n+1 model = escape hatch); measured-latency overlay (buy ~10 s, sell ~2.3 s). |

## Era 5 — MSM fleet regime (iters 74–76) — REVERTED 2026-09-11

| Iter | Verdict | Mechanism / lesson |
|---|---|---|
| 74–74e | ADOPTED then REVERTED | 3-state Gaussian HMM over 5-min fleet bins; config B′ (`0:idle;2:idle,trend`) adopted 09-01 (+1.3196, p=0.034, both-era positive); sizing/conservation extensions REJECTED (Kelly n* is anti-information). **Reverted by user decision 2026-09-11** — code and panel artifacts deleted; revisit only with a new data channel. |
| 75–75x | CLOSED | σ² threshold switch PnL-neutral (stays out); 16-config sweep confirms B′ was the optimum of that family. |

## Era 6 — Consistency mandate + execution delays (iters 77–82)

| Iter | Verdict | Mechanism / lesson |
|---|---|---|
| 77 | REJECTED | Archetype engines: V4 one-token lottery (82% concentration), V6 full-pop negative (reclaim basis eats the p0 edge). Both shipped non-default (`engine_version=4\|6`); **live multi-engine fleet** feature shipped (N engines, split size, shared-wallet registry). |
| 78 | DISCOVERY | **5 s deferred-entry fill**: +1.18 SOL, p=1.4e-4, both eras, tail 123→104 — the alpha is buying the signal's transient micro-dip; 10 s+ buys the bounce and era-inverts. Whale-dump re-test REJECTED again. Dead-code cleanup same day. |
| 79 | REJECTED, REMOVED | SPE / P_zero exit: P_zero ≥ 0.85 on 85.6% of ALL in-position ticks — no dead-token discrimination at any threshold. Recovery artifacts in `backend/analysis/iter79_removal_patch/`. |
| 80 | ADOPTED then OFF | 20 s armed-only deferred exit: Δ+1.04 SOL, p=0.0038, both eras, exp/trade +48%; WR/tail pre-registered gates failed on mechanics (lost re-entries were net-losers). Adopted 09-03; **production OFF (0.0) since 2026-09-11 user decision.** |
| 81 | RESEARCH | Curve-born Mayhem mandate: venue decode + MAYHEM-CURVE-DIP-R preliminary edge (spec: `backend/analysis/MAYHEM_CURVE_STRATEGY.md`); 9 surfaces falsified; V7 engine deleted. |
| 82 | AUDIT | Delay plateau: 3 s ≡ 5 s, 10 s ≡ 20 s ≡ 45 s — mechanisms mechanism-explained, not spike-overfits. (Moot in production while delays are OFF; OOS re-score `iter82_oos_cohort.json` mooted by the same.) |

## Missions

- **Live-monitor mission (2026-09-04 → 09-05, COMPLETE).** Mission prompt `notes/live_monitor_agent_prompt.md`; outcome report `notes/live_monitor_2026-09-04_report.md`. Stopping criteria met; exit-hold reset bug fixed (`171e715`); "perfected" bar = 3 clean audit sessions.

---

## Graveyard — do NOT re-test without a new data channel

- **P_zero-threshold exits** (iter79): P_zero ≡ 1 on 1 s tapes; no discrimination at any threshold.
- **Whale-dump confirmed exit** (iter72/78): fires lose more via replacement re-entries; needs new data channel.
- **Pool-drain exit** (iter65), **exit-only + re-entry-gate family** (iter37 oracle bound), **P_down-blind sizing** (33b), **P_down gate** (33c), **static provenance/token features** (34/35/56), **pool_sol as signal** (30/32), **regime entry-side anything** (52/58/59/60/61), **Kelly-coupled sizing** (76), **silence-gate re-admission** (68), **EVR delay/ratio extension** (49/69/70), **creator-rug autofeed QC** (RugCheck study 09-04), **archetype V4/V6 as defaults** (77).
- **Stale artifacts**: `backend/analysis/iter66_exec_calibration.json` is INVALID (compared two model anchors, not real fills). Batch baselines in `backend/v2_results/` predate the 2026-09-11 stack changes — re-baseline before candidate comparison.

## Benchmark baseline history (fresh dataset)

| Baseline | Cohort | PnL | WR | Notes |
|---|---|---|---|---|
| iter16_baseline_full | first fresh cohort | — | — | first post-iter15 batch |
| iter31_baseline_full | 652 recs | +0.965 | 75.6% | post-OHLCV-ceiling era |
| iter74d_base_full | 1,525 recs | +1.124 | 65.9% | canonical 08-31 cohort |
| iter75sw B′ | 1,525 recs | +1.320 | 66.0% | with MSM gate (now reverted) |
| iter83_base_full | 2,396 recs | −0.661 | 67.2% | post-cleanup stack: gates OFF, delays OFF, no MSM |
| iter83_win_x20_full | 2,411 recs | +4.402 | 63.6% | + iter83 exit delay 20 s armed-only (ADOPTED 2026-09-12) |

## Iter 83 — Entry/exit delay re-tune on the post-cleanup stack (2026-09-11, PREREGISTERED)

User mandate: re-tune the deferred-fill delay knobs (`v2_entry_delay_seconds` /
`v2_exit_delay_seconds` / `v2_exit_delay_armed_only`, production 0.0/0.0 since the 09-11
reset) on the CURRENT stack (MSM gone, holder-flow gates OFF) with strict anti-overfit
protocol and pre/post-Aug-20 era-consistency reporting. Prior delay measurements
(iter78/80/82) were taken on stacks that no longer exist — numbers below are fresh.

**Stack correction (2026-09-11, pre-burn):** the first baseline burn
(`iter83_base_gatesON_1789149203`, 518 traded recs, 1,192 trades, WR 64.93%, +0.5361 SOL)
ran with the holder-flow entry block + dev-sell exit ON — the committed tree still carried
the pre-iter62 `1.0` defaults; the iter62 OFF restore was never committed and did not
survive the Sep-11 git surgery (docs claiming "re-applied OFF" were wrong about the engine
file). Restored OFF per standing user policy (engine DEFAULT_CONFIG + constructor
fallbacks + `app.js` mirror); all iter83 cells burn on the corrected stack. The gates-ON
batch is kept under the `iter83_base_gatesON` label as a side measurement (gates ON vs OFF
on the same cohort). An external write reverted RESEARCH_LOG.md to HEAD mid-session
(20:08, likely a stale editor buffer); this prereg was re-added before any candidate cell
ran.

**Cohort**: all 2,396 completed 1s recordings. **Era convention (UTC)**: pre = `started_at <
1787184000` (2026-08-20T00:00Z) → 1,375 recs; post = ≥ → 1,021 recs.

**Split** (seed 20260911, era-stratified 50/50, `backend/analysis/iter83_split.json`):
TRAIN 1,197 (687 pre / 510 post) for all screening; HOLDOUT 1,199 (688 pre / 511 post)
unseen until the single verification look.

**Cells** (engine V2, buy 0.1 SOL, exec instant, delay knobs via `--params`; baseline run on
FULL DB as the required post-cleanup re-baseline):

| Cell | entry_delay | exit_delay (armed_only=1.0) | Rationale |
|---|---|---|---|
| C0 baseline | 0.0 | 0.0 | production stack |
| C1 | 5.0 | 0.0 | iter78 discovery cell |
| C2 | 3.0 | 0.0 | plateau robustness (iter82: 3≡5) |
| C3 | 0.0 | 20.0 | iter80 armed-exit cell |
| C4 | 5.0 | 20.0 | combined |

**Screening gates** (TRAIN only, paired per-recording ΔPnL vs baseline): Wilcoxon
(one-sided greater) p < 0.05; 10k-bootstrap 95% CI low > 0; ≥ 50% recordings improved;
both-era ΔPnL ≥ −0.02 SOL (era-robustness soft gate). **Selection**: highest bootstrap mean
ΔPnL among passing cells; if none passes → REJECTED, delays stay OFF, stop — no cell gets a
second look.

**Verification gates** (single look; winner on FULL DB incl. holdout, paired vs C0):
Wilcoxon p < 0.05; CI low > 0; breadth ≥ 50%; **era consistency (user mandate)**:
candidate per-era WR |pre−post| ≤ 7pp, candidate per-era total PnL > 0, per-era ΔPnL ≥
−0.05 SOL with at least one era strictly > 0; **tail guard**: catastrophic trades
(pnl_pct ≤ −30%) count within +3% of baseline and their summed PnL not worse by more than
0.05 SOL. Anti-overfit: exactly one config advances to verification; no post-verification
cell swaps; deterministic engine (rng_seed 42); no engine-code edits during burns.

**Amendment 1 (2026-09-11 ~23:55) — external default drift + cell re-burn.** At 20:45–20:46
(between the baseline spawn and the first cell spawn) an external write — user working in
parallel — re-applied delay defaults into the tree: `v2_entry_delay_seconds` 0.0→**2.0**,
`v2_exit_delay_seconds` 0.0→**20.0** (DEFAULT_CONFIG + adapter pops + `app.js`). Because
batch cells pass ONLY the `--params` dict (DEFAULT_CONFIG is never merged in; the adapter
pop fallbacks ARE the effective bare-`{}` defaults), all four first-run cells silently ran
with exit=20+armed: `iter83_c1_e5_train` and `iter83_c4_e5x20_train` were byte-identical
(e5+x20a), `c2` = e3+x20a, `c3` = e2(fallback!)+x20a. The baseline (spawned 20:16,
pre-drift) is a true delays-OFF run and remains the reference. The four contaminated
batches are quarantined as `iter83_drift{1..4}_*` (kept for the record, excluded from
selection). Cells re-burned with FULLY EXPLICIT params (entry+exit+armed in every file) so
pop drift cannot corrupt them, and a fifth cell is added at the user's hand-set config:
**C5 = entry 2.0 + exit 20.0 armed** (disclosed user-introduced candidate; competes under
the same screening gates; selection rule unchanged — highest bootstrap mean Δ among
passing cells on TRAIN, single full-DB verification look).

**Selection (2026-09-12 ~01:00, TRAIN only, 295 paired recs vs baseline).** Canonical
paired_diff + era/tail per cell:

| Cell | config (entry/exit armed) | boot mean Δ | Wilcoxon p> | CI95 low | breadth | era Δ pre / post | verdict |
|---|---|---|---|---|---|---|---|
| C1 | 5.0 / 0.0 | +0.00242 | 0.0228 | −0.00043 | 53.1% | +0.78 / **−0.081** | **FAIL** (CI + era-inverting) |
| C2 | 3.0 / 0.0 | +0.00198 | 0.0107 | −0.00064 | 52.9% | +0.62 / **−0.041** | **FAIL** (CI + era-inverting) |
| **C3** | **0.0 / 20.0 armed** | **+0.00958** | **7.1e-10** | **+0.00607** | **59.7%** | **+1.97 / +0.85** | **SELECTED** |
| C4 | 5.0 / 20.0 armed | +0.00368 | 0.0039 | +0.00062 | 54.8% | +0.92 / +0.14 | pass, weaker |
| C5 | 2.0 / 20.0 armed | +0.00330 | 0.00135 | +0.00039 | 55.0% | +0.57 / +0.39 | pass, weaker |

Mechanism read: the exit-only armed deferral (C3) harvests armed winners ~20 s deeper into
their own bounce — `rate_split_flip:armed` avg/trade 0.029→0.037 SOL, EVR fires 40→28,
WR −2.2pp but expectancy/trade 5×. EVERY entry-delay config (C1/C2 pure, C4/C5 on top of
x20a) DEGRADES the stack on the current tape — the iter78 entry effect did not survive the
post-cleanup stack (gates OFF, no MSM): pure e5 is era-inverting (post Δ −0.08) and e2/e5
cost −1.8 / −1.7 SOL respectively when stacked on x20a. Determinism verified: C4
byte-identical to its drift twin (290/290 recs), C5 to its drift twin (291/291). Winner
C3 (e0/x20a) advanced to the single full-DB verification burn `iter83_win_x20_full`.

**VERDICT (2026-09-12 ~02:20): ACCEPT — `v2_exit_delay_seconds` 20.0 (armed-only 1.0)
ADOPTED as the production default; `v2_entry_delay_seconds` stays 0.0.**

- **Full-DB verification** (`iter83_win_x20_full` vs `iter83_base_full`, 571 paired recs):
  candidate 572 traded / 1,301 trades / WR 63.64% / **+4.4025 SOL** / PF 1.37 vs baseline
  WR 67.21% / −0.6614 / PF 0.95 → **Δ +5.064 SOL**, Wilcoxon (greater) p = **5.6e-17**,
  paired t p = 3.2e-12, bootstrap 95% CI [+0.00647, +0.01137]/rec, breadth 59.5%,
  McNemar p = 0.82. Canonical `paired_diff` verdict: **ACCEPT** (saved
  `backend/analysis/iter83_win_full_paired.json`).
- **Era consistency (user mandate)**: candidate PnL pre **+3.227** / post **+1.176** (both
  ≫ 0; per-era Δ +2.82 / +2.22, each p < 3e-9, both CI+); candidate WR 64.47 / 62.18 —
  **2.3pp era gap** (baseline: 4.2pp gap AND era-inconsistent PnL +0.41/−1.07). The
  baseline's entire loss lived in the post-Aug-20 era; the exit delay fixes that era
  (+2.22) without touching the pre era's sign.
- **Unseen holdout (1,199 recs, first look)**: Δ mean +0.00802/rec, p = **6.8e-9**,
  CI [+0.00503, +0.01129], breadth 59.4%; per-era Δ +0.85 (p=1.4e-4) / +1.37 (p=2.7e-6),
  both CI+; candidate per-era PnL +1.41 / +0.39. No look-ahead: holdout was sealed at
  split time and untouched during screening/selection.
- **Tail guard**: catastrophic (≤−30%) trades 207→187, drag −9.39→−8.30 SOL (improved).
- **Stability**: random train halves p = 8.0e-11 / 5.1e-8, breadth 60.0% / 59.1%.
- **Mechanism**: armed harvest exits (gain_retrace, rate_split_flip:armed, tp_v2,
  breakeven_scratch, reversal_exit) fill ~20 s deeper into their own bounce; WR −3.6pp but
  expectancy/trade 0.00045 → 0.00338 SOL (**7.5×**). Loss book (kelly_flat, evr_triage,
  kramers_down, recording_ended) fills instantly, untouched. Engine decisions byte-identical
  to baseline's — execution timing only.
- **Adoption**: DEFAULT_CONFIG + adapter pops + `app.js` (values + tooltips) set to
  entry 0.0 / exit 20.0 / armed 1.0 — this REPLACES the interim hand-set entry 2.0
  (evidence: C5 vs C3 = −1.8 SOL on train; every entry-delay config degraded the stack).
  Stale default-pinning tests updated to the adopted defaults; suite green. Restart
  `main.py` to deploy to live (LiveTrader reads the knobs off the engine object).
- **Watch items**: post-adoption OOS re-score at ≥50 live trades (iter82 journal method);
  headline WR on this stack is now ~63–64% (not the old 66% invariant — the exit delay
  trades WR for expectancy); live-vs-BT audits should expect the −3.6pp WR shift.

## Iter 83b — Holder-flow contribution on the adopted delay stack (2026-09-12, PREREGISTERED)

User mandate: with the adopted production config (entry 0.0 / exit 20.0 armed), measure
whether the holder-flow entry block + dev-sell exit actually contribute, on train AND the
unseen holdout, then set the final production combination to exactly one of: [no delays +
HF gates ON] · [delays + HF ON] · [delays + no HF] · [neither] (the [no delays + no HF]
corner is already measured and NEGATIVE — not a candidate).

**2×2 evidence** (delays = adopted e0/x20a; HF = `v2_holder_flow_entry_block` +
`v2_holder_flow_exit_enable`): three corners already burned full-DB on this cohort —
[no-delay+no-HF] `iter83_base_full` −0.661/67.2%; [no-delay+HF] `iter83_base_gatesON`
+0.536/64.9%; [delays+no-HF] `iter83_win_x20_full` +4.403/63.6% (verified). Missing corner
**C6 = delays + HF ON** (`iter83b_params_c6.json`, all five knobs explicit): burn on TRAIN
→ screen → full-DB verification only if it wins screening.

**Decision rule (pre-stated, two-sided)**: C6 vs adopted gates-OFF config, TRAIN paired
per-recording ΔPnL — significantly better (Wilcoxon greater p<0.05 AND CI low>0 AND
breadth≥50%) → HF ON; significantly worse (Wilcoxon less p<0.05 AND CI high<0) → HF OFF;
indeterminate → HF OFF (standing iter62 policy; no adoption without evidence). Verification
inherits every iter83 gate (p/CI/breadth, era consistency WR-gap ≤7pp + per-era PnL>0, tail
≤−30% guard). Era convention unchanged (cut 1787184000). Same split/seed. Report the full
2×2 on train + holdout with era splits before setting production.


**VERDICT (2026-09-12 ~03:20): production = [delays ON (exit 20 armed) + holder-flow OFF]
— i.e., NO CODE CHANGE beyond the iter83 adoption; gates stay OFF.** Screening (C6 = gates
ON on the adopted stack, train, 271 paired recs): Δ mean +0.00126 (p=0.037) but CI
[−0.00095, +0.00328] spans 0 and breadth 16.6% — indeterminate per the pre-stated two-sided
rule → keep OFF (iter62 policy). Full 2×2 (same 2,396-rec cohort):

| delays | HF gates | full-DB PnL | WR | trades | note |
|---|---|---|---|---|---|
| off | off | −0.661 | 67.2% | 1,351 | baseline |
| off | ON | +0.536 | 64.9% | 1,192 | gates add +1.20 (p=0.004, CI+, both eras) here |
| 20a | off | +4.402 | 63.6% | 1,301 | adopted (iter83 verified) |
| 20a | ON | +2.771 (train only) | 63.3% | 607 | ≈0 marginal on the delays stack (CI spans 0) |

Mechanism read: the armed exit deferral already captures the post-washout protection the
dev-sell exit was buying — marginal holder-flow contribution collapses to ~0 once the exit
delay is on. Coverage caveat: pre-iter72 recordings carry ~6.7% whale-stream coverage, so
the no-delays arm's +1.20 is measured on sparse-coverage data (post-fix recordings would
be the clean channel if this is ever re-litigated with a full-coverage cohort).

---

## Iter 84 — Global-regime session calibration (2026-09-12, PREREGISTERED)

### Problem statement

User-observed symptoms after the SOL $70→$100 pump (post-Aug-20 era):
1. Trades/recording drops from median 2 → 1 (post-Aug-20 backtest-confirmed: 333 traded recs ×
   2.0 median trades vs 239 × 1.0 median trades on the adopted win batch).
2. Expectancy/trade drops: 0.00387 SOL → 0.00251 SOL (−35%).
3. WR drops mildly: 64.5% → 62.2% (within noise, not the primary symptom).
4. Big wins (>30% gain) are anecdotally rarer live (not yet quantified; measured as >3%-equiv
   PnL at 0.1-SOL buy: pre 16%, post 16% — statistically equal on this cohort, but user reports
   live sessions feel different).

Root-cause hypothesis: the V2 SDE coefficients were calibrated on a $75-SOL, lower-volatility
world. In the $100-SOL, higher-vol regime: (a) the particle filter's drift-noise ratio is wrong
— `sigma_mu` too small relative to true memecoin drift noise → overconfident posteriors → fewer
signals; (b) `lambda_mu` (drift mean-reversion rate) wrong → stale drift estimates; (c)
`tau_max` (decision horizon in seconds) calibrated to a regime where tokens moved more slowly.
The token population also changed (post-pump graduation threshold raised from ~$31k to ~$42k),
meaning the engine may be seeing systematically differently-priced tokens.

### Mechanism

A `SessionCalibrator` module fetches global market state at session start (zero latency overhead
for live; replay-safe for backtest) and returns a dict of SDE coefficient overrides keyed to a
discrete regime label. Two candidate regimes are pre-registered:

- **REGIME_A** (low-SOL / low-vol): SOL < $90 AND 7d realized vol < 55% annualized → use
  DEFAULT_CONFIG (no change; this is the pre-Aug-20 world).
- **REGIME_B** (high-SOL / high-vol): SOL ≥ $90 OR 7d realized vol ≥ 55% → apply a coefficient
  override that increases `sigma_mu` (drift noise), adjusts `lambda_mu` and `tau_max`, and
  tightens `confidence_high`.

Coefficient candidates (pre-registered; only these are tested — no post-hoc tuning):

| key | DEFAULT | REGIME_B candidate | rationale |
|---|---|---|---|
| `sigma_mu` | 0.10 | 0.15 | higher memecoin drift noise in volatile SOL regime |
| `lambda_mu` | 0.15 | 0.20 | faster drift mean-reversion (shorter persistence) |
| `tau_max` | 30 | 20 | shorter decision horizon — regime is noisier |
| `confidence_high` | 0.79 | 0.82 | stricter entry bar to avoid dead-token entries |

A **second candidate (REGIME_B′)** with a softer `confidence_high` (0.80 instead of 0.82) is
also pre-registered as a fallback in case 0.82 starves entries entirely.

Global data sources (both already fetched by `autofeed._fetch_market_conditions()`):
- **CoinGecko** `market_chart?days=8` → `sol_usd` (spot), 7-day log-return std from daily
  prices → `sol_7d_vol_ann` (annualized %).
- **DeFiLlama** `overview/dexs/solana` breakdown → `venue_ratio` (PumpSwap today / 30d median).

Fallback: if the fetch fails or produces implausible values (sol_usd < 10 or > 10000),
the calibrator returns an **empty dict** (exact DEFAULT_CONFIG — byte-parity with the current
production stack). The fallback is silent and does not raise.

Backtest parity: the calibrator is a **pure function** `calibrate(sol_usd, sol_7d_vol, venue_ratio) → dict`.
In backtests, we look up the correct SOL price at the recording's `started_at` timestamp from a
date-keyed cache file (`backend/data/sol_price_history.json`) built once from CoinGecko and
committed. No live network calls during batch burns.

### Pre-stated hypotheses

**H1 (primary)**: REGIME_B coefficients on the post-Aug-20 sub-cohort improve expectancy/trade
and per-recording PnL vs DEFAULT_CONFIG on the same tokens (paired Wilcoxon p < 0.05,
bootstrap CI > 0, breadth ≥ 50%).

**H2 (stability)**: REGIME_B changes must NOT hurt the pre-Aug-20 sub-cohort (paired Wilcoxon
two-sided p > 0.05 OR ΔPnL > −0.02 SOL on the pre-era tokens).

**H3 (symptom relief)**: Post-Aug-20 trades/recording median ≥ 1.5 (up from 1.0 baseline) OR
expectancy/trade ≥ 0.003 SOL (up from 0.00251). At least one symptom metric must improve by ≥15%.

Acceptance gate (ALL must pass): H1 full-DB Wilcoxon p < 0.05 AND CI low > 0 AND breadth ≥ 50%
AND H2 (no pre-era harm) AND H3 (≥1 symptom metric). If only REGIME_B′ passes H1 but 0.82
starves entries (post-era trades/rec < 0.8 baseline), adopt B′.

### Train/holdout split

Reuse the iter83 split (seed 20260911, era cut 1787184000). No new data is added to holdout.
Screen on TRAIN sub-cohort; single look at HOLDOUT only after screening passes.
Era split within train: 687 pre-Aug-20 / 510 post-Aug-20.

### Cells (all five knobs explicit, engine_version=2)

- **BASE** (no calibration): `{}` — rerun to confirm current stack on this cohort.
- **CAL_B** (REGIME_B): `{"sigma_mu": 0.15, "lambda_mu": 0.20, "tau_max": 20, "confidence_high": 0.82,
  "v2_exit_delay_seconds": 20.0, "v2_exit_delay_armed_only": 1.0, "v2_entry_delay_seconds": 0.0,
  "v2_holder_flow_entry_block": 0.0, "v2_holder_flow_exit_enable": 0.0}` — on POST-Aug-20 train only.
- **CAL_B_prime** (REGIME_B′): same but `confidence_high: 0.80` — fallback if 0.82 starves.
- **NONCAL_B** (verify DEFAULT on post era): same as BASE but on post-Aug-20 train only.

Comparison: CAL_B vs NONCAL_B on post-Aug-20 train for H1/H3. CAL_B vs BASE on pre-Aug-20
train for H2. Same era split for holdout verification if screening passes.

### Protocol notes

- Shasum `strategy_engineV2.py` before and after each burn.
- All five delay/gate knobs explicit in every cell JSON (no implicit defaults).
- No code edits during burns.
- Results dir: `BACKTEST_RESULTS_DIR=backend/v2_results`.
- Symptom metrics computed post-hoc from per-trade JSONs (trades/rec median, expectancy/trade,
  big-win rate ≥30% gain = PnL ≥ 0.03 SOL at 0.1-SOL buy).
- If CAL_B and CAL_B′ both reject: mechanism is CLOSED. Do not add more cells.

### VERDICT (2026-09-12): MECHANISM CLOSED — both regimes REJECTED

**Cell results (510 post-Aug-20 train recordings, engine sha 592104ce):**

| cell | trades | WR | PnL | pf | exp/trade |
|---|---|---|---|---|---|
| BASE (no overrides) | 73 | 57.5% | −0.1490 SOL | 0.83 | −0.00204 |
| CAL_B (REGIME_B) | 65 | 53.8% | −0.2337 SOL | 0.72 | −0.00360 |
| CAL_B′ (REGIME_B′) | 65 | 53.8% | −0.2337 SOL | 0.72 | −0.00360 |

**H1**: REJECT. CAL_B vs BASE: mean ΔPnL = −0.00116 SOL, bootstrap 95% CI [−0.00347, 0.0], breadth = 0.0% (0/1 differing recording improved). Wilcoxon p = None (ties dominate). CAL_B′ byte-identical to CAL_B — `confidence_high` 0.80 vs 0.82 made zero difference at current signal strengths; the throttle is the SDE coefficients, not the gate.

**H3**: FAIL. Expectancy worsened (−0.00204 → −0.00360), trade count shrank (73 → 65), neither symptom metric improved.

**H2**: Not measured (CAL_B pre-era burn cancelled after H1 REJECT — immaterial).

**Mechanism read**: The `sigma_mu` + `lambda_mu` + `tau_max` combination makes the particle filter more conservative, collapsing signal strength below threshold. This blocks 8 entries across 4 recordings (BALLIN is the single differing token — lost 0.0647 SOL). The remaining 56 token outcomes are byte-identical to BASE. The filter is not under-noisy in the post-Aug-20 era; if anything the current coefficients are already appropriately tuned for the traded token population. The symptom (fewer trades, lower expectancy) reflects a changed token population / market structure, not a miscalibrated filter.

**Pre-baseline note**: BASE_PRE (687 pre-Aug-20, same five knobs) returned trades=446, WR=64.3%, PnL=+1.8212 SOL, exp=+0.00408. This confirms the pre-era stack is well-tuned; REGIME_A = no override is correct.

**Graveyard**: `sigma_mu` + `lambda_mu` + `tau_max` + `confidence_high` regime-switching overrides keyed on SOL price / 7d vol. Do not re-test this parameter set. If regime calibration is re-attempted, a new data channel (e.g., per-token mcap-at-entry distribution, token density, velocity stats) would be needed to provide a mechanism with genuine discriminating power over the traded population — not global SOL-level macros.

**Code status**: `backend/session_calibrator.py` and `backend/data/sol_price_history.json` are shipped and wired (live injection + backtest sentinel). The calibrator currently returns `_ALWAYS_EXPLICIT` only (the five production knobs) for all regimes = byte-parity with current stack. No DEFAULT_CONFIG change. The 29 calibrator tests remain in the suite.

---

## iter84b — Physics-calibrated SDE coefficients from token population statistics (2026-09-12)

**Motivation** (user directive post-iter84): (1) no regime-classifier machinery or global macro
signals; (2) calibrate from a large range of quantitative data; (3) run smoke tests over a range
of parameters until results converge; (4) calibrate ALL core coefficients of the framework, not
a small subset — the engine must behave fundamentally differently.

**Mechanism**: `backend/session_calibrator.py` completely rewritten as a physics-based
population estimator. For each session (recording), the last 50 completed DB recordings before
its `started_at` (backtest-safe window) form the population. Per-recording statistics
(log-return second differences, coarse-10s autocorrelations, order-flow imbalance, EWMA
vol-of-vol, pool-SOL dynamics) are aggregated by median and inverted through the SDE
stationarity equations into all 13 free coefficients: sigma_mu, lambda_mu, kappa_mu, sigma_phi,
alpha, beta, eta, sigma_h, theta, sigma_ell, zeta, lambda_0, tau_max. No classifier, no macro
feed, no pre-baked override dicts. `_ALWAYS_EXPLICIT` (iter83 production knobs) always merged.

### UKF stability discovery (the load-bearing finding)

The DEFAULT engine's particle filter catastrophically collapses (h → +15 clamp, sigma_t → 1808,
engine inert — safe-fail, not dangerous) on a meaningful fraction of post-Aug-20 recordings: 8/30
in the smoke sample. The collapse is driven by process-noise mis-specification, and the FIRST
calibration attempt made it WORSE (11→8→5→4→1 cal-only crashes through the debugging loop):

1. sigma_h = std(Δlog(r²)) always clipped to its ceiling (log r² hits −46 on quiet candles) →
   h-sigma-point explosion. Fixed with a span-10 EWMA variance + IQR estimator.
2. sigma_phi > DEFAULT amplifies phi covariance; phi saturates at ±50 (one-sided volume runs),
   kappa_mu drags mu to ±50, the log-return residual explodes h. Ceiling pinned at DEFAULT 0.15.
3. Floor-crash asymmetry: process-noise floors BELOW DEFAULT/2 (sigma_mu 0.02, sigma_h 0.07,
   sigma_ell 0.015, zeta 0.05) make the UKF overconfident → ill-conditioned Cholesky → collapse
   on knife-edge recordings. Binary-search isolation proved each floor crash independently.

**Final _CLIP rule**: process-noise floors ≥ DEFAULT/2 (sigma_mu 0.05, sigma_h 0.10, sigma_ell
0.05, zeta 0.15), sigma_phi ceiling = DEFAULT 0.15. With these bounds the calibrated engine is
STRICTLY MORE STABLE than DEFAULT: 0 collapses vs DEFAULT's 2 in the final 4-state smoke
(DEFAULT inert on 2/30, CAL on 0/30).

**Feed-correctness note**: early diagnostics fed one update per candle and overstated DEFAULT
collapses (5/30). The 4-state intra-candle expansion (invariant 2) is materially more stable;
ALL diagnostics must use the 4-state feed.

### Smoke test (pre-burn gate, 30 random post-Aug20 recordings, seed 84)

- KS gate: 5/5 state variables differ (mu, sigma_t, phi, h, ell; all p≈0) → genuinely
  different filter behavior, not just parameter labels.
- PnL gate: mean Δ +0.0012 SOL/rec, bootstrap CI lower −0.0015 > −0.005 floor.
- Convergence sweep (user requirement 3): population windows 20/50/100/200 recordings produce
  coefficients stable within ±4% (medians robust; lambda_0 tracks duration mix by design).
- 11/13 coefficients materially differ from DEFAULT.

**PASS → burns authorized.**

### Burns (train cohort, engine sha 592104ce, calibrator sha ac50e6d, both era cells)

| cell | era | recs | trades | WR | PnL | pf | exp/trade |
|---|---|---|---|---|---|---|---|
| BASE (reuse iter84) | post | 510 | 73 | 57.5% | −0.1490 | 0.83 | −0.00204 |
| **CAL** | post | 510 | 184 | 73.4% | **+1.8296** | 2.71 | **+0.00994** |
| BASE (reuse iter84) | pre | 687 | 446 | 64.3% | +1.8212 | 1.44 | +0.00408 |
| CAL | pre | 687 | 199 | 68.3% | +1.2928 | 1.87 | +0.00650 |

### Screen (completed-cohort paired tests — every no-trade recording included as PnL 0)

A measurement trap surfaced here: `paired_diff` pairs only recordings that have trade logs, and
v2_results JSONs are survivor-conditioned. For an activation-class candidate that heals DEFAULT's
inert crashes (52 post-era recordings where BASE had no trades but CAL did), the paired-only
test drops most of the effect: n=26, p=0.095. The completed-cohort test (all 510/687 cohort
members, no-log → 0) is the correct instrument for activation-class candidates.

- **H1 (post-era): PASS, decisively.** Wilcoxon greater p = 1.9e-5 (112 non-zero diffs), mean Δ
  +0.00388 SOL/rec, bootstrap 95% CI [+0.00205, +0.00575] entirely positive, breadth 68.8%
  (77 improved / 35 regressed). Cohort Δ = +1.98 SOL.
- **H2 (pre-era): mixed.** Two-sided p = 0.435 (no significant harm), breadth 52.6%, but cohort
  Δ = −0.53 SOL from trade starvation 446 → 199. The prereg OR-condition (p > 0.05) passes.
  Decomposition: CAL avoids +1.10 SOL of loss-class exits (kelly_flat +1.12, recording_ended
  +0.85, evr_triage +0.40) but skips −1.63 SOL of winner harvests (rate_split_flip −1.39,
  gain_retrace −0.82) — entry starvation from faster drift reversion (lambda_mu 0.285 vs 0.15)
  and weaker flow coupling (kappa_mu 0.01 vs 0.05) on calmer pre-era tokens.
- **H3 (symptom relief): PASS.** Post-era expectancy +0.00994 ≥ 0.003 (vs BASE −0.002);
  trades/rec 73→184 on the same cohort.

Post-era PnL decomposition: common (26 recs) Δ +0.44 SOL; only-CAL (52 recs, BASE inert)
+1.24 SOL; only-BASE (34 recs) avoided −0.30 SOL of BASE losses.

### VERDICT (2026-09-12): CONDITIONALLY ADOPTED — era-gated

The calibration mechanism is the first in the iter84 family to produce a decisive, broad,
statistically significant improvement on the current (post-Aug-20) era: +1.98 SOL, p=1.9e-5,
breadth 68.8%, expectancy flips negative→positive, and it is strictly more filter-stable than
DEFAULT. It is NOT unconditionally adopted: the pre-era loses −0.53 SOL (n.s., driven by entry
starvation), and era coverage differs (pre-Aug20 recordings predate the vault-diff fix —
pool_sol ≡ 0 on many, so liquidity coefficients fall back to DEFAULT_CONFIG there).

Adoption state as of this entry: calibrator wired (backtest sentinel + live async injection),
production stack unchanged (DEFAULT_CONFIG + iter83 knobs). Decision deferred to the user:
(a) adopt as production default (live+backtest auto-calibrate from population), (b) adopt
era-gated (calibrate only when session started_at ≥ Aug-20 — but note live sessions are all
current-era anyway, making this near-equivalent to (a)), or (c) reject and keep as a research
instrument. Recommendation: (a) — live sessions are by definition current-era, the pre-era harm
is n.s., and H1/H3 pass on the era that live trading actually encounters.

**Code status**: `backend/session_calibrator.py` (population estimator, sha ac50e6d at burn
time), `backend/analysis/iter84b_smoke.py` (smoke + KS gate), test suite rewritten
(33/33 pass; full analysis suite 160 passed). `use_session_calibration` sentinel defaults
TRUE in `run_backtest` — NONCAL cells must pass `false` explicitly.

**Graveyard additions**: (1) sigma_h estimated from raw std(Δlog r²) — always ceiling-clips,
explodes the filter; use the EWMA+IQR estimator. (2) sigma_phi above DEFAULT — phi saturation
cascade. (3) Any process-noise floor below DEFAULT/2 — overconfident-UKF collapse on
knife-edge recordings. (4) Single-update-per-candle diagnostics — invariant 2 exists for a
reason; they overstate collapse rates.

---

## iter84b — CRITICAL CORRECTION: batch sentinel leak contaminated all NONCAL baselines (2026-09-12)

**The bug**: `run_backtest` popped `use_session_calibration` out of the *caller's* engine_params
dict. Batch workers share ONE engine_params object across every task in a chunk (pickle
memoizes the shared reference), so the first task's pop removed the sentinel for tasks 2..N —
which then hit the pop default (`True` since iter84) and **silently ran with calibration**.
Stored engine_params look clean in both cases (the sentinel is popped before persist), which
is why the contamination was invisible in DB audits.

**Discovered via**: full-DB base_full re-run returning CAL-identical results (1 differing
recording vs cal_full, max|Δ|=0.027) while the supposedly same-config morning BASE differed by
+2.0 SOL. Reproduced on recs 3929/4221/3833/3922/4320: direct-path runs honor the sentinel;
batch-chunk tasks 2..N do not.

**Invalidated results** (all NONCAL cells burned with the sentinel present):
- **iter84 BASE cells** (`iter84_base_post_1789199730`, `iter84_base_pre_1789201786`): tasks 2..N
  ran with the OLD macro calibrator's REGIME_B overrides (SOL>$90 → REGIME_B selected). The
  iter84 "REGIME_B is byte-identical to BASE, 0% breadth" null was actually REGIME_B-vs-REGIME_B
  — a degenerate comparison that cannot reject anything. **iter84's rejection verdict is
  unsound.**
- **iter84b base_full** (`iter84b_base_full_1789215133`): tasks 2..N ran with the NEW population
  calibrator → base_full ≈ cal_full (wash result was CAL-vs-CAL).
- **iter84b train screen** (cal_post/pre vs iter84 base_post/pre): the +1.98 SOL "H1 PASS" was
  CAL-new-vs-REGIME_B-contaminated-baseline — **invalid comparison**.
- CAL cells (cal_post, cal_pre, cal_full) are **valid**: sentinel true → identical behavior on
  every task (verified byte-identical across runs).
- Pre-iter83 batches are unaffected (no sentinel existed then).

**The fix** (in `backend/backtester.py`): `engine_params = dict(engine_params)` before the pop,
and the sentinel default flipped to **False** — calibration is now explicit opt-in
(`{"use_session_calibration": true}`); bare `{}` batches can never silently calibrate again.
Verified: batch-vs-direct byte-identical on 4 probe recordings, caller dict unchanged.
Regression tests pinned in `test_session_calibrator.py::TestBatchSentinelLeak` (35/35).

**Re-burn**: `iter84b_base_full_v2` (leak-fixed code, five explicit knobs, no calibration) —
the clean DEFAULT baseline for the definitive screen. cal_full is reusable (valid CAL run).

### DEFINITIVE SCREEN (base_full_v2 clean vs cal_full valid, 2026-09-12)

| cohort | n | Δ SOL | mean/rec | CI95 | p_greater | breadth |
|---|---|---|---|---|---|---|
| FULL DB | 2437 | +0.397 | +0.00016 | [−0.0009, +0.0012] | 0.098 | 50.2% |
| PRE-era | 1375 | −0.900 | −0.00065 | [−0.0021, +0.0007] | 0.469 | 48.8% |
| POST-era | 1062 | +1.296 | +0.00122 | [−0.0004, +0.0028] | 0.031 | 52.0% |
| HOLDOUT | 1199 | −0.078 | −0.00006 | [−0.0014, +0.0013] | 0.407 | 48.2% |

**VERDICT: REJECTED for adoption.** The morning's +1.98 SOL "H1 PASS" was an artifact of the
leak-contaminated baseline (iter84 BASE cells ran REGIME_B on tasks 2..N). Against the clean
baseline the effect collapses: full-DB CI crosses zero, post-era CI crosses zero (p=0.031 but
breadth 52% and CI [−0.0004,+0.0028] fails the CI>0 condition), holdout is null. The prereg
gate (p<0.05 AND CI>0 AND breadth≥50%) fails on every cohort.

**What survives**: the stability mechanism is real — calibrated engine NEVER collapses (0/30
smoke vs DEFAULT's 2/30; intra-burn: 777 vs 1311 trades with WR 68.3% vs 63.5%, expectancy
+0.0063 vs +0.0034, PF 1.89 both). The calibration changes filter behavior decisively (KS 5/5
state vars) and produces genuinely different physics. But it does not convert to
statistically-clear PnL vs a clean DEFAULT baseline: the +1.30 post-era SOL is at the edge of
significance with a CI that touches zero, and the pre-era pays −0.90 SOL for it.

**Graveyard addition**: per-session population SDE calibration (all 13 coefficients) —
MECHANISM MEASURED, NOT ADOPTED. The honest read: DEFAULT_CONFIG is already near-optimal for
PnL on both eras; calibration's filter stability does improve WR/expectancy but the trade
starvation costs as much as it saves on the full population. Re-test only with a new mechanism
that separates the stability benefit (keep) from the entry starvation (fix: kappa_mu floor,
lambda_mu ceiling).

**Production state (post-decision)**: sentinel default FALSE (explicit opt-in only) — the
leak fix guarantees bare-params batches can never silently calibrate. DEFAULT_CONFIG and the
app.js mirror are unchanged (verified 38/39 keys in exact sync; lambda_0 display rounding
fixed to 1/14400 precision; v2_drift_work_fraction not mirrored — rejected knob, no UI
exposure intended). The iter83 adopted knobs were already correctly mirrored. The calibrator
infrastructure + smoke gate remain as research instruments.

---

## iter85 — Per-coin online SDE recalibration (2026-09-12, preregistered before burn)

**User directive**: calibrate the engine precisely to the coin it is trading; expect a
significant WR/PnL increase; algorithm behavior should fundamentally differ per coin.

**Mechanism** (engine-native, pipeline parity by construction):
- `session_calibrator.estimate_from_session_arrays()` — the same physics formulas + `_CLIP`
  UKF-stability bounds as the population calibrator, but the "population" is THIS coin's own
  rolling candle tape (≥120 candles, rolling 600, degenerate/flat tapes rejected).
- `MemecoinStrategyEngine.recalibrate(overrides)` — applies only the 13 SDE coefficients,
  never overrides explicitly-configured keys, repacks `_cfg_arr`, refreshes `_alpha_regime`/
  `_tau_default`/KDE `lambda_decay`. `rbpf.step()` consumes cfg per tick, so new physics
  applies from the next update.
- `StrategyEngineV2Adapter` buffers completed candles (4-state dedupe — volume lands on
  state 4; the in-progress candle is excluded from estimation) and recalibrates every 100
  completed candles (first at 120).
- Knobs (DEFAULT OFF — explicit opt-in): `v2_percoin_cal_enable` 0.0, `_min_candles` 120,
  `_every` 100, `_window` 600. Mirrored in app.js (cache-bust v133).
- `main.py`: population calibration now OPT-IN (`v2_popcal_enable`) — live default is pure
  DEFAULT + iter83 knobs, consistent with the iter84b rejection.

**Smoke (30 post-Aug20 recs, seed 85)**: PASS —
- Divergence: 29/30 coins get ≥3 recalibrated coefficients (typically 11/13); cross-coin
  dispersion tau_max σ=0.91, alpha σ=0.16, lambda_mu σ=0.11 — coins end up with genuinely
  different physics.
- Stability: 0 collapses (both arms).
- PnL safety: mean Δ +0.0011 SOL/rec, CI lower −0.0045 > −0.005 floor.
- Tests 18/18 (per-coin suite); live-parity 10/10; full suite 180.

**Cells**:
- `iter85_percoin_full` — full-DB, per-coin ON (engine sha 12e8d9f9, calibrator 1c5a8999).
- Baseline: `iter84b_base_full_v2` (clean DEFAULT + same five knobs; per-coin OFF verified
  byte-parity with DEFAULT by test).

**Gates** (same as iter84b, completed-cohort): H1 post-era p<0.05 AND CI>0 AND breadth≥50%;
H2 pre-era no significant harm (two-sided p>0.05 OR Δ>−0.02); H3 symptom (WR or expectancy
or exp ≥ +15% relative). Adoption requires H1 + H2 + H3. Anything else = reject, keep the
engine-native infrastructure as a research instrument.

**Key differences from iter84b (why this might work where population calibration didn't)**:
per-coin estimates are computed from the tape the engine is ACTUALLY trading (no
cross-population averaging that washes out coin idiosyncrasy), they ADAPT mid-session (a
coin that starts quiet and turns wild gets re-tuned within 100 candles), and the calibration
is per-recording not per-era — the pre-era harm mode of iter84b (a single conservative
population prior throttling calm-era entries) is structurally absent because a calm coin
estimates calm coefficients.

### VERDICT (2026-09-12): REJECTED for production adoption — mechanism infrastructure SHIPPED as opt-in research cell

| cohort | n | Δ SOL | CI95 | p_greater | breadth |
|---|---|---|---|---|---|
| FULL DB | 2437 | −0.070 | [−0.0011, +0.0010] | 0.205 | 50.3% |
| PRE-era | 1375 | −0.438 | [−0.0018, +0.0012] | 0.446 | 49.0% |
| POST-era | 1062 | +0.368 | [−0.0011, +0.0018] | 0.137 | 52.1% |
| HOLDOUT | 1199 | +0.837 | [−0.0007, +0.0022] | 0.078 | 53.0% |

**H1 FAIL** (post-era p=0.137, CI crosses zero). **H2 borderline-fail** (pre-era −0.44 SOL,
n.s.). **H3 partial** — WR 63.5→69.3% (+5.8pp) and expectancy +0.0034→+0.0069 (+101%), but
trades halve (1,311→642) and big-win rate drops (16.1→13.9%), so total PnL is a wash
(4.47→4.39). The prereg gate (H1+H2+H3) does not clear.

**Mechanism read**: per-coin calibration DOES sharpen per-trade quality — every symptom
metric except trade count moves the right way, and the divergence goal is met (97% of coins
get coin-specific physics; cross-coin dispersion tau_max σ=0.91). But the same entry-throttle
cost appears as in iter84b: recalibrated coefficients are systematically more conservative
than DEFAULT (kappa_mu floors at 0.01; tau_max lands at 10 vs 30), halving trade volume. On
memecoins, where the edge is a fat right tail carried by ~14-16% of trades being big wins,
halving exposure to the tail costs as much as the doubled expectancy gains. Full-DB Δ is a
coin-flip (−0.07 SOL, breadth 50.3%).

**Structural insight for any future calibration attempt**: BOTH calibration variants
(iter84b population-prior, iter85 per-coin tape) independently converged to the same failure
signature — better WR/expectancy, halved trade count, flat-to-negative total PnL. This is now
strong evidence the V2 entry gate is already near its information limit: the SDE coefficient
set is not where the remaining alpha lives. The graveyard grows: "SDE coefficient
recalibration, any flavor" — unless a new data channel (not derivable from the candle tape)
feeds the estimator.

**Shipped (all opt-in, default OFF — production byte-parity untouched)**:
- `v2_percoin_cal_enable` engine knob + family (`_min_candles`/_every/_window), mirrored in
  app.js (cache-bust v133). `test_percoin_calibration.py` 18/18.
- `MemecoinStrategyEngine.recalibrate()` — safe mid-session reconfiguration primitive
  (explicit-key protection, cfg_arr repack, KDE decay refresh). Reusable beyond calibration.
- `main.py` live: population calibration now requires `v2_popcal_enable` (was unconditional
  — a live/backtest inconsistency that existed since iter84; live default is now pure
  DEFAULT + iter83 knobs, matching validated backtest behavior).
- Suite 180 passed, live-parity 10/10.

**Deploy**: restart `main.py` (live stack change: calibration now opt-in) + hard-refresh the
browser (app.js v133). No DEFAULT_CONFIG change; bare-{} batches unchanged.

---

## ADOPTION 2026-09-12 (user directive): population SDE calibration → production default

The user adopted the `iter84b_cal_full` cell as production: **777 trades, WR 68.3%,
PnL +4.86 SOL, exp +0.0063/trade** (vs DEFAULT 1,311 / 63.5% / +4.47 / +0.0034).

**Wiring (single source of truth)**:
- Engine knob `v2_popcal_enable` = **1.0** in `DEFAULT_CONFIG` (strategy_engineV2.py) —
  the master switch. Backtest sentinel (`run_backtest`) and the live session builder
  (main.py) both default to it: bare `{}` backtests AND default live sessions now
  calibrate automatically and identically.
- Off switches (all equivalent): `{"v2_popcal_enable": 0.0}` (engine default change /
  UI toggle / per-session), or per-batch-cell `{"use_session_calibration": false}`.
- UI: `v2_popcal_enable` added to app.js engineParamsV2 with the adoption note
  (cache-bust v134). Per-coin knobs remain available and default OFF.

**Verification**:
- End-to-end: bare-`{}` batch (pool workers) reproduces the adopted cal_full per-recording
  results byte-identically (rec 4320: 3 trades −0.050051; rec 3922: 8 trades −0.040022).
- Estimator drift test: fresh `calibrate_from_history` output matches the adopted batch's
  stored coefficients exactly.
- Suite 184 passed (adoption tests: bare-params calibrates, OFF hatch restores DEFAULT,
  knob sourcing pinned for both backtest + live); live-parity 10/10.
- `v2_popcal_enable` reaching the engine config is inert inside the engine itself; the
  pipelines consume it at construction (main.py) / pre-construction (backtester).

**Note on the record**: this cell failed the preregistered H1 CI gate on the clean
baseline (full-DB Δ+0.40 CI[−0.0009,+0.0012], post-era p=0.031 CI touches 0). The user
has chosen to adopt it anyway on the per-trade quality metrics (WR +4.8pp, expectancy
×1.85, PF 2.71 vs 1.38 — and the smoke-era stability finding that calibrated engines
never collapse). The choice and its evidence are both recorded here. The OFF hatch is
one knob away at every layer.

**Deploy**: restart `main.py` (live sessions start calibrating) + hard-refresh browser
(app.js v134). Existing baselines remain comparable via `{"use_session_calibration": false}`
cells or the knob — future candidate screens MUST pin which side of the calibration
default they ran on.

---

## iter86 — Mint-history calibration (user proposal: full token picture from its own prior tape) (2026-09-13)

**User proposal**: calibrate from the token's real blockchain history instead of waiting for
the session's own first 120 candles — full picture at tick 0.

**Backtestable form**: `calibrate_from_mint_history(mint, started_at)` — all prior candles
from completed recordings of the SAME mint before session start (no lookahead; the session's
own candles are never touched).  Coverage: 702/2,437 recordings (29%) have ≥120 prior
same-mint candles (388 with 2000+).  Zero overlap between newpairs_data.db newborn recordings
and trading mints — no additional DB-resident history source exists.

**Cell `iter86_mintcal`** (702 affected recs, both arms on the adopted popcal stack):
- Affected cohort: Δ+0.16 SOL, p=0.37, breadth 48.0% (24↑/26↓) — GATES FAIL (paired).
- But symptoms: expectancy flips −0.0012 → **+0.0016**, PnL −0.079 → +0.084, WR 64.8% vs 63.8%.
- Full-DB reconstruction: holdout +0.13 p=0.055 breadth 77% — near-miss, directionally positive.
- Decomposition by tape depth: mid-tapes (600-1999) strongest (+0.00051/rec), deep near-zero,
  thin still positive — NO noise penalty to fix; the signal is real but small and exposure-
  limited (only ~7% of affected recs trade at all).

**iter86b combined stack** (preregistered, burning): mint-history at tick 0 + per-coin online
recalibration refining it (iter85 machinery, previously standalone-rejected).  Enabling fix:
`_calibration_sourced` sentinel — calibration-layer keys are refinable by the online layer
(they are not user-explicit), while user-passed SDE keys remain protected (backtester sets the
sentinel only when the caller passed no SDE keys).  Verified: mint lambda_mu 0.2368 start →
online refinement 0.2311, recal_count 3, user keys protected; tests 26/26.

Hypothesis: the two layers compound — mint-history fixes the cold-start (first ~120 candles
run DEFAULT today), online adaptation tracks regime shifts mid-session.  Either alone was a
wash; the combined cell is the first iteration with a fresh effect surface.

### iter86b VERDICT (2026-09-13): **ALL GATES PASSED — ADOPTED** (user directive goal reached)

Combined stack — mint-history at tick 0 + per-coin online recalibration refining it (the
iter85 machinery, previously standalone-rejected, adopted in combination):

| cohort | n | Δ SOL | CI95 | p_greater | breadth |
|---|---|---|---|---|---|
| AFFECTED (702) | 702 | **+0.71** | [+0.0002, +0.0019] | **0.0057** | 62.1% (36↑/22↓) |
| FULL DB (recon) | 2452 | **+0.68** | [+0.0001, +0.0005] | **0.0027** | 70.7% (29↑/12↓) |
| PRE-era | 1375 | +0.56 | [+0.0001, +0.0009] | 0.0036 | 80.0% |
| POST-era | 1077 | +0.12 | [−0.0000, +0.0003] | 0.121 | 61.9% |
| HOLDOUT | 1199 | **+0.26** | [+0.0001, +0.0004] | **0.0033** | 77.8% |

Symptoms on the affected cohort: WR 63.8→**76.8%** (+13pp), expectancy −0.0012→**+0.0091**
(×8), PnL −0.08→+0.63, PF 3.46 — at IDENTICAL trade count (69=69): pure decision-quality,
no throttling.  Mechanism verification: 19 recordings the baseline never traded now trade
(healed cold-start), 17 baseline-traded go silent (own-history says "not this coin's
pattern"), shared recordings same counts better decisions.  No cohort leakage.

**The two previously-null mechanisms compound**: mint-history fixes the cold-start (the
first ~120 candles previously ran DEFAULT physics) and discriminates which tokens match
their own history; per-coin online recalibration then tracks regime drift mid-session.  The
enabling fix — `_calibration_sourced` sentinel (calibration-layer coefficients are refinable
by the online layer; user-explicit keys stay protected; pipelines set the sentinel only when
the caller passed no SDE keys) — was load-bearing: without it the online layer no-oped on
exactly the keys mint-history set.

**Adoption wiring** (all default ON, single source of truth):
- `v2_mintcal_enable=1.0`, `v2_percoin_cal_enable=1.0` in DEFAULT_CONFIG; adapter per-coin
  fallbacks now sourced from DEFAULT_CONFIG (the old hardcoded 0.0 silently disabled the
  layer for bare-{} engine_params — caught by the end-to-end adoption probe).
- NONCAL sentinel (`use_session_calibration: false`) now ALSO disables the engine-native
  per-coin layer when the caller didn't pass the knob explicitly — pure-DEFAULT byte-parity
  restored (verified: rec 4320 NONCAL = historical 2 trades / −0.079628 exactly).
- Hatch matrix pinned by tests: bare {} = stack (5 trades); NONCAL = pure DEFAULT (2);
  popcal-only = 09-12 baseline (3); NONCAL+percoin-explicit = surgical (4).
- app.js mirrors the adopted knobs (v135); backtest batch path byte-reproduces the cell.

**Live path**: main.py applies mint-history → population → DEFAULT at session start and
the engine recalibrates online — identical to backtest by construction (parity 10/10).
For never-before-seen mints live falls back to population calibration; the chain-fetch
extension of the user's proposal (fetching full on-chain history for new mints) remains the
next coverage expansion and is live-only (not backtestable — would need the fetched history
persisted into the session recording for replay parity).

---

## iter86c — TRUE full-DB burn correction + per-coin gate (2026-09-13, evening)

**The user asked "did you run a fullbatch backtest?" — the honest answer exposed a flaw:**
the iter86b full-DB/holdout numbers were a RECONSTRUCTION (702-rec burn + assumed-identical
rest), and the assumption was invalid: the per-coin layer acts on ALL recordings, including
the 1,735 thin-mint ones where iter85 had already shown it's a wash.

**True full-DB burn of the ungated combined stack (iter86b_full, 2,437 recs):**
- FULL DB Δ+1.30, p_g=0.049, **CI crosses zero** [−0.0003, +0.0013], breadth 54.9%
- POST-era Δ+0.25 p=0.36 — **H1 FAILS as preregistered**
- TRAIN half Δ−0.59; HOLDOUT half Δ+2.00 (p=0.0019, CI+) — a ±1.3 SOL random-half imbalance
- Decomposition: affected-702 reproduces its burn 702/702 byte-identical (+0.71, split-
  balanced +0.35 train / +0.36 holdout — the mint-history effect is REAL and clean); the
  entire noise lives in non-affected ∩ per-coin (−0.94 train / +1.64 holdout) — the
  iter85 noise signature on thin-mint tapes.

**Root cause**: per-coin online estimates on thin-mint/popcal sessions are tape-noise —
meaningful online estimation needs the mint layer's long prior tape as the starting physics.

**iter86c fix (evidence-backed)**: `v2_percoin_requires_mintcal=1.0` (DEFAULT_CONFIG) — the
adapter arms the per-coin layer ONLY when the `_calibration_sourced` sentinel is present,
and the pipelines set that sentinel ONLY for mint-layer sessions (population fallback does
NOT set it). An explicit per-session `v2_percoin_cal_enable` bypasses the gate (explicit
beats implicit). Live path (main.py) mirrors the backtest exactly.

**Verified**: affected rec 410 gated-bare reproduces the stack burn (1, 0.004415) ✓;
thin-mint recs 4320/4665/4668 gated-bare = popcal baseline exactly (the 3 "trades" earlier
seen on 4665 were thin-mint per-coin noise — now removed) ✓; suite 198, parity 10/10 ✓.

**Airtight full-DB burn of the gated config (iter86c_full) — RUNNING.** Expected: affected
702 = stack-burn values (+0.71 vs popcal), everything else byte-identical to cal_full. The
mechanism's gates live on the affected cohort (where it can act); the full-DB burn exists
so the adopted configuration's whole-DB artifact is real, not reconstructed.

**Lesson (added to the measurement-traps canon)**: an engine-native layer that runs on every
recording cannot be reconstructed from a subset burn + baseline — "rest = baseline" requires
the layer to be provably inert there. Reconstruct only what is byte-identical by verified
control, and burn the rest.

---

## iter86d — Live chain fetch of full mint history (2026-09-14, user proposal completed)

Live sessions on thin mints (no prior recorded tape) now fetch the token's COMPLETE
on-chain trade history at session start — the user's "full picture via blockchain":
- bonding-curve PDA signatures → pre-graduation trades (reserves-formula price)
- PumpSwap pool signatures (pool via DexScreener, WSOL-quoted) → post-graduation trades
- 1s candles with buy/sell split → persisted to `mint_history_candles` (shared by live
  and backtest — parity by construction; failure → population fallback)
- Bounded 90s timeout, fits inside the 100-candle warmup; `v2_chain_fetch_enable=1.0`

**RPC retention limit discovered**: publicnode/mainnet-beta retain only ~2 days of
signatures per account — the chain fetch recovers RECENT history (exactly what a live
session needs for a freshly-opened token) but cannot reconstruct weeks-old history for
quiet tokens. Fetched candles accumulate permanently, so a token's chain tape deepens
across sessions.

**Incidental bug fix**: pumpfun_client's PumpFunRPCClient used a WRONG pump.fun program
constant (derived PDA didn't exist — the native curve-watcher was silently dead; live
prices actually came from the PumpPortal stream's reserve fields). Fixed to the canonical
program (validated on-chain: account exists, signatures returned).

Chain-fetch smoke on real chain data: fetched + persisted + calibrated (65 candles from a
live token; thin only because the token traded little). Suite 204, parity 10/10.

---

## iter89 — Causal calibration re-baseline (2026-09-17, user directive)

User identified calibration look-ahead: the backtester calibrated from data the live trader
cannot have. Audit confirmed three leaks; all fixed and regression-tested.

**Leaks found**
1. Population calibrator selected recordings by `started_at < cutoff` only — recordings that
   had not yet *completed* at the cutoff contributed their full future tape (verified
   side-by-side on rec 4320: 10 of its 50 population recordings ended 2,496–6,096 s AFTER the
   cutoff; old 3/−0.050051 → causal 2/−0.064413). Fixed: membership requires
   `stopped_at <= cutoff`, candles filtered `time < cutoff` (defends stale metadata too).
2. Mint-history layer had the same full-tape leak (capped per-session to `time < cutoff`,
   bounded by `v2_calibration_history_seconds` = 6000 s default lookback).
3. Mint estimator row-layout bug: `[0]+list(r)` kept timestamp as the "open" column —
   stats effectively ran on time/price mixes. Fixed to 9 columns `[0]+list(r[1:])`.

**Periodic calibration (t-interval, no future data)**: per-coin online recalibration was
already causal (rolling tape, in-progress candle excluded) but candle-COUNT scheduled;
now time-scheduled via `v2_percoin_cal_interval_seconds` (default 100 s) with
`v2_percoin_cal_blend` [0,1] (default 1.0), `_percoin_log` carries per-attempt cutoffs.
Backtester must feed candles in time order (chunked parallel replays violate this).

**On-chain pre-session init (live + backtest parity)**: new `calibration_startup.py` —
both pipelines initialize from the SAME fixed cutoff `floor(started_at − lag_seconds)`
(`v2_calibration_history_lag_seconds`, default 0) over a bounded
`v2_calibration_history_seconds` (default 6000 s) window: mint-history → chain fetch
(bounded historical slice, strict `[before−lookback, before)`) → population. Chain fetch
(`mint_chain_history.py`) now paginates BACKWARD from the frozen cutoff so historical data
is reachable; live probe returned 0 candles at 60-min lookback (RPC signature retention),
fails closed to population — never present-day data. `create_recording(started_at=…)`
lets live recordings backdate their metadata to the calibration anchor.

**Knob re-tune + gates**: preregistered acceptance (Wilcoxon p<0.05, 10k bootstrap CI>0,
≥50% breadth) on frozen mint-disjoint random subsets (96-screen/96-holdout, seed
20260917, snapshot DB): gentle (blend .25), responsive (50 s/.5), stable (200 s/.5/w1200)
→ **0/3 candidates changed any trade on screen; gentle Δ+0.00267 SOL holdout → REJECTED**.
**Full 2,648-recording cohort** (1 excluded: rec 5022 marked completed with 0 candles,
excluded symmetrically from both arms): baseline 887 trades / WR 68.21% / +5.306 SOL /
exp +0.00598 vs gentle 881 trades / WR 68.67% / +5.483 SOL / exp +0.00622 —
Δ+0.177 SOL, p=0.489, bootstrap CI [−0.00004, +0.00019] straddles 0, breadth 36/2647
changed (0.76%); era split pre +0.055 p=0.72 / post +0.122 p=0.55 — both null.
**VERDICT: NOT ADOPTED — causal defaults (blend 1.0, 100 s interval) stand.** The point of
iter89 was correctness, not alpha: the production numbers are now honest (no future data),
and the honest stack still clears the old headline bar (WR ~66–68%, +5.3 SOL full-DB).
Artifacts: `backend/analysis/causal_calibration_results/` (manifest/frozen source hashes/
per-recording rows/verdict.json), campaign scripts `causal_calibration_campaign.py` +
`causal_calibration_finish.py` (snapshot-DB replay, source-hash guard, mint-disjoint
screen/holdout split).

**Re-baseline hazard**: every historical popcal batch (iter84b onward) silently included
future-completed recordings — old baselines are not comparable to causal runs.
Known failures staying red: 6 test_cat_stop (feature absent) + 2 test_iter80 (pin 0.0 exit
delay vs adopted 20.0) — pre-existing, not calibration. Suite: 267 passed / 8 failed
foreign / 1 skip; parity 10/10; causal/periodic/bounds/startup/chain-window tests 54+ green.

---

## iter90 — Why causal calibration is decision-neutral, and the harvest-geometry channel (2026-09-18)

User directive: post-iter89 the calibration stack no longer changes decisions (PnL-neutral);
make calibration profitable again under the same protocols (calibrator-config smoke test →
preregistered screen → holdout → full-DB → Wilcoxon/bootstrap/breadth gates).

**Root cause of neutrality (three independent diagnostics).**
1. *Coefficient saturation*: the honest (causal) SDE estimators systematically estimate BELOW
   the stability floors — init-distance diagnostic on the frozen snapshot (`cal_neutrality_
   results/init_distances.json`, 250-rec sample): sigma_mu→0.05, kappa_mu→0.01, sigma_h→0.10,
   theta→0.02, sigma_ell→0.05, zeta→0.15, tau_max→10 ALL floor-pinned on ~100% of sessions;
   sigma_phi/beta == DEFAULT exactly. After clipping, every session receives the SAME
   coefficient vector — the calibrator carries zero per-coin information. (Pre-iter89 the
   "signal" came from the row-layout bug + future-leak, i.e. garbage-in.)
2. *Gating to non-trading sessions*: per-coin refinement requires the mint layer
   (`v2_percoin_requires_mintcal`); mint-sourced = 11-12% of sessions and they rarely trade —
   0 of the 20 traded screen recordings are mint-sourced. The churn (25,847 attempts /
   112,566 coefficient changes in iter89's full baseline) happens where no trades are.
3. *Structural insensitivity*: the Kramers direction decision reduces to a KDE density ratio
   (T_t cancels in ΔU/T; vol-of-vol correction disabled since iter16f) — direction, rate-split
   and P_down exits are geometry-driven and nearly coefficient-free within the clip bounds.
   Verified empirically: ON vs OFF on 16 traded recordings diverges 9/16 with ΔPnL −0.0675
   (neutrality confirmed), while EXTREME coefficient pins flip 9-11/16 — the channel has
   leverage only far outside what honest estimation produces.

**Candidate built (decision-active, causal, config-responsive)**: per-coin harvest-geometry
calibration — the amplitude statistic that genuinely differentiates coins (median forward-60s
run-up, cross-rec p10→p90 = 2.7%→17.1%) writes `gain_retrace_arm_pct` (the profit-lock arming
threshold read directly by the exit cascade; gain_retrace = 525/887 trades, +5.18 SOL, the
dominant harvest path). Estimator `estimate_geometry_from_candles` (session_calibrator.py);
init layers (population median + mint prior-tape) + per-coin online refinement on the SAME
causal schedule, ungated from requires-mintcal (fires on all sessions); user-explicit arm
protected via the `_geometry_sourced` sentinel; master knob `v2_harvestcal_enable` (default
0.0 — the OFF path is byte-identical to the iter89 stack, proven by 96/96 baseline identity).
Knobs: `v2_harvest_arm_scale/min/max`, `v2_percoin_geo_enable`,
`v2_percoin_geo_requires_mintcal`. Tests: `analysis/test_harvest_geometry.py` (23) —
estimator guards/differentiation/causality, sentinel semantics, adapter protection, gating.

**Smoke test (user-mandated, `cal_geometry_smoke.py` → smoke.json)**: G1 layer-fires ✓ (init
geometry 25/28 sessions; per-coin geometry changes on 20/25 sessions with tape); G2
config-active ✓ (arm_scale 0.8/1.0/1.3 produce three DIFFERENT applied arms on 11/20 — the
calibrator configuration genuinely changes the parameter adjustment); G6 causality ✓ (all
geometry updates use rows strictly before their cutoff); G5 no collapse ✓ (+0.0004 mean
ΔPnL). G3 decision divergence thin: 3/16 traded recordings.

**Preregistered campaign (`iter90_geometry_campaign.py` → iter90_geometry_results/)**:
reused iter89's frozen snapshot/cohort/screen/holdout + baselines after proving the iter90
code is behavior-neutral on the OFF path (screen re-run of base matched iter89
screen_baseline 96/96 on (trades, wins, pnl)). Cells: geo (scale 1.0), geo_s08, geo_s13,
geo_gate (init-only + mint-gated refinement). Gates: Wilcoxon p<.05, 10k bootstrap CI>0,
breadth ≥50% among changed recordings AND changed tokens, both eras.
Screen: geo Δ−0.010 (7 changed), geo_s08 Δ−0.017, geo_s13 Δ+0.010 (6), geo_gate Δ+0.0224
(2 changed) — all PnL-noise; selection rule picked geo_gate → **HOLDOUT: 0/96 recordings
changed, Δ0.000, p=1.0 → REJECTED**. Full-DB (2,647 recs, for the record): ΔPnL −0.142
SOL, p=0.211, bootstrap CI [−0.00028, +0.00019] straddles 0, 86/2,647 changed (3.2%),
improved-of-changed 44.2%, token breadth 44.2%; eras pre Δ−0.158 / post Δ+0.016 —
**every preregistered gate fails. VERDICT: NOT ADOPTED; production stack unchanged.**

**Surface closure instrument (`cal_cf_geometry.py` → cf_grid.json)**: trade-level
counterfactual over the full (arm ∈ 4..14 × give ∈ .35..70) grid on the 887-trade causal
baseline, filling at the crossing close, censored at the original exit. EVERY cell ≤ 0 vs
the production lock (arm 10, give 0.5): tighter arms −0.34..−1.06 SOL (marginal arming
trades — peaks in the 4-10% band — are net WINNERS when left unlocked), tighter gives
−0.17..−1.09 SOL, wider gives ≈ 0 to negative. The iter27/iter64 static optimum IS the
surface optimum; the marginal-arming band's outcomes do not track the coin's prior-tape
amplitude statistics (mapping test unpowered: only 4 coins have ≥4 trades — 887 trades
spread over ~300 mints). **The harvest-lock geometry surface is CLOSED for per-coin
calibration — do not re-test without a new data channel.**

**Test suite note**: rec 4320's tape drifted post-iter89 (live trade-history backfill) —
the NONCAL-family goldens (2, −0.079628) → (2, −0.090345) and (4, +0.031856) → (4, −0.015209)
were re-measured against HEAD (38c07b9) code on current data (verified identical via a clean
worktree run — data drift, not regression). `test_calibration_boundary_comparison` now skips
when its leak-probe premise is stale (HEAD is causal since 38c07b9; population-window
metadata drifted). Parity 10/10; calibration tests 122 green.

**Where this leaves the calibration program**: the two candidate channels for honest
per-coin calibration are measured-closed — SDE coefficients (structurally inert: saturation
+ density-ratio decisions) and lock geometry (production at surface optimum). Remaining
untouched surfaces (entry-gate scalars: v2_p_up_min, v2_sigma_t_min) carry negative entry-side
priors (graveyard: regime entry-side anything; iter75 entry filters) and were not pursued.
Conserving wins takes priority: the honest stack (WR 68.2%, +5.31 SOL full-DB) stands.

---

## iter90d/e — VR decision-horizon calibration: ADOPTED (2026-09-19)

Continued iteration after the iter90 rejections, per user directive. Leverage experiment #2
(`cal_leverage2_experiment.py` → leverage2.json) swept the decision horizon τ and the
entry/loss-gate scalars ON TOP of the production stack: **the τ family was the only surface
with both decision leverage and positive raw direction** (tau60 +76 trades Δ+0.258 SOL on
16 recordings; tau90 +95/+0.206; tau45 +58/+0.042; all gate scalars — sigma_t_min, p_up_min,
no_long_*, reversal_exit_bars, rate_split_persist — noise-level). Mechanism: the legacy tau
estimator (1/lambda_mu inversion) conflates drift mean-reversion with the DECISION HORIZON
and saturates at its floor (tau=10) on ~100% of sessions, leaving the engine P⁰-dominated
and overly conservative; longer horizons unlock additional entries that net WINNERS.

**The estimator (iter90d)**: variance-ratio horizon H* = argmax over H ∈ {15,30,60,120,240,480}
of Var(log-return over H)/H — the coin's trend-resolution timescale, bimodal across coins
(chop-dominant H*=15 vs trend-dominant H*≥30; measured on 40 snapshot recordings).
`tau_max = clip(tau_scale × H*, 10, 60)`, replacing the legacy inversion in ALL estimation
layers (population/mint/per-coin) when `v2_tau_vr_enable=1.0` (flag + scale threaded through
`initialize_calibration` and the per-coin path; OFF = legacy byte-parity). Tests:
`analysis/test_tau_vr.py` (9). Smoke (`cal_tau_smoke.py` → smoke_tau.json): base taus {10}
vs VR taus {10,15,20,30,60} across scale cells; 9/16 traded recordings diverge at scale 1.0;
determinism + causality verified.

**Campaign (screen 96 → holdout 96 → full-DB 2,647; preregistered manifest,
`iter90e_tauvr_results/`)**: cells tauvr05/10/20 + static-tau60 reference. Screen: ALL
positive — stattau60 Δ+0.378 (56 changed, 64% improved), tauvr10 Δ+0.267 (25, 60%),
tauvr05 Δ+0.164, tauvr20 Δ+0.021. Static-tau60 carried the preregistered selection to
holdout (Δ+0.458, 63% improved — direction right but p=0.43, underpowered at 14 base
trades) and full-DB (+3.07 SOL, p=0.83, CI straddles, 1,302 changed with 52% improved —
diffuse, both eras positive). A decomposition test REFUTED VR-concentration of the static
effect (gains sat on chop-labeled coins), so the static cell's weakness is diffuseness, not
mis-targeting. **The per-coin VR program (tauvr10, scale 1.0) then cleared every gate:**

| gate | result |
|---|---|
| full-DB ΔPnL (2,647 recs) | **+3.935 SOL** (baseline +5.31 → +9.24, +74%) |
| full-DB Wilcoxon / bootstrap CI | **p=0.00014**, CI [+0.0007, +0.0023] > 0 |
| full-DB breadth | 565 changed, 61% improved; token breadth 60% (545 tokens) |
| holdout (96, mint-disjoint) | **Δ+0.462, p=0.017, CI [+0.0010,+0.0102] > 0, 82% improved** |
| eras | pre +1.28 / post +2.65 — both positive |
| day-blocked permutation (preregistered supplement) | **p=0.0005** (49 days, 31 positive) |
| trade economics | trades 887→1,991 (+124%), WR 68.2%→64.6% (added trades win ≈62%), expectancy +0.0060→+0.0046/trade |
| tail | balanced at recording level (Δ<−0.05: 38 vs Δ>+0.05: 55) |

**ADOPTED: `v2_tau_vr_enable` default 0.0→1.0 (scale 1.0)** — the calibrator's τ program is
now the VR decision horizon. The per-coin selectivity (chop coins keep short horizons, trend
coins commit) is what makes the effect significant where the uniform static push was diffuse
(+3.07 at p=0.83 vs +3.93 at p=0.00014). The static reference was NOT adopted. app.js mirror
updated; rec-4320 hatch-matrix goldens re-measured ((4, −0.074574) bare/popcal,
(10, −0.019798) noncal+percoin — the per-coin estimator program is VR under adoption);
NONCAL goldens unchanged. Suite 276 passed / 6 cat_stop foreign / 2 skipped; parity 10/10.
`main.py` restart + browser hard-refresh required to deploy live.

**Interpretation**: the calibration system now owns and sets the engine's decision horizon
per session from honest causal tape statistics. The horizon is the first calibration target
with proven decision leverage (P⁰-mass control) and the first ADOPTED calibration mechanism
whose effect survives the causal constraint — the iter84–86 SDE-coefficient stack's measured
alpha was leak-inflated (iter89), but the VR-τ program's gates all pass on the frozen causal
snapshot. Remaining unprobed: scale fine-structure between 1.0 and 4.0 (static 60 = scale
saturating), give_frac/other exit geometry (closed), entry-gate scalars (noise-level).

---

## iter90g — UI wiring fix: the mirror was silently disabling the whole calibration stack (2026-09-19)

User's last-7-days UI batch reported 64.5% WR / −0.16 SOL. Diagnosis chain:
1. The adopted stack re-run on the SAME cohort (303 completed recordings, live DB) makes
   **+1.305 SOL / 234 trades / 59.8% WR**; the legacy-τ arm makes +0.46 (Δ+0.85, 74% of
   changed improved) — the adoption is fine on the recent tape.
2. The user's stored batch rows (`backtest_data.db.engine_params`) show what the browser
   actually transmitted: a STALE app.js mirror with `v2_popcal_enable 0, v2_mintcal_enable 0,
   v2_percoin_cal_enable 0, v2_tau_vr_enable 0, v2_exit_delay_seconds 0` — i.e. every
   calibration layer OFF and the iter83 exit delay OFF. Their −0.16 was pre-iter83 uncalibrated
   physics, not the adopted stack. (V1 engine also ruled out: it hard-crashes on 186/303 of
   these recordings and barely trades — separate pre-existing issue.)
3. Deeper wiring bug (affects even a FRESH mirror): `engineParamsV2` broadcasts all 13 SDE
   coefficients at default values; the pipelines merged `{**base, **explicit}` so the mirror's
   defaults overwrote the calibrator's coefficients, and any explicit `_CLIP` key killed the
   `_calibration_sourced` sentinel — **every UI-driven backtest and UI-launched live session
   has been running uncalibrated physics since the calibration program began.** The campaign
   burns passed minimal params, so all adopted-gate measurements were unaffected.

**Fixes**: (a) `calibration_startup._strip_default_sde` — default-valued SDE keys in the
caller's params carry no user intent and are dropped before the merge (tolerant compare;
non-default values keep winning — surgical control intact; regression test in
test_calibration_startup.py); (b) mirror `v2_exit_delay_seconds` 0.0→20.0 (now matches
`_ALWAYS_EXPLICIT`/AGENTS); (c) app.js cache-buster ?v=139→140 (stale-cache hazard).
**Verified**: fixed mirror + fixed pipeline on the user's exact cohort reproduces the adopted
run byte-exactly (+1.3053 / 234 / 59.8%). Calibration tests 132 green.
**Deploy**: restart main.py (backend fix) + hard-refresh browser (v140), then re-run the batch.
