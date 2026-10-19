# market_live — decision-identical live execution

The live stack runs the **same executor the backtester runs**. There is no
second implementation of the strategy to drift out of sync.

```
market_backtest.runner.run_market_backtest          market_live.trader
──────────────────────────────────────────         ─────────────────────
MarketForwardTester(exec_model="instant")     ───── same class, same kwargs
  for candle in candles:                            on_bar(candle):
    ft.update(open tick)                              ─────────────────────
    ft.update(first extreme)                          identical four calls
    ft.update(second extreme)                         identical OHLC per state
    ft.update(close tick + volume)                    identical order
  ft._close_long("recording_ended")                 host.force_close()
```

`test_live_parity.py` proves it field-by-field (entry/exit timestamps, fill
prices, size, tokens, PnL, exit reason, fees, final balance) on real tapes.
**Gate result: PASS** — stocks 1d (12 tapes) and crypto perp 1h.

## What the live layer adds (and only this)

| layer | effect on decisions |
|---|---|
| `k` sizing multiplier | none — scales notional only |
| broker adapter | none — executes the engine's target position |
| prop guard | can flatten/halt *after* the engine decides (risk overlay) |
| execution mode | `close` = exact; `next_open` = real-world, measured −0.47 pp / 2.8 y |

## Modules

| file | role |
|---|---|
| `host.py` | `MarketEngineHost` — one symbol, one FT, the four-state bar replay |
| `feeds.py` | closed-bar feeds: `TapeFeed` (replay), `YahooFeed`, `AlpacaFeed` |
| `brokers.py` | `PaperBroker` (ledger), `AlpacaBroker` (REST), `MT5Broker`/`HttpBridgeBroker` |
| `prop_guard.py` | firm templates + live rule enforcement (daily/max-loss/target/consistency) |
| `journal.py` | JSONL session journal (`session_open`, `bars`, `decisions`, `orders`, `equity`, `guard`, `heartbeat`) |
| `trader.py` | orchestration + `run_replay` dry-run |
| `run_live.py` | CLI |
| `test_live_parity.py` | the gate |

## Quickstart

```bash
cd backend
PY=./.venv/bin/python

# 1) prove parity on your own tapes
$PY -m market_live.test_live_parity --market stocks --interval 1d --limit 12

# 2) dry-run the whole stack over history (paper broker + prop guard)
$PY -m market_live.run_live replay \
  --market stocks --symbols AAPL,MSFT,NVDA,META,AMZN --start 2024-06-01 \
  --k 0.75 --firm ftmo_2step --results-dir market_backtest/results/arm14 \
  --overrides '{"gain_retrace_arm_pct": 14.0}' --exec-mode close

# 3) measure the execution wedge (close-fill vs next-open)
$PY -m market_live.run_live compare \
  --market stocks --symbols AAPL,MSFT,NVDA,META,AMZN,TSLA,GOOGL,JPM \
  --start 2024-01-01 --k 0.75 --results-dir market_backtest/results/arm14 \
  --overrides '{"gain_retrace_arm_pct": 14.0}'

# 4) live paper session (no orders leave the machine)
$PY -m market_live.run_live paper \
  --market stocks --symbols AAPL,MSFT,NVDA --k 0.75 --firm ftmo_2step \
  --results-dir market_backtest/results/arm14 \
  --overrides '{"gain_retrace_arm_pct": 14.0}'
```

## Going live

### Alpaca (US equities, personal or paper)
```bash
export APCA_API_KEY_ID=... APCA_API_SECRET_KEY=...
$PY -m market_live.run_live live --broker alpaca \
  --market stocks --symbols AAPL,MSFT --k 0.75 --firm ftmo_2step
```
Orders are sent as `market` / `time_in_force=cls` (market-on-close) so the
fill lands at the backtest's instant. Start on the paper endpoint
(`AlpacaBroker(paper=True)`, the default).

### MT5 / FTMO (CFD prop accounts)
Implement a 4-endpoint bridge on the Windows box that runs MT5:

```
GET  /equity                        -> {"equity": 100000.0}
GET  /position?symbol=AAPL          -> {"qty": 0.0}
POST /order   {symbol, qty, side}   -> {"price": 123.4, "id": "..."}
POST /flatten {reason}              -> [{"symbol": "AAPL", "qty": 1, "price": ...}]
```
Then:
```bash
$PY -m market_live.run_live live --broker mt5 --bridge http://127.0.0.1:8765 ...
```
`MT5Broker` is a preset of `HttpBridgeBroker`; the bridge is ~80 lines using
the `MetaTrader5` package (`order_send`, `positions_get`, `account_info`).

### Prop guard
`--firm ftmo_2step` enforces the published rules live:
* daily loss cap (flattens immediately on breach),
* static max loss (halts),
* profit target → phase pass,
* consistency (best day ≤ 50 % of total profit) → refuses payout eligibility,
* min trading days.

The guard never chooses entries; it can only block or flatten.

## Journals & audit

`backend/data/live_market/<session>/`:

```
session_open.json   config + code_commit/code_dirty  ← stale-process tripwire
bars.jsonl          every closed bar consumed
decisions.jsonl     per-bar desired position + fills (engine truth)
orders.jsonl        broker fills (account truth)
equity.jsonl        per-bar equity + guard status
guard.jsonl         phase passes, halts, consistency holds
heartbeat.jsonl     liveness + position snapshot
```

Audit rule: read the journals, not the dashboard. Compare `decisions.jsonl`
against `orders.jsonl` — any divergence means the broker missed a target.

## Kill-switches to wire before real money

1. **Parity drift** — re-run `test_live_parity.py` after every deploy; a stale
   process shows up as `code_dirty: true` + a missing commit in `session_open`.
2. **Edge decay** — rolling 90-day realised mean vs the backtest's +0.043 %/day.
   Halt if the rolling mean is negative for 2 consecutive months.
3. **Broker divergence** — any symbol where `desired_position` and the broker
   position disagree for > 1 bar.
4. **Rule proximity** — `daily_floor` within 1 % of equity → size down or
   flatten (the guard does this automatically; the journal records it).

## Intraday ("day trading") status

The stack is bar-class agnostic: `--interval 1h` / `5m` works end-to-end
(`BAR_SPEC["stocks_1h"]`, `stocks_5m`, costs, calibration, host, feeds).
What is missing is **data**: Yahoo is IP-rate-limiting this machine and Alpaca
needs free API keys. Provide keys and the command is:

```bash
$PY -m market_live.run_live replay --market stocks --interval 1h \
  --symbols AAPL,MSFT,NVDA ... 
```

Evidence so far says the edge is a *daily* phenomenon: the same engine on
crypto 1h bars is unfundable (test Σ −0.27 at arm14 vs +0.33 on stocks 1d),
and the daily strategy's hold time is 32 days. Day-trading the current
calibration is an open experiment, not a validated path.

## Daily operating loop (what actually runs)

```bash
# US equities close 16:00 ET.  Run the session shortly after, once per day.
35 16 * * 1-5 cd /path/to/pump-chart-market/backend && \
  ./.venv/bin/python -m market_live.run_live live \
    --broker mt5 --bridge http://127.0.0.1:8765 \
    --market stocks --symbols "$(cat symbols.txt)" \
    --k 0.75 --firm ftmo_2step \
    --results-dir market_backtest/results/arm14 \
    --overrides '{"gain_retrace_arm_pct": 14.0}' \
    --once >> logs/live_$(date +%F).log 2>&1
```

`--once` processes every newly closed bar, reconciles positions, checks the
guard, persists `guard_state.json`, and exits — restart-safe by design.

Weekly checks:
* `decisions.jsonl` vs `orders.jsonl` divergence (broker missed a target)
* `guard.jsonl` floors/phase (must match the broker's account page)
* rolling 60/90-day realised mean vs +0.043%/day (edge decay)
* re-run `test_live_parity.py` after any code change

## Recalibration (M11)

Candidates are generated offline; the live stack only ever reads an
**adopted** snapshot at session start:

```bash
./.venv/bin/python -m market_backtest.recalibrate_market snapshot \
    --asof "$(date +%F)" --selection stats_only --tag weekly_$(date +%Y%m%d)
./.venv/bin/python -m market_backtest.recalibrate_market report \
    --run market_backtest/results/recal/full_so/replay.json
./.venv/bin/python -m market_backtest.recalibrate_market adopt \
    --snapshot market_backtest/results/recal/weekly_20261025

./.venv/bin/python -m market_live.run_live live \
    --config market_live/config/prop_ftmo_100k.json \
    --calibration-snapshot market_backtest/results/recal/weekly_20261025 --once
```

Journal contract: `recalibration` (accepted, with version/asof/git commit),
`recalibration_rejected` (missing `adopted.json` / unreadable / empty), and
`heartbeat.jsonl` carries the live calibration version.  A missing snapshot
falls back to `results_dir` frozen params; it never crashes the session.

## Prop-account operating rules

1. **Withdraw every payout.** At k=0.75 the funded account blows ~33%/yr;
   withdrawn profit is the only profit that survives.
2. **Never re-enter after a halt.** `guard_state.json` keeps the halt sticky
   until a new account is funded.
3. **Run 2–3 parallel attempts**, not one — 93–98% chance at least one funds.
4. **Do not raise k to "pass faster".** Above k=1 the blow-up rate outruns the
   pass rate; EV peaks at 0.5–0.75.
