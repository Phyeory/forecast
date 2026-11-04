# Refactor Report

Behaviour-preserving refactor of `pump-chart` against the brief in
`REFACTOR_PROMPT.md`. This report records the safety net, the baseline, every
seam, and every deliberate deviation.

## Safety net

### Golden harness (built first, on pristine `88c0719`)

* `backend/tools/goldenlib.py` — runs one backtest cell (recording x engine
  version x exec model) through the unmodified `run_backtest` pipeline with
  only its output sinks patched:
  * `forward_tester.create_engine` is wrapped so every `engine.update()`
    result dict is hashed (sha256 over a canonical sorted-key JSON dump);
  * `backtester.create_backtest` captures the persisted candle series hash and
    the resolved (calibration-merged) engine params instead of writing
    `backtest_data.db`;
  * `backtester._write_trade_log` captures the full-precision trade list
    instead of writing `v2_results/`.
  No production code is edited; the computation path is byte-identical to
  production.
* `backend/tools/golden_capture.py` — `--scan` (candidate probe), `--select`
  (corpus rules), `--capture` (baseline fixtures).
* `backend/tools/golden_verify.py` — re-runs cells (parallel workers) and
  prints the first differing JSON path. Floats compare bit-exactly (repr
  round-trip preserves doubles).
* Pure fixtures (no DB, no network): `ForwardTester`/`LiveTrader`
  `_intrabar_price` / `_fill_fraction` / `_path_price_at` parity over fixed
  grids (asserted equal, not just recorded), `CandleAggregator` on a seeded
  500-trade synthetic tape at 1s/5s/15s,
  `correct_candles_to_effective_basis` on synthetic candles around a real
  sidecar graduation boundary (corrected / post-fix identity / disabled
  identity / wedge-factor grid), and the corpus-wide exit/entry reason sets.

### Corpus

Fixed in `backend/tools/golden/recordings.json` (committed): 12 recordings x
engines {1,2,3,4,6} x exec models {instant, legacy, latency} = 180 cells.
Selection: era spread (pre/post `PUMPSWAP_VR_FIX_EPOCH`), two highest-volume,
two mid, two low, one zero-trade, one `gain_retrace` exit, one
`recording_ended` close.

Corpus deviations from the brief (data facts, verified against the DBs):

1. **No curve-era recording exists.** Zero completed recordings start before
   their pool's `pair_created_at` (closest gap: 64 s after). The
   pairCreatedAt gate (curve-era prefix untouched) is covered by the
   synthetic effective-basis fixture instead.
2. Latency cells pin explicit `entry_latency_seconds=5.0` /
   `exit_latency_seconds=10.0`; `instant`/`legacy` cells run pure defaults so
   engine-keyed delay knobs take their production path.
3. V2 cells pass `v2_chain_fetch_enable: 0.0`. The production default
   (`1.0`) triggers a bounded network fetch + write to
   `mint_history_candles` whenever a mint lacks local history, which would
   mutate shared state mid-capture and make goldens non-reproducible.
   Calibration itself stays on its deterministic DB path (mint history if
   present, else population).
4. `warmup` etc. all default; calibration is recomputed per cell exactly as
   `run_backtest` does it.

### Test-suite baseline (measured on the pristine tree)

`88c0719`, this machine, standard run (AGENTS.md ignore list plus the two
collection blockers `test_recording_db_switch.py`,
`test_stream_fallback.py`, which import removed APIs):

```
49 failed, 322 passed, 14 skipped in 200.43s
```

(The brief's recorded 320/51/14 ignored only the two blockers; the extra 4
ignored modules are the AGENTS.md standard ignores
`test_live_trader_balance`, `test_timeout_hypothesis`, `test_iter98_fill_attribution`,
`test_iter100_tuner` — accounting for the 2-failure difference. The full
failure triage against a pristine worktree was already done by the repo's
own 2026-10-23 audit: every failure is pre-existing rot; none are touched by
this refactor.)

Golden baseline: 183 fixtures (180 matrix cells + 1 holder-flow extra cell +
`pure.json` + `exit_reasons.json`), 968 KB, committed under
`backend/tools/golden/baseline/`. Pristine-tree verify self-check:
**183 passed, 0 failed** (parallel, 8 workers, ~11 min full corpus;
`--subset` for the inner loop).

Coverage note: the corpus exercises `buy_idle`, `buy_trend`, `buy_exhaustion`,
`buy_reversal`, `buy_continuation` entries and `continuation_exit`,
`gain_retrace`, `kelly_flat`, `kramers_down_exit`, `rate_split_flip:armed`,
`recording_ended` exits. Later-cascade V2 exit strings (`tp_v2`,
`breakeven_scratch`, `evr_triage`, `bayesian_flip`) did not occur in any
corpus cell; the reason-set fixture catches renames of the strings that do
occur.

## Seams

### Seam 1: golden harness + pristine baseline (no production edits)

Harness, corpus, and baselines as described above. Commit `237bddf`.

### Seam 2: one 4-state expansion + one fill-pricing implementation

`core/state4.py` now owns `candle_states`, `intrabar_price`, `path_price_at`,
`record_path_candle`; `core/fills.py` owns the legacy fill fraction (with the
`REFERENCE_FEE`/`REFERENCE_SIZE` constants). The backtester loop,
`ForwardTester`, and `LiveTrader` all delegate; the four hand-copied
expansions (backtester inline, FT `_intrabar_price`, LT `_intrabar_price`,
LT `_process_completed_candle`) and the FT/LT byte-mirror pricing twins are
gone. Live/BT parity is now structural: both pipelines call the same code.

Deleted as dead: `ForwardTester._slippage_cost_buy/_sell` (zero callers;
slippage is computed inline where used). `ForwardTester._REFERENCE_FEE/
_REFERENCE_SIZE` moved to `core.fills`. Golden corpus 183/183 PASS.
Commit `d0cd80f`.

### Seam 3: one knob registry

`core/config.py` is the single source for every engine knob default:
`V2_ENGINE_KNOBS` (76, original key order), `V2_ADAPTER_KNOBS` (the 34
pop-only defaults), `V3/V4/V6_KNOBS` (54/24/15). The four engine modules
derive `DEFAULT_CONFIG = family_defaults(...)` — keys, values, and dict
order verified identical to the originals programmatically before the
rewrite. All 59 convertible `engine_kwargs.pop("key", <literal>)` sites in
the V2 adapter now read `v2_default(key)`; the `warmup_bars` `None`-sentinel
pop keeps its shape. Cross-family differences (`warmup_bars` 400 vs 120 vs
60) preserved verbatim — renaming keys would break the dashboard mirror and
`engine_params` journals, so it is out of scope by the byte-identity rule.
Net −185 lines. Golden corpus 183/183 PASS. Commit `5288676`.

Scope cut, recorded: the dashboard (`app.js engineParamsV2`) still mirrors
knobs by hand; the `/api/engine/schema` endpoint and generated form are
frontend work (seam 11) and were not started.

### Seam 4: one adapter base for the engine forks

`engines/adapter_base.py` (171 lines) holds the V1-compat init surface
(`_init_v1_compat` 57 constants + `_init_v1_state` + `_init_v1_indicators`,
identical across V3/V4/V6) and the helpers whose bodies are byte-identical
across engine copies (verified by AST + dedented source equivalence classes,
not by eye): `_price_overextended` and `_momentum_past_peak` (V2/V3/V4/V6),
`_confidence_lerp`, `_is_chop_zone`, `_global_stoploss_pct`,
`_update_peak_price` (V3/V4/V6 or V4/V6 subsets). V2 inherits only the two
helpers byte-identical to the merged copy and keeps its 9 divergent ones
explicitly. The brief's "byte-identical 3x" claim was inexact: `stoploss_pct`
and `takeprofit_pct` init values differ per family, so the compat init takes
them as parameters. `engine_factory.py` (158→90) became one
`_ADAPTER_PATHS` table + one loader; the fake `_V2_IMPORT_LOCK` boolean (not
thread-safe, its RuntimeError unreachable — no engine module imports the
factory) removed as dead code. Net −299 lines. Attribute-equivalence probe:
110 V1-compat attrs across V1/V2/V3/V4/V6 byte-identical; 19 moved method
bodies verified byte-identical to the originals. Golden corpus 183/183
PASS. Commit `82e799a`, cherry-picked as `e27c3c7`.

### Regression fixes the safety net caught

Commit `22bed72` records both; they are documented here because they are
part of the refactor's history:

1. **Seam 2 introduced a `NameError` in the live trader.** The rewritten
   state-4 call passed a nonexistent `mcap_usd` local (the parameter is
   `market_cap_usd`). The golden corpus cannot see the live trader — this
   was caught by the pytest baseline (`test_signal_capture`), which is
   exactly why the brief mandates running it after risky seams. The
   incident validates the two-layer gate design.
2. **Seam 5 broke the `data_store.PRICE_DB` runtime repoint contract.**
   Freezing the DB path into a module-scope `RecordingStore` at import made
   tests that retarget `PRICE_DB` to a scratch database hit the real one.
   `_price_store()` now rebinds on path drift; single-path production
   behaviour is unchanged. Caught by `test_engineV4V6` +
   `test_live_parity` (the latter via a `RecursionError` from an over-eager
   first patch, fixed before commit).

### Deferred deletions landed

The 8 zero-caller `strategy_engineV2` functions (second-derivative grid,
single-step RBPF, repeated-posterior helpers, scalar mean, `particles_list`,
`_silverman_bandwidth`, `regime_name`) were deleted after seam 4 landed
(they live in a file the engine seam owned). −171 lines. Commit `795558b`.

### Seam 5: one recordings store, two facades

`backend/stores/recordings.py` — `RecordingStore(db_path, schema)` with the
schema variants (init DDL, migration lists with per-statement error
semantics) copied verbatim; `data_store.py` (606→486 lines) and
`newpairs_store.py` (266→164) are thin facades over two module-scope
instances; zero call-site changes. The backtest-DB section and the four
holder-flow functions stay in `data_store.py` verbatim (a parity test
monkeypatches `data_store._get_price_read_conn`). Authoritative difference
inventory kept in the seam commit message. Golden corpus 183/183 PASS +
30/30 read-only equivalence probe. Commit `edfd101`, cherry-picked as
`5263ca6`.

Package-name deviation from the brief's target layout: the brief wants a
`backend/data/` package, but `backend/data/` is the on-disk DB directory
(git-tracked files inside it, symlinked into refactor worktrees) — the
package is `backend/stores/` instead.

### Seam 6: router split

All 51 REST routes + 4 WS moved verbatim from `main.py` (3,316→1,494 lines)
into `app/routers/{chart,recordings,backtests,live,autofeed,newpairs}.py`
plus `app/deps.py` for shared process state. The recordings routes use ONE
`recordings_router(prefix, store, default_min_candles)` factory for the
price and newpairs groups; the duplicate-verb aliases (POST+DELETE on
`finish_stale`/`cleanup`) all survive. Proofs: route inventory (method,
path, handler) identical including ORDER; **`/openapi.json` byte-identical**
(the factory mirrors the original bottom-up dual-verb registration);
`on_startup`/`on_shutdown` lists identical; 13 read-only TestClient probes
+ WS close codes identical before/after in one process; moved function
bodies AST-identical except 6 intended late-import lines and
`app.state`→`request.app.state`. Commit `f88b57a`, cherry-picked as
`d52630a`.

### Seam 8: engines expose snapshot(), signal_capture formats it

`AdapterBase.snapshot()` (plus V1/V2/V3/V4 overrides where their state
genuinely differs) returns the full decision state as a flat dict;
`signal_capture.py` reads it (with a byte-exact getattr fallback per field)
and formats the same journal records. The warmup-floor magic `400` now lives
in the engine's own snapshot where the gate lives. Equivalence probe: 5
engines × 10 capture points (incl. a full exit cascade and warmup-knob
variants) — journal output byte-identical (`cmp` clean).
`test_signal_capture` 11/11. `forward_tester._capture_entry_params/_exit`
left untouched (different key names/rounding — not trivially equivalent;
noted). +502/−284. Commit `5caf3c5`, cherry-picked as `981e62e`.

### Seam 9: every swallowed exception states itself

47 `except Exception: pass` sites + 33 extra silent swallows = 80 sites
across 12 files now carry a `logger.debug` (3 get `logger.warning` at the
DB-write / wallet-journal / sidecar-poisoning sites), or a why-comment where
logging is meaningless. Zero control-flow change: no narrowing (it would
change the error path), no retries, no new fallbacks; `except Exception`
stays at task/loop boundaries. Gate grep: 0 remaining in scope. 14 lifecycle
idioms (`asyncio.TimeoutError`/`CancelledError`/`WebSocketDisconnect`)
left as the exception type IS the end state. Commit `d2da729`, cherry-picked
as `b770327`.

### Seam 7: one candidate-feed base, two feeds

`feeds/feed_base.py` — `CandidateFeed` owning the shared lifecycle
(`set_config`, `is_running`, `start()` skeleton, `stop()` drain, state block
of the snapshot, backpressure accounting); `autofeed.py` (990→945) and
`newpairs.py` (526→494) keep every genuinely different piece verbatim
(loops, dedupe state machines, acceptance gates, forwarder details, exact
snapshot key order, exact log strings and logger names). `main.py` needed no
edits. Equivalence probe: 40/40 checks (snapshot key order and values,
config results, lifecycle results, log bytes). `test_autofeed_tune.py`
24 passed before and after. Commit `ef458a0`, cherry-picked as `a1bce92`.

## Found but not fixed (pre-existing bugs)

Logged during the work; none touched, per the brief.

1. **V1 + holder-flow recordings crash.** `StrategyEngine` (V1) has no
   `set_holder_flow_events`, but `ForwardTester.__init__` calls it whenever a
   recording has holder-flow rows: any V1 backtest on such a recording raises
   `AttributeError`. (Discovered by the corpus scan; constrained corpus
   selection.)
2. **`delete_recording` orphans holder-flow rows.** `ON DELETE CASCADE` is
   declared but `PRAGMA foreign_keys` is never enabled, so holder-flow rows
   survive recording deletion forever. (Seam 5 agent.)
3. **`insert_candles_batch` `INSERT OR REPLACE` never replaces.** No UNIQUE
   constraint on `(recording_id, time)`, so it inserts duplicates; the
   documented origin of the multi-row buckets `get_recording_candles` dedupes
   by `MAX(id)`. Single `insert_candle` does a true delete-then-insert — the
   two paths disagree. (Seam 5 agent.)
4. **`candle_count` vs cleanup measure different things.** `stop_recording`
   stores the raw row count; `cleanup_small_recordings` counts
   `COUNT(DISTINCT time)`. (Seam 5 agent.)
5. **AutoFeed backpressure is dead.** `main.py` wires a sync
   `_autofeed_active_count`, but `AutoFeed._tick` awaits it; awaiting an
   `int` raises `TypeError`, swallowed to 0, so the concurrency cap can never
   trip. (Seam 7 agent.)
6. **`autofeed.py` imports `Iterable` unused**; `AutoFeed` remains a
   600+-line class. (Seam 7 agent.)
7. **`prewarm_mints` poisons its own sidecar on transient failures** — a
   DexScreener batch failure writes every mint in the chunk as a
   `batch-negative` row that later reads as resolved; a getMultipleAccounts
   failure writes `pair_created_at` with `v_sol=0`, silently disabling the
   effective-basis correction for that mint. (Seam 9 agent.)
8. **`_sidecar_load` has no TTL on negative rows**, so a poisoned row
   persists indefinitely. (Seam 9 agent.)
9. **V6 `_compute_trail_stop_price` omits the `abs()`** every other engine
   applies — equal only while `v6_scratch_tol >= 0`; a negative tolerance
   would RAISE the trail stop. (Seam 4 agent.)
10. **V4/V6 `_maintain_v1_indicators` freeze different attribute subsets**
    on a non-positive close (V4 gates `p_hat` behind `c > 0`, V6 does not).
    (Seam 4 agent.)
11. **V6 never initialises `_market_cap_usd`/`_pool_sol`** though its
    `update()` accepts them (it ignores them). (Seam 4 agent.)
12. **V2 `_merge_config` docstring claims case-insensitivity** but performs
    none (brief §5.8 confirmed). (Seam 4 agent.)
13. **AOE journal fields (`aoe_*`) are dead** — `_v2_aoe_*` exists on no
    engine; the capture block is kept for fallback parity. (Seam 8 agent.)
14. **V1/V3 journal warmup floor omits the `max(..., 400)` clamp** the
    engine itself applies, so a dashboard `warmup<100` reports a floor
    lower than enforced. (Seam 8 agent.)
15. **V3 `_has_recent_dev_sell` takes 2 args**; the capture's 3-arg call
    always falls into its except and would record "no_recent_dev_sell" if
    the V3 HF gate were ever enabled. (Seam 8 agent.)
16. **`GET /static/js/{filename}` is dead code** — the `/static` Mount
    shadows it, so JS is served without the route's intended no-cache
    headers. (Seam 6 agent.)
17. **`GET /api/token/{mint}` has no resolver timeout** unlike every other
    `resolve_input` call site — a hung resolver blocks the request
    indefinitely. (Seam 6 agent.)
18. **The dead `key_str` fallback in `_get_or_create_live_session`**
    (brief §6: `if not private_key: return` guarantees the fallback chain
    cannot fall through). (Seam 6 agent.)
19. **`main.py:363` swallows poll-loop errors with only a sleep**, and
    **live_trader had a dead `finally: pass`** after an
    `except Exception: return`. (Seam 9 agent.)
20. **`DEFAULT_SOL_USD = 70.0` vs `_SOL_USD_FALLBACK = 200.0`** remain
    contradictory (brief §6 item; the unified `core/prices.py` cache is
    planned but not yet landed). (Seam 9 agent.)

## Deliberately left alone

* The pre-existing pytest failures (49 in the standard run; triaged against
  a pristine worktree by the repo's own 2026-10-23 audit; separate
  project). The only sanctioned deltas: `test_autofeed_tune.py` (+24
  passing) moved into `analysis/`, and 2 regressions introduced and fixed
  during this refactor (see the regression section).
* Engine numerics, RNG order, dict key order, exit-reason strings, DB
  schemas, HTTP shapes.
* `warmup` pop default (100) vs `DEFAULT_CONFIG["warmup_bars"]` (400): both
  preserved exactly as they behave today; unifying them is a behaviour
  change and belongs to the research pipeline, not this refactor.
* The 12 oversized functions/classes (`execute_sell` 725 lines,
  `StrategyEngineV2Adapter.__init__` 540, `build_report` 555,
  `_detect_regime` 505, `_LiveSession.run` 451, `_monitor_trade` 325,
  `_queue_signal_from_state` 243; `LiveTrader` 5,096-line class,
  `ForwardTester` 832-line class, `HolderFlowMonitor` 814,
  `AutoFeed` 659, `StrategyEngineV4Adapter` 411) and the
  module-size violations (`strategy_engine.py` 2,279, `trade_report.py`
  1,580, `session_calibrator.py` 1,009, `forward_tester.py` 986,
  `holder_flow.py` 1,157, `autofeed.py` 946). `tools/check_gates.py`
  enumerates them mechanically. Splitting these is real engineering on the
  hottest live-path code in the repo and deserves its own reviewed seams,
  not tail-end work under a deadline.
* The 15 `global` statements in production modules (the `_price_store`
  rebind is a deliberate runtime-repoint contract; the fleet/session
  registries want an explicit `AppState` object — same seam as the class
  splits).
* `paper/`, `notes/`, `audit_report/` (documents, not code — the brief
  says ask before deleting; pending the user's call).
