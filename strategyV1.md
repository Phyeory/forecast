# Physics-Based Regime Detection Strategy: Math & Logic

This document breaks down the mathematical foundations and logical architecture of the `StrategyEngine` algorithm.

## 1. Core Physics Analogy & Observables
The algorithm models price action as a physical system undergoing Langevin dynamics (a particle moving through a fluid with friction and random forces).

* **Position & Momentum ($p, m$)** $\rightarrow$ Estimated using a 2-State Kalman Filter ($\hat{p}, \hat{m}$)
* **Trend Direction** $\rightarrow$ Fast/Slow Moving Averages (EMA 3 vs EMA 7)
* **Damping ($\gamma$)** $\rightarrow$ The contraction rate of the EMA spread
* **Noise/Friction ($\sigma$)** $\rightarrow$ Average True Range (ATR)
* **Potential Energy Landscape ($U(p)$)** $\rightarrow$ Fixed-range Volume Profile (identifying High/Low Volume Nodes as resistance/support)
* **External Force** $\rightarrow$ Cumulative Delta Volume (Buy vs Sell pressure)

### Core Signal Formulation
The fundamental signal strength ($S$) is defined as a signal-to-noise ratio:
$$ S = \frac{|\hat{m}|}{ATR_{floor}} $$
where $ATR_{floor}$ is dynamically bounded by a rolling median to avoid zero-division and to accurately represent baseline market noise.

When the algorithm considers the potential landscape (Volume Profile), it computes a barrier-adjusted signal:
$$ S_{effective} = \frac{S}{\Delta U} $$
where $\Delta U$ is the relative distance to the nearest High Volume Node (HVN). Entering *into* heavy resistance (a potential barrier) severely dampens the effective signal.

## 2. Kalman Filter Momentum Estimation
A 2-state Kalman filter continuously estimates the true price ($p$) and momentum ($m$) while filtering out market noise.
* **State Vector**: $x = [p, m]^T$
* **Prediction Phase**: 
  - $p_{pred} = p_{prev} + m_{prev}$
  - $m_{pred} = (1 - \gamma) \cdot m_{prev}$ (Momentum naturally decays/damps by $\gamma$)
* **Measurement Update**: The filter dynamically adjusts its measurement variance based on recent price variance. This yields robust estimates $\hat{p}$ and $\hat{m}$ that lag significantly less than traditional moving averages.

## 3. The State Machine (Regime Detection)
The engine operates as a continuous state machine:
`IDLE` $\rightarrow$ `TREND` $\rightarrow$ `EXHAUSTION` $\rightarrow$ `REVERSAL` or `CONTINUATION` $\rightarrow$ `TREND`

* **TREND**: Confirmed when momentum persistence, volatility expansion, and EMA separation all pass their respective thresholds.
* **EXHAUSTION**: Triggered when the trend loses energy. Conditions include momentum decay ($|\hat{m}| < |\hat{m}_{prev}|$), EMA spread shrinking, and localized price stalling relative to ATR.
* **CONTINUATION / REVERSAL**: Upon leaving the exhaustion phase, the system evaluates if the new incoming force (Delta Volume, structural breakout) aligns with the prior trend or signals a definitive flip.

## 4. Signal Confidence Scoring
The global trend confidence $C \in [0, 1]$ is a weighted sum of four components:
1. **Persistence ($w_1 = 0.30$)**: Fraction of consecutive bars exhibiting same-sign momentum.
2. **Normalized Momentum ($w_2 = 0.25$)**: Current $|\hat{m}|$ relative to its long-term moving average.
3. **Volatility Expansion ($w_3 = 0.25$)**: Current ATR relative to the rolling median ATR.
4. **EMA Separation ($w_4 = 0.20$)**: Magnitude of the EMA fast/slow spread normalized against a minimum required threshold.

$C$ must exceed strict thresholds (e.g., `confidence_high` = 0.62) to permit a trade entry. If $C$ falls into the "ambiguous zone," the logic explicitly blocks new signals.

## 5. Advanced Entry Gates & Risk Safeguards

### A. Top-Blast Prevention (Blow-off Top Guard)
To prevent buying the absolute peak of a parabolic, overextended move:
1. **Price Overextension Check**: If the actual price vastly exceeds the Kalman estimate ($Price > \hat{p} \times (1 + k)$) AND the signal is extremely strong ($S > S_{strong}$), it detects a blow-off peak and blocks the entry.
2. **Momentum Peak Decline**: If $|\hat{m}|$ declines for several consecutive bars, momentum has verifiably peaked. Subsequent signals are blocked.
3. **Long-Baseline Spike Filter**: Instead of comparing the latest candle's body to the recent pump bars (which are all abnormally huge), the algorithm anchors the comparison to a much older "calm" window. If the current candle is a statistical anomaly compared to the calm baseline, it's flagged as a blow-off top.

### B. Anti-Chop & Consolidation Box Filter
1. **Local Chop Detector**: Blocks entries if the recent N-bar price range is extremely small (e.g., `< 2%` of price) AND the price is floating in the dead middle (35%–65%) of that range.
2. **Sign Flips**: High frequency of momentum sign flips ($m_{i} \times m_{i-1} < 0$) inside a tight range completely invalidates trend entries.

### C. First-Leg Breakout Optimization (Cold Starts)
1. **Dynamic Pre-Entry Stability**: Normally, entries require $N$ consecutive bars of strictly increasing momentum. However, if Confidence is very high ($> 0.67$), this is dynamically relaxed to 1 bar. This prevents Kalman lag from blocking massive, high-conviction early breakouts.
2. **Cold Start Acceleration**: On the very first breakout from a long `IDLE` or `EXHAUSTION` state, the required signal threshold ($S$) is deliberately lowered. This ensures the algorithm aggressively catches fresh, highly profitable first legs before the momentum fully saturates.

---
**Summary**: By combining rigorous digital signal processing (Kalman filters), physics analogies (Langevin dynamics, potential barriers), and robust risk-gating state machines, the strategy ensures high-precision entries while systematically preventing "FOMO" into overextended tops or choppy consolidation zones.