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
