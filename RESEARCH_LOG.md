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
| Entry delay | **0.0 = OFF** (was 5.0, iter78) | user decision 2026-09-11 |
| Exit delay | **0.0 = OFF** (was 20.0 armed-only, iter80) | user decision 2026-09-11 |
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

