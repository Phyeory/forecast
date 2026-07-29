# A Bayesian State-Space Framework for Short-Horizon Memecoin Trading

**Jaime Mok**

## Abstract

This document specifies a complete mathematical framework for a short-horizon (5–30 second) trading engine designed for highly volatile, organically driven Solana memecoins. The framework abandons standard Langevin dynamics in favor of a non-equilibrium statistical mechanics approach. By combining a Rao-Blackwellised Particle Filter (RBPF) with a volume-weighted Kernel Density Estimate (KDE) market potential and modified Kramers escape rates, the engine derives Bayesian posterior probabilities of barrier crossings. Trading decisions are ultimately driven by a Kelly-optimal expected log-wealth increment, ensuring mathematical rigor in the presence of transaction costs, slippage, and latency.

---

## 1. State Space Formulation

Let the traded asset be a memecoin with mid-price $S_t$ and log-price $x_t = \log S_t$. We construct a continuous-time stochastic framework to model the dynamics of $x_t$ on a horizon $\tau \in [5, 30]$ seconds.

### 1.1 Observable Variables

The observation at second $k$ is the discrete-time multivariate process:

$$y_k = \left(\Delta x_k,\ v_k,\ \delta_k,\ q_k,\ d_k^{(b)},\ d_k^{(a)}\right), \quad t_k = k\Delta t \tag{1}$$

where $\Delta x_k$ is the log-return over $\Delta t = 1$s, $v_k$ is the total traded volume, $\delta_k$ is the signed volume imbalance (Lee–Ready classification), $q_k$ is the quoted bid-ask spread, and $d_k^{(b)}, d_k^{(a)}$ are the bid and ask depths within a proximal range.

### 1.2 Latent Variables

The latent state vector is defined as the tuple $\Psi_t := (x_t, \mu_t, h_t, \phi_t, \ell_t, r_t) \in \mathbb{R}^5 \times \mathbb{R}$, where:

- $x_t$: Log mid-price.
- $\mu_t = \lim_{\Delta \downarrow 0} \mathbb{E}[\Delta x_t / \Delta \mid \mathcal{F}_t]$: Instantaneous expected log-return (drift).
- $h_t = \log \sigma_t^2$: Log instantaneous variance.
- $\phi_t = \mathbb{E}[\delta_t / \ell_t \mid \mathcal{F}_t]$: Order-flow pressure normalized by liquidity.
- $\ell_t = \log L_t$: Log instantaneous Kyle depth (price impact per unit volume).
- $r_t \in \mathbb{R}$: Discrete market regime label.

---

## 2. Governing Stochastic Differential Equations

The continuous latent state evolves according to a system of stochastic differential equations derived from microstructural equilibrium arguments.

### 2.1 Price Dynamics

The log-price obeys a jump-diffusion process where the drift is explicitly coupled to the order-flow pressure via Kyle's lambda ($1/L_t$):

$$dx_t = \mu_t\, dt + \frac{\phi_t}{L_t}\, dt + e^{h_t/2}\, dW_t^{(x)} + J_t\, dN_t \tag{2}$$

where $W_t^{(x)}$ is a standard Wiener process, $N_t$ is a Cox process with volume-dependent intensity $\lambda_J(t) = \lambda_0 + \lambda_1 v_t$, and $J_t$ are i.i.d. double-exponential jump sizes.

### 2.2 Drift Evolution

Drift is modeled as mean-reverting (sentiment decay) and is pushed by sustained, unexpected order flow:

$$d\mu_t = -\lambda_\mu \mu_t\, dt + \kappa_\mu(\phi_t - \bar\phi_t)\, dt + \sigma_\mu\, dW_t^{(\mu)} \tag{3}$$

where $\bar\phi_t$ is the rolling mean of order-flow pressure, and $\kappa_\mu(\phi_t - \bar\phi_t)$ encodes that only deviations from the running baseline update sentiment.

### 2.3 Volatility Evolution

Log-variance follows a mean-reverting Ornstein–Uhlenbeck (OU) process, guaranteeing positivity without boundary constraints:

$$dh_t = -\eta(h_t - \bar h_t)\, dt + \sigma_h\, dW_t^{(h)} \tag{4}$$

where $\bar h_t$ is the long-run log-variance, tracked recursively as an exponential moving average of realized log-variance.

### 2.4 Order-Flow Pressure

The autocorrelation of order flow on short horizons is empirically single-exponential, allowing a Markovian embedding via an OU process:

$$d\phi_t = -\alpha \phi_t\, dt + \beta \left(\frac{\delta_t}{v_t + \varepsilon}\right) dt + \sigma_\phi\, dW_t^{(\phi)} \tag{5}$$

where $\varepsilon \ll 1$ is a mathematical regularizer to prevent division-by-zero in vacuum intervals.

### 2.5 Liquidity Evolution

Effective Kyle depth $L_t$ is observable via $\hat L_k = v_k \cdot q_k / |\Delta x_k|$. The log-liquidity follows:

$$d\ell_t = -\theta(\ell_t - \bar\ell)\, dt + \sigma_\ell\, dW_t^{(\ell)} - \zeta\, \mathbb{1}_{\{|dN_t| > 0\}}\, dN_t \tag{6}$$

where the jump term $\zeta > 0$ models sudden liquidity withdrawal.

---

## 3. Market Potential

We construct a non-equilibrium market potential $U(x,t)$ such that price motion is modeled as an overdamped particle in $U$ with structural temperature $T_t = \sigma_t^2/2$.

### 3.1 Volume-Weighted KDE

Let $\{(x_i, v_i, t_i)\}$ be the trade tape. The empirical price density is:

$$\rho(x,t) = \frac{1}{Z(t)} \sum_{i:\, t_i \in [t - T_w,\, t]} v_i\, K_h(x - x_i) \tag{7}$$

where $K_h(\cdot) = h^{-1}K(\cdot/h)$ is a Gaussian kernel, $T_w = 300$s is the memory window, and $Z(t) = \sum v_i$ is the normalizer. The bandwidth $h$ is determined by Silverman's rule adapted for volume weights.

### 3.2 Liquidity Cost Field

The marginal price-impact cost of moving price from $x_t$ to $u$ is:

$$V_{liq}(x,t) = \int_{x_t}^{x} \frac{|u - x_t|}{\sqrt{L_t \cdot D(u,t)}}\, du \tag{8}$$

where $D(u,t)$ is the local depth density obtained by interpolating L2 snapshots.

### 3.3 Non-Equilibrium Potential

The market potential is defined as:

$$U(x,t) = -T_t \log \rho(x,t) + V_{liq}(x,t) \tag{9}$$

In equilibrium, an overdamped Langevin particle in $U$ at temperature $T$ has stationary density $\rho^{eq}(x) \propto e^{-U(x)/T}$. Our construction inverts this: given an empirical $\rho$, the potential that generates it is $-T\log\rho$. The liquidity term encodes the non-equilibrium friction.

### 3.4 Barrier Energy

Local minima of $U(\cdot,t)$ represent basins of attraction (support). Local maxima $x_t^{(b)}$ represent barriers (resistance). The upward and downward barrier energies are defined as:

$$\Delta U_t^+ = U(x_t^{(+)}, t) - U(x_t, t) + \frac{1}{2}\mu_t\left(x_t^{(+)} - x_t\right) \tag{10}$$

$$\Delta U_t^- = U(x_t^{(-)}, t) - U(x_t, t) - \frac{1}{2}\mu_t\left(x_t^{(-)} - x_t\right) \tag{11}$$

The drift-work terms correctly adjust the barrier height: a positive drift lowers the upward barrier (drift-assisted) and raises the downward barrier (drift-opposed).

---

## 4. Modified Kramers Escape Rates

Classical Kramers escape rate $k \propto \exp(-\Delta U/T)$ assumes equilibrium, constant $T$, and time-independent $U$. We modify this to handle non-equilibrium driving, stochastic volatility, and Kyle-lambda friction.

### 4.1 Non-Equilibrium Density Ratio

We replace the Boltzmann factor with the exact empirical density ratio (Large Deviation Principle):

$$e^{-\Delta U/T} \longrightarrow \frac{\rho_{emp}(x_t^{(\pm)}, t)}{\rho_{emp}(x_t, t)} \tag{12}$$

### 4.2 Stochastic Temperature Correction

We average the Boltzmann factor over the volatility posterior:

$$\left\langle e^{-\Delta U/T} \right\rangle_{h_t} \approx e^{-\Delta U/T_t} \cdot \exp\left[\frac{1}{2}\left(\Delta U/T_t\right)^2 \operatorname{Var}(h_t \mid \mathcal{F}_t)\right] \tag{13}$$

This mathematically captures that volatility-of-volatility increases escape probability.

### 4.3 Friction Correction

Friction $\gamma$ is replaced by the Kyle lambda inverse:

$$\gamma_t = \frac{1}{L_t} \tag{14}$$

### 4.4 Final Escape Rates

The modified Kramers rates over the barriers are:

$$k_t^{\pm} = \frac{\sqrt{U''(x_t)\,|U''(x_t^{(\pm)})|}}{2\pi L_t} \cdot \frac{\rho(x_t^{(\pm)}, t)}{\rho(x_t, t)} \cdot \exp\left[\frac{1}{2}\left(\Delta U_t^{\pm}/T_t\right)^2 \operatorname{Var}(h_t \mid \mathcal{F}_t)\right] \tag{15}$$

### 4.5 Transition Probabilities

Using a competing-risk exit model and the slow-variation approximation over horizon $\tau$, the probabilities of upward and downward escape are:

$$P_t^+(\tau) \approx \frac{k_t^+}{k_t^+ + k_t^-}\left(1 - e^{-(k_t^+ + k_t^-)\tau}\right) \tag{16}$$

$$P_t^-(\tau) \approx \frac{k_t^-}{k_t^+ + k_t^-}\left(1 - e^{-(k_t^+ + k_t^-)\tau}\right) \tag{17}$$

$$P_t^0(\tau) = e^{-(k_t^+ + k_t^-)\tau} \tag{18}$$

where $P_t^0(\tau)$ is the probability of consolidation (no barrier crossing).

---

## 5. Bayesian Estimation: Rao-Blackwellised Particle Filter

Because the system is non-linear, non-Gaussian, and contains a discrete regime variable $r_t$, we employ a Rao-Blackwellised Particle Filter (RBPF).

### 5.1 Continuous Layer (UKF)

Conditional on $r_t = i$ and the jump path, the continuous subsystem $(x_t, \mu_t, h_t, \phi_t, \ell_t)$ is approximately linear-Gaussian. It is propagated via an Unscented Kalman Filter (UKF) per particle. The UKF uses the scaled unscented transform with sigma points $\chi_i = \mu \pm \left(\sqrt{(L+\lambda)P}\right)_i$.

The marginal measurement likelihood for the particle weight update strictly uses the UKF innovation variance $P_{zz}$:

$$p(y_k \mid \Psi_k^{(i)}) \propto \exp\left[-\frac{1}{2}(y_k - \hat z_k)^2 P_{zz}^{-1}\right] \tag{19}$$

### 5.2 Discrete Layer (Particle Filter)

The discrete layer maintains $N_p$ particles. Each particle carries a regime label $r_t^{(i)}$ and a UKF instance. The transition rates $Q_{ij}(\Psi_t)$ between regimes are derived from the rate at which the phase vector crosses topological separating surfaces, rather than being imposed exogenously. Systematic resampling is triggered when the Effective Sample Size (ESS) falls below $N_p/2$.

---

## 6. Kelly-Optimal Trading Decisions

Trading decisions are driven by the Kelly criterion, specifically maximizing the expected log-wealth increment.

### 6.1 Expected Log-Wealth

For a trade of signed size $z \in \{-1, +1\}$ and notional $n$ executed at slippage $s(n, \ell_t)$ and fee rate $f$, the post-trade log-wealth change over horizon $\tau$ is:

$$\Delta \log W = z \cdot n \cdot \left[\Delta x_{t,t+\tau} - z\left(s(n, \ell_t) + f\right)\right] - \frac{1}{2}n^2 \sigma_{res}^2\, \tau \tag{20}$$

### 6.2 Bayesian Edge

The Bayesian edge of a long trade is:

$$E^+(n, t, \tau) := \mathbb{E}\left[\Delta \log W \mid \mathcal{F}_t, z=+1, n=n\right] = n\left(\hat\mu_\tau - s(n, \hat\ell_t) - f\right) - \frac{1}{2}n^2 \hat\sigma_\tau^2 \tag{21}$$

where $\hat\mu_\tau = \mathbb{E}[x_{t+\tau} - x_t \mid \mathcal{F}_t]$ and $\hat\sigma_\tau^2 = \operatorname{Var}[x_{t+\tau} - x_t \mid \mathcal{F}_t]$ are computed from the RBPF posterior and escape rates.

### 6.3 Entry and Exit Logic

The optimal entry triggers iff there exists $(z, n)$ such that $E^z(n, t, \tau^\star) > 0$, where $\tau^\star$ is the horizon maximizing the edge. The optimal size is the analytical maximum:

$$n^\star = \frac{\hat\mu_{\tau^\star} - f - s_0}{\hat\sigma_{\tau^\star}^2 + \partial s/\partial n} \tag{22}$$

The position is held until the optimal stopping time:

$$\tau_{exit} = \inf\left\{s > t : E^z(n^\star, s, \tau_{rem}) \le 0\right\} \tag{23}$$

This ensures the engine exits the moment the expected future drift no longer covers the marginal cost of holding (fees + slippage + latency drift).

---

## 7. Trade Expectancy and Positive EV Proof

To rigorously demonstrate that the engine only executes trades with a mathematical advantage, we derive the closed-form expectancy of a trade and prove it is strictly positive.

### 7.1 Defining the Trade Expectancy

Let $X$ be the expected log-wealth increment of a single trade. At entry time $t$, for a chosen direction $z \in \{-1,+1\}$ and horizon $\tau$, the expectancy is given by the Bayesian Edge equation:

$$\mathbb{E}[X \mid \mathcal{F}_t, z, n] = E^z(n, t, \tau) = n(\hat\mu_\tau - C) - \frac{1}{2}n^2\hat\sigma_\tau^2 \tag{24}$$

where:

- $\hat\mu_\tau = \mathbb{E}[x_{t+\tau} - x_t \mid \mathcal{F}_t]$ is the posterior expected log-return.
- $\hat\sigma_\tau^2 = \operatorname{Var}[x_{t+\tau} - x_t \mid \mathcal{F}_t]$ is the posterior variance.
- $C = f + s_0 + |\hat\mu_t|\Delta_{lat}$ is the aggregate cost function (fees, base slippage, latency drift). For simplicity, we fold the marginal slippage $s_1$ into the variance term, letting $\hat\sigma_\tau^2$ represent $\hat\sigma_\tau^2 + s_1$.

### 7.2 Finding the Optimal Size $n^\star$

The engine sizes the trade to maximize the expected log-wealth (Kelly criterion). We find $n^\star$ by taking the derivative of $E$ with respect to $n$ and setting it to zero:

$$\frac{\partial E}{\partial n} = (\hat\mu_\tau - C) - n\hat\sigma_\tau^2 = 0 \tag{25}$$

Solving for $n$ yields the optimal Kelly size:

$$n^\star = \frac{\hat\mu_\tau - C}{\hat\sigma_\tau^2} \tag{26}$$

### 7.3 Deriving the Closed-Form Expectancy

Substitute the optimal size $n^\star$ back into the original expectancy equation to find the maximized expectancy $\mathbb{E}[X^\star]$:

$$\mathbb{E}[X^\star] = n^\star(\hat\mu_\tau - C) - \frac{1}{2}(n^\star)^2\hat\sigma_\tau^2 \tag{27}$$

$$\mathbb{E}[X^\star] = \left(\frac{\hat\mu_\tau - C}{\hat\sigma_\tau^2}\right)(\hat\mu_\tau - C) - \frac{1}{2}\left(\frac{\hat\mu_\tau - C}{\hat\sigma_\tau^2}\right)^2 \hat\sigma_\tau^2 \tag{28}$$

$$\mathbb{E}[X^\star] = \frac{(\hat\mu_\tau - C)^2}{\hat\sigma_\tau^2} - \frac{1}{2}\frac{(\hat\mu_\tau - C)^2}{\hat\sigma_\tau^2} \tag{29}$$

$$\mathbb{E}[X^\star] = \frac{1}{2}\frac{(\hat\mu_\tau - C)^2}{\hat\sigma_\tau^2} \tag{30}$$

### 7.4 Proof that $\mathbb{E}[X] > 0$

**Claim.** The expectancy of any executed trade in the framework is strictly positive ($\mathbb{E}[X^\star] > 0$).

**Proof.**

1. By definition of the stochastic processes, the posterior variance over a finite horizon $\tau$ is strictly positive: $\hat\sigma_\tau^2 > 0$.
2. Therefore, the denominator $\hat\sigma_\tau^2$ is strictly positive.
3. The numerator is a squared term: $(\hat\mu_\tau - C)^2 \ge 0$. It equals zero if and only if $\hat\mu_\tau = C$.
4. The engine's entry logic strictly mandates that a trade is only executed if:
   $$E^z(n^\star, t, \tau^\star) > 0 \tag{31}$$
   Which implies:
   $$\frac{1}{2}\frac{(\hat\mu_\tau - C)^2}{\hat\sigma_\tau^2} > 0 \tag{32}$$
5. For this strict inequality to hold, the numerator cannot be zero. Therefore, $\hat\mu_\tau \ne C$.
6. Because the engine only enters when the expected move exceeds the cost ($\hat\mu_\tau > C$), we have $(\hat\mu_\tau - C)^2 > 0$.
7. A strictly positive numerator divided by a strictly positive denominator yields a strictly positive result.

$$\therefore \mathbb{E}[X^\star] > 0 \qquad \blacksquare \tag{33}$$

### 7.5 Expanding $\hat\mu_\tau$ via Transition Probabilities

To make the expectancy fully observable in terms of the engine's math, we substitute the posterior expected return $\hat\mu_\tau$. Using the modified Kramers escape probabilities, the expected return is the probability-weighted distance to the barriers:

$$\hat\mu_\tau = P_t^+(\tau) \cdot d^+ - P_t^-(\tau) \cdot d^- \tag{34}$$

Where $d^+$ and $d^-$ are the distances to the upward and downward barriers. Substituting this into the expectancy formula yields the complete, mathematically rigorous expectancy of the system:

$$\mathbb{E}[X^\star] = \frac{1}{2}\frac{\left(P_t^+(\tau)d^+ - P_t^-(\tau)d^- - C\right)^2}{\hat\sigma_\tau^2} \tag{35}$$

Because the entry gate strictly requires $P_t^+(\tau)d^+ - P_t^-(\tau)d^- > C$, the square of this difference is strictly positive, mathematically guaranteeing that every trade taken by the engine has a positive expected log-wealth increment.

---

## 8. Topological Regime Derivation

Regimes are not defined by arbitrary thresholds, but by the topology of the phase vector $p_t = (\mu_t, \dot\mu_t, \phi_t, h_t, \ell_t, d_t)$ relative to derived noise floors:

$$\mu_t^\star = \sigma_t / \sqrt{\tau} \quad \text{(drift noise floor)} \tag{36}$$

$$\phi_t^\star = \sigma_\phi / \sqrt{\alpha} \quad \text{(flow noise floor)} \tag{37}$$

The partition identifies **Trend** ($\|\mu_t\| > \mu_t^\star$, accelerating, flow-aligned), **Exhaustion** ($\|\mu_t\| > \mu_t^\star$, decelerating), **Reversal** ($\mu_t$ crossed zero, opposing flow), and **Consolidation** ($\|\mu_t\| < \mu_t^\star$, near basin center). The transition rates between these topological regions are fully determined by the SDE dynamics.

---

## 9. Computational Architecture

To meet the sub-2ms latency requirement for live trading, the math-critical inner loops (UKF propagation, KDE evaluation, barrier grid search) are Just-In-Time (JIT) compiled using `numba`. The RBPF runs $N_p = 200$ particles, resulting in a per-tick computational cost of $\mathcal{O}(N_p \cdot n^3) \approx 2.5 \times 10^4$ FLOPs, ensuring real-time streaming capability without look-ahead bias.