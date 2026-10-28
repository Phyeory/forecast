# Live Paper Evaluation — Market Strategy (arm14, Alpaca Paper)

Publication-ready record of the live paper-trading deployment of the V2 strategy engine on
daily US equities. Companion to `backend/market_backtest/` (backtest stack) and
`.github/workflows/market-paper-trading.yml` (runner). Started **2026-10-24**; this document
is updated as the evaluation accrues.

---

## 1. What is running

A daily-bar implementation of the same `StrategyEngineV2` used for the pump.fun project,
ported to stock OHLCV and executed through a paper broker:

| Parameter | Value |
|---|---|
| Universe | 67 US large-cap stocks (AAPL … XOM; see `session_open.json`) |
| Bar interval | 1d, **closed-bar** only (warm filter — no partial current bar) |
| Execution | `exec_mode="next_open"` — signals computed on a closed bar, filled at next open |
| Order type | Alpaca `time_in_force="opg"` (market-on-open) |
| Broker | Alpaca **paper**, $100,000 starting equity |
| Label / config | `arm14` — `gain_retrace_arm_pct=14.0` override on production defaults |
| Sizing | `k=0.75` Kelly fraction, `buy_size=0.1` (10 % of equity per position) |
| Risk guard | `ftmo_2step` template: phase-1 target +10 % ($10k), daily floor −5 % ($5k), max drawdown floor −10 % ($10k) |
| Restart safety | Runtime state snapshot (`engine_snap.bin.gz`, `state.json`, `guard_state.json`) restored each run |
| Runner | GitHub Actions, ~2 min/day, journals committed to branch `origin/market-live-journals` |

Session identity is pinned: `session_open.json` records `code_commit` and `code_dirty`
(Oct 24 local run: `b03d4acc…`, clean) — the AGENTS.md invariant that every session must
verify the engine it ran against HEAD.

## 2. Infrastructure (how to inspect)

- **Journal branch:** `origin/market-live-journals`, path
  `backend/data/live_market/<session_id>/`:
  - `session_open.json` — config, commit, symbols, overrides (the ground truth of what ran)
  - `bars.jsonl` — closed bars ingested per symbol
  - `decisions.jsonl` — per-symbol per-bar desired position + last price
  - `orders.jsonl` — submissions and fills (`signal_submit`, `next_open_fill`), with order ids
  - `equity.jsonl` — per-bar equity + guard status
  - `heartbeat.jsonl` — broker state + per-symbol warm/prep status
  - `guard.jsonl` — FTMO-guard init/events (`guard_init`, halts, phase progress)
- **Runtime state:** `backend/data/live_market/runtime/` (`state.json` = broker book +
  desired-position book; `engine_snap.bin.gz` = engine warm snapshot).
- **Runner:** workflow `market-paper-trading.yml`; secrets hold the (rotated) Alpaca keys.
  Two daily runs: ~00:24 UTC (warm/prepare) and ~07:41 UTC (decision pass).
- **Health check:** `gh run list` or the journals branch (no local Alpaca keys — credentials
  exist only as GH Actions secrets since the 2026-10 security rotation).

## 3. Session & trade history

| Date | Session | Action |
|---|---|---|
| 2026-10-24 | `20261024_135929` (local run) | First live session; engine prepared 67 symbols, 956 bars warmup each; no orders |
| 2026-10-26/27 | `20261027_070002` (GH) | ADBE buy 29 sh @ $258.60 submitted (`signal_1792987200` = Oct 26 bars, `opg`); apparently **did not fill** (broker book empty next morning) |
| 2026-10-28 | `20261028_074203` (GH) | ADBE buy 29 sh re-issued, booked $255.775, notional $7,417; order working at open. Desired book: 9 long signals (ABT, ADBE, HD, IBM, LOW, META, MSTR, NKE, PG) |

Equity at last check: **$100,000.00** (guard status `ok`, no drawdown events). First
mark-to-market will appear in `equity.jsonl` after the first fill settles.

## 4. Backtest baseline (what live is being measured against)

`backend/market_backtest/results/arm14/stats_arm14_stocks.json` — 67 symbols, 3.0-year span,
train/test split:

| Metric | Train | **Test** |
|---|---|---|
| Trades | 194 | **113** |
| Win rate | 73.7 % | **77.9 %** |
| Mean trade PnL | +0.189 % | **+0.288 %** |
| Bootstrap 95 % CI (per trade) | [0.031, 0.342] % | **[0.065, 0.498] %** |
| Wilcoxon p | 2.3e-06 | **5.6e-06** |
| Profit factor | 1.63 | **2.12** |
| Avg win / avg loss | +0.66 % / −1.14 % | +0.70 % / −1.17 % |
| Symbol breadth positive | 35/67 | 31/67 |

Portfolio-level (test): annualized **+10.9 %** on account, Sharpe 0.75, max drawdown
**13.3 %**, worst day −5.2 %, best day +9.6 %.

Note the shape of the edge: high win rate (78 %) with average loss ≈ 1.7× average win —
classic gain-retrace/exit-cascade profile. Many small wins, occasional larger losses. This
matters for evaluation (below): the equity curve is a slow grind with sudden −1 %+ hits.

## 5. How long before the live result is meaningful?

The limiting factor is **sample size, not calendar time**. The edge is small per trade
(+0.29 % mean, 0.78 % per-trade std), so a handful of trades cannot distinguish it from zero.

**Power analysis** (one-sample t-test on per-trade PnL, from backtest moments):

| Confidence / power | Trades needed |
|---|---|
| 95 % confidence, 50 % power | ~39 |
| 95 % confidence, 80 % power | **~45** |
| 99 % confidence, 80 % power | ~73 |

**Expected trade rate live:** 113 trades / 3.0 years ≈ **38 trades/year** portfolio-wide
(0.56 per symbol-year) at 0.1 sizing on 67 symbols. Observed live rate is consistent so far
(1 signal-driven entry in the first week; the desired book carries ~9 concurrent signals, but
entries only execute when a fresh signal coincides with a session).

**Answer:**

- **~6 months** — ~20 trades: directional read only. Enough to catch gross divergence from
  backtest (fill quality, sign of the mean), not enough to confirm the edge.
- **~12 months** — ~38 trades (one backtest-equivalent year): first credible read; the
  95 %/50 % power threshold is approached. If the live mean is tracking the backtest this is
  where it starts to show.
- **~14–15 months** — ~45 trades: **the primary decision point.** 95 % confidence, 80 % power
  to confirm mean per-trade PnL > 0.
- **~2 years** — ~73 trades: publication-grade confirmation (99 % / 80 % power).

Do **not** stop early on a good streak: with 78 % win rate, P(≥10 straight wins at start) is
nontrivial, and 10 trades carry a confidence interval roughly ±0.5 % per trade — wider than
the edge itself.

## 6. Decision criteria (pre-registered)

Judge live against the backtest test-segment expectation, not against raw profit alone:

1. **Trade count ≈ expectation** — materially fewer trades than ~0.72/week suggests the live
   pipeline (warm filter, closed-bar logic, opg handling) is dropping signals.
2. **Win rate within 77.9 % ± ~10 pts** (binomial noise at n≈40 is ±12 pts).
3. **Mean per-trade PnL within the backtest bootstrap CI [0.065 %, 0.498 %]** — live mean
   inside this band = consistent with the edge; below 0 at n≥45 = edge did not transfer.
4. **Profit factor ≥ 1.5** and guard never hits the −10 % floor.
5. **Execution parity** — opg fills should land at the opening price; compare
   `orders.jsonl` fill prices vs next-bar `open` from `bars.jsonl`. Persistent slippage vs
   the open is the single most likely live-vs-backtest gap (backtest fills at bar open exactly).
6. **No journal anomalies** — every `session_open.code_commit` == HEAD, `code_dirty` false,
   no duplicate positions across sessions (Oct 27 unfilled-opg case is the known hazard:
   verify one order → one fill → one position).

**Pass:** ≥45 trades, mean in-band, PF ≥ 1.5, drawdown within backtest envelope (13 %),
parity checks clean → the algorithm is confirmed profitable at the backtest's reported
magnitude, and the result is publishable as an out-of-sample live confirmation.

## 7. Known caveats for the writeup

- **Paper execution, simulated queue:** market-on-open paper fills approximate, not
  guaranteed, real opening-auction prints. Costs modeled: commission $0, no slippage knob on
  the opg path — real-world transferability will be overstated.
- **Two runs/day, state restart:** the 00:24 UTC run is warm/prepare only (no orders, no bars
  in journal — expected). Decision pass ~07:41 UTC, pre-open; opg orders bridge the gap.
- **Signal conditioning:** backtest metrics are from the same 3-year tape the parameters were
  selected on (train/test split mitigates but does not eliminate selection). The live window
  is the only truly untouched sample — that is precisely why it is worth running the full
  14+ months.
- **Concurrent-signal book:** desired positions persist in `state.json`; a signal not acted
  on in one session can execute in a later one (reason tag `signal_<unix_ts>` shows the
  originating bar — Oct 26 signal executed Oct 28).
- **Guard is prophecy, not history:** `guard.jsonl` starts at pristine $100k each session
  until equity events accrue; phase progress lives in `runtime/guard_state.json`.

## 8. Publication checklist

- [ ] Export per-trade table from `orders.jsonl` + `equity.jsonl` (entry/exit, hold time, PnL, guard status)
- [ ] Equity curve vs backtest test-segment expectation curve (same starting $100k)
- [ ] Fill-quality study: opg fill vs bar open (criterion 5 above)
- [ ] Rolling mean per-trade PnL with 95 % CI vs trade count (the "how sure are we" figure)
- [ ] Divergence log: every parity check that fired (Section 6)
- [ ] Session integrity table: commit hash per session, dirty flags, restarts
- [ ] Final stats block mirroring Section 4's table, live column beside test column
