# Cascade V2: A Rao-Blackwellised Particle Filter with Modified Kramers Escape Rates for Short-Horizon Solana Memecoin Trading

**Complete Mathematical Specification and As-Built Implementation**

Jaime Mok
Independent Quantitative Research
August 2026

## Abstract

We document the complete mathematical specification and as-built implementation of Cascade V2, a fully autonomous short-horizon ($\tau \in [5, 30]$ s) trading engine for Solana memecoins. The engine models the market as a five-dimensional continuous-time jump-diffusion latent state $\Psi_t = (x_t, \mu_t, h_t, \phi_t, \ell_t)$ — log-price, instantaneous drift, log-variance, signed order-flow pressure, and log-Kyle-depth — estimated online by a Rao-Blackwellised Particle Filter (RBPF) whose continuous layer is a per-particle Unscented Kalman Filter (UKF) under the scaled unscented transform ($\alpha = 0.3$, $\beta = 2$, $\kappa = 0$, $N_p = 200$ particles). A non-equilibrium market potential $U(x, t) = -T_t \log \rho(x, t)$ is constructed from a volume- and recency-weighted kernel density estimate of the trade tape evaluated on a 200-point log-price grid with Silverman bandwidth and $\pm 4.3\sigma$ spatial pruning, and transition probabilities to adjacent local minima are derived via a modified Kramers escape-rate formalism. A Kelly-type sizing rule converts posterior escape probabilities and barrier geometry into a bounded position size gated by a positive expected log-wealth increment.

Unlike a standard strategy description, this paper treats the gap between original theoretical specification and final production implementation as a primary research object: of the principal structural terms in the specification, four were empirically removed or replaced after producing reproducible, quantified pathologies on live token data. We present the complete 22-iteration falsification program — governed by a five-gate paired-difference statistical acceptance protocol — that carried the engine from a degenerate, never-trading filter (iteration 1) to a canonical baseline of 366 trades, 76.5% win rate, +1.12 SOL aggregate PnL, and profit factor 1.48 on 558 recorded tokens (iteration 22). An exhaustive 87-rule counterfactual stop-loss and entry-gate sweep establishes that, on the current feature set, this configuration is a measurable Pareto frontier: no price-only or posterior-only decision rule improves aggregate PnL without a net-negative trade-off against winning trades.

## Contents

1. Introduction
   1.1 Motivation
   1.2 What This Paper Does and Does Not Do
   1.3 Contribution Summary
2. Theoretical Specification
   2.1 Latent State Space
   2.2 Stochastic Differential Equations
   2.3 Observation Model
   2.4 Non-Equilibrium Market Potential
   2.5 Modified Kramers Escape Rates
   2.6 Decision Theory and Sizing
   2.7 Estimation
3. Free Parameters
4. The RBPF/UKF Estimator
   4.1 Euler–Maruyama Discretisation
   4.2 Scaled Unscented Transform
   4.3 UKF Predict Step
   4.4 UKF Update Step
   4.5 Observable-Anchored OU Targets
   4.6 Adaptive Measurement Noise
   4.7 RBPF Outer Loop
   4.8 Topological Regime Derivation
5. Market Potential, Barriers, and Kramers Rates
   5.1 Volume-Weighted KDE
   5.2 Liquidity Cost Field: Computed but Excluded
   5.3 Barrier Search
   5.4 Production Escape Rates
   5.5 Transition Probabilities and Direction Decision
6. Kelly Sizing: As Implemented
   6.1 Barrier-Distance-Weighted Expected Return
   6.2 Horizon Variance
   6.3 Position Sizing and Trade Gate
7. Entry Gate and Exit Waterfall
   7.1 Entry Gate
   7.2 Exit Waterfall
8. Structural Finding: Down-Barrier Asymmetry
9. Acceptance Protocol and Iteration History
   9.1 Five-Gate Acceptance Protocol
   9.2 Complete Iteration Ledger
   9.3 The Order-Flow Data Integrity Bug (Iteration 15)
10. Iteration 22: Loss Anatomy and the Pareto-Frontier Result
    10.1 Loss Attribution
    10.2 Big-Loser Trajectory Analysis
    10.3 Entry-Time Feature Indistinguishability
    10.4 Exhaustive Counterfactual Sweep — A Negative Result
11. Execution Infrastructure
    11.1 Lookahead-Free Intra-Candle Backtest Replay
    11.2 Forward-Tester Fill Model
    11.3 Live On-Chain Execution
12. Discussion
    12.1 Specification as Falsifiable Hypothesis
    12.2 The Limits of a Price/Volume/Order-Flow-Only State
    12.3 Threats to Validity
13. Conclusion
    A. Complete Configuration Reference
    B. Notation

---

## 1. Introduction

### 1.1 Motivation

Solana memecoins launched on bonding-curve platforms (pump.fun and successors) exhibit price dynamics that violate most assumptions of classical market microstructure. Liquidity is endogenous to the bonding curve; order flow is retail and reflexive; price histories are seconds to minutes long; a substantial fraction of tokens terminate abruptly in a liquidity-withdrawal event rather than a stationary regime. Yet the local dynamics over a 5–30 second horizon — the object of this paper — can be usefully modelled as an overdamped particle diffusing in a potential landscape defined by the accumulated volume distribution. This physical analogy, developed formally in §2, motivates a market-potential and escape-rate formalism directly analogous to Kramers' theory of thermally activated barrier crossing [1, 2].

### 1.2 What This Paper Does and Does Not Do

The paper has two objects treated with equal rigour.

- **The theoretical specification (§2):** the original mathematical derivation from `strategyV2.md`, reproduced in full. This constitutes the null hypothesis we test against trading data.
- **The as-built implementation (§4–§7):** the production estimator (`backend/strategy_engineV2.py`, 3,375 lines), which departs from the specification in four material respects, each the outcome of a falsifiable, logged experiment. Implementation fidelity to the specification is not assumed; it is measured.

The paper does not document the V1 deterministic regime engine or the sniper subsystem, which share none of the V2 mathematical core.

### 1.3 Contribution Summary

(i) A complete re-derivation of the RBPF/UKF estimator as coded — including the $\alpha_{ukf} = 0.3$ tuning (not the textbook $10^{-3}$), the numba-JIT batched kernel architecture, and the Mehra adaptive-$R$ estimator — and why each departs from a naive reading of the specification.

(ii) Full accounting of four specification terms empirically removed from the potential/escape-rate/Kelly stack, each with its falsifying experiment, quantitative failure signature, and mathematical explanation for its degeneracy at 1-second memecoin scale.

(iii) The complete 22-iteration research program, condensed into a reproducible empirical narrative governed by a uniform five-gate acceptance protocol.

(iv) A negative result: an exhaustive 87-rule counterfactual sweep (§10) establishing the engine at a measurable Pareto frontier on the current feature set.

---

## 2. Theoretical Specification

### 2.1 Latent State Space

Let $x_t = \log S_t$ be the log-price of a Solana memecoin. The engine targets prediction horizons $\tau \in [5, 30]$ s. The latent state is

$$\Psi_t := (x_t, \mu_t, h_t, \phi_t, \ell_t, r_t) \in \mathbb{R}^5 \times \mathbb{R}, \tag{1}$$

where $x_t$ is log-price, $\mu_t$ is instantaneous drift, $h_t = \log \sigma_t^2$ is log-variance, $\phi_t$ is signed order-flow pressure, $\ell_t$ is log-effective-liquidity ($L_t = e^{\ell_t}$ is the Kyle depth [8]), and $r_t \in \mathbb{R}$ is a discrete regime label drawn from a 7-element alphabet (defined analytically in §4.8).

### 2.2 Stochastic Differential Equations

**Log-price.**

$$dx_t = \mu_t\, dt + \frac{\phi_t}{L_t}\, dt + \sigma_t\, dW_t^{(x)} + J_t\, dN_t, \tag{2}$$

where $W_t^{(x)}$ is a standard Wiener process, $N_t$ is a Cox process with volume-modulated intensity $\lambda_J(t) = \lambda_0 + \lambda_1 v_t$, and $J_t$ are i.i.d. double-exponential jump sizes.

**Drift.** Mean-reverting with order-flow coupling:

$$d\mu_t = -\lambda_\mu \mu_t\, dt + \kappa_\mu(\phi_t - \bar\phi_t)\, dt + \sigma_\mu\, dW_t^{(\mu)}. \tag{3}$$

**Log-variance.** Heston-type OU in log-space [9]:

$$dh_t = -\eta(h_t - \bar h_t)\, dt + \sigma_h\, dW_t^{(h)}. \tag{4}$$

**Order-flow pressure.** OU process driven by observed normalised signed volume:

$$d\phi_t = -\alpha \phi_t\, dt + \beta \frac{\delta_t}{v_t + \varepsilon}\, dt + \sigma_\phi\, dW_t^{(\phi)}. \tag{5}$$

**Log-liquidity.** OU with downward jumps:

$$d\ell_t = -\theta(\ell_t - \bar\ell)\, dt + \sigma_\ell\, dW_t^{(\ell)} - \zeta\, dN_t^{(\ell)}. \tag{6}$$

### 2.3 Observation Model

Observations arrive at discrete 1-second intervals $t_k = k\Delta t$, $\Delta t = 1$ s. The observation vector is $y_k = (\Delta x_k, v_k, \delta_k, q_k, d_k^{(b)}, d_k^{(a)})$ — bucket log-return, volume, signed volume, spread, bid and ask depth. The measurement equation for the log-return is

$$\Delta x_k = \int_{t_{k-1}}^{t_k} \left(\mu_s + \frac{\phi_s}{L_s}\right) ds + \sigma_k^{(\text{eff})} \varepsilon_k^{(x)} + \sum_{j:\, N_j \in (t_{k-1}, t_k]} J_j. \tag{7}$$

### 2.4 Non-Equilibrium Market Potential

**Volume-weighted KDE.** For trade tape $\{(x_i, v_i, t_i)\}$ with memory window $T_w$:

$$\rho(x, t) = \frac{1}{Z(t)} \sum_{i:\, t_i \in [t-T_w,\, t]} v_i\, e^{-\lambda_d(t - t_i)}\, K_h(x - x_i), \qquad Z = \sum_i v_i\, e^{-\lambda_d(t - t_i)}, \tag{8}$$

with Gaussian kernel $K_h(u) = \exp(-u^2/2h^2)$ and Silverman bandwidth $h$.

**Liquidity cost field.**

$$V_{\text{liq}}(x, t) = \int_{x_0}^{x} \frac{|u - x_t|}{\sqrt{L_t \cdot D(u, t)}}\, du, \tag{9}$$

with $D(u, t)$ the local depth density.

**Composite potential.**

$$U(x, t) = -T_t \log \rho(x, t) + V_{\text{liq}}(x, t), \qquad T_t = \sigma_t^2 / 2. \tag{10}$$

**Barrier energy.** With $x_t^{(\pm)}$ the nearest local maxima of $U$:

$$\Delta U_t^{\pm} = U(x_t^{(\pm)}, t) - U(x_t, t) \pm \tfrac{1}{2}\mu_t (x_t^{(\pm)} - x_t). \tag{11}$$

The $\pm\tfrac{1}{2}\mu_t \Delta x$ term is the work done by the drift in climbing the barrier.

### 2.5 Modified Kramers Escape Rates

Classical Kramers theory [1] is modified for non-equilibrium driving, stochastic volatility, and Kyle-lambda friction:

$$k_t^{\pm} = \frac{\sqrt{U''(x_t)\, |U''(x_t^{(\pm)})|}}{2\pi \gamma_t} \cdot \frac{\rho(x_t^{(\pm)}, t)}{\rho(x_t, t)} \cdot \exp\!\left(-\frac{\Delta U_t^{\pm}}{T_t}\right) \cdot V_t^{\pm}, \tag{12}$$

where $\gamma_t = 1/L_t$ is the Kyle-lambda friction coefficient and

$$V_t^{\pm} = \exp\!\left[\tfrac{1}{2}\left(\frac{\Delta U_t^{\pm}}{T_t}\right)^2 \operatorname{Var}(h_t \mid \mathcal{F}_t)\right] \tag{13}$$

is the volatility-of-volatility correction (second-order Gaussian moment expansion of $\langle e^{-\Delta U/T}\rangle$ over the posterior log-variance distribution).

**Transition probabilities.** Under the slow-variation approximation over horizon $\tau$:

$$P_t^{+}(\tau) = \frac{k_t^{+}}{k_t^{+} + k_t^{-}}\left(1 - e^{-(k_t^{+}+k_t^{-})\tau}\right), \tag{14}$$

$$P_t^{-}(\tau) = \frac{k_t^{-}}{k_t^{+} + k_t^{-}}\left(1 - e^{-(k_t^{+}+k_t^{-})\tau}\right), \tag{15}$$

$$P_t^{0}(\tau) = e^{-(k_t^{+}+k_t^{-})\tau}. \tag{16}$$

### 2.6 Decision Theory and Sizing

**Direction.**

$$z^{*} = \begin{cases} +1 & P_t^{+} > P_t^{-} \text{ and } P_t^{+} > P_t^{0} \\ -1 & P_t^{-} > P_t^{+} \text{ and } P_t^{-} > P_t^{0} \\ 0 & \text{otherwise.} \end{cases} \tag{17}$$

**Expected log-return.** By Itô integration of (2) with the slow-variation approximation and OU solution $\phi_s = \phi_t e^{-\alpha(s-t)}$:

$$\mathbb{E}[X \mid \Psi_t] = \mu_t \tau + \frac{\phi_t}{L_t \alpha}\left(1 - e^{-\alpha\tau}\right) + \lambda_J \mathbb{E}[J]\, \tau, \qquad X := x_{t+\tau} - x_t. \tag{18}$$

For $\alpha\tau \ll 1$ this simplifies to $\hat\mu_\tau = (\mu_t + \phi_t/L_t)\tau + \lambda_J \mathbb{E}[J]\tau$.

**Kelly sizing.** Position size $n$ is determined by the positive-utility condition:

$$n^{*} = \frac{|z^{*}\hat\mu_\tau| - f - s_0 - |\mu_t \Delta_{\text{lat}}|}{\sigma_\tau^2 + s_1}, \qquad n^{*} \leftarrow \min(n^{*},\, 0.1 \cdot L_t), \tag{19}$$

and a trade fires iff the expected Kelly log-wealth increment $E^{*} > 0$:

$$E^{*} = z^{*} n^{*} \hat\mu_\tau - \tfrac{1}{2}(n^{*})^2 \sigma_\tau^2 - f n^{*} - s_0 n^{*} - \tfrac{1}{2} s_1 (n^{*})^2 - z^{*} n^{*} \mu_t \Delta_{\text{lat}}. \tag{20}$$

### 2.7 Estimation

Online estimation of $\Psi_t$ uses an RBPF: $N_p = 200$ particles each carrying a discrete regime label and a 5-D continuous state propagated by a per-particle UKF under the scaled unscented transform, with resampling when $\text{ESS} < N_p/2$. The posterior mean $\hat\Psi_t = \sum_i w_t^{(i)} \Psi_t^{(i)}$ feeds the Kramers and decision layers.

---

## 3. Free Parameters

Table 1 lists all 16 SDE free parameters and the principal structural meta-parameters at their production-default values (`DEFAULT_CONFIG` in `strategy_engineV2.py`, line 140). Every Greek symbol in §2 maps 1:1 to a named key.

| Symbol | Key | Default | Role |
|---|---|---|---|
| $\lambda_\mu$ | `lambda_mu` | 0.15 | drift OU mean-reversion rate |
| $\kappa_\mu$ | `kappa_mu` | 0.05 | order-flow → drift coupling |
| $\sigma_\mu$ | `sigma_mu` | 0.10 | drift diffusion coefficient |
| $\eta$ | `eta` | 0.10 | log-variance OU rate |
| $\sigma_h$ | `sigma_h` | 0.20 | log-variance diffusion coefficient |
| $\alpha$ | `alpha` | 0.20 | order-flow OU rate |
| $\beta$ | `beta` | 1.00 | $\delta_k/(v_k+\varepsilon)$ coupling |
| $\sigma_\phi$ | `sigma_phi` | 0.15 | order-flow diffusion coefficient |
| $\theta$ | `theta` | 0.10 | liquidity OU rate |
| $\sigma_\ell$ | `sigma_ell` | 0.10 | liquidity diffusion coefficient |
| $\zeta$ | `zeta` | 0.30 | liquidity-jump magnitude |
| $\lambda_0$ | `lambda_0` | 1/14400 | KDE exponential decay rate ($=1/T_w$) |
| $\lambda_1$ | `lambda_1` | 0.10 | secondary slow-decay component |
| $\kappa_J$ | `kappa_J` | 0.05 | jump-intensity Poisson rate |
| $s_0$ | `s_0` | 0.011 | base slippage fraction |
| $s_1$ | `s_1` | 0.0005 | marginal slippage per unit size |

**Structural meta-parameters**

| Symbol | Key | Default | Role |
|---|---|---|---|
| $N_p$ | `n_particles` | 200 | particle count |
| $G$ | `n_grid` | 200 | spatial grid resolution for $U(x,t)$ |
| — | `grid_sigma_extent` | 5.0 | grid half-width in units of $\sigma_t\sqrt{T_w}$ |
| $T_w$ | `tw_window_seconds` | 14,400 | KDE memory window (s); see Finding 5.1 |
| $\tau$ | `tau_min/max/step` | 5/30/5 | horizon sweep (s) |
| $\varepsilon$ | `eps_div` | 1.0 | $\varepsilon$ in $\delta_k/(v_k+\varepsilon)$ |
| $f$ | `fee_fraction` | 0.0011 | flat fee fraction |
| $\Delta_{\text{lat}}$ | `latency_seconds` | 0.5 | execution latency (s) |
| — | `liquidity_cap_frac` | 0.10 | Kelly cap as fraction of $L_t$ |
| — | `warmup_seconds` | 30 | bars before first decision |
| $\sigma_{fl}$ | `sigma_floor` | $10^{-6}$ | numerical volatility floor |
| — | `logprob_floor` | −50 | log-likelihood clamp |
| — | `v2_drift_work_fraction` | 0.0 | disabled drift-work knob (§5.5) |

*Table 1: All 16 SDE free parameters and principal structural meta-parameters at production defaults. The slippage base $s_0 = 0.011$ was recalibrated in iteration 16h to the empirically measured $\approx 1.1\%$ one-way execution cost; the pre-calibration value of $\approx 0.002$ made the positive-EV gate (20) near-vacuous.*

---

## 4. The RBPF/UKF Estimator

### 4.1 Euler–Maruyama Discretisation

The continuous SDEs (2)–(6) are discretised at $\Delta t = 1$ s:

$$x_k = x_{k-1} + \left(\mu_{k-1} + \frac{\phi_{k-1}}{L_{k-1}}\right)\Delta t + \sigma_{\text{eff},k-1}\, \varepsilon_x, \tag{21}$$

$$\mu_k = \mu_{k-1}(1 - \lambda_\mu \Delta t) + \kappa_\mu(\phi_{k-1} - \bar\phi)\Delta t + \sigma_\mu \sqrt{\Delta t}\, \varepsilon_\mu, \tag{22}$$

$$h_k = h_{k-1}(1 - \eta \Delta t) + \eta \bar h\, \Delta t + \sigma_h \sqrt{\Delta t}\, \varepsilon_h, \tag{23}$$

$$\phi_k = \phi_{k-1}(1 - \alpha \Delta t) + \beta \frac{\delta_k}{v_k + \varepsilon}\Delta t + \sigma_\phi \sqrt{\Delta t}\, \varepsilon_\phi, \tag{24}$$

$$\ell_k = \ell_{k-1}(1 - \theta \Delta t) + \theta \bar\ell\, \Delta t - \zeta \cdot \mathbb{1}_{\text{jump}} + \sigma_\ell \sqrt{\Delta t}\, \varepsilon_\ell, \tag{25}$$

with $\sigma_{\text{eff}} = e^{h/2}$. All states are hard-clamped after propagation: $x \in [-10^6, 10^6]$, $\mu, \phi \in [-50, 50]$, $h, \ell \in [-15, 15]$, and $L_t = e^{\ell} \geq 10^{-3}$, guarding against unphysical blow-ups driven by the $\phi/L_t$ nonlinearity under extreme memecoin volatility.

### 4.2 Scaled Unscented Transform

Conditional on its discrete regime label, each particle's continuous state $y = (x, \mu, h, \phi, \ell)^\top \in \mathbb{R}^5$ is propagated by a UKF under the van der Merwe scaled unscented transform [5] with $L = 5$. The implementation uses $\alpha_{ukf} = 0.3$, $\beta_{ukf} = 2.0$, $\kappa_{ukf} = 0.0$, giving

$$\lambda = \alpha_{ukf}^2 (L + \kappa_{ukf}) - L = -4.55, \qquad L + \lambda = 0.45 > 0, \tag{26}$$

and weights

$$W_0^{(m)} = \frac{\lambda}{L+\lambda} = -\frac{4.55}{0.45} \approx -10.1, \qquad W_0^{(c)} = W_0^{(m)} + (1 - \alpha_{ukf}^2 + \beta_{ukf}) \approx -6.0, \qquad W_i = \frac{1}{2(L+\lambda)} \approx 1.11\ (i>0). \tag{27}$$

The $2L+1 = 11$ sigma points are $\chi_0 = \hat y$, $\chi_i = \hat y + (\sqrt{(L+\lambda)P})_{:,i-1}$ for $i = 1,\dots,L$, and $\chi_i = \hat y - (\sqrt{(L+\lambda)P})_{:,i-L-1}$ for $i = L+1,\dots,2L$, with $\sqrt{P}$ computed by a hand-rolled $5\times5$ Cholesky routine (`chol_lower_5x5`) for numba compatibility.

**Remark 4.1 (Why $\alpha_{ukf} = 0.3$, not $10^{-3}$).** The textbook-standard $\alpha_{ukf} = 10^{-3}$ gives $L + \lambda \to -4.999\ldots < 0$, rendering the square root of the scaled covariance imaginary and the weight normalisation (27) a $0/0$ under floating-point arithmetic. The implementation uses the SciPy/filterpy convention $\alpha_{ukf} = 0.3$, which keeps $L + \lambda = 0.45$ a compile-time-provable strictly positive constant. This is a numerical-safety deviation from the naive textbook default, not a modelling choice.

Note that $W_0^{(m)} < 0$ under this parameterisation. This means the sigma-point weighted mean can exceed the range of any individual (already-clamped) sigma point. A second post-aggregation hard clamp is therefore applied to the predicted mean $\hat y_{\text{pred}} = \sum_i W_i^{(m)} \chi_i^{\text{prop}}$ after the sigma-point weighted sum, mirroring the per-state bounds of (21)–(25).

### 4.3 UKF Predict Step

Each sigma point $\chi_i$ is propagated deterministically through (21)–(25) (with $\varepsilon_\cdot \equiv 0$; process noise enters through $Q$). The predicted mean and covariance are

$$\hat y_{\text{pred}} = \sum_{i=0}^{2L} W_i^{(m)} \chi_i^{\text{prop}}, \qquad P_{\text{pred}} = \sum_{i=0}^{2L} W_i^{(c)} (\chi_i^{\text{prop}} - \hat y_{\text{pred}})(\chi_i^{\text{prop}} - \hat y_{\text{pred}})^\top + Q, \tag{28}$$

with additive diagonal process noise

$$Q = \operatorname{diag}\left(\sigma_{\text{eff}}^2 \Delta t,\ \sigma_\mu^2 \Delta t,\ \sigma_h^2 \Delta t,\ \sigma_\phi^2 \Delta t,\ \sigma_\ell^2 \Delta t\right). \tag{29}$$

After accumulation, $P_{\text{pred}}$ is symmetrised and its diagonal is clamped to $[10^{-12}, 10^3]$, guarding against ill-conditioning under long chains of near-degenerate updates.

### 4.4 UKF Update Step

The observation model is $y_k = \Delta x_k = x_k - x_{k-1}$ (a difference measurement, not a level). Each sigma point's predicted measurement is

$$Z_i = \chi_{i,0}^{\text{prop}} - x_{\text{prev}}, \tag{30}$$

where $x_{\text{prev}}$ is the previous posterior mean of $x$ (not the predicted mean), matching the discretisation (21). The innovation covariance, cross-covariance, Kalman gain, and posterior update follow standard UKF equations:

$$P_{zz} = \sum_i W_i^{(c)} (Z_i - \hat z)^2 + R_{\text{meas}}, \tag{31}$$

$$P_{xz} = \sum_i W_i^{(c)} (\chi_i^{\text{prop}} - \hat y_{\text{pred}})(Z_i - \hat z), \tag{32}$$

$$K = P_{xz}/P_{zz}, \qquad |K| \le 100 \text{ (clamped)}, \tag{33}$$

$$\hat y_{\text{post}} = \hat y_{\text{pred}} + K(y_k - \hat z), \qquad P_{\text{post}} = P_{\text{pred}} - K P_{xz}^\top. \tag{34}$$

The marginal measurement log-likelihood used for particle weighting is

$$\log \ell_i = -\frac{r_i^2}{2 P_{zz,i}} - \frac{1}{2}\log(2\pi P_{zz,i}), \qquad r_i = \Delta x_k - \hat z_i, \tag{35}$$

using the UKF's own innovation variance $P_{zz}$ — which already incorporates cross-covariance through the sigma-point structure — not the simpler prior-variance-plus-$R$ approximation.

### 4.5 Observable-Anchored OU Targets

The OU mean-reversion targets $\bar h$ and $\bar\phi$ for the predict step (21)–(25) are set from observable quantities, not from the posterior latent state:

$$\bar h_{\text{obs},k} = \log\left(\text{EWMA}_a(\Delta x_k^2)\right), \qquad a = 0.05, \tag{36}$$

$$\bar\phi_{\text{obs},k} = \text{EWMA}_a\left(\delta_k/(v_k + \varepsilon)\right). \tag{37}$$

This is the standard stochastic-volatility convention of Barndorff-Nielsen/Harvey/Heston [10, 11]: anchor the log-variance OU process to realised (observable) variance rather than its own latent posterior. The liquidity target $\bar\ell$ retains a latent EMA because no observable L2 channel is available.

**Empirical Finding 4.2 (Self-referential OU collapse).** The original implementation anchored $\bar h_t$ to an EWMA of the posterior $\hat h_t$. A self-referential OU recursion with no exogenous signal has only one stable fixed point: the per-state clamp. Every particle's $h$ collapsed to the floor $-15$ within a few dozen ticks on every recording, giving $\sigma_t \approx 5\times10^{-4}$, a near-delta KDE density, $U(x) \equiv 0$, and $P_t^+ \equiv 0$ on every bar — the engine never traded. Identical collapse affected $\bar\phi_t$. Fix: observable anchors (36)–(37), matching the standard SV-OU convention.

### 4.6 Adaptive Measurement Noise

$R_{\text{meas}}$ is estimated online by the Mehra [3] innovation-covariance method:

$$R_{\text{ema},k} = (1-a) R_{\text{ema},k-1} + a \cdot r_k^2, \qquad a = 0.05, \tag{38}$$

with $r_k^2$ the mean squared innovation residual across all particles at step $k$. No new free parameter is introduced: $a$ reuses the EMA smoothing constant already used by (36)–(37).

**Empirical Finding 4.3 (Adaptive-$R$ motivation).** Once Finding 4.2 starved the filter of observable variance, a fixed-$R$ heuristic ($R = \max(q_k^2, \sigma_{fl}^2)$) underweighted realistic bar-to-bar log-return magnitudes, causing the Kalman gain to saturate and teleport $\mu_t$ to its $\pm 50$ walls on single noisy bars. Mehra's estimator eliminates this by tracking actual innovation magnitudes recursively.

### 4.7 RBPF Outer Loop

**Algorithm 1 — RBPF single-step update (`RaoBlackwellisedParticleFilter.step`)**

*Require:* stacked arrays $\{\mu^{(i)}, P^{(i)}, \sqrt{P}^{(i)}, \log w^{(i)}\}_{i=1}^{N_p}$; observation $(\Delta x_k,\ \delta_k/v_k,\ \mathbb{1}_{\text{jump}})$

1. Update observable OU anchors: $\bar h \leftarrow \log(\text{EWMA}_a(\Delta x_k^2))$; $\bar\phi \leftarrow \text{EWMA}_a(\delta_k/(v_k+\varepsilon))$ — Eq. (36)–(37)
2. Compute $R_{\text{meas}} \leftarrow \max(R_{\text{ema}}, q_k^2, \sigma_{fl}^2)$ — Mehra adaptive noise; Eq. (38)
3. **for** $i = 1,\dots,N_p$ (batched numba kernel, single tight loop) **do**
4. $\quad \sigma_{\text{eff}}^{(i)} \leftarrow e^{\mu^{(i)}[h]/2}$; $x_{\text{prev}}^{(i)} \leftarrow \mu^{(i)}[x]$
5. $\quad (\mu_{\text{pred}}^{(i)}, P_{\text{pred}}^{(i)}, Y^{(i)}) \leftarrow \text{UkfPredict}(\mu^{(i)}, P^{(i)}, \sqrt{P}^{(i)}, \sigma_{\text{eff}}^{(i)}, \bar\phi, \bar h, \bar\ell)$
6. $\quad (\mu^{(i)}, P^{(i)}, P_{zz}^{(i)}, \hat z^{(i)}) \leftarrow \text{UkfUpdate}(\mu_{\text{pred}}^{(i)}, P_{\text{pred}}^{(i)}, Y^{(i)}, \Delta x_k, R_{\text{meas}}, x_{\text{prev}}^{(i)})$
7. $\quad$ Refresh Cholesky $\sqrt{P}^{(i)}$ in-place (inlined $5\times5$ loop; avoids numba allocation overhead)
8. $\quad \log w^{(i)} \leftarrow \log w^{(i)} + \log \ell^{(i)}$ — Eq. (35)
9. **end for**
10. Update Mehra: $R_{\text{ema}} \leftarrow (1-a) R_{\text{ema}} + a \cdot N_p^{-1}\sum_i r_i^2$ — $r_i = \Delta x_k - \hat z^{(i)}$
11. Normalise weights via log-sum-exp; reset to uniform on all-NaN
12. **if** $\text{ESS} = 1/\sum_i (w^{(i)})^2 < N_p/2$ **then**
13. $\quad$ Systematic resample (Algorithm 2); reset $w^{(i)} = 1/N_p$
14. **end if**
15. Posterior mean $\hat\Psi \leftarrow \sum_i w^{(i)}\mu^{(i)}$ (NaN-safe; Algorithm 3)
16. Posterior variance $\hat V \leftarrow \sum_i w^{(i)}(\mu^{(i)} - \hat\Psi)^2$ (NaN-safe; for Kelly flow-variance)
17. Update $\bar\ell \leftarrow (1-a)\bar\ell + a\hat\ell$; compute $\dot{\hat\mu} \leftarrow (\hat\mu - \hat\mu_{\text{prev}})/\Delta t$
18. Re-derive per-particle regime labels from each particle's own $(\mu, \dot\mu, \phi, h, \ell)$ (Algorithm 4)
19. Regime posterior $\leftarrow$ population-weighted histogram over labels; modal label = point estimate regime
20. **return** posterior state, variance, regime distribution

All numerically hot inner loops (UKF sigma-point propagation, batched predict/update, Cholesky refresh, KDE evaluation, barrier search, second-derivative grid) are `numba.njit`-compiled with `cache=True`, with a pure-NumPy fallback path. The batched kernel `rbpf_step_batched` iterates the population of $N_p$ particles in a single tight numba loop, eliminating $\approx 600$ Python-level dispatch operations per tick and achieving a sub-2 ms combined per-tick latency.

**Algorithm 2 — Systematic resampling (`systematic_resample_indices`)**

*Require:* normalised weights $w_{1:N}$; $u \sim \text{Unif}[0,1)$

1. $\text{pos}_i \leftarrow (u+i)/N$ for $i = 0,\dots,N-1$
2. $\text{cum} \leftarrow \text{cumsum}(w)$; $\text{cum}[N-1] \leftarrow 1$ (rounding guard)
3. $\text{idx}_i \leftarrow \min\{j : \text{cum}_j \ge \text{pos}_i\}$
4. Copy particle state at $\text{idx}_{0:N-1}$ into fresh arrays (no aliasing)

**Algorithm 3 — NaN-safe posterior mean (`posterior_mean_batched`)**

*Require:* stacked state $\mu_{\text{arr}} \in \mathbb{R}^{N\times5}$, weights $w \in \mathbb{R}^N$

1. Skip any particle $i$ for which $w^{(i)}$ or any component of $\mu^{(i)}$ is non-finite
2. $\hat y \leftarrow \sum_{\text{valid } i} w^{(i)}\mu^{(i)} / \sum_{\text{valid } i} w^{(i)}$
3. Replace any non-finite component of $\hat y$ with its respective prior (0 for $x,\mu,\phi,\ell$; $-4$ for $h$)
4. **return** $\hat y$

### 4.8 Topological Regime Derivation

The discrete regime label is a deterministic function of each particle's own continuous posterior state, re-evaluated every tick — not a separately estimated hidden Markov state:

**Algorithm 4 — Per-particle topological regime derivation (`derive_regimes_batched`)**

*Require:* per-particle $(\mu, \dot\mu, \phi)$; global posteriors $(\sigma_t, \sigma_\phi^{ou})$; horizon $\tau$

1. $\mu^{*} \leftarrow \sigma_t/\sqrt{\tau}$ — drift noise floor (derived, not fixed)
2. $\phi^{*} \leftarrow \sigma_\phi/\sqrt{\alpha}$ — order-flow noise floor (OU stationary std)
3. **for** each particle $i$ **do**
4. $\quad$ **if** $|\mu_i| \le \mu^{*}$ and $|\phi_i| \le \phi^{*}$ **then return** `Idle`
5. $\quad$ **if** $|\mu_i| \le \mu^{*}$ and $|\phi_i| > \phi^{*}$ **then return** `Consolidation`
6. $\quad$ **if** $|\mu_i| > \mu^{*}$ and $\operatorname{sgn}(\mu_i) = \operatorname{sgn}(\dot\mu)$ **then**
7. $\qquad$ **if** $\operatorname{sgn}(\phi_i) = \operatorname{sgn}(\mu_i)$ **then return** `Trend` **else return** `Transition`
8. $\quad$ **if** $|\mu_i| > \mu^{*}$ and $\operatorname{sgn}(\mu_i) \ne \operatorname{sgn}(\dot\mu)$ **then**
9. $\qquad$ **if** $|\phi_i| > \phi^{*}$ and $\operatorname{sgn}(\phi_i) \ne \operatorname{sgn}(\mu_i)$ **then return** `Reversal` **else return** `Exhaustion`
10. $\quad$ **return** `Continuation`
11. **end for**
12. Population-weighted histogram of labels $\to$ regime posterior; modal label $\to$ point estimate

The noise floors $\mu^{*} = \sigma_t/\sqrt\tau$ and $\phi^{*} = \sigma_\phi/\sqrt\alpha$ are the posterior-derived drift and order-flow significance thresholds. No fixed numerical thresholds are hard-coded. The regime posterior is therefore a genuine Bayesian posterior: a strong trend collapses the population to a near-delta mass on `Trend`; an ambiguous state spreads probability across two or three labels.

**Empirical Finding 4.4 (Per-particle regime re-derivation — iteration 2).** The original implementation froze each particle's regime label at initialisation, leaving the regime posterior permanently near-uniform and the entry-confidence gate permanently near-zero (entropy $\approx \log 7$ on all bars). The specification mandates that regime is a function of the continuous phase vector, sampled deterministically — re-deriving it per tick from each particle's own $(\mu, \dot\mu, \phi)$ is the correct Rao-Blackwellisation.

---

## 5. Market Potential, Barriers, and Kramers Rates

### 5.1 Volume-Weighted KDE

The KDE density (8) is evaluated on a $G = 200$-point grid spanning $x_t \pm 5\sigma_t\sqrt{T_w}$, using a Gaussian kernel with volume- and decay-weighted Silverman bandwidth:

$$h_{\text{Sil}} = 0.9\, \hat\sigma_w\, n^{-1/5}, \qquad \hat\sigma_w^2 = \sum_i w_i (x_i - \bar x_w)^2, \qquad w_i = v_i e^{-\lambda_d(t-t_i)} \Big/ \sum_j v_j e^{-\lambda_d(t-t_j)}. \tag{39}$$

The batched kernel `kde_eval_kernel` applies spatial pruning: each trade contributes non-negligibly only within $\pm 4.3\sigma$ of its own log-price (beyond which $\exp(-4.3^2/2) \approx 10^{-4}$), reducing the naive $O(T\cdot G)$ complexity to $O(T\cdot w)$ with $w \ll G$. The trade buffer is pruned at $3T_w$, where the decay weight has fallen to $e^{-3} \approx 0.05$.

**Empirical Finding 5.1 (KDE memory window — the single largest empirical lever).** The legacy window was $T_w = 75$ s (a "wiggle" window intended to capture local micro-structure). Iteration 16k replaced this with $T_w = 14{,}400$ s — the full recording lifetime for a typical token — transforming the density from a local smoother into a structural, full-lifetime volume profile. This single change raised win rate from 24% to 41.5% and profit factor from 0.26 to 0.84 on a fresh 235-recording dataset. A monotone-improvement sweep over $T_w \in \{300, \dots, 57{,}600\}$ s confirmed a genuine structural regime rather than a coincidental point optimum.

After KDE evaluation, $\rho$ is normalised to $[0,1]$ (dividing by $\rho_{\max}$) for numerical stability before entering the potential.

### 5.2 Liquidity Cost Field: Computed but Excluded

The liquidity cost field (9) is fully implemented (`liquidity_cost_kernel`) as a two-sided trapezoidal integral of $|u-x_t|/\sqrt{L_t D(u,t)}$ outward from the current price index, using an asymmetric exponential taper of bid/ask depth as a proxy for $D(u,t)$. Both $V_{\text{lo}}$ and $V_{\text{hi}}$ are computed and retained for diagnostic logging on every tick. However:

**Empirical Finding 5.2 (Temperature/impact-cost scale mismatch; iteration 16d).** On 1-second memecoin data, $T_t = \sigma_t^2/2 \approx 5\times10^{-5}$ while $V_{\text{liq}} \approx 0.05$–$0.7$ on the same grid — a $10^3$–$10^4$ ratio. The composite potential $U = -T\log\rho + V_{\text{liq}}$ was therefore entirely dominated by the synthetic depth taper. The barrier-search routine located barriers on $O(T_t)$-amplitude KDE noise riding the $V_{\text{liq}}$ ramp ("noise barriers"), the Boltzmann factor $\exp(-\Delta U/T)$ collapsed to numerical clamps $\{0, e^{50}\}$, and the entire escape-rate machinery degenerated to a binary drift-sign detector at 24% win rate under either sign convention (iteration 16b measured 24.2%). The production potential is therefore:

$$U(x,t) = -T_t \log \rho(x,t) \qquad \text{(production; no } V_{\text{liq}} \text{ term)}. \tag{40}$$

Liquidity's role in the non-equilibrium framework is retained through the friction coefficient $\gamma_t = 1/L_t$ in the escape-rate prefactor.

### 5.3 Barrier Search

Given the current log-price grid index $i_t$, `barrier_find_kernel` performs independent rightward and leftward sweeps:

1. Track running maximum of $U$ outward from $i_t$.
2. When $U$ begins to decrease from a running maximum, walk forward to the next local minimum.
3. If $U$ is monotone to a boundary (no local maximum found), the boundary index is returned as the barrier.

The second-derivative field for curvature computation uses the correctly normalised central difference:

$$U_i'' = \frac{U_{i+1} - 2U_i + U_{i-1}}{\Delta x^2}, \tag{41}$$

with $\Delta x$ the uniform grid spacing. An early implementation bug (iteration 16f) omitted $\Delta x^2$, understating $U''$ by a factor $\sim 160$ and silencing essentially all barrier escapes.

### 5.4 Production Escape Rates

The production Kramers escape rates are:

$$k_t^{\pm} = \frac{\sqrt{\omega_0^2\, \omega_{b,\pm}^2}}{2\pi\gamma_t} \cdot \exp_{\text{clamp}[-50,50]}\!\left(-\frac{\Delta U_t^{\pm}}{T_t}\right), \qquad \gamma_t = e^{-\ell_t}, \tag{42}$$

where $\omega_0^2 = U''(x_t)$, $\omega_{b,\pm}^2 = |U''(x_t^{(\pm)})|$, and $\Delta U_t^{\pm} = U(x_t^{(\pm)}) - U(x_t)$ with no drift-work term (by default). Two specification terms are absent from (42):

**Empirical Finding 5.3 (Density ratio is algebraically subsumed).** The specification (12) includes an explicit density-ratio factor $\rho(x_t^{(\pm)})/\rho(x_t)$. With the production potential $U = -T\log\rho$ (40), the barrier energy is $\Delta U^{\pm} = -T[\log\rho(x_t^{(\pm)}) - \log\rho(x_t)]$, so

$$\exp\!\left(-\frac{\Delta U^{\pm}}{T}\right) = \exp\!\left(\log\rho(x_t^{(\pm)}) - \log\rho(x_t)\right) = \frac{\rho(x_t^{(\pm)})}{\rho(x_t)}.$$

The Boltzmann factor **is** the density ratio. Multiplying it again as a separate factor — as a literal reading of (12) would require — squares the density ratio, which is mathematically incorrect given the specification's own choice of potential.

**Empirical Finding 5.4 (Volatility-of-volatility correction removed; iteration 16c).** The correction (13), $V_t^{\pm} = \exp[\tfrac{1}{2}(\Delta U^{\pm}/T)^2 \operatorname{Var}(h)]$, is valid only when $|\Delta U/T|\sqrt{\operatorname{Var}(h)} \ll 1$. On 1-second memecoin data $|\Delta U/T| \sim 5$–$30$; the correction anti-inverts the physics. Since $\exp[\tfrac{1}{2}(\Delta U/T)^2\operatorname{Var}(h)]$ grows super-exponentially while $\exp(-\Delta U/T)$ decays only exponentially, farther barriers receive *higher* escape rates. Traced on recording 205: $\Delta U_{\text{up}}/T \approx 31$, bare Boltzmann factor $\sim 10^{-14}$, correction $e^{+50}$ (clamp), giving $k_{\text{up}} = 8\times10^6 \gg k_{\text{down}}$ and $P_t^{+} \equiv 1.00$ on 690 of 1,352 bars — constant buy output regardless of barrier geometry. The correction is set to unity ($V_t^{\pm} \equiv 1$) in production.

**Empirical Finding 5.5 (Drift-work term removed by default; iteration 16d).** The specification's $\pm\tfrac{1}{2}\mu_t\Delta x$ contribution to $\Delta U^{\pm}$ (11) couples the instantaneous OU-process posterior $\mu_t$ directly to the escape exponent. At 1-second scale, $|\tfrac{1}{2}\mu_t\Delta x/T| \sim 10$–$100$ — large enough to swamp the KDE geometry with tick-level $\mu_t$ noise and reduce the decision layer to an unstable drift-sign detector (iteration 16d: 1,421 trades over 8 recordings at 15.7% win rate). The term is retained as a configurable knob (`v2_drift_work_fraction`, default 0.0); every non-zero setting tested in iteration 21 (fractions 0.1, 0.5) reproduced the same churn pathology and was rejected.

### 5.5 Transition Probabilities and Direction Decision

Equations (14)–(16) are implemented verbatim with $k_{\text{total}} = k_t^{+} + k_t^{-}$ floored at $10^{-9}$. The direction decision (17) uses $\ge$ (not $>$) so a degenerate tie $P_t^{+} = P_t^{0}$ resolves to "no trade."

**Empirical Finding 5.6 (Posterior escape majority vs. $\operatorname{sgn}(\hat\mu_\tau)$; iteration 3).** The first working decision rule used $z^{*} = \operatorname{sgn}(\hat\mu_\tau)$, the sign of the naive extrapolated expected return (18). Because $\hat\mu_\tau \approx \mu_t\tau + \phi_t\tau/L_t$ is dominated by the instantaneous OU posterior $\mu_t$ (expected bar-to-bar variation $\sim \sigma_\mu\sqrt{\Delta t}$), single bars routinely flipped $z^{*}$ while the underlying trend was intact, producing 79 trades at 7.6% win rate and profit factor 0.08. Replacing this with the specification's own decision rule (17) — already computed but previously unused for direction — reduced trade count by 57% and raised win rate to 35.3%: $P_t^{\pm}(\tau)$ integrate the entire posterior over $U(x,t)$, barrier heights, and curvatures, and are far smoother decision statistics than a single point estimate.

---

## 6. Kelly Sizing: As Implemented

### 6.1 Barrier-Distance-Weighted Expected Return

The production expected log-return used for sizing is not (18). It is:

$$\hat\mu_\tau^{(\text{prod})} = P_t^{+}(\tau)\, d^{+} - P_t^{-}(\tau)\, d^{-}, \qquad d^{\pm} = |x_t^{(\pm)} - x_t|, \tag{43}$$

the probability-weighted distance to the barriers.

**Empirical Finding 6.1 ($\tau$-linear extrapolation makes the Kelly gate vacuous).** At typical posterior magnitudes $|\mu_t| \sim 0.01$–$0.05$, the naive extrapolation (18) gives $\hat\mu_\tau \sim 1.5$–$150$ log-units at $\tau = 30$s — so $E^{*} \approx +12$ on essentially every candidate trade, making the positive-utility gate a non-discriminating pass-through. The bounded, barrier-geometry-weighted form (43) restores a meaningful scale: $\hat\mu_\tau^{(\text{prod})}$ is bounded by the actual distance to the nearest KDE high-volume node, not an unbounded linear-in-$\tau$ projection.

### 6.2 Horizon Variance

$$\sigma_\tau^2 = \sigma_t^2\, \tau + \operatorname{Var}_{\text{RBPF}}(\phi_t) \cdot \tau^2, \tag{44}$$

combining a diffusive term (variance $\propto \tau$) with a flow-uncertainty term for the accumulated order-flow $\int_0^\tau \phi_s\, ds$ (variance $\propto \tau^2$ for a not-yet-decorrelated process). The flow-uncertainty uses the RBPF particle population's own posterior variance $\operatorname{Var}(\phi_t) = \sum_i w^{(i)}(\phi^{(i)} - \hat\phi)^2$, computed batch-efficiently in `posterior_var_batched`.

**Empirical Finding 6.2 (Posterior variance, not squared mean; iteration 16j).** The initial plug-in used $\mathbb{E}[\phi_t]^2$ for the flow-uncertainty term. By the variance decomposition, $\mathbb{E}[\phi]^2 = \operatorname{Var}(\phi) + (\text{bias term})$, but the substitution of the squared mean for the second central moment is wrong in kind. With realistic $|\bar\phi| \sim 0.1$–$0.15$, the mean-squared term overstated $\sigma_\tau^2$ by $\sim 10^4\times$ relative to the diffusive term, collapsing $n^{*} \to 0$ and making $E^{*} > 0$ near-vacuous in the opposite direction. Using the actual posterior variance restores genuine risk discrimination across the $\tau$ sweep.

### 6.3 Position Sizing and Trade Gate

$$n^{*} = \frac{|\hat\mu_\tau^{(\text{prod})}| - f - s_0 - |\mu_t\Delta_{\text{lat}}|}{\sigma_\tau^2 + s_1}, \qquad n^{*} \leftarrow \min(n^{*},\, 0.1 L_t), \tag{45}$$

and the expected Kelly log-wealth increment:

$$E^{*} = z^{*} n^{*} \hat\mu_\tau^{(\text{prod})} - \tfrac{1}{2}(n^{*})^2 \sigma_\tau^2 - f n^{*} - s_0 n^{*} - \tfrac{1}{2} s_1(n^{*})^2 - z^{*} n^{*} \mu_t \Delta_{\text{lat}}. \tag{46}$$

A trade fires iff $z^{*} \ne 0$ and $E^{*} > 0$; otherwise the engine reports direction 0.

---

## 7. Entry Gate and Exit Waterfall

### 7.1 Entry Gate

A candidate entry ($z^{*} = +1$ and $E^{*} > 0$, long-only in production) additionally requires all of:

1. Regime posterior trend-confidence $\ge$ `entry_confidence_high` (high threshold).
2. $P_t^{+}(\tau) \ge 0.62$ (`v2_p_up_min`).
3. Posterior volatility $\sigma_t \ge 0.021$ (`v2_sigma_t_min`); counterfactual replay showed entries below this threshold carried essentially zero net PnL.
4. Price above the macro EMA trend filter.
5. No entry while the EMA-smoothed posterior drift derivative $\dot\mu_t$ has been negative for $\ge 80\%$ of a trailing 60-tick window (the "leading-decay entry block", iteration 5).

**Empirical Finding 7.1 (Replacement-entry dynamics — the most important methodological lesson).** Iteration 17b tested a gate requiring momentum to be past its local peak (a static counterfactual showed past-peak entries carried 96% of PnL on 34% of trades). Adding this gate as a live entry filter produced worse aggregate win rate and PnL than the ungated baseline. The mechanism: blocking one entry does not remove that trade from the record — it re-routes the engine to a different, subsequent entry on the same token, and the replacement trades were on average worse. This was re-confirmed in iterations 19 and 22. Static counterfactual masks computed against a fixed historical trade log are not reliable predictors of the dynamic, path-dependent outcome of re-running the same rule through the live decision loop. All acceptance decisions in this program are therefore based on full live-batch re-runs, never on static-mask projections alone.

### 7.2 Exit Waterfall

Seven exit conditions are evaluated in fixed priority order on every tick while a position is open (Table 2).

| Exit | Trigger | Iteration 22 status |
|---|---|---|
| `tp_v2_c` | $\ge$ entry $\cdot(1+\text{tp}/100)$ | Rare; high WR when fires |
| `gain_retrace` | Peak gain $\ge A=10\%$; exit when gain retraces to peak gain $\cdot(1-g)$, $g=0.4$ | Dominant win harvester: 162 trades, 91.4% WR, +1.50 SOL (iter 19) |
| `breakeven_scratch` | Drawdown $\ge 25\%$ from entry; exit at entry+2.5% buffer | 81.5% WR, +0.06 SOL |
| `reversal_exit` | Regime = REVERSAL for $\ge 2$ consecutive ticks | Persistence guard (iter 18b) reduces from 62 fires at 54% WR to 1 fire |
| `leading_decay_exit` | EMA-smoothed $\dot\mu_t < 0$ for $\ge 6$ bars AND $\ge 8\%$ offside | Disabled by default; entry-side block preferred |
| `kramers_down_exit` | $P_t^{-} > P_t^{+}$, $P_t^{-} > P_t^{0}$, $P_t^{-} \ge 0.5$ | "Crown logic" (iter 3): 60–100% WR when fires |
| `bayesian_flip` | $z^{*} \ne +1$ AND $E^{*} > 0$ (counter-direction utility) | Rare; small negative net PnL in most batches |
| `kelly_flat` | $z^{*} \le 0$ AND $E^{*} \le 0$ sustained $\ge 60$ ticks AND offside $\ge 40\%$ | Migrates $\sim 2/3$ of `recording_ended` bleed into smaller-median-loss exits |
| `hard_stop` | Fixed %-from-peak stop | Disabled (`stoploss_pct = 0`); see Finding 7.2 |
| `recording_ended` | Forced close at data-stream end | Residual; structurally hard to close |

*Table 2: The exit waterfall at iteration 22 canonical configuration, evaluated in this priority order.*

**Empirical Finding 7.2 (Removing the hard stop — iteration 18b).** A fixed $-25\%$-from-peak hard stop (iterations 16l–17a) produced a 0% win-rate loss bucket of 40 trades totalling $-1.10$ SOL in iteration 17a: the stop was cutting healthy pullbacks of eventual winners as often as genuine breakdowns, because at 1-second memecoin volatility a 25% retracement is well within the noise envelope of winning trades. Iteration 18b removed the hard stop entirely. Clearing all five statistical gates against the iteration 16 baseline (Wilcoxon $p=0.007$, paired $t$-test $p=0.038$, bootstrap 95% CI $[0.002, 0.033]$, 72.2% token breadth, McNemar $p=0.003$), win rate rose from 56.2% to 75.6%.

**Empirical Finding 7.3 (The reversal exit persistence guard — iteration 18b).** A single-tick REVERSAL label can be driven by a transient $\phi$ spike (OU noise std $\approx \sigma_\phi/\sqrt{2\alpha} \approx 0.24$) that reverts within 2–3s. Exiting immediately on a single-tick reversal exits on posterior noise rather than a sustained regime shift. Requiring $\ge 2$ consecutive REVERSAL bars — a temporal coherence condition equivalent to integrating the regime posterior over a 2-bar window — reduced reversal-exit fires from 62 to 1 between iteration 17a and iteration 18b.

**Empirical Finding 7.4 (The `kelly_flat` exit and its limits — iteration 21).** Exit 7 formalises a Bayesian consistency argument: the entry gate requires $z^{*}=+1$ and $E^{*}>0$; if both fail persistently while the position is deep offside, holding asserts a Kelly utility the engine's own posterior no longer supports. Full-trace analysis of every W→L regression the rule introduced found that a companion $\mu$-persistence guard (cut only when EMA-smoothed $\dot\mu_t$ negative for $\ge 75\%$ of the trailing window) does not discriminate recoverable pullbacks from genuine slides: the single worst W→L regression the guard would have prevented had $\mu_{\text{neg\_frac}} = 1.00$ at the cut, yet price recovered 84% over the subsequent 29 minutes. This is direct empirical evidence that price recovery after a deep-offside slide is, at the current feature set, an exogenous event not encoded in the RBPF posterior.

---

## 8. Structural Finding: Down-Barrier Asymmetry

**Empirical Finding 8.1 ($P_t^{-} \equiv 0$ saturation during active declines).** Iteration 20's discriminator analysis traced the KDE barrier geometry at every catastrophic loss and found $P_t^{-}(\tau) < 0.10$ throughout the entire decline in every case. The mechanism is structural: during a genuine crash, there is by construction no accumulated trade-volume history below the current price (the price is visiting a level for the first time), so the downside barrier search terminates at the grid boundary rather than at a genuine local KDE maximum. The upside barrier, meanwhile, sits at the prior pump's HVN, giving $\Delta U_{\text{down}} \gg \Delta U_{\text{up}}$ at every slide moment, saturating $P_t^{-} \to 0$ regardless of the true forward probability of continued decline. Re-confirmed independently across iterations 8, 14, and 20 on three different datasets. The escape-rate formalism is asymmetrically blind to first-visit downside moves by construction.

---

## 9. Acceptance Protocol and Iteration History

### 9.1 Five-Gate Acceptance Protocol

Every candidate change is evaluated by re-running all completed recordings through a fresh `ForwardTester` pass and comparing against the current canonical baseline using a paired-difference protocol with five simultaneous gates, all of which must pass for acceptance:

1. Wilcoxon signed-rank (one-sided, "greater") on paired per-recording $\Delta_i$: $p < 0.05$.
2. Paired $t$-test on $\Delta_i$: $p < 0.05$.
3. Bootstrap 95% CI of mean $\Delta$: strictly positive (excludes zero).
4. McNemar's test on the per-token win/loss pivot: $p < 0.05$.
5. Breadth: $\ge 50\%$ of common tokens individually improve.

Gate 5 is an explicit anti-overfit guard against changes that improve aggregate PnL only through a handful of outlier tokens.

### 9.2 Complete Iteration Ledger

*PnL in SOL; PF = profit factor. "ACCEPTED" means all five gates cleared.*

| Iter | Label | Trades | WR | PnL | PF | Outcome |
|---|---|---|---|---|---|---|
| 01 | baseline | 0 | — | 0 | — | Structural failure — filter never traded (Finding 4.2) |
| 02 | v2subset | 79 | 7.6% | −0.241 | 0.08 | REJECTED: traded, but churned |
| 03 | v2subset | 34 | 35.3% | −0.153 | 0.59 | CANDIDATE: direction from $P^{\pm}$ |
| 03b | subset30 | 66 | 39.4% | −0.007 | 0.99 | Break-even on 30-token subset |
| 04 | subset30 | 16 | 93.75% | +0.610 | 150.0 | ACCEPTED: Bayesian exit-only |
| 04b | random100 | 14 | 100% | +0.274 | ∞ | Confirmed; low frequency |
| 04c | sub50b | 62 | 82.3% | +0.768 | 7.96 | Confirmed on 50-token set |
| 09 | signflip | few | 3.1% | neg. | — | REJECTED: 457× overtrading on empty $\rho$ |
| 13 | anchor_rho | 11 | 29.2% | −0.090 | — | REJECTED: $k_{\text{down}} = 10^6$ every uptrend |
| 14 | dt_fix / ig | 7/519 | 0%/3.4% | 0/−1.889 | — | REJECTED: $\Delta t = 0.25$ silenced entries; IG-catalyst churned 519 trades |
| 15 | recorder_fix | n/a | n/a | n/a | n/a | Data-pipeline patch: all prior data had zero order-flow |
| 16 | data_landfall | n/a | n/a | n/a | n/a | Fresh dataset with real order flow; prior findings invalidated |
| 16-base | baseline_full | 287 | 24.4% | −0.798 | 0.257 | New baseline: 74% entries in EXHAUSTION at 22% WR |
| 16b | signflip_full | 528 | 24.2% | −1.926 | 0.40 | REJECTED |
| 16c | spec_kramers | 19,657 | 17.2% | −35.91 | 0.02 | REJECTED: vol-corr $e^{50}$ (Finding 5.4) |
| 16h | cost-cal | 2,707 | 33.4% | −6.061 | 0.48 | REJECTED PnL; cost-cal foundation kept |
| 16i | tau_smoke | 502 | 29.1% | −1.078 | 0.37 | REJECTED: $\tau$ pinned by $\phi^2\tau^2$ risk term |
| 16j | var_phi_full | 2,606 | 33.8% | −5.930 | 0.50 | REJECTED PnL; correctness fix kept |
| 16k | $T_w=14{,}400$ | 325 | 41.5% | −0.534 | 0.842 | Breakthrough (Finding 5.1) |
| 16l | floor_full | 401 | 38.9% | +0.110 | 1.030 | Best full-batch to date; gates not cleared ($p=0.39$) |
| 16m | hysteresis | 60 | 45.0% | +0.202 | 1.42 | REJECTED: Kelly hysteresis blocked profitable rebounds |
| 16n | sign_flip_U | 34 | 35.3% | −0.054 | 0.75 | REJECTED: $U=+T\log\rho$ destroys entries |
| 16o | boundary_fix | 86/56 | 36/32% | +0.054/−0.396 | 1.11/0.30 | REJECTED: net negative |
| 17a | full | 187 | 56.2% | +0.572 | 1.41 | Best WR↔PnL point; REJECTED on breadth |
| 17b | past-peak gate | 135 | 52.6% | +0.363 | 1.39 | REJECTED (Finding 7.1) |
| 17c | tighter overlays | 194 | 57.7% | +0.123 | 1.09 | REJECTED: cuts right tail |
| 18b | opt hard-stop off | 217 | 75.6% | +0.437 | 1.31 | ACCEPTED: all 5 gates |
| 19 | give-frac 0.4 | 229 | 78.6% | +0.547 | 1.36 | ACCEPTED: all 5 gates |
| 21 | kelly_flat K60 | 259 | 77.2% | +0.884 | 1.55 | CANDIDATE vs. iter 19; ACCEPTED vs. iter 16 |
| 22 | canonical | 366 | 76.5% | +1.120 | 1.48 | **NEW CANONICAL BASELINE** (558 recordings) |
| 22 | k35 offside 35 | 370 | 74.9% | +0.886 | 1.35 | REJECTED: CI strictly negative |
| 22 | k45 offside 45 | 366 | 76.8% | +1.108 | 1.48 | REJECTED: statistical parity, no mechanism gain |

*Table 3: Full iteration ledger.*

### 9.3 The Order-Flow Data Integrity Bug (Iteration 15)

Perhaps the single most consequential finding of the program was not modelling-related. Iteration 15 discovered that the on-chain recorder had never been correctly extracting buy/sell volume from the Solana `accountSubscribe` vault-delta stream: every recording from iterations 1 through 14 had buy volume and sell volume populated with zero or degenerate values. Every experiment in iterations 1–14 was therefore conducted on price-only data, with the order-flow-pressure state $\phi_t$ receiving no genuine signal. Once the recorder was patched and data re-collected (iteration 16), the research log explicitly bars use of any pre-iteration-15 baseline as an acceptance target. This is a cautionary methodological finding: a large, sophisticated ablation program can proceed for many iterations on a subtly broken data pipeline without any individual experiment's statistics revealing the defect.

---

## 10. Iteration 22: Loss Anatomy and the Pareto-Frontier Result

### 10.1 Loss Attribution

On the iteration 22 canonical batch of 366 trades, 49 trades with loss $\le -20\%$ account for $-2.10$ SOL — 90.8% of gross loss and $-187.7\%$ of net PnL. Table 4 decomposes this by exit reason.

| Exit | $n$ | PnL (SOL) | % of BIG-loss |
|---|---|---|---|
| `kelly_flat` | 32 | −1.432 | 68.1% |
| `recording_ended` | 12 | −0.503 | 23.9% |
| `bayesian_flip` | 2 | −0.082 | 3.9% |
| `kramers_down_exit` | 2 | −0.053 | 2.5% |
| `reversal_exit` | 1 | −0.033 | 1.6% |

*Table 4: Big-loss attribution by exit reason, iteration 22.*

### 10.2 Big-Loser Trajectory Analysis

Three measurements characterise the BIG-loser population:

**Fast-crash trajectory.** 37/49 BIG losers touch $-10\%$ below entry within 60 seconds (vs. 21% of winners); median time to $-10\%$ is 10s (BIG) vs. 28s (WIN), both differences significant under Mann–Whitney $U$.

**"Never arm" dynamic.** Median peak unrealised gain is $+2.1\%$ for BIG losers vs. $+14.4\%$ for winners. Only 5/49 BIG losers ever reach the $+10\%$ gain-retrace arming threshold. The dominant loss mechanism is a post-entry phenomenon, not an entry-time selection problem.

**Depth and dwelling.** Median max drawdown $-44.8\%$ (BIG) vs. $-5.6\%$ (WIN); fraction of bars below entry 98.5% (BIG) vs. 31.0% (WIN).

### 10.3 Entry-Time Feature Indistinguishability

All 26 tested entry-time engine features — including $P_t^{+}$, $E^{*}$, $\sigma_t$, $\phi_t$, $\mu_t$, $h_t$, $\Delta U_{\text{down}}$, momentum, trend confidence, and 17 others — are statistically indistinguishable between BIG losers and winners by Mann–Whitney $U$ ($p > 0.05$ for every feature). The sole exception, holding time ($p = 0.011$), is an outcome, not an entry-time predictor. This confirms and sharpens Finding 8.1: the RBPF/Kramers posterior does not encode, in its current feature set, the information needed to distinguish a good entry from a catastrophic one at the moment of entry.

### 10.4 Exhaustive Counterfactual Sweep — A Negative Result

Ten purpose-built analysis scripts tested every price-only, time-only, or engine-posterior-only stop rule the team could construct via full candle-path replay of all 558 recordings:

- **Fixed hard stop** $L \in \{-15\%, \dots, -40\%\}$: only $L=-40\%$ is marginally net-positive ($+0.056$ SOL, touching only 1 winner); every shallower threshold cuts 23–67 winners.
- **First-$W$-seconds stop** ($W \in [10,180]$s, $L \in [8\%,25\%]$, 87-rule grid): only 2/87 combinations are marginally net-positive, the best at $+0.042$ SOL — smaller than the gain already captured by the production `kelly_flat` rule.
- **Tick-streak rule** (consecutive negative closes + offside): net $-0.1$ to $-8.5$ SOL across the grid.
- **Late-armed floor** (only act after minimum hold): net-negative at every threshold.
- **Post-big-loss cooldown** (120–3600s on same token): net $-0.13$ to $-0.22$ SOL; winners frequently rebound immediately after a losing entry on the same token.

**Empirical Finding 10.1 (Pareto frontier).** Combined with the offside-bracket parameter sweep (`no_long_offside_pct` $\in \{35, 40, 45\}$, both directions rejected), this constitutes an exhaustive negative result: **on the current 558-token dataset and current engine feature set, no price-only, time-only, or engine-posterior-only decision rule improves aggregate PnL without a net-negative trade-off against winning trades.** The iteration 22 configuration is a measured Pareto frontier. Two directions of unexplored surface remain: (a) a regime-conditioned $E^{*}$ threshold restricted to idle-regime entries (static projection $+0.30$ SOL, but Finding 7.1's replacement-entry warning applies and live-batch validation is pending), and (b) external state not currently modelled — most plausibly a multi-scale, faster-decaying down-barrier KDE to address Finding 8.1, or holder-concentration/cross-token contagion features exogenous to the five-dimensional latent state.

---

## 11. Execution Infrastructure

### 11.1 Lookahead-Free Intra-Candle Backtest Replay

The offline `Backtester` expands each stored 1-second candle into four intra-candle states that replicate the live `CandleAggregator`'s evolution:

- **State 1 (open):** $c = h = l = \text{open}$, vol $= 0$
- **State 2 (first extreme):** $c = \text{high}$ (bull) or $\text{low}$ (bear), vol $= 0$
- **State 3 (second extreme):** $c = \text{second extreme}$, vol $= 0$
- **State 4 (close):** $c = \text{close}$, vol $= \text{full bar volume}$

so all filter state (UKF covariance, EMAs, Mehra $R$) evolves identically to a genuine live tick stream. A pending BUY/EXIT signal detected during candle $N$ executes at State 1 (the open) of candle $N+1$ — a strict one-bar execution delay.

### 11.2 Forward-Tester Fill Model

The paper-trading simulator models the intra-bar fill time as a fill fraction $\in [0.02, 0.98]$:

$$f_{\text{fill}} = \operatorname{clip}\left[\frac{f_{\text{ref}}}{f_{\text{ref}} + f_{\text{total}}} \cdot \left(1 + \log_{10}\max\!\left(1, \frac{n_{\text{SOL}}}{n_{\text{ref}}}\right)\right) \cdot (1 + \text{slip}\%/100),\ 0.02,\ 0.98\right], \tag{47}$$

with $f_{\text{ref}} = 0.0005$ SOL and $n_{\text{ref}} = 0.1$ SOL. The fill price is linearly interpolated along the realistic intra-bar path open→first-extreme→second-extreme→close at the computed fill fraction, with slippage applied multiplicatively.

### 11.3 Live On-Chain Execution

The live trader mirrors the backtester's four-state candle expansion and one-bar-delay signal deferral exactly — preserving identical indicator evolution — while broadcasting the actual swap immediately on signal detection. Execution path: Jupiter quote → Jupiter-built swap transaction → solders-based server-side signing → direct RPC broadcast. Two safety mechanisms sit outside the strategy engine: a market-cap safety floor (emergency sell if live USD market cap falls below \$6,000 while in position), and a sell watchdog that re-triggers the exit transaction if on-chain balance still shows tokens after `WATCHDOG_TIMEOUT_S` seconds. RPC endpoint selection is latency-adaptive via hot-path endpoint affinity.

---

## 12. Discussion

### 12.1 Specification as Falsifiable Hypothesis

The central methodological contribution of this work is a worked example of treating a physics-inspired market microstructure specification as a set of falsifiable hypotheses rather than an implementation target. Four terms of the original specification — the $V_{\text{liq}}$ composite potential, the volatility-of-volatility escape-rate correction, the $\tau$-linear Kelly return extrapolation, and (by default) the drift-work barrier-energy term — are mathematically well-posed as continuous-time constructs and simultaneously numerically pathological at 1-second memecoin scale. In each case the pathology was not a coding bug in the conventional sense — the equations were correctly implemented — but a scale mismatch invisible from the SDE system alone and detectable only by running the fully-specified estimator against real trade data. This suggests a general prescription for this class of model: physically-motivated closed-form corrections derived under small-parameter assumptions should be treated as provisional until their validity regime is verified against the actual scale of the target market.

### 12.2 The Limits of a Price/Volume/Order-Flow-Only State

Findings 8.1 and 10.1 together constitute strong evidence that the current five-dimensional latent state $\Psi_t = (x_t, \mu_t, h_t, \phi_t, \ell_t)$ is information-incomplete for distinguishing a recoverable pullback from a catastrophic decline at entry time, and that this is not a tuning deficiency addressable within the current feature set. The exhaustive negative result of §10 covers price-only, time-only, and posterior-only rule families broadly enough to make further within-feature-set search unlikely productive. This motivates two concrete extensions: a multi-scale (dual-bandwidth or dual-$T_w$) KDE providing genuine down-barrier structure on tokens with no prior downside trade history, and features genuinely exogenous to a single token's price/volume tape — most plausibly holder-concentration dynamics or cross-token contagion.

### 12.3 Threats to Validity

Three limitations warrant explicit statement. First, all acceptance decisions use the same evolving dataset (235→558 recordings over 22 iterations); the five-gate protocol guards against overfit on any single change but repeated sequential testing against the same data carries a multiple-comparisons risk not directly controlled for. Second, "recording" boundaries conflate two qualitatively different situations — arbitrary data-collection termination vs. genuine token activity cessation — and the `recording_ended` forced-close bucket conflates these. Third, the backtester's fill model, while calibrated to observed cost figures, remains a simulation; this paper reports backtest/forward-test performance, not live-capital results.

---

## 13. Conclusion

We have documented, equation by equation and experiment by experiment, the complete evolution of a non-equilibrium statistical mechanics trading engine from theoretical specification through a 22-iteration empirical research program to a statistically-gated production configuration (iteration 22: 366 trades, 76.5% win rate, +1.12 SOL, profit factor 1.48 on 558 tokens). The as-built estimator retains the specification's core architecture — RBPF/UKF Bayesian state estimation, a volume-weighted KDE market potential, modified Kramers escape rates, and a Kelly-optimal decision layer — while removing or replacing four specification terms shown to be numerically pathological at the engine's 1-second operating scale. An exhaustive counterfactual sweep establishes the current configuration at a measurable Pareto frontier given its current feature set, with the remaining loss exposure attributable to information genuinely absent from a price/volume/order-flow-only latent state. We believe the identification of this information boundary, as a direct empirical output of a rigorous sequential falsification program, is of independent interest to any physics-inspired microstructure model applied to thinly-traded, adversarial, non-stationary markets.

---

## References

[1] H. A. Kramers, "Brownian motion in a field of force and the diffusion model of chemical reactions," *Physica*, 7(4):284–304, 1940.

[2] P. Hänggi, P. Talkner, and M. Borkovec, "Reaction-rate theory: fifty years after Kramers," *Rev. Mod. Phys.*, 62(2):251–341, 1990.

[3] R. K. Mehra, "On the identification of variances and adaptive Kalman filtering," *IEEE Trans. Autom. Control*, 15(2):175–184, 1970.

[4] S. J. Julier and J. K. Uhlmann, "Unscented filtering and nonlinear estimation," *Proc. IEEE*, 92(3):401–422, 2004.

[5] R. van der Merwe, *Sigma-Point Kalman Filters for Probabilistic Inference in Dynamic State-Space Models*, PhD thesis, OHSU, 2004.

[6] A. Doucet, N. de Freitas, K. Murphy, and S. Russell, "Rao-Blackwellised particle filtering for dynamic Bayesian networks," *Proc. UAI*, pp. 176–183, 2000.

[7] J. L. Kelly, "A new interpretation of information rate," *Bell Syst. Tech. J.*, 35(4):917–926, 1956.

[8] A. S. Kyle, "Continuous auctions and insider trading," *Econometrica*, 53(6):1315–1335, 1985.

[9] S. L. Heston, "A closed-form solution for options with stochastic volatility," *Rev. Financ. Stud.*, 6(2):327–343, 1993.

[10] O. Barndorff-Nielsen, "Exponentially decreasing distributions for the logarithm of particle size," *Proc. Roy. Soc. A*, 353:401–419, 1977.

[11] A. Harvey, *Dynamic Models for Volatility and Heavy Tails*, Cambridge University Press, 2016.

[12] B. W. Silverman, *Density Estimation for Statistics and Data Analysis*, Chapman & Hall, 1986.

---

## Appendix A: Complete Configuration Reference

| Meta-parameter | Default | Description |
|---|---|---|
| `v2_p_up_min` | 0.62 | Entry gate: minimum $P_t^{+}(\tau)$ |
| `v2_sigma_t_min` | 0.021 | Entry gate: minimum $\sigma_t$ |
| `gain_retrace_arm_pct` | 10.0 | Trailing-lock arm threshold (%) |
| `gain_retrace_give_frac` | 0.4 | Trailing-lock give-back fraction (iter 19) |
| `breakeven_arm_dd_pct` | 25.0 | Breakeven-scratch arm drawdown (%) |
| `breakeven_buffer_pct` | 2.5 | Breakeven-scratch exit buffer (%) |
| `reversal_exit_bars` | 2 | Reversal persistence guard (iter 18b) |
| `no_long_exit_bars` | 60 | `kelly_flat` persistence window |
| `no_long_offside_pct` | 40 | `kelly_flat` offside threshold (%) |
| `stoploss_pct` | 0.0 | Hard stop (disabled, Finding 7.2) |
| `iter05_decay_entry_block` | 1.0 | Leading-decay entry block (enabled) |
| `iter05_decay_exit_enable` | 0.0 | Leading-decay exit (disabled) |
| `iter05_decay_window` | 60 | Trailing window for decay block |
| `iter05_decay_window_thresh` | 0.8 | Fraction negative for block |
| `v2_drift_work_fraction` | 0.0 | Drift-work knob (disabled by default) |
| `alpha_ukf` | 0.3 | UKF scaled unscented transform $\alpha$ |
| `beta_ukf` | 2.0 | UKF $\beta$ (Gaussian prior assumption) |
| `kappa_ukf` | 0.0 | UKF $\kappa$ (secondary scaling) |
| `ema_alpha` | 0.05 | EMA smoothing coefficient (all EWMA) |

*Table 5: Complete non-SDE configuration at iteration 22 production defaults.*

## Appendix B: Notation

| Symbol | Meaning |
|---|---|
| $x_t$ | log-price |
| $\mu_t$ | instantaneous drift |
| $h_t = \log\sigma_t^2$ | log-variance |
| $\phi_t$ | signed order-flow pressure |
| $\ell_t = \log L_t$ | log-Kyle-depth (liquidity) |
| $r_t$ | discrete regime label |
| $\rho(x,t)$ | volume-weighted KDE of the trade tape |
| $U(x,t) = -T_t\log\rho$ | non-equilibrium market potential (production) |
| $T_t = \sigma_t^2/2$ | effective market temperature |
| $V_{\text{liq}}(x,t)$ | liquidity cost field (computed; excluded from $U$ in production) |
| $k_t^{\pm}$ | modified Kramers escape rates (up/down) |
| $P_t^{\pm}(\tau), P_t^{0}(\tau)$ | competing-risk transition probabilities over horizon $\tau$ |
| $z^{*}$ | decision direction $\in \{-1,0,+1\}$ |
| $n^{*}$ | Kelly-optimal position size |
| $E^{*}$ | expected Kelly log-wealth increment |
| $\hat\mu_\tau^{(\text{prod})}$ | barrier-distance-weighted expected return (Eq. 43) |
| $\sigma_\tau^2$ | horizon variance (Eq. 44) |
| $d^{\pm} = |x_t^{\pm} - x_t|$ | barrier distances |
| $\gamma_t = 1/L_t$ | Kyle-lambda friction coefficient |
| $T_w$ | KDE memory window |
| $N_p$ | particle count |
| ESS | effective sample size $= (\sum_i (w^{(i)})^2)^{-1}$ |