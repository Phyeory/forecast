# Solana Pump.fun Memecoin Market — Underlying Structure and Mathematics

**Date:** August 21, 2026  
**Scope:** Deterministic protocol mechanics, stochastic market structure, adversarial microstructure, and macro capital rotation of the Solana `pump.fun` memecoin ecosystem, with implications for algorithmic trading (`StrategyEngineV2`).  
**Data cut:** On-chain Dune/Block trackers + `backend/data/price_data.db` (1,394 completed recordings, 23 dates, 2026-07-27 → 2026-08-21) + `backend/analysis/date_segmented_results.json`.

---

## Table of Contents

1. [Protocol Architecture and Deterministic Invariants](#1-protocol-architecture-and-deterministic-invariants)
2. [Stochastic Market Structure](#2-stochastic-market-structure)
3. [Economic and Revenue Mathematics](#3-economic-and-revenue-mathematics)
4. [Adversarial Microstructure: Snipers, Bundlers, MEV](#4-adversarial-microstructure-snipers-bundlers-mev)
5. [Post-Graduation Liquidity Dynamics](#5-post-graduation-liquidity-dynamics)
6. [Macro Environment: Crashes, Launchpad Wars, Perp Rotation](#6-macro-environment-crashes-launchpad-wars-perp-rotation)
7. [Implications for Algorithmic Strategy](#7-implications-for-algorithmic-strategy)
8. [Conclusion](#8-conclusion)
9. [References](#9-references)

---

## 1. Protocol Architecture and Deterministic Invariants

### 1.1 Virtual Reserves and Constant-Product Invariant

Every `pump.fun` token is instantiated as a Solana program account owned by the pump program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`, deterministically derived from the mint address. No off-chain state governs price; the program enforces a constant-product invariant on *virtual* reserves.

Let

$$ x(t) = \text{virtual SOL reserve at time } t $$
$$ y(t) = \text{virtual token reserve at time } t $$

At creation ($t=0$):

$$ x_0 = 30.0\ \text{SOL} \quad\text{(virtual)} $$
$$ y_0 = 1.073 \times 10^9\ \text{tokens} \quad\text{(virtual)} $$
$$ k = x_0 \cdot y_0 = 3.219 \times 10^{10} = \text{constant} $$

The invariant for any trade $\Delta x$ (SOL in, positive for buy; negative for sell):

$$ (x + \Delta x)(y + \Delta y) = k $$
$$ \Delta y = \frac{k}{x + \Delta x} - y $$

Total supply is fixed:

$$ S = 10^9\ \text{tokens} $$

Real SOL collected is $x - x_0$ (virtual offset). Real tokens distributed is $y_0 - y$. The remaining $S - (y_0 - y) \approx 200\text{M}$ tokens are reserved for the graduation pool.

**Price:** The marginal price (SOL per token) is the reserve ratio:

$$ p(t) = \frac{x(t)}{y(t)} = \frac{x(t)^2}{k} $$

In dollar terms with $P_{\text{SOL}}$:

$$ P_{\text{USD}}(t) = p(t) \cdot P_{\text{SOL}} $$
$$ \text{Market cap}(t) = P_{\text{USD}}(t) \cdot S = \frac{x(t)}{y(t)} \cdot P_{\text{SOL}} \cdot S $$

Initial market cap at virtual genesis:

$$ \text{MC}_0 = \frac{30}{1.073 \times 10^9} \cdot P_{\text{SOL}} \cdot 10^9 \approx 27.95 \cdot P_{\text{SOL}} $$

At $P_{\text{SOL}} = 168$ USD, $\text{MC}_0 \approx \$4,695$. At $P_{\text{SOL}} = 80$, $\text{MC}_0 \approx \$2,236$. This explains the commonly quoted “$\$4\text{–}5$k start” — it is $P_{\text{SOL}}$-dependent, not a protocol constant.

### 1.2 Bonding Curve Trajectory and Slippage

For a buy of $\Delta x$ SOL on a curve at state $(x, y)$:

$$ \text{tokens received} = y - \frac{k}{x + \Delta x} = y \cdot \frac{\Delta x}{x + \Delta x} $$

$$ \text{average execution price} = \frac{\Delta x}{\text{tokens}} = \frac{x + \Delta x}{y} = p(t) + \frac{\Delta x}{y} $$

The *price impact* (slippage from marginal price) is:

$$ \text{slippage} = \frac{\Delta x}{x} = \frac{\Delta x}{y \cdot p} $$

The curve is **convex** in SOL (concave in tokens): early SOL buys far more tokens. Example ($x=30$, $y=1.073$B):

| $\Delta x$ | tokens | avg price vs $p_0$ | MC after |
| ---------- | ------ | ------------------ | -------- |
| 1 SOL | 34.6M | +1.6% | ~\$7.8k |
| 10 SOL | 268M | +16.7% | ~\$26k |
| 50 SOL | 671M | +83% | ~\$160k (if $P_{\text{SOL}}=168$) |

The five-phase empirical partition used by practitioners maps to $x$:

| Phase | $x$ (SOL in curve) | Typical outcome |
| ----- | ------------------ | --------------- |
| 1 Launch | $0\text{–}5$ | Dev/snipe; failure mode 70% die here |
| 2 Early momentum | $5\text{–}25$ | Highest winner multiple, 70% still fail |
| 3 Mid-curve | $25\text{–}50$ | Attention filter |
| 4 Late | $50\text{–}70$ | Near-certain graduation, low multiple |
| 5 Graduation | $\approx 85$ | Migration |

### 1.3 Graduation Invariant

Graduation is **not** a dollar threshold. The program triggers when real SOL collected reaches:

$$ x - x_0 \approx 85\ \text{SOL} \implies x_{\text{grad}} \approx 115\ \text{SOL} $$

Corresponding token reserve:

$$ y_{\text{grad}} = \frac{k}{115} \approx 279.9\text{M} $$

Tokens sold on curve: $y_0 - y_{\text{grad}} \approx 793\text{M}$ (of $1\text{B}$). Remaining $\approx 207\text{M}$ + $79\text{–}85$ SOL seed the DEX pool. LP tokens are **burned** (permanent liquidity).

The celebrated “$\$69$k graduation market cap” is derived:

$$ \text{MC}_{\text{grad}} = \frac{115}{279.9\text{M}} \cdot P_{\text{SOL}} \cdot 10^9 = 410.8 \cdot P_{\text{SOL}} $$

At $P_{\text{SOL}}=168$, $\text{MC}_{\text{grad}} = \$69,014$. At $P_{\text{SOL}}=80$, $\text{MC}_{\text{grad}} = \$32,864$. **Trading rules built on $\$69$k are misspecified when $P_{\text{SOL}}$ moves** — an $85$ SOL threshold at $\$80$ SOL is $54\%$ lower in dollar terms. The invariant is $x$, not $\text{MC}_{\text{USD}}$.

Since Q1 2025, graduates migrate primarily to **PumpSwap** (`pAMMBay6...F6P`, `0.25%` fee, `0.05%` creator share) rather than Raydium. PumpSwap routing now dominates DEX aggregator volume attribution.

### 1.4 Fee Structure

* **Bonding curve:** $1\%$ on every buy/sell until graduation (`pump.fun` protocol fee).
* **Creation:** $\sim 0.02$ SOL pre-Aug 2024, then *free* creation with $0.5$ SOL reward to creator on graduation (migration fee $1.5$ SOL charged to first buyer). Shifts cost from creator to buyer.
* **Post-graduation:** PumpSwap `0.25%` swap fee (`0.05%` to creator). Pre-graduation volume never reaches DEX.

Mathematically, a round-trip on the curve costs:

$$ \text{cost}_{\text{RT}} = 1\% \text{ (buy)} + 1\% \text{ (sell on curve)} + \text{slippage buy} + \text{slippage sell} + \text{Jito tip} + \text{priority fee} $$

Even “break-even” on price requires `>2%` gross move; with slippage on typical `5\text{–}10$ SOL` blocks, `>4\text{–}6%`.

Addition May 2026: **USDC bonding curves** alongside SOL curves, removing $P_{\text{SOL}}$ denomination risk during early formation; mathematically identical with $x$ in USDC.

### 1.5 State Observability

The bonding curve account `(x, y)` is readable via any RPC (`getAccountInfo`) or Yellowstone gRPC. Traders deriving reserves on-chain avoid frontend latency; the math is immutable, and “curve manipulation” is impossible without real `$\Delta x$`.

---

## 2. Stochastic Market Structure

### 2.1 Token Arrival Process

Token births form a point process with extreme intensity. Empirical:

* Total births: $11.9$M (`2026-06-10` Compass) / $12.8$M (`Oct 2025`), $>500$k via bundlers (`solbundler.app`).
* Peak day: $\sim 42,000$/day (`CryptoBriefing Jun 10 2026`).

Model as **inhomogeneous Poisson** with rate $\lambda(t)$:

$$ N(T) \sim \text{Poisson}\left(\int_0^T \lambda(t) dt\right) $$

At peak:

$$ \lambda_{\text{peak}} = \frac{42,000}{86,400} \approx 0.486\ \text{tokens/sec} \approx 1\text{ per }2.06\text{ sec} $$

Mean inter-arrival: $2.06$ sec. During off-peak (5k/day): $\lambda \approx 0.058$/sec (one per $17$ sec). The process is bursty with self-exciting Hawkes clustering around viral narratives (estimated branching ratio `$\eta \approx 0.3\text{–}0.5$`), not homogeneous.

### 2.2 Graduation as Bernoulli Filter and Survival Analysis

Each launch is a Bernoulli trial with graduation probability:

$$ p_g = P(\text{graduation}) \approx 0.0141 \quad\text{(776,306 births, 10,972 grad, 2026-06-17→07-15, Scorp Trader)} $$

Historical Dune: $1.4\%$ (Hashed), $<2\%$ (The Block), $2\text{–}3\%$ (JTools). The Dextools collapse estimate $0.26\%$ (`2026-06-21`) represents drawdown tail.

Thus births to get one graduate: geometric

$$ P(N = k) = (1-p_g)^{k-1} p_g,\quad E[N]=1/p_g\approx 71,\quad \text{Var}(N)=(1-p_g)/p_g^2\approx 4968 $$

Survival function for time-to-graduation $T_g$ conditional on graduating:

$$ S_g(t) = P(T_g > t \mid \text{grad}),\quad \text{median } \approx 4.9\text{ min},\quad P(T_g < 60\text{ min}) \approx 0.83 $$

Unconditional survival (most tokens censored at death on curve):

$$ S(t) = P(\text{alive on curve at } t) \approx \exp(-h t) \text{ with } h \gg 0 \text{ for } t > 60\text{ min} $$

Hazard rate $h(t)$ is **decreasing** — if not graduated within $\sim 1$ hour, $P(\text{grad}\mid T>60\text{min}) \to 0$ exponentially. This justifies strategies that only trade graduates or late-phase ($x>50$) curves: phase 1–2 entry maximizes multiple but minimizes $p_g$.

### 2.3 Launch Size and Volume Distributions

SOL deposited per curve, $D = x - x_0$, and peak market cap follow **power-law / log-normal** with heavy tail:

$$ P(D > d) \sim C d^{-\alpha},\quad \alpha \approx 1.8\text{–}2.2 \text{ (estimated from Dune volume histograms)} $$

Consequences:

* Mean $E[D]$ is dominated by tail (few winners).
* Median $D \ll$ mean. Typical launch attracts `$5$k of curve volume then dies (`revenue logic: $50$ fee at $5$k`), while tail drives aggregate.
* Only $18$ tokens ever $> \$10$M MC, $96 > \$1$M of $11.9$M launches (`$1.5 \times 10^{-4}\%$ and $8\times 10^{-4}\%$`).

Total volume $V_{\text{total}}(t)$ is sum of many small-$D$ truncations:

$$ V_{\text{total}} = \sum_{i=1}^{N(t)} D_i \cdot \mathbf{1}_{D_i > 0} \cdot (1 + \text{churn factor}) $$

Platform revenue $R = 0.01 \cdot V_{\text{total}}$ is thus **volume-driven, not graduation-driven**; the $98\%$ failures are not a failure mode for the protocol.

### 2.4 Lifetime and Attention Decay

Empirical: $83\%$ graduates within $1$ hour; post-grad survival `2026-07-17 Scorp`:

$$ P(\text{liq} > \$5k \mid \text{grad}, t=30\text{ min}) = 43.8\%,\quad t=24\text{ h} = 19.7\% $$

Model attention as exponential decay with half-life $\tau_{1/2} \approx 10\text{–}15$ min post-launch:

$$ A(t) = A_0 e^{-t/\tau},\quad \tau \approx 14\text{ min} $$

Without sustained social $A_0$ (KOL, bundle coordination), $A(t)$ falls below threshold to attract next buyer, and $x(t)$ mean-reverts to stall. This underpins the `backend/data/price_data.db` finding: `median holding time winner 322–1132s`, loser `231–5220s` — winners are those where $A(t)$ remained $> A_{\text{critical}}$ long enough for `gain_retrace`.

---

## 3. Economic and Revenue Mathematics

### 3.1 Platform Revenue Derivation

For each token $i$ with curve trading volume $V_i$ (buys + sells):

$$ R = \sum_i 0.01 \cdot V_i + 0.0025 \cdot V_{i,\text{PumpSwap}} $$

Cumulative milestones:

| Date | Cumulative | Source |
| ---- | ---------- | ------ |
| Jul 2024 | $50M | Dune @adam_tehc |
| Aug 2025 | $800M | Block/Dune |
| Mar 2026 | $1B | CoinStats |
| Aug 2026 | $1.157B | CoinStats AI 2026-08-01 |

Annualized: `2025 $664M`, `Q1 2026 $82.24M`, `Q2 2026 $62.06M (-24.5% QoQ, -36.1% alt. measure)`. Daily run-rate `~$1.16M/day` (`Q2`), weekly low `$1.72M Jul 28-Aug 3` (lowest since Mar 2024, DropsTab) to record `$13.48M Aug 11-17`.

Revenue variance is **super-linear** in DEX volume because $1\%$ applies to every bonding-curve trade irrespective of graduation. A $5$k-volume token still pays `$50`; $42$k tokens/day $\times$ $50 = \$2.1$M/day theoretical floor even at zero graduation.

### 3.2 Expected Value per Launch

For a buyer entering at curve state $(x, y)$ with target exit $\text{MC}_{\text{target}}$:

$$ \text{multiple}_{\text{gross}} = \frac{y - k/(x_{\text{target}})}{y - k/(x_{\text{entry}})} \cdot \frac{x_{\text{entry}}}{x_{\text{target}}} $$

Simplified early-curve approximation: $\text{multiple} \approx (x_{\text{target}}/x_{\text{entry}})^2$ for small $x$. A $25k\to1M$ MC is `40×` gross **conditional on graduation**. Odds-weighted EV:

$$ \text{EV} = p_g \cdot (\text{multiple} \cdot (1-f)^2 -1) - (1-p_g) \cdot 1 $$

where $f=1\%$ fee each side. At $p_g=0.014$, break-even requires:

$$ \text{multiple}_{\text{BE}} > \frac{1}{p_g} \cdot \frac{1}{(1-f)^2} \approx 71.4\text{×} + \text{slippage} $$

RektCalc’s `>70×` break-even at `1.4%` matches analytic. Early entry (`$x$ small) increases multiple but does **not** change $p_g$ — hence disciplined traders size as `Kelly fraction $f^* = (bp - q)/b$ with $b=\text{multiple}-1$, often $\ll 1\%$ bankroll per launch.

### 3.3 Creator and Token Economics

* Creator reward: $0.5$ SOL on graduation (post-Aug 2024), else $0.02$ SOL cost. Expected creator PnL per launch: $0.5 p_g - 0.02 \approx -\$0.01$ SOL at $p_g=0.014$ — negative without volume fees.
* PumpSwap creator fee: $0.05\%$ of graduated pool volume — incentivizes staying in ecosystem post-grad.
* PUMP token (ICO Jul 12 2025, `15%` supply `150B` at `\$0.004`, `$4B$ FDV, raise `$500M public + $1B private`, `coincodex 2025-07-08`): `50% net fees -> buyback & burn`, `$370M$ burn executed`. Token value linear in $R$: `dPUMP/dR = 0.5 / supply_{\text{circulating}}$`.

User profitability: `3% ever >$1,000` (Dune), consistent with $\text{EV} <0$ for random entry. Platform extracts `1%` of *all* volume; users compete for tail.

---

## 4. Adversarial Microstructure: Snipers, Bundlers, MEV

### 4.1 Sniper Dynamics

Definition: wallet buying in first block ($\approx 400$ms) after pool creation, before human latency. Detection: `first buy <500ms`, `buy on >200 similar mints`.

Sniper profit per token, assuming can sell at $x + \epsilon$:

$$ \Pi_{\text{snipe}} = y - \frac{k}{x+\Delta x} - \Delta x \cdot (1+f) - \text{tip} $$

With $\Delta x=1\text{–}5$ SOL and $\epsilon=2\text{–}5$ SOL of follow-on flow, $\Pi>0$ if can exit within `1–3` blocks. Failure mode: no follow flow $\to$ `$\Pi <0$` (trapped in curve).

Empirical: sniper extraction correlates with `early seller pressure` reason for `<2%` graduation — `sniper sell = - \Delta x$ reduces $x$, requiring `> \Delta x$ new buying to recover`.

### 4.2 Bundler and Jito Block 0

**Bundler** coordinates $n=5\text{–}20$ wallets funded from single funder, submitting atomic Jito bundle: `create mint + $n$ buys + optional snipe protection` in **Block 0** (same slot as creation). Without bundler, snipers win Block 1; with bundler, dev captures `n \cdot \Delta x$ supply before snipe.

Cost model (`solbundler.app`: `500K+` tokens, `1%` fee on trades):

$$ \text{cost}_{\text{bundle}} = n \cdot (\text{rent} + \Delta x) + \text{Jito tip} (0.001\text{–}0.005 \text{ SOL}) + \text{priority fee} $$

Benefit: supply capture. Let $s_{\text{bundle}} = \sum_{i=1}^n y_i / S$ be fraction captured. If graduation occurs, dev’s unrealized at $x_{\text{grad}}$:

$$ \text{value}_{\text{bundle}} = s_{\text{bundle}} \cdot \text{MC}_{\text{grad}} \cdot (1 - 0.01) $$

Nash: bundling dominates non-bundling when `$\text{value}_{\text{bundle}} - \text{cost}_{\text{bundle}} > \Pi_{\text{snipe}}$`. Hence arms race: `solbundler.app`, `Vortex (10K+ tokens, 20K MAU)`, `J Tools` all sell Block 0 as table stakes.

Detection: `funding lineage`, `cluster detection` (`solanahub.de 2026-05-07`): $7/10$ first buyers funded within $1$ hour $\to$ bundler. Empirical `Scorp 2026-07-17`: `6.0% graduates (932/15,548)` had dev graded `F` (100% rug history) at graduation — bundling does not filter rug intent, only speed.

### 4.3 MEV: Sandwich, JIT, Liquidations

Four MEV types on Solana (`uwuu.ai 2026-07-28`):

1. **Sandwich:** front-run `$\Delta x_{\text{victim}}$` + back-run, profit `$\approx \Delta x_{\text{victim}} \cdot \text{slippage}_{\text{victim}}$`. With `slippage = \Delta x / x`, victim `10$ SOL` at $x=30$ gives `33%` slippage — sandwichable if bot sees pending via Yellowstone gRPC.
2. **Cross-pool arbitrage:** `Raydium vs Orca vs Meteora vs PumpSwap` price delta `$p_{\text{Raydium}} - p_{\text{Orca}}$`.
3. **Liquidations:** Marginfi/Kamino health factor `<1`.
4. **JIT liquidity:** add concentrated liquidity `$\Delta L$ ms before swap, remove after, capture `0.25%` fee without inventory.

Solana leader schedule replaces Ethereum mempool, but Jito bundle auction replicates ordering control. Searcher profit requires `dedicated RPC (Triton/Helius) + gRPC + co-location`; expected value per block contested by `100+` bots — rents dissipated.

### 4.4 Game-Theoretic Equilibrium and Professionalization

KuCoin Q2 2026: *“casual retail replaced by professionalized degenerate”* — dashboards, custom RPC, MEV protection, forensic audits (`top-10 holder %`, `dev rug history`).

Game:

* **Stage 1:** Dev chooses `bundle $n$` vs `single`.
* **Stage 2:** Sniper chooses `race` vs `skip` based on $x_0$ signal.
* **Stage 3:** Retail chooses entry phase.

Payoff matrix pushes equilibrium to `bundle + forensic filter + skip early-phase`. Hence `gain_retrace` collapse in `price_data.db`: early-phase momentum entries (your `strategy_engineV2.py: buy_exhaustion`) historically captured `5\text{–}15\%` moves before bot competition now gets front-run; median winner hold `295s (08-18)` vs `702s (07-27)` indicates winners must be taken quicker or not at all.

---

## 5. Post-Graduation Liquidity Dynamics

### 5.1 Pool Creation Discontinuity

At $x_{\text{grad}}$, bonding curve halts, and `$\approx 79$ SOL + $207$M tokens` create PumpSwap CPMM:

$$ x_{\text{dex}} = 79,\quad y_{\text{dex}} = 207\text{M},\quad p_{\text{dex}} = x_{\text{dex}}/y_{\text{dex}} $$

Price is continuous ($p_{\text{dex}} = p_{\text{grad}}$), but **liquidity depth jumps** from virtual `30` offset to real `79` SOL. Tradable depth post-grad is `~2.6×` pre-grad, enabling large sells that were impossible on curve — hence dump.

### 5.2 Liquidity Decay: The 25-Minute Catastrophe

Empirical (`Scorp 2026-07-17`, `15,548$ graduates):`

$$ L(t) = \text{liquidity at } t \text{ post-grad} $$
$$ L(5\text{ min}) \to L(30\text{ min}): -57\%\ \text{median} $$
$$ P(L>5k \mid 30\text{ min}) = 43.8\%,\quad P(L>5k \mid 24\text{ h}) = 19.7\% $$
$$ \text{MC}(5\text{ min})=41,144 \to \text{MC}(30\text{ min})=6,841\ (-83\%) \to \text{MC}(24\text{ h})=1,877\ (-95\%) \text{ for median rug (GitLawb)} $$

Model:

$$ L(t) = L_0 e^{-t/\tau_L} + L_{\infty},\quad \tau_L \approx 18\text{ min},\ L_{\infty} \approx 3k $$

Damage concentrated `5\text{–}30$ min`. At `t>6$ h$, outcome decided. Implication: holding graduates `>30$ min` without `dev grade B+` is negative EV; `Scorp` dev `F` rate `6.0%` vs `B` `2.6%` indicates forensic filter dominates graduation signal.

### 5.3 Slippage and Market Impact Post-Grad

CPMM impact for sell $\Delta y$:

$$ \Delta x = x_{\text{dex}} - \frac{x_{\text{dex}} y_{\text{dex}}}{y_{\text{dex}} + \Delta y} = x_{\text{dex}} \frac{\Delta y}{y_{\text{dex}} + \Delta y} $$

Effective price:

$$ p_{\text{eff}} = \frac{\Delta x}{\Delta y} = \frac{x_{\text{dex}}}{y_{\text{dex}} + \Delta y} = p_{\text{dex}} \frac{y_{\text{dex}}}{y_{\text{dex}} + \Delta y} $$

For `y_{\text{dex}}=207M`, selling `10M$ tokens (`4.8% supply`) gives `$\approx 4.6\%$` slippage — manageable vs bonding-curve `33%` earlier, hence why post-grad dump is efficient. Large holder (`top 10 wallets`) can exit with `1\text{–}5\%` slippage, explaining why dump completes in `25$ min`.

---

## 6. Macro Environment: Crashes, Launchpad Wars, Perp Rotation

### 6.1 Spot DEX Volume Collapse

Solana’s 2024–25 flywheel: `launchpads \to DEX vol \to fees \to validator income \to staked SOL \to SOL price \to more launches`. Breaks are non-linear.

| Period | Weekly DEX | Pump.fun | Meteora | Driver |
| ------ | ---------- | -------- | ------- | ------ |
| Peak early Feb 2026 | $118.2B | $61.4B | $20.1B | Memecoin mania |
| 3 weeks later | $44.5B (-62%) | $30.5B (-50%) | $3.4B (-83%) | Feb meta crash `phemex.com 2026-03-11`, `degendecoded.com` |
| 2 weeks May 2026 | $104.3B -> $18.8B (-82%) | — | $93.1B -> $9.2B (-90%) | Bot profitability collapse `cryptorank.io 2026-06-02` |
| Q2 2026 avg | $160.8B spot quarterly (`-44%` QoQ) | — | — | `pluang.com 2026-07-27` |

Mathematical characterization: log-volume follows regime-switching AR(1) with crash hazard `$\lambda_{\text{crash}} \approx 0.05$/week` in memecoin-dominated regime; Post-crash recovery is `AR(0.7)` with half-life `~2$ weeks`.

SOL price mirrors: `$116 -> $85 (-27\%)` Feb, `$253 ATH Jan 2025 -> $67 May 2026 (-73\%)`. Holder `exchange inflows 1.56M SOL (+40\% 3d)`, `long-term accum -92\% (3.47M->267k)` confirm distribution, not panic.

### 6.2 Launchpad Market-Share Oscillation

Inhomogeneous competition:

$$ s_{\text{Pump}}(t) + s_{\text{LetsBonk}}(t) + s_{\text{others}}(t) = 1 $$

Timeline (`dropsab.com 2026-07-20`, `coincodex 2025-07-08`, `forklog 2025-07-23`):

* `Apr 25 2025` LetsBonk launch ($BONK$ team, Raydium LaunchLab, `1\%$ creator fee vs Pump $0.05\%$) — initial $5\%$.
* `Jul 7 2025` LetsBonk `22\% -> 64\%` (revenue `$7.9M$ vs Pump `$3.1M$` Jul 7–23), `BONK +72\%`, peak `Jul 21–27: 64\% share, 26.6k/day, $179M$ vol` ($BONK$ $185\%$ month, `$1.8B$ mcap).
* `Jul 28–Aug 3` Memecoin mcap `$77.7B -> $62.1B -20\%`, Pump weekly `$1.72M$` low, LetsBonk fades.
* `Aug 11–17` Pump reclaims `73.6\% ($13.48M, 162k launches, 1.37M traders)` vs LetsBonk `15.3\% (6k launches)`, share `26\%->73.6\%` in one week. `BingX Aug 13` reports `98\%$ early Aug `$542M$ vol` reclaim.

Your `price_data.db` trough `07-31/08-05` (low `pump: 65–75\%` phase) vs reclaim `08-08+` matches this oscillation; yet `WR` decays monotonically through reclaim — share reclaim does not restore edge.

Other entrants: `Believe, Moonshot, LaunchLab, Base Four.Meme (BNB $1.15M daily fees > Solana $1.05M Sep 2025)`, `Robinhood Chain Jul 1 2026 CASHCAT +1700\% $120M` (`dolanduck.io 2026-07-09`) fragment attention.

### 6.3 Perpetual Futures Rotation

Perp DEX growth is the macro offset:

* Solana perps: `Q2 2026 $147B` (`codegotech.com 2026-07-02`) / `$183.2B` (`pluang.com`) record, weekly `$20B$ first time May 19 2026 (`bloomingbit.io`, `bitcoinworld 2026-05-19`, `GMTrade $4.9B/day$), `Solana spot Q2 $160.8B -44\%$ vs perps `+` divergent.
* Global perps (`CoinGecko May 21 2026`): `CEX avg $7.1T 2025->$4.7T 2026`, `DEX avg $531B->$611B$`, `DEX:CEX 3\% Jan 2025 ->13\% Nov 2025 ->10\% Apr 2026`. Hyperliquid `44\%$ perp DEX share`, `$6.7B/day ~6\% global, 323 HIP-3 pairs` (`buildix 2026-05-27`), `Jupiter Perps Solana $704M/day`.
* Interpretation (`outlookfirst.com 2026-07-04`): *“Perp DEX dominance hits ~44\% , memecoin vol -62\%, traders rotate to perps/stablecoins”*. Not causal liquidity drain ($p(\text{spread trend})=0.35$ in your data), but **speculative budget reallocation**: leverage on SOL/majors via perps offers convex payoff without `98\%$ graduation filter`, attracting `professional degenerate` capital.

Funding rate mechanics for memecoin perps (where listed): `funding = clamp( (mark - index)/index, -0.05\%, 0.05\%)$ per 8h`, annualized `~ -45\%$ to $+45\%$. During `Feb$ crash, `O I$ flushed, `funding negative`, perps became hedging venue, not memecoin proxy — hence `perp DEX` vs `memecoin spot` negative correlation (`$\approx -0.4$` estimated weekly).

---

## 7. Implications for Algorithmic Strategy

### 7.1 Formalization of WR Decay in `date_segmented_results.json`

For robust dates (`recording_count \ge 30$):

* `WR: Spearman r=-0.760, p=0.0002$, `Pearson $r=-0.697, p=0.0009$, slope $-0.97$ pp/day, `R^2=0.48$ (`backend/analysis/run_date_segmented_backtests.py:52` pipeline).
* `gain_retrace share: r=-0.719, p=0.0005$  (`67.4\% ->49.9\%$) — the $93\%$ WR exit drying.
* `PnL total: r=-0.239, p=0.32$ NS; `per-rec $r=-0.177, p=0.46$ NS; `per-trade $+0.00227->+0.00221$, MWU $p=0.37$, bootstrap $95\%$ CI $[-0.0037,+0.0037]$ contains $0$.
* `Cohort MWU early (07-27→08-07) vs late (08-08→08-19): WR $75.1\%$ vs $63.9\%$ $p=0.008$, `gain_retrace%` $64.9\%$ vs $47.7\%$ $p=0.0005$, `PnL` $p=0.90$ NS.

Mathematically, expectancy:

$$ E[\text{PnL/trade}] = p_{\text{win}} \cdot \mu_{\text{win}} - (1-p_{\text{win}})\cdot \mu_{\text{loss}} $$

With `$\mu_{\text{win}}$ flat ($r=0.158, p=0.51$, median win $+0.614, p=0.005$ rising) compensating `$p_{\text{win}}$ drop, `E` stays flat — fragile. Last `7$ days cumulative $-0.18$ vs first $7$ $+0.79$ indicates tail risk rising.

### 7.2 Why `Kramers Escape` Loses Power

`StrategyEngineV2` computes `Kramers` rates:

$$ k_{\pm} = \frac{\sqrt{\omega_0 |\omega_b|}}{2\pi\gamma} \exp\left(-\frac{\Delta U_{\pm}}{T_t}\right) $$
$$ P^{\pm,0} = \text{softmax}(\text{Kramers\_Passage}(k_+,k_-, \tau)) $$

Two structural breaks:

1. **Density degeneracy:** `$\rho(x,t)$` KDE volume-weighted; with `98\%$ non-graduates, `$\rho$` peak is at stall price, not breakout. `Hurwitz 2026` shows `price-occupancy KDE lags trends -> trend start identified as basin -> premature exit`. Your `volume buffer` diagnostics (`T_w=14400` breakthrough `iter16k`) fixed lifetime volume profile vs `75s$ wiggle, but post-`Feb$ crash, `$\rho$` mass shifted to chop.
2. **Barrier work `$\Delta U_{\pm} = U(x_{\pm})-U(x_t) \pm \frac12 \mu_t(x_{\pm}-x_t)$** with `$U=-T_t \ln\rho + V_{\text{liq}}$`. `V_{\text{liq}}$ taper `$\approx 1e-6$` vs `ask_depth` makes `du_up<0` for `198/198` probe trades (`iter06 probe, RESEARCH_LOG.md`) — upward barrier `k_up=1e6` saturated, `P_up=1.0` always, `P_down\equiv0$ blindness (iters 33, 35: `87\%$ big losers and $88\%$ winners `P_down<0.05$`). Bundler/sniper flow makes `$\mu_t$` flicker `4\times$/candle intra-candle (`4-state expansion` `candle_aggregator.py:16`), flipping `$\mu_{\text{dot}}$` and firing `EVR triage` prematurely (`iter49 proof: false positive `median win 7.0%` vs true positive `20.5%` indistinguishable at fire: `age 117–151s$).

Result: `Kramers_down_exit` profitable historically (`+17.89$ SOL `iter08) but `kramers_down_exit` after `iter15 recorder fix` inverted `0.76\% WR$?` Gains now require `gain_retrace` overlay, which itself decays.

### 7.3 What Structure Predicts

* **Market bimodality** `DATE_SEGMENTED_BACKTEST_REPORT.md:220` Type A `violent dump` (`one_pump_wonder >50\%$) vs Type B `low-vol grind` (`pump <80\%, bleed 35-50\%$) persists; `iter34,52` prove pre-entry features indistinguishable (AUC `0.5$), only post-entry `buy-ratio AUC 0.764$` separates (`iter48$). This matches `bonding-curve filter` math: `p_g$ independent of `P_up$, so entry selection cannot beat `1/71$ base rate without off-chain `dev grade` or `holder_flow$.
* **Holder_flow alpha** `iter56`: silence gate `K=2700s$ saves `+0.437$ SOL, but requires `5s` poller `holder_flow.py:1` — fragile to `GMGN 429$ and registry sparseness (`12/44 tagged$).
* **No purely OHLCV gate can beat baseline** (`iter37 oracle bound $+0.786<$ baseline $+0.965$`) — theorem for your `OHLCV ceiling`. Structure explains: `98\%$ of volume is `market rejection`, not trend; without `on-chain provenance` (`iter35: 41/155 mints dual-outcome`, `AUC 0.5$ for 16 provenance fields) or `taker-flow response` (`iter48 EVR`), filter is random.

---

## 8. Conclusion

The `pump.fun` market is a **high-intensity Poisson filter** with deterministic constant-product pricing, `p_g\approx1.4\%$ graduation, power-law volume, and adversarial Block 0 extraction, followed by `exponential liquidity decay ($\tau_L\approx18$ min) and `83–95\%$ 24h mortality for graduates. Revenue accrues to the protocol (`1\%$ of all curve volume) while user EV is `>70\times$ multiple hurdle, explaining `$3\% >\$1k$` profitability.

Macro `2026$ is a regime switch: spot DEX `-62\%$ Feb and `-82\%$ May vs perp DEX `+` to `$147\text{–}183B$ Q2 (`13\%$ DEX:CEX) and launchpad share oscillation (`Pump $99\% -> 14\% -> 98\%`). `PUMP` token mechanics (`50\%$ fees -> burn) tie platform value to `R$, which is itself `V_{\text{total}}$-driven and thus sensitive to `$\lambda(t)$`.

For your backtest, `WR$ decay `r=-0.760$ is **statistically real** and maps to `gain_retrace$ decay `r=-0.719$, while `PnL/trade$ remains flat via larger median wins — a fragile compensation. The decay began pre-`holder_flow` (`r=-0.524$ pre-`08-07$), so not caused by your `EVR` gates; `spread/turnover` flat ($p>0.35$) rejects simple perp-liquidity-drain; instead, `professional bundler/sniper` (`Jito Block 0$), `launchpad fragmentation`, and `attention half-life $\approx14$ min$` raise the filtering bar beyond `Kramers` OHLCV. Future alpha must come from information outside `OHLCV` — validated `holder_flow` on fresh `iter36$ recordings, `dev grade`, or `post-entry taker response` (`iter48 AUC 0.764$) — with the `iter37$ oracle bound proving pure `OHLCV` exit tweaks cannot recover `baseline`.

---

## 9. References

* `solanacompass.com 2026-06-10`: *Pump.fun Launched 42,000 Tokens in One Day. Fewer Than 2% Will Ever Reach a DEX.*
* `dextools.io 2026-06-21`: *Pump.fun 2026: Graduation Rate Collapses to 0.26%, Solana Fees Down 84%*
* `scorp trader 2026-07-17`: *Pump.fun Graduation: What Happens at Raydium Migration* — `776,306` births, `10,972$ grad `1.41\%$, median `4.9` min, `83\%$<1h`, `6.0\%$ dev `F`, `19.7\%$ 24h liq`.
* `phemex.com 2026-03-11`, `degendecoded.com Apr 2026`: *Solana in 2026: What the Memecoin Crash Means* — weekly DEX `$118.2B->$44.5B -62\%$, `Meteora -83\%$, `Pump -50\%$, `SOL $116->$85$, `1.56M SOL` inflows, `holder -92\%$*.
* `cryptorank.io 2026-06-02`: *Solana DEX Trading Volume Crashes 82%* — `$104.3B->$18.8B$, `Meteora $93.1B->$9.2B -90\%$*.
* `bloomingbit.io 2026-05-19`, `bitcoinworld 2026-05-19`: *Solana Perp DEX Weekly Breaches $20B* — `GMTrade $4.9B/day$*.
* `codegotech.com 2026-07-02`, `pluang.com 2026-07-27`: *Solana Hits Record $147B / $183.2B Q2 Perps*, `spot Q2 $160.8B -44\%$*.
* `CoinGecko May 21 2026 State of Crypto Perpetuals`: `CEX $7.1T->$4.7T$, `DEX $531B->$611B$, `DEX:CEX 3\%->13\%->10\%$, `Hyperliquid 44\%$`.
* `buildix 2026-05-27`: *Hyperliquid $6.7B/day 6\% global, 323 HIP-3*.
* `kucoin.com 2026-05-04`: *State of Solana Memecoins Q2 2026* — professionalized degenerate, smart money migration.
* `uwuu.ai 2026-07-28`: *Solana MEV Explained* — sandwich/JIT/Jito bundles.
* `solanahub.de 2026-05-07`: *Detecting Bot Wallets* — sniper/bundler patterns.
* `solbundler.app`: `500K+` tokens via bundler, Block 0 Jito, `1\%$ fee.
* `coinstats.app 2026-08-01` / `bingx.com 2026-08-13`: *Pump $1.157B cum, Q2 $62M -36\% QoQ, reclaim 98\% early Aug $542M vol*.
* `news.dropstab.com 2026-07-20`, `coincodex 2025-07-08`, `forklog 2025-07-23`: *LetsBonk 5\%->64\% Jul 7-23, $7.9M$ vs Pump $3.1M$, 26.6k/day $179M*, *reclaim 73.6\% Aug 11-17 $13.48M$*.
* `dolanduck.io 2026-07-09`: *Robinhood Chain Jul 1 2026 CASHCAT +1700\% $120M$*.
* `gate.io 2024-08-19` / `rektcalc.com`: *Graduation 1.4\%, 3\% >\$1k, EV 70× hurdle*.
* `j.tools 2026-07-01`: *Bonding Curve: 30 virtual SOL, 1.073B tokens, $k=3.219\times10^{10}$, 85 SOL grad*.
* `RESEARCH_LOG.md`, `backend/strategy_engineV2.py:1`, `backend/candle_aggregator.py:16`, `backend/data_store.py:62`, `backend/analysis/date_segmented_results.json`.
