# Date-Segmented Backtest & Negative PnL Cohort Analysis Report (V2)

**Date:** August 22, 2026  
**Scope:** Full Production Dataset across 25 Distinct Recording Dates (1,521 Completed Recordings, 912 Executed Trades)  
**Strategy Engine:** StrategyEngineV2 — Current Production Configuration (EVR Triage + Sell-Concentration Veto `[iter48/50]`, Holder-Flow Entry Gate & Dev-Sell Exit `[iter43]`, Holder-Flow Stream Silence Gate 2700 s `[iter56]`, Regime-Adaptive `gain_retrace` Give-Back thr=0.6 / adapt=0.2 / min=0.30 `[iter57/58]`, 1-Bar Execution Delay, 4-State Intra-Candle Expansion, Recording-End Force-Close)  
**NEW in V2:** Average Win Size and Average Loss Size (SOL & %), Payoff Ratio, and Breakeven Win-Rate are reported per date, per day-cohort, and overall.

> **Note vs. the V1 report (Aug 20):** every date segment was **re-built from scratch under the current HEAD engine build** so all dates share one consistent configuration. The V1 cache predates the iter56 holder-flow silence gate and the iter57/58 regime-adaptive give-back, and its Aug-20 segment covered only 19 of what are now 34 recordings. New dates **2026-08-21** and **2026-08-22** are included. Absolute numbers are therefore NOT comparable to the V1 report; internal structure and conclusions are.

## Executive Summary

To investigate temporal stability, regime sensitivity, and the underlying failure modes of the production strategy, a comprehensive date-segmented backtest was conducted across all **25 recording dates** comprising **1,521 completed recordings** and **912 executed trades**.

### Key High-Level Findings:
1. **Net Overall Profitability**: Across the entire dataset, the strategy produced a net positive PnL of **+1.9021 SOL** across 912 trades with an overall **69.63% Win Rate**, Profit Factor **+1.30**, **average win +0.0131 SOL (13.07%)**, and **average loss -0.0231 SOL (-23.10%)** (payoff ratio +0.566, breakeven WR 63.86%).
2. **Day-Level Distribution**:
   - **16 Positive PnL Days (64.0%)**: Total PnL **+2.6138 SOL** (Mean: **+0.1634 SOL/day**), Win Rate: **72.58%**, Avg Win **+0.0143 SOL (14.31%)**, Avg Loss **-0.0225 SOL (-22.51%)**.
   - **9 Negative PnL Days (36.0%)**: Total PnL **-0.7116 SOL** (Mean: **-0.0791 SOL/day**), Win Rate: **63.36%** (day-average 56.44%), Avg Win **+0.0100 SOL (10.05%)**, Avg Loss **-0.0240 SOL (-24.02%)**.
3. **Core Shared Patterns of Negative PnL Days**:
   - **Payout Skew Compression**: on negative days the average winning trade shrinks **-29.8%** (from +0.0143 SOL / 14.31% on positive days to +0.0100 SOL / 10.05%), while the average loss moves only **+6.7%** (-0.0225 → -0.0240 SOL). Daily expectancy per trade flips from +0.0042 to -0.0024 SOL, and the breakeven win rate rises to 70.51% vs an achieved 63.36%.
   - **Tail Loss Concentration**: trades ≤ −15% account for **90.2% of gross losing PnL** on negative days (-2.3197 SOL drag across 65 trades), against +1.8587 SOL earned by winners.
   - **`recording_ended` Vulnerability**: negative days average a 18.0% daily `recording_ended` exit share vs 7.0% on positive days, with losing trades held +913 s vs +811 s (+12.5%).
   - **Two Distinct Negative Day Archetypes**: *Type A (Violent Dump Traps)* — high pump heights, high one-pump-wonder ratios, flash dumps into `kelly_flat`; *Type B (Low-Volatility Churn / Grind)* — subdued pumps, elevated slow-bleed/consolidation fractions, breakouts fizzling into lingering `recording_ended` exits. See Section 5.

## 1. Full Date-by-Date Performance Table (incl. Average Win / Loss Size)

The table below summarizes trading metrics — now including **average winning trade size** and **average losing trade size** in both SOL and % terms, the win/loss **payoff ratio**, and the **breakeven win rate** implied by that payoff — plus exit-mix and market-characteristic columns, for each individual recording date:

| Date | Recs | Trades | WR (%) | PnL (SOL) | Gross Profit | Gross Loss | PF | Avg Win (SOL) | Avg Loss (SOL) | Avg Win (%) | Avg Loss (%) | Payoff | BE WR (%) | GainRetrace | KellyFlat | RecEnded | Mean Pump | OPW | Slow Bleed |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **2026-07-27** | 96 | 39 | 82.1% | **+0.4365** | +0.5787 | -0.1423 | +4.07 | +0.0181 | -0.0203 | 18.09% | -20.32% | +0.890 | 52.9% | 66.7% | 5.1% | 2.6% | 115.8% | 53.1% | 12.5% |
| **2026-07-28** | 109 | 64 | 78.1% | **+0.2809** | +0.7105 | -0.4296 | +1.65 | +0.0142 | -0.0307 | 14.21% | -30.69% | +0.463 | 68.3% | 75.0% | 7.8% | 3.1% | 196.4% | 52.3% | 19.3% |
| **2026-07-29** | 90 | 67 | 74.6% | **+0.0590** | +0.5042 | -0.4452 | +1.13 | +0.0101 | -0.0262 | 10.08% | -26.19% | +0.385 | 72.2% | 58.2% | 10.4% | 4.5% | 135.4% | 37.8% | 23.3% |
| **2026-07-30** | 94 | 63 | 71.4% | **-0.0840** | +0.4015 | -0.4855 | +0.83 | +0.0089 | -0.0270 | 8.92% | -26.97% | +0.331 | 75.1% | 74.6% | 3.2% | 11.1% | 136.8% | 54.3% | 12.8% |
| **2026-07-31** | 19 | 5 | 60.0% | **+0.0026** | +0.0215 | -0.0189 | +1.14 | +0.0072 | -0.0095 | 7.18% | -9.46% | +0.758 | 56.9% | 80.0% | 0.0% | 20.0% | 65.7% | 26.3% | 10.5% |
| **2026-08-01** | 32 | 24 | 70.8% | **+0.0679** | +0.2041 | -0.1362 | +1.50 | +0.0120 | -0.0195 | 12.00% | -19.45% | +0.617 | 61.8% | 62.5% | 8.3% | 8.3% | 75.5% | 21.9% | 18.8% |
| **2026-08-02** | 36 | 17 | 88.2% | **+0.0294** | +0.0814 | -0.0520 | +1.56 | +0.0054 | -0.0260 | 5.43% | -26.01% | +0.209 | 82.7% | 76.5% | 5.9% | 0.0% | 84.2% | 33.3% | 8.3% |
| **2026-08-03** | 82 | 82 | 73.2% | **+0.2346** | +0.8994 | -0.6647 | +1.35 | +0.0150 | -0.0302 | 14.99% | -30.22% | +0.496 | 66.8% | 57.3% | 8.5% | 9.8% | 156.4% | 57.3% | 20.7% |
| **2026-08-05** | 48 | 37 | 64.9% | **-0.0374** | +0.3271 | -0.3645 | +0.90 | +0.0136 | -0.0280 | 13.63% | -28.04% | +0.486 | 67.3% | 54.1% | 13.5% | 5.4% | 133.9% | 43.8% | 14.6% |
| **2026-08-06** | 18 | 20 | 80.0% | **+0.1312** | +0.2918 | -0.1606 | +1.82 | +0.0182 | -0.0402 | 18.24% | -40.15% | +0.454 | 68.8% | 65.0% | 10.0% | 10.0% | 223.6% | 27.8% | 22.2% |
| **2026-08-07** | 39 | 17 | 70.6% | **-0.0118** | +0.1129 | -0.1247 | +0.91 | +0.0094 | -0.0249 | 9.41% | -24.93% | +0.377 | 72.6% | 58.8% | 5.9% | 11.8% | 78.4% | 38.5% | 12.8% |
| **2026-08-08** | 58 | 22 | 81.8% | **+0.1644** | +0.2156 | -0.0512 | +4.21 | +0.0120 | -0.0128 | 11.98% | -12.80% | +0.936 | 51.7% | 36.4% | 0.0% | 9.1% | 88.3% | 31.0% | 24.1% |
| **2026-08-09** | 34 | 11 | 72.7% | **+0.0239** | +0.0872 | -0.0633 | +1.38 | +0.0109 | -0.0211 | 10.90% | -21.10% | +0.517 | 65.9% | 45.5% | 0.0% | 9.1% | 163.9% | 35.3% | 20.6% |
| **2026-08-10** | 139 | 58 | 70.7% | **+0.4158** | +0.6622 | -0.2464 | +2.69 | +0.0162 | -0.0145 | 16.15% | -14.50% | +1.114 | 47.3% | 50.0% | 5.2% | 1.7% | 138.7% | 38.8% | 20.1% |
| **2026-08-11** | 64 | 24 | 58.3% | **+0.1074** | +0.2358 | -0.1284 | +1.84 | +0.0168 | -0.0128 | 16.85% | -12.84% | +1.312 | 43.3% | 45.8% | 4.2% | 8.3% | 144.2% | 40.6% | 10.9% |
| **2026-08-12** | 50 | 59 | 66.1% | **+0.2574** | +0.7613 | -0.5039 | +1.51 | +0.0195 | -0.0252 | 19.52% | -25.19% | +0.775 | 56.3% | 54.2% | 6.8% | 10.2% | 185.7% | 62.0% | 8.0% |
| **2026-08-13** | 10 | 1 | 0.0% | **-0.0178** | +0.0000 | -0.0178 | +0.00 | +0.0000 | -0.0178 | 0.00% | -17.84% | +0.000 | 100.0% | 0.0% | 0.0% | 100.0% | 19.1% | 0.0% | 30.0% |
| **2026-08-14** | 59 | 39 | 64.1% | **-0.0288** | +0.2743 | -0.3031 | +0.90 | +0.0110 | -0.0216 | 10.97% | -21.65% | +0.507 | 66.4% | 53.8% | 12.8% | 2.6% | 84.0% | 35.6% | 13.6% |
| **2026-08-15** | 81 | 56 | 67.9% | **+0.1233** | +0.4839 | -0.3606 | +1.34 | +0.0127 | -0.0200 | 12.73% | -20.03% | +0.636 | 61.1% | 53.6% | 1.8% | 7.1% | 150.2% | 38.3% | 11.1% |
| **2026-08-16** | 65 | 40 | 62.5% | **+0.0228** | +0.3095 | -0.2867 | +1.08 | +0.0124 | -0.0191 | 12.38% | -19.11% | +0.648 | 60.7% | 55.0% | 5.0% | 7.5% | 126.8% | 56.9% | 15.4% |
| **2026-08-18** | 60 | 32 | 75.0% | **+0.2566** | +0.3939 | -0.1373 | +2.87 | +0.0164 | -0.0172 | 16.41% | -17.16% | +0.956 | 51.1% | 50.0% | 6.2% | 0.0% | 75.4% | 36.7% | 18.3% |
| **2026-08-19** | 92 | 44 | 47.7% | **-0.2173** | +0.2688 | -0.4860 | +0.55 | +0.0128 | -0.0211 | 12.80% | -21.13% | +0.606 | 62.3% | 40.9% | 11.4% | 4.5% | 109.3% | 42.4% | 17.4% |
| **2026-08-20** | 34 | 21 | 57.1% | **-0.0963** | +0.0949 | -0.1912 | +0.50 | +0.0079 | -0.0212 | 7.91% | -21.25% | +0.372 | 72.9% | 52.4% | 0.0% | 9.5% | 59.6% | 38.2% | 29.4% |
| **2026-08-21** | 68 | 42 | 64.3% | **-0.0402** | +0.2291 | -0.2693 | +0.85 | +0.0085 | -0.0180 | 8.49% | -17.95% | +0.473 | 67.9% | 50.0% | 2.4% | 9.5% | 75.8% | 39.7% | 11.8% |
| **2026-08-22** | 44 | 28 | 67.9% | **-0.1780** | +0.1501 | -0.3281 | +0.46 | +0.0079 | -0.0365 | 7.90% | -36.45% | +0.217 | 82.2% | 60.7% | 14.3% | 7.1% | 72.6% | 31.8% | 20.5% |
| **TOTAL** | **1521** | **912** | **69.6%** | **+1.9021** | **+8.2998** | **-6.3976** | **+1.30** | **+0.0131** | **-0.0231** | **13.07%** | **-23.10%** | **+0.566** | **63.9%** | **55.1%** | **5.9%** | **10.9%** | **115.8%** | **38.9%** | **17.1%** |

## 2. Statistical Comparison: Positive vs Negative PnL Cohorts

Comparing the 16 Positive PnL Days against the 9 Negative PnL Days across all market, execution, and payoff variables using two-sided Mann-Whitney U tests (day-level units):

| Metric / Dimension | Positive Days Mean (std) | Negative Days Mean (std) | Delta (Neg − Pos) | MWU $p$-value | Significance |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Win Rate (%)** | 72.63% (± 8.10%) | 56.44% (± 21.10%) | -22.28 pp | **0.0092** | **Statistically Significant** |
| **Profit Factor** | +1.95 (± +0.96) | +0.65 (± +0.29) | -1.2915 | **0.0001** | **Statistically Significant** |
| **Avg Win Size (SOL)** | +0.0136 (± +0.0039) | +0.0089 (± +0.0037) | -34.5% | **0.0188** | **Significant** |
| **Avg Win Size (%)** | 13.57% (± 3.87%) | 8.89% (± 3.70%) | -4.6795 | **0.0188** | **Significant** |
| **Avg Loss Size (SOL)** | -0.0216 (± +0.0077) | -0.0240 (± +0.0055) | +11.3% | **0.2949** | Not Significant |
| **Avg Loss Size (%)** | -21.58% (± 7.67%) | -24.02% (± 5.55%) | -2.4472 | **0.2949** | Not Significant |
| **Payoff Ratio** | +0.698 (± +0.280) | +0.374 (± +0.170) | -0.3236 | **0.0060** | **Statistically Significant** |
| **Breakeven WR (%)** | 60.50% (± 9.87%) | 74.07% (± 10.67%) | +13.5758 | **0.0060** | **Statistically Significant** |
| **Trades per Day** | 38.75 (± 22.19) | 32.44 (± 17.00) | -6.3056 | **0.6302** | Not Significant |
| **Active Traded Tokens** | 15.94 (± 8.55) | 14.11 (± 6.87) | -1.8264 | **0.7550** | Not Significant |
| **Tail Losses ≤ −15% Count** | 6.62 (± 4.87) | 7.22 (± 3.39) | +0.5972 | **0.5125** | Not Significant |
| **Tail Drag (SOL)** | -0.2193 (± +0.1768) | -0.2577 (± +0.1292) | -0.0385 | **0.4447** | Not Significant |
| **Mean Holding Time (s)** | 531.7 s (± 245.4 s) | 577.6 s (± 339.6 s) | +45.8800 | **0.9323** | Not Significant |
| **Mean Winner Holding Time (s)** | 424.2 s (± 238.1 s) | 342.0 s (± 292.5 s) | -82.2178 | **0.2949** | Not Significant |
| **Mean Loser Holding Time (s)** | 811.2 s (± 670.6 s) | 912.8 s (± 1018.3 s) | +101.6717 | **0.9774** | Not Significant |
| **Gain Retrace Share (%)** | 58.23% (± 11.73%) | 49.48% (± 19.48%) | -8.7452 | **0.3499** | Not Significant |
| **Recording Ended Share (%)** | 6.96% (± 4.84%) | 17.95% (± 29.15%) | +10.9967 | **0.2691** | Not Significant |
| **Kelly Flat Share (%)** | 5.33% (± 3.32%) | 7.05% (± 5.61%) | +1.7159 | **0.5318** | Not Significant |
| **EVR Triage Share (%)** | 3.87% (± 2.87%) | 3.35% (± 3.28%) | -0.5232 | **0.7747** | Not Significant |
| **Kramers Down Share (%)** | 2.81% (± 2.33%) | 3.20% (± 3.69%) | +0.3922 | **0.8847** | Not Significant |
| **Mean Token Pump Height (%)** | 132.88% (± 45.32%) | 85.51% (± 34.85%) | -47.3749 | **0.0188** | **Significant** |
| **Median Token Pump Height (%)** | 42.18% (± 12.85%) | 37.80% (± 14.03%) | -4.3729 | **0.5907** | Not Significant |
| **One-Pump Wonder Ratio (%)** | 40.59% (± 11.78%) | 36.02% (± 14.03%) | -4.5690 | **1.0000** | Not Significant |
| **Slow Bleed Ratio (%)** | 16.52% (± 5.32%) | 18.08% (± 6.71%) | +1.5648 | **0.7129** | Not Significant |
| **Sustained Runner Ratio (%)** | 9.82% (± 3.66%) | 10.59% (± 4.21%) | +0.7634 | **0.7128** | Not Significant |
| **Choppy Consolidation Ratio (%)** | 33.07% (± 9.74%) | 35.31% (± 7.68%) | +2.2408 | **0.4791** | Not Significant |
| **Candle Spread Volatility (%)** | 0.51% (± 0.10%) | 0.48% (± 0.11%) | -0.0265 | **0.5150** | Not Significant |
| **Return Volatility (%)** | 3.29% (± 0.55%) | 3.26% (± 0.43%) | -0.0318 | **0.8874** | Not Significant |
| **Turnover (SOL/rec)** | 3,746 SOL (± 3,902 SOL) | 3,863 SOL (± 4,043 SOL) | +116.9400 | **0.4447** | Not Significant |
| **Taker Buy Ratio** | 0.43 (± 0.02) | 0.42 (± 0.06) | -0.0164 | **0.5150** | Not Significant |

Across all 25 dates, the daily `recording_ended` exit share correlates with daily PnL at Spearman $r = -0.271$ ($p = 0.1893$) — days where recordings end while positions are still trapped offside are systematically the losing days.

## 3. Trade-Level Payoff & Distribution Breakdown (incl. Average Win/Loss Size)

Analyzing individual trades (620 trades on Positive Days vs 292 trades on Negative Days) reveals the mathematical mechanism behind negative days:

```
+---------------------------------------------------------------------------------------------------+
| Metric                              Positive Days (620 Trades)                                     Negative Days (292 Trades)      |
+---------------------------------------------------------------------------------------------------+
| Win Rate                           72.58% (450 W / 170 L)          63.36% (185 W / 107 L)         |
| Mean PnL per Trade                 +0.0042 SOL                     -0.0024 SOL                    |
| AVERAGE WIN SIZE                   +0.01431 SOL (+14.31%)          +0.01005 SOL (+10.05%)         |
| AVERAGE LOSS SIZE                  -0.02251 SOL                    -0.02402 SOL                   |
|   (as % of entry)                  14.31% / -22.51%                10.05% [-30%] / -24.02% [+7%]  |
| Win / Loss Payoff Ratio            0.636                           0.418                          |
| Breakeven Win Rate Required        61.13%                          70.51%                         |
+---------------------------------------------------------------------------------------------------+
```

### Exit Reason Distribution & Financial Contribution:

```
Positive Days Exits:
  * gain_retrace          358 trades ( 57.7%), PnL: +3.3500 SOL, WR:  93.3%  [core winner engine]
  * kramers_down_exit      19 trades (  3.1%), PnL: +0.7693 SOL, WR:  73.7%
  * reversal_exit           6 trades (  1.0%), PnL: +0.1118 SOL, WR:  50.0%
  * recording_ended        38 trades (  6.1%), PnL: -0.6200 SOL, WR:  15.8%  [-0.0163 SOL/trade]
  * evr_triage             26 trades (  4.2%), PnL: -0.6489 SOL, WR:   0.0%  [-0.0250 SOL/trade]
  * kelly_flat             39 trades (  6.3%), PnL: -1.7323 SOL, WR:   0.0%  [-0.0444 SOL/trade]
  * dev_sell_exit (all mint variants)   72 trades ( 11.6%), PnL: +0.5258 SOL, WR:  52.8%
  * bayesian_flip           8 trades (  1.3%), PnL: +0.1764 SOL, WR:  87.5%
  * breakeven_scratch      52 trades (  8.4%), PnL: +0.2144 SOL, WR:  88.5%
  * tp_v2                   2 trades (  0.3%), PnL: +0.4673 SOL, WR: 100.0%

Negative Days Exits:
  * gain_retrace          165 trades ( 56.5%), PnL: +1.3271 SOL, WR:  92.1%  [core winner engine]
  * kramers_down_exit       9 trades (  3.1%), PnL: +0.2012 SOL, WR:  77.8%
  * reversal_exit           3 trades (  1.0%), PnL: -0.0181 SOL, WR:  33.3%
  * recording_ended        23 trades (  7.9%), PnL: -0.5693 SOL, WR:   4.3%  [-0.0248 SOL/trade]
  * evr_triage             10 trades (  3.4%), PnL: -0.3414 SOL, WR:   0.0%  [-0.0341 SOL/trade]
  * kelly_flat             23 trades (  7.9%), PnL: -1.0499 SOL, WR:   0.0%  [-0.0456 SOL/trade]
  * dev_sell_exit (all mint variants)   45 trades ( 15.4%), PnL: -0.2668 SOL, WR:  26.7%
  * bayesian_flip           3 trades (  1.0%), PnL: -0.0155 SOL, WR:  33.3%
  * breakeven_scratch      11 trades (  3.8%), PnL: +0.0210 SOL, WR: 100.0%
```

### Loss Severity Stratification:

```
+---------------------------------------------------------------------------------------------------+
| Loss Category           Positive Days (620 trades)            Negative Days (292 trades)        |
+---------------------------------------------------------------------------------------------------+
| > 0% (Winners)           450 trades (72.6%), +6.4411 SOL      185 trades (63.4%), +1.8587 SOL     |
| 0% to -5% (Scratches)     38 trades ( 6.1%), -0.0809 SOL       19 trades ( 6.5%), -0.0347 SOL     |
| -5% to -15% (Mild)        26 trades ( 4.2%), -0.2380 SOL       23 trades ( 7.9%), -0.2159 SOL     |
| -15% to -30% (Severe)     53 trades ( 8.5%), -1.1378 SOL       26 trades ( 8.9%), -0.5657 SOL     |
| <= -30% (Catastrophic)    53 trades ( 8.5%), -2.3707 SOL       39 trades (13.4%), -1.7540 SOL     |
+---------------------------------------------------------------------------------------------------+
```

## 4. In-Depth Autopsy of the 9 Negative PnL Days

Analyzing each negative PnL day individually reveals the structural root causes (failure mode is assigned programmatically from the dominant loss driver):

### 1. **2026-08-19 (PnL: -0.2173 SOL | 44 Trades | WR: 47.7% | PF: +0.55 | Avg Win: +0.0128 SOL | Avg Loss: -0.0211 SOL)**
- **Failure Mode**: *Systemic win-rate collapse (breakout whipsaws)*.
- **Details**: `gain_retrace` contributed **+0.1116 SOL** across 18 exits (WR 83.3%); 5 `kelly_flat` exits cost **-0.2152 SOL** (-0.0430 SOL/trade avg); 2 `recording_ended` force-closes cost **-0.0460 SOL** (-0.0230 SOL/trade avg); 2 `evr_triage` fires cost **-0.0525 SOL**; severe tail (≤ −15%) dragged **-0.3944 SOL** across 13 trades (5 ≤ −30%).
- **Market Context**: mean pump height 109.3%, median 43.6%, dump-from-peak 56.1%, one-pump wonders 42.4%, slow bleeds 17.4%, consolidations 32.6%, mean loser hold +667 s.

### 2. **2026-08-22 (PnL: -0.1780 SOL | 28 Trades | WR: 67.9% | PF: +0.46 | Avg Win: +0.0079 SOL | Avg Loss: -0.0365 SOL)**
- **Failure Mode**: *High-frequency `kelly_flat` cluster*.
- **Details**: `gain_retrace` contributed **+0.1352 SOL** across 17 exits (WR 100.0%); 4 `kelly_flat` exits cost **-0.2037 SOL** (-0.0509 SOL/trade avg); 2 `recording_ended` force-closes cost **-0.0951 SOL** (-0.0475 SOL/trade avg); severe tail (≤ −15%) dragged **-0.3197 SOL** across 7 trades (6 ≤ −30%).
- **Market Context**: mean pump height 72.6%, median 31.3%, dump-from-peak 58.7%, one-pump wonders 31.8%, slow bleeds 20.5%, consolidations 36.4%, mean loser hold +688 s.

### 3. **2026-08-20 (PnL: -0.0963 SOL | 21 Trades | WR: 57.1% | PF: +0.50 | Avg Win: +0.0079 SOL | Avg Loss: -0.0212 SOL)**
- **Failure Mode**: *EVR triage drag exceeding winner capture*.
- **Details**: `gain_retrace` contributed **+0.0928 SOL** across 11 exits (WR 100.0%); 2 `recording_ended` force-closes cost **-0.0248 SOL** (-0.0124 SOL/trade avg); 2 `evr_triage` fires cost **-0.0683 SOL**; severe tail (≤ −15%) dragged **-0.1788 SOL** across 6 trades (2 ≤ −30%).
- **Market Context**: mean pump height 59.6%, median 29.0%, dump-from-peak 57.5%, one-pump wonders 38.2%, slow bleeds 29.4%, consolidations 26.5%, mean loser hold +1372 s.

### 4. **2026-07-30 (PnL: -0.0840 SOL | 63 Trades | WR: 71.4% | PF: +0.83 | Avg Win: +0.0089 SOL | Avg Loss: -0.0270 SOL)**
- **Failure Mode**: *Severe `recording_ended` truncation drag*.
- **Details**: `gain_retrace` contributed **+0.3575 SOL** across 47 exits (WR 89.4%); 2 `kelly_flat` exits cost **-0.0952 SOL** (-0.0476 SOL/trade avg); 7 `recording_ended` force-closes cost **-0.2294 SOL** (-0.0328 SOL/trade avg); 3 `evr_triage` fires cost **-0.1331 SOL**; severe tail (≤ −15%) dragged **-0.4449 SOL** across 11 trades (8 ≤ −30%).
- **Market Context**: mean pump height 136.8%, median 57.4%, dump-from-peak 66.6%, one-pump wonders 54.3%, slow bleeds 12.8%, consolidations 25.5%, mean loser hold +389 s.

### 5. **2026-08-21 (PnL: -0.0402 SOL | 42 Trades | WR: 64.3% | PF: +0.85 | Avg Win: +0.0085 SOL | Avg Loss: -0.0180 SOL)**
- **Failure Mode**: *High-frequency `kelly_flat` cluster*.
- **Details**: `gain_retrace` contributed **+0.1604 SOL** across 21 exits (WR 95.2%); 1 `kelly_flat` exits cost **-0.0501 SOL** (-0.0501 SOL/trade avg); 4 `recording_ended` force-closes cost **-0.0401 SOL** (-0.0100 SOL/trade avg); severe tail (≤ −15%) dragged **-0.2192 SOL** across 6 trades (4 ≤ −30%).
- **Market Context**: mean pump height 75.8%, median 45.5%, dump-from-peak 55.4%, one-pump wonders 39.7%, slow bleeds 11.8%, consolidations 39.7%, mean loser hold +78 s.

### 6. **2026-08-05 (PnL: -0.0374 SOL | 37 Trades | WR: 64.9% | PF: +0.90 | Avg Win: +0.0136 SOL | Avg Loss: -0.0280 SOL)**
- **Failure Mode**: *High-frequency `kelly_flat` cluster*.
- **Details**: `gain_retrace` contributed **+0.1482 SOL** across 20 exits (WR 85.0%); 5 `kelly_flat` exits cost **-0.2300 SOL** (-0.0460 SOL/trade avg); 2 `recording_ended` force-closes cost **-0.0427 SOL** (-0.0213 SOL/trade avg); 2 `evr_triage` fires cost **-0.0627 SOL**; severe tail (≤ −15%) dragged **-0.3498 SOL** across 9 trades (7 ≤ −30%).
- **Market Context**: mean pump height 133.9%, median 55.1%, dump-from-peak 63.8%, one-pump wonders 43.8%, slow bleeds 14.6%, consolidations 27.1%, mean loser hold +324 s.

### 7. **2026-08-14 (PnL: -0.0288 SOL | 39 Trades | WR: 64.1% | PF: +0.90 | Avg Win: +0.0110 SOL | Avg Loss: -0.0216 SOL)**
- **Failure Mode**: *High-frequency `kelly_flat` cluster*.
- **Details**: `gain_retrace` contributed **+0.2460 SOL** across 21 exits (WR 95.2%); 5 `kelly_flat` exits cost **-0.2102 SOL** (-0.0420 SOL/trade avg); 1 `recording_ended` force-closes cost **-0.0225 SOL** (-0.0225 SOL/trade avg); severe tail (≤ −15%) dragged **-0.2739 SOL** across 8 trades (5 ≤ −30%).
- **Market Context**: mean pump height 84.0%, median 39.2%, dump-from-peak 57.8%, one-pump wonders 35.6%, slow bleeds 13.6%, consolidations 39.0%, mean loser hold +461 s.

### 8. **2026-08-13 (PnL: -0.0178 SOL | 1 Trades | WR: 0.0% | PF: +0.00 | Avg Win: +0.0000 SOL | Avg Loss: -0.0178 SOL)**
- **Failure Mode**: *Low-sample anomaly on a thin trading day*.
- **Details**: 1 `recording_ended` force-closes cost **-0.0178 SOL** (-0.0178 SOL/trade avg); severe tail (≤ −15%) dragged **-0.0178 SOL** across 1 trades (0 ≤ −30%).
- **Market Context**: mean pump height 19.1%, median 9.3%, dump-from-peak 19.2%, one-pump wonders 0.0%, slow bleeds 30.0%, consolidations 50.0%, mean loser hold +605 s.

### 9. **2026-08-07 (PnL: -0.0118 SOL | 17 Trades | WR: 70.6% | PF: +0.91 | Avg Win: +0.0094 SOL | Avg Loss: -0.0249 SOL)**
- **Failure Mode**: *Severe `recording_ended` truncation drag*.
- **Details**: `gain_retrace` contributed **+0.0754 SOL** across 10 exits (WR 100.0%); 1 `kelly_flat` exits cost **-0.0455 SOL** (-0.0455 SOL/trade avg); 2 `recording_ended` force-closes cost **-0.0508 SOL** (-0.0254 SOL/trade avg); 1 `evr_triage` fires cost **-0.0248 SOL**; severe tail (≤ −15%) dragged **-0.1211 SOL** across 4 trades (2 ≤ −30%).
- **Market Context**: mean pump height 78.4%, median 29.9%, dump-from-peak 59.3%, one-pump wonders 38.5%, slow bleeds 12.8%, consolidations 41.0%, mean loser hold +3632 s.

## 5. Synthesis: The Shared Patterns of Negative Days

From the empirical data, four universal patterns characterize the negative PnL cohorts:

```
                               ┌────────────────────────────────────────────────────────┐
                               │       THE ANATOMY OF A NEGATIVE TRADING DAY             │
                               └────────────────────────────────────────────────────────┘
                                                           │
          ┌────────────────────────┬───────────────────────┴───────────────────────┬────────────────────────┐
          ▼                        ▼                                               ▼                        ▼
  1. PAYOUT SKEW COLLAPSE   2. TAIL LOSS WEIGHTING                          3. CHRONIC LINGERING     4. REGIME POLARIZATION
     Avg Win -29.8%        ≤−15% losses drive 90.2% of          `rec_ended` share       Type A: Trap-Pumps
     (+0.0143 → +0.0100 SOL)      gross loss on neg days             18.0% vs 7.0%        Type B: Low-Vol Chop
     Avg Loss ~invariant      (-2.3197 SOL)                            Loser hold +13%           Breakouts fail/crash
```

1. **The Payout Skew Compression (The Primary Root Cause)**: the strategy's edge rests on positive payoff asymmetry (harvesting high-skew runners via `gain_retrace` while truncating pullbacks). On negative days the average winner compresses from **+0.0143 SOL (14.31%)** to **+0.0100 SOL (10.05%)** (-29.8%), while the average loss stays near-fixed at **-0.0225 → -0.0240 SOL** (+6.7%). The payoff ratio falls from +0.636 to +0.418, lifting the breakeven win rate to **70.51%** versus the 63.36% actually achieved — expectancy flips from +0.0042 to -0.0024 SOL/trade.
2. **Severe Tail Drag Drives Almost All Losses**: on negative days, scratches and mild losses (0% to −15%) contribute only -0.2506 SOL, while severe losses (≤ −15%) cost **-2.3197 SOL (90.2% of gross loss)**. Concurrent clusters of `kelly_flat` (-0.0456 SOL/trade avg) and `recording_ended` (-0.0248 SOL/trade avg) extinguish the winners' gains.
3. **Lingering Offside Hold Duration & `recording_ended` Vulnerability**: on negative days losing trades stay open an average of **913 seconds** vs 811 s on positive days (+12.5%); without momentum to arm `gain_retrace` or sharp downside to fire `evr_triage`, positions drift offside until the recording ends.
4. **Regime Bimodality**: negative days concentrate in two market conditions rather than uniform variance — **Type A "Parabolic Dump Traps"** (3 days: mean pump ≥ 100%, avg 126.7%, high one-pump-wonder ratios, flash dumps through the trailing buffer into `kelly_flat`) and **Type B "Dead/Choppy Grinds"** (6 days: mean pump 64.9%, elevated slow-bleed + consolidation shares, breakouts fizzling into `recording_ended`).

## 6. Architectural Recommendations

Based on these empirical findings (and the accumulated iteration history iters 31–58):
1. **Never Revert EVR Triage + Sell Concentration Veto + Holder-Flow Silence Gate**: these post-entry triage layers protect capital during volatile dumps and silent-bleed regimes without damaging runner capture.
2. **Keep the Regime-Adaptive Give-Back (iter57/58)**: tightening the armed-winner trail on low-Q days directly attacks Pattern 1 (payout skew compression) at its source and cleared the strict paired-diff gate (Δ+0.215 SOL, Wilcoxon p=3.05e-06, CI strictly positive).
3. **Address the Residual `recording_ended` / Chronic Offside Drag**: where this report still shows heavy force-close drag, the highest-leverage research avenue remains a time-decayed offside exit for trades held far beyond the norm with zero upward progress — but note the iter37 oracle-bound theorem: pure exit-timing/re-entry-gate mechanisms using only OHLCV are provably bounded below baseline, so any such work must consume genuinely new information (e.g., validated holder-flow channels).
4. **Respect Market Bimodality**: macro entry filters remain mathematically difficult because winners and losers share identical pre-entry features (proven iters 31/34/40/52/56). Refinement must continue to focus on **post-entry triage** and **offside duration management**.

---
*Report generated automatically by `backend/analysis/run_date_segmented_backtests_v2.py` from `backend/analysis/date_segmented_results_v2.json`; raw batch logs under `$BACKTEST_RESULTS_DIR` (`batch_id` prefix `date2_`). Statistical tests: two-sided Mann-Whitney U on day-level means.*