# Refactoring Brief — `pump-chart`

> **You are a senior software engineer doing a behaviour-preserving rewrite of a live-trading
> codebase.** The system streams Solana memecoin trades, aggregates 1-second candles, runs
> stochastic strategy engines, executes real money swaps, records everything to SQLite, and
> replays recordings through backtests. It is in production and it trades real SOL.
>
> Your job is **not** to improve the strategy. It is to make the code obvious, small, and
> maintainable **without changing a single number it produces**.

---

## 0. Repo facts you must internalise first

| Fact | Value |
|---|---|
| Working dir | `/Users/jaime/pump-chart` |
| Python | `backend/.venv/bin/python` (3.13.1) — the repo venv, **not** system python |
| Backend modules | 28 `.py` at `backend/` root, **60,361 lines** |
| Frontend | `frontend/js/app.js` 4,068 · `frontend/index.html` 913 · `frontend/css/style.css` 614 |
| Largest files | `live_trader.py` 5,762 · `strategy_engineV2.py` 4,449 · `main.py` 3,316 · `strategy_engine.py` 2,278 · `pumpfun_client.py` 1,655 · `trade_report.py` 1,579 |
| Functions | 752 total; 47 have no textual caller (24 test functions + 23 dead production functions, listed in §6). REST handlers are registered by decorator, not by call |
| HTTP surface | 47 REST routes + 4 WebSocket endpoints, all declared in `backend/main.py` |
| Config surface | `DEFAULT_CONFIG` 76 keys · `app.js engineParamsV2` 99 keys · **60 `engine_kwargs.pop()` sites** |
| Test suite | 42 modules under `backend/analysis/` — **currently red and unreliable**, see §2 |
| Git | `main` @ `88c0719`, clean tree |

Read these before writing code: `AGENTS.md`, `RESEARCH_LOG.md` (skim for the invariants only),
`strategyV1.md`, `strategyV2.md`.

**Do not read the 8,977-line original research log. It is archaeology, not documentation.**

---

## 1. The prime directive

> **Every observable output must be byte-identical before and after.**
>
> If a change would alter one float, one exit-reason string, one JSON key, one DB row, or one
> ordering, **it is out of scope**. Stop, write it down as a separate bug report, and move on.

This constraint is not perfectionism. The engines compute barrier probabilities from a
particle filter seeded by numpy RNG; a reordered summation or a `numpy → python` conversion
changes the posterior in the 4th decimal, which flips a borderline signal, which changes the
trade list. The repo's entire research history (`v2_results/`, 937 recordings, adopted
"iter" cells with p-values) is meaningless if the tape shifts.

Concretely, **you may not**:

* reorder floating-point arithmetic, replace `sum(xs)` with `math.fsum`, or change
  `a * b + c` grouping;
* change numpy dtype/promotion, RNG call order, or `rng_seed` handling;
* change the number or order of `engine.update()` calls per candle (always exactly 4);
* change any string literal that leaves the process: exit reasons, event names, journal keys,
  JSON field names, WS message `type` values, DB column names;
* change the DB schema (existing `price_data.db` / `backtest_data.db` / `newpairs_data.db`
  files must open and replay unchanged);
* change HTTP routes, query-parameter names, or response shapes.

You **may**: rename locals, extract functions, split files, delete dead code, unify duplicated
branches, replace `except Exception: pass` with explicit handling that behaves identically on
the happy path, and delete anything provably unreachable.

---

## 2. Build the safety net before you touch anything

The existing test suite **cannot be your gate**. Verified on a clean tree at `88c0719`:

* `pytest analysis/` aborts at collection: `test_recording_db_switch.py` imports a
  non-existent `recording_mode`; `test_stream_fallback.py` imports a removed
  `DexScreenerFallbackStream`.
* Ignoring those two, the recorded full run is **320 passed / 51 failed / 14 skipped in
  3m13s**, and all 51 failures were triaged against a pristine worktree as pre-existing.
* It does not always reproduce that runtime. A run of the same command on this machine was
  still going after 30 minutes at 56%, so at least one module blocks on network, a DB, or a
  process pool. Treat wall-clock as unbounded and run tests with a per-test timeout.
* Several test files are untracked and pin APIs that do not exist at HEAD.

Re-measure the baseline yourself and record the exact numbers in `REFACTOR_REPORT.md` before
you change anything. Do not "fix" the 51 failures as part of this work: they are a separate
project, and changing them makes the goldens untrustworthy.

So: **step 1 is to build a golden-output harness, not to fix tests.**

### 2.1 Golden corpus

Create `backend/tools/golden_capture.py` and `backend/tools/golden_verify.py`.

Pick a fixed list of ~12 recording ids from `backend/data/price_data.db` that spans:

1. bonding-curve era (pre-migration) · 2–3. PumpSwap era, one pre-`FIX_EPOCH` and one post-fix ·
4–5. the two highest-volume recordings · 6–7. two mid-activity · 8–9. two low-activity ·
10. one that produces zero trades · 11. one with a `gain_retrace` exit · 12. one that ends
with an open position (exercises `recording_ended`).

Store the id list in `backend/tools/golden/recordings.json` **and commit it**, so the corpus
never drifts.

### 2.2 What to capture

For every recording × engine version `{1,2,3,4,6}` × exec model `{instant, legacy, latency}`:

```
golden/<rec_id>/v<ver>/<exec_model>.json
```

containing, in a deterministic key order:

* the full `stats` dict returned by `run_backtest()` (all floats via `repr()`);
* the complete trade list — every field of every trade, including `entry_params`,
  `exit_params`, `entry_reason`, `exit_reason`, `pnl_sol`, `pnl_pct`, timestamps;
* the persisted candle series (`persist_candles=True`) when the run is a single backtest;
* a `sha256` of the concatenated per-state `engine.update()` result dicts.

Also capture, separately and cheaply (no DB, no network):

* `ForwardTester._intrabar_price` / `_fill_fraction` / `_path_price_at` over a fixed grid of
  (o,h,l,c,frac) tuples;
* `LiveTrader._intrabar_price` / `_fill_fraction` — must equal the ForwardTester values
  exactly (this is the parity invariant);
* `CandleAggregator` output for a synthetic 500-trade tape at each timeframe;
* `pool_virtual_reserves.correct_candles_to_effective_basis` on a fixed synthetic candle set;
* every exit-reason string emitted across the corpus (a sorted set — catches accidental
  renames).

### 2.3 Gate script

`backend/tools/golden_verify.py` must:

* exit non-zero and print the **first** differing JSON path, not a wall of diff;
* run in <5 min for the full corpus (use `--subset` for the inner loop);
* be the *only* thing you trust during the rewrite.

**Run it once on a pristine tree and commit the output as `golden/baseline/`. If it ever
fails after a refactor step, you revert that step — you do not "re-baseline".**

### 2.4 Live-execution goldens

`LiveTrader` cannot be replayed without a wallet. Instead:

* Extract the pure decision/sizing/pricing functions out of `LiveTrader` first (§5.2) and
  golden them in isolation.
* Keep one scripted harness that stubs `aiohttp` + Jupiter with recorded responses and drives
  `execute_buy` / `execute_sell` through the success, quote-failure, and partial-fill paths,
  asserting the exact sequence of journal events.

---

## 3. Definition of "byte identical"

For backtests:

```
golden_verify == PASS
```

means, per recording: identical trade count; per trade identical `entry_time`, `exit_time`,
`entry_price`, `exit_price`, `size_sol`, `pnl_sol`, `pnl_pct`, `entry_reason`, `exit_reason`
at full float precision; identical `stats` dict; identical candle-series `sha256`.

For live behaviour: identical engine state per state-tick, identical signal/exit reason per
signal, identical journal event sequence. Timing (wall-clock) is allowed to differ; **decisions
are not**.

If you find a refactor that is provably behaviour-preserving but changes a float in the 17th
significant digit (e.g. `repr` formatting), that is still a failure — fix the formatting, not
the baseline.

---

## 4. Target architecture

Layer the code so a new engineer can answer "where does X live?" without grep.

```
backend/
  app/
    server.py          # assembles FastAPI, mounts routers, starts/stops background loops
    deps.py            # process-wide state: semaphores, locks, registries (explicit, injected)
    routers/
      chart.py  recordings.py  backtests.py  live.py  autofeed.py  newpairs.py  portfolio.py
  core/
    types.py           # Candle, Trade, Signal/Regime/Direction enums, TF constants
    state4.py          # THE 4-state intra-candle expansion (single implementation)
    fills.py           # intrabar price path, fill fraction, slippage, fees — all pipelines
    config.py          # single source of truth for every knob + its default + its type
    errors.py
  engines/
    contract.py        # the engine interface every pipeline speaks
    adapter_base.py    # shared V1-compat surface (the 60-line block, written once)
    v1.py v2.py v3.py v4.py v6.py
    factory.py
  pipelines/
    forward_tester.py  # the fill simulator (single source of execution semantics)
    backtester.py      # replay driver + persistence
    live_trader.py     # live driver
    live/              # jupiter.py, swap.py, watchdogs.py, reconcile.py
  data/
    db.py              # connection factory + schema/migrations
    recordings.py      # ONE recordings store, parameterised by database
    backtests.py
    holder_flow_repo.py
  feeds/
    pumpfun_client.py  # transports only
    holder_flow.py
    autofeed.py newpairs.py   # both on a shared FeedBase
  reporting/
    trade_report.py
  tools/
    golden_capture.py golden_verify.py
frontend/js/
  main.js api.js state.js charts.js format.js
  pages/{chart,recordings,backtests,live,autofeed,newpairs,portfolio}.js
```

**Dependency rules** (enforce with a tiny import-lint test):

```
app       → pipelines, data, feeds, engines, core
pipelines → engines, data, core            (NEVER → app, NEVER → feeds)
engines   → core                            (NEVER → data, NEVER → feeds, NEVER → app)
feeds     → core, data
core      → stdlib + numpy only
```

`engines` must stay importable without FastAPI, aiohttp, or solders installed.

Reach the layout in two stages: first extract *logical* units in place (small diffs,
goldens green), then do a final mechanical `git mv`-only commit. Never combine a move with a
behaviour edit.

---

## 5. The duplication catalogue — this is the actual work

Ordered by risk × payoff. Each item names the evidence so you can verify it yourself.

### 5.1 The 4-state expansion exists **four times** (highest risk)

The invariant is: every candle is fed to the engine as exactly
`open → extreme1 → extreme2 → close`. That sequence is hand-copied in four places:

| Site | Code |
|---|---|
| `backend/backtester.py:423-467` | `bullish = c >= o` … 4× `ft_update(...)` |
| `backend/forward_tester.py:384` | `_intrabar_price` path interpolation |
| `backend/live_trader.py:4799` | `_intrabar_price` — comment literally says *"Byte-mirror of forward_tester … do not diverge"* |
| `backend/live_trader.py:5162-5240` | `_process_completed_candle`, 4× `engine.update()` with 4 copy-pasted "propagate earliest signal" blocks |

**Fix:** one module `core/state4.py` exposing

```python
def candle_states(o, h, l, c) -> tuple[State, State, State, State]   # (o,o,o,o), (o,h2,l2,m1), ...
def intrabar_price(o, h, l, c, frac) -> float
def path_price_at(segments, target_ts) -> float
```

Every pipeline iterates `candle_states(...)`. The live trader's `_fill_path_candles` and the
forward tester's `_path_candles` become one buffer class.

The 4 repeated "if `final_signal` is None" blocks in `_process_completed_candle` collapse to a
loop with `first_signal(...)`.

> This is the single change that makes live/BT parity structurally impossible to break.

### 5.2 `LiveTrader` re-implements `ForwardTester` by hand

`forward_tester.py` is documented as the read-only fill contract, and `live_trader.py`
re-derives it:

* `_intrabar_price` (dup), `_fill_fraction` (dup with its own `_REFERENCE_FEE` /
  `_REFERENCE_SIZE` constants), `_path_price_at` (dup), `_record_path_candle` (dup);
* entry-basis anchoring, deferred-exit two-tier freeze, boundary lookups;
* `iter91/94/96` comments repeatedly say "mirrors the backtester".

**Fix:** move the *pure* parts into `core/fills.py` + a `FillPath` class. Both pipelines call
it. `LiveTrader` keeps only I/O (RPC, signing, retries) and state transitions.

### 5.3 Engines V3 / V4 / V6 are copy-paste forks of each other

Measured duplication between `strategy_engineV3.py`, `V4`, `V6` (2,269 lines total):

* an identical ~60-line `__init__` block assigning fake V1-compat constants
  (`self.ema_fast_p = 3`, `self.max_entry_bar_count = 5700`, `self.forbidden_bc_lo = 0`,
  `self.atr_floor_k = 0.0`, … 50 more) — present **3×, byte-identical**;
* an identical machine-state block (`self.regime`, `self.direction`, `self._peak_price`, …)
  — present **3×**;
* 12 methods implemented 4–5× across all engines, several at ratio **1.000**:

  `_confidence_lerp`, `_price_overextended`, `_momentum_past_peak`, `_is_chop_zone`,
  `_global_stoploss_pct`, `_effective_stoploss_pct`, `_effective_takeprofit_pct`,
  `_compute_trail_stop_price`, `_merge_config`, `_passes_engine_version_check`,
  `_update_peak_price`, and `_maintain_v1_indicators` (56–107 lines each, 4 copies);
* `engine_factory.py:44-100` — four identical `_load_vN_adapter()` lazy loaders, plus a
  `_V2_IMPORT_LOCK` boolean that pretends to be a lock (it is not thread-safe).

**Fix:** `engines/contract.py` (the interface), `engines/adapter_base.py` (one copy of the
V1-compat surface + all 12 helpers), and each engine overrides only what is genuinely
different. `factory.py` becomes a dict of `{version: import_path}` with one loader.

**Do not** touch the numerics inside `MemecoinStrategyEngine`, `RaoBlackwellisedParticleFilter`,
`MarketPotential`, or `StrategyEngine._detect_regime`. Extract helpers; do not re-derive math.

### 5.4 Two identical SQLite stores

`backend/newpairs_store.py` (262 lines) is a subset of `backend/data_store.py` (606 lines):
same 13 functions (`create_recording`, `stop_recording`, `insert_candle`,
`insert_candles_batch`, `list_recordings`, `get_recording`, `get_recording_candles`,
`delete_recording`, `cleanup_small_recordings`, …), differing only by `NEWPAIRS_DB` vs
`PRICE_DB`. `insert_candles_batch` is 99% identical.

**Fix:** one `data/recordings.py` with a `RecordingStore(db_path, schema_variant)` class.
Two instances at module scope if you must keep call sites stable (`data_store.list_recordings()`
→ `price_store.list_recordings()`).

### 5.5 The recordings REST API is written twice

`main.py:2763-2797` mirrors `main.py:407-503` endpoint-for-endpoint:

```
/api/recordings            ↔ /api/newpairs/recordings
/api/recordings/{id}       ↔ /api/newpairs/recordings/{id}
/api/recordings/{id}/candles ↔ ...
DELETE /api/recordings/{id} ↔ ...
/api/recordings/cleanup    ↔ /api/newpairs/recordings/cleanup
```

**Fix:** one router factory `recordings_router(prefix, store, default_min_candles)`.

Also delete the duplicate-verb routes: `/api/recordings/finish_stale` is registered for both
`POST` and `DELETE`; so is `/api/recordings/cleanup`. Pick one verb, keep the other as a
deprecated alias only if the frontend calls it (check `app.js` first).

### 5.6 `autofeed.py` and `newpairs.py` are the same state machine

`set_config` is byte-identical (`autofeed.py:360` ≡ `newpairs.py:200`). `start()` is 84%
identical. Both implement: candidate list → dedupe → gate → spawn forwarder → status snapshot.

**Fix:** `feeds/feed_base.py` with `CandidateFeed` (lifecycle, config merge, snapshot,
backpressure). Subclasses implement `_discover()` and `_accept(candidate)` only.

### 5.7 The SOL/USD price is cached twice, with two different fallbacks

* `holder_flow.py:183` `_get_sol_usd()` (async, CoinGecko, 60 s TTL, fallback `200.0`)
* `holder_flow.py:831` `HolderFlowMonitor._cached_sol_usd()` (sync, same TTL, same fallback,
  spawns a refresh task)
* `trade_report.py:38` `DEFAULT_SOL_USD = 70.0` — contradicts both

**Fix:** one `core/prices.py` with `SolUsdCache` (read/write/refresh) and one constant.
`trade_report` takes the rate as an argument rather than inventing a third default.

### 5.8 The dashboard exposes knobs the engine silently drops

Cross-checking `DEFAULT_CONFIG` against `app.js engineParamsV2`:

* **37 keys exist only in the frontend** — including `stoploss_pct`, `takeprofit_pct`,
  `confidence_high`, `confidence_low`, `v2_rate_split_enable`, `v2_require_past_peak`,
  `warmup`, `gain_retrace_arm_pct`, `breakeven_arm_dd_pct`, `reversal_exit_bars`.
  Some of these *are* consumed — but through `engine_kwargs.pop("…", <default>)` inside
  `StrategyEngineV2Adapter.__init__`, not through the dict. Others are `float(cfg[...])` reads.
  The routing is invisible, so nobody can tell which UI control is wired.
* **13 keys exist only in Python** (`logprob_floor`, `rng_seed`, `ticks_per_state`,
  `v2_chain_fetch_enable`, `v2_harvest_arm_min/max/scale`, `v2_harvestcal_enable`,
  `v2_percoin_cal_*` …) — not editable at runtime.
* **34 pop-defaults are not in `DEFAULT_CONFIG` at all**, and **29 keys carry the default
  twice** (`DEFAULT_CONFIG["v2_evr_eval_delay"] = 120` *and*
  `pop("v2_evr_eval_delay", 120)`).
* At least one default is genuinely inconsistent: `warmup` pops at **100** while
  `DEFAULT_CONFIG["warmup_bars"]` is **400**.
* Dead legacy knob still live: `iter05_decay_window`, `iter05_decay_window_thresh`,
  `iter05_decay_entry_block`.
* `_merge_config`'s docstring claims "case-insensitive" — it performs no such normalisation.

**Fix:** `core/config.py` is the *only* place a default exists:

```python
@dataclass(frozen=True)
class Knob:
    name: str
    default: float | int | str
    kind: Literal["float", "int", "bool", "str"]
    group: str           # dashboard section
    doc: str             # one sentence, becomes the tooltip
    scope: Literal["engine", "adapter", "pipeline"]
```

There are **four** `DEFAULT_CONFIG` dicts today, one per engine family, plus V1's inline
constructor defaults:

| File | Line | Keys |
|---|---|---|
| `strategy_engineV2.py` | 142 | 76 |
| `strategy_engineV3.py` | 125 | 54 |
| `strategy_engineV4.py` | 63 | 24 |
| `strategy_engineV6.py` | 65 | 15 |

The same knob name carries a different default in each: `warmup_bars` is **400** in V2,
**120** in V4, and **60** in V3/V6 (the adapters divide by 4 because V2 counts 4-state ticks
while V3/V4/V6 count candles). That is not a bug today, but it is unreadable and one edit away
from becoming one. Name them `warmup_states` and `warmup_candles` in the registry, keep both
values, and make the conversion explicit in one place.

`ENGINE_KNOBS: dict[str, Knob]` should hold ~110 entries (the 76 + the 34 currently inline).
The engine reads `cfg.<name>` only; `__init__` accepts `**kwargs`, validates against
`ENGINE_KNOBS`, and raises `UnknownKnob` for anything else **in tests only** (production
tolerates extras for forward-compat with the dashboard, but logs once).
The dashboard's parameter form is **generated from a `/api/engine/schema` endpoint** — the
99-key `engineParamsV2` literal in `app.js` is deleted.

> Until that endpoint exists, keep `app.js` hand-written but make the *defaults* come from
> the server on load. Behaviour must not change.

### 5.9 The diagnostics layer re-derives the gate ladder

`backend/signal_capture.py` (747 lines) recomputes the engine's entry/exit gates to record
"every decision value vs its threshold":

* `collect_signal_fields` — 339 lines, one `if` per gate, thresholds written as literals;
* `warmup_floor = max(int(warmup_bars), 400)` at line 363 — the engine's own warmup rule,
  copy-pasted with a magic 400;
* `collect_coefficients` (151 lines) and `collect_engine_state` (66 lines) overlap heavily
  with `forward_tester._capture_entry_params` (137 lines) and `_capture_exit_params`
  (78 lines).

**Fix:** each engine exposes `snapshot()` returning its full decision state as a flat dict.
`signal_capture` *formats* that dict; it does not recompute anything. `_capture_entry_params`
also reads `snapshot()`. One source of truth for "what the engine believed".

### 5.10 Small but pervasive

* `{"1s", "5s", "15s"}` is a literal set in `main.py:238`, `:681`, `:1047` while
  `pumpfun_client.SUB_MINUTE_TFS` already exists. Use the constant.
* Stream-client selection (`PumpSwapRPCClient` / `DexScreenerPollClient` / `PumpFunWSClient`
  by `_live_source`) is written 3× in `main.py` (238, 681, 1047) — one
  `build_stream_client(token_info, timeframe)` factory.
* `resolve_input` + `_resolve_sem` + 20 s timeout + "fall back to the raw mint" is written
  3× in `main.py`.
* `main.py` has **two** `@app.on_event("startup")` handlers (lines 144 and 474) and one
  shutdown handler. Replace with a FastAPI `lifespan` context manager — same ordering.
* `app.js` creates a LightweightCharts instance in 6 places (1086, 1523, 1939, 3515, 3988,
  and the live card) and repeats the volume-series mapping 4× verbatim:
  `c.close >= c.open ? "#26a69a33" : "#ef535033"`. One `createCandleChart(el)` helper.
* `formatMcap` / `fmtLarge` / `fmtTs` / `fmtDuration` are duplicated in intent across
  `app.js` and the HTML inline block.

---

## 6. Delete list (verify, then remove)

Confirm each with a grep before deleting; report anything ambiguous instead of guessing.

| Item | Evidence |
|---|---|
| `backend/audit_v1.py` | 1,779 bytes, zero functions, **zero imports anywhere** |
| `run_batch.py`, `run_batch_v2.py`, `run_iter.py` | superseded by `run_iteration.py`. `run_iter.py:27` calls `re.sub` but never imports `re` — it raises `NameError` on first use |
| `backend/test_autofeed_tune.py` | 365 lines living at module root, invisible to the `analysis/` run; move into `analysis/` or delete |
| 23 dead production functions | `candle_aggregator.get_last_n`, `data_store.get_holder_flow_for_window`, `forward_tester._slippage_cost_buy/_slippage_cost_sell`, `holder_flow.get_recent_sells/has_recent_dev_sell/get_events_as_dicts`, `live_session_logger.bind_record`, `live_trader._get_cached_token_balance/_get_tx_rent_sol/confirm_failed`, `mint_chain_history.load_chain_candles`, `newpairs.age_seconds`, `session_calibrator.summarize_population`, `signal_capture.collect_signal_record`, `strategy_engineV2._second_derivative_grid/_rbpf_step_single/_posterior_mean_repeated/_posterior_var_repeated/_repeated_scalar_mean/particles_list/_silverman_bandwidth/regime_name` |
| Dead branch in `main.py:2278` | `if not private_key: return None, False` at 2264 guarantees `key_str = private_key or getattr(app.state, "lt_private_key", "") or _load_backend_private_key()` can never fall through. Either the guard is wrong or the fallback is — **investigate, then keep exactly one** |
| `.github/workflows/market-paper-trading.yml` | belongs to a different project (`market` branch, US equities via Alpaca). Not part of this app |
| `yfinance>=0.2.0`, `python-binance>=1.0.0` in `backend/requirements.txt` | imported nowhere in this repo |
| `backend/sniper/` (empty), `backend/market_backtest/` (pycache only), `backend/backend/v2_results/`, `degen/` (pycache only), `paper/`, `notes/`, `audit_report/` | stray artefacts. **Ask before deleting `paper/`, `notes/`, `audit_report/` — they are documents, not code** |
| `DEFAULT_SOL_USD = 70.0` vs `_SOL_USD_FALLBACK = 200.0` | contradictory constants, one of them is wrong |
| 62 `except Exception: pass` | see §7.3 — keep the behaviour, make the intent explicit |

Do **not** delete `strategy_engineV3/V4/V6` without asking. They are reachable via
`engine_factory` and the dashboard's fleet selector, even though V2 is production default.

---

## 7. Engineering standard

### 7.1 Size and shape

| Rule | Limit |
|---|---|
| Function body | ≤ 60 lines, ≤ 4 levels of nesting |
| Class | ≤ 400 lines |
| Module | ≤ 800 lines |
| Cyclomatic complexity | ≤ 12 |

Current violations you must fix (these alone justify the rewrite):

* `live_trader.execute_sell` — **725 lines, nesting depth 7**
* `strategy_engineV2.StrategyEngineV2Adapter.__init__` — **540 lines**
* `trade_report.build_report` — **555 lines**
* `strategy_engine._detect_regime` — **505 lines**
* `main._LiveSession.run` — **451 lines**
* `live_trader._monitor_trade` — **325 lines, nesting depth 8**
* `main._NewPairSession.run` — **270 lines**
* `live_trader._queue_signal_from_state` — **243 lines, nesting depth 7**

Split `execute_sell` along its documented phases (preflight → balance → mutex → quote →
escalation groups → settle → journal), each a method of a `SellExecutor` collaborator.

### 7.2 Naming and structure

* One concept, one name. Pick `entry` **or** `open`, `exit` **or** `close`, `mint` **or**
  `token_mint` — not all of both across files.
* No prefixes like `_v2_`, `iter91_`, `_stashed_`, `_effective_` on *public* attributes.
  Keep them only where the string is part of an external contract (journal keys, config keys).
* Booleans are `is_*` / `has_*` / `should_*`.
* Every public function has a one-line docstring stating intent, not mechanics.
  Non-trivial functions add a short paragraph on *why*, not *what*.
* Typed signatures on every function in `core/`, `engines/contract.py`, `data/db.py`.
  `from __future__ import annotations` everywhere for consistency.
* No `global`. Process-wide state becomes an explicit object passed to the constructor
  (`AppState`, `FleetRegistry`). The 19 current `global` statements are all replaceable.

### 7.3 Error handling

Current state: 62 `except Exception: pass`, 0 bare `except:`. The bare-`pass` pattern hides
real failures (a swallowed `KeyError` in the recorder looks identical to "no data yet").

Rules:

1. Never swallow silently. If the behaviour is "ignore and continue", write
   `logger.debug("...")` and keep a counter, or comment **why** it is safe.
2. `except Exception` is only allowed at a task/loop boundary (a background loop must not die).
   Everywhere else, name the exception types you expect.
3. Never `except` around a DB write or an on-chain call without journalling the failure —
   these are the two categories where silence costs money.
4. Preserve current *behaviour*: if today a failure is ignored, it stays ignored; you only
   add observability.

### 7.4 Resource handling

* `sqlite3` connections: one factory, `contextmanager`, always closed. Today every call opens
  a fresh connection; keep the semantics but centralise it so `PRAGMA`/`row_factory` cannot
  drift between the three stores.
* `aiohttp.ClientSession`: one per component, owned by that component's lifecycle, closed in
  `stop()`. `pumpfun_client` already learned this the hard way — do not regress it.
* No `import` inside a hot function. `data_store.get_recording_candles` lazily imports
  `pool_virtual_reserves` on every call — hoist it (watch for a circular import; if one
  appears, that is the architecture telling you the dependency is inverted).

### 7.5 Logging

* `logger.info` for lifecycle, `debug` for per-tick, `warning` for degraded, `error` for
  "a human must look".
* No f-string concatenation of 6 fields on one line. Use structured extras:
  `logger.info("session start", extra={"mint": m, "engines": v})`.
* No `print()` in library code (18 remain). CLI entrypoints may print.

---

## 8. Comment policy — this is a hard requirement

The codebase reads like a commit log pasted into source. Measured:

* **462** occurrences of `iterNN` markers inside executable modules
* **92** comments containing a calendar date or incident name (`2026-08-26 MILKERS`,
  `2026-08-25 logs`, `2026-09-28 consolidation`, `Brezy49c`)
* **489** box-drawing banner lines (`# ─── …`, `# ═══ …`)
* **636** em-dashes inside comments
* **207** lines longer than 160 characters
* Extensive narrative in the imperative-voice style of generated text

### 8.1 What to delete outright

```
# ── iter57 parity fix: delivery is by DB row id (lossless), NOT by counting the
# monitor's in-memory list.  The monitor trims `recent_events` to a 60 s
# window, so a count-based diff desynchronises the moment an
# event ages out — every event landing below the stale high-water mark
# was silently never delivered to the engine (empirically only 8–25 %
# of events reached it).  The DB rows are persisted at discovery time
# and are exactly what the backtester later replays, so an id-cursor
# read makes the live engine's event stream identical to backtest.
```

```python
# ── Intra-candle 4-state expansion (matches ForwardTester exactly) ──────────
# iter91: feed the fill-path buffer (the backtester's Step-0
# _record_path_candle writes the same four progressive state tuples
# per candle) so boundary-anchor lookups price on the recorded path.
```

These are *changelogs*, not comments. They answer "what did we fix and when", which belongs
in `RESEARCH_LOG.md` and in `git log`.

### 8.2 What to write instead

State the invariant, in ≤2 lines, in the present tense, with no dates and no narrative:

```python
# Deliver events by DB row id, not by list position: the monitor trims its
# buffer to 60 s, so a count-based cursor silently skips aged-out events.
```

```python
# One candle feeds the engine as four states, in this order, everywhere.
# Replay, backtest, and live must not diverge here.
```

### 8.3 Rules

1. **No comment may restate the code.** `# increment the counter` is noise.
2. Comment the *why* and the *constraint*, never the *how*.
3. Max 2 lines per comment, 88 columns.
4. No dates, no iteration numbers, no person names, no incident names, no ticket ids in code.
   Those go in `RESEARCH_LOG.md`, referenced as `see RESEARCH_LOG.md#<anchor>` if truly needed.
5. No emoji anywhere in Python. No box-drawing banners. A single `# ---` section separator is
   acceptable at most once per 200 lines.
6. No em-dashes. Use a full stop, a colon, or a comma.
7. British/American spelling: pick one (the repo uses British: `normalise`, `initialise`).
8. Docstrings: first line is a single sentence ending in a full stop. No "Args/Returns"
   boilerplate unless the function genuinely has >3 parameters whose meaning is non-obvious.
9. Delete every docstring that explains history ("was previously", "the old code", "after the
   rewrite"). If the current behaviour needs justification, keep one sentence of *why*.
10. Every deleted paragraph of explanation must either be (a) obviously derivable from the
    code, or (b) moved to `docs/architecture.md`. Nothing load-bearing is allowed to vanish.

### 8.4 Also rewrite

* Module docstrings longer than 25 lines (`strategy_engineV3.py` opens with 51 lines of
  product narrative). Compress to: what it is, what it trades, what it must not do. Move the
  rest to `docs/engines.md`.
* `AGENTS.md` itself is a 17 KB operations log. Rewrite it as: commands, invariants (max 12),
  architecture diagram, and a short "history lives in RESEARCH_LOG.md" pointer.

---

## 9. Frontend

`app.js` is 4,068 lines in one IIFE with `af*`, `np*`, `pf*`, `lt*` prefixes as the only
module boundary.

1. Split into ES modules along those prefixes (they are already separate features).
2. One `api.js` wrapping `fetch` + WS reconnect; today each page re-implements retry and
   error toasts.
3. One `charts.js` with `createCandleChart(el)` and `createPnlChart(el)`.
4. Delete the 99-key `engineParamsV2` literal once `/api/engine/schema` exists; until then
   fetch defaults and merge overrides.
5. The 20-line inline `<script>` in `index.html` should move to `js/boot.js`.
6. No behaviour change: same element ids, same message types, same DOM structure.
   Verify with `node --check` on every file and a manual pass through all 7 pages.

---

## 10. Execution protocol

Work in **one seam per commit**, in this order. Every step ends with `golden_verify == PASS`
and a one-line summary in the commit body.

| # | Step | Risk |
|---|---|---|
| 1 | Commit the golden harness + baseline (no production edits) | none |
| 2 | `core/state4.py` + `core/fills.py`; repoint `backtester`, `forward_tester`, `live_trader` | **high** |
| 3 | `core/config.py`: single knob registry, remove 60 inline pop-defaults | medium |
| 4 | `engines/adapter_base.py`: collapse the 12 helpers + V1-compat block | medium |
| 5 | `data/recordings.py`: merge `data_store` + `newpairs_store` | low |
| 6 | Router split: `main.py` → `app/routers/*` (pure moves first) | low |
| 7 | `feeds/feed_base.py`: merge `autofeed` + `newpairs` | low |
| 8 | `signal_capture` reads engine `snapshot()` | medium |
| 9 | Error-handling pass (62 `except: pass`) | low |
| 10 | Comment/docstring rewrite, dead-code deletion | none |
| 11 | Frontend module split | low |
| 12 | Mechanical `git mv` into the §4 layout | none |

Rules for the loop:

* **Never** run two seams at once. A failing golden after a 900-line diff is undebuggable.
* If a seam cannot be made byte-identical, revert it and write the reason in the report.
* Commit messages describe the *invariant preserved*, not the mechanics.
* Do not rebase, squash, or force-push. Preserve the linear history — it is the audit trail.
* Do **not** run the full-DB batch (60–70 min, 8 workers) as a check. Use the 12-recording
  corpus. Full-DB batches are for research, not for CI.

### Verification commands

```bash
cd /Users/jaime/pump-chart/backend

# every edit
./.venv/bin/python -m compileall -q . 2>&1 | grep -v __pycache__
./.venv/bin/python tools/golden_verify.py --subset

# each seam
./.venv/bin/python tools/golden_verify.py

# import smoke: engines must not need aiohttp/fastapi
./.venv/bin/python -c "from engine_factory import create_engine; \
  [create_engine(v) for v in (1,2,3,4,6)]"

# server boots and serves
cd .. && ./start.sh   # then: curl -s localhost:8000/api/recordings | head

# frontend
node --check frontend/js/*.js
```

`start.sh` boots `main.py` on :8000 serving `frontend/`. **Pool workers freeze engine code at
spawn**, so restart the server after any deploy — irrelevant for static refactors, critical
if you change an engine.

---

## 11. Acceptance gates

The refactor is done only when **all** of these hold:

1. `golden_verify` passes for the full corpus, all engines, all exec models.
2. Both restart paths produce identical JSON: pre-refactor capture vs post-refactor capture,
   byte for byte (`cmp` on each file).
3. `python -c "import main"` succeeds with only `fastapi`, `aiohttp`, `solders` installed.
4. `main.py` is under 400 lines and contains **no** business logic — only app assembly,
   lifespan, and router mounts.
5. No function > 60 lines; no module > 800 lines. Verify with a script, not by eye.
6. `grep -rn "iter[0-9]" backend/*.py` returns **zero** hits.
7. `grep -rn "20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]" backend/*.py` returns **zero** hits.
8. `grep -rn "except Exception:\s*$" -A1 backend/*.py | grep -c "pass"` returns **zero**.
9. `grep -rn "global " backend/*.py` returns zero outside `tools/`.
10. Every knob's default appears exactly once in the repo.
11. A developer who has never seen the repo can go from `start.sh` to "where do I change the
    take-profit?" in under 3 minutes, using only `README.md` + `AGENTS.md`.
12. A live paper session runs 10 minutes with no traceback and a journal event sequence
    matching the pre-refactor reference.

---

## 12. Deliverables

1. The refactored tree, one commit per seam.
2. `backend/tools/golden_capture.py`, `golden_verify.py`, `golden/` fixtures.
3. `docs/architecture.md` — module map, dependency rules, one sequence diagram per pipeline
   (backtest, live, recording), and the "where do I change X" index.
4. `docs/engines.md` — one page per engine: what it trades, its gates in order, its exits in
   order, its knobs. The 49-line module docstrings move here.
5. `AGENTS.md` rewritten to ≤120 lines: commands, invariants, architecture pointer.
6. `REFACTOR_REPORT.md` — what you merged, what you deleted, what you deliberately left
   alone and why, and every behavioural difference you found but did **not** fix.

---

## 13. Forbidden moves

* Changing engine numerics, RNG order, dtype, or summation order.
* Changing exit-reason strings, journal keys, WS message types, DB schema, or HTTP shapes.
* "Fixing" a pre-existing bug inside the refactor. Log it; do not touch it.
* Deleting `strategy_engineV3/V4/V6` or the fleet feature without explicit approval.
* Adding a framework (pydantic models for everything, SQLAlchemy, an ORM, a DI container)
  because it "makes the code cleaner". The stack is what it is; simplify within it.
* Adding abstractions for a single call site. Two occurrences is not a pattern; three is.
* Rewriting tests to match new behaviour. Golden fixtures are the contract.
* Re-baselining goldens. Ever.
* Touching `v2_results/`, `backend/data/*.db`, or `backend/backtest_results/`.

---

## 14. First 30 minutes

1. `git status` (must be clean) → `git checkout -b refactor/behaviour-preserving`.
2. Read `AGENTS.md` §Invariants and §Commands. Read `backend/engine_factory.py`,
   `backend/forward_tester.py` (top 200 lines), `backend/backtester.py` (280–520).
3. Run the golden capture on the pristine tree. Read one output file end to end so you know
   what "identical" looks like.
4. Write `docs/architecture.md` **before** changing code — you cannot refactor what you have
   not described. If a module resists description in three sentences, that is your first seam.
5. Start at §5.1.

Do not skip step 4. The failure mode of this task is a large, confident diff that passes
"it compiles" and quietly shifts the tape.
