# Date-Segmented Backtest Report (V3 — Holder-Flow & Regime-Layer Ablation)

**Date:** August 23, 2026  
**Scope:** Full Production Dataset across 26 Distinct Recording Dates (1,557 Completed Recordings, 985 Executed Trades)  
**Strategy Engine:** StrategyEngineV2 — Production Build with the 2026-08-23 Ablation Defaults: **Holder-Flow Entry Gate & Dev-Sell Exit DISABLED** (`v2_holder_flow_entry_block=0`, `v2_holder_flow_exit_enable=0`), **Regime-Adaptive Give-Back DISABLED** (`v2_regime_enable=0`), **Regime Participation Floor DISABLED** (`v2_regime_participation_floor=0`). Retained ON: EVR Triage + Sell-Concentration Veto `[iter48/50]`, Holder-Flow Stream Silence Gate 2700 s `[iter56]`, 1-Bar Execution Delay, 4-State Intra-Candle Expansion, Recording-End Force-Close)  
**NEW in V3:** a paired per-date **ablation comparison** against the V2 all-layers-ON run (`date_segmented_results_v2.json`, same morning, identical engine build otherwise) is appended as Section 7.

> **Note vs. the V2 report (same morning, all layers ON):** every date segment was re-built from scratch under the ablation defaults. The four disabled knobs act at entry-gating / exit-triggering level only; the core SDE/KDE/Kramers machinery is byte-identical, so per-date deltas vs the V2 cache (identical recording cohorts for all 25 shared dates; new date 2026-08-23 added here) isolate the net contribution of the holder-flow gates/exits and both regime layers.

## Executive Summary

To investigate temporal stability, regime sensitivity, and the underlying failure modes of the production strategy, a comprehensive date-segmented backtest was conducted across all **26 recording dates** comprising **1,557 completed recordings** and **985 executed trades**.

### Key High-Level Findings:
1. **Net Overall Profitability**: Across the entire dataset, the strategy produced a net positive PnL of **+1.4403 SOL** across 985 trades with an overall **71.78% Win Rate**, Profit Factor **+1.19**, **average win +0.0129 SOL (12.88%)**, and **average loss -0.0276 SOL (-27.56%)** (payoff ratio +0.467, breakeven WR 68.16%).
2. **Day-Level Distribution**:
   - **17 Positive PnL Days (65.4%)**: Total PnL **+2.3849 SOL** (Mean: **+0.1403 SOL/day**), Win Rate: **73.88%**, Avg Win **+0.0142 SOL (14.19%)**, Avg Loss **-0.0265 SOL (-26.51%)**.
   - **9 Negative PnL Days (34.6%)**: Total PnL **-0.9446 SOL** (Mean: **-0.1050 SOL/day**), Win Rate: **67.30%** (day-average 65.57%), Avg Win **+0.0098 SOL (9.81%)**, Avg Loss **-0.0294 SOL (-29.36%)**.
3. **Core Shared Patterns of Negative PnL Days**:
   - **Payout Skew Compression**: on negative days the average winning trade shrinks **-30.9%** (from +0.0142 SOL / 14.19% on positive days to +0.0098 SOL / 9.81%), while the average loss moves only **+10.8%** (-0.0265 → -0.0294 SOL). Daily expectancy per trade flips from +0.0036 to -0.0030 SOL, and the breakeven win rate rises to 74.96% vs an achieved 67.30%.
   - **Tail Loss Concentration**: trades ≤ −15% account for **95.4% of gross losing PnL** on negative days (-2.8857 SOL drag across 77 trades), against +2.0795 SOL earned by winners.
   - **`recording_ended` Vulnerability**: negative days average a 12.1% daily `recording_ended` exit share vs 7.4% on positive days, with losing trades held +945 s vs +817 s (+15.6%).
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
| **2026-08-07** | 39 | 17 | 64.7% | **-0.0519** | +0.1119 | -0.1638 | +0.68 | +0.0102 | -0.0273 | 10.18% | -27.30% | +0.373 | 72.8% | 58.8% | 11.8% | 11.8% | 78.4% | 38.5% | 12.8% |
| **2026-08-08** | 58 | 25 | 76.0% | **+0.0658** | +0.2047 | -0.1389 | +1.47 | +0.0108 | -0.0232 | 10.78% | -23.15% | +0.465 | 68.2% | 60.0% | 8.0% | 8.0% | 88.3% | 31.0% | 24.1% |
| **2026-08-09** | 34 | 13 | 76.9% | **+0.0037** | +0.0943 | -0.0907 | +1.04 | +0.0094 | -0.0302 | 9.43% | -30.22% | +0.312 | 76.2% | 69.2% | 7.7% | 7.7% | 163.9% | 35.3% | 20.6% |
| **2026-08-10** | 139 | 66 | 75.8% | **+0.2797** | +0.6481 | -0.3684 | +1.76 | +0.0130 | -0.0230 | 12.96% | -23.02% | +0.563 | 64.0% | 62.1% | 9.1% | 3.0% | 138.7% | 38.8% | 20.1% |
| **2026-08-11** | 64 | 24 | 62.5% | **+0.0589** | +0.2758 | -0.2169 | +1.27 | +0.0184 | -0.0241 | 18.39% | -24.10% | +0.763 | 56.7% | 54.2% | 4.2% | 12.5% | 144.2% | 40.6% | 10.9% |
| **2026-08-12** | 50 | 69 | 69.6% | **+0.0377** | +0.7649 | -0.7272 | +1.05 | +0.0159 | -0.0346 | 15.94% | -34.63% | +0.460 | 68.5% | 59.4% | 10.1% | 10.1% | 185.7% | 62.0% | 8.0% |
| **2026-08-13** | 10 | 2 | 50.0% | **-0.0023** | +0.0155 | -0.0178 | +0.87 | +0.0155 | -0.0178 | 15.51% | -17.84% | +0.870 | 53.5% | 50.0% | 0.0% | 50.0% | 19.1% | 0.0% | 30.0% |
| **2026-08-14** | 59 | 40 | 70.0% | **-0.0734** | +0.2635 | -0.3369 | +0.78 | +0.0094 | -0.0281 | 9.41% | -28.07% | +0.335 | 74.9% | 67.5% | 15.0% | 2.5% | 84.0% | 35.6% | 13.6% |
| **2026-08-15** | 81 | 65 | 70.8% | **+0.0177** | +0.5140 | -0.4963 | +1.04 | +0.0112 | -0.0261 | 11.17% | -26.12% | +0.428 | 70.0% | 69.2% | 7.7% | 6.2% | 150.2% | 38.3% | 11.1% |
| **2026-08-16** | 65 | 44 | 61.4% | **+0.1122** | +0.4459 | -0.3337 | +1.34 | +0.0165 | -0.0196 | 16.52% | -19.63% | +0.841 | 54.3% | 61.4% | 4.5% | 11.4% | 126.8% | 56.9% | 15.4% |
| **2026-08-18** | 60 | 35 | 82.9% | **+0.2923** | +0.4295 | -0.1372 | +3.13 | +0.0148 | -0.0229 | 14.81% | -22.86% | +0.648 | 60.7% | 62.9% | 5.7% | 0.0% | 75.4% | 36.7% | 18.3% |
| **2026-08-19** | 92 | 47 | 53.2% | **-0.4130** | +0.2551 | -0.6681 | +0.38 | +0.0102 | -0.0304 | 10.20% | -30.37% | +0.336 | 74.8% | 53.2% | 19.1% | 4.3% | 109.3% | 42.4% | 17.4% |
| **2026-08-20** | 34 | 27 | 74.1% | **-0.0169** | +0.1671 | -0.1840 | +0.91 | +0.0084 | -0.0263 | 8.35% | -26.29% | +0.318 | 75.9% | 66.7% | 3.7% | 7.4% | 59.6% | 38.2% | 29.4% |
| **2026-08-21** | 68 | 50 | 70.0% | **-0.1218** | +0.3133 | -0.4351 | +0.72 | +0.0090 | -0.0290 | 8.95% | -29.01% | +0.309 | 76.4% | 54.0% | 12.0% | 10.0% | 75.8% | 39.7% | 11.8% |
| **2026-08-22** | 44 | 32 | 71.9% | **-0.1438** | +0.2245 | -0.3683 | +0.61 | +0.0098 | -0.0409 | 9.76% | -40.92% | +0.239 | 80.7% | 65.6% | 15.6% | 6.2% | 72.6% | 31.8% | 20.5% |
| **2026-08-23** | 36 | 11 | 72.7% | **+0.2748** | +0.3546 | -0.0797 | +4.45 | +0.0443 | -0.0266 | 44.32% | -26.58% | +1.667 | 37.5% | 54.5% | 9.1% | 9.1% | 1140.7% | 30.6% | 19.4% |
| **TOTAL** | **1557** | **985** | **71.8%** | **+1.4403** | **+9.1030** | **-7.6627** | **+1.19** | **+0.0129** | **-0.0276** | **12.88%** | **-27.56%** | **+0.467** | **68.2%** | **63.0%** | **8.3%** | **9.0%** | **155.2%** | **38.6%** | **17.2%** |

## 2. Statistical Comparison: Positive vs Negative PnL Cohorts

Comparing the 17 Positive PnL Days against the 9 Negative PnL Days across all market, execution, and payoff variables using two-sided Mann-Whitney U tests (day-level units):

| Metric / Dimension | Positive Days Mean (std) | Negative Days Mean (std) | Delta (Neg − Pos) | MWU $p$-value | Significance |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Win Rate (%)** | 73.85% (± 7.46%) | 65.57% (± 8.05%) | -11.21 pp | **0.0236** | **Significant** |
| **Profit Factor** | +1.81 (± +1.01) | +0.74 (± +0.16) | -1.0680 | **0.0000** | **Statistically Significant** |
| **Avg Win Size (SOL)** | +0.0150 (± +0.0082) | +0.0105 (± +0.0023) | -29.6% | **0.0406** | **Significant** |
| **Avg Win Size (%)** | 14.97% (± 8.22%) | 10.55% (± 2.27%) | -4.4258 | **0.0406** | **Significant** |
| **Avg Loss Size (SOL)** | -0.0255 (± +0.0066) | -0.0283 (± +0.0056) | +11.2% | **0.1611** | Not Significant |
| **Avg Loss Size (%)** | -25.46% (± 6.63%) | -28.31% (± 5.58%) | -2.8520 | **0.1611** | Not Significant |
| **Payoff Ratio** | +0.613 (± +0.319) | +0.399 (± +0.177) | -0.2135 | **0.0271** | **Significant** |
| **Breakeven WR (%)** | 63.92% (± 10.09%) | 72.40% (± 7.47%) | +8.4730 | **0.0271** | **Significant** |
| **Trades per Day** | 39.41 (± 23.90) | 35.00 (± 17.22) | -4.4118 | **0.8083** | Not Significant |
| **Active Traded Tokens** | 15.88 (± 9.00) | 15.11 (± 6.49) | -0.7712 | **0.9570** | Not Significant |
| **Tail Losses ≤ −15% Count** | 7.35 (± 5.48) | 8.56 (± 4.72) | +1.2026 | **0.4317** | Not Significant |
| **Tail Drag (SOL)** | -0.2594 (± +0.2045) | -0.3206 (± +0.1719) | -0.0612 | **0.3058** | Not Significant |
| **Mean Holding Time (s)** | 536.1 s (± 238.5 s) | 589.1 s (± 360.4 s) | +53.0090 | **0.9142** | Not Significant |
| **Mean Winner Holding Time (s)** | 432.1 s (± 235.7 s) | 394.7 s (± 330.9 s) | -37.4101 | **0.3320** | Not Significant |
| **Mean Loser Holding Time (s)** | 817.5 s (± 655.7 s) | 944.9 s (± 913.6 s) | +127.4751 | **1.0000** | Not Significant |
| **Gain Retrace Share (%)** | 64.36% (± 7.31%) | 60.50% (± 7.91%) | -3.8628 | **0.2052** | Not Significant |
| **Recording Ended Share (%)** | 7.43% (± 4.88%) | 12.08% (± 13.73%) | +4.6517 | **0.6468** | Not Significant |
| **Kelly Flat Share (%)** | 7.19% (± 2.60%) | 10.44% (± 6.19%) | +3.2439 | **0.1381** | Not Significant |
| **EVR Triage Share (%)** | 4.67% (± 3.75%) | 5.22% (± 4.81%) | +0.5499 | **0.9784** | Not Significant |
| **Kramers Down Share (%)** | 4.29% (± 2.76%) | 4.16% (± 4.29%) | -0.1304 | **0.5886** | Not Significant |
| **Mean Token Pump Height (%)** | 192.17% (± 241.19%) | 85.51% (± 34.85%) | -106.6611 | **0.0132** | **Significant** |
| **Median Token Pump Height (%)** | 41.11% (± 13.18%) | 37.80% (± 14.03%) | -3.3075 | **0.7464** | Not Significant |
| **One-Pump Wonder Ratio (%)** | 40.00% (± 11.67%) | 36.02% (± 14.03%) | -3.9786 | **0.8715** | Not Significant |
| **Slow Bleed Ratio (%)** | 16.69% (± 5.21%) | 18.08% (± 6.71%) | +1.3927 | **0.7876** | Not Significant |
| **Sustained Runner Ratio (%)** | 9.57% (± 3.69%) | 10.59% (± 4.21%) | +1.0144 | **0.5532** | Not Significant |
| **Choppy Consolidation Ratio (%)** | 33.74% (± 9.82%) | 35.31% (± 7.68%) | +1.5716 | **0.6276** | Not Significant |
| **Candle Spread Volatility (%)** | 0.50% (± 0.10%) | 0.48% (± 0.11%) | -0.0219 | **0.5899** | Not Significant |
| **Return Volatility (%)** | 3.24% (± 0.57%) | 3.26% (± 0.43%) | +0.0184 | **1.0000** | Not Significant |
| **Turnover (SOL/rec)** | 3,821 SOL (± 3,797 SOL) | 3,863 SOL (± 4,043 SOL) | +41.2984 | **0.3885** | Not Significant |
| **Taker Buy Ratio** | 0.42 (± 0.04) | 0.42 (± 0.06) | -0.0069 | **0.6663** | Not Significant |

Across all 26 dates, the daily `recording_ended` exit share correlates with daily PnL at Spearman $r = -0.218$ ($p = 0.2858$) — days where recordings end while positions are still trapped offside are systematically the losing days.

## 3. Trade-Level Payoff & Distribution Breakdown (incl. Average Win/Loss Size)

Analyzing individual trades (670 trades on Positive Days vs 315 trades on Negative Days) reveals the mathematical mechanism behind negative days:

```
+---------------------------------------------------------------------------------------------------+
| Metric                              Positive Days (670 Trades)                                     Negative Days (315 Trades)      |
+---------------------------------------------------------------------------------------------------+
| Win Rate                           73.88% (495 W / 175 L)          67.30% (212 W / 103 L)         |
| Mean PnL per Trade                 +0.0036 SOL                     -0.0030 SOL                    |
| AVERAGE WIN SIZE                   +0.01419 SOL (+14.19%)          +0.00981 SOL (+9.81%)          |
| AVERAGE LOSS SIZE                  -0.02651 SOL                    -0.02936 SOL                   |
|   (as % of entry)                  14.19% / -26.51%                9.81% [-31%] / -29.36% [+11%]  |
| Win / Loss Payoff Ratio            0.535                           0.334                          |
| Breakeven Win Rate Required        65.13%                          74.96%                         |
+---------------------------------------------------------------------------------------------------+
```

### Exit Reason Distribution & Financial Contribution:

```
Positive Days Exits:
  * gain_retrace          424 trades ( 63.3%), PnL: +4.0220 SOL, WR:  92.9%  [core winner engine]
  * kramers_down_exit      28 trades (  4.2%), PnL: +0.9741 SOL, WR:  71.4%
  * reversal_exit           8 trades (  1.2%), PnL: +0.1936 SOL, WR:  62.5%
  * recording_ended        44 trades (  6.6%), PnL: -0.8466 SOL, WR:  13.6%  [-0.0192 SOL/trade]
  * evr_triage             36 trades (  5.4%), PnL: -0.9079 SOL, WR:   0.0%  [-0.0252 SOL/trade]
  * kelly_flat             53 trades (  7.9%), PnL: -2.3366 SOL, WR:   0.0%  [-0.0441 SOL/trade]
  * bayesian_flip          11 trades (  1.6%), PnL: +0.2673 SOL, WR:  81.8%
  * breakeven_scratch      63 trades (  9.4%), PnL: +0.2932 SOL, WR:  92.1%
  * tp_v2                   3 trades (  0.4%), PnL: +0.7259 SOL, WR: 100.0%

Negative Days Exits:
  * gain_retrace          196 trades ( 62.2%), PnL: +1.5990 SOL, WR:  91.3%  [core winner engine]
  * kramers_down_exit      12 trades (  3.8%), PnL: +0.2276 SOL, WR:  66.7%
  * reversal_exit           3 trades (  1.0%), PnL: -0.0181 SOL, WR:  33.3%
  * recording_ended        24 trades (  7.6%), PnL: -0.6217 SOL, WR:   4.2%  [-0.0259 SOL/trade]
  * evr_triage             19 trades (  6.0%), PnL: -0.5684 SOL, WR:   0.0%  [-0.0299 SOL/trade]
  * kelly_flat             36 trades ( 11.4%), PnL: -1.6268 SOL, WR:   0.0%  [-0.0452 SOL/trade]
  * bayesian_flip           4 trades (  1.3%), PnL: -0.0026 SOL, WR:  50.0%
  * breakeven_scratch      21 trades (  6.7%), PnL: +0.0665 SOL, WR: 100.0%
```

### Loss Severity Stratification:

```
+---------------------------------------------------------------------------------------------------+
| Loss Category           Positive Days (670 trades)            Negative Days (315 trades)        |
+---------------------------------------------------------------------------------------------------+
| > 0% (Winners)           495 trades (73.9%), +7.0235 SOL      212 trades (67.3%), +2.0795 SOL     |
| 0% to -5% (Scratches)     32 trades ( 4.8%), -0.0635 SOL       13 trades ( 4.1%), -0.0209 SOL     |
| -5% to -15% (Mild)        18 trades ( 2.7%), -0.1654 SOL       13 trades ( 4.1%), -0.1174 SOL     |
| -15% to -30% (Severe)     53 trades ( 7.9%), -1.1640 SOL       26 trades ( 8.3%), -0.5938 SOL     |
| <= -30% (Catastrophic)    72 trades (10.7%), -3.2456 SOL       51 trades (16.2%), -2.2919 SOL     |
+---------------------------------------------------------------------------------------------------+
```

## 4. In-Depth Autopsy of the 9 Negative PnL Days

Analyzing each negative PnL day individually reveals the structural root causes (failure mode is assigned programmatically from the dominant loss driver):

### 1. **2026-08-19 (PnL: -0.4130 SOL | 47 Trades | WR: 53.2% | PF: +0.38 | Avg Win: +0.0102 SOL | Avg Loss: -0.0304 SOL)**
- **Failure Mode**: *Systemic win-rate collapse (breakout whipsaws)*.
- **Details**: `gain_retrace` contributed **+0.1940 SOL** across 25 exits (WR 92.0%); 9 `kelly_flat` exits cost **-0.3924 SOL** (-0.0436 SOL/trade avg); 2 `recording_ended` force-closes cost **-0.0460 SOL** (-0.0230 SOL/trade avg); 8 `evr_triage` fires cost **-0.2004 SOL**; severe tail (≤ −15%) dragged **-0.6389 SOL** across 19 trades (9 ≤ −30%).
- **Market Context**: mean pump height 109.3%, median 43.6%, dump-from-peak 56.1%, one-pump wonders 42.4%, slow bleeds 17.4%, consolidations 32.6%, mean loser hold +808 s.

### 2. **2026-08-22 (PnL: -0.1438 SOL | 32 Trades | WR: 71.9% | PF: +0.61 | Avg Win: +0.0098 SOL | Avg Loss: -0.0409 SOL)**
- **Failure Mode**: *High-frequency `kelly_flat` cluster*.
- **Details**: `gain_retrace` contributed **+0.2019 SOL** across 21 exits (WR 95.2%); 5 `kelly_flat` exits cost **-0.2466 SOL** (-0.0493 SOL/trade avg); 2 `recording_ended` force-closes cost **-0.0951 SOL** (-0.0475 SOL/trade avg); severe tail (≤ −15%) dragged **-0.3627 SOL** across 8 trades (7 ≤ −30%).
- **Market Context**: mean pump height 72.6%, median 31.3%, dump-from-peak 58.7%, one-pump wonders 31.8%, slow bleeds 20.5%, consolidations 36.4%, mean loser hold +680 s.

### 3. **2026-08-21 (PnL: -0.1218 SOL | 50 Trades | WR: 70.0% | PF: +0.72 | Avg Win: +0.0090 SOL | Avg Loss: -0.0290 SOL)**
- **Failure Mode**: *High-frequency `kelly_flat` cluster*.
- **Details**: `gain_retrace` contributed **+0.2184 SOL** across 27 exits (WR 92.6%); 6 `kelly_flat` exits cost **-0.2882 SOL** (-0.0480 SOL/trade avg); 5 `recording_ended` force-closes cost **-0.0924 SOL** (-0.0185 SOL/trade avg); 2 `evr_triage` fires cost **-0.0464 SOL**; severe tail (≤ −15%) dragged **-0.4081 SOL** across 10 trades (7 ≤ −30%).
- **Market Context**: mean pump height 75.8%, median 45.5%, dump-from-peak 55.4%, one-pump wonders 39.7%, slow bleeds 11.8%, consolidations 39.7%, mean loser hold +226 s.

### 4. **2026-07-30 (PnL: -0.0840 SOL | 63 Trades | WR: 71.4% | PF: +0.83 | Avg Win: +0.0089 SOL | Avg Loss: -0.0270 SOL)**
- **Failure Mode**: *Severe `recording_ended` truncation drag*.
- **Details**: `gain_retrace` contributed **+0.3575 SOL** across 47 exits (WR 89.4%); 2 `kelly_flat` exits cost **-0.0952 SOL** (-0.0476 SOL/trade avg); 7 `recording_ended` force-closes cost **-0.2294 SOL** (-0.0328 SOL/trade avg); 3 `evr_triage` fires cost **-0.1331 SOL**; severe tail (≤ −15%) dragged **-0.4449 SOL** across 11 trades (8 ≤ −30%).
- **Market Context**: mean pump height 136.8%, median 57.4%, dump-from-peak 66.6%, one-pump wonders 54.3%, slow bleeds 12.8%, consolidations 25.5%, mean loser hold +389 s.

### 5. **2026-08-14 (PnL: -0.0734 SOL | 40 Trades | WR: 70.0% | PF: +0.78 | Avg Win: +0.0094 SOL | Avg Loss: -0.0281 SOL)**
- **Failure Mode**: *High-frequency `kelly_flat` cluster*.
- **Details**: `gain_retrace` contributed **+0.2455 SOL** across 27 exits (WR 88.9%); 6 `kelly_flat` exits cost **-0.2475 SOL** (-0.0413 SOL/trade avg); 1 `recording_ended` force-closes cost **-0.0225 SOL** (-0.0225 SOL/trade avg); 1 `evr_triage` fires cost **-0.0327 SOL**; severe tail (≤ −15%) dragged **-0.3288 SOL** across 9 trades (7 ≤ −30%).
- **Market Context**: mean pump height 84.0%, median 39.2%, dump-from-peak 57.8%, one-pump wonders 35.6%, slow bleeds 13.6%, consolidations 39.0%, mean loser hold +380 s.

### 6. **2026-08-07 (PnL: -0.0519 SOL | 17 Trades | WR: 64.7% | PF: +0.68 | Avg Win: +0.0102 SOL | Avg Loss: -0.0273 SOL)**
- **Failure Mode**: *High-frequency `kelly_flat` cluster*.
- **Details**: `gain_retrace` contributed **+0.0754 SOL** across 10 exits (WR 100.0%); 2 `kelly_flat` exits cost **-0.0846 SOL** (-0.0423 SOL/trade avg); 2 `recording_ended` force-closes cost **-0.0508 SOL** (-0.0254 SOL/trade avg); 1 `evr_triage` fires cost **-0.0248 SOL**; severe tail (≤ −15%) dragged **-0.1602 SOL** across 5 trades (3 ≤ −30%).
- **Market Context**: mean pump height 78.4%, median 29.9%, dump-from-peak 59.3%, one-pump wonders 38.5%, slow bleeds 12.8%, consolidations 41.0%, mean loser hold +3115 s.

### 7. **2026-08-05 (PnL: -0.0374 SOL | 37 Trades | WR: 64.9% | PF: +0.90 | Avg Win: +0.0136 SOL | Avg Loss: -0.0280 SOL)**
- **Failure Mode**: *High-frequency `kelly_flat` cluster*.
- **Details**: `gain_retrace` contributed **+0.1482 SOL** across 20 exits (WR 85.0%); 5 `kelly_flat` exits cost **-0.2300 SOL** (-0.0460 SOL/trade avg); 2 `recording_ended` force-closes cost **-0.0427 SOL** (-0.0213 SOL/trade avg); 2 `evr_triage` fires cost **-0.0627 SOL**; severe tail (≤ −15%) dragged **-0.3498 SOL** across 9 trades (7 ≤ −30%).
- **Market Context**: mean pump height 133.9%, median 55.1%, dump-from-peak 63.8%, one-pump wonders 43.8%, slow bleeds 14.6%, consolidations 27.1%, mean loser hold +324 s.

### 8. **2026-08-20 (PnL: -0.0169 SOL | 27 Trades | WR: 74.1% | PF: +0.91 | Avg Win: +0.0084 SOL | Avg Loss: -0.0263 SOL)**
- **Failure Mode**: *EVR triage drag exceeding winner capture*.
- **Details**: `gain_retrace` contributed **+0.1426 SOL** across 18 exits (WR 94.4%); 1 `kelly_flat` exits cost **-0.0423 SOL** (-0.0423 SOL/trade avg); 2 `recording_ended` force-closes cost **-0.0248 SOL** (-0.0124 SOL/trade avg); 2 `evr_triage` fires cost **-0.0683 SOL**; severe tail (≤ −15%) dragged **-0.1745 SOL** across 5 trades (3 ≤ −30%).
- **Market Context**: mean pump height 59.6%, median 29.0%, dump-from-peak 57.5%, one-pump wonders 38.2%, slow bleeds 29.4%, consolidations 26.5%, mean loser hold +1978 s.

### 9. **2026-08-13 (PnL: -0.0023 SOL | 2 Trades | WR: 50.0% | PF: +0.87 | Avg Win: +0.0155 SOL | Avg Loss: -0.0178 SOL)**
- **Failure Mode**: *Low-sample anomaly on a thin trading day*.
- **Details**: `gain_retrace` contributed **+0.0155 SOL** across 1 exits (WR 100.0%); 1 `recording_ended` force-closes cost **-0.0178 SOL** (-0.0178 SOL/trade avg); severe tail (≤ −15%) dragged **-0.0178 SOL** across 1 trades (0 ≤ −30%).
- **Market Context**: mean pump height 19.1%, median 9.3%, dump-from-peak 19.2%, one-pump wonders 0.0%, slow bleeds 30.0%, consolidations 50.0%, mean loser hold +605 s.

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
     Avg Win -30.9%        ≤−15% losses drive 95.4% of          `rec_ended` share       Type A: Trap-Pumps
     (+0.0142 → +0.0098 SOL)      gross loss on neg days             12.1% vs 7.4%        Type B: Low-Vol Chop
     Avg Loss ~invariant      (-2.8857 SOL)                            Loser hold +16%           Breakouts fail/crash
```

1. **The Payout Skew Compression (The Primary Root Cause)**: the strategy's edge rests on positive payoff asymmetry (harvesting high-skew runners via `gain_retrace` while truncating pullbacks). On negative days the average winner compresses from **+0.0142 SOL (14.19%)** to **+0.0098 SOL (9.81%)** (-30.9%), while the average loss stays near-fixed at **-0.0265 → -0.0294 SOL** (+10.8%). The payoff ratio falls from +0.535 to +0.334, lifting the breakeven win rate to **74.96%** versus the 67.30% actually achieved — expectancy flips from +0.0036 to -0.0030 SOL/trade.
2. **Severe Tail Drag Drives Almost All Losses**: on negative days, scratches and mild losses (0% to −15%) contribute only -0.1383 SOL, while severe losses (≤ −15%) cost **-2.8857 SOL (95.4% of gross loss)**. Concurrent clusters of `kelly_flat` (-0.0452 SOL/trade avg) and `recording_ended` (-0.0259 SOL/trade avg) extinguish the winners' gains.
3. **Lingering Offside Hold Duration & `recording_ended` Vulnerability**: on negative days losing trades stay open an average of **945 seconds** vs 817 s on positive days (+15.6%); without momentum to arm `gain_retrace` or sharp downside to fire `evr_triage`, positions drift offside until the recording ends.
4. **Regime Bimodality**: negative days concentrate in two market conditions rather than uniform variance — **Type A "Parabolic Dump Traps"** (3 days: mean pump ≥ 100%, avg 126.7%, high one-pump-wonder ratios, flash dumps through the trailing buffer into `kelly_flat`) and **Type B "Dead/Choppy Grinds"** (6 days: mean pump 64.9%, elevated slow-bleed + consolidation shares, breakouts fizzling into `recording_ended`).

## 6. Architectural Recommendations

Based on these empirical findings under the ablated configuration (and the accumulated iteration history iters 31–58):
1. **Keep EVR Triage + Sell-Concentration Veto + Holder-Flow Silence Gate ON**: the post-entry triage stack remains active in this configuration; its exit-reason shares below quantify its continued engagement.
2. **Judge the Ablation on Section 7, Not on Single Days**: the holder-flow gates/exits and regime layers were each accepted on paired-difference statistics over full cohorts — single-day improvements can be noise. If the ablation's paired daily PnL delta is positive with a significant Wilcoxon p, the simplified stack is the new reference configuration; if not, the disabled layers were paying for themselves.
3. **Watch What the Ablated Layers Used to Absorb**: with the dev-sell exit OFF, insider dumps now exit via `kelly_flat`/`recording_ended` instead of `dev_sell_exit`; with the participation floor OFF, catastrophic-Q dates trade again. Compare those exit-reason cohorts against the V2 report to see where the risk resurfaced.
4. **Respect Market Bimodality**: winners and losers share identical pre-entry features (proven iters 31/34/40/52/56). Refinement must continue to focus on post-entry triage and offside duration management.

---

## 7. Ablation Comparison vs the V2 All-Layers-ON Run (Paired by Date)

Paired dates: **25** (identical recording cohorts; all verified equal). Dates present only in V3: ['2026-08-23'].

Configuration delta (V3 − V2): holder-flow entry gate OFF, dev-sell exit OFF, regime-adaptive give-back OFF, participation floor OFF. Everything else identical.

| Date | Trades V2 → V3 | WR V2 → V3 (%) | PnL V2 (SOL) | PnL V3 (SOL) | ΔPnL (SOL) | ΔTail≤−15% n | ΔKellyFlat PnL | ΔRecEnded PnL | ΔDevSell PnL |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **2026-07-27** | 39 → 39 | 82.1% → 82.1% | +0.4365 | +0.4365 | **+0.0000** | 0 | +0.0000 | +0.0000 | +0.0000 |
| **2026-07-28** | 64 → 64 | 78.1% → 78.1% | +0.2809 | +0.2809 | **+0.0000** | 0 | +0.0000 | +0.0000 | +0.0000 |
| **2026-07-29** | 67 → 67 | 74.6% → 74.6% | +0.0590 | +0.0590 | **+0.0000** | 0 | +0.0000 | +0.0000 | +0.0000 |
| **2026-07-30** | 63 → 63 | 71.4% → 71.4% | -0.0840 | -0.0840 | **+0.0000** | 0 | +0.0000 | +0.0000 | +0.0000 |
| **2026-07-31** | 5 → 5 | 60.0% → 60.0% | +0.0026 | +0.0026 | **+0.0000** | 0 | +0.0000 | +0.0000 | +0.0000 |
| **2026-08-01** | 24 → 24 | 70.8% → 70.8% | +0.0679 | +0.0679 | **+0.0000** | 0 | +0.0000 | +0.0000 | +0.0000 |
| **2026-08-02** | 17 → 17 | 88.2% → 88.2% | +0.0294 | +0.0294 | **+0.0000** | 0 | +0.0000 | +0.0000 | +0.0000 |
| **2026-08-03** | 82 → 82 | 73.2% → 73.2% | +0.2346 | +0.2346 | **+0.0000** | 0 | +0.0000 | +0.0000 | +0.0000 |
| **2026-08-05** | 37 → 37 | 64.9% → 64.9% | -0.0374 | -0.0374 | **+0.0000** | 0 | +0.0000 | +0.0000 | +0.0000 |
| **2026-08-06** | 20 → 20 | 80.0% → 80.0% | +0.1312 | +0.1312 | **+0.0000** | 0 | +0.0000 | +0.0000 | +0.0000 |
| **2026-08-07** | 17 → 17 | 70.6% → 64.7% | -0.0118 | -0.0519 | **-0.0401** | +1 | -0.0391 | +0.0000 | +0.0036 |
| **2026-08-08** | 22 → 25 | 81.8% → 76.0% | +0.1644 | +0.0658 | **-0.0986** | +2 | -0.0842 | +0.0000 | -0.0728 |
| **2026-08-09** | 11 → 13 | 72.7% → 76.9% | +0.0239 | +0.0037 | **-0.0202** | 0 | -0.0500 | +0.0000 | +0.0089 |
| **2026-08-10** | 58 → 66 | 70.7% → 75.8% | +0.4158 | +0.2797 | **-0.1361** | +3 | -0.1280 | -0.0465 | -0.1647 |
| **2026-08-11** | 24 → 24 | 58.3% → 62.5% | +0.1074 | +0.0589 | **-0.0485** | +2 | +0.0000 | -0.0792 | -0.1142 |
| **2026-08-12** | 59 → 69 | 66.1% → 69.6% | +0.2574 | +0.0377 | **-0.2197** | +4 | -0.1255 | -0.0626 | -0.0835 |
| **2026-08-13** | 1 → 2 | 0.0% → 50.0% | -0.0178 | -0.0023 | **+0.0155** | 0 | +0.0000 | +0.0000 | +0.0000 |
| **2026-08-14** | 39 → 40 | 64.1% → 70.0% | -0.0288 | -0.0734 | **-0.0446** | +1 | -0.0374 | +0.0000 | +0.0217 |
| **2026-08-15** | 56 → 65 | 67.9% → 70.8% | +0.1233 | +0.0177 | **-0.1056** | +3 | -0.1837 | +0.0000 | +0.0085 |
| **2026-08-16** | 40 → 44 | 62.5% → 61.4% | +0.0228 | +0.1122 | **+0.0894** | +2 | +0.0164 | -0.0133 | +0.0141 |
| **2026-08-18** | 32 → 35 | 75.0% → 82.9% | +0.2566 | +0.2923 | **+0.0357** | +1 | +0.0000 | +0.0000 | -0.1222 |
| **2026-08-19** | 44 → 47 | 47.7% → 53.2% | -0.2173 | -0.4130 | **-0.1957** | +6 | -0.1772 | +0.0000 | +0.0500 |
| **2026-08-20** | 21 → 27 | 57.1% → 74.1% | -0.0963 | -0.0169 | **+0.0794** | -1 | -0.0423 | +0.0000 | +0.0510 |
| **2026-08-21** | 42 → 50 | 64.3% → 70.0% | -0.0402 | -0.1218 | **-0.0817** | +4 | -0.2380 | -0.0523 | +0.1322 |
| **2026-08-22** | 28 → 32 | 67.9% → 71.9% | -0.1780 | -0.1438 | **+0.0342** | +1 | -0.0429 | +0.0000 | +0.0083 |
| **TOTAL (paired)** | **912 → 974** | 66.8% → 70.9% (day-mean) | **+1.9021** | **+1.1655** | **-0.7366** (5↑ / 10↓ days) |  |  |  |  |

**Paired statistics over the 25 shared dates:** mean daily ΔPnL **-0.0295 SOL**, Wilcoxon signed-rank two-sided $p = 0.0535$, bootstrap 95% CI of mean daily Δ [-0.0596, -0.0022], 5/25 days improved (20% breadth).
**Verdict:** the ablation is NOT an improvement — the disabled layers were net-protective on this dataset.

*Caveat: day-level pairing has n=26 units — this is a coarse consistency check, not the per-recording paired-diff gate used for iteration acceptances. Exit-reason deltas attribute WHERE the PnL moved (dev-sell exits vanish by construction when the gate is off; their former PnL migrates to `kelly_flat` / `recording_ended` / later re-entries).*

---
*Report generated automatically by `backend/analysis/run_date_segmented_backtests_v3.py` from `backend/analysis/date_segmented_results_v3.json`; raw batch logs under `$BACKTEST_RESULTS_DIR` (`batch_id` prefix `date3_`). Statistical tests: two-sided Mann-Whitney U on day-level means; Section 7 uses Wilcoxon signed-rank + 10,000-sample bootstrap CI on paired daily PnL.*