# Architecture

One FastAPI process streams Solana memecoin trades, aggregates 1-second
candles, feeds stochastic strategy engines, executes real swaps on PumpSwap
via Jupiter, records every candle to SQLite, and replays recordings through
backtests. Three pipelines (live, backtest, forward replay) must evolve the
engine identically; that parity is the system's core invariant.

Ops commands live in `AGENTS.md`. Research history lives in `RESEARCH_LOG.md`.

## Module map (refactor in progress)

The behaviour-preserving refactor (see `REFACTOR_REPORT.md`) is extracting
the logical units in place; the table below reflects the landed structure.

| Layer | Module | Role |
|---|---|---|
| app | `backend/main.py` | FastAPI assembly, lifespan/startup-shutdown, server-side `_LiveSession` / `_NewPairSession` run loops, fleet registry; routes moved to `app/routers/` |
| app | `backend/app/deps.py` | Process-wide state shared by routers (registries, locks, helpers) |
| app | `backend/app/routers/{chart,recordings,backtests,live,autofeed,newpairs}.py` | All 51 REST routes + 4 WebSockets; recordings uses one router factory for the price and newpairs groups |
| core | `backend/core/state4.py` | THE 4-state intra-candle expansion, `intrabar_price`, `path_price_at`, `record_path_candle` — every pipeline delegates |
| core | `backend/core/fills.py` | Legacy fill fraction + the reference fee/size constants |
| core | `backend/core/config.py` | Single source for every engine knob default (per-family tables; `DEFAULT_CONFIG` derived) |
| engines | `backend/engine_factory.py` | `create_engine(version, **kwargs)` via one `_ADAPTER_PATHS` table |
| engines | `backend/engines/adapter_base.py` | Shared V1-compat init surface + the byte-identical helpers |
| engines | `backend/strategy_engine.py` (V1), `strategy_engineV2.py`, `V3`, `V4`, `V6` | The engines; V2 holds `DEFAULT_CONFIG` (derived from core.config) and the production adapter |
| pipelines | `backend/forward_tester.py` | THE fill contract: `Trade`, stats, exec models, latency overlay |
| pipelines | `backend/backtester.py` | `run_backtest` replay driver, batch pool, persistence, trade JSON logs |
| pipelines | `backend/live_trader.py` | Live driver: Jupiter swaps, journals, deferred exits, boundary anchors, fleet mutex, watchdogs |
| data | `backend/stores/recordings.py` | One `RecordingStore(db_path, schema)`; `data_store.py` / `newpairs_store.py` are facades |
| data | `backend/data_store.py` | Facade + holder-flow functions + the whole backtest-DB section |
| data | `backend/pool_virtual_reserves.py` | PumpSwap virtual quote reserve (V) resolver, sidecar, executable-basis correction |
| feeds | `backend/pumpfun_client.py` | Trade streaming + mint/pool resolution (PumpPortal WS, REST, RPC, DexScreener) |
| feeds | `backend/feeds/feed_base.py` | Shared candidate-feed lifecycle; `autofeed.py` / `newpairs.py` subclass it |
| feeds | `backend/holder_flow.py` | Insider/whale sell watcher + GMGN fallback |
| support | `backend/signal_capture.py` | Per-decision value capture for live journals |
| support | `backend/session_calibrator.py`, `backend/calibration_startup.py` | SDE estimation (population / mint history) + causal init |
| support | `backend/trade_report.py`, `backend/live_session_logger.py`, `backend/rate_limiter.py`, `backend/process_watchdog.py` | Reports, journals, rate limiting, orphan-worker guard |
| ui | `frontend/js/app.js` | Vanilla-JS dashboard (single IIFE, not yet split) |

## Dependency rules (target)

```
app       -> pipelines, data, feeds, engines, core
pipelines -> engines, data, core            (never app, never feeds)
engines   -> core                            (never data, never feeds, never app)
feeds     -> core, data
core      -> stdlib + numpy only
```

Engines stay importable without FastAPI, aiohttp, or solders installed.

## The 4-state expansion

Every completed candle reaches the engine as four accumulated states, in this
exact order, in all three pipelines:

```
State 1: o, o, o, o          (open tick, volume 0)
State 2: o, H|L, L|H, mid1   (first extreme; bull bars high-first, bear low-first)
State 3: o, H, L, mid2       (both extremes)
State 4: o, H, L, c          (close tick, full volume + split)
```

`backtester.run_backtest` inlines it; `forward_tester._intrabar_price`
interpolates fills along the same path; `live_trader._process_completed_candle`
hand-copies it with four "propagate earliest signal" blocks. These must never
diverge: a different order changes which intra-candle state produces a signal,
which changes every trade.

## Sequence: backtest

```
run_backtest(recording_id, engine_version, exec_model)
  -> data_store.get_recording_candles        (VR correction to executable basis)
  -> data_store.get_holder_flow              (dev/insider events, on-chain timestamps)
  -> calibration_startup.initialize_calibration_sync   (V2 only: mint history
     or population SDE coefficients merged into engine_params; causal cutoffs)
  -> ForwardTester(engine_kwargs, exec_model)
       loop over candles: 4 x ft.update(state)     # 4-state expansion
         engine.update -> signal -> fill per exec_model
       force-close open position  ("recording_ended")
  -> stats + trade list -> backtest_data.db + v2_results/*.json
```

## Sequence: live

```
WS /ws/{mint}  or  autofeed candidate
  -> main._LiveSession.run
       pumpfun_client stream -> CandleAggregator (1 s) -> completed candle
       4-state expansion -> engine.update x4
       signal -> LiveTrader:
         quote (Jupiter) -> swap -> settle -> journal events
         deferred exits (engine delay knobs), boundary anchors, fill-path buffer
         fleet sell mutex, wallet reservations, watchdogs, steady-state resync
```

Server-side sessions survive tab closure; a backend restart does not resume
autofeed. Display truth: Model PnL vs wallet delta are reported side by side;
ATA rent is never folded into PnL.

## Sequence: recording

```
newpairs or live stream
  -> CandleAggregator -> insert_candle(s) into price_data.db (or newpairs_data.db)
  -> holder_flow events inserted alongside
  -> stop_recording sets status; replay reads via get_recording_candles
```

## Where do I change X

| Change | Location |
|---|---|
| A knob's default | engine `DEFAULT_CONFIG` (V2 = source of truth; `app.js::engineParamsV2` mirrors it for the dashboard) |
| Take-profit / stop logic | engine exit cascade (`strategy_engineV2` adapter + engine) |
| Fill price semantics (slippage, fees, latency) | `forward_tester.py` (the contract); `live_trader.py` must mirror, never fork |
| When a trade enters | engine entry gates; execution timing via `v2_entry_delay_seconds` |
| How candles reach the engine | the 4-state expansion sites listed above |
| What the recorder stores | `data_store.py` schema + `candle_aggregator.py` |
| A dashboard control | `frontend/js/app.js` (fetches REST; mirrors engine knobs) |
| Calibration | `session_calibrator.py` (estimation), `calibration_startup.py` (init/merge policy) |
| Effective-basis pricing | `pool_virtual_reserves.py` (`PUMPSWAP_VR_FIX_EPOCH`, sidecar, correction) |
