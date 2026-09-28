# AGENTS.md

Real-time analytics + auto-trading for volatile Solana memecoins (pump.fun/PumpSwap).
Browser dashboard (vanilla JS) ↔ single FastAPI app (`backend/main.py`): trade streaming,
1 s candle aggregation, stochastic strategy engines, live on-chain execution (Jupiter),
SQLite recording, batch backtests over recordings.

Research history: **RESEARCH_LOG.md** (condensed). Old 900-line inline version:
`git show 3a33e47:AGENTS.md`. Original 8,977-line log: `git show fde6abc:RESEARCH_LOG.md`.

## Commands

```bash
./start.sh
# or: cd backend && source .venv/bin/activate && python main.py  # :8000, serves frontend

cd backend && ./.venv/bin/python -m pytest analysis/ -q \
  --ignore=analysis/test_live_trader_balance.py \
  --ignore=analysis/test_timeout_hypothesis.py

BACKTEST_RESULTS_DIR=backend/v2_results python run_iteration.py --label <label> --max-workers 8
BACKTEST_RESULTS_DIR=backend/v2_results python run_iteration.py --label <label> \
  --recording-ids-file /path/to/ids.json --max-workers 8
python backend/analysis/paired_diff.py --baseline <batch> --candidate <batch> --save <name>
python backend/analysis/aggregate_results.py --batch-id <batch> --save <name>
```

No batch resume (restart re-runs the cell); full-DB cell ≈60–70 min at 8 workers, ~7× slower
under load; batch label = start unix time; **restart `main.py` after deploy** (pool workers
freeze engine code at spawn). `run_backtest()` returns a summary dict, not trades — read
per-trade logs from `backend/v2_results/`.

## Invariants (do not break)

1. **Pipeline parity** — `Backtester`/`ForwardTester`/`LiveTrader` must evolve the engine
   identically. Gate: `backend/analysis/test_live_parity.py`.
2. **4-state intra-candle expansion** — every candle = `open → extreme1 → extreme2 → close`
   (4× `engine.update()`). Never one update per candle.
3. **Signal-instant execution (iter73)** — live swaps on the signal state; BT default
   `exec_model="instant"` fills at that state's close ± slippage (`legacy` = retired n+1 model).
   `entry/exit_latency_seconds > 0` defers to `t_signal + latency` on the recorded path.
4. **Delay knobs live on the ENGINE** (`v2_entry_delay_seconds` 0.0; `v2_exit_delay_seconds`
   20.0 + `v2_exit_delay_armed_only` 1.0, iter83 ADOPTED 2026-09-12: full-DB Δ+5.06 p=5.6e-17,
   holdout-confirmed). 0.0 = byte-exact signal-instant. Batch cells pass only `--params`, so
   set ALL knobs explicitly in sweep cells; `shasum` the engine file around long burns.
5. **Complete decision streams** — BT force-closes open positions at tape end
   (`recording_ended`). Never drop unclosed trades.
6. **Determinism** across backtest/paper/live.
7. **Engine factory only** — `engine_factory.create_engine(version=1|2|3|4|6)`. V2
   `StrategyEngineV2Adapter` mirrors V1's interface. V3/V4/V6 non-default.
8. **DB isolation** — real data in `backend/data/*.db`; never write `backend/candles.db`.
9. **Result dirs** — `BACKTEST_RESULTS_DIR=backend/v2_results` for all V2 sweeps; do not prune.
10. **No orphaned pool workers** — every worker calls `guard_parent()`
    (`backend/process_watchdog.py`).
11. **Effective-basis tape (iter94)** — PumpSwap pools price on `vault +
    virtual_quote_reserves` (V = 17.5845 SOL protocol constant); every price the
    engine/chart/recorder/backtester sees for a graduated token must be on that
    executable basis. Live: `pool_virtual_reserves.py` V resolution + correction in
    `pumpfun_client._normalise` / `PumpSwapRPCClient`. Replay: `get_recording_candles`
    corrects pre-FIX_EPOCH recordings via `(pool_sol+V)/pool_sol`, pairCreatedAt-gated
    (curve-era prefix untouched). Escape hatches: `effective_basis=False` (loader param),
    `PUMPCHART_DISABLE_VR_FIX=1` (global). Never reintroduce raw-vault pricing.
    **Fill-calibrated V (iter95)**: the resolver can silently miss a pool (newest-pair
    selection, stale negative `v_sol=0.0` rows) — the trader reports every real fill to
    the hub (`report_fill_price`), which derives the pool's virtual reserves from the
    fill + live vaults and engages the same `vq` correction within 2 fills; the resolver
    can never clobber a fill-engaged calibration. Journal: `basis_calibrated`.
12. **Live calibration = the backtest call (iter95)** — `_get_or_create_live_session`
    calibrates via `initialize_calibration(mint, recording_anchor, {})` — identical to
    `run_backtest`; the dashboard `engineParamsV2` payload is BARRED from calibration
    keys (it re-sends stale values from a previous session's runtime broadcast, which
    once ran live on different physics than any replay). Regression signature:
    session_open kwargs with the defaults-triple (alpha=0.2 AND eta=0.1 AND
    lambda_0=6.9444e-05). tau_max varies by population window (15 morning / 30 evening
    both legitimate — the VR horizon follows the sample). **The population sample is
    immutable per cutoff (iter96b)**: membership requires `candle_count >= 100`
    (written atomically with stopped_at; 1–3 s retry recordings can never join) and
    the recordings cleanup clamps min_candles to 100 — a cleanup click must never
    retroactively change what a replay computes (2026-09-27 divergence: 4 of 6
    sessions ran different eta/lambda_0/lambda_mu than their own replays). The
    resolved calibration is journaled per session (`calibration_audit` in
    session_open) — diff it against a replay before blaming the engine.

## Architecture

```mermaid
graph TD
    Client[Browser: frontend/js/app.js] <-->|REST + WS| FastAPI[backend/main.py]
    FastAPI <--> Stream[pumpfun_client.py]
    Stream --> Agg[candle_aggregator.py]
    Agg --> Factory[engine_factory.py]
    Factory --> V1[strategy_engine.py]
    Factory --> V2[strategy_engineV2.py]
    Sub1[backtester.py] --> FT[forward_tester.py] --> Factory
    Sub2[live_trader.py] --> FT
    HF[holder_flow.py] --> Main & FT
    Data[data_store.py SQLite] --> Sub1 & HF
```

- **`main.py`** — REST (`/api/token/*`, `/api/recorder/*`, `/api/backtest*`, `/api/live/*`),
  WS (`/ws/{mint}`, `/ws/live/{mint}`, `/ws/autofeed`). Server-side live sessions (trader,
  stream, auto-recording, 1 s holder-flow pump + immediate exit dispatch, multi-engine fleet
  registry) survive tab closure. Backend restart does NOT auto-resume autofeed. `live_status`
  / portfolio aggregate cash/fees/rent/wallet-Δ alongside model PnL (iter95, GLM-FIX).
- **`pumpfun_client.py`** — mint/pool resolution + streaming (PumpPortal WS, pump.fun REST,
  RPC `accountSubscribe` vault-diff, DexScreener), `Semaphore(8)`. FD-leak fix (4731969):
  `stop()` force-closes the aiohttp session — do not remove. Hub resolves per-mint
  PumpSwap virtual quote reserves (iter94) so pool-era prints are on the executable basis.
- **`pool_virtual_reserves.py`** — iter94 V resolver (DexScreener pair → pool decode),
  sidecar cache (`pool_virtual_resolves` in price_data.db), `prewarm_mints()` for batch
  workers (hooked in `run_backtest_batch`). iter95 hazard: DexScreener's newest pair can
  be a pool created *after* the trades, and its decode can transiently read 0 — the
  stored negative row then silently disables the wedge; the fill-calibrated V in the hub
  supersedes it (live-side), and the hub's `_refresh` never clobbers an engaged
  calibration.
- **`candle_aggregator.py`** — trades → OHLCV with 4-state expansion.
- **`data_store.py`** — SQLite recordings + holder-flow persistence (`get_holder_flow_since`).
- **`holder_flow.py`** — insider/whale sells: realtime watcher (≥$100 stream sells tick-time +
  dev ATA subscribe) + legacy GMGN poll (fallback). CoinGecko SOL cached, off hot paths.
- **`forward_tester.py`** — shared execution simulator (slippage, `exec_model`, latency,
  `holder_flow_latency_seconds`, per-trade JSON).
- **`live_trader.py`** — Jupiter V1 Lite + `solders`. Blocked launches retry every
  state/boundary/settle; BUY may queue while prior trade settles. Mcap-floor breach =
  emergency sell + entry block + terminate (idle breach = immediate terminate). Fill-anchor
  booking (iter91b): books BT-identical fills, journals wallet truth as `cash_*`;
  first-buy account rent journaled separately (`rent_sol`), `exit_price_actual` = ledger
  price (iter94). Wallet truth (iter95, full-fix merge): `wallet_pnl_sol/pct` = cash_pnl −
  real buy fee (meta.fee via `_journal_buy_rent`, reconciles closed trades; NET persistent
  rent via `rent_sol_net` — in-tx-refunded WSOL accounts excluded); session wallet-truth
  stats (`total_cash_pnl_sol`, `total_fees_sol` measured both sides, `total_rent_sol` NET,
  `wallet_balance`, derived `wallet_delta_sol`); `starting_balance` set-once (balance-cache
  seed); `_resolve_landed_sell_sig` books retry-path fills under the true landed sig
  (`tx_delta_resolved`); reports every real fill to the hub for basis calibration;
  `basis_calibrated` journal event on engagement. Booking anchors verified BT-exact:
  entry = signal-candle OPEN, deferred exit = boundary-candle OPEN, loss-book exit =
  intrabar(frac≈0.505) — all ×(1±1%); live `_fill_fraction` reads the *configured* fee.
  Sparse-tape exit anchor (2026-09-28 koinu audit): latency-target seconds with no exact
  candle resolve via `_path_price_at` interpolation (ForwardTester `_resolve_latency_fill`
  mirror) instead of falling back to `_last_price` at settle.
- **`backtester.py`** — replay via ForwardTester + ProcessPool (`guard_parent`), persists to
  `backtest_data.db` + `v2_results/`.
- **`signal_capture.py` / `autofeed.py` / `newpairs*.py` / `process_watchdog.py`** — live
  signal capture (`data/live_logs/<session>/signals.jsonl`), server-side session auto-open,
  newborn recorder (separate DB, default OFF), orphan protection.
- **`analysis/`** — paired_diff, aggregate_results, test suite, per-iteration artifacts.
- **`frontend/`** — vanilla JS + LightweightCharts; `app.js::engineParamsV2` mirrors all knobs.
  Session/trader cards + portfolio show **Model PnL vs Wallet Δ** side by side, labelled
  (iter95, GLM-FIX); trade rows carry a Cash PnL column.

Removed (graveyard — need a new data channel to resurrect): futures, sniper, MSM/HMM gate (reverted
2026-09-11), iter57 Q-layer, whale-dump/SPE/pool-drain exits, V1 trailing stop, mayhem V7.

## Engines

- **V1** (`strategy_engine.py`): Langevin/Kalman (p, m); S=|m̂|/ATR_floor, barrier-adjusted
  S_eff; IDLE→TREND→EXHAUSTION→REVERSAL/CONTINUATION; 4-pillar confidence ≥0.79; blow-off-top
  guard, anti-chop, cold-start breakout. Math: `strategyV1.md`.
- **V2** (`strategy_engineV2.py`): latent [log-price, drift μ, log-vol h, flow φ, liquidity ℓ];
  OU drift/log-vol (anchored to observable EWMA r̄²/φ̄), adaptive measurement variance (Mehra),
  RBPF → topological regime + entropy confidence; volume-KDE potential U, Kramers k± →
  P⁺/P⁻/P⁰, Bayesian-majority direction, Kelly E* long gate.
- **V2 exit cascade** (first match): `tp_v2` → `gain_retrace` (armed +10%, exit at 50%
  give-back) → `breakeven_scratch` → `rate_split_flip` (split ≥0.55 × 12 ticks while armed)
  → `reversal_exit` → `kramers_down_exit` (P⁻≥0.5) → `bayesian_flip` → `kelly_flat`
  (no-long + E*≤0 × 60 ticks + ≥40% offside) → `evr_triage` (unconfirmed + buy-ratio<0.45 +
  ≥20% offside after 120 s, veto if max 1 s sell share >0.25). Plus BT `recording_ended`.
- **Production defaults** (`DEFAULT_CONFIG` authoritative; `app.js` must mirror):
  EVR ON (120 s/20%/0.45/veto 0.25) · HF entry gate OFF · dev-sell exit OFF (iter62 policy)
  · rate-split ON (10%/0.55/12) · kelly_flat ON (60/40%) · entry delay 0.0 · exit delay 20.0
  armed-only · warmup 100 · confidence_high 0.79 · popcal/mintcal/per-coin(gated) ON ·
  · execution (iter95): buy slippage budget 7000 bps, sell ladder ×2 at the session
  boundary (journal shows 2000), priority fee 100_000 µL/CU with sell escalation to
  400k (real fee ≈0.000037/rt — µL/CU ≠ flat lamports), wallet truth = cash_pnl − real
  buy fee (meta.fee) displayed everywhere, booked BT-basis kept for audits ·
  harvestcal OFF · **τ program = VR horizon** (`v2_tau_vr_enable/scale` 1.0/1.0, iter90e:
  `tau_max=clip(H*×scale,10,60)`; +3.935 p=0.00014, breadth 60%, holdout p=0.017).

## Holder flow

Events → `holder_flow` table in `price_data.db`; BT replays at on-chain timestamps; live
pre-loads + 1 s id-cursor pump with immediate exit dispatch. Gates **OFF** (iter62 policy;
`require_tag=0` = any ≥$100 sell qualifies when enabled). Whale stream events are anonymous;
GMGN tags sparse (12/44 iter43). Coverage: pre-iter72 recs ~6.7% of ≥$100 sells,
post-iter72 ~full.

## Benchmarking

- **Accept gate**: paired per-recording ΔPnL Wilcoxon p<0.05 + 10k-bootstrap 95% CI>0 +
  ≥50% breadth. Tail tools need tail tests too (~80% of tokens have no tail).
- **Re-baseline before comparing** — history in `v2_results/` was measured on older stacks
  (MSM/gates/delays changed 2026-09-11; calibration made causal 2026-09-17; the tape moved
  to the executable basis 2026-09-26 — iter94: pool-era prices were the raw vault ratio,
  understated by (vault+17.58)/vault). WR ~66% only cross-era invariant.
- **Traps**: 10× notional + restart-reset counters break live-vs-BT headlines (use iter67
  journal method); `v2_results` JSONs are survivor-conditioned; pool workers freeze code at
  spawn; thin tapes need random-sample probes; subset burns can't reconstruct engine-native
  layers (iter86c); UI batches re-fit calibration — like-for-like replay needs session kwargs.
  Live calibration MUST come from `initialize_calibration(mint, anchor, {})` — the
  dashboard `engineParamsV2` payload re-sends stale runtime values (Luna 11-vs-8
  divergence; invariant 12). UI batches re-fit calibration — like-for-like replay needs
  session kwargs.

## Testing / Ops

- `test_live_parity.py` (parity gate), `test_exit_delay_hold_reset.py`,
  `test_signal_capture.py`, `test_client_fd_leak.py`,
  `test_pool_virtual_reserves.py` (iter94 effective-basis gate),
  `test_display_truth.py` (iter95 wallet-Δ gate), `test_first_buy_rent_preflight.py`,
  `test_population_determinism.py` (iter96b sample-immutability gate).
  Standard run = command block above. Rot note (verified 2026-09-28 on the full-fix
  merge, cross-checked against pristine main-fix/GLM-fix worktrees): failures in
  `test_sell_quote_prefetch.py` (13) + `test_real_entry_basis.py` (4) +
  `test_confirm_sell_keeps_exact_wallet_chain_pnl` target the stashed iter92/93
  `buy_wallet_delta_sol` API (never merged) — see iter94/95 in RESEARCH_LOG before
  resurrecting; `test_first_buy_rent_preflight.py` 2 failures + percoin
  `TestMintHistoryLayer` OperationalError are pre-existing flakes. **Fixed 2026-09-28
  (full-fix)**: `test_overnight_gap_exit_anchor_matches_forward_tester` had pinned a real
  live-vs-BT booking gap — the booked fee modelled both fee sides (0.002 rate) while the
  BT's trade.pnl reflects only the close-side fee (total_fees_per_trade = 0.0001 absolute
  at the 0.1 reference ⇒ 0.001 rate; the open-side fee hits balance, not pnl). Booked fee
  is now basis × 0.001; booked pnl ≡ BT replay.
  Bare `ForwardTester()` defaults `slippage_pct=10.0`
  vs 1.0 in `run_backtest`/live — always pass slippage explicitly in harnesses.
- Deploy: restart `main.py` + hard-refresh browser (app.js cached — **bump `?v=` on every
  app.js edit and `node --check` it before deploying**; an unparenthesized `??`/`||` mix
  kills the whole dashboard). Live audits use
  `backend/data/live_logs/<session>/` (`trades.jsonl`, `signals.jsonl`,
  `intake_audit.jsonl` — logs `basis_calibrated` when the fill-calibrated wedge
  engages), not dashboard counters.
- **Hourly parity guard** (`automation-294c0b66`, hourly :13): `parity_monitor_check.py`
  scans journals → initialize({}) replay → on-chain tx verification → wallet balance;
  on ANY divergence it stops autofeed + `stop_all`, reports, and deletes itself
  (self-pause). Re-arm via CronCreate; re-init the cursor with `--init` after any
  pipeline change. Wallet floor: keep ≥0.015 SOL or buys die Custom:1.
- Fill forensics: `analysis/iter94_wedge_study.py` (on-chain fill vs tape),
  `analysis/iter94_validate_fix.py` (post-fix fill/PnL parity per session).
  GLM-FIX audit trail (reference only, gitignored): `analysis/GLM-FIX/` (forensic
  scripts + `test_display_truth.py` — fails on main by design, gates GLM-FIX API)
  + 15 `*_iter95_141244_*.json` in `v2_results/`; note in RESEARCH_LOG.
- External: pump.fun/Cloudflare blocks curl (use browser API-proxy); DexScreener 30-mint
  batches; DefiLlama newest `dexs/solana` row is copy-forward; CoinGecko SOL cached 60 s.
- Stash hazard: stale `ce4a316`-era stash merges into selective pops — check
  `git stash list`; restore via `git checkout HEAD -- <path>`.
