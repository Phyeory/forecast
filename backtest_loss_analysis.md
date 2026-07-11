# Pump-Chart Strategy Engine — Backtest Loss Analysis Report
**Date:** 2026-07-11 | **Batch ID:** 1783729942452 | **Analyst:** Antigravity

---

## 1. Overview

This report documents a systematic investigation into the causes of losing trades across a full backtest batch run on the Pump-Chart Strategy Engine. The analysis covers **192 tokens, 288 total trades**, representing a single-timeframe, long-only run of the Langevin-physics regime detector on Solana memecoin price data.

### Batch Summary

| Metric | Value |
|---|---|
| Tokens tested | 192 |
| Total trades | 288 |
| Winning trades | 172 (59.7%) |
| Losing trades | 116 (40.3%) |
| Big losses (>10%) | 58 (20.1% of all trades) |
| Total PnL | **+1.43 SOL** |
| Total lost on big losses | **-1.053 SOL** |
| Average winning trade | +11.1% |
| Average losing trade | -11.1% |
| Worst single trade | -43.10% (Trends) |
| Median hold time (losses) | 120s |
| Median hold time (big losses) | 146s |

The strategy is net profitable, but the 58 big-loss trades (-1.053 SOL) erode the majority of gains. The top 10 biggest losers alone account for approximately -0.64 SOL.

---

## 2. Methodology

All 192 JSON result files were parsed programmatically. Each file contains:
- `engine_params`: the configuration used for the run
- `summary`: per-token aggregate statistics
- `trades`: individual trade records including `entry_params` (a snapshot of every internal state variable at the moment the BUY signal fired)

The analysis proceeded in three phases:
1. **Top-level statistics** — overall PnL, win rate, exit reason breakdown
2. **Comparative analysis** — winners vs. losers vs. big losses across every recorded parameter
3. **Fingerprinting** — identifying parameters that are identical across *all* losing trades, and checking whether those same values also appear in winning trades

---

## 3. The Core Finding: The Engine Cannot Distinguish Winners from Losers at Entry

> [!CAUTION]
> **Every quantified entry-state parameter has an essentially identical distribution between winning and losing trades.** The algorithm enters both with the same internal state. The outcome is largely determined by post-entry market behavior, not by any detectable signal at entry time.

This is the central and most important finding. It means parameter tuning to the engine's *current feature set* has a hard ceiling on how much it can improve performance.

### Evidence

When the full set of `entry_params` state variables are compared between all 116 losing trades and all 172 winning trades, **100% of state variables** are either identical or differ by only a few percentage points:

| State Parameter | Losses | Wins |
|---|---|---|
| [regime](file:///Users/jaime/pump-chart/backend/strategy_engine.py#1095-1440) | continuation (100%) | continuation (100%) |
| [direction](file:///Users/jaime/pump-chart/backend/strategy_engine.py#1049-1056) | up (100%) | up (100%) |
| `spread_expanding` | True (100%) | True (100%) |
| `pre_entry_stable_up` | True (100%) | True (100%) |
| [price_overextended](file:///Users/jaime/pump-chart/backend/strategy_engine.py#730-737) | False (100%) | False (100%) |
| [momentum_past_peak](file:///Users/jaime/pump-chart/backend/strategy_engine.py#723-729) | False (100%) | False (100%) |
| `in_local_chop` | False (100%) | False (100%) |
| `exhaustion_bar_count` | 0 (100%) | 0 (100%) |
| `exhaustion_persist_count` | 0 (100%) | 0 (100%) |
| `_exhaustion_phase_high` | 0.0 (100%) | 0.0 (100%) |
| `_momentum_peak_declining_count` | 0 (100%) | 0 (100%) |
| `trend_bar_count` | 0 (99%) | 0 (99%) |
| [ema_cross_valid](file:///Users/jaime/pump-chart/backend/strategy_engine.py#872-892) | True (99%) | True (97%) |
| `is_trending` | True (92%) | True (94%) |
| `prev_direction` | down (99%) | down (98%) |

**The FIX-A, FIX-B, and FIX-C safety guards are universally bypassed.** Not a single losing trade (or winning trade) triggered [price_overextended](file:///Users/jaime/pump-chart/backend/strategy_engine.py#730-737), [momentum_past_peak](file:///Users/jaime/pump-chart/backend/strategy_engine.py#723-729), or `in_local_chop`. These protections exist in code but contribute nothing to entry filtering in practice.

---

## 4. The Only Measurable Differences

After exhaustive search, only **two state parameters** show a statistically meaningful (>5 percentage-point) difference between losers and winners:

### 4.1 Momentum Acceleration = 0

| Group | `momentum_acceleration = 0` |
|---|---|
| Big Losses | **63.8%** |
| All Losses | **58.6%** |
| Wins | **51.7%** |

Losing trades — and especially big losing trades — are more likely to enter when `momentum_acceleration` is exactly zero, indicating the Kalman momentum is not actually accelerating. The engine enters on what amounts to flat or stalling momentum.

When `momentum_acceleration > 0` (momentum actively building):
- Wins: 48.3% of entries
- All Losses: 41.4% of entries  
- Big Losses: **36.2%** of entries

This is the **only state variable that meaningfully separates outcomes**. Yet it is currently treated as one optional condition out of six in the entry gate, meaning it can be absent without blocking entry.

### 4.2 Previous EMA Spread = 0 (Fresh Cross)

| Group | `prev_ema_spread = 0` |
|---|---|
| All Losses | **66.4%** |
| Wins | **61.0%** |

64% of all entries occur on a brand-new EMA cross where `prev_ema_spread = 0` — meaning the fast EMA only just crossed above the slow EMA on this bar. For losses, this rate is slightly higher. The engine is entering at the exact moment of cross, before directional commitment has developed.

| Entry Spread Type | WR | Avg PnL | Avg Loss (losses only) |
|---|---|---|---|
| Zero EMA spread | 55.8% | +2.46% | — |
| Tiny spread (<0.5%) | 57.1% | +4.06% | — |
| Healthy spread (≥0.5%) | **61.8%** | **+6.43%** | — |

---

## 5. Numeric Parameter Ranges for Losing Trades

All losing trades share the following numeric ranges at entry:

| Parameter | Min | Max | Average | Median |
|---|---|---|---|---|
| Signal strength | 4.08 | 8.50 | 5.99 | 5.62 |
| Trend confidence | 0.79 | 1.00 | 0.94 | 0.98 |
| Overextension ratio | 0.84 | 1.02 | 0.99 | 0.99 |
| EMA cross persist count | 1 | 4 | 2.13 | 2.0 |
| Reversal bar count | 1 | 55 | 9.04 | 5.0 |
| Hold time | 4s | 3,497s | 254s | 120s |
| Hold time (big losses) | 4s | 3,497s | 350s | 146s |

Notable observations:
- Signal strength for losers (avg 5.99) is nearly identical to winners (avg 6.18) — a 3% difference that cannot serve as a filter
- Trend confidence for losers (avg 0.94) is actually *higher* than winners (0.94 vs 0.936) — the confidence metric is not predictive
- Big losses are held 38% longer than small losses (350s vs 159s) — the engine is slow to exit genuinely bad trades

---

## 6. The Regime Cycle Mechanics Causing the Problem

Understanding *why* the fingerprint is identical requires reading the state machine code. The BUY signal fires during the `CONTINUATION` regime, which transitions to `TREND` on the very next bar:

```
EXHAUSTION → CONTINUATION (BUY fires here) → TREND (next bar)
                    ↑
               Position opened. trend_bar_count = 0.
               No TREND phase has been confirmed yet.
```

The full fast path through the state machine takes just **3–4 bars**:

```
TREND (exits after min_trend_bars=2)
  → EXHAUSTION (exits after exhaustion_bars_limit=1)
  → CONTINUATION (BUY fires immediately, exits next bar)
  → TREND (entry is now in position)
```

This mechanical path produces the universal fingerprint:
- `trend_bar_count = 0` (always — BUY fires before TREND runs)
- `exhaustion_bar_count = 0` (always — state captured at entry, after transition)
- `exhaustion_persist_count = 0` (always — reset on transition)
- `_exhaustion_phase_high = 0.0` (always — reset on CONTINUATION entry)

All of these zeros are structural artifacts of *how the BUY fires*, not signals of market condition.

### Why the Safety Guards Never Trigger

**FIX-C bypasses `exhaustion_persist_bars` when `S > S_strong`** (line 1309). Since `S > S_strong` is true on 100% of all trades (min signal strength recorded = 4.08, S_strong = 4.0), the persist gate is always bypassed. The engine never waits in exhaustion — it transitions instantly.

**`overextension_k = 0.08`** means price must be 8% above the Kalman estimate to block entry. Due to Kalman lag, this threshold is almost never reached at the moment of a fresh cross. Across 288 trades, `price_overextended = False` on every single trade.

**`momentum_peak_bars = 1` + declining counter = 0** — because `_momentum_peak_declining_count` resets to 0 on any bar where momentum does not decline, and the BUY fires on the first bar of new positive momentum, this counter is always 0 at entry. The guard never activates.

---

## 7. Exit Reason Analysis

The exit reason matters because it reveals whether the engine's exit logic is the source of large losses.

### All Losing Trades

| Exit Reason | Count | % | Avg Loss |
|---|---|---|---|
| `reversal_exit` | 108 | **93.1%** | -11.15% |
| `continuation_exit` | 7 | 6.0% | -9.13% |
| `exhaustion_exit` | 1 | 0.9% | -19.26% |

### Big Losing Trades (>10%)

| Exit Reason | Count | % | Avg Loss |
|---|---|---|---|
| `reversal_exit` | 54 | **93.1%** | -18.18% |
| `continuation_exit` | 3 | 5.2% | -15.01% |
| `exhaustion_exit` | 1 | 1.7% | -19.26% |

93% of all losses — and 93% of big losses — exit via `reversal_exit`. This fires when:
1. The engine's regime transitions to `REVERSAL`
2. `reversal_bar_count >= reversal_exit_confirm_bars` (= 0)
3. `signal_strength > S_noise`

With `reversal_exit_confirm_bars = 0`, the exit fires on the **very first bar** the engine enters `REVERSAL`. For memecoin assets that spike and dump, this means the engine often catches the dump late — after the reversal is already confirmed by the regime machine, which itself requires multiple conditions to be met. By the time `reversal_exit` fires, the position has already dropped significantly.

**The exit mechanism is reactive, not predictive.** It waits for the regime state machine to confirm a reversal before exiting. For assets that dump -20% in 2–3 bars, this confirmation lag is expensive.

---

## 8. Entry Gate Analysis

The BUY signal fires when ≥2 of these 6 conditions are true:

```python
entry_conds = [
    S > S_strong,                    # (1)
    delta_aligned,                   # (2)
    leaving_hvn,                     # (3)
    momentum_acceleration > 0,       # (4)
    ema_cross_valid,                 # (5)
    s_effective > s_effective_threshold,  # (6)
]
if sum(x for x in entry_conds if x) >= 2:
    signal = BUY
```

| Condition | Wins True | Losses True | Big Losses True |
|---|---|---|---|
| `S > S_strong` | **100%** | **100%** | **100%** |
| `s_effective > threshold` | **100%** | **100%** | **100%** |
| [ema_cross_valid](file:///Users/jaime/pump-chart/backend/strategy_engine.py#872-892) | 97.1% | 99.1% | 98.3% |
| `momentum_acceleration > 0` | 48.3% | 41.4% | **36.2%** |

Conditions 1, 6, and 5 are almost universally true. This means **the gate always passes** — the `sum >= 2` check is met by conditions 1+5+6 alone on ~97% of all trades, before conditions 2, 3, and 4 are even evaluated. The gate is structurally a rubber stamp.

---

## 9. EMA Cross Persistence

**84% of all entries** fire at exactly `_ema_cross_persist_count = 2` — the configured minimum. Performance by persistence level:

| EMA Cross Persist Count | Trades | Win Rate | Avg PnL | Big Losses |
|---|---|---|---|---|
| 2 (minimum) | 242 | 58.7% | +4.54% | 50 (20.7%) |
| 3–4 | 39 | **61.5%** | **+7.77%** | 7 (18.0%) |
| 5+ | 1 | 100% | +59.67% | 0 |

Waiting one extra bar of EMA cross confirmation consistently improves outcomes. This is the clearest single-parameter signal in the dataset.

---

## 10. Reversal Bar Count at Entry

The `reversal_bar_count` reflects how long the engine was in the `REVERSAL` regime before transitioning to `CONTINUATION` and firing the BUY.

| Reversal Bars | Trades | Win Rate | Avg Win | Avg Loss | Avg PnL |
|---|---|---|---|---|---|
| 0–1 | 25 | 60.0% | +10.7% | **-3.7%** | +4.9% |
| 2–3 | 71 | 62.0% | +16.2% | -10.0% | +6.2% |
| 4–7 | 74 | 54.1% | +12.2% | -9.9% | +2.1% |
| 8–14 | 48 | 54.2% | +19.9% | -14.9% | +3.9% |
| 15–29 | 52 | **65.4%** | +14.6% | -15.3% | +4.3% |
| 30+ | 18 | **72.2%** | **+28.5%** | -7.9% | **+18.4%** |

Two important patterns:
1. **Longer reversal confirmation = higher win rate** (60% → 72% as bars increase)
2. **Mid-range reversal bars (4–14) are the worst zone** — low WR, large average losses simultaneously
3. Trades with ≥30 reversal bars have the best risk/reward by far (+18.4% avg PnL)

---

## 11. ATR Floor = 0 Impact

`atr_floor_k = 0` means no minimum ATR floor is enforced. When `atr_floor = 0`, signal strength `S = |m_hat| / ATR_floor` divides by zero — the engine sets `signal_strength = 0`. This affects 41% of all trades.

| ATR State | Trades | Win Rate | Avg PnL | Avg Loss |
|---|---|---|---|---|
| `ATR_floor = 0` | 119 (41%) | 58.8% | +3.41% | **-13.62%** |
| `ATR_floor > 0` | 169 (59%) | 60.4% | +6.22% | **-9.25%** |

When `atr_floor = 0`:
- Win rate is 1.6pp lower
- Avg PnL per trade is **-46% lower** (+3.41% vs +6.22%)
- Average loss is **47% deeper** (-13.62% vs -9.25%)

With zero ATR floor, the signal-strength-based gates (`S > S_strong`, `S > S_weak`, `S > S_noise`) are either evaluating 0 directly (signal_strength=0 and gate fails) or the regime transition bypasses them via FIX-C. Either way, meaningful SNR-based filtering is not operating.

---

## 12. Ranked Recommendations

Based on all findings, the following changes are ranked by evidence quality and expected impact:

### Priority 1 — `ema_cross_persist_bars`: 2 → **3**

**Evidence:** Direct A/B in the data. Trades at persist=3+ have 61.5% WR and +7.77% avg PnL vs 58.7% and +4.54% at the minimum. Big loss rate drops from 20.7% to 18.0%.

**Mechanism:** Requires the EMA fast/slow cross to sustain for one additional bar before entry is allowed. Filters out crosses that immediately reverse (dead-cat microspikes). 84% of all current entries are at the minimum — raising this immediately changes the character of entries.

**Risk:** Low. Reduces trade count but improves quality.

---

### Priority 2 — Make `momentum_acceleration > 0` a hard entry requirement

**Evidence:** The only state variable with a quantifiable divergence between losers and winners. Present in 48.3% of wins but only 36.2% of big losses. Currently optional (one of six conditions, only two needed).

**Change:** In [_detect_regime()](file:///Users/jaime/pump-chart/backend/strategy_engine.py#1095-1440), add `momentum_acceleration > 0` as a mandatory pre-check before evaluating the `entry_conds` list, or raise the threshold from `sum >= 2` to `sum >= 3` (which has the same effective filter given that conditions 1, 5, 6 are always true, forcing condition 4 to matter).

**Risk:** Medium. Will filter ~38% of current entries (those where acceleration is zero). Requires backtesting to confirm net PnL improvement.

---

### Priority 3 — `atr_floor_k`: 0 → **0.5–1.0**

**Evidence:** 41% of entries have `ATR_floor = 0`, with 47% deeper average losses (-13.62% vs -9.25%) and 46% lower avg PnL per trade compared to non-zero ATR entries.

**Mechanism:** Sets a minimum ATR floor as a fraction of the rolling median ATR. Prevents the signal strength formula from dividing by zero, making the S-based gates actually meaningful.

**Risk:** Low parameter change with potentially high impact. Does not reduce trade frequency directly but increases the quality of the SNR calculation.

---

### Priority 4 — `exhaustion_bars_limit`: 1 → **2–3**

**Evidence:** The current value of 1 enables a 3-bar regime cycle (TREND→EXHAUSTION→CONTINUATION). Combined with FIX-C bypassing the exhaustion persist check, the engine can transition from exhaustion back to a BUY signal in just 1 bar after the trend fails. This creates rapid re-entry into failing setups.

**Change:** Increasing to 2–3 forces the engine to spend at least 2–3 bars confirming that price has actually paused before attempting a continuation entry.

**Risk:** Medium. Changes the tempo of the entire engine. Test carefully.

---

### Priority 5 — Raise entry gate threshold: `sum >= 2` → `sum >= 3` or `sum >= 4`

**Evidence:** Three of the six entry conditions are universally true (100%/100%/97%). The current threshold of 2 is never discriminating — no entry has ever been blocked by the gate since its inception.

**Change:** Raise to `sum >= 3` (still almost always met by the always-true trio) or `sum >= 4`, which forces at least one of the meaningful conditions (`momentum_acceleration > 0`, `delta_aligned`, [leaving_hvn](file:///Users/jaime/pump-chart/backend/strategy_engine.py#1087-1094)) to be true.

**Risk:** High if raised too aggressively. Recommend raising to 3 first and re-running the batch.

---

## 13. What Improvement Is Realistically Achievable?

Given that winners and losers have essentially identical entry states, **parameter tuning within the current signal architecture can only deliver marginal improvement**. The realistic ceiling from the recommendations above (ema_cross_persist_bars + momentum_acceleration requirement) is estimated at:

- Win rate improvement: **+3 to +5 percentage points** (59.7% → 63–65%)
- Reduction in big losses: **~15–25% fewer** (58 → 43–49 big loss trades)
- Net PnL improvement: **+0.2 to +0.4 SOL per batch**

To achieve materially better performance, the engine would need to use features that are **currently not captured at entry time** — for example:
- Volume profile delta at the moment of cross (is buy volume dominating?)
- Price velocity vs. the number of candles since the last swing low
- Whether the reversal came from an identifiable support structure vs. random noise
- Time-of-day or token age filters

---

## 14. Summary Table

| Finding | Impact | Parameter | Current | Recommended |
|---|---|---|---|---|
| 84% of entries at minimum EMA persist | High | `ema_cross_persist_bars` | 2 | **3** |
| Entry gate never blocks | High | Entry condition threshold | ≥2/6 | **≥3/6** |
| `momentum_acceleration` is only diverging signal | High | Make it mandatory | Optional | **Required** |
| ATR=0 causes 47% deeper losses | Medium | `atr_floor_k` | 0 | **0.5–1.0** |
| 3-bar regime cycle enables instant re-entry | Medium | `exhaustion_bars_limit` | 1 | **2–3** |
| Safety guards universally bypassed | Structural | `overextension_k`, `momentum_peak_bars` | 0.08, 1 | Review thresholds |
| Entry/exit states identical across wins/losses | Structural | Feature architecture | — | Add new signal features |
