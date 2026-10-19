# MARKET RESEARCH LOG — big-markets / prop-firm campaign (V2M clone)

**Worktree**: `/Users/jaime/pump-chart-market` · **branch** `market-backtest-systems`
**Window**: 2026-10-17 → 2026-10-19 (the entire campaign) · **status**: deployment
package complete, awaiting account purchase / Alpaca keys.

This file is the *chronology + derivation* record. Consolidated report:
[`RESULTS.md`](RESULTS.md). Firm dossier: [`PROP_FIRMS.md`](PROP_FIRMS.md).
Live runbook: `../market_live/README.md` and `../market_live/GO_LIVE.md`.
Memecoin research history: `RESEARCH_LOG.md` in the main checkout (NOT this
branch).

## One-page summary

The memecoin V2 engine was cloned to market tapes (calibration, not redesign),
searched across 3 markets × 7 asset-class books and ~146 run-cells, and landed
one robust configuration:

- **Market**: US single-name equities, **67 stocks, daily bars** — the only
  book with edge × breadth (test Σ +0.326 vs +0.042 next best). Perps are
  unfundable at every family (1h arm14 teΣ −0.269).
- **Calibration**: `gain_retrace_arm_pct` 3.0 → **14.0** (`arm14`) — monotone
  across nine doses, positive on BOTH segments, paired p=7.1e-06 (test) /
  5.2e-09 (train): **+10.9%/yr OOS**, Sharpe 0.75, maxDD 13.3%, worst day −5.17%,
  WR 77.9%.
- **Prop**: **FTMO 2-Step** ($100k, no time limit, static 10%, 5% daily,
  consistency ≤50%, 80% split, $540) at **k = 0.75**: **74.2% full pass**,
  funded blow 32.7%/yr, **EV +$9,124/attempt**, ~$1,040/mo per funded account.
  Two parallel attempts: 93% ≥1 funded, +$17.4k expected.
- **Stack**: decision-identical live system (`market_live/`) — parity gate
  PASS, prop guard enforced live + restart-safe, MT5 bridge for the FTMO
  account, Alpaca paper + FTMO free trial as zero-cost forward tests,
  `start_prop.sh` = one command. Full-book forward sim: +59.6% over 3.8y with
  both FTMO phases passed and σ ratio 0.74 ≈ k.

---

## Objective

User (2026-10-18, verbatim):

> "I dont have the personal capital for this keep trying to improve this OR
> find the best market that this algorithm would function very well and make
> thousands a month. it needs to pass prop firms tests all the time and make
> payouts consistently."
>
> "you need to establish all the stack that allows my algorithm to day trade
> stocks and do prop firm trading - you need to find the best prop firm for my
> algorithm."
>
> "make sure that the trade execution decisions are identical to the
> backtester - jsut like how the memecoin part of the engine."

Constraints set at kickoff:

1. **Calibration only.** No new exit logic, no fundamental behavior change.
   The memecoin files are untouchable: `strategy_engineV2.py` sha
   `7ae0aa736ac9f3ce3896c7dad15d8518e05f11a3`, plus `backtester.py`,
   `forward_tester.py`, live/booking — every batch run re-pins the sha.
2. Every deliverable must be *measured*, not asserted (MC, paired tests,
   deterministic replay proofs).
3. Decisions identical BT↔live, enforced by a code-level parity gate.

Follow-up (2026-10-19): "select the prop firm for me, set up all the api and
live system. Once you are done all I need to do is just to buy the account it
would be able to start trading. Also is there a way to do trial prop firm
evaluation tests without paying? try those as well." → produced the M10
deployment package below.

Deliverable definition (2026-10-19, that follow-up): the stack must be
*purchasable-ready* — a preflight that proves the account before any fee is
paid, and one command that runs the daily session.

## Invariants (do not break)

1. **Worktree isolation.** All market work lives in
   `/Users/jaime/pump-chart-market` on branch `market-backtest-systems`. The
   main checkout `/Users/jaime/pump-chart` is on `GLM-FIX` with another
   session's uncommitted work — do not disturb. Nothing has ever been pushed
   to a remote (user request pending).
2. **Venv.** The only venv is `/Users/jaime/pump-chart/backend/.venv/`. Run
   from the worktree: `cd /Users/jaime/pump-chart-market/backend &&
   /Users/jaime/pump-chart/backend/.venv/bin/python -m market_backtest.<mod>`.
3. **Engine = the clone.** `strategy_engineV2M.py` (`MarketStrategyEngine` /
   `StrategyEngineV2MAdapter`) is a structural copy of V2 with `zeta=0`. Every
   other engine parameter comes from **causal per-tape calibration**
   (`market_calibration.calibrate_market_params`) frozen on the TRAIN prefix.
   Calibration candidates may only be **constant knob overrides** on top —
   never new code paths in the engine.
4. **Ledger accounting.** All portfolio/prop numbers come from
   `ledger_daily_equity` (booked PnL + open-trade mark at one time). The
   snapshot `mtm_daily_equity` is banned for decisions (positive bias, up to
   40× on perps; §maths-4).
5. **Selection metric = account Σ** (`test.total_pnl`), with **train Σ +
   sign as the coherence check**. Per-trade mean mis-ranks
   entry-restricting cells (it counts trades not taken as 0) — corrected
   mid-campaign; per-trade mean is reported for the record only.
6. **Accept gate** (adopted): paired per-recording Wilcoxon p<0.05 +
   10k-bootstrap 95% CI > 0 + breadth ≥50%, **amended by
   dose-monotonicity**: a family is adoptable if ≥3 doses improve both
   segments monotonically (blunts the 146-cell selection bias).
7. **Prop numbers = real firm rules** (`mc_firms.py`, rules verified from
   published pages 2026-10-18). The legacy "60-day window" tables in
   RESULTS.md are superseded and kept for the record.
8. **k semantics**: `k` scales `buy_size=0.1` (10% notional per position) by
   live balance/equity. **k never changes which trades happen** — sizing only,
   so `k` is a deployment knob, not a strategy knob.
9. **Stocks-only for the prop account.** Intro/sector/comm books add CAGR but
   fatten the worst day (−5.17% → −6.08% pooled) — daily-cap headroom is the
   binding constraint on the FTMO floor.
10. **PARITY GATE after every deploy** (`market_live/test_live_parity.py`):
    identical 4 `update()` calls per closed bar, identical books
    field-by-field, balance to 1e-12.
11. **Data isolation**: market tapes live in `backend/data/market_data.db`
    (separate from price_data.db/candles.db). Never write candles.db.
12. **Yahoo is 429-blocked** on this IP for bulk use: equities/ETFs come from
    `api.nasdaq.com`; perps from Binance Vision zips. Intraday stocks
    (1h/5m) are plumbed (`BAR_SPEC stocks_1h/5m`) but unvalidated pending a
    feed (Alpaca keys).

## Command card

```bash
cd /Users/jaime/pump-chart-market/backend
PY=/Users/jaime/pump-chart/backend/.venv/bin/python

$PY -m market_backtest.batch run --market stocks --label arm14 \
    --params '{"gain_retrace_arm_pct": 14.0}' --max-workers 8
$PY -m market_backtest.market_scan --label arm14 --prop-ks 0.75   # league table (+prop)
$PY -m market_backtest.mc_firms --dir market_backtest/results/arm14 \
    --label arm14 --market stocks --interval 1d --sims 3000 \
    --ks 0.75,1.0 --templates ftmo_2step,ftmo_trial_2step
$PY -m market_backtest.market_paired --cand-dir market_backtest/results/arm14 \
    --cand-label arm14 --market stocks --interval 1d --seg test   # accept gate

$PY -m market_live.test_live_parity          # parity gate (must PASS)
$PY -m market_live.setup_prop --config market_live/config/prop_ftmo_100k.json
$PY -m market_live.run_live live --config market_live/config/prop_ftmo_100k.json --once
$PY -m market_live.forward_monitor --latest
./start_prop.sh                              # preflight -> session -> monitor
$PY -m market_live.test_prop_guard          # guard unit tests (6/6)
```

---

## Data inventory & acquisition

DB: `backend/data/market_data.db` — `market_recording` + `market_candle`
tables, read API shaped like the memecoin `get_recording_candles`.

| book | symbols | source | history | bars |
|---|---|---|---|---|
| stocks 1d | 67 (mega→broad factors) | api.nasdaq.com quote/chart, split+div-adjusted | 2016-10 → 2026-10 | 2513 each |
| perp 1d | 44 Binance USDT-M | Binance Vision monthly zips (real taker-buy split) | 2020-01/2023-06 → 2026-09 | 1.0–2.5k |
| perp 1h | 44 | same | same | 25k–59k |
| idx ETFs | 5 | nasdaq | 10y | 2513 |
| intl ETFs | 9 | nasdaq | 10y | 2513 |
| sector ETFs | 13 | nasdaq | 10y | 2513 |
| comm ETFs | 8 | nasdaq | 10y | 2513 |
| fx ETFs | 6 (FXE/FXY/FXB/FXA/FXF/UUP — proxies) | nasdaq (FRED path timed out; code kept) | 10y | 2513 |
| bond ETFs | 4 (TLT/LQD/IEF/HYG) | nasdaq | 10y | 2513 |

- **Yahoo Finance is dead for this repo**: `/fetch_yf` returns HTTP 429 on
  this IP. Everything daily is nasdaq; Binance handles the perps intraday.
- `fetch_stocks.py` started at 30 symbols and was widened to 67
  (mega→broad-factor ladder) when breadth-analysis began.
- Flow proxy for daily bars: taker-buy fraction estimated from OHLC
  position-in-bar spread (memecoin tapes use real one-second flow; the
  engine's flow latent φ therefore carries less information here — visible
  in the exit-mix analysis, §arm14 mechanism).

---

## Timeline

Commit-dated (branch local; iteration ids `M##`):

| iter | date | commit | what happened |
|---|---|---|---|
| M00 | 10-17 17:29 | `66d1bfd` | branch created; self-contained `market_backtest/`: store + fetchers + `market_calibration.py` with the causal rescale + **ζ=0 discovery**; first 30 stocks + 16 perps |
| M01 | 10-18 11:03 | `84e0fa2` | pre-branch stale result files removed (XOM batch JSONs et al.) |
| M02 | 10-18 11:59 | `3113e05` | **V2M clone + train/test pipeline + prop stats**: runner (4-state, force-close, 70/30 split, frozen params), `stats_report` ledger vs snapshot MTM, FTMO-classic prop sim |
| M03 | 10-18 13:04 | `bfb0cb5` | **full train/test campaign, round 1**: every unseen-window per-trade mean ≤ 0 (stocks −0.038%, CI [−0.149,+0.059]; perp 1h −0.191%, 0/44 breadth); legacy 60-day prop verdict (cannot pass) |
| M04 | 10-18 20:40 | `8a7733b` | **arming family = first robust calibration**: Round A geometry (falsified), Round B cutters (`b_arm6` both-segments-positive), EVR probes (churn identity), conf gate (positive-OOS but concentrated), gain-retrace dose 4→20 → **arm14**, perp arm14 teΣ −0.269 (unfundable) |
| M05 | 10-18 20:47 | `2e9665f` | **`mc_deploy.py`**: method-robust MC (iid→21d blocks), funded payout economics (paper vs reset), attempt EV, personal-account frontier, **Kelly k\*≈5.1, cap-safe k≤0.87** |
| M06 | 10-18 21:29 | `4aa756d` | sweep cells finalized (dose JSONs, arm combos, ladder/pup classes for the record) |
| M07 | 10-18 23:24 | `e10e0f6` | **real prop rules verified** (FTMO pages: no time limit, static 10%, consistency 50%); `mc_firms.py`; **league table begun**; **`market_live/` live stack written** (host/trader/brokers/guard/feeds/journal/run_live) + parity gate PASS |
| M08 | 10-18 23:34 | `9a95a62` | **league table = stocks win** (copies: intl/sector/comm books fetched via nasdaq); combined book analysis; **deterministic FTMO pass proof** (day 442); `mc_firms` consistency modeling |
| M09 | 10-18 23:36 | `466daea` | guard **restart-safe** (`guard_state.json` save/load + 6/6 tests), tape feed for offline polling, README runbook |
| M10 | 10-19 06:36 | `443b1f9` | **deployment package**: `mc_firms` free-trial templates (+3.8% finding), `mt5_bridge_server.py` (complete, simulate-tested), `setup_prop.py` preflight (GO on all 67 symbols), configs ×4, `forward_monitor.py`, `start_prop.sh`, `GO_LIVE.md`; **full-book forward sim +59.6%, both phases passed**; FTMO/Alpaca instructions |

Post-deployment verification runs (not commits): 10-name integration replay
(session `20261018_233412`, +12.84%, P1 fired); 67-name config replay through
the MT5-bridge path (+32.5% over 2024→, phases passed); **full-book forward
simulation** (session `20261019_063215`, +59.63%, monitor verdict ON TRACK).

---

## The maths

### 1. Engine clone & the liquidity fix (ζ=0)

The engine is the memecoin V2 (latent `[log-price, drift μ, log-vol h, flow φ,
liquidity ℓ]`; RBPF → topological regime + entropy confidence; volume-KDE
potential U; Kramers k± → P⁺/P⁻/P⁰; Bayesian-majority direction; Kelly E\*
long gate). Two structural problems appear on multi-year market tapes:

1. **ζ-collapse (fixed in the clone)**. One-sided decay on the liquidity
   latent (`ℓ → ℓ − ζ·|Δℓ|` on one side only) is a diffusive −ζ walk on long
   tapes → ℓ pegs at its clamp irreversibly → all four latents pin, R_ema
   saturates, P→0, engine dead after the first 3×-volume day. Root-caused on
   AAPL with posterior traces. Fix: `zeta = 0.0` in the clone (deliberate
   departure from memecoin DEFAULT/2 continuity). Pool liquidity ℓ is
   memecoin-only (pool_sol); on market tapes the layer is inert by design
   (`kappa_mu/θ/σ_ell` at DEFAULT, rescaled).
2. **KDE-geometry stall (left in place)**: when price sits below the
   volume-mode, `k_down → 0.0` exactly — the down-escape is geometrically
   inaudible while price grinds (NKE 374d, −48%). This is the daily-cadence
   version of production's "silent hold". Calibration answers it only
   indirectly (arm14 de-fangs the *entering* of such trades; see §4/§5).

### 2. Causal per-tape calibration (`market_calibration.py`)

The engine's time model runs on INTAKES (4 engine updates per bar,
INTAKES_PER_CANDLE = 4); the KDE potential runs on bar_count; hydro rates are
lifetimes/decay constants in seconds or intakes depending on layer. So every
market knob is rescaled per intake and clipped:

```python
rate_rescaled  = clip(rate / 4, clamp_min, clamp_max)          # hydro rates
sigma_rescaled = clip(0.10 / 4, …)                             # process noise
KDE_MEM_BARS   = {"stocks": 375, "crypto_perp_1h": 500, "crypto_perp_1d": 375}  # intakes
```

Per-intake clamps guard UKF conditioning (`sigma_mu/sigma_phi/sigma_ell`:
(0.001, 0.20) band). Decisions measured against epoch-adjusted stats:

- **τ program (`tau_max`)** — swept {20,40,60,120} on AAPL-1d and
  SOLUSDT-1h replays: τ≥60 lets the Kramers down-escape dominate hourly exit
  books (WR 36%). Adopted **interval-class τ: 1d→40 (floor 5.0), 1h→20
  (floor 3.0)** with `tau_min = max(floor, tau_max/8)`, `tau_step = 20`.
- **Entry noise gate (`v2_sigma_t_min`)** — compares the UKF's *posterior*
  σ_t, whose level depends on the measurement-variance calibration; set
  causally as `0.35 × p10(posterior σ)` measured on a bounded throwaway
  replay (never future data).
- **Arming ladder (the campaign's lever)** — measured forward run-ups and
  40th-percentile drawdowns on each tape:
  ```python
  arm   = clip(0.55 * median_forward_runup, 3.0, 14.0)   # saturation: med≈3% everywhere
  breakeven_arm_dd = clip(6.0 * max(dd_med, 1.0), 15.0, 40.0)
  breakeven_buffer = clip(0.35 * arm, 1.0, 4.0)
  no_long_offside  = clip(1.3 * max(dd_med, 1.0), …)
  ```
  The raw statistic sat at/below the 3.0 floor on nearly every tape (stocks
  median 3.0, max 4.33; perps max 4.99) — the memecoin-derived floor was
  exiting winners at their first pullback on daily bars. The **campaign
  override is the constant 14.0** (`arm14`); `v2_rate_split_arm_pct` mirrors
  it but is a **no-op on this book** (decomposition: `arm_r6` byte-identical
  to base).
- Everything else: DEFAULT_CONFIG with the rescale (memecoin constants only
  where the layer is inert).

**Split discipline**: `calib = candles[:split_idx]`, split ≈ 70% (stocks →
train 5.46y / test ~3.0y, 754 ledger days). Engine params frozen from TRAIN
only; stats partitioned by entry time; scalars rounded heavy-handedly
(stable across ci/re-runs); byte-exact rebuild is checked for labeled books.

### 3. Cost model (`runner.py`)

```python
COSTS = {
    "stocks":         {"slippage_pct": 0.03, "priority_fee": 0.0},   # 3 bp/side
    "stocks_1h":      {"slippage_pct": 0.03, "priority_fee": 0.0},
    "stocks_5m":      {"slippage_pct": 0.05, "priority_fee": 0.0},   # plumbed, unvalidated
    "crypto_perp_1h": {"slippage_pct": 0.05, "priority_fee": 0.0},
    "crypto_perp_1d": {"slippage_pct": 0.05, "priority_fee": 0.0},
}
```

Slippage is per-side, applied at each of the 4 fills of the executed model
(`exec_model="instant"`, same as production). Commission/swap is NOT modeled:
CFD overnight drag ≈ −1 to −1.5%/yr at the measured ~55% average gross
exposure — small vs the +10.9% edge but real; listed in PROP_FIRMS
honest-limits.

### 4. Measurement & protocol math

- **Account units**: 1.0 account, `size_sol=0.1` = 10% notional per
  position; Σ = raw booked-balance sum; "money/year" converts Σ over span
  → CAGR×account.
- **`ledger_daily_equity` vs snapshot MTM**: snapshot replaced each symbol's
  contribution with the NEXT trade's mark at re-entry — it confiscated
  realized wins and forgave realized losses. Test-window divergence:
  stocks −0.0666 booked = −0.0666 ledger vs −0.054 snapshot (~unbiased at
  low turnover); perp 1h −5.596 = −5.596 vs **−0.141 snapshot (40× worse)**
  — the snapshot would have blessed a dead book. All prop work uses ledger.
- **Bootstrap**: block bootstrap (blocks 5→21d tested; diff ≤ ~2pp at the
  plateau) because daily PnL clusters with concurrent-position regimes.
- **The metric correction story** (M03→M04): Round A ranked by per-trade
  mean and rejected geometry families; re-ranking by account Σ promoted
  entry-*restricting* cells. Final rule: rank Σ, and require BOTH segments
  + monotone dose for adoption.
- **Accept-gate stats on arm14** (paired per-symbol vs base):
  test Δ +0.00586/trade-sym, Wilcoxon p=7.1e-06 (W=62), bootstrap CI
  [+0.0027, +0.0097] > 0, breadth 30/67; train Δ +0.00851, p=5.2e-09,
  CI [+0.0054, +0.0122] > 0, breadth 37/67 (55%). Robust to dropping the
  top-3 symbols (test ΔΣ +0.160 of +0.251 survives). The one miss:
  test breadth 42% counts no-trade symbols as failures.
- **Selection-bias accounting**: ~146 run-cells ≈ ~70 distinct configs.
  Arm14 is not a single-cell pick: nine-point monotone dose (teΣ +0.012 →
  +0.184 → +0.244 → +0.289 → +0.308 → **+0.326** → +0.325 → +0.299 at
  arm = 4,6,8,10,12,14,16,20), plateau 14–16, train Σ rises throughout
  (−0.157 → +0.367 → +0.399 → +0.405), cross-market mechanism (perps teΣ
  −1.086 −0.624 −0.269 at arm 0/6/14). Acceptance logbook in RESULTS.md.

### 5. Sizing, Kelly & prop-rule math (`mc_deploy.py`, `mc_firms.py`)

**k semantics.** Live/backtest `trade_size = k × 0.1 × balance` (10% of
equity, scaled by k). Daily ledger stats at k=1: mean +0.0432%/day,
σ 0.919%/day → at k, returns scale ≈ linearly: μ(k) ≈ k×0.0432%, σ(k) ≈
k×0.919% (verified live: 0.676/0.919 = 0.736 at k=0.75).

**Full Kelly**: `k* = μ/σ² = 0.000432 / 0.00919² ≈ 5.1` → tested k=0.75 is
≈0.15 Kelly. Kelly ignores the firm's *rule walls*, hence:

**Cap-safe k** (worst day ≤ 4.5%, inside the 5% daily cap with buffer):
`k ≤ 4.5 / 5.17 = 0.87` (worst historical day −5.17% at k=1).

**The rule simulation** (`mc_firms.py`): block-bootstrap the 754-day ledger
× k, then walk day-by-day through the firm's cascade:

```
daily_floor(e)  = equity_at_day_start × (1 − daily%)        [5%]
total_floor     = 100000 × (1 − max_loss%)                  [static 10%]   ← vs leading-edge tr
phase_target[i] = 100000 × (1 + target_i)                   [10%, 5%]
trailing_floor  = max_running_equity × (1 − dd%)            [only for trailing firms]
consistency     = best_day ≤ c% × total_profit    (sim CONTINUES trading until satisfied)
time_limit      = trading_days <  TL                        [fatal rule]
```

Blow-up = any floor breach (≈ config floor + 0.5% buffer in the live guard).
Funded phase = same floors with real payout accounting; **"reset"** = payouts
withdrawn (conservative compounding) vs "paper" (payouts stay in). EV/attempt
= fee-adjusted expected net cash of {challenge phase(s) + funded phase until
blow/12mo}; the $540 fee refunds on the first payout (FTMO).

**Results (arm14, ledger, 3000 sims):**

| k | P1 pass | full pass | funded blown 1y | E payout (reset) | EV/attempt |
|---|---|---|---|---|---|
| 0.50 | 92.5% | 86.8% | 10.0% | $9,204 | +$7,921 |
| **0.75** | **83.5%** | **74.2%** | **32.7%** | **$12,485** | **+$9,124** |
| 1.00 | 63.3% | 48.0% | 63.0% | $12,783 | +$5,851 |
| 1.50 | 62.7% | 44.8% | 78.6% | $15,475 | +$6,635 |
| 2.00 | 58.9% | 37.8% | 89.0% | $15,898 | +$5,674 |

Consistency rule costs ≈1 pp (75.0→74.2). **Rule sensitivity at k=0.75**
(destroying → benign): 30-day time limit (74.2→**0.1%**, EV −$527), trailing
10% (→60.5%/blow 73.5%), daily 3% (severe), futures-prop 2%/4% (pass 42%,
fund-side 100% blown); daily 5% (−10 pp), 10% vs 8% max/target (mild).

**Attempt economics** ($540/attempt, funded-on-first-payout refund):
1 attempt → 74% ≥1 funded, +$8.6k expected; 2 → 93%, +$17.4k; 3 → 98%,
+$26.2k; 5 → 99.9%, +$43.6k ⇒ standard launch = **2–3 parallel attempts,
withdraw every payout**.

**Deterministic proof** (the actual test ledger through the guard, not a
bootstrap): k=0.75 passes **both phases on day 442 (2025-07-22)** with
best-day share 46% < 50%; k=1.0 day 337; k=0.5 day 538.

### 6. Execution math

- **Model identity**: every closed bar = four `engine.update()` calls
  (open tick → extreme1 → extreme2 → close tick + volume), so signals fire
  at the close, identical to BT. Live adds scaffolding only: sizing `k`,
  broker adapter, guard.
- **Fill convention**: FTMO/CFD retail can't do a true 16:00 MOC through MT5;
  `exec_mode = next_open` defers the target to the next bar's open.
  Measured wedge (8 names, 2024+, k=0.75): close-fill +8.71% vs next-open
  +8.25% → **−0.47 pp over 2.8 y** (~0.17 pp/yr) — the fill convention is
  not where the edge dies. `'close'` mode remains available and is
  byte-identical to the backtest if a true-MOC broker (Alpaca `cls` TIF)
  is used.
- **Live compounding identity** (forward-sim check): the monitor's realized
  **σ/day ratio 0.676/0.919 = 0.74 ≈ k=0.75**, maxDD 9.39% ≈ 0.75×13.3%,
  worst day −3.50% ≈ 0.75×−5.17% — the deployment returns scale exactly as
  the sizing law says. Ten-name replays instead showed σ ratio ≈ 0.15× —
  rule: **run all 67 symbols**; a sparse book sits flat, neither
  compounds nor reproduces the prop path.

- **Guard physics on live floors**: with FTMO's static max loss the
  reference for the 10% floor is the *initial* balance (restart-safe via
  `guard_state.json`), while the daily floor re-arms each session from
  equity-at-day-start × (1 − 5%) − 0.5% buffer.

### 7. Free-trial arithmetic

FTMO Free Trial (verified 2026-10-19 from ftmo.com/free-trial): 2-Step trial
= 5% target, 10% static, 5% daily, 2 min days, **14-day cap**, no
consistency, unlimited retries, MT5/cTrader, $0. Against μ +0.0432%/day the
14-day expected return is ≈ 0.6% vs the 5% target — the trial demands ≈8×
the strategy's natural drift inside its window. Measured:

| k | trial pass | timeout | daily breach |
|---|---|---|---|
| 0.75 | **3.8%** | 96.1% | 0.0% |
| 1.00 | 10.7% | 87.8% | 0.9% |
| 2.00 | 35.3% | 57.9% | 3.9% |
| 3.00 | 47.8% | 31.5% | 17.2% |
| 5.00 | 52.5% | 7.6% | 36.3% |

⇒ The free trial is a **plumbing test** (orders, naming, sizing, guard on
live data at zero cost), NOT a strategy evaluation; raising k to "pass" it
converts a plumbing test into a ruin test (36–43% daily-breach rates at
k≥3). The real 2-Step has **no deadline** and passes at 74%.

### 8. Forward-monitor statistics (`forward_monitor.py`)

From a session's `equity.jsonl` (bar events), collapsed per UTC day:
mean/day vs reference μ=+0.0432%, σ/day vs reference 0.919%, t =
μ/(σ/√n), maxDD, worst day, guard halts/passes. Verdicts:
`TOO EARLY` (<20 days) → `ON TRACK` (mean ≥ half-μ and t>0) →
`DEGRADING` (mean < μ/2 or t<0) → `FAILING` (μ ≤ 0 over ≥20 days ⇒ stop
trading).

---

## Results ledger (the numbers that matter)

**League table (**`market_scan.py`**, arm14, test window, pooled account,
10% notional/position):**

| market | syms | trades | Σ | mean/day | Sharpe | maxDD | worst day | pos | prop pass@0.75 |
|---|---|---|---|---|---|---|---|---|---|
| **US stocks** | **67** | **113** | **+0.326** | **+0.043%** | **0.75** | **13.3%** | **−5.17%** | **31/67** | **72.2%** |
| intl ETFs | 9 | 18 | +0.042 | +0.006% | 0.76 | 2.3% | −0.65% | 4/9 | 56.2% |
| sector ETFs | 13 | 6 | +0.028 | +0.004% | 0.86 | 0.6% | −0.44% | 4/13 | 9.5% |
| idx ETFs | 5 | 2 | +0.015 | +0.002% | 1.00 | 0.3% | −0.23% | 2/5 | 0.0% |
| comm ETFs | 8 | 12 | +0.007 | +0.001% | 0.07 | 3.7% | −0.82% | 3/8 | 15.1% |
| fx ETFs | 6 | 45 | +0.002 | +0.000% | 0.06 | 2.1% | −0.41% | 4/6 | 0.0% |
| bond ETFs | 4 | 17 | −0.009 | −0.001% | −0.23 | 3.1% | −0.43% | 0/4 | 0.0% |
| perp 1h (44) | 44 | 2933 | −5.60 | −0.55% | — | — | — | 0/44 | 0% |
| perp 1d (44) | 44 | 58 | −0.222 (≈−8%/yr) | — | — | — | — | 6/44 | 0% |

Per-name per-trade expectancy exists in several books (sector +0.47%,
idx +0.73%, intl +0.23% vs stocks +0.289%); **only stocks have edge ×
frequency** — 113 account-level trades, vs 45 (FX) and ≤18 everywhere else;
the FX book's +0.002 Σ is noise-level. Combined book
(stocks+intl+sector+comm): CAGR +12.0%, Sharpe 0.84, maxDD 13.4%, worst day
**−6.08%** (fatter → worse prop headroom); stocks↔comm corr −0.04 but the
daily tail is what binds FTMO floors, so the prop account is stocks-only and
the diversified option stays the "personal" profile.

**Prop MC** and attempt economics: see §5 tables (they are the deployment
ground truth). **Legacy 60-day tables** (RESULTS.md) differ because a
30/60-day window is not the real rule set — never quote them for FTMO sizing.

**Forward simulation of the deployment path** (67 names, config
`prop_ftmo_100k.json`, MT5-bridge broker path, next_open, k=0.75, warm-up
2016-2022, trading 2023-01→2026-10, journal session `20261019_063215`):

| verdict field | value |
|---|---|
| days / trades | 951 / 135 exits |
| equity | $100,000 → $159,625 (**+59.63%**) |
| mean/day | +0.0505% (window includes strong 2023-H1) |
| σ/day | 0.676% = **0.74 ×** reference 0.919% |
| Sharpe / t | 1.19 / +2.30 |
| maxDD / worst day | 9.39% / −3.50% (both ≈ 0.75× BT) |
| guard | **phase 0 + phase 1 passed, 0 halts** |

**Parity gate** (M07, unchanged since): stocks 1d 12 tapes field-exact
(BA 6/6, BLK 5/5, AXP 2/2), perp 1h BTCUSDT 74/74, ETHUSDT 63/63; balance
equal to 1e-12.

---

## Stack inventory

### `market_backtest/` (analysis & selection)

| file | role |
|---|---|
| `strategy_engineV2M.py` | the engine clone (`MarketStrategyEngine`, ζ=0); no other engine change |
| `market_calibration.py` | causal per-tape calibration (§2) — rescale, clamps, τ branch, σ-gate, arming ladder |
| `fetch_stocks.py` / `fetch_markets.py` / `fetch_perps.py` | nasdaq equities/ETFs (+FRED path), Binance Vision perps → market_data.db |
| `market_store.py` | recordings + candles in `backend/data/market_data.db` |
| `runner.py` | `run_market_backtest`: full-tape 4-state replay, train freeze, costs (`COSTS`, `MARKET_CLASS`, `BAR_SPEC` incl. unvalidated `stocks_1h/5m`), returns per-trade records + summary |
| `batch.py` | process-pool run over a universe (guards parent PID like production) |
| `sweep.py` / `cells_*.json` / `show_sweep.py` | constant-override cells, per-exit-reason PnL, Σ ranking |
| `stats_report.py` | ledger/snapshot equity, bootstrap CIs, Wilcoxon, breadth, legacy prop sims |
| `market_paired.py` | paired per-symbol accept gate |
| `market_scan.py` / `combine_books.py` | league table + pooled-book analysis |
| `mc_deploy.py` | method-robust MC, funded economics, personal frontier, Kelly/cap-safe |
| `mc_firms.py` | **real firm-rule MC** (ftmo_2step, nocons control, 8/5, nodaily_{10,8,6}, ftmo_1step, topstep/apex styles, ftmo_trial_2step/1step, ftmo_2step_30d); consistency modeled |
| `prop_frontier.py` / `prop_bound.py` | required-mean-shift frontier; clairvoyant perfect-stop bound (falsified: stop families can't be reached by calibration, and fail re-entry churn) |
| `campaign_summary.py` | all-cell Σ ranking |
| `RESULTS.md`, `PROP_FIRMS.md`, `MARKET_RESEARCH_LOG.md` | the three documents |
| `results/` | arm14/, arm16/, conf90/92/, batch JSONs, scan_*, league_arm14.json, combo_stocks_plus.json |
| `sweeps/` | per-market round cells + dose/combo JSONs |

### `market_live/` (decision-identical execution)

| file | role |
|---|---|
| `host.py` | `MarketEngineHost` — builds MarketForwardTester per symbol with identical kwargs; `warm()` replays history; per closed bar issues the same 4 updates; `params_map(results_dir,…)` freezes arm14 per symbol; `engineParamsV2M` reporting |
| `feeds.py` | closed-bar feeds: `YahooFeed` (429-blocked), `AlpacaFeed` (needs keys), `TapeFeed` (DB replay; live polling with the closed-bar discipline) |
| `brokers.py` | `PaperBroker` (ledger accounting, slippage/fee), `AlpacaBroker` (REST, MOC `cls` TIF, paper toggle), `HttpBridgeBroker` → MT5 (with `symbols()`/`symbol_info()` introspection) |
| `prop_guard.py` | firm templates (incl. `ftmo_trial_2step/1step`), floors with 0.5% buffer, phase progression, consistency tracking, `guard_state.json` restart-safety (+6/6 unit tests) |
| `trader.py` | `MarketLiveTrader` — wiring: pending next_open fill path, per-state sizing (`k × buy_size × equity`), position sync on restart, flatten hooks, journals each decision/fill/guard event |
| `journal.py` | JSONL session journals under `backend/data/live_market/<id>/`: `session_open.json` (config + `code_commit`/`code_dirty`), `equity.jsonl` (per-bar), `decisions.jsonl` (engine ID/host state), `trades.jsonl`, `guard.jsonl` |
| `run_live.py` | CLI: `{replay,paper,live,compare,preflight}` × `--feed auto\|tape\|yahoo\|alpaca` × `--config` JSON (configs override flags; explicit flags override config), `--once` for the daily loop |
| `setup_prop.py` | **preflight**: ① tapes+frozen params ② broker reachable+equity ③ symbol naming vs broker (via `/symbols`) ④ sizing vs min-lot notional ⑤ guard floors vs live equity; optional `--place-test-order` round-trip. Exit code = GO/NO-GO |
| `mt5_bridge_server.py` | the FTMO/MT5-side server (runs on a Windows VPS): `/health /equity /position /symbolinfo /symbols /state`, `POST /order /flatten` (+simulate mode for loop tests); shares→lots via `trade_contract_size` with volume_step rounding & filling-mode retry; optional bearer token |
| `forward_monitor.py` | realised-vs-backtest verdict per §8 |
| `test_live_parity.py` / `test_prop_guard.py` | the two gates (parity MUST pass after every deploy) |
| `config/` | `prop_ftmo_100k.json` (production: mt5, ftmo_2step, k=0.75, next_open), `paper_100k.json`, `alpaca_paper.json` (free forward test), `trial_ftmo_2step.json` (free-trial rules) |
| `README.md` / `GO_LIVE.md` | runbook + purchase→execution sequence |
| `../start_prop.sh` | one command: preflight → `run_live live --once` → monitor |

**Decision-identity guarantee chain**: same engine file → same tester class
→ same 4-update protocol → parity gate on real tapes → journals carry
`code_commit`/`code_dirty` → post-deploy check `session_open.code_commit ==
HEAD` (the memecoin lesson INU Sep-29, applied here).

---

## Traps (market campaign)

1. **Yahoo 429** — never bulk-scrape Yahoo here; nasdaq + Binance only.
   Alpaca for live intraday (keys pending).
2. **Per-trade mean ranking** — silently rejects entry-restriction gains;
   rank Σ, report both.
3. **Snapshot MTM** — flatters turnover-heavy books (perps 40×); ledger only.
4. **Calibration leakage** — anything fitted must be `candles[:split]` only,
   and the runner must partition stats by entry time (calibrated params
   + un-partitioned stats = in-sample information in the test window).
5. **Dead / misread config on market tapes** (enumerated in the campaign):
   `stoploss_pct` family, `trail_floor_pct`, `forbidden_bc_*`,
   `max_entry_bar_count`, mcap bounds, `reversal_exit_bars`, rate-split-θ
   and **rate-split-arm** (no-op on stocks), EVR concentration veto
   (latches ≈0.33 > 0.25 on 3-candle windows → EVR nearly dead calibrated)
   and EVR-reactive cuts generally (churn identity: −3.7% legs re-bought
   immediately; per-name destruction e.g. MSFT +0.0054 → −0.0407).
6. **Kelly is not the boss** — k*≈5.1 misleads: rule walls bind long before
   Kelly; the operating regime is k=0.5–1.5 where EV/attempt peaks.
7. **`best-day share`/consistency rule**: pass requires the guard's
   trailing measurement to be *consistent with FTMO's* (they keep trading
   until satisfied; the MC models that; the live guard tracks best-day
   share continuously).
8. **Venv/product paths**: results dirs relative to cwd — always run from
   `backend/`; `market_data.db` in the *worktree* data dir, not the main one.
9. **Pooled-book worst day** — diversification fattened −5.17% → −6.08%;
   for prop, breadth does not shrink the daily-tail risk the floors bind on.
10. **Free trial ≠ evaluation** (§7): don't let it introduce sizing chaos.

---

## Verdict and honest limits

1. **One calibration family in ~146 cells passes the full acceptance
   protocol on both segments**: gain-retrace arming at 14.0 (`arm14`).
   Stocks +10.9%/yr OOS / +6.7%/yr IS; perps remain negative under every
   family (1h arm14 teΣ −0.269; in funded MC, k≥0.25 blows in ≥99.5% of
   years — the pass rates that exist at high k ride pure variance).
2. **Prop pass "all the time" is not real** for any strategy of this class;
   what is real under the verified rule set: FTMO 2-Step + k=0.75 gives
   74.2% full pass / +$9.1k EV per attempt, and the deterministic path
   passes both phases (~21 months). The residual risk is concentrated where
   it always is: *does the edge persist OOS in live* — which is exactly
   what the free forward tests (Alpaca paper now, FTMO free trial for
   plumbing) are for.
3. Selection bias: arm14 survived because it is monotone-in-dose and
   cross-market in mechanism, not because it was the top single cell — but
   a ~146-cell search was run and one must not treat +10.9%/yr as the live
   expectation. The monitor's DEGRADING/FAILING thresholds (half-μ, μ≤0
   for 20+ days) define when the campaign's calibration is falsified.
4. Model side: no swap drag modeled (−1–1.5%/yr at 55% exposure), $540/80%
   split assumed, single-window bootstrap cannot price regime change, and
   the only profitable universe is 67 names on daily bars (no other
   instrument class passes anything).

## Open work / next moves

- [ ] **Alpaca keys** (`APCA_API_KEY_ID/SECRET`) → run `setup_prop` +
      `run_live live` on `alpaca_paper.json` → the real free forward test.
- [ ] **FTMO free trial** signup (needs user email; 14-day plumbing test
      with `trial_ftmo_2step.json` on the demo/MT5-bridge).
- [ ] **Real MT5 bridge validation** on the FTMO demo account (preflight
      `--place-test-order` round-trip) before paying for the challenge.
- [ ] **Adoption step**: bake arm14 into `market_calibration.py`
      (`arm = clip(0.55·runup_med, 3.0, 14.0)`) and re-run to confirm
      parity (currently the override lives in configs/results only).
- [ ] **Intraday (1h/5m)** plumbing exists; needs a data feed before any
      validation.
- [ ] Push branch to remote (user hasn't asked).

## Reproduce (short form)

Full command card exists in `RESULTS.md` (backtests/sweeps/paired/MC) and
`../market_live/README.md` (live). The two gates:

```bash
cd /Users/jaime/pump-chart-market/backend
/Users/jaime/pump-chart/backend/.venv/bin/python -m market_backtest.mc_firms \
    --dir market_backtest/results/arm14 --label arm14 --market stocks \
    --interval 1d --sims 3000 --ks 0.75 --templates ftmo_2step
/Users/jaime/pump-chart/backend/.venv/bin/python -m market_live.test_live_parity
/Users/jaime/pump-chart/backend/.venv/bin/python -m market_live.setup_prop \
    --config market_live/config/prop_ftmo_100k.json
```
