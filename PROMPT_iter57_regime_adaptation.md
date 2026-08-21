## 0. ROLE & OBJECTIVE

You are a quantitative researcher + execution engineer embedded in the **Pump-Chart** repo (`/Users/jaime/pump-chart`). Your sole objective for this iteration is:

**Engineer a strictly additive, pipeline-parity-safe, default-OFF mechanism that makes `StrategyEngineV2` automatically adapt its entry / exit / sizing behaviour to shifts in the *global pump.fun market regime* — so that the decay documented in `pump_fun_market_structure_report.md §6-7` and `DATE_SEGMENTED_BACKTEST_REPORT.md` (WR Spearman r=-0.76, p=0.0002; `gain_retrace` 67.4%→49.9%; avg win -26.8% on negative days) is neutralised without manual retuning.**

You must clear the **Paired-Difference Anti-Overfit Decision Protocol** vs the current canonical baseline. Anything that does not clear the gate is REJECTED and reverted — no exceptions.

---

## 1. MANDATORY PRE-READ (do not skip)

Read these files **in full** before writing any code:

1. `AGENTS.md` — § Core Conventions & Execution Invariants (esp. #1 Pipeline Parity, #2 4-State Expansion, #4 Force-Close, #6 Engine Factory, #9 guard_parent), § Strategy Engine V2 Specification, § Guidelines for Engine Developers.
2. `pump_fun_market_structure_report.md` — entire file, with emphasis on:
   - §6.1 Spot DEX Volume Collapse (weekly $118B→$44B -62%, May $104B→$18B -82%, crash hazard λ≈0.05/week)
   - §6.2 Launchpad Share Oscillation (Pump 99%→14%→98% in 1–2 weeks, LetsBonk 5%→64%→15%)
   - §6.3 Perp DEX Rotation (Solana perps Q2 $147–183B record vs spot $160B -44% QoQ, DEX:CEX 3%→13%)
   - §7.1 Formal WR decay (Pearson r=-0.697, slope -0.97pp/day, R²=0.48; gain_retrace -0.719, PnL/trade flat via fragile median-win compensation)
   - §7.2 Why Kramers loses power (ρ peak at stall, V_liq taper → du_up<0 → k_up=1e6, P_down≡0 blindness 87% losers + 88% winners, 4× intra-candle μ flicker)
   - §7.3 What structure predicts (OHLCV ceiling theorem — iter37 oracle bound +0.786 < baseline +0.965)
3. `RESEARCH_LOG.md` — Methodology §1-5 + Iteration Summary Table + detailed Iter52 (Dynamic Market-Condition Adaptation REJECTED, Δ=-0.93 SOL, p=0.985), Iter56 (HF Silence Gate ACCEPTED), Iter37/48/50. You must not repeat a proven-negative mechanism unchanged.
4. `DATE_SEGMENTED_BACKTEST_REPORT.md` — 23-date table, §2 MWU cohort comparison (WR 73.2%→53.4% p=0.005, rec_ended 7.5%→20.7% p=0.068, loser hold +64%), §3 trade-level payoff collapse (avg win 0.0146→0.0107 -26.8%, breakeven WR 61%→68%), §4 two archetypes Type A (violent dump) vs Type B (low-vol grind), §5 synthesis 4 patterns.
5. `backend/analysis/date_segmented_results.json` — raw per-date JSON that `DATE_SEGMENTED_BACKTEST_REPORT.md` is built from (produced by `backend/analysis/run_date_segmented_backtests.py:52`).
6. `backend/strategy_engineV2.py` — `DEFAULT_CONFIG` (16 SDE params + meta), `FUTURES_DEFAULT_CONFIG` pattern, `StrategyEngineV2Adapter.__init__` futures-override merge, `_check_exit_v2` exit ladder, `update()` entry gate. Note how iter52/56 added `v2_*` knobs strictly additive with default-OFF.
7. `backend/data_store.py` — schema: `recordings(id, mint, started_at, ...)`, `candles(time, open/high/low/close, volume, buy_volume, sell_volume, pool_sol, market_cap_usd, funding_rate, mark_price)`, `holder_flow`, `backtests` / `backtest_trades`.
8. `backend/backtester.py`, `backend/forward_tester.py`, `backend/live_trader.py` — the "Three Amigos" must stay byte-identical via `engine_factory.create_engine(engine_version=2)`.
9. `backend/analysis/paired_diff.py` and `run_iteration.py` — the canonical validation tools.

---

## 2. DIAGNOSIS PHASE — ESTABLISH WHAT "REGIME" MEANS HERE

Before coding, produce a 1-page diagnosis artefact (print to stdout or `backend/analysis/iter57_diagnosis.md`) that answers:

- What **time-varying regime variable** would have predicted the WR decay ex ante? Candidate families (test all, keep the one with out-of-sample rank correlation to next-day WR / gain_retrace rate):
  - **Global spot liquidity:** daily DEX volume (Solana aggregate), Pump.fun vs LetsBonk share `s_Pump(t)`, `λ(t)` token arrival rate (births/day), SOL price level `P_SOL` (re-denominates MC_grad 32k→69k).
  - **Global risk appetite:** perp volume (Solana perps, Hyperliquid), funding rate / OI, BTC/SOL momentum, stablecoin inflows.
  - **Token-local market state:** per-token `mean_pump_pct`, `turnover_sol`, `spread`, plus the engine-internal `sigma_t`, `P_up`, `T_t`, `rho` mass already logged in `entry_params`.
  - **Structural:** holder_flow silence rate, dev-sell frequency (iter56 silence gate is a special case of this).
- What is the **mathematical link** between that regime variable and the failure mode? E.g.:
  - On Type B (grind) days payout skew collapses → `gain_retrace_give_frac=0.5` that was optimal on the pooled dataset is too loose; it rides winners back to scratch and inflates `recording_ended` (-27% avg, 20.7% share on negative days).
  - On Type A (dump) days `kelly_flat` fires at 10.4–13.6% share and `recording_ended` lingers +576s because global liquidity is thin.
- Why did **iter52 fail**? (`q_t = q_pump * q_dd` scaling of `C_high / P_up / τ / n*` suppressed profitable recovery trades symmetrically with losers → breadth 22.7%). Your new controller must separate winners from losers **within a regime**, not suppress all trading in that regime.

**Formal requirement:** define a scalar or 2-D **regime score** `Q(t) ∈ [0,1]` (or discrete `Z_t ∈ {grind, normal, dump}`) that is:
- **Causal** — computable from information available at bar `t` (no lookahead; at backtest time `Q(t)` may only use data ≤ `t`; at live time it uses a live fetcher).
- **Coin-agnostic** — when `Q` is a global market variable it is the same for all mints on that date/session (the report stresses `98%` of volume is market rejection, not token idiosyncrasy).
- **Testable** — you must show its Spearman correlation to next-trade `gain_retrace` hit-rate / WR on the 23-date panel before wiring it to the engine.

---

## 3. DATA AVAILABILITY AUDIT — AND MANDATORY HISTORICAL FETCH IF MISSING

The current backtest DB `backend/data/price_data.db` contains **1,394 completed recordings** (2026-07-27→2026-08-21) + per-candle OHLCV. It does **NOT** contain global regime history by default.

**Step 3a — Audit locally first (cheap):**

```bash
sqlite3 backend/data/price_data.db "SELECT date(started_at,'unixepoch'), count(*) FROM recordings GROUP BY 1 ORDER BY 1;"
sqlite3 backend/data/price_data.db "SELECT sql FROM sqlite_master WHERE type='table';"
python3 -c "import sqlite3; conn=sqlite3.connect('backend/data/price_data.db'); print(conn.execute('SELECT * FROM candles LIMIT 1').description)"
cat backend/analysis/date_segmented_results.json | head -n 80
```

Check what `market_features` already exist per-date (mean_pump, turnover, etc.) vs what is missing for your chosen `Q(t)`.

**Step 3b — If your chosen regime requires data NOT in the DB, you MUST fetch it historically from online sources before any engine edit.** Global regime data is coin-agnostic, so one historical fetch covers the whole cohort.

You are explicitly authorised to obtain **historical** (not just live) data from public APIs. Suggested sources (pick the minimal set your hypothesis needs — do not fetch everything):

| Regime feature | Source | How to fetch historically |
|---|---|---|
| SOL price `P_SOL` (denominates MC_grad, fee revenue) | CoinGecko `/coins/solana/market_chart?vs_currency=usd&days=30-90`, or DexScreener, or Bybit spot klines | `WebFetch` / `curl https://api.coingecko.com/api/v3/coins/solana/market_chart?...` — cache to `backend/data/global_regime_cache.json` or new `global_regime` SQLite table keyed by `date` |
| Solana DEX spot volume (aggregate) | DeFiLlama `https://api.llama.fi/overview/dexs?chain=Solana`, Dune `@adam_tehc` pump.fun daily volume, DropsTab weekly | DeFiLlama has daily history; Dune CSV via `WebFetch` |
| Launchpad share `s_Pump` / `s_LetsBonk` | `dropsab.com` timeline in report §6.2, or on-chain birth counts via Dune `pump.fun` vs `letsbonk.fun` programme IDs | If API not available, encode the published weekly share breakpoints (Apr25, Jul7, Jul21, Jul28, Aug11) as a piecewise-constant `s_Pump(t)` — cite source table in code comments. This is admissible as a historical prior; do not hallucinate daily values. |
| Perp volume / funding (risk rotation) | Bybit `backend/futures_exchange.py` already has `futures_cache.db` pattern; or CoinGecko `/derivatives/exchanges/hyperliquid`, `GMTrade` daily | Reuse `futures_exchange.py::get_futures_candles` for BTC/ETH/SOL perps funding & OI history; or `https://api.llama.fi/perps` |
| Token arrival rate λ(t) | Dune `11.9M births` tracker, or directly `count(*)/day` from `recordings` table itself — already available locally | Prefer local `recordings` count/day as λ proxy if external not reachable |
| Memecoin market cap / attention | CoinGecko `memecoin` category market cap, or `price_data.db` `mean_pump_pct` already per-date | Use CoinGecko category chart as global alternative |

**Persistence contract:**
- Cache fetched history to **one** of: `backend/data/global_regime_cache.json` (simple) or a new SQLite table `global_regime(date TEXT PRIMARY KEY, sol_price REAL, dex_volume REAL, pump_share REAL, perp_volume REAL, ...)` via `data_store.py` migration (preferred for backtester joins).
- Key by `date` (`YYYY-MM-DD` in UTC, matching `recordings.started_at`) or by `time` (unix sec) if intraday resolution needed.
- The backtester must join `Q(t)` by `candle.time` → `date(Q)` at replay time, so future live inference can do the same (live fetcher polls same endpoint every e.g. 60s).
- Log fetch provenance (URL, date range, row count) in the diagnosis artefact. No fabricated data.
- All 1,394 recordings already have a `started_at` date; the join is `LEFT JOIN` so recordings without global data simply get the default (neutral) regime value — preserving parity on incomplete history.

**If you cannot reach an external API (rate-limited, 429), fall back to the best local proxy** (`recordings` count/day, `mean_turnover_sol`, `mean_pump_pct` from `date_segmented_results.json`) and document why. Do not block the iteration on a fetch failure.

---

## 4. SOLUTION ENGINEERING — HARD CONSTRAINTS

### 4.1 Protocol compliance

- **One hypothesis, one mechanism.** Do not bundle 3 ideas. If you want to test 2 regime features, pick the strongest and leave the other for a follow-up iteration.
- **Strictly additive, default-OFF.** Every new param must be `float`/`int` with a neutral default that restores byte-exact baseline behaviour. Example pattern (copy iter56/iter52 style):

  ```python
  # backend/strategy_engineV2.py  DEFAULT_CONFIG
  "v2_regime_enable": 0.0,          # 1.0 = ON, 0.0 = OFF (parity)
  "v2_regime_q_threshold": 0.35,    # regime score below which adaptation arms
  "v2_regime_mode": "auto",        # "auto" | "off" — future-proof
  # ... + whatever your controller needs

  # StrategyEngineV2Adapter.__init__
  self._v2_regime_enable = float(engine_kwargs.pop("v2_regime_enable", 0.0))
  # guard every new branch with `if self._v2_regime_enable > 0.5:` or `<=0: return neutral`
  ```

  When all new flags are OFF, a spot run with `engine_params={}` must be **byte-identical** to HEAD (prove with `diff` on 2–3 probe recordings).

- **Pipeline parity invariant.** `Backtester`, `ForwardTester`, `LiveTrader` must evolve engine state identically:
  - All three must load `Q(t)` the same way (backtester via SQLite join on `candle.time`; live via `HolderFlowMonitor`-style poller or `main.py` background task if global; forward tester same as backtester).
  - 4-State Intra-Candle Expansion is sacred — feed `Q` at State 1..4 identically (global `Q` is constant within the candle; do NOT recompute per sub-tick from future candles).
  - 1-Bar Execution Delay enforced; `recording_ended` force-close preserved.
  - `engine_factory.create_engine(engine_version=2, **params)` is the only instantiation path.
  - If you add a new DB table, ensure `price_data.db` WAL mode + read-only worker connections (`_get_price_read_conn`) still work for `ProcessPoolExecutor` workers; add `guard_parent()` to any new worker initializer.

- **Determinism.** No `random()` without `rng_seed`. No wall-clock branching.

- **No lean on OHLCV-only exit tweaks alone.** `iter37` proved pure exit-timing changes are bounded below baseline (oracle +0.786 < +0.965). Your controller must be **regime-conditioned** — i.e. the same price action triggers different thresholds depending on `Q(t)`. A flat `gain_retrace_give_frac` retune is not a regime adaptation.

### 4.2 What "automatic adaptation" should look like (pick ONE concrete mapping)

You must choose a **causal, monotonic mapping** from `Q(t)` to an engine decision variable that directly addresses the payout-collapse mechanism. Examples that are admissible (you may invent your own if better justified):

- **A. Adaptive `gain_retrace_give_frac` / `gain_retrace_arm_pct`:** `Q` low (grind regime, low `mean_pump` / low DEX vol) → tighten trail (e.g. `give_frac = 0.5 - α·(1-Q)`) so winners are banked earlier before they mean-revert; `Q` high (trend regime) → keep loose `0.5`.
- **B. Adaptive entry confidence `C_high` / `P_up_min` / `σ_t_min`:** `Q` low → raise `C_high` 0.79→0.86 / `P_up_min` 0.62→0.72 to suppress low-skew entries that become `recording_ended` (-27% avg).
- **C. Adaptive persistence guards:** `Q` low → shorten `no_long_exit_bars` 60→30 or arm `recording_ended`-risk exits earlier (but with a regime-conditioned offside threshold so winners aren't chopped — prove with per-trade offside analysis).
- **D. Regime-conditioned position sizing / Kelly cap:** scale `n*` by `f(Q)` (but note iter53/33b sizing was fragile; you must prove sizing path actually hits executed `size_sol` via `forward_tester.py` and doesn't just saturate at the `0.1·L_t` cap).
- **E. Hierarchical Bayesian regime switch:** two `DEFAULT_CONFIG` presets (grind vs trend) and a **learned HMM posterior** over `Z_t` updated from global + local features; the engine blends or switches presets by posterior weight (most ambitious, must still be default-OFF).

**Do not implement more than one mapping in this iteration.** If you pick A, do not also sneak in B.

### 4.3 Forbidden moves (learned from rejected iters)

- No unbounded `mu_dot` / `phi` EMA entry blocks (iter05/06 REJECTED — `k_up=1e6` degeneracy).
- No hard SL caps -7%..-25% (iter07 — winner truncation > loser saving by 10–20 SOL).
- No `T_w` or `dt` retunes to fix regime (iter14/16k already converged).
- No provenance / holder-concentration token filters (iter35 — 26% dual-outcome mints, AUC 0.5).
- No naïve `q_t = q_pump * q_dd` multiplicative entry suppression (iter52 — breadth 22.7%).
- Do not re-introduce a `max_entry_bar_count` or `forbidden_bc` gate.
- Do not touch `FUTURES_DEFAULT_CONFIG` unless your regime explicitly covers both spot and futures (default: spot only).

---

## 5. IMPLEMENTATION CHECKLIST

- [ ] `backend/strategy_engineV2.py`
  - `DEFAULT_CONFIG` new `v2_regime_*` keys with neutral defaults
  - `StrategyEngineV2Adapter.__init__` pops + stores them, `_is_futures_engine` guard untouched
  - `update()` and/or `_check_exit_v2()` branch on `Q(t)` only when `v2_regime_enable > 0`
  - If global regime: add `set_global_regime_events()` / `append_global_regime()` mirror of holder_flow pattern, or simpler per-candle `Q` lookup inside `update()` via injected `global_regime_map` (choose one, keep parity)
  - `frontend/js/app.js` `engineParamsV2` — expose the same keys so the UI can toggle them (or document why UI not needed for global auto-mode)
- [ ] `backend/data_store.py` (only if you add a table)
  - `init_price_db()` creates `global_regime` table + index, with `ALTER TABLE` migration for existing DBs
  - Helpers: `get_global_regime_for_time(t)`, `insert_global_regime_batch(rows)`, `get_global_regime_map(start, end)`
- [ ] `backend/backtester.py`
  - Loads `global_regime` map once per batch and passes it per-recording to `ForwardTester` (same pattern as `get_holder_flow`)
  - No new worker without `guard_parent(initializer=guard_parent)` 
- [ ] `backend/forward_tester.py`
  - Accepts `global_regime_events` / `global_regime_map`, feeds `engine.update(..., global_q=...)` or `engine.set_global_regime(...)` before the 4-state loop
- [ ] `backend/main.py` (only if global regime needs live polling)
  - Background poller task (mirrors `_holder_flow_pump` 1s task) that fetches latest global snapshot and calls `engine.append_global_regime(...)`
  - Pre-loads historical map at session start via `set_global_regime_events()`
- [ ] `backend/live_trader.py`
  - Respects the same `Q(t)` gating as backtester (no divergence in exit reason labels)
- [ ] Tests: `cd backend && python -m pytest` or at minimum `python test_futures.py` and a 3-recording parity probe (`recs {1019,878,951}` or any 3 from `date_segmented_results.json`) showing `v2_regime_enable=0` byte-identical to baseline

---

## 6. VALIDATION PROTOCOL — THE ONLY GATE THAT MATTERS

### 6.1 Baseline

Run a **fresh baseline** on the CURRENT `price_data.db` (do NOT reuse a stale `iter08_baseline_full.json` from the deleted dataset):

```bash
BACKTEST_RESULTS_DIR=backend/v2_results python run_iteration.py --label iter57_baseline_full --max-workers 8
# produces backend/analysis/iter57_baseline_full.json + backend/v2_results/*_iter57_baseline_full_*.json
```

Record headline: trades / WR / total PnL / PF / expectancy / exit-reason breakdown / per-date WR curve. This is your `baseline` label.

### 6.2 Candidate

Run the same cohort with your regime controller ON:

```bash
# Example: params file that enables your regime controller
cat > /tmp/iter57_candidate.json <<'JSON'
{
  "v2_regime_enable": 1.0,
  "v2_regime_q_threshold": 0.35,
  "v2_regime_give_frac_adapt": 0.3
}
JSON
BACKTEST_RESULTS_DIR=backend/v2_results python run_iteration.py --label iter57_candidate --params /tmp/iter57_candidate.json --max-workers 8
```

***Full-cohort only.*** Do not hand-pick a favourable 50-recording subset. Date-segmented diagnosis is for **analysis**, not for cherry-picking the backtest cohort.

### 6.3 Paired-difference gate (strict)

```bash
python backend/analysis/paired_diff.py --baseline iter57_baseline_full --candidate iter57_candidate --save iter57_vs_baseline
cat backend/analysis/iter57_vs_baseline.json
```

**ACCEPT iff ALL three hold:**

1. **Wilcoxon signed-rank (one-sided greater) p < 0.05** on per-recording Δ PnL (`candidate - baseline`)
2. **Bootstrap 95% CI (10,000 samples) lower bound > 0** (mean Δ PnL strictly positive)
3. **≥ 50% of common tokens improved** (anti-overfit breadth)

Also report (but not gate): paired t-test p, McNemar p, tokens L→W vs W→L, worst 10 regressions, per-exit-reason PnL migration (especially `gain_retrace` vs `recording_ended` / `kelly_flat` / `evr_triage`), and per-date Δ PnL (does the candidate fix negative days without hurting positive days?).

**Additionally — regime-specific validation (required for this iteration):**

- Split the paired diff by **date**: `early (2026-07-27→08-07)` vs `late (08-08→08-19)` or `positive-days` vs `negative-days` cohort. The candidate must **not** sacrifice positive-day PnL to fix negative days (report both sub-cohorts). A candidate that is +0.5 SOL on 9 negative days but -0.4 SOL on 14 positive days is still a net +0.1 SOL — but breadth per-date matters.
- Show that `Q(t)` was **not fitted with lookahead**: `Q` at `2026-08-19` must be computable from data ≤ `2026-08-19 00:00 UTC` (global daily) or ≤ candle time `t` (intraday).
- If you did a parameter sweep over `v2_regime_*` thresholds, report the **sweep table** (all thresholds, not just the best) and prove monotonicity / stability — a one-cell +0.6 SOL spike with neighbours at -0.3 SOL is overfit.

### 6.4 Tail-focused secondary checks (informational, not gating)

Run `backend/analysis/iter45_tail_test.py`-style checks if your mechanism targets tails: big-loser count (≤-15%, ≤-30%), tail_drag PnL, worst-trade PnL, `kelly_flat` + `recording_ended` composite. These are lenses, not substitutes for the whole-PnL gate.

### 6.5 Decision

- **If gate clears:** keep the code, set the validated `v2_regime_*` defaults to the winning values in `DEFAULT_CONFIG` + `app.js`, document in `RESEARCH_LOG.md` (see §7), and keep the global fetch cache in-repo (`backend/data/global_regime_cache.json` committed or `.gitignore`'d with a fetch script).
- **If gate fails:** **REVERT** `strategy_engineV2.py` to byte-identical HEAD for `engine_params={}` (prove with `git diff backend/strategy_engineV2.py` showing only default-OFF scaffolding, or full revert). Keep the diagnosis artefact + fetched cache for the next iteration's prior — do not delete evidence of a negative result. Document the REJECTED iteration in `RESEARCH_LOG.md` with the exact Δ, p, CI, breadth, and why the mapping failed (e.g. "tightening give_frac in grind regime cut winners symmetrically").
- **Do NOT** claim improvement from single-token anecdotes or small samples. The full paired diff is the only verdict.

---

## 7. DOCUMENTATION

On completion (ACCEPT or REJECT), append to `RESEARCH_LOG.md` following the existing pattern (see Iter52 / Iter56 / Iter48 entries):

```
## iter57 — [Title] — [ACCEPTED | REJECTED]
**Date:** 2026-08-21
**Hypothesis:** ...
**Mechanism:** Q(t) definition, mapping to engine variable, causal guarantee
**Data:** what was in DB vs what was fetched (source, URL, date range, row count, cache path)
**Files modified:** ...
**Batch labels:** baseline `iter57_baseline_full` (trades X, WR Y%, PnL Z SOL, PF ...), candidate `iter57_candidate` (same)
**Paired diff:** Δ PnL, Wilcoxon p (greater), bootstrap CI, breadth %, McNemar p, per-date split, tail cut
**Verdict:** ...
**Lesson:** ...
```

Link the diagnosis artefact and the fetched cache file.

---

## 8. REMINDER — WHAT "AUTOMATICALLY ADAPT" MEANS

- **Not** a one-time retuning of `gain_retrace_give_frac 0.5→0.4` that is optimal on the pooled 23 days but still static.
- **Not** a manual "if date < Aug07 use preset A else preset B" switch.
- **Yes** a function `θ_t = f(Q(t); θ_0)` where `θ_t` is the engine threshold (e.g. `give_frac`, `C_high`, `P_up_min`, `no_long_offside_pct`) and `Q(t)` is updated causally as global market data arrives — so when DEX volume collapses next week or Pump share swings again, the engine moves itself without a code deploy.
- The live path must actually poll `Q(t)` (or derive it from live `recordings` count / SOL price WebSocket) — not just backtest-join a static CSV.

---

## 9. QUICK-START COMMAND SEQUENCE (for the agent)

```bash
# 0. Read the docs (see §1)
cat pump_fun_market_structure_report.md
cat DATE_SEGMENTED_BACKTEST_REPORT.md
cat backend/analysis/date_segmented_results.json | python3 -m json.tool | head -n 200
sqlite3 backend/data/price_data.db "SELECT count(*), min(started_at), max(started_at) FROM recordings;"
ls -lh backend/data/*.db backend/v2_results/ 2>&1 | head -n 30

# 1. Diagnosis — correlate candidate Q(t) to WR/gain_retrace
python3 backend/analysis/run_date_segmented_backtests.py  # if you need fresh per-date panel
# ... your correlation script that builds Q(t) from local + fetched global ...

# 2. If needed — fetch historical global regime (see §3b)
mkdir -p backend/data
python3 backend/fetch_global_regime.py  # you will create this; it writes backend/data/global_regime_cache.json

# 3. Baseline
BACKTEST_RESULTS_DIR=backend/v2_results python run_iteration.py --label iter57_baseline_full --max-workers 8

# 4. Implement regime controller in backend/strategy_engineV2.py (default-OFF)

# 5. Candidate
echo '{"v2_regime_enable":1.0}' > /tmp/iter57_params.json
BACKTEST_RESULTS_DIR=backend/v2_results python run_iteration.py --label iter57_candidate --params /tmp/iter57_params.json --max-workers 8

# 6. Gate
python backend/analysis/paired_diff.py --baseline iter57_baseline_full --candidate iter57_candidate --save iter57_vs_baseline
python backend/analysis/aggregate_results.py --batch-id iter57_baseline_full --save iter57_baseline_summary
python backend/analysis/aggregate_results.py --batch-id iter57_candidate --save iter57_candidate_summary

# 7. Parity probe (must be byte-identical with flag OFF)
python3 -c "from engine_factory import create_engine; e1=create_engine(2); e2=create_engine(2, v2_regime_enable=0.0); assert e1.cfg==e2.cfg; print('parity OK')"
```

---

## 10. CONSTRAINTS SUMMARY

- Follow `AGENTS.md` invariants verbatim. Do not break 4-state expansion, force-close, engine_factory, or determinism.
- Keep `backend/candles.db` untouched. All real data is `backend/data/*.db`.
- Use `BACKTEST_RESULTS_DIR=backend/v2_results` for all sweeps.
- Guard every new `ProcessPoolExecutor` worker with `guard_parent()`.
- No orphaned files, no linter/formatter assumptions, preserve existing code style.
- Cite every external data source URL and cache it.

