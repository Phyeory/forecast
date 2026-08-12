## 0. Scope of this report (agreed narrowing)

The mission brief specified three tasks and named a set of stored artifacts as
cross-check inputs. **Those artifacts are not present in this checkout.**
`.gitignore` excludes `backend/analysis/*`, `backend/v2_results`, `*.db`, and
`logs/`, and no artifact from iter13 onward was ever committed to git.

Present and audited: all backend source, `AGENTS.md`, `RESEARCH_LOG.md`,
`strategyV2.md`, `trade_report.md`.
Absent and therefore **not** cross-checked: `iter31_baseline.json`,
`iter37_vs_iter31.json`, `iter37_pse.json`, all `backend/v2_results/*`
per-trade logs, `backend/data/price_data.db`, `logs/*`.

By explicit user decision the deliverable was narrowed to:

- **Task 2** — full diagnostic pass on the code (bugs, miscalibrations, dead code);
- **Task 3** — the single committed, mathematically justified fix.

The Task 1 per-rejection verdict table (which depends on artifact numbers) is
**out of scope by agreement**. However, one Task 1 hypothesis — item 1, "the
`P_down ≡ 0` blindness is a software miscalibration, not market truth" — is
answerable *purely from the code*, and it turned out to be the central finding.
It is adjudicated in §2 because it is a code defect, not a statistical claim.

This report is read-only. No production file was modified. No program was run.
Every numbered finding carries a `file:line` citation to code I actually read.

---

## 1. Executive summary

**The research log's central conclusion is FLAWED.**

The claim that "the engine sits at its OHLCV-data ceiling" and that the left
tail is "entry-selection error addressable only by information the engine does
not yet observe" rests on the premise that the engine's downward-escape
posterior `P_down` genuinely never fires on real dumps (iter33). It does not
fire — but **not because of market structure. It cannot fire, as a matter of
arithmetic, because of a barrier-identification defect in
`_barrier_find_kernel`.**

I prove in §2.1 (Theorem 1) that whenever the current log-price sits at or
below the lower edge of the KDE volume support — which is the *definition* of
the dead-coin dump the left tail is made of — the implementation forces

$$\frac{P_{\text{down}}}{P_{\text{up}}} \;\le\; \sqrt{\frac{\omega_{b,\text{down}}}{\omega_{b,\text{up}}}}\cdot\frac{\varepsilon_\rho}{\hat\rho(x_t)}\cdot e^{-\delta}, \qquad \varepsilon_\rho = 10^{-12}$$

which for realistic $\hat\rho(x_t)\in[10^{-3},10^{-2}]$ gives
$P_{\text{down}} \lesssim 10^{-9}$. The exit condition
`p_down >= 0.5` at `strategy_engineV2.py:3631` is therefore unreachable by
roughly **nine to ten orders of magnitude**, on exactly the trades that produce
the −2.72 SOL tail.

Worse, the same defect is sign-asymmetric: the up-side barrier is assigned a
**negative** height (`du_up < 0`, physically inadmissible in Kramers theory),
which *boosts* `k_up`. So during a monotone collapse the engine computes
$P_{\text{up}} \approx 1$, sets `direction = +1`, and passes its own entry gate
`P_up >= 0.62` (`strategy_engineV2.py:3721`) **by construction**. This is a
mechanical explanation for iter40's observation that the engine enters at
median variance-ratio 0.695 — it does not merely "catch falling knives", it is
*driven* to catch them, and then it is *blind* to the fall.

Consequently a whole family of rejections is void: every mechanism whose
decision variable was `P_down`, `direction`, or `E_star` was tested against a
posterior that was numerically pinned at zero on the relevant cohort. Those
experiments did not measure what they claimed to measure.

**The committed fix (§4): replace the boundary-artifact escape rate with the
regime-correct first-passage rate** — Kramers' barrier-limited rate on sides
that possess a genuine interior saddle, and the drifted-diffusion
(gambler's-ruin) first-passage probability on sides that are unconfined because
the KDE density there is floor-saturated. This is a bug fix plus the
mathematically correct limit for the regime the price is actually in, not a new
heuristic. Its first-order effect is at **entry** (`direction → 0` on monotone
dumps), which is where the log itself localises the tail — and which the
iter37 exit-only oracle bound never covered.

---

## 2. Code-quality findings

### 2.1 FINDING 1 (critical) — `_barrier_find_kernel` returns the grid boundary as a barrier, making `P_down` structurally zero

**Site:** `backend/strategy_engineV2.py:848–901`, consumed at `:1969`, `:2006–2007`, `:2063–2071`.

#### The construction

The potential is built at `strategy_engineV2.py:1913–1917`:

```
eps_rho = 1e-12
U_kde   = -T_t * np.log(np.maximum(rho, eps_rho))
```

with `rho` normalised to unit maximum at `:1862` (`rho = rho / rho_max`) and
$T_t = \tfrac12 e^{h_t} = \tfrac12\sigma_t^2$ at `:1888`.

Define the dimensionless potential $u(x) \equiv U(x)/T_t = -\ln\hat\rho(x)$.
Because $\hat\rho \in [\varepsilon_\rho, 1]$,

$$u(x) \in [0,\; -\ln 10^{-12}] = [0,\; 27.631].$$

The density is a volume-weighted, time-decayed Gaussian KDE over the trade
buffer (`_kde_eval_kernel`, `:723–780`). Critically, that kernel applies a
**hard** spatial cutoff at $\pm 4.3h$ (`:747–748`, `:768–775`): grid points
farther than $4.3h$ from *every* buffered trade receive `out[g] == 0.0`
exactly, not merely a small value. Those points are then floored to
$\varepsilon_\rho$, so $u = 27.631$ there — a flat, maximal plateau.

The plateau is large. The grid spans $\pm\,5\sigma_t\sqrt{T_w}$ with
$T_w = 14400\,\mathrm{s}$ (`:1837`, `DEFAULT_CONFIG:180`), i.e. a half-width of
$600\,\sigma_t$, whereas the bandwidth is Silverman on the visited prices only,
$h = 0.9\,s\,n^{-1/5}$ (`:1818`). The grid is therefore far wider than the KDE
support, and exact-zero density occupies most of it.

#### The defect

On a dead-coin dump the current price $x_t$ sits at or below the lower edge of
the visited support. Hence, locally:

- **down side** ($x < x_t$): $\hat\rho$ is monotone non-increasing, saturating at $\varepsilon_\rho$ ⟹ $u$ monotone non-decreasing to 27.631;
- **up side** ($x > x_t$): $\hat\rho$ is monotone non-decreasing toward the historical volume node ⟹ $u$ monotone non-increasing.

Now trace `_barrier_find_kernel`.

*Left (down) sweep,* `:886–896`. `U_down_max` is initialised to `-1e18` and the
loop records a running maximum. Since $U$ increases monotonically leftward,
every step satisfies `U_grid[i] > U_down_max` and updates `idx_down_peak`. Once
the floor plateau is reached, $U$ is exactly constant, so neither `>` (strict)
nor the `elif U_grid[i] < U_grid[i+1]` descent test fires; the loop runs to
`i = 0` **without ever breaking**. The returned `idx_down_peak` is the first
floor-plateau index and

$$U_{\text{down}} = 27.631\,T_t \quad(\text{the maximum attainable value}),\qquad \Delta u_{\text{down}} = 27.631 - u(x_t).$$

The code has selected, as the "barrier", **the density floor of unvisited
territory** — the single largest barrier the grid can express.

*Right (up) sweep,* `:865–881`. `U_up_max` is likewise initialised to `-1e18`,
so the very first probe `i = x_idx + 1` unconditionally satisfies
`U_grid[i] > U_up_max` and is recorded as the "peak". At `i = x_idx + 2` the
`elif` fires (U is descending) and the loop breaks. So `idx_up_peak = x_idx+1`
and, since $U$ is *decreasing* upward,

$$\Delta u_{\text{up}} = u(x_t + \Delta x) - u(x_t) = -\delta, \qquad \delta > 0.$$

**A negative barrier height.** In Kramers theory $\Delta U \ge 0$ by
definition of a saddle; a negative value is inadmissible and here it *inflates*
the escape rate by $e^{+\delta}$.

Note also that `_barrier_find_kernel` **never returns `None`** — it returns
plain integers, with fallbacks to `G-1` (`:881`) and `0` (`:898`). Therefore
the guards `if idx_up is not None` / `if idx_down is not None` at `:2000`,
`:2017–2018`, and `:2093–2094` are **always true** and are dead code. There is
no "no barrier" code path at all.

This directly contradicts the comment at `:1909–1911`, which asserts that the
iter16o work fixed exactly this:

> "…fixing the boundary-barrier artifact in `_barrier_find_kernel` (iter16o) —
> during crashes the down side has NO barrier (monotonic to grid edge) → open
> escape at the attempt rate → P_down can fire, without touching the wells."

**That fix is not present in the code.** The monotone-to-grid-edge case is
precisely the case that yields the *maximal* barrier, not an open escape. The
comment documents an intent that was never implemented, and the engine has been
run and its results interpreted ever since as if it had been.

#### Theorem 1 (structural suppression of `P_down`)

*Hypotheses.* (i) $U = -T_t\ln\hat\rho$ with $\hat\rho$ normalised to unit max
and floored at $\varepsilon_\rho$ (`:1862`, `:1913–1914`); (ii) $\hat\rho$ is
monotone non-increasing on $[x_t-\text{span},\,x_t]$ and monotone
non-decreasing on $[x_t,\,x_t+\Delta x]$ (the monotone-dump regime);
(iii) rates as implemented at `:2063–2071`; (iv) $\tau > 0$.

*Claim.*
$$P_{\text{down}} \;\le\; \frac{k_{\text{down}}}{k_{\text{up}}} \;=\; \sqrt{\frac{\omega_{b,\text{down}}}{\omega_{b,\text{up}}}}\;\cdot\;\frac{\varepsilon_\rho}{\hat\rho(x_t)}\;\cdot\;e^{-\delta}.$$

*Proof.* From `:2068–2071`,
$k_\pm = \frac{\sqrt{\omega_0^2\,\omega_{b\pm}}}{2\pi\gamma}\,e^{-\Delta u_\pm}$
(the code's `omega0_sq` $=U''=\omega_0^2$ and `omega_b` $=|U''|=\omega_b^2$, so
the radical is $\omega_0\omega_b$ — this part is correct per spec §5). The
common prefactor $\omega_0/(2\pi\gamma)$ cancels in the ratio:

$$\frac{k_{\text{down}}}{k_{\text{up}}} = \sqrt{\frac{\omega_{b,\text{down}}}{\omega_{b,\text{up}}}}\; e^{-(\Delta u_{\text{down}} - \Delta u_{\text{up}})}.$$

By hypothesis (ii) and the sweep trace above,
$\Delta u_{\text{down}} = -\ln\varepsilon_\rho + \ln\hat\rho(x_t)$ and
$\Delta u_{\text{up}} = -\delta$, so
$\Delta u_{\text{down}} - \Delta u_{\text{up}} = \ln\!\big(\hat\rho(x_t)/\varepsilon_\rho\big) + \delta$,
giving the stated identity. Finally
$P_{\text{down}} = \frac{k_{\text{down}}}{k_{\text{total}}}(1-e^{-k_{\text{total}}\tau}) \le \frac{k_{\text{down}}}{k_{\text{up}}}$
since $k_{\text{total}} \ge k_{\text{up}}$ and $(1-e^{-k_{\text{total}}\tau}) \le 1$. ∎

*Corollary 1.* $P_{\text{down}} \ge 0.5$ requires
$\hat\rho(x_t) \le 2\varepsilon_\rho\sqrt{\omega_{b,\text{down}}/\omega_{b,\text{up}}}\,e^{-\delta}$,
i.e. the current price's own normalised density must sit essentially *at* the
$10^{-12}$ floor. But the current tick is inserted into the KDE buffer with
decay weight $e^0 = 1$ (`add_trade`, `:1768–1772`; weight at `:762`), so
$\hat\rho(x_t)$ is bounded away from $\varepsilon_\rho$ by construction. For
realistic $\hat\rho(x_t)\in[10^{-3},10^{-2}]$ the bound gives
$P_{\text{down}}\lesssim 10^{-9}$.

*Corollary 2.* Under the same hypotheses $\Delta u_{\text{up}} < 0 \Rightarrow$
$e^{-\Delta u_{\text{up}}} > 1$, so `direction = +1` at `:2150` and the entry
gate `decision["P_up"] >= self._v2_p_up_min` (`:3721`, default 0.62) is passed
**on monotone dumps by construction**.

*Corollary 3.* Exit #5 `kramers_down_exit` (`:3631`) is unreachable on the
left-tail cohort. Exit #6 `bayesian_flip` (`:3641`) requires
`direction != 1 and E_star > 0`, but `:2158–2166` returns `E_star = -1e3`
whenever `direction == 0`, so exit #6 can only fire when the engine actively
wants the *opposite* trade — which by Corollary 2 it does not. **Both Bayesian
exits are dead on precisely the trades that need them**, leaving `kelly_flat`
(a fixed −40% / 60-tick rule, `:3687–3693`) as the sole backstop. The reported
attribution — `kelly_flat` on 44 of 63 BIG losers, "rides the bleed down, no
Bayesian exit fires" — is the exact signature this defect predicts.

*Scope caveat.* The $\varepsilon_\rho$ arithmetic and the $\pm 50$ clamp at
`:2065–2066` never bind here, since $\Delta u_{\text{down}} \le 27.631 < 50$;
the suppression is carried entirely by the exponential, not by the clamp.

#### Verdict on Task-1 hypothesis 1

**CONFIRMED.** The `P_down ≡ 0` blindness is a software miscalibration, not
market truth. iter33's framing of it as "a structural property of memecoin
crashes" is an artifact of this defect.

### 2.2 FINDING 2 (critical) — the potential is an equilibrium object applied to a manifestly non-equilibrium process

**Site:** `strategy_engineV2.py:1890–1917`.

$U = -T\ln\hat\rho$ inverts the Boltzmann relation $\rho \propto e^{-U/T}$,
which holds **only for the stationary measure of a time-homogeneous, ergodic
diffusion**. A launching-and-dying memecoin is neither stationary nor ergodic
over $T_w = 14400\,\mathrm{s}$.

The consequence is qualitative, not merely quantitative: *unvisited territory
is read as infinitely high potential*. But price has not visited lower prices
**yet** — that is a statement about the past trajectory, not about an energy
barrier. A trending process is guaranteed to move into low-$\hat\rho$ regions;
the estimator declares exactly that motion impossible. Finding 1 is the
mechanical expression of this modelling error.

This also explains, and vindicates, the otherwise puzzling iter16n result
recorded at `:1904–1907`: flipping to $U = +T\ln\rho$ "fixed crash-detection
but destroyed entry quality". Both signs are wrong, because a single static
density cannot serve as both the confinement landscape and the direction
detector. The regime-correct treatment in §4 resolves the two roles separately
and is the reason the fix does not have to choose a sign.

### 2.3 FINDING 3 (major) — `get_decision` selects the horizon that maximises long-Kelly utility, then the exit path reads `P_down` from that same maximiser

**Site:** `strategy_engineV2.py:2440–2461`; consumed by `_check_exit_v2` at `:3617–3619`.

```
for tau in taus:
    d = _kramers_escape_and_decision(..., tau=float(tau))
    if best is None or d["E_star"] > best["E_star"]:
        best = d
```

$\tau$ is swept over $\{5,10,15,20,25,30\}$ (`DEFAULT_CONFIG:181–183`) and the
argmax of `E_star` is returned. With `direction = +1`,

$$\mathcal{E}^*(\tau) = n^*\hat\mu_\tau - \tfrac12 (n^*)^2\sigma^2_\tau - \ldots, \qquad \hat\mu_\tau = P_{\text{up}}(\tau)d_{\text{up}} - P_{\text{down}}(\tau)d_{\text{down}}$$

so $\arg\max_\tau \mathcal{E}^*$ is biased toward the horizon at which
$P_{\text{up}}$ is largest **and $P_{\text{down}}$ smallest**. That same
dictionary is then handed to `_check_exit_v2`, whose exit #5 tests
`p_down >= 0.5` on it.

**The exit's decision variable is evaluated at the horizon chosen to minimise
it.** This is a second, independent suppression of `kramers_down_exit`,
logically distinct from Finding 1 and present even if Finding 1 were repaired.
An exit test must be evaluated on its own horizon (or on the worst case over
$\tau$), never on the entry objective's maximiser.

Selecting $\tau$ by $\arg\max \mathcal{E}^*$ is additionally an
optimistically-biased estimator of $\mathcal{E}^*$ itself: for any random
$\hat{\mathcal{E}}^*(\tau)$,
$\mathbb{E}[\max_\tau \hat{\mathcal{E}}^*] \ge \max_\tau \mathbb{E}[\hat{\mathcal{E}}^*]$
by Jensen. The Kelly positivity gate `E_star > 0` is therefore systematically
too permissive at entry.

### 2.4 FINDING 4 (major) — the fill model has no pool-depth dependence, so micro-cap exit slippage cannot be represented

**Site:** `backend/forward_tester.py:7–25`, `:157–159`, `:187–189`; `backend/backtester.py:92`, `:361–363`, `:596–597`.

The documented fill model is

```
fill_fraction = clamp(base_delay * size_penalty * slippage_penalty, 0.02, 0.98)
base_delay    = reference_fee / (reference_fee + total_fee)
slippage_penalty = 1 + slippage_pct / 100
```

Every term is a function of **fees and a configured `slippage_pct` constant**.
No term references pool reserves, market cap, or realised depth. `backtester`
passes `slippage_pct = 1.0` (`backtester.py:92`).

Two consequences bear on the tail:

1. Exit slippage on a liquidity-drained dead coin is modelled identically to
   exit slippage on a healthy pool. For the 63 BIG losers — by construction the
   trades where the pool is worst — the modelled 1% is a **lower bound of
   unknown looseness**, so the baseline tail is optimistic in an unquantified
   way.
2. `recording_ended` force-closes (`backtester.py:361–363`, `:596–597`) sell at
   the final candle close under the same depth-free model. On a tape that ended
   because the coin died, that price may not be attainable at any size.

I cannot quantify either effect: it requires `price_data.db` reserve columns
and the per-trade logs, both absent. **UNVERIFIED-BY-ARTIFACT.** Flagged as a
measurement-validity caveat on the −2.72 SOL figure, not as a claim that the
tail is an artifact.

Note also the default mismatch: `ForwardTester.__init__` defaults
`slippage_pct = 10.0` (`forward_tester.py:159`) while the backtest path passes
`1.0`. Any code path that constructs a `ForwardTester` without an explicit
`slippage_pct` runs a 10× different cost model. I did not trace every
construction site; worth a grep before the next batch.

### 2.5 FINDING 5 (moderate) — `_mu_neg_frac` divides by `maxlen`, not the populated length

**Site:** `strategy_engineV2.py:3684–3686`.

```
_mu_neg_frac = (self._mu_post_neg_count /
                self._mu_post_neg_window.maxlen
                if self._mu_post_neg_window.maxlen else 0.0)
```

Before the deque fills, the denominator exceeds the sample count, biasing the
fraction **downward**. With the production default `_no_long_mu_neg_frac = 0.0`
the comparison at `:3690` (`0.0 <= _mu_neg_frac`) is vacuously true, so this is
currently latent. It becomes a live bug for anyone re-testing the iter21
hypothesis-K guard, and it would silently bias that experiment toward firing
early in every trade. Since iter21-K was *rejected* partly on threshold
behaviour, the rejection is worth re-running after this is fixed.

### 2.6 FINDING 6 (minor) — dead code and stale computations in the hot path

All verified by reading:

| Site | Issue |
|---|---|
| `strategy_engineV2.py:1962` | `eps = cfg["sigma_floor"]` assigned, never used. |
| `:2045–2047` | `var_h` computed from `sigma_h`/`eta`, then `vol_corr_* = 1.0` unconditionally. Dead since iter16f. |
| `:1835` | `grid_half` computed, never used. |
| `:1882–1885` | `L_t = math.exp(0.0)` assigned then immediately superseded by `L_now`. Misleading dead line. |
| `:866`, `:885` | `idx_up_min` / `idx_down_min` computed by the sweeps but never returned — the local-minimum information the docstring promises is discarded. |
| `:2000`, `:2017–2018`, `:2093–2094` | `is not None` guards that can never be false (see §2.1). |
| `:1915–1917` | `U_up = U_down = U = U_kde`; the three-way split is vestigial, and `last_U_up`/`last_U_down` are set on the instance but never declared in `__init__` (`:1759–1766`). |

None of these change results today. They matter because `:2045` and
`:2000`/`:2017` are the exact places a reader would look to confirm that the
"open escape" path exists — and their presence makes the absent iter16o fix
look present.

### 2.7 Determinism and pipeline parity — assessed, not cleared

I read `engine_factory.py` (94 lines) and the three pipelines' engine-update
call sites. I found no RNG seeded from wall-clock, and the RBPF resampling uses
a passed-in `u` (`_systematic_resample_indices`, `:1205`). The 4-state
expansion and 1-bar delay are structurally present in `backtester.py`.

However, `AGENTS.md` documents that `live_trader` receives holder-flow events
from an asynchronous 1 s pump task, with iter39 and iter41 both recorded as
*parity fixes to that path*. A mechanism that required two successive
timing-parity corrections is one whose event-ordering is timing-dependent by
nature. Judging whether the three pipelines actually agree requires the
`*iter36_parity*` per-token logs, which are absent. **UNVERIFIED-BY-ARTIFACT.**
I neither confirm nor refute divergence.

---

## 3. Why this invalidates a family of rejections

Stated narrowly, as a logical consequence of Theorem 1 and Corollaries 2–3, and
not as a re-audit of any individual iteration's statistics (out of scope, §0):

- Any experiment whose treatment variable was `P_down`, or whose control flow
  passed through `kramers_down_exit` / `bayesian_flip`, was applied to a
  variable pinned at $\lesssim 10^{-9}$ on the left-tail cohort. A null result
  there is uninformative about the mechanism — it is a measurement of the
  defect. This covers iter33 directly and, by Corollary 3, constrains how any
  "Bayesian exits do not fire" observation may be interpreted.
- The iter37 oracle bound is a bound on **exit-timing mechanisms over the
  recorded OHLCV stream**. Corollary 2 places the dominant failure at
  **entry**: the gate admits monotone dumps by construction. An entry-side
  correction is outside the bound's hypotheses, so citing it against an
  entry-side fix is a category error — independently of whether the bound's own
  arithmetic is right, which I could not check (`iter37_pse.json` absent).
- iter40's "median entry VR 0.695 — catches falling knives by construction" is
  best read as a *symptom of Corollary 2*, not an independent property of the
  strategy. It is the observable the defect predicts.

I make no claim about iter22/26/30/31/32/34/35/38/42, whose verdicts require
the absent artifacts.

---

## 4. The fix

> One fix, committed. Report-only: no production file was modified. What
> follows specifies the change precisely enough to be applied and tested
> without further design work.

### 4.1 The defect addressed

Findings 1 and 2, with Finding 3 as a required companion correction. The
engine applies the barrier-limited Kramers rate to sides that have no barrier,
using the KDE density floor of never-visited territory as the barrier height
(`strategy_engineV2.py:848–901`, consumed `:2006–2007`, `:2063–2071`).

### 4.2 The mathematical argument

**Setup.** Let the posterior log-price obey, over a horizon $\tau$ short
relative to the KDE memory,

$$dx_s = \mu_t\,ds + \sigma_t\,dW_s, \qquad x_0 = x_t,$$

with $\mu_t,\sigma_t$ the RBPF/UKF posterior drift and volatility already
computed at `:1987` and `:2455`. Let $d_\pm > 0$ be the distances from $x_t$ to
the up/down decision levels (already computed at `:2093–2094`).

**Two regimes.** Kramers' formula

$$k_\pm = \frac{\omega_0\omega_{b\pm}}{2\pi\gamma}e^{-\Delta U_\pm/T}$$

is an asymptotic result for escape from a **metastable well over an interior
saddle**, valid only when $\Delta U_\pm/T \gg 1$ *and a saddle exists*. Its
hypotheses fail on a side where $U$ is monotone to the domain boundary: there is
no saddle, $\omega_b$ is undefined, and $\Delta U$ measured to the boundary is
an artifact of the domain, not of the dynamics. This is exactly the
monotone-dump geometry of §2.1.

On such a side the correct object is not a barrier rate but the **first-passage
probability of the drifted diffusion**. For $dx = \mu\,ds + \sigma\,dW$ started
at 0 with absorbing levels at $+d_{\text{up}}$ and $-d_{\text{down}}$, define
the Péclet numbers

$$\mathrm{Pe}_\pm \equiv \frac{2\mu_t d_\pm}{\sigma_t^2}.$$

Then the classical two-sided exit (gambler's-ruin) probabilities are

$$\boxed{\;P_{\text{down}} = \frac{e^{\mathrm{Pe}_{\text{up}}}-1}{e^{\mathrm{Pe}_{\text{up}}}-e^{-\mathrm{Pe}_{\text{down}}}},\qquad P_{\text{up}} = 1 - P_{\text{down}}\;}$$

with the driftless limit $P_{\text{down}} \to d_{\text{up}}/(d_{\text{up}}+d_{\text{down}})$
as $\mu_t\to 0$ (removable singularity; implement via the series expansion when
$|\mathrm{Pe}| < 10^{-8}$).

*Derivation.* $\phi(x) = \mathbb{P}(\text{hit } -d_{\text{down}} \text{ before } +d_{\text{up}} \mid x_0=x)$
solves the backward equation $\tfrac12\sigma^2\phi'' + \mu\phi' = 0$ with
$\phi(-d_{\text{down}})=1$, $\phi(d_{\text{up}})=0$. The general solution is
$\phi(x)=A+Be^{-2\mu x/\sigma^2}$; imposing the boundary conditions and
evaluating at $x=0$ gives the boxed expression. ∎

**Why this is the right correction and not a new heuristic.** Three properties:

1. **It is the exact $\Delta U \to 0$ limit.** On a floor-saturated side the
   density carries no geometric information, so the confining term must vanish;
   the surviving asymmetry is the drift. The formula is what the SDE at `:1987`
   already asserts — nothing is added to the model.
2. **It restores the correct dimensionless scale.** $\mathrm{Pe} = 2\mu d/\sigma^2$.
   Since $T_t = \tfrac12\sigma_t^2$ (`:1888`), the iter21 drift-work term
   $\tfrac12\mu\Delta x/T = \mu\Delta x/\sigma^2$ is *the same scale up to a
   factor 2*. This is diagnostic: iter21's `v2_drift_work_fraction` (`:1999`)
   had the right dimensional group but injected it as a multiplicative
   Boltzmann correction **on top of the bogus boundary barrier**, so each $\mu$
   sign flip re-ordered two rates that were themselves $10^{10}$ apart — the
   observed churn. Applied as the exit probability of the unconfined problem,
   the same group is bounded in $[0,1]$ and cannot produce that pathology. The
   iter21-B rejection is therefore evidence against the *implementation*, not
   against using drift.
3. **It is bounded and normalised by construction**, so it cannot reproduce the
   $e^{\pm 50}$ clamp regime that iter16b/16f fought.

**Sign structure at entry (the mechanism that cuts the tail).** On a monotone
dump $\mu_t < 0$, so $\mathrm{Pe}_\pm < 0$. Writing $a=|\mathrm{Pe}_{\text{up}}|$,
$b=|\mathrm{Pe}_{\text{down}}|$,

$$P_{\text{down}} = \frac{1-e^{-a}}{1-e^{-a}+e^{-a}(1-e^{-b})\cdot\!\big/\!\ldots} \;>\; \frac{d_{\text{up}}}{d_{\text{up}}+d_{\text{down}}}$$

i.e. $P_{\text{down}}$ strictly exceeds its driftless value whenever $\mu_t<0$,
and $P_{\text{down}} \to 1$ as $|\mu_t| \to \infty$. Compare Theorem 1, where
the same configuration yields $P_{\text{down}} \le 10^{-9}$. Hence:

- **Entry.** `direction` at `:2148–2153` becomes $-1$ (or 0) instead of $+1$
  on monotone dumps, and the gate `P_up >= 0.62` (`:3721`) **fails**. Corollary
  2 is repealed. This is the first-order effect.
- **Exit.** `kramers_down_exit` (`:3631`) becomes reachable: $P_{\text{down}} \ge 0.5$
  iff (driftless-symmetric case $d_{\text{up}} = d_{\text{down}}$) $\mu_t \le 0$.
  Corollary 3 is repealed.

**Assumptions, stated plainly.**

- (A1) Locally constant $\mu_t,\sigma_t$ over $\tau$. Standard, and $\tau \le 30\,$s
  against $T_w = 14400\,$s.
- (A2) The posterior $\mu_t$ is informative about the *sign* of near-term drift
  on the tail cohort. **This is the load-bearing assumption and it is
  conjectural.** iter21-K found `mu_neg_frac` could not separate genuine slides
  from winners' recovery dips at the `kelly_flat` moment. My fix does not
  require that separation — it requires only that $\mu_t<0$ at *entry* on
  persistent-drift paths, a strictly weaker condition, and the log's own
  loser/winner submersion split (0.98 vs 0.21) is consistent with it. But it is
  not proven, and §5 says how to falsify it.
- (A3) Gaussian diffusion with two absorbing levels. Jumps are not modelled;
  the engine's own jump component (`kappa_J`, `:166`) is outside this
  approximation, which is conservative for a *down*-side estimate.

What is **proven**: Theorem 1 and its corollaries — the current
`P_down` is structurally zero and the entry gate is passed by construction on
monotone dumps. What is **made plausible**: that repairing it blocks a large
share of the tail. What is **assumed**: (A2).

### 4.3 The concrete change

Four edits, all inside `strategy_engineV2.py`. Gate the whole thing behind one
new config key `v2_escape_model` (`0.0` = current behaviour, byte-identical
parity; `1.0` = regime-correct), added to `DEFAULT_CONFIG` at `:140–198`,
preserving the project's parity invariant.

**(a) `_barrier_find_kernel` (`:848–901`) — report absence of a saddle.**
Initialise the running maxima to the basin value `U_grid[x_idx]` rather than
`-1e18`, so a side that descends immediately records no peak. Treat
floor-saturated plateaus as information-free: stop the sweep when
`U_grid[i] >= U_floor - tol`, where `U_floor = -T_t*log(eps_rho)`. Return
`-1` for a side with no interior local maximum. Keep the signature; only the
sentinel is new.

**(b) `_kramers_escape_and_decision` (`:1969`, `:2000–2071`) — branch on regime.**
For each side independently:

- *interior saddle found* (`idx_± >= 0`): keep the existing Kramers rate, but
  clamp `du_± = max(du_±, 0.0)` (barrier heights are non-negative) and take
  $\omega_{b\pm}$ from an interior index, never a grid boundary — recall
  `_second_derivative_grid` returns exactly `0.0` at indices 0 and `G-1`
  (`:919`), which the `<= 0 → 1e-6` clamp at `:2019–2022` then silently
  converts into a spurious near-zero attempt frequency.
- *no interior saddle on either side*: bypass $k_\pm$ entirely and set
  $P_{\text{up}}, P_{\text{down}}$ from the boxed first-passage formula, with
  $P_{\text{zero}} = e^{-k_{\text{total}}\tau}$ replaced by the complementary
  no-exit mass $\mathbb{P}(\text{neither level hit by }\tau)$, obtained from the
  same diffusion. A defensible and cheap choice is the two-sided Chernoff
  bound $P_{\text{zero}} = \exp\!\big(-\tau\sigma_t^2/(2\min(d_+,d_-)^2)\big)$,
  then renormalise $(P_{\text{up}},P_{\text{down}})$ onto $1-P_{\text{zero}}$.
  Document it as an approximation; it affects only the $P_{\text{zero}}$
  comparison in `direction`, not the up/down ratio that drives the fix.
- *mixed* (saddle on one side only): use the first-passage form, with the
  saddled side's $d$ set to the saddle distance. The confinement then enters
  through $d$, which is its correct geometric role.

**(c) Finding 3 — decouple the exit horizon (`:2440–2461`, `:3617–3619`).**
Return, alongside `best`, the quantity
`P_down_max = max over tau of d["P_down"]`, and have `_check_exit_v2` read
**that** at `:3618` instead of `best["P_down"]`. An exit test must not be
evaluated at the maximiser of the entry objective. This is a strict
prerequisite: without it, (a)+(b) repair `P_down` but the exit still samples it
at its least favourable horizon.

**(d) Instrumentation (no behaviour change).** Emit, per entry and per exit,
`{du_up, du_down, idx_up, idx_down, saddle_up, saddle_down, rho_hat_xt, Pe_up, Pe_down, P_down, P_down_max}`
into the per-trade JSON. Finding 1 survived ~42 iterations because none of
these were logged; the next audit must not need a proof to see it.

### 4.4 Falsifiable test protocol (for a follow-up run — not executed here)

**Stage 0 — confirm Theorem 1 empirically before changing anything.**
With `v2_escape_model=0.0` and (d) only, re-run the canonical batch:

```
BACKTEST_RESULTS_DIR=backend/v2_results python run_iteration.py \
  --label iter43_instrumented --max-workers 8
```

Prediction: on BIG losers (`pnl <= -20%`), `max_over_trade(P_down) < 1e-6` in
≥95% of trades, and `saddle_down == False` at entry in the large majority.
**If `P_down` is routinely $O(10^{-1})$ on that cohort, Theorem 1's hypothesis
(ii) does not describe the data and the entire fix is refuted.** This stage is
cheap, requires no engine change, and is the single highest-value action
available.

**Stage 1 — parity.** `v2_escape_model=0.0` must reproduce
`iter31_baseline_1786096269` byte-identically on trade count, PnL, and exit
attribution. Non-parity means (a)–(c) leaked into the default path.

**Stage 2 — treatment.**

```
BACKTEST_RESULTS_DIR=backend/v2_results python run_iteration.py \
  --label iter43_escapefix --max-workers 8
python backend/analysis/paired_diff.py \
  --baseline iter31_baseline_full --candidate iter43_escapefix \
  --save iter43_vs_iter31
```

**Stage 3 — the pre-registered acceptance criterion.** Report both:

- the existing five gates (Wilcoxon one-sided $p<0.05$, bootstrap 95% CI $>0$, ≥50% token breadth), and
- a **tail criterion**, pre-registered here before any result is seen:
  paired per-trade CVaR at the 10% level, $\mathrm{CVaR}_{10\%}$, with a
  10,000-sample bootstrap 95% CI on the difference, plus total BIG-loser PnL
  and count.

I am explicitly **not** recommending that the tail criterion replace the five
gates, and I am not authorised to change the protocol (§6). Reporting both, on
a criterion fixed in advance, keeps the anti-overfit discipline intact while
making the drawdown effect visible if it exists.

**Decision rule.** Accept if total PnL is non-inferior (paired bootstrap CI
lower bound $> -0.10$ SOL) **and** BIG-loser total PnL improves by $\ge 0.8$ SOL
with a bootstrap CI excluding 0. Reject otherwise. Any regression must be
attributed per-token before a re-tune.

### 4.5 Expected effect magnitude, derived

Let $N_B = 63$ BIG losers totalling $L = -2.72$ SOL, and let $\beta$ be the
fraction whose entry state satisfies Theorem 1's hypothesis (ii) — no interior
down-saddle, $\mu_t < 0$. Those entries flip to `direction ∈ {0,-1}` and are
blocked (long-only), so the tail contribution becomes

$$L' = (1-\beta)\,L, \qquad \Delta_{\text{tail}} = -\beta L = +2.72\,\beta \text{ SOL}.$$

Against this, blocked winners cost $\Delta_{\text{win}} = -\beta_W W$ where
$W = +3.68$ SOL is the winners' gross and $\beta_W$ their block rate. Net:

$$\Delta \text{PnL} = 2.72\,\beta - 3.68\,\beta_W.$$

The discriminant is the trailing-submersion fraction, reported as median 0.98
for losers and 0.21 for winners — the sharpest separation in the entire
research history, and a *monotone proxy for hypothesis (ii)*, since submersion
near 1 means the path is at new lows, which is exactly "no interior
down-saddle". Taking $\beta \approx 0.8$ and $\beta_W \approx 0.2$ from that
split gives

$$\Delta \text{PnL} \approx 2.72(0.8) - 3.68(0.2) = +2.18 - 0.74 = +1.44 \text{ SOL},$$

against a baseline net of $+0.96$ SOL — i.e. roughly $2.5\times$ net PnL, with
BIG-loser PnL improving from $-2.72$ to $\approx -0.54$ SOL. **Break-even is
$\beta/\beta_W = 1.35$**; the submersion split implies a ratio near 4. This is
the quantity Stage 2 measures, and the $1.35$ ratio is the number that falsifies
the fix.

These figures inherit the baseline statistics from `RESEARCH_LOG.md` /
`trade_report.md`, which I could **not** cross-check against
`iter31_baseline.json` (absent, §0). $\beta$ and $\beta_W$ are estimated from a
reported median, not measured — Stage 0 measures them directly and should be
run before anyone relies on the point estimate. Treat $+1.44$ SOL as a
derivation from stated inputs, not a forecast.

---

## 5. What the fix depends on

1. **(A2), that posterior $\mu_t < 0$ at entry on persistent-drift paths.** The
   load-bearing conjecture. Falsified by Stage 0 if entry-time $\mu_t$ is
   sign-indeterminate on the BIG-loser cohort.
2. **Theorem 1's hypothesis (ii)** — that BIG-loser entries occur at or below
   the KDE support's lower edge. Directly measured by Stage 0 via
   `saddle_down` and `rho_hat_xt`.
3. **That $\beta/\beta_W > 1.35$.** Measured by Stage 2.
4. **That the baseline numbers are what the log says.** Requires
   `iter31_baseline.json` and `backend/v2_results/*iter31*`, both absent.
5. **That no lookahead/parity defect confounds the comparison** (§2.7,
   UNVERIFIED-BY-ARTIFACT). If `iter36_parity` logs show pipeline divergence,
   fix that first — a backtest-only improvement would not survive live.
6. **Dataset drift.** `AGENTS.md` notes recordings keep arriving; the 652-recording
   baseline is time-sensitive. Stages 1–2 must run on an identical
   `--recording-ids-file` snapshot.

---

## 6. Asks for the user (minimal)

1. **Restore the artifacts, or accept the audit's boundary.** The absent
   `backend/analysis/` iter13+ JSONs, `backend/v2_results/`, and
   `price_data.db` are the only way to close Findings 4 and 2.7 and to verify
   the baseline. Consider committing aggregate JSONs (they are small) or
   archiving them outside the ignore rules — a research log whose evidence is
   gitignored cannot be audited.
2. **Decide the acceptance protocol.** I recommend *reporting* the pre-registered
   paired $\mathrm{CVaR}_{10\%}$ tail criterion alongside the existing five
   gates, and I have deliberately not changed anything. Whether a tail metric
   may ever *override* a PnL gate is a risk-appetite decision that is yours,
   not mine, and it must be fixed before results are seen.
3. **Authorise Stage 0.** It changes no engine behaviour — instrumentation only
   — and it either confirms or destroys this entire diagnosis in one batch. It
   is the cheapest decisive experiment available.

---

## Appendix — evidence index

| Claim | Citation |
|---|---|
| `U = -T·ln(max(ρ,1e-12))`, ρ normalised to unit max | `strategy_engineV2.py:1862`, `:1913–1914` |
| $T_t = \tfrac12 e^{h_t}$ | `strategy_engineV2.py:1888` |
| KDE hard spatial cutoff at ±4.3h ⟹ exact zeros | `strategy_engineV2.py:747–748`, `:768–775` |
| Grid half-width $5\sigma_t\sqrt{T_w}$, $T_w=14400$ s | `strategy_engineV2.py:1837`; `DEFAULT_CONFIG:180` |
| Running max init `-1e18`; boundary fallbacks | `strategy_engineV2.py:867`, `:881`, `:886`, `:898` |
| Kernel never returns `None` ⟹ dead guards | `strategy_engineV2.py:900–901` vs `:2000`, `:2017–2018`, `:2093–2094` |
| iter16o "open escape" claimed but absent | comment `:1909–1911` vs code `:886–898` |
| Escape rates and ±50 clamp | `strategy_engineV2.py:2063–2071` |
| `d2 == 0.0` at grid boundaries; `<=0 → 1e-6` clamp | `strategy_engineV2.py:919`; `:2019–2022` |
| `direction` from $P$ comparison | `strategy_engineV2.py:2148–2153` |
| `E_star = -1e3` when `direction == 0` | `strategy_engineV2.py:2158–2166` |
| Entry gate `P_up >= v2_p_up_min` (0.62) | `strategy_engineV2.py:3721` |
| Exit #5 `p_down >= 0.5` | `strategy_engineV2.py:3631` |
| Exit #6 requires `direction != 1 and E_star > 0` | `strategy_engineV2.py:3641` |
| `kelly_flat` fixed-threshold backstop | `strategy_engineV2.py:3687–3693` |
| τ swept, argmax `E_star` returned | `strategy_engineV2.py:2440–2461` |
| Exit reads `P_down` from that argmax | `strategy_engineV2.py:3617–3619` |
| τ range {5..30} | `DEFAULT_CONFIG:181–183` |
| `v2_drift_work_fraction` default 0.0 | `strategy_engineV2.py:1999`; `DEFAULT_CONFIG:197` |
| Depth-free fill model | `forward_tester.py:7–25`, `:187–189` |
| Backtest `slippage_pct=1.0` vs FT default `10.0` | `backtester.py:92` vs `forward_tester.py:159` |
| `recording_ended` force-close at close price | `backtester.py:361–363`, `:596–597` |
| `_mu_neg_frac` divides by `maxlen` | `strategy_engineV2.py:3684–3686` |
| Dead `var_h` / `vol_corr` | `strategy_engineV2.py:2045–2047` |
| Current tick enters KDE at decay weight 1 | `strategy_engineV2.py:1768–1772`, `:762` |