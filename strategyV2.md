# A Non-Equilibrium Statistical Mechanics Framework for Short-Horizon Memecoin Trading

*Quantitative Research*

## Abstract

This document rigorously specifies a stochastic trading engine designed to predict short-term (5–30 second) continuation and reversal probabilities in highly volatile, organic Solana memecoins. The framework abandons standard Langevin dynamics in favor of a continuous-discrete Bayesian state-space model, driven by a Rao-Blackwellised Particle Filter (RBPF). Price dynamics are governed by a non-equilibrium market potential derived from volume-weighted kernel density estimation and liquidity costs. Transition probabilities are mathematically derived via modified Kramers escape rates. We conclude by rigorously deriving the expected return $E[X]$ and the expected profit objective.

## 1. State Space Formulation

Let the traded asset be a Solana memecoin with log-price $x_t = \log S_t$. We seek a model for $x_t$ on a horizon $\tau \in [5, 30]$ seconds. The latent state vector is defined as the tuple:

$$
\Psi_t := \bigl(x_t, \mu_t, h_t, \phi_t, \ell_t, r_t\bigr) \in \mathbb{R}^5 \times \mathcal{R} \tag{1}
$$

where $x_t$ is log-price, $\mu_t$ is instantaneous drift, $h_t = \log \sigma_t^2$ is log-volatility, $\phi_t$ is signed order-flow pressure, $\ell_t$ is log-effective-liquidity, and $r_t \in \mathcal{R}$ is a discrete regime label.

### 1.1 Stochastic Differential Equations

The continuous-time dynamics are governed by the following system of stochastic differential equations (SDEs):

**Price Dynamics**

The log-price evolves as an Itô process with drift, order-flow impact, diffusion, and jumps:

$$
dx_t = \mu_t dt + \frac{\phi_t}{L_t} dt + \sigma_t dW^{(x)}_t + J_t dN_t \tag{2}
$$

where $L_t = e^{\ell_t}$ is the Kyle depth, $W^{(x)}_t$ is a standard Wiener process, $N_t$ is a Cox process with volume-dependent intensity $\lambda_J(t) = \lambda_0 + \lambda_1 v_t$, and $J_t$ are i.i.d. double-exponential jump sizes.

**Drift Evolution**

Drift is mean-reverting and updated by unexpected order-flow:

$$
d\mu_t = -\lambda_\mu \mu_t dt + \kappa_\mu (\phi_t - \bar{\phi}_t) dt + \sigma_\mu dW^{(\mu)}_t \tag{3}
$$

**Volatility Evolution**

Log-variance follows a mean-reverting Ornstein-Uhlenbeck (OU) process:

$$
dh_t = -\eta (h_t - \bar{h}_t) dt + \sigma_h dW^{(h)}_t \tag{4}
$$

**Order-Flow Pressure**

Order-flow pressure is modeled as an OU process driven by the normalized signed volume $\delta_t / (v_t + \varepsilon)$:

$$
d\phi_t = -\alpha \phi_t dt + \beta \frac{\delta_t}{v_t + \varepsilon} dt + \sigma_\phi dW^{(\phi)}_t \tag{5}
$$

**Liquidity Evolution**

Log-liquidity follows an OU process with a jump component to model sudden liquidity withdrawal:

$$
d\ell_t = -\theta(\ell_t - \bar{\ell}) dt + \sigma_\ell dW^{(\ell)}_t - \zeta dN^{(\ell)}_t \tag{6}
$$

### 1.2 Observation Model

Observations occur at discrete 1-second intervals $t_k = k\Delta t$. The observation vector is $\mathbf{y}_k = (\Delta x_k, v_k, \delta_k, q_k, d^{(b)}_k, d^{(a)}_k)$. The measurement equation for log-returns is:

$$
\Delta x_k = \int_{t_{k-1}}^{t_k} \Bigl(\mu_s + \frac{\phi_s}{L_s}\Bigr) ds + \sigma_k^{(\text{eff})} \varepsilon^{(x)}_k + \sum_{j:N_j \in (t_{k-1}, t_k]} J_j \tag{7}
$$

where $\sigma_k^{(\text{eff})} = \int e^{h_s} ds$ and $\varepsilon^{(\cdot)}_k \sim \mathcal{N}(0, 1)$.

## 2. Non-Equilibrium Market Potential

We construct a market potential $U(x, t)$ such that price motion is *as if* $x_t$ were an overdamped particle in $U$ with temperature $T_t = \sigma_t^2 / 2$.

### 2.1 Volume-Weighted Density

Let $\{(x_i, v_i, t_i)\}$ be the trade tape. The volume-weighted price density is:

$$
\rho(x, t) = \frac{1}{Z(t)} \sum_{i:\, t_i \in [t - T_w, t]} v_i K_h(x - x_i) \tag{8}
$$

where $K_h(u) = h^{-1} K(u/h)$ is a Gaussian kernel with Silverman bandwidth $h$, $T_w$ is the memory window, and $Z(t) = \sum v_i$.

### 2.2 Liquidity Cost Field

The liquidity cost field is defined as the marginal price-impact cost:

$$
V_{\text{liq}}(x, t) = \int_{x_0}^{x} \frac{|u - x_t|}{\sqrt{L_t \cdot D(u, t)}} du \tag{9}
$$

where $D(u, t)$ is the local depth density at price level $u$.

### 2.3 Composite Potential

The total market potential is:

$$
U(x, t) = -T_t \log \rho(x, t) + V_{\text{liq}}(x, t) \tag{10}
$$

### 2.4 Barrier Energy

Define local minima of $U$ as basins and local maxima as barriers $x^{(\pm)}_t$. The upward and downward barrier energies are:

$$
\Delta U^{\pm}_t = U(x^{(\pm)}_t, t) - U(x_t, t) \pm \frac{1}{2}\mu_t (x^{(\pm)}_t - x_t) \tag{11}
$$

The $\pm \frac{1}{2}\mu_t \Delta x$ term represents the work done by the drift in climbing the barrier.

## 3. Modified Kramers Escape Rates

Classical Kramers theory assumes equilibrium and constant temperature. We modify the escape rates to account for non-equilibrium driving, stochastic volatility, and Kyle-lambda friction.

The modified upward and downward escape rates $k^{\pm}_t$ are:

$$
k^{\pm}_t = \frac{\sqrt{U''(x_t) |U''(x^{(\pm)}_t)|}}{2\pi \gamma_t} \cdot \frac{\rho(x^{(\pm)}_t, t)}{\rho(x_t, t)} \cdot \exp\left(-\frac{\Delta U^{\pm}_t}{T_t}\right) \cdot \mathcal{V}^{\pm}_t \tag{12}
$$

where:

- $\gamma_t = 1/L_t$ is the Kyle-lambda friction coefficient.
- $\dfrac{\rho(x^{(\pm)}_t, t)}{\rho(x_t, t)}$ is the exact non-equilibrium density ratio.
- $\mathcal{V}^{\pm}_t = \exp\left( \frac{1}{2} \left(\frac{\Delta U^{\pm}_t}{T_t}\right)^2 \text{Var}(h_t \mid \mathcal{F}_t) \right)$ is the volatility-of-volatility correction.

### 3.1 Transition Probabilities

Using the slow-variation approximation over horizon $\tau$, the competing-risk exit probabilities are:

$$
P^+_t(\tau) \approx \frac{k^+_t}{k^+_t + k^-_t}\Bigl(1 - e^{-(k^+_t + k^-_t)\tau}\Bigr) \tag{13a}
$$

$$
P^-_t(\tau) \approx \frac{k^-_t}{k^+_t + k^-_t}\Bigl(1 - e^{-(k^+_t + k^-_t)\tau}\Bigr) \tag{13b}
$$

$$
P^0_t(\tau) \approx e^{-(k^+_t + k^-_t)\tau} \tag{13c}
$$

## 4. Decision Theory and Trading Rules

### 4.1 Directional Decision

The direction $z^* \in \{-1, 0, 1\}$ is determined strictly by the Bayesian posterior probabilities of barrier escape:

$$
z^* = \begin{cases}
+1 & \text{if } P^+_t(\tau) > P^-_t(\tau) \text{ and } P^+_t(\tau) > P^0_t(\tau) \\
-1 & \text{if } P^-_t(\tau) > P^+_t(\tau) \text{ and } P^-_t(\tau) > P^0_t(\tau) \\
0 & \text{otherwise}
\end{cases}
$$

### 4.2 Trade Execution and Sizing

A trade is triggered in direction $z^*$ if the posterior probabilities indicate a statistically meaningful escape likelihood. The trade size $n$ is bounded by the instantaneous market liquidity to prevent adverse market impact and slippage exhaustion:

$$
n \le 0.1 \cdot L_t \tag{14}
$$

where $L_t = e^{\ell_t}$ is the posterior Kyle depth. This ensures the position size scales dynamically with the market's capacity to absorb the trade without catastrophic slippage.

## 5. Rigorous Derivation of Expected Return $E[X]$

We now rigorously derive the expected return over the trading horizon $\tau$, denoted $E[X]$, where $X = x_{t+\tau} - x_t$.

> **Theorem (Expected Log-Return).** Given the latent state $\Psi_t$ at time $t$, the expected log-return over horizon $\tau$ is:
>
> $$
> \mathbb{E}[X \mid \Psi_t] \approx \mu_t \tau + \frac{\phi_t}{L_t \alpha} \Bigl(1 - e^{-\alpha \tau}\Bigr) + \lambda_J \mathbb{E}[J] \tau
> $$

**Proof.**

By definition, $X = x_{t+\tau} - x_t = \int_t^{t+\tau} dx_s$. Substituting the price SDE from Eq. (2):

$$
X = \int_t^{t+\tau} \mu_s ds + \int_t^{t+\tau} \frac{\phi_s}{L_s} ds + \int_t^{t+\tau} \sigma_s dW^{(x)}_s + \int_t^{t+\tau} J_s dN_s
$$

Taking the conditional expectation $\mathbb{E}[X \mid \Psi_t]$, the Itô integral vanishes since $\mathbb{E}\left[\int \sigma_s dW_s\right] = 0$. We are left with:

$$
\mathbb{E}[X \mid \Psi_t] = \mathbb{E}\left[\int_t^{t+\tau} \mu_s ds \mid \Psi_t\right] + \mathbb{E}\left[\int_t^{t+\tau} \frac{\phi_s}{L_s} ds \mid \Psi_t\right] + \mathbb{E}\left[\int_t^{t+\tau} J_s dN_s \mid \Psi_t\right]
$$

Under the slow-variation approximation over the short horizon $\tau \le 30\text{s}$, we treat the drift and liquidity as locally constant: $\mu_s \approx \mu_t$ and $L_s \approx L_t$. The first term trivially integrates to:

$$
\mathbb{E}\left[\int_t^{t+\tau} \mu_s ds \mid \Psi_t\right] \approx \mu_t \tau
$$

For the order-flow term, we require the conditional expectation of the order-flow pressure $\phi_s$. Since $\phi_t$ follows an OU process $d\phi_t = -\alpha \phi_t dt + \dots$, its deterministic evolution from an initial state $\phi_t$ is known exactly:

$$
\phi_s = \phi_t e^{-\alpha(s-t)} \quad \text{for } s \in [t, t+\tau]
$$

Substituting this into the integral yields:

$$
\mathbb{E}\left[\int_t^{t+\tau} \frac{\phi_s}{L_s} ds \mid \Psi_t\right] \approx \frac{1}{L_t} \int_0^\tau \phi_t e^{-\alpha u} du = \frac{\phi_t}{L_t} \left[ \frac{-e^{-\alpha u}}{\alpha} \right]_0^\tau = \frac{\phi_t}{L_t \alpha} \Bigl(1 - e^{-\alpha \tau}\Bigr)
$$

For the jump component, $N_s$ is a Cox process with intensity $\lambda_J$. The expected number of jumps is $\lambda_J \tau$, and the expected jump size is $\mathbb{E}[J]$. Thus:

$$
\mathbb{E}\left[\int_t^{t+\tau} J_s dN_s \mid \Psi_t\right] \approx \lambda_J \mathbb{E}[J] \tau
$$

Summing these components, we arrive at the exact expected log-return:

$$
\mathbb{E}[X \mid \Psi_t] \approx \mu_t \tau + \frac{\phi_t}{L_t \alpha} \Bigl(1 - e^{-\alpha \tau}\Bigr) + \lambda_J \mathbb{E}[J] \tau
$$

For sufficiently small $\alpha \tau$ (which holds for $\tau \le 30\text{s}$), we can apply the first-order Taylor expansion $1 - e^{-\alpha \tau} \approx \alpha \tau$, simplifying the expression to:

$$
\hat{\mu}_\tau \equiv \mathbb{E}[X \mid \Psi_t] \approx \left( \mu_t + \frac{\phi_t}{L_t} \right) \tau + \lambda_J \mathbb{E}[J] \tau
$$

$\blacksquare$

### 5.1 Expected Profit $E[\Pi]$

Let a trade be opened at time $t$ with signed direction $z \in \{-1, 1\}$ and notional $n$. The expected profit over horizon $\tau$, net of proportional fees $f$, base slippage $s_0$, and latency drift cost, is:

$$
\mathbb{E}[\Pi \mid z, n] = n \left( z \hat{\mu}_\tau - f - s_0 - |z \mu_t| \Delta_{\text{lat}} \right)
$$

The engine triggers a trade if and only if the expected profit is strictly positive, $\mathbb{E}[\Pi \mid z^*, n] > 0$, subject to the liquidity constraint on $n$ defined in Eq. (13).

## 6. Estimation via Rao-Blackwellised Particle Filter

The latent state $\Psi_t$ is estimated online using a Rao-Blackwellised Particle Filter (RBPF).

- **Particle Layer**: $N_p = 200$ particles are maintained, each carrying a discrete regime label $r^{(i)}_t$ and a continuous state sample. Resampling is triggered when the Effective Sample Size (ESS) falls below $N_p / 2$.
- **Kalman Layer**: Conditional on the particle's discrete state, the continuous 5-dimensional state is propagated and updated using an Unscented Kalman Filter (UKF). The UKF employs the scaled unscented transform to handle the nonlinearities in $1/L_t$ and $e^{h_t/2}$.

The posterior mean $\widehat{\Psi}_t = \sum_i w^{(i)}_t \Psi^{(i)}_t$ provides the inputs for the Kramers escape and decision logic.

## 7. Conclusion

This document mathematically specifies a complete stochastic trading engine. By grounding short-horizon memecoin price action in a non-equilibrium statistical mechanics framework, we derive continuous transition probabilities and a rigorous expected return $E[X]$ that forms the mathematical basis of the trading decision and expected profit objective.