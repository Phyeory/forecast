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

(one section per seam, in execution order)

## Deliberately left alone

## Found but not fixed (pre-existing bugs)

## Acceptance gates
