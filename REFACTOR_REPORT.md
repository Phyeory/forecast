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

(delegated to a worktree agent; to be integrated)

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

(pending)

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

## Deliberately left alone

* The 51→49 pre-existing pytest failures (triaged against a pristine
  worktree by the repo's own 2026-10-23 audit; separate project).
* Engine numerics, RNG order, dict key order, exit-reason strings, DB
  schemas, HTTP shapes.
* `warmup` pop default (100) vs `DEFAULT_CONFIG["warmup_bars"]` (400): both
  preserved exactly as they behave today; unifying them is a behaviour
  change and belongs to the research pipeline, not this refactor.

## Acceptance gates

(pending: run at the end of the seam series)
