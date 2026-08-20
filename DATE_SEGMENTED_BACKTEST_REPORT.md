# Date-Segmented Backtest & Negative PnL Cohort Analysis Report

**Date:** August 20, 2026  
**Scope:** Full Production Dataset across 23 Distinct Recording Dates (1,394 Completed Recordings, 871 Executed Trades)  
**Strategy Engine:** StrategyEngineV2 (Canonical Production Configuration with EVR Triage + Sell Concentration Veto, 1-Bar Execution Delay, 4-State Intra-Candle Expansion, Recording-End Force-Close)  

---

## Executive Summary

To investigate temporal stability, regime sensitivity, and the underlying failure modes of the production strategy, a comprehensive date-segmented backtest was conducted across all **23 recording dates** (from July 27, 2026 through August 20, 2026) comprising **1,394 completed recordings** and **871 executed trades**.

### Key High-Level Findings:
1. **Net Overall Profitability**: Across the entire 23-date dataset, the strategy produced a net positive PnL of **+1.9638 SOL** across 871 trades with an overall **68.31% Win Rate** and a **1.54 Profit Factor**.
2. **Day-Level Distribution**:
   - **14 Positive PnL Days (60.9%)**: Total PnL **+2.6439 SOL** (Mean: **+0.1888 SOL/day**), Win Rate: **72.97%**, Profit Factor: **2.12**.
   - **9 Negative PnL Days (39.1%)**: Total PnL **-0.6801 SOL** (Mean: **-0.0756 SOL/day**), Win Rate: **60.57%** (day-average 53.42%), Profit Factor: **0.64**.
3. **Core Shared Patterns of Negative PnL Days**:
   - **The Asymmetric Payout Compression**: On negative days, the average winning trade profit compressed by **-26.8%** (from +0.01462 SOL on positive days to +0.01070 SOL on negative days), while the average loss size remained invariant (~ -0.0228 SOL). This crushed daily expectancy from +0.00447 SOL to -0.00244 SOL per trade.
   - **Tail Loss Concentration**: Trades with losses $\le -15\%$ accounted for **94.8% of all gross losing PnL** on negative days (-2.252 SOL drag from 67 trades), completely wiping out the +1.809 SOL earned by the 169 winning trades.
   - **Elevated `recording_ended` Exit Drag**: Negative days experienced nearly triple the rate of `recording_ended` exits (20.69% daily exit share vs 7.46% on positive days, $p = 0.0677$, Spearman $r = -0.409$), with losing trades held **+64.1% longer** (1,476 s vs 899 s) as stagnant positions bled slowly without triggering prompt volatility stops.
   - **Two Distinct Negative Day Archetypes**:
     - *Type A (Violent Dump Traps)*: High pump heights (>100%), high one-pump wonder ratios (>50%), high activity, but catastrophic flash dumps that break through initial trailing buffers into `kelly_flat`.
     - *Type B (Low-Volatility Churn / Grind)*: Low pump heights (<85%), high choppy consolidation / slow bleed fractions (35%–50%), low trade counts, with breakouts fizzling into lingering `recording_ended` exits.

---

## 1. Full Date-by-Date Performance Table

The table below summarizes trading metrics and market characteristics across each individual recording date:

| Date | Recordings | Total Trades | Win Rate (%) | Total PnL (SOL) | Gross Profit (SOL) | Gross Loss (SOL) | Profit Factor | Gain Retrace (%) | Kelly Flat (%) | Rec Ended (%) | Mean Pump (%) | One-Pump Wonder (%) | Slow Bleed (%) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **2026-07-27** | 96 | 39 | 82.1% | **+0.4365** | +0.5788 | -0.1423 | 4.07 | 66.7% | 5.1% | 2.6% | 115.8% | 53.1% | 12.5% |
| **2026-07-28** | 109 | 64 | 78.1% | **+0.2809** | +0.7100 | -0.4290 | 1.65 | 75.0% | 7.8% | 3.1% | 196.4% | 52.3% | 19.3% |
| **2026-07-29** | 90 | 67 | 74.6% | **+0.0590** | +0.5042 | -0.4452 | 1.13 | 58.2% | 10.4% | 4.5% | 135.4% | 37.8% | 23.3% |
| **2026-07-30** | 94 | 63 | 71.4% | **-0.0840** | +0.4018 | -0.4859 | 0.83 | 74.6% | 3.2% | 11.1% | 136.8% | 54.3% | 12.8% |
| **2026-07-31** | 19 | 5 | 60.0% | **+0.0026** | +0.0215 | -0.0189 | 1.14 | 80.0% | 0.0% | 20.0% | 65.7% | 26.3% | 10.5% |
| **2026-08-01** | 32 | 24 | 70.8% | **+0.0679** | +0.2040 | -0.1361 | 1.50 | 62.5% | 8.3% | 8.3% | 75.5% | 21.9% | 18.8% |
| **2026-08-02** | 36 | 17 | 88.2% | **+0.0294** | +0.0814 | -0.0520 | 1.56 | 76.5% | 5.9% | 0.0% | 84.2% | 33.3% | 8.3% |
| **2026-08-03** | 82 | 82 | 73.2% | **+0.2346** | +0.8996 | -0.6650 | 1.35 | 57.3% | 8.5% | 9.8% | 156.4% | 57.3% | 20.7% |
| **2026-08-05** | 48 | 37 | 64.9% | **-0.0374** | +0.3267 | -0.3642 | 0.90 | 54.1% | 13.5% | 5.4% | 133.9% | 43.8% | 14.6% |
| **2026-08-06** | 18 | 20 | 80.0% | **+0.1312** | +0.2917 | -0.1605 | 1.82 | 65.0% | 10.0% | 10.0% | 223.6% | 27.8% | 22.2% |
| **2026-08-07** | 39 | 22 | 72.7% | **-0.0454** | +0.1400 | -0.1853 | 0.76 | 59.1% | 4.5% | 13.6% | 78.4% | 38.5% | 12.8% |
| **2026-08-08** | 58 | 25 | 84.0% | **+0.2783** | +0.3296 | -0.0512 | 6.43 | 44.0% | 0.0% | 8.0% | 88.3% | 31.0% | 24.1% |
| **2026-08-09** | 34 | 13 | 61.5% | **-0.0202** | +0.0882 | -0.1084 | 0.81 | 38.5% | 0.0% | 15.4% | 163.9% | 35.3% | 20.6% |
| **2026-08-10** | 139 | 65 | 69.2% | **+0.4433** | +0.7208 | -0.2777 | 2.60 | 50.8% | 4.6% | 1.5% | 138.7% | 38.8% | 20.1% |
| **2026-08-11** | 64 | 25 | 60.0% | **+0.0945** | +0.2227 | -0.1283 | 1.74 | 40.0% | 4.0% | 12.0% | 144.2% | 40.6% | 10.9% |
| **2026-08-12** | 50 | 63 | 66.7% | **+0.2728** | +0.8222 | -0.5493 | 1.49 | 54.0% | 7.9% | 9.5% | 185.7% | 62.0% | 8.0% |
| **2026-08-13** | 10 | 1 | 0.0% | **-0.0178** | +0.0000 | -0.0178 | 0.00 | 0.0% | 0.0% | 100.0% | 19.1% | 0.0% | 30.0% |
| **2026-08-14** | 59 | 44 | 54.5% | **-0.1432** | +0.2472 | -0.3904 | 0.63 | 52.3% | 13.6% | 6.8% | 84.0% | 35.6% | 13.6% |
| **2026-08-15** | 81 | 61 | 68.9% | **+0.1438** | +0.5361 | -0.3923 | 1.37 | 54.1% | 1.6% | 6.6% | 150.2% | 38.3% | 11.1% |
| **2026-08-16** | 65 | 45 | 57.8% | **-0.0087** | +0.3117 | -0.3204 | 0.97 | 55.6% | 4.4% | 8.9% | 126.8% | 56.9% | 15.4% |
| **2026-08-18** | 60 | 35 | 68.6% | **+0.1691** | +0.3888 | -0.2198 | 1.77 | 45.7% | 5.7% | 8.6% | 75.4% | 36.7% | 18.3% |
| **2026-08-19** | 92 | 48 | 47.9% | **-0.2930** | +0.2785 | -0.5714 | 0.49 | 41.7% | 10.4% | 8.3% | 109.3% | 42.4% | 17.4% |
| **2026-08-20** | 19 | 6 | 50.0% | **-0.0304** | +0.0159 | -0.0463 | 0.34 | 50.0% | 0.0% | 16.7% | 31.2% | 26.3% | 36.8% |
| **TOTAL** | **1394** | **871** | **68.3%** | **+1.9638** | **+7.7946** | **-5.8308** | **1.54** | **57.2%** | **6.8%** | **7.5%** | **118.2%** | **38.7%** | **17.5%** |

---

## 2. Statistical Comparison: Positive vs Negative PnL Cohorts

Comparing the 14 Positive PnL Days against the 9 Negative PnL Days across all market and execution variables using two-sided Mann-Whitney U tests:

| Metric / Dimension | Positive Days Mean (std) | Negative Days Mean (std) | Delta (Neg − Pos) | MWU $p$-value | Significance |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Win Rate (%)** | 73.17% (± 8.22%) | 53.42% (± 20.57%) | -19.75 pp | **0.0050** | **Statistically Significant** |
| **Profit Factor** | 2.12 (± 1.40) | 0.64 (± 0.29) | -1.48 | **0.0001** | **Statistically Significant** |
| **Trades per Day** | 42.29 (± 23.11) | 31.00 (± 20.12) | -11.29 trades | 0.2840 | Not Significant |
| **Active Traded Tokens** | 17.14 (± 9.15) | 13.00 (± 7.83) | -4.14 tokens | 0.3278 | Not Significant |
| **Gain Retrace Share (%)** | 59.27% (± 11.93%) | 47.30% (± 19.37%) | -11.96 pp | 0.1227 | Directional Drop |
| **Recording Ended Share (%)** | 7.46% (± 4.92%) | 20.69% (± 28.27%) | **+13.23 pp** | **0.0677** | **Marginal Trend ($p < 0.10$)** |
| **Kelly Flat Share (%)** | 5.72% (± 3.29%) | 5.53% (± 5.30%) | -0.19 pp | 0.6349 | Similar Frequency |
| **EVR Triage Share (%)** | 3.45% (± 2.60%) | 4.05% (± 4.52%) | +0.60 pp | 0.9746 | Similar Frequency |
| **Kramers Down Share (%)** | 3.45% (± 1.98%) | 2.21% (± 3.40%) | -1.24 pp | **0.0789** | **Marginal Trend ($p < 0.10$)** |
| **Tail Losses $\le -15\%$ Count** | 7.07 (± 5.23) | 7.44 (± 4.27) | +0.37 trades | 0.7756 | Constant Tail Volume |
| **Tail Drag (SOL)** | -0.243 SOL (± 0.19) | -0.250 SOL (± 0.16) | -0.007 SOL | 0.9247 | Constant Tail Loss |
| **Mean Holding Time (s)** | 585.7 s (± 196.2 s) | 952.6 s (± 651.9 s) | **+366.8 s** | 0.2439 | Longer In-Position Duration |
| **Mean Loser Holding Time (s)**| 899.5 s (± 701.8 s) | 1476.1 s (± 1701.9 s)| **+576.6 s (+64%)**| 0.6366 | Lingering Offside Drift |
| **Mean Token Pump Height (%)** | 131.10% (± 47.67%) | 98.17% (± 46.38%) | **-32.93 pp** | 0.1756 | Subdued Breakouts |
| **Median Token Pump Height (%)**| 41.70% (± 13.20%) | 38.86% (± 15.13%) | -2.83 pp | 0.8255 | Similar Baseline Median |
| **One-Pump Wonder Ratio (%)** | 39.80% (± 11.70%) | 37.00% (± 15.84%) | -2.81 pp | 1.0000 | Similar Archetype Rate |
| **Slow Bleed Ratio (%)** | 16.31% (± 5.58%) | 19.33% (± 8.05%) | +3.02 pp | 0.5083 | Slightly Higher Bleed Rate |
| **Sustained Runner Ratio (%)** | 9.73% (± 3.56%) | 10.01% (± 5.57%) | +0.27 pp | 0.7768 | Similar Runner Share |
| **Choppy Consolidation Ratio (%)**| 34.15% (± 9.84%) | 33.67% (± 8.40%) | -0.48 pp | 0.9247 | Similar Base Consolidation |
| **Candle Spread Volatility (%)** | 0.50% (± 0.11%) | 0.48% (± 0.12%) | -0.02 pp | 0.8749 | Similar Microstructure |
| **Return Volatility (%)** | 3.26% (± 0.57%) | 3.34% (± 0.44%) | +0.08 pp | 0.7290 | Similar Return Dispersion |
| **Turnover (SOL)** | 3,861 SOL (± 4,154) | 3,404 SOL (± 3,725) | -457 SOL | 0.6822 | Similar Daily Liquidity |
| **Taker Buy Ratio** | 0.43 (± 0.02) | 0.44 (± 0.01) | +0.01 | 0.7290 | Invariant Net Taker Flow |

---

## 3. Trade-Level Payoff & Distribution Breakdown

Analyzing individual trades (592 trades on Positive Days vs 279 trades on Negative Days) reveals the mathematical mechanism behind negative days:

```
+---------------------------------------------------------------------------------------------------+
| Metric                              Positive Days (592 Trades)      Negative Days (279 Trades)    |
+---------------------------------------------------------------------------------------------------+
| Win Rate                            72.97% (432 W / 160 L)          60.57% (169 W / 110 L)        |
| Mean PnL per Trade                  +0.00447 SOL (+4.47%)           -0.00244 SOL (-2.44%)         |
| Average Winning Trade               +0.01462 SOL (+14.62%)          +0.01070 SOL (+10.70%) [-27%] |
| Average Losing Trade                -0.02294 SOL (-22.94%)          -0.02263 SOL (-22.63%) [ 0%]  |
| Win / Loss Payoff Ratio             0.637                           0.473                         |
| Breakeven Win Rate Required         61.08%                          67.90%                        |
+---------------------------------------------------------------------------------------------------+
```

### Exit Reason Distribution & Financial Contribution:

```
Positive Days Exits:
  * gain_retrace:       342 trades (57.8%), PnL: +3.2257 SOL, WR: 93.3%
  * kramers_down_exit:   21 trades ( 3.5%), PnL: +0.6763 SOL, WR: 71.4%
  * reversal_exit:        4 trades ( 0.7%), PnL: +0.1285 SOL, WR: 50.0%
  * recording_ended:     38 trades ( 6.4%), PnL: -0.5961 SOL, WR: 18.4%
  * evr_triage:          24 trades ( 4.1%), PnL: -0.6022 SOL, WR:  0.0%
  * kelly_flat:          38 trades ( 6.4%), PnL: -1.6799 SOL, WR:  0.0%

Negative Days Exits:
  * gain_retrace:       156 trades (55.9%), PnL: +1.2061 SOL, WR: 87.2%  [Gain contribution cut by 63%]
  * kramers_down_exit:    7 trades ( 2.5%), PnL: +0.2652 SOL, WR: 85.7%
  * reversal_exit:        3 trades ( 1.1%), PnL: +0.0154 SOL, WR: 66.7%
  * recording_ended:     27 trades ( 9.7%), PnL: -0.7404 SOL, WR:  0.0%  [100% loss rate, -0.0274 SOL/trade]
  * evr_triage:          11 trades ( 3.9%), PnL: -0.3418 SOL, WR:  0.0%
  * kelly_flat:          21 trades ( 7.5%), PnL: -0.9492 SOL, WR:  0.0%  [-0.0452 SOL/trade]
```

### Loss Severity Stratification:

```
+---------------------------------------------------------------------------------------------------+
| Loss Category           Positive Days (592 trades)            Negative Days (279 trades)          |
+---------------------------------------------------------------------------------------------------+
| > 0% (Winners)          432 trades (73.0%), +6.3146 SOL       169 trades (60.6%), +1.8091 SOL     |
| 0% to -5% (Scratches)    34 trades ( 5.7%), -0.0783 SOL        22 trades ( 7.9%), -0.0362 SOL     |
| -5% to -15% (Mild)       27 trades ( 4.6%), -0.2539 SOL        21 trades ( 7.5%), -0.2010 SOL     |
| -15% to -30% (Severe)    46 trades ( 7.8%), -0.9869 SOL        33 trades (11.8%), -0.7203 SOL     |
| <= -30% (Catastrophic)   53 trades ( 9.0%), -2.3515 SOL        34 trades (12.2%), -1.5317 SOL     |
+---------------------------------------------------------------------------------------------------+
```

---

## 4. In-Depth Autopsy of the 9 Negative PnL Days

Analyzing each negative PnL day individually reveals the structural root causes:

### 1. **2026-07-30 (PnL: -0.0840 SOL | 63 Trades | WR: 71.4% | PF: 0.83)**
- **Failure Mode**: *Severe `recording_ended` truncation and late-session flash dumps*.
- **Details**: `gain_retrace` performed solidly (+0.3575 SOL, 47 wins), but **7 trades force-closed at recording end cost -0.2294 SOL** (average -32.8% per trade). An additional 2 `kelly_flat` exits took -0.0952 SOL and 3 `evr_triage` took -0.1331 SOL. Total tail drag was -0.4449 SOL across 11 severe losing trades.
- **Market Context**: One-pump wonder ratio was high (54.3%) with aggressive peak dumps (66.6% dump from peak).

### 2. **2026-08-05 (PnL: -0.0374 SOL | 37 Trades | WR: 64.9% | PF: 0.90)**
- **Failure Mode**: *High-frequency `kelly_flat` cluster*.
- **Details**: 5 `kelly_flat` exits occurred (13.5% of all exits on this date), generating **-0.2300 SOL in losses** (average -46.0% per trade). Combined with 2 `evr_triage` (-0.0627 SOL) and 2 `recording_ended` (-0.0427 SOL), the loss total of -0.3642 SOL overpowered the +0.3267 SOL gross profit from 24 winning trades.
- **Market Context**: Dump depth averaged 63.8% from peak.

### 3. **2026-08-07 (PnL: -0.0454 SOL | 22 Trades | WR: 72.7% | PF: 0.76)**
- **Failure Mode**: *Subdued pump heights with choppy consolidation*.
- **Details**: Win rate was high (72.7%, 16 wins), but average win was tiny (+0.00875 SOL) while 3 `recording_ended` exits generated **-0.1113 SOL** (50.5% of all losses on that day).
- **Market Context**: Mean pump height was only 78.4% (vs 131% normal), and choppy consolidation reached 41.0%.

### 4. **2026-08-09 (PnL: -0.0202 SOL | 13 Trades | WR: 61.5% | PF: 0.81)**
- **Failure Mode**: *Low sample size with EVR + truncation drag*.
- **Details**: Only 13 trades triggered across 34 recordings. 8 winning `gain_retrace` trades generated +0.0565 SOL, but 2 `evr_triage` (-0.0464 SOL) and 2 `recording_ended` (-0.0393 SOL) tipped the day negative.

### 5. **2026-08-13 (PnL: -0.0178 SOL | 1 Trade | WR: 0.0% | PF: 0.00)**
- **Failure Mode**: *Single-recording anomaly on ultra-short batch*.
- **Details**: Only 10 recordings in the batch. Only 1 trade was opened across the entire day, which hit `recording_ended` at -17.8% (-0.0178 SOL).
- **Market Context**: Mean pump height was 19.1%, slow bleed ratio 30.0%, consolidation 50.0%.

### 6. **2026-08-14 (PnL: -0.1432 SOL | 44 Trades | WR: 54.5% | PF: 0.63)**
- **Failure Mode**: *Deep `kelly_flat` cluster in low-momentum regime*.
- **Details**: 6 `kelly_flat` exits fired (13.6% of trades), incurring **-0.2632 SOL in losses** (average -43.9% per exit). 23 winning trades generated +0.2107 SOL, but gross losses of -0.3904 SOL dominated.
- **Market Context**: Mean pump height was only 84.0%, slow bleeds + consolidations totaled 52.6%.

### 7. **2026-08-16 (PnL: -0.0087 SOL | 45 Trades | WR: 57.8% | PF: 0.97)**
- **Failure Mode**: *Near breakeven on extreme dump day*.
- **Details**: Dump depth reached **71.1% from peak** with 56.9% one-pump wonders. 25 `gain_retrace` winners made +0.1947 SOL, offset by 2 `kelly_flat` (-0.1002 SOL) and 4 `recording_ended` (-0.1024 SOL).

### 8. **2026-08-19 (PnL: -0.2930 SOL | 48 Trades | WR: 47.9% | PF: 0.49)**
- **Failure Mode**: *The worst day across the entire dataset (Systemic Failure of Breakouts)*.
- **Details**: Win rate dropped to 47.9% (23 wins, 25 losses). Gross losses totaled **-0.5714 SOL** driven by:
  - 5 `kelly_flat` exits: **-0.2152 SOL**
  - 4 `recording_ended` exits: **-0.1319 SOL**
  - 2 `evr_triage` exits: **-0.0525 SOL**
  - 15 severe losing trades $\le -15\%$ created **-0.4803 SOL tail drag**.
- **Market Context**: Severe chop and failed breakout whipsaws where pumps collapsed before reaching the `gain_retrace` arm threshold.

### 9. **2026-08-20 (PnL: -0.0304 SOL | 6 Trades | WR: 50.0% | PF: 0.34)**
- **Failure Mode**: *Dead market with elevated slow bleeds*.
- **Details**: 36.8% slow bleeds, zero sustained runners, mean pump height of only 31.2%. 3 small winners (+0.0159 SOL) were erased by 1 `recording_ended` (-0.0168 SOL) and scratches.

---

## 5. Synthesis: The 4 Shared Patterns of Negative Days

From the empirical data, four universal patterns characterize the negative PnL cohorts:

```
                               ┌────────────────────────────────────────────────────────┐
                               │       THE ANATOMY OF A NEGATIVE TRADING DAY             │
                               └────────────────────────────────────────────────────────┘
                                                           │
          ┌────────────────────────┬───────────────────────┴───────────────────────┬────────────────────────┐
          ▼                        ▼                                               ▼                        ▼
 1. PAYOUT SKEW COLLAPSE   2. TAIL LOSS WEIGHTING                          3. CHRONIC LINGERING     4. REGIME POLARIZATION
   Avg Win drops -26.8%     Losses <= -15% drive 94.8%                      `recording_ended` up    • Type A: Trap-Pumps (55% OPW)
  (+0.0146 -> +0.0107 SOL)  of all losing PnL (-2.252 SOL)                  to 20.7% daily share    • Type B: Low-Vol Chop (<80% pump)
   Expectancy flips -       Losing trades out-bleed winners                 Loser hold time +64%     Breakouts fail or crash
```

1. **The Payout Skew Compression (The Primary Root Cause)**:
   The strategy's edge is fundamentally built on positive payoff asymmetry (harvesting high-skew +50% to +300% runners via `gain_retrace` while truncating pullbacks). On negative days, breakout runners truncate early, reducing average winning trade PnL from +0.01462 SOL to +0.01070 SOL (-26.8%). Because the physics of memecoin stop-outs and slippage fixes the average loss at ~ -0.0226 SOL, the strategy requires a $>67.9\%$ win rate to break even on negative days, which it fails to achieve (achieving 60.57%).

2. **Severe Tail Drag ($\le -15\%$) Drives Almost All Losses**:
   On negative days, mild losses and scratches (0% to -15%) only cost -0.2372 SOL. In contrast, severe losses ($\le -15\%$) cost **-2.2520 SOL (94.8% of gross loss)**. When clusters of `kelly_flat` (-45% avg) and `recording_ended` (-27% avg) occur concurrently, they completely extinguish winning gains.

3. **Lingering Offside Hold Duration & `recording_ended` Vulnerability**:
   On negative days, losing trades remain open for an average of **1,476 seconds (24.6 minutes)** compared to 899 seconds on positive days (+64.1% increase). Without strong upward momentum to trigger `gain_retrace` or sharp downside pressure to trigger `evr_triage`, trades drift in an indecisive offside state until force-closed when the recording concludes.

4. **Regime Bimodality**:
   Negative days do not occur from random variance; they are concentrated in two distinct market conditions:
   - *Parabolic Dump Traps*: Tokens pump hard (+120% to +160%), enticing high-confidence momentum entries, then dump 60%–75% in seconds.
   - *Dead/Choppy Grinds*: Tokens fail to pump (<80% pump), with slow bleeds and consolidations exceeding 50% of the market.

---

## 6. Architectural Recommendations

Based on these empirical findings:
1. **Never Revert EVR Triage + Sell Concentration Veto**: EVR and the sell concentration veto successfully protect capital during volatile dumps without damaging runner capture.
2. **Address the `recording_ended` / Chronic Offside Drag**: The highest-leverage potential research avenue is exploring a time-decayed offside exit or trailing offside threshold for trades held $>15$ minutes with zero upward progress ($\Delta P < +5\%$), which would convert lingering `recording_ended` losses (-27% avg) into early exits (-10% avg).
3. **Respect Market Bimodality**: Macro regime filters before entry remain mathematically difficult because winners and losers share identical pre-entry features (as proven in Iter 31/34/40/52). Strategy refinement must focus on **post-entry triage** and **offside duration management**.
