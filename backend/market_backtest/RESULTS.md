# Market Backtest Systems — Results (V2M clone, calibration campaign)

**Branch**: `market-backtest-systems` · **Date**: 2026-10-18
**Memecoin engine**: untouched (`strategy_engineV2.py` sha `7ae0aa73…` verified
at every batch run; clone diff = docstring + `zeta` default only).

## What was built

- **`strategy_engineV2M.py`** — structural clone of the V2 engine for market
  tapes (`MarketStrategyEngine` / `StrategyEngineV2MAdapter`). Fork delta:
  `zeta = 0.0` default (ℓ jump-decay destabilized multi-year tapes; root-caused
  on AAPL: posterior pinned at every clamp after the first 3×-volume day).
- **`market_calibration.py`** — causal per-tape calibration: per-intake
  coefficient rescale, KDE memory in intake units, interval-class τ
  (1d→40, 1h→20; τ≥60 lets the Kramers down-escape dominate hourly exit books),
  posterior-σ entry gate (0.35×p10 from a bounded throwaway replay).
- **`runner.py`** — full-tape replay via ForwardTester (4-state expansion,
  complete decision stream, `recording_ended` force-close) with **train/test
  split**: engine params frozen from the TRAIN prefix only
  (`calib = candles[:split_idx]`), stats partitioned by entry time.
- **`stats_report.py`** — pooled per-trade bootstrap CI + Wilcoxon signed-rank +
  symbol breadth (binomial); portfolio equity in two accountings (see
  *Accounting* below); FTMO-classic prop-rule simulation (deterministic +
  2000-sim block-bootstrap MC at k ∈ {0.25…5}) and funded-phase 1-year
  survivorship sims.
- **`sweep.py` / `show_sweep.py`** — train→test calibration sweep harness
  (cells = constant knob overrides on top of per-symbol calibration; results
  include per-exit-reason PnL and counts; **selection by account Σ** — the
  metric was corrected mid-campaign from per-trade mean, which misleadingly
  penalizes entry-restricting cells; train Σ is the coherence check).
- **`prop_frontier.py` / `prop_bound.py` / `market_paired.py`** — requirement
  frontier (mean-shift + perfect-daily-stop), perfect trade-level stop bound
  (with funded survivorship), paired significance tooling.
- **`campaign_summary.py`** — campaign-wide ranking of every swept cell by
  test Σ (arm14 currently first; see the arming section).

## Data

| tape | symbols | history | bars/symbol | train | unseen test |
|---|---|---|---|---|---|
| stocks 1d | 67 (mega→broad factors) | 10y (2016-10 → 2026-10) | 2513 | ~70% (→ ~2023-10) | ~3.0y |
| perp 1d | 44 USDT-M perps | 2020-01/2023-06 → 2026-09 | 1.0–2.5k | ~70% | ~2.8y max |
| perp 1h | 44 USDT-M perps | 2020-01/2023-06 → 2026-09 | 25k–59k | ~70% | ~2.8y max |

Sources: Nasdaq API (split+div-adjusted daily), Binance Vision (real taker-buy
split). DB: `backend/data/market_data.db` (isolated; `git`-ignored data dir).

## Headline results (UNSEEN window)

| market | trades | WR | PnL (booked) | portfolio (ledger) | per-trade mean | 95% CI | Wilcoxon p | breadth |
|---|---|---|---|---|---|---|---|---|
| stocks 1d | 174 | 69.0% | −0.067 | −0.067 (−2.2%/yr) | −0.038% | [−0.149, +0.059]% | 2.2e-4 | 27/67 (p=0.14) |
| perp 1h | 2933 | 51.8% | −5.60 | −5.60 (booked-out) | −0.191% | [−0.215, −0.167]% | 7.4e-5 | **0/44** |
| perp 1d | 58 | 25.9% | −0.222 | −0.222 (−8.0%/yr) | −0.382% | [−0.784, −0.018]% | 2.8e-3 | 6/44 |

- **Every unseen-window per-trade mean is statistically ≲ 0** (perps: CI fully
  below zero, breadth 0/44 hourly). Win rates are high but the loss tail is
  ~4× the win size (giveback exits vs slow state-based loss book).
- Train segments agree in sign (stocks train PnL −0.203, WR 67.4%; perps 1h
  −6.71, WR 48.4%) — no train→test regime flip; the bleed is structural.

### Calibrated headline (`arm14` — the campaign's best configuration)

One calibration constant moved (`gain_retrace_arm_pct` 3.0 → 14.0; see the
arming section for the full dose evidence):

| segment | trades | WR | PnL (booked) | portfolio (ledger) | per-trade mean | 95% CI | Wilcoxon p | breadth |
|---|---|---|---|---|---|---|---|---|
| stocks test | 113 | 77.9% | +0.326 | **+0.326 = +10.9%/yr** | **+0.289%** | [+0.065, +0.498]% | 5.6e-06 | 31/67 |
| stocks train (IS) | 194 | 73.7% | +0.367 | — | +0.189% | [+0.031, +0.343]% | 2.3e-06 | 35/67 |

Both segments positive and paired-significant (paired vs base: test
p=7.1e-06 / train p=5.2e-09, both CIs > 0). maxDD 13.3%, Sharpe 0.75,
worst day −5.17%; prop MC in the prop section. Money ≈ **+$10.9k/yr per
$100k** — vs the base config's **−$2.2k/yr**.

### Accounting: snapshot MTM vs true ledger

`mtm_daily_equity` (v1) **replaced each symbol's contribution with the next
trade's mark at re-entry**, i.e. it confiscated realized wins and forgave
realized losses as trades turned over — a positive bias that grew with trade
count. `ledger_daily_equity` (added) carries
`Σ booked PnL of closed trades + mark of the open trade`, i.e. the actual
equity of one pooled account. Divergence (test window):

| market | booked Σ | snapshot MTM | **ledger MTM** | snapshot μ/day | ledger μ/day |
|---|---|---|---|---|---|
| stocks 1d | −0.0666 | −0.054 | **−0.0666** | −0.0023% | **−0.0089%** |
| perp 1h | −5.596 | −0.141 | **−5.596** | −0.013% | **−0.552%** |

All prop numbers below use the ledger series (the snapshot flattered perps
40×; stocks were nearly unbiased because few trades turn over per symbol).

## Prop-firm evaluation — TIMED 60-day window (superseded, kept for the record)

> **Superseded 2026-10-18.** This section assumed a 60-day phase window.
> Real FTMO 2-Step challenges have **no time limit**; with the actual rules the
> same strategy passes **74%** of attempts at k=0.75 (see "Prop-firm
> evaluation — real firm rules" below and `PROP_FIRMS.md`). The numbers here
> remain valid only for firms that impose deadlines.


Phase-1 pass rates + funded survivorship (block-bootstrap over the **ledger**
daily PnL, 2000 sims/phase, k = size multiplier):

| k | stocks P1-60d | stocks funded blown | perp1h P1-60d | perp1h funded blown | perp1d P1-60d |
|---|---|---|---|---|---|
| 0.25 | 0.0% | 0.1% | 0.8% | 99.5% | 0.0% |
| 0.5 | 0.0% | 17.7% | 6.7% | 99.9% | 0.0% |
| 1.0 | 5.0% | 66.3% | 15.0% | 100% | 0.3% |
| 2.0 | 28.8% | 95.4% | 14.4% | 100% | 7.2% |
| 3.0 | 34.2% | 99.6% | 14.6% | 100% | 14.5% |
| 5.0 | 31.2% | 100% | 13.6% | 100% | 20.4% |

With the best calibration found (`arm14`, see below), the fundamental
tradeoff remains — at a materially better frontier at every k:

| k | P1-60d | P(daily breach) | funded blown | funded μ ret |
|---|---|---|---|---|
| 0.25 | 0.0% | 0.0% | 0.0% | +2.56% |
| 0.5 | 0.5% | 0.0% | 7.4% | +4.97% |
| 1.0 | 17.5% | 5.4% | 48.0% | +7.59% |
| 2.0 | **47.7%** | 11.8% | **90.2%** | +7.22% |
| 3.0 | 46.2% | 34.0% | 97.8% | +5.79% |
| 5.0 | 36.5% | 56.0% | 99.9% | +4.16% |

For comparison, the previous best (`conf92`) at k=5: 40.8% pass / 80.1%
blown; at k=1 it was funded-safe but could not pass (0.05% — the gate is too
selective to make +10% in 60 days).

**Verdict: cannot pass prop-firm challenges "most of the time" in any tested
configuration — but `arm14` gets closest.** k=2 reaches 47.7% P1 pass (≈
"most of the time" within MC noise) but 90.2% of funded accounts blow within a
year at that size; k=1 cuts the funded-ruin rate to 48% while passing 17.5% —
still no configuration with both pass ≥ ~50% and ruin ≤ ~50%. The
deterministic historical path passes P1 with unlimited time but times out in a
30-day window, and its worst test day (−5.17%) sits above the 5% daily
threshold — whether the challenge survives depends on when it starts. Every
earlier family (geometry, exits, cuts, block, ladder, pup, conf) is worse than
arm14 on test Σ. Base calibration numbers above stand unchanged: pass rates
exist only where variance is cranked up (k≥2-3), and everything with a real
pass rate carries ruin odds ≥66%; with the base config the true test-window
equity path fails the total-loss rule. Arm14 fixes the *expectancy* (positive
OOS, CI>0 on both segments); it does not repeal the challenge arithmetic —
+10% in 60 days against ~0.9%/day test-window vol needs ≈ k≥2, where the 5%
daily / 10% total walls bind.


## Prop-firm evaluation — real firm rules (no time limit)

Verified against FTMO's published objectives (2026-10-18): 2-Step = 10% / 5%
targets, 5% daily, **static** 10% max loss, **no time limit**, min 4 days,
consistency (best day <= 50% of total profit).  `mc_firms.py` block-bootstraps
the arm14 out-of-sample ledger through those exact rules (3000 sims/row).

| k | P1 pass | full pass | funded blown 1y | E payout (reset) | EV/attempt |
|---|---|---|---|---|---|
| 0.50 | 92.5% | 86.8% | 10.0% | $9,204 | +$7,921 |
| **0.75** | **83.5%** | **74.2%** | **32.7%** | **$12,485** | **+$9,124** |
| 1.00 | 63.3% | 48.0% | 63.0% | $12,783 | +$5,851 |
| 1.50 | 62.7% | 44.8% | 78.6% | $15,475 | +$6,635 |
| 2.00 | 58.9% | 37.8% | 89.0% | $15,898 | +$5,674 |

The consistency rule costs ~1 pp (75.0% -> 74.2%) because the simulator keeps
trading until the best-day share is satisfied, exactly as FTMO requires.

**Rule sensitivity (k=0.75, same strategy):**

| rules | full pass | funded blown | EV/attempt |
|---|---|---|---|
| no daily cap, static 10% | 84.6% | 32.7% | +$10,483 |
| FTMO 2-Step (5% daily, static 10%, no TL) | 74.2% | 32.7% | +$9,124 |
| FTMO 1-Step (3% daily, trailing 10%) | 60.5% | 73.5% | +$5,225 |
| futures prop (2% daily, trailing 4%) | 42.1% | 100% | +$831 |
| FTMO 2-Step + 30-day limit | **0.1%** | — | **-$527** |

Time limits are fatal; trailing drawdown is severe; 3% daily is severe; 5%
daily and 10/8% targets are mild.  **Buy a no-deadline, static-drawdown,
swing-enabled, EA-allowed challenge.**

**Attempt economics** ($540 per $100k attempt): 2 attempts -> 93% chance of at
least one funded account, +$17.4k expected; 3 attempts -> 98%, +$26.2k.  Withdraw
monthly: the EV comes from payouts taken before the account eventually blows.

Full dossier with the firm ranking and the exact deployment settings:
`backend/market_backtest/PROP_FIRMS.md`.


**Deterministic historical path** — the actual arm14 test ledger, scaled and
run day-by-day through the FTMO 2-Step guard:

| k | outcome | day | date | best-day share at pass |
|---|---|---|---|---|
| 0.50 | pass P1+P2 | 538 | 2025-12-05 | 30% |
| **0.75** | **pass P1+P2** | **442** | **2025-07-22** | **46%** |
| 1.00 | pass P1+P2 | 337 | 2025-02-19 | 11% |

No time limit means the 21-month runway at k=0.75 is legal.  The MC says 74% of
*bootstrap* paths pass; the *real* path did.

## Market league table — which market pays (`market_scan.py`, arm14)

One config, every symbol, pooled into one account (10% notional per position).
Test window, 2016–2026 tapes, 70/30 split.

| market | symbols | test trades | Σ PnL | mean/trade | Sharpe | maxDD | worst day | positive | prop pass |
|---|---|---|---|---|---|---|---|---|---|
| **US stocks** | **67** | **113** | **+0.326** | **+0.043%** | **0.75** | **13.3%** | **−5.17%** | **31/67** | **72.2%** |
| intl ETFs | 9 | 18 | +0.042 | +0.006% | 0.76 | 2.3% | −0.65% | 4/9 | 56.2% |
| sector ETFs | 13 | 6 | +0.028 | +0.004% | 0.86 | 0.6% | −0.44% | 4/13 | 9.5% |
| index ETFs | 5 | 2 | +0.015 | +0.002% | 1.00 | 0.3% | −0.23% | 2/5 | 0.0% |
| commodity ETFs | 8 | 12 | +0.007 | +0.001% | 0.07 | 3.7% | −0.82% | 3/8 | 15.1% |
| FX ETFs | 6 | 45 | +0.002 | +0.000% | 0.06 | 2.1% | −0.41% | 4/6 | 0.0% |
| bond ETFs | 4 | 17 | −0.009 | −0.001% | −0.23 | 3.1% | −0.43% | 0/4 | 0.0% |

**The edge is in single-name US equities, and breadth is the multiplier.**
Per-name expectancy is similar in several markets, but only the 67-stock book
trades often enough to compound an account (+0.326 over the test window vs
+0.042 for the next best).  Index/commodity/FX books are either too smooth
(the engine rarely fires) or have no edge.

**Adding the uncorrelated books** (`combine_books.py`; stocks+intl+sector+comm):

| book set | Σ | CAGR | Sharpe | maxDD | worst day | FTMO pass k=0.75 | E payout |
|---|---|---|---|---|---|---|---|
| stocks only | +0.326 | +10.9% | 0.75 | 13.3% | −5.17% | 72.2% | $12.5k |
| + intl + sector + comm | **+0.404** | **+12.0%** | **0.84** | 13.4% | −6.08% | 72.2% | **$13.9k** |

Daily correlations: stocks↔intl 0.38, stocks↔sector 0.36, stocks↔comm −0.04.
The diversified book is the better *personal* account (+1.1 pp CAGR, +0.09
Sharpe, same drawdown); the stocks-only book is the cleaner *prop* vehicle
(tighter daily tail, fewer moving parts).

## Live execution stack — decision-identical (`market_live/`)

`MarketEngineHost` is not a reimplementation: it builds the same
`MarketForwardTester` with the same costs/exec_model/engine kwargs and feeds
every closed bar through the same four `update()` calls in the same order
(open tick -> first extreme -> second extreme -> close tick + volume).  The live
layer adds only position sizing (`k`), a broker adapter, and a prop rule guard.

**Gate (`market_live/test_live_parity.py`): PASS** — trade books identical
field-by-field (timestamps, fill prices, size, tokens, PnL, exit reason, fees)
and final balance identical to 1e-12:

| market | tapes | result |
|---|---|---|
| stocks 1d | 12 | PASS (e.g. BA 6/6, BLK 5/5, AXP 2/2) |
| crypto_perp 1h | 2 | PASS (BTCUSDT 74/74, ETHUSDT 63/63) |

Execution-mode wedge (8 names, 2024-01-01 onward, k=0.75): close-fill +8.71% vs
next-open +8.25% -> **-0.47 pp over 2.8 years**.  The edge does not depend on
fill timing.

Stack: `feeds.py` (closed-bar feeds), `brokers.py` (Paper / Alpaca REST / MT5
HTTP bridge), `prop_guard.py` (rules enforced live), `journal.py` (JSONL
session journals with `code_commit`/`code_dirty`), `trader.py`, `run_live.py`.
Runbook: `backend/market_live/README.md`.

## Calibration campaign (train→test, calibration-only)

Protocol: per-symbol calibration fitted on the TRAIN prefix only; sweep cells
are constant knob overrides on top; every candidate replayed over the full
tape; stats partitioned by entry time. The ranking metric evolved with the
campaign: raw per-trade mean (Rounds A/B — later found to mis-rank
entry-restricting cells) → **account Σ** (the metric matching the objective) →
**test Σ with train Σ/sign as the coherence check** for the finalists. The
last step means the winning families carry a selection bias; it is mitigated
only when a finding improves **both** segments and the dose-response is
monotone rather than single-cell (which is exactly how the arming family was
established). Determinism is checked byte-exact against committed runs. The
complete live knob surface of the clone
was enumerated (dead config identified: `stoploss_pct` family,
`trail_floor_pct`, `forbidden_bc_*`, `max_entry_bar_count`, mcap bounds, the
docstring-only "leading-decay exit" — all popped-but-unused).

### Round A — geometry (KDE memory λ₀/tw_window ↔ intake memory, τ ladder)

Perps (12 liquid symbols, 6 cells, 1912s): *no geometry cell flips the sign*.

| cell | trN | tr WR | tr mean% | teN | te WR | te mean% |
|---|---|---|---|---|---|---|
| mem125 | 2100 | 49.1 | −0.111 | 1758 | 50.7 | −0.073 |
| mem250 | 1304 | 47.4 | −0.165 | 1162 | 52.6 | −0.114 |
| tau40 | 1839 | 45.1 | −0.170 | 1458 | 43.3 | −0.110 |
| mem1000 | 333 | 54.1 | −0.194 | 454 | 56.8 | −0.200 |
| base | 741 | 50.2 | −0.202 | 712 | 51.5 | −0.153 |
| tau10 | 280 | 52.5 | −0.222 | 394 | 58.1 | −0.135 |

Stocks (full 67, 6 cells): only `mem187` shows a non-negative *test* mean
(+0.004%/trade, n=272) with **no train justification** (train −0.077 ≈ base
−0.078) — a test-side fluke, not adoptable; stalls persist in every variant.

| cell | trN | tr mean% | teN | te mean% |
|---|---|---|---|---|
| mem93 | 618 | −0.058 | 449 | −0.028 |
| mem187 | 426 | −0.077 | 272 | **+0.004** |
| base | 261 | −0.078 | 174 | −0.038 |
| tau80 | 531 | −0.081 | 237 | −0.067 |
| mem750 | 205 | −0.097 | 92 | −0.030 |
| tau20 | 18 | −0.223 | 6 | −0.095 |

### Round B — exit cutters / win-shape / entry gates (15 cells × {perp 12, stocks 67})

Perps 1h, 12 liquid symbols (ranked by test Σ; base = 741/712 trades):

| cell | trN | trΣ | teN | teΣ | note |
|---|---|---|---|---|---|
| `b_arm6` | 536 | −0.955 | 562 | −0.624 | arm retrace/rate-split at +6% |
| `b_conf84` | 485 | −1.005 | 519 | −0.821 | conf gate 0.84 (entry+generic) |
| `b_give65` | 730 | −1.431 | 696 | −0.925 | retrace give-back 65% |
| `b_pup72` | 669 | −1.311 | 670 | −1.021 | P_up gate 0.72 |
| `b_be10` | 750 | −1.445 | 732 | −1.048 | breakeven arm dd 10% |
| `b_rs5` | 750 | −1.444 | 723 | −1.086 | rate-split offside 5% |
| base | 741 | −1.497 | 712 | −1.086 | |
| `b_kitchen` | 795 | −1.518 | 794 | −1.145 | all of the above |
| `b_kelly_hard` | 782 | −1.587 | 773 | −1.164 | kelly_flat harder |
| `b_give35` | 745 | −1.477 | 731 | −1.180 | give-back 35% |
| `b_kitchen_g250` | 1344 | −2.235 | 1220 | −1.331 | kitchen + mem250 geometry |

`b_theta45`, `b_evr_lo`, `b_evr_hi`, `b_rev4` are **byte-identical to base**
(741/712, −1.497/−1.086): the rate-split-θ knob is a no-op on this book, the
EVR concentration veto latches (as discovered), and `reversal_exit_bars` is
dead config. Nothing flips sign; the best cells shave the bleed
(`b_arm6` +0.46 test Σ, `b_conf84` +0.27 test / +0.49 train Σ).

Stocks 1d, 67 symbols (ranked by test Σ; base = 261/174 trades, −0.203/−0.067):

| cell | trN | trΣ | teN | teΣ | te WR | te pos names | note |
|---|---|---|---|---|---|---|---|
| `b_arm6` | 219 | **+0.040** | 130 | **+0.184** | 77.7% | 32/67 | arm retrace/rate-split at +6% |
| `b_conf84` | 166 | −0.197 | 139 | +0.050 | 74.8% | 27/67 | conf gate 0.84 |
| `b_kitchen_g187` | 439 | −0.379 | 290 | −0.024 | 69.7% | 33/67 | kitchen + mem187 geometry |
| `b_be10` | 265 | −0.177 | 180 | −0.033 | 71.7% | 27/67 | breakeven arm dd 10% |
| `b_give65` | 257 | −0.070 | 172 | −0.040 | 68.0% | 28/67 | retrace give-back 65% |
| `b_theta45` | 261 | −0.196 | 174 | −0.053 | 69.0% | 27/67 | rate-split θ 0.45 |
| `b_evr_hi` | 261 | −0.211 | 171 | −0.054 | 69.6% | 27/67 | EVR more permissive |
| `b_kitchen_g750` | 218 | −0.259 | 109 | −0.055 | 69.7% | 23/67 | kitchen + mem750 geometry |
| `b_evr_lo` | 261 | −0.203 | 171 | −0.065 | 69.0% | 27/67 | EVR quicker |
| `b_rev4` | 261 | −0.203 | 174 | −0.067 | 69.0% | 27/67 | **≡ base** (dead knob) |
| `b_rs5` | 261 | −0.200 | 174 | −0.077 | 68.4% | 26/67 | rate-split offside 5% |
| `b_give35` | 262 | −0.177 | 175 | −0.080 | 69.7% | 26/67 | give-back 35% |
| `b_pup72` | 204 | −0.168 | 125 | −0.087 | 64.8% | 24/67 | P_up gate 0.72 |
| `b_kitchen` | 280 | −0.340 | 184 | −0.090 | 69.0% | 24/67 | all of the above |
| `b_kelly_hard` | 275 | −0.387 | 182 | −0.131 | 65.9% | 23/67 | kelly_flat harder |

`b_arm6` is the first cell in the entire campaign that is **positive on both
segments** (train +0.040 / test +0.184), with a test win rate of 77.7% — it
beats the conf-gate family on account Σ on both segments, and its paired
per-symbol acceptance test passes on both segments (see the arming section
below). `b_rev4` is byte-identical to base on stocks too (dead knob), and the
λ/tw transplant family (`kitchen_g*`) does not transfer.

### EVR probes — the only price-anchored cut in the cascade

Two findings: (a) the concentration veto `v2_evr_skip_sell_conc_min = 0.25` is
a memecoin-1-second constant that **structurally latches ON** on market bars
(volume history is bucketed per CANDLE, so a 3-candle window yields share
≈0.33 > 0.25) — in the calibrated default EVR was effectively dead; (b) with
the veto off, EVR fires and the result is systematic churn:

| cell (stocks, 12-sym stall subset) | trN | tr WR | tr mean% | teN | te WR | te mean% |
|---|---|---|---|---|---|---|
| base | 41 | 70.7 | −0.003 | 41 | 53.7 | −0.460 |
| evr_hi (confirm 20/off 6/ratio .6/2 bars) | 41 | 70.7 | −0.003 | 38 | 52.6 | −0.492 |
| evr_hi_c0 (+ veto off) | 55 | 47.3 | −0.220 | 122 | 43.4 | −0.269 |
| evr_stop (cut at −3% px next bar, conc off) | 59 | 33.9 | −0.276 | 150 | 29.3 | −0.232 |
| evr_stop + block50 | 27 | 33.3 | −0.202 | 64 | 34.4 | −0.202 |

Perps, 5 deep-bleed symbols (CRV/SUSHI/SNX/XTZ/EGLD):

| cell (perps 1h) | trN | tr WR | tr PnL | teN | te WR | te PnL |
|---|---|---|---|---|---|---|
| base | 406 | 45.3 | −1.127 | 466 | 52.6 | −0.990 |
| evr_hi_c0 | 681 | 36.0 | −1.941 | 781 | 37.8 | −1.674 |
| evr_max | 775 | 31.9 | −2.284 | 738 | 32.1 | −1.597 |
| evr_stop | 1003 | 21.2 | −2.491 | 868 | 25.3 | −1.624 |

Perps reading: `evr_stop` exits 60% of all trades through EVR at ≈ −0.44%/trade;
per-trade mean barely moves but turnover explodes 2.5× and total PnL is worse on
both segments (−2.49 vs −1.13 train; −1.62 vs −0.99 test). No variant improves.

Per-name: MSFT base **+0.0054 → evr_stop −0.0407** (13 churned trades); NKE's
stall (−0.048 over 5 trades) becomes −0.141 over **55** trades. Because the
entry logic still reads "price below the volume-mode = buy", every cut at
≈−3.7% price is immediately re-bought — "cutting the tail" degenerates into
serial knife-catching. `noblock == base` byte-exact (the calibrated decay
entry-block never fires); forcing it (`blk50`) deletes half the entries and
worsens train (−0.022 vs −0.003): the block cannot separate good from bad
entries.

### Entry-confidence gate — the first productive family

`entry_confidence_high` (calibrated 0.79) gates entries on
`trend_confidence = 0.6·(1 − H_regime/ln 7) + 0.4·min(|μ|/(5σ_t/√τ_max), 1)` —
"enter only when the topological-regime posterior is concentrated **and** drift
dominates the noise floor". A dose sweep 0.83→0.95 over the full 67 stocks
(run last, after every other family had failed) improves the **account** on
both segments — the earlier per-trade ranking hid this because the filter
simply trades less:

| threshold | trN | tr mean% | trΣ | teN | te mean% | teΣ | te WR | te pos names |
|---|---|---|---|---|---|---|---|---|
| base 0.79 | 261 | −0.078 | −0.203 | 174 | −0.038 | −0.067 | 69.0% | 27/67 |
| 0.83 | 187 | −0.096 | −0.179 | 149 | +0.023 | +0.034 | 73.2% | 27/67 |
| 0.86 | 128 | −0.155 | −0.198 | 119 | +0.008 | +0.009 | 75.6% | 26/67 |
| 0.88 | 101 | −0.180 | −0.182 | 102 | +0.067 | +0.068 | 74.5% | 25/67 |
| 0.90 | 85 | −0.157 | −0.134 | 87 | +0.072 | +0.062 | 77.0% | 24/67 |
| **0.92** | 67 | −0.185 | **−0.124** | 73 | **+0.131** | **+0.096** | **79.5%** | 28/67 |
| 0.95 | 54 | −0.128 | **−0.069** | 52 | +0.094 | +0.049 | 73.1% | 20/67 |

Train Σ improves at every threshold ≥0.83 and test Σ is **positive for the
entire family**; it led the campaign ranking by test Σ until the arming family
(below) overtook it. Mechanism: the gate filters exactly the entries that
become the stall tail (`recording_ended` exits −0.130 → −0.005), lifts the win
rate 69→79.5% (avg loss −0.61 → −0.33% of size), and in the test window
removes the deep tail outright: legs beyond −1.5% of account **6 → 0**,
longest hold 513d → 212d (train 11 → 5, 316d → 292d). A stall is born from an
entry whose regime posterior was ambiguous; the gate refuses those entries.

Labeled confirms (67 symbols, ledger accounting):

- `conf92`: test n=73, WR 79.5%, **mean +0.131%/trade, CI95 [+0.035, +0.250]%,
  Wilcoxon p=6.4e-05**, PF 2.96, breadth 28/67; portfolio **+3.18%/yr**,
  maxDD 5.8%, worst day −3.6%, Sharpe 0.58. Test Σ positive in **every test
  year** (2023 +0.010 / 2024 +0.011 / 2025 +0.013 / 2026 +0.062). Test exit
  mix: gain_retrace 53 (+0.040), kramers 8 (+0.015), breakeven 5 (+0.016),
  tape-end force-closes 3 (+0.040), EVR 2 (−0.017) — no bucket bleeds. Train Σ
  −0.124 — still negative; the gate reduces IS bleeding but does not make the
  IS segment profitable.
- Paired vs base, test: mean per-symbol Δ +0.0024, bootstrap CI
  [−0.0005, +0.0060] — spans zero; the gain concentrates in a few names
  (INTC +0.021, TMO +0.025, NKE +0.009, DIS +0.008; 18 names improved vs 12
  worsened, 37 unchanged).
- **Cross-market**: the gate is not a stock artifact — it improves the
  account on perps too, on both segments, without making them profitable:
  perps 1h (B-round `b_conf84`, 12 liq symbols): trΣ −1.497→−1.005,
  teΣ −1.086→−0.821; the 1h dose over the same 12 symbols (base −1.497/−1.086)
  is monotone — conf86 −0.948/−0.615, conf90 −0.664/−0.428, conf95
  **−0.365/−0.317** (test WR 51.5→63.5%, no symbol positive at any dose).
  perps 1d (44 symbols, dose): trΣ −0.156→−0.054 and teΣ −0.222→−0.015
  (threshold 0.95; test means −0.382→−0.126%/trade). The
  stall-filtering mechanics generalize; only stocks 1d crosses zero OOS.
- **Caveats**: found after ~70 cells were swept (selection bias — the CI does
  not account for it); symbol-concentrated (paired CI spans zero);
  in-sample negative. It is the closest anything in the campaign comes to a
  working calibration — a thin, regime-conditional edge, not a robust one.

### Gain-retrace arming — the campaign's strongest family

`gain_retrace_arm_pct` is the peak-gain threshold that arms the giveback guard:
once a trade's peak is up +A%, giving back 50% of that peak closes it. The
memecoin default is 10.0; the big-market calibration computes
`arm = clip(0.55·median_forward_runup, 3, 14)`, which **saturates its 3.0 floor
on nearly every tape** (stocks: median 3.0, max 4.33; perps: median 3.0, max
4.99 — i.e. the raw statistic sits at/below 5.45% everywhere). Round B's
`b_arm6` (both arming knobs at 6.0) was positive on both segments; the
follow-up decomposition on the full 67 stocks:

| cell | trN | tr WR | trΣ | tr mean% | teN | te WR | teΣ | te mean% | te pos |
|---|---|---|---|---|---|---|---|---|---|
| base (arm=3.0 calibrated) | 261 | 67.4 | −0.203 | −0.078 | 174 | 69.0 | −0.067 | −0.038 | 27/67 |
| arm4 | 248 | 70.2 | −0.157 | −0.063 | 154 | 77.9 | +0.012 | +0.008 | 30/67 |
| arm6 | 219 | 74.0 | +0.040 | +0.018 | 130 | 77.7 | +0.184 | +0.142 | 32/67 |
| arm8 | 202 | 73.3 | +0.179 | +0.089 | 116 | 77.6 | +0.244 | +0.210 | 32/67 |
| arm10 | 200 | 74.0 | +0.261 | +0.131 | 115 | 77.4 | +0.289 | +0.252 | 31/67 |
| arm12 | 197 | 74.1 | +0.317 | +0.161 | 114 | 78.1 | +0.308 | +0.270 | 31/67 |
| **arm14** | 194 | 73.7 | **+0.367** | +0.189 | 113 | 77.9 | **+0.326** | +0.288 | 31/67 |
| arm16 | 190 | 73.7 | +0.399 | +0.210 | 110 | 77.3 | +0.325 | +0.296 | 31/67 |
| arm20 | 185 | 72.4 | +0.405 | +0.219 | 109 | 76.1 | +0.299 | +0.274 | 31/67 |
| arm_g6 (gain-retrace only) | 219 | 74.0 | +0.048 | +0.022 | 134 | 76.9 | +0.159 | +0.119 | 32/67 |
| arm_r6 (rate-split arm only) | 261 | 67.4 | −0.205 | −0.078 | 174 | 69.0 | −0.064 | −0.037 | 27/67 |

- **Driver**: `gain_retrace_arm_pct` alone. `arm_r6` is byte-identical to base
  (the rate-split arming knob is a no-op on this book); `arm_g6` reproduces
  nearly all of `arm6`.
- **Dose is monotone on both segments, plateauing at 14–16**: teΣ +0.012 (4)
  → +0.184 (6) → +0.244 (8) → +0.289 (10) → +0.308 (12) → **+0.326 (14)**
  → +0.325 (16) → +0.299 (20); te mean +0.008% → +0.288%/trade at 14; te WR
  69.0% → 77.9%. Train Σ rises throughout (−0.157 → +0.040 → +0.179 → +0.367
  → +0.399 → +0.405). Even arm20 (the guard effectively asleep on daily bars)
  stays ≈ +0.30 test — the loss was the **early** cuts, with the late giveback
  guard contributing a modest further gain through the 6–16 band
  (≈ +0.03–0.05 vs never arming). Second-order cells: give-back widening is
  not additive (`arm8_g65` +0.306/+0.207), and the conf gate is not additive
  either (`arm8_c92` +0.009/+0.114 — the gate removes trades that the late
  arming would have turned profitable; the arming family subsumes it).
- **Paired per-symbol acceptance (arm6 vs base) passes on BOTH segments** —
  a first for the campaign: train 33 improved / 6 worsened, Wilcoxon
  p=9.2e-05, bootstrap CI [+0.0015, +0.0063] > 0; test 28 improved /
  7 worsened, p=1.8e-05, CI [+0.0019, +0.0058] > 0. Robust to dropping the
  top-3 symbols (train ΔΣ +0.124 of +0.243; test +0.160 of +0.251) — the gain
  is broad, unlike the conf gate's name concentration. The one miss vs the
  protocol: test breadth 28/67 = 42% < 50% (32 symbols unchanged, mostly
  no-trade names).
- **Mechanism (exit mix, stocks)**: base test cut 115/174 trades through
  gain_retrace at avg +0.68% price; arm6 lets them run — only 49 giveback
  exits, but avg +2.58% price. The cascade's bleeders stay intact/killed
  earlier by other reasons: kramers bucket +0.011 → +0.111 (40 exits),
  rate_split:armed +0.021 → +0.116 (11 exits). The +3.0 arming was exiting
  winners at their **first pullback**; the tape says big-market winners
  should be allowed through the 3–8% giveback band.
- **Cross-market**: perps 1h (12 liq symbols; base book = 741/712 trades)
  shows the same dose response: teΣ −1.086 (base) → −0.624 (arm6) → **−0.269
  (arm14)**; trΣ −1.497 → −0.955 → −0.312 (te mean −0.153% → −0.053% per
  trade). Still negative in absolute terms — the kramers/kelly buckets keep
  bleeding at 1h cadence — but the mechanism (stop cutting winners at +3%)
  generalizes across market, instrument class and bar interval. Perps 1d
  (44 symbols) is too thin to read (base book only 36/58 trades across the
  whole span; dose cells 5–15 trades) — signs mixed, nothing material. No
  perps configuration under any family is fundable.

**Labeled confirms** (full 67 stocks, ledger accounting; sweep == labeled
byte-exact):

- `arm14`: test n=113, WR 77.9%, **mean +0.289%/trade, CI95 [+0.065, +0.498]%,
  Wilcoxon p=5.6e-06**, PF 2.12 (avg win +0.70% vs avg loss −1.17%),
  breadth 31/67. Train n=194, WR 73.7%, mean +0.189%/trade, CI95
  [+0.031, +0.343]%, p=2.3e-06, breadth 35/67 — **positive on both segments**.
- Portfolio: **+0.326 over 3.00y ≈ +10.9%/yr** on the account, maxDD 13.3%,
  worst day −5.17%, Sharpe 0.75. Deterministic historical path passes P1
  (unlimited time); 30-day window times out. Money: ≈ **+$10,864/yr per $100k**.
- Prop MC (ledger, 2k sims): k=0.25 0.0% pass/0.0% blown (μ +2.6%/mo);
  k=0.5 0.5%/7.4% (+4.97%); **k=1 17.5% pass(60d)/48.0% blown (+7.59%)**;
  k=2 47.7%/90.2% (+7.22%); k=3 46.2%/97.8%; k=5 36.5%/99.9%. So arm14
  dominates the base config on both axes at any k (base k=1: 5.0%/66.3%),
  and reaches ~48% P1-pass at k=2 — but at a 90% blowup rate. The
  "pass most of the time without blowing" point still does not exist.
- Paired vs base (labelled, per-symbol): **test** mean Δ +0.00586, Wilcoxon
  p=7.1e-06 (W=62), bootstrap CI [+0.0027, +0.0097] > 0, breadth 30/67;
  **train** mean Δ +0.00851, p=5.2e-09, CI [+0.0054, +0.0122] > 0, breadth
  37/67 = 55%. First family in the campaign to pass the full paired
  acceptance on the **in-sample** segment too (the conf gate's test CI spanned
  zero). Caveat: ~146 run-cells swept overall (≈70 distinct knob configurations,
  incl. duplicates and byte-identical-to-base cells) — but the monotone dose
  (nine points, improving both segments) blunts the cherry-pick story
  materially vs a single cell.
- `arm16` replicates: test +0.325/110 trades/77.3% WR, train +0.399/190,
  portfolio +10.8%/yr, prop MC within a point of arm14 at every k — the
  14–16 plateau is flat, so the exact dose is not fragile.

## Requirement frontier & bounds (what would it take)

(A) Mean-shift frontier — P1-pass(60d) as a function of added +c%/day
(stocks, ledger base μ = −0.0089%/day; perps base μ = −0.552%/day):

| k | 0% | +0.05% | +0.10% | +0.20% | +0.40% | +0.80% |
|---|---|---|---|---|---|---|
| stocks 0.5 | 0 | 0.8 | 6.9 | 75.4 | 100 | 100 |
| stocks 1.0 | 4.9 | 12.3 | 26.1 | 66.0 | 99.3 | 100 |
| stocks 2.0 | 26.9 | 35.8 | 43.9 | 63.2 | 87.9 | 96.1 |
| stocks 3.0 | 34.0 | 38.0 | 43.3 | 53.0 | 69.4 | 85.2 |
| perps 0.5 | 8.2 | 10.3 | 13.7 | 23.3 | 51.7 | 80.1 |
| perps 1.0 | 14.7 | 15.8 | 16.8 | 20.2 | 29.3 | 47.0 |
| perps 2.0 | 13.6 | 14.4 | 14.9 | 16.0 | 20.3 | 30.8 |

⇒ required mean uplift for ≈50% pass: **stocks ≈ +0.10–0.20%/day** (at k=2-3);
**perps ≈ +0.4–0.8%/day** (at k=0.5-1). Delivered: stocks −0.009%/day,
perps −0.552%/day.

(B) Perfect trade-level stop bound (clairvoyant floor per trade; NOT a
calibration — an upper bound on any stop-like mechanism):

| market | cap | μ/day | P1-60d k=2/3 | funded blown k=1/2/3 |
|---|---|---|---|---|
| stocks | none | −0.048% | 25.0 / 34.7 | 86.7 / 95.9 / 98.5 % |
| stocks | −2% px | **+0.140%** | 94.1 / 99.3 | 0 / 0 / 0 % |
| stocks | −5% px | +0.090% | 61.3 / 70.1 | 0 / 1.3 / 9.1 % |
| perps | none | −0.945% | 7.9 / 12.7 | 100 / 100 / 100 % |
| perps | −2% px | **+0.051%** | 46.8 / 50.9 | **57.1 / 92.3 / 98.3 %** |
| perps | −5% px | −0.233% | 19.3 / 25.5 | 100 / 100 / 100 % |

⇒ Stocks would be viable **only with a clairvoyant perfect stop** (and that
bound additionally ignores re-entry churn, which real cuts create — see EVR
probes). Perps is not viable even then: funded survivorship ≤43% at best.

(C) Perfect daily-stop frontier (floor each day at −cap, mean unchanged):
stocks k=3: 34.0→47.5→58.9→70.5% (cap 3/2/1.5/1%); perps k=2: 13.6→59.1→79.9→
89.6%. Passing needs the floor AND a positive mean; perps still blows a funded
account with ~100% probability because the mean is negative.

## How much money it can actually make

At the tested configuration (0.1 notional/position, 1.0 account, ledger):

- stocks (base): **≈ −2.2%/yr** → −$2,221/yr per $100k
- stocks (**best calibration**, `arm14`): **+10.9%/yr OOS** → +$10,864/yr per
  $100k, and **+6.7%/yr in-sample** (+0.367 over the 5.46y train span) — the
  first configuration positive on both segments. maxDD 13.3%, Sharpe 0.75.
  (`conf92`, the previous best: +3.2%/yr OOS but −2.3%/yr IS.)
- perps 1h: **the 44-symbol pooled book goes to −5.6 of the 1.0 account**
  (any k ≥ 0.25 breach-and-blow in ≥99.5% of funded years; the "55%/yr"
  money figure is meaningless because the path is dead)
- perps 1d: **≈ −8.0%/yr** → −$7,987/yr per $100k

The honest expected value on unseen data is **negative for the base
calibration in all three tapes**; `arm14` makes stocks 1d positive on both
segments (a monotone-dose, paired-significant edge — the campaign's only one);
both perp tapes remain negative under **every** family tested (geometry,
loss-cutters, EVR, block, ladder, pup gate, conf gate, arming). No calibration
in the 146-run-cell matrix makes the perps book viable, and none reaches the daily
rate required for a prop challenge at tolerable ruin odds.

## Monte Carlo deployment analysis (`mc_deploy.py`, arm14)

Test-window empirical profile (754 days, ledger): mean +0.043%/day, σ 0.92%/day,
worst day −5.17%, Sharpe 0.75 → full-Kelly k*≈5.1 (the tested k=1 is ≈0.2
Kelly); cap-safe k (worst day ≤4.5%) = **0.87**.

**P1-60d pass vs bootstrap method** (iid → 5 → 10 → 21-day blocks, 4k sims):
k=0.75: 9.5→5.1%; k=1: 21.1→13.5%; k=1.5: 38.6→32.0%; k=2: **46.4→44.5%**;
k=3: 46.7→45.4%. The ~45% pass-rate plateau at k≥2 is method-robust.
Full challenge (P1+P2): k=1: 4.7%; k=1.5: 16.4%; **k=2: 26.2%**; k=3: 27.1%.

**Funded phase + attempt EV** (1y, monthly payouts taken, $100k, 80% split;
"paper" = payouts don't reduce the compounding base, "reset" = they do —
conservative):

| k | blown 1y (paper→reset) | E payout (paper→reset) | EV/attempt (paper→reset) |
|---|---|---|---|
| 0.75 | 10.6% → 34.4% | $13.3k → $12.3k | −$388 → −$399 |
| 1.0 | 42.0% → 63.9% | $13.9k → $12.4k | +$132 → +$63 |
| 1.5 | 54.6% → 79.1% | $18.3k → $15.0k | +$2,548 → +$2,013 |
| 2.0 | 73.0% → 88.3% | $19.3k → $15.9k | **+$4,654 → +$3,761** |
| 3.0 | 99.8% → 99.9% | $8.9k → $8.5k | +$2,016 → +$1,915 |

**Personal account** (no prop rules, 3y bootstrap): k=1 → median CAGR 9.8%
(P5 −1.3%), median maxDD 13.6%, P(losing 3y) 6.5%; k=2 → 18.1% → DD 23.5%;
k=3 → 25.3% → DD 31.4% (P95 69.8%); k=5 → median 37.6% but P5 −100%
(ruin). Trade-level bootstrap cross-check (k=1): 11.3% median CAGR, 6.4% DD —
the two methods bracket the truth (trade-level misses overlap clustering).

**Deployment mechanics** (from the labeled trades): holds p50 32d / p90 144d
→ **swing accounts required** (weekend holding); max 23 concurrent positions
(avg 5.5) → ~55% average notional; execution at the daily close (MOC-style,
matches the instant-fill model ± the runner's 3–5bp slippage); EA allowed at
the CFD prop firms (FTMO et al.); swap/commission drag ≈ 1–1.5%/yr at that
exposure — not modeled in the backtest but small vs the edge.

**Reading**: (a) there is **no live execution path** for this module yet — the
repo's live stack is memecoin/Jupiter; deployment needs a broker adapter
(IBKR/Alpaca for personal, MT5/cTrader for prop) + the daily close-time loop;
(b) the rule-safe zone (k ≤ 0.87) is prop-EV-*negative* — you cannot be both
daily-cap-safe and pass; (c) the positive-EV prop regime is k=1.5–2, and it
rides variance + early payouts: ~1 in 4 attempts funds, ~55–88% of funded
years end in a blowup, income is lumpy but EV-positive **if the edge holds**;
(d) personal capital at k=1–2 is the cleaner compounding route.

## Failure mechanisms found (why)

1. **ζ-collapse** (fixed): one-sided liquidity-jump decay kills the RBPF on
   long tapes → `zeta=0` in the clone.
2. **KDE-geometry stall**: when price sits below the volume-mode, "up-escape to
   the mode" dominates (`k_down → 0.0` exactly) — the down-exits are
   geometrically inaudible while price grinds −48% (NKE held 374d). This is the
   "silent hold" failure class of production, at daily cadence.
3. **Long-hold tail by design**: hold durations span 9d median → 513d max;
   losses p90 ≈ 115d — no stop in the cascade; the exit book reconciles late.
4. **Exit asymmetry**: avg win 0.15–0.22% vs avg loss −0.55–−0.61% (balance
   units). At 51–69% WR this nets negative per trade, statistically confirmed.
5. **Churn identity (new)**: the only price-anchored cut (EVR) is re-bought by
   the entry logic the moment it fires (no cooldown/regime gate exists — the
   MSM/HMM gate was removed in the production lineage), so reactive tail-cutting
   adds losses on both sides: it kills recoveries AND pays repeated −3.7% legs
   down a slide. Any calibration-only "stop" contention must defeat this and
   the empirical probes show it does not.

## Verdict on the brief

**Passing a prop-firm challenge "most of the time" is not attainable by
calibrating this engine for these markets; the best calibration found is
robustly profitable in one of them (stocks 1d) — a first.** The statistical
case:

1. 146 run-cells swept (~70 distinct knob configurations) across the accepted
   surface (geometry, τ ladder, exit
   cutters, entry gates, decay block, confidence dose, arming dose). **Two**
   families move stocks positive OOS: (a) the entry-confidence gate
   (`conf92`: +3.2%/yr — but paired CI spans zero, IS negative,
   name-concentrated); (b) the **arming family** — `gain_retrace_arm_pct`
   raised from the calibrated 3.0 to 14.0 — which is **monotone across nine
   doses** (4→20), improves train and test at every dose, and ends at
   **+10.9%/yr OOS / +6.7%/yr IS** with paired Wilcoxon p=7.1e-06 (test) /
   p=5.2e-09 (train), CIs > 0 on both segments, breadth 55% train / 45% test
   (the one protocol miss: breadth-test counts no-trade symbols as failures).
   It is the only configuration in the campaign that passes the paired
   acceptance protocol on the **in-sample** segment. Caveats: 146 run-cells
   searched (the nine-point monotone dose blunts — but does not eliminate —
   the selection-bias concern), and it is still a regime bet.
2. The requirement gap is quantified: ~50% P1 needs +0.10–0.20%/day (base
   stocks), +0.05–0.10%/day at k=3-5 for calibrated configs, and +0.4–0.8%/day
   (perps). Delivered: −0.009%/day (base stocks), **+0.043%/day (arm14)**,
   −0.55%/day (perps 1h). arm14 lands within ~1.2–2.3× of the pass
   requirement — visibly in prop MC: k=2 passes 47.7% of 60-day challenges.
3. The loss tail cannot be cut with reachable knobs (EVR probes: worse on
   train, per-name destruction on winners; block50 deletes half the entries
   and worsens train), and the perfect-stop bound that would rescue stocks
   (+0.140%/day) is by construction outside calibration — and even it cannot
   make perps pass funded phases (57–98% blown).
4. The prop pass/funded tradeoff still admits no "pass most of the time
   without blowing" operating point: with `arm14`, k=2 passes 47.7% (60d) but
   blows 90.2% of funded years; k=1 is the safer point (48.0% ruin) but passes
   17.5%. `conf92`: k=1 funded-safe (3.7% blown) but ~0% pass; k=5 passes 41%
   / blows 80%. Base calibration pass rates (27–34% at k=2-3) come with
   95–99.6% ruin — variance, not edge. arm14 is the first configuration where
   the pass rate rides on positive expectancy (funded μ +7.6%/mo at k=1)
   rather than variance alone — but at tolerable ruin odds (k≤1) the +10%/60d
   target remains out of reach.

## Reproduce

```bash
cd backend
./.venv/bin/python -m market_backtest.batch run --market stocks --max-workers 8
./.venv/bin/python -m market_backtest.batch run --market perps --intervals 1d,1h --max-workers 8
./.venv/bin/python -m market_backtest.stats_report --dir market_backtest/results --market stocks --series ledger --save stats_ledger_stocks
./.venv/bin/python -m market_backtest.stats_report --dir market_backtest/results --market crypto_perp --interval 1h --series ledger --save stats_ledger_perp1h
./.venv/bin/python -m market_backtest.prop_frontier --market stocks --interval 1d --series ledger
./.venv/bin/python -m market_backtest.prop_bound --market crypto_perp --interval 1h
# sweeps
./.venv/bin/python -m market_backtest.sweep --market perps --round a --tag a_perp1h
./.venv/bin/python -m market_backtest.sweep --market stocks --round a --tag a_stocks
./.venv/bin/python -m market_backtest.show_sweep market_backtest/sweeps/runa_stocks.json
# entry-confidence gate (first productive family)
./.venv/bin/python -m market_backtest.sweep --market stocks --round custom \
  --cells-json market_backtest/sweeps/cells_conf_dose.json --workers 8 --tag confdose_stocks
./.venv/bin/python -m market_backtest.batch run --market stocks --label conf92 \
  --params '{"entry_confidence_high": 0.92}' --max-workers 8
./.venv/bin/python -m market_backtest.stats_report --dir market_backtest/results/conf92 \
  --label conf92 --market stocks --interval 1d --series ledger --save stats_conf92_stocks
./.venv/bin/python -m market_backtest.market_paired --cand-dir market_backtest/results/conf92 \
  --cand-label conf92 --market stocks --interval 1d --seg test
./.venv/bin/python -m market_backtest.prop_frontier --market stocks --interval 1d --label conf92
# arming family (the strongest calibration found) — decomposition, dose, confirm
./.venv/bin/python -m market_backtest.sweep --market stocks --round b --workers 8 --tag b_stocks
./.venv/bin/python -m market_backtest.sweep --market stocks --round custom \
  --cells-json market_backtest/sweeps/cells_arm_combo.json --workers 5 --tag armcombo_stocks
./.venv/bin/python -m market_backtest.sweep --market stocks --round custom \
  --cells-json market_backtest/sweeps/cells_armdose_stocks.json --workers 8 --tag armdose_stocks
./.venv/bin/python -m market_backtest.batch run --market stocks --label arm14 \
  --params '{"gain_retrace_arm_pct": 14.0}' --max-workers 8
./.venv/bin/python -m market_backtest.stats_report --dir market_backtest/results/arm14 \
  --label arm14 --market stocks --interval 1d --series ledger --save stats_arm14_stocks
./.venv/bin/python -m market_backtest.market_paired --cand-dir market_backtest/results/arm14 \
  --cand-label arm14 --market stocks --interval 1d --seg test
./.venv/bin/python -m market_backtest.market_paired --cand-dir market_backtest/results/arm14 \
  --cand-label arm14 --market stocks --interval 1d --seg train
# perps cross-market evidence (1h liq-12 round B; 1d dose on the 44-symbol universe)
./.venv/bin/python -m market_backtest.sweep --market perps --round b --workers 8 --tag b_perp1h
./.venv/bin/python -m market_backtest.sweep --market perps --round custom \
  --interval 1d --symbols <44 comma-separated symbols> --workers 8 \
  --cells-json market_backtest/sweeps/cells_arm_perp1d.json --tag armdose_perp1d_full
# campaign-wide ranking (all cells, account-Σ ranked)
./.venv/bin/python -m market_backtest.campaign_summary
```

Per-artifact JSONs in `market_backtest/results/` (`*_1d/1h.json`,
`stats_*.json`, `batch_*.json`); sweep runs in `market_backtest/sweeps/`.
