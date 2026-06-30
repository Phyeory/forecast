# Coding Prompt: `strategy_engine.py`

## Context

You are implementing `strategy_engineV2.py` — a single Python module that turns a stream of on-chain market events for one migrated Pump.fun token (Solana, trading on a constant-product AMM such as Raydium or PumpSwap, market cap roughly 40k–200k USD) into a regime classification, a confidence score, and an entry/exit decision.

The math this module implements comes from a specific theoretical framework (summarized in the formula reference at the bottom of this prompt). Do not invent alternative formulas — implement the ones given. Where a derivation explicitly says "calibrated empirically" or "fitted online," implement the calibration/fitting mechanism, not a hardcoded constant.

**Scope: this file only.** Do not implement execution, order routing, RPC/websocket ingestion, position sizing, or a backtester. The engine consumes already-parsed events and returns decisions; what the caller does with those decisions is out of scope.

---

## Top-level object

```python
class StrategyEngine:
    def __init__(self, pool_invariant_k: float, population_priors: "PopulationPriors", config: "EngineConfig"): ...
    def on_pool_update(self, pool: "PoolState") -> None: ...
    def on_trade(self, trade: "Trade") -> "EngineOutput": ...
    def on_holder_snapshot(self, snapshot: "HolderSnapshot") -> None: ...
```

`StrategyEngine` is the only object the caller touches directly. It owns one instance of each component below, keyed to a single token, and maintains all state internally between calls — every public method is an O(1)-ish incremental update on the previous state, not a full reprocessing of history. The engine must work correctly when called minutes after a token migrates with almost no history, and must not require a minimum window of data to produce *some* output (degrade gracefully — see "Cold start" below).

`on_trade` is the main entry point: each new trade event triggers a full recompute of regime/confidence/decision and returns an `EngineOutput`. `on_pool_update` and `on_holder_snapshot` update internal state without necessarily producing a decision (pool reserves change every slot; holder snapshots may arrive on a slower cadence).

---

## Data contracts

Define these as `@dataclass(frozen=True)` (or `slots=True` dataclasses) at the top of the module. Use them as the only inputs/outputs of the public API — do not let callers pass raw dicts.

```python
@dataclass(frozen=True)
class PoolState:
    timestamp: float          # unix seconds
    reserve_token: float      # R_t
    reserve_quote: float      # Q_t (SOL or USDC)
    lp_locked: bool
    lp_burned: bool

@dataclass(frozen=True)
class Trade:
    timestamp: float
    price: float               # p_t, quote per token, post-trade
    side: Literal["buy", "sell"]
    size_quote: float           # trade size in quote currency
    wallet: str
    is_dev_wallet: bool

@dataclass(frozen=True)
class HolderSnapshot:
    timestamp: float
    holders: list[tuple[str, float, float]]   # (wallet, tokens_held, vwap_cost_basis_price)

@dataclass(frozen=True)
class EngineOutput:
    timestamp: float
    regime: Literal["trend", "exhaustion", "transition", "breakout", "chop", "idle", "terminal"]
    edge: float                 # Edge = P_up - P_down
    p_up: float
    p_down: float
    confidence: float           # C'  (hazard-penalized log-likelihood-ratio score)
    mu_over_sigma: float
    v_edge: float                # dEdge/dt
    a_edge: float                # d2Edge/dt2
    lambda_rug: float
    survival_prob: float
    decision: Literal["buy", "sell", "hold", "exit"]
    diagnostics: dict[str, float]   # everything else useful for logging/debugging
```

---

## Components to implement

Each component below should be its own class, independently unit-testable, composed inside `StrategyEngine`. Every formula reference (§N) maps to the corresponding section of the framework appendix at the end of this prompt — put that section number in the docstring of the method that implements it.

### 1. `AMMPotential` (§3)

Pure function of pool state, no internal mutable state beyond the current `k`.

- `reserve_from_price(p) -> R, Q` — invert the constant-product invariant.
- `potential(p) -> float` — \(U_{AMM}(p) = \sqrt{k p}\).
- `marginal_force(p) -> float` — \(-\partial U_{AMM}/\partial p = -\tfrac12\sqrt{k/p}\).
- `cost_to_reach(p_from, p_to) -> float` — \(\sqrt{k}(\sqrt{p_{to}} - \sqrt{p_{from}})\), signed (positive = costs quote currency to push price up to that level).

`k` should be recomputed from each `PoolState` as `reserve_token * reserve_quote` and treated as the live invariant (it changes if liquidity is added/removed, which is itself a signal — see `RugHazardModel`).

### 2. `CostBasisField` (§4)

Maintains a running, incrementally-updatable kernel density estimate over holder cost bases.

- `update(snapshot: HolderSnapshot) -> None` — replace/update the internal holder table.
- `density(p) -> float` — \(\rho(p) = \sum_i n_i K_h(p - p_i)\). Use a Gaussian kernel; bandwidth `h` should be a config parameter with a sane default derived from the empirical std-dev of cost bases (Silverman's rule is acceptable), not a fixed magic number.
- `sell_pressure(p) -> float` — \(S(p) = \int \rho(p') h_{sell}(p/p')\,dp'\). Implement `h_sell(r)` as a pluggable function (default: a smooth bump increasing for `r` near round multiples 2, 5, 10 and increasing again as `r \to 0`; expose it as an injectable callable so it can later be replaced with a fitted version per §11 without touching this class).
- `buy_pressure(p) -> float` — \(D(p)\), driven by current Hawkes buy intensity (pull from `LatentStateEstimator`, injected as a dependency rather than recomputed here).
- `local_extrema() -> tuple[float, float]` — nearest density maxima above and below current price; these become \(p_r, p_s\) for the escape probability model. Use a simple local-maximum search over a discretized grid of `density(p)`; document the grid resolution as a config parameter.
- `combined_potential(p, gamma) -> float` — \(U(p) = U_{AMM}(p) + \gamma\, U_{cost\_basis}(p)\), where `U_cost_basis` is the integral of `(buy_pressure - sell_pressure)` from a reference price to `p` (implement via numerical integration — trapezoidal is fine, this does not need to be fast).

### 3. `LatentStateEstimator` (§2, §6)

Owns the Kalman filter and the Hawkes intensity states. This is the only component allowed genuinely latent, online-updated state.

- Kalman filter for \(\mu_t\) (OU-process drift) and \(\sigma_t^2\) (EWMA realized variance from log returns) — standard linear Kalman update on each new `Trade`. Expose `mu`, `sigma2` as properties.
- Hawkes intensity for buy side, \(\lambda_{buy}(t) = \lambda_0 + \sum \kappa e^{-\beta(t-t_i)}\), and an independent Hawkes intensity for sell side with a concentration covariate, \(\lambda_{sell}(t) = \lambda_0 + \phi(C_t) + \sum \kappa e^{-\beta(t-t_i)}\) (§6). Implement the exponential-kernel update incrementally (the standard trick: maintain a running sum that decays multiplicatively between events and gets a `+1` term added at each new event — do not recompute the full sum from event history each call).
- `concentration() -> float` — top-10 holder share or Gini coefficient, computed from the latest `HolderSnapshot` held by `CostBasisField` (inject as a dependency, don't duplicate the holder table).

### 4. `EscapeProbabilityModel` (§5)

Stateless given current potential and barrier locations.

- `compute(p, sigma2, U: CostBasisField, amm: AMMPotential) -> (p_up, p_down, edge)` — get \(p_r, p_s\) from `U.local_extrema()`, compute \(\Delta U_{up}, \Delta U_{down}\) via `amm.cost_to_reach` plus the cost-basis contribution, normalize by `sigma2` to get \(E_{up}, E_{down}\), apply the Boltzmann normalization to get \(P_{up}, P_{down}\), return `Edge = P_up - P_down`.
- Guard explicitly against `sigma2 == 0` (early in a token's life, or during a dead-flat period) — clamp to a small epsilon rather than dividing by zero.

### 5. `JumpRiskModel` (§6)

Tracks recent large-sell events and exposes the current sell-side jump intensity and an implied jump-size distribution.

- `register_trade(trade: Trade) -> None` — feed large sells (size above a configurable quote-currency threshold) into the Hawkes sell intensity (delegates to `LatentStateEstimator`, doesn't duplicate state).
- `implied_jump_distribution() -> Callable[[], float]` — compose the empirical large-wallet balance distribution (estimate a Pareto tail exponent from the current `HolderSnapshot`'s upper tail, online, via a simple Hill estimator) with `AMMPotential.cost_to_reach` to produce a sampler/quantile function for "what does a realistic whale dump do to price right now." This is used for diagnostics/risk sizing by the caller, not for the entry/exit decision directly.

### 6. `RugHazardModel` (§7)

Proportional-hazards model over directly observable covariates.

- `update(pool: PoolState, holders: HolderSnapshot | None) -> None` — refresh covariates: `lp_unlocked` (from `pool.lp_locked`/`lp_burned`), `dev_wallet_active` (set true once any `Trade` arrives with `is_dev_wallet=True` and `side="sell"`), `concentration`, `1/market_cap`.
- `hazard(t) -> float` — \(\lambda_{rug}(t) = \lambda_0(t)\exp(\beta_1 \cdot lp\_unlocked + \beta_2 \cdot dev\_active + \beta_3 \cdot concentration + \beta_4 / mcap)\). `\beta` coefficients and the baseline `\lambda_0(t)` shape come from `PopulationPriors` (injected at construction, not fitted inside this class — fitting happens offline, see §11 below).
- `survival(t) -> float` — \(S(t) = \exp(-\int_0^t \lambda_{rug}(u)\,du)\), maintained incrementally (multiply by `exp(-hazard * dt)` on each update rather than re-integrating from scratch).
- `detect_terminal_event(prev_pool: PoolState, new_pool: PoolState) -> bool` — hard, deterministic trigger: a sudden large drop in `reserve_quote` inconsistent with any single observed trade size is a directly observed liquidity-pull event, not a probabilistic inference. This should short-circuit everything else in `StrategyEngine.on_trade`/`on_pool_update` and force `regime = "terminal"`, `decision = "exit"`.

### 7. `RegimeClassifier` (§8)

Pure function from the current computed quantities to a regime label — implements the table in §8 exactly (Trend / Exhaustion / Transition / Breakout / Chop / Idle / Terminal), with Terminal checked first and short-circuiting everything else. Thresholds (`k`, `E_T`, `V_T`, `A_T`, `E_C`, `C_min`, `λ*`) come from `EngineConfig`, not hardcoded.

### 8. `ConfidenceFunctional` (§9)

This is the only component that needs a fitted statistical model rather than a closed-form formula.

- Internally: a logistic regression (or its online/recursive-least-squares equivalent) mapping the feature vector `[edge, v_edge, a_edge, mu/sigma]` to a continuation-vs-reversal log-odds, i.e. `C = w · features`. Initialize `w` from `PopulationPriors.confidence_weights` (offline-fit prior); update online via a recursive update rule (RLS or stochastic gradient on log-loss) as labeled outcomes become available (a labeled outcome = "did Edge's sign hold over the next N seconds," which the engine can self-supervise by comparing past predictions to realized outcomes — implement this as an internal replay buffer of fixed length).
- `score(features, lambda_rug, w5) -> float` — returns \(C' = C - w_5 \lambda_{rug}\).
- Do not hand-pick the weights `w_1..w_4`. The whole point of §9 is that they are fitted, not asserted — if you find yourself hardcoding them, stop and implement the online logistic update instead.

### 9. `EntryExitPolicy` (§10)

- `no_trade_band(sigma2, theta) -> (c_sell, c_buy)` — compute band half-width using the \(\theta^{1/3}\) asymptotic scaling from §10 as a starting point (`half_width = K * (theta / sigma2) ** (1/3)` with `K` a config constant), then return `(-half_width, +half_width)`. Document clearly in a comment that this is the *asymptotic justification*, not an exact closed-form optimum, per the framework's own caveat — do not present it as exact.
- `decide(confidence, c_sell, c_buy, regime, current_position: bool) -> Literal["buy","sell","hold","exit"]`:
  - if `regime == "terminal"`: always `"exit"` regardless of everything else.
  - if not in a position and `confidence > c_buy` and `edge > 0` and `v_edge > 0` and `a_edge > 0`: `"buy"`.
  - if in a position and (`edge` changes sign, or `v_edge < 0`, or `confidence < c_sell`): `"exit"`.
  - otherwise: `"hold"`.
- `theta` (proportional transaction cost) should be passed in or estimated from a rolling average of realized fill slippage — expose a setter so the caller can update it from execution feedback; do not hardcode it.

### 10. `PopulationPriors` / `HierarchicalParameterStore` (§11)

This is a data container plus a shrinkage update, **not** an offline calibration pipeline — the offline fitting of `θ_pop` from the historical token corpus is out of scope for this file (it would live in a separate calibration script). What belongs here:

```python
@dataclass
class PopulationPriors:
    theta_pop: dict[str, float]       # population means for OU lambda, Hawkes beta/kappa, hazard betas, confidence weights
    sigma_pop: dict[str, float]       # population variances, used for shrinkage weighting

class HierarchicalParameterStore:
    def __init__(self, priors: PopulationPriors): ...
    def posterior(self, param_name: str, token_estimate: float, token_n_obs: int) -> float:
        """James-Stein-style shrinkage: blend token_estimate toward theta_pop[param_name],
        with shrinkage weight decreasing as token_n_obs grows."""
```

Every component above that has a fittable parameter (Hawkes `beta`/`kappa`, hazard `beta_1..4`, confidence `w_1..w_5`, KDE bandwidth `h`) should pull its *prior* from `PopulationPriors` at construction and call `HierarchicalParameterStore.posterior(...)` to blend in token-specific evidence as it accumulates — not use the population prior forever, and not switch to a raw per-token MLE the moment any data exists.

---

## Cold start

On a freshly migrated token, `CostBasisField` may have one holder snapshot, `LatentStateEstimator`'s Kalman filter has an uninformative prior, and `RugHazardModel` has only the baseline hazard. The engine must still return a well-formed `EngineOutput` after the very first trade — wide confidence intervals / heavy reliance on `PopulationPriors` is correct behavior here, a crash or `None` return is not. Write a short cold-start test scenario (first trade ever) as a sanity check while implementing, even though full test suite construction is out of scope for this file.

---

## Numerical stability checklist

Handle these explicitly, with comments at the relevant line:

- `k = 0` or near-zero reserves (just after a rug, or pathological input) — short-circuit to terminal regime rather than dividing by zero in `AMMPotential`.
- `sigma2 = 0` in `EscapeProbabilityModel`'s normalization — clamp to `epsilon`.
- Hawkes intensity sums growing unbounded if `beta` is too small relative to event rate — clamp cumulative intensity to a sane ceiling and log a warning rather than letting it diverge.
- `log(0)` in any likelihood-ratio computation in `ConfidenceFunctional` — clamp probabilities away from 0/1 before taking logs.
- KDE bandwidth collapsing to ~0 if all holders share an identical cost basis (common right after migration, when most holders bought in the first block) — enforce a minimum bandwidth floor.

---

## Definition of done

- All ten components above exist as separate, independently constructable classes with type hints and docstrings citing the relevant framework section (§N).
- `StrategyEngine.on_trade` runs end-to-end on a single synthetic trade against a freshly constructed engine (cold start) without raising.
- No component silently hardcodes a value that the framework specifies as "calibrated" or "fitted" — those must route through `PopulationPriors` / `HierarchicalParameterStore`.
- No execution, sizing, persistence, or networking code anywhere in this file.

---

## Formula reference appendix

```
U_AMM(p)            = sqrt(k * p)
force(p)             = -d U_AMM/dp = -0.5 * sqrt(k / p)
cost_to_reach(p0,p1) = sqrt(k) * (sqrt(p1) - sqrt(p0))

rho(p')              = sum_i n_i * K_h(p' - p_i)                     [holder KDE]
S(p)                 = integral rho(p') * h_sell(p / p') dp'
U(p)                 = U_AMM(p) + gamma * U_cost_basis(p)

dU_up   = U(p_r) - U(p);   dU_down = U(p_s) - U(p)
E_up    = dU_up / sigma^2; E_down  = dU_down / sigma^2
P_up    = exp(-E_up) / (exp(-E_up) + exp(-E_down));  P_down = 1 - P_up
Edge    = P_up - P_down

lambda_sell(t) = lambda_0 + phi(C_t) + sum_{t_i<t} kappa * exp(-beta*(t - t_i))   [mirror for buy side]

lambda_rug(t)  = lambda_0(t) * exp(b1*lp_unlocked + b2*dev_active + b3*concentration + b4/mcap)
S_surv(t)      = exp(- integral_0^t lambda_rug(u) du )

C  = w1*Edge + w2*V_E + w3*A_E + w4*(mu/sigma)      [log-likelihood-ratio combination, weights fitted via logistic regression]
C' = C - w5 * lambda_rug(t)

no_trade_band_half_width ~ K * (theta / sigma^2)^(1/3)
```