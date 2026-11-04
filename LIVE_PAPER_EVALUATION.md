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
| 2026-10-30 | `20261030_075231` (GH) | No orders. The Oct 28 `opg` order was canceled unfilled (broker buying power restored to $400k, no position), but the engine still carries `desired: ADBE=1` (+ 8 others) and does not re-submit — a desired-vs-broker desync deadlock. Broker truth: **flat, $100,000, zero fills after 6 sessions** |
| 2026-10-30 20:38 ET | `20261031_003818` (GH) | **Fixed code runs.** `warm_sync` adoption submits 9 REAL orders (ABT 71 @104.56, ADBE 30 @248.05, HD 22 @328.93, IBM 29 @258.12, LOW 36 @207.26, META 12 @624.50, MSTR 52 @141.61, NKE 195 @38.42, PG 46 @160.58), journaled as `sync_submit` — **no phantom fill booked** |
| 2026-10-31 03:42 ET | `20261031_074202` (GH) | Pending records **survived the restart with their order ids** (the core fix): 9 `order_status` checks, all "accepted", fq=0 → intent kept alive, nothing resubmitted, no phantom fills. Two engine flips (ABT, PG) fired exit signals → their working entry orders canceled (`order_cancel_flip`). Buying power $400k → $348.5k (7 working orders ≈ $51.5k reserved). Orders target Monday Nov 2's open |
| 2026-11-02 04:35 ET | `20261102_083538` (GH) | Monday's auction came and went: the 7 `opg` orders still "accepted" at fq=0 pre-open, then **canceled unfilled** (gone from the broker by Monday night — paper opg now confirmed dead across 3 auction cycles). ABT/PG exit intents resolved as no-ops (entries were canceled, nothing to sell) |
| 2026-11-02 17:27/17:50 ET | `20261102_222726/225114` (GH) | **Both runs died in prepare on transient Alpaca API errors** (died between `guard_init` and heartbeat) — lost the Monday booking day |
| 2026-11-02 21:51 ET | `20261103_025136` (GH) | Recovery: warm_sync re-submitted 7 fresh `opg` orders (ADBE 29 @250.12, HD 22 @329.24, IBM 29 @257.84, LOW 35 @209.06, META 12 @614.86, MSTR 57 @130.53, NKE 193 @38.69) targeting Tuesday's open |
| 2026-11-03 03:47 ET | `20261103_084722` (GH) | Venue pass + bar pass both ran (14 `order_status` checks — the `_poll_pending_orders` fix working): all "accepted", fq=0, awaiting Tuesday's auction |

**Third fix (2026-11-03, `329c151`): tif `opg` → `day` on the Alpaca paper account.** Paper
`opg` has never filled across three auctions (accepted, then canceled ~9:45 ET). Per Alpaca
docs, `day` market orders submitted after the close are **queued and execute at the next
open** — exactly the backtest's `next_open` semantics, with no auction participation and no
dependence on a market-hours runner (the 13:35/14:35 UTC crons have never fired — GitHub
never registered them). `prepare` is also hardened against transient venue API errors (the
Nov-2 double failure); a skipped sync self-heals next session since `warm_sync` re-runs
every prepare. Convergence is now: evening run submits → broker executes at next open →
next evening run books the fills at venue prices.

**🎯 FIRST REAL FILLS — 2026-11-03 15:17–15:45 ET.** With the market open, the venue pass
found the overnight `opg` orders stale, canceled them, and the resubmitted `day` orders
executed at market immediately — the exact conversion path this fix was built for. 7
positions opened and topped up to target over Nov 3–4 sessions. Broker truth (Nov 4
20:54 UTC): equity **$101,946.45 (+$1,946 / +1.95%)**, cash $47,046, all 7 positions green,
**realized PnL $0.00** (no exits yet). Entry prices are close-of-day prints (fills landed
15:17–15:45 ET), not the backtest's next-open — the first entry-price wedge the parity
tracking should note.

### Fill-quality audit (2026-11-04)

| Mechanism | Fills | Price vs 5-minute tape | Verdict |
|---|---|---|---|
| `day` market orders, filled seconds after submission (15:17 ET) | ADBE 10@246.56, HD 8@331.72, IBM 27@249.32, META 12@618.73, NKE 70@38.18 | **±0.05%** | realistic |
| `day` resubmit (MSTR 57@135.03, ~15:20 ET) | booked at venue avg price | −0.29% vs the 15:35 bar — executed minutes earlier | realistic |
| Top-ups (15:31 ET Nov 4) | ADBE 2@249.52, HD 1@355.05, IBM 1@255.41, NKE 3@40.38 | **±0.03%** | realistic |
| Overnight `opg` partial fills (expired with fq>0) | LOW 34@217.49, HD ~14, NKE ~126 | LOW printed **+0.8% above the 215.80 open**; HD/NKE ≈ the open | **LOW is the one unrealistic print** — paper opg partially fills off-auction at prices the auction never printed; real opg orders of this size fill fully at the auction price |

Two audit findings, both fixed (`8005f94`):

1. **41% of fill notional ($21.9k of $53k) was missing from the journal** — auction partial
   fills (expired/canceled with `filled_qty>0`) went straight to resubmit without being
   booked, and a partially-filled day order consumed its pending so the remainder never
   reconciled. The broker account was always correct (venue truth); the journal — the
   publication data source — under-reported. Fix: `booked_qty` per pending, cumulative
   `filled_qty` booked as deltas (never re-booked — that would double-count); expired/canceled
   orders book their fills first, then resubmit only the unfilled remainder.
2. **Partial auction fills are paper-simulator artifacts** (HD 14/22, NKE ~126/193, LOW 34/35
   filled; ADBE/IBM/META/MSTR zero). Real opening auctions fill retail-size orders fully at
   the auction price. Expect the live-vs-backtest entry-price distribution to carry this
   noise until the account is on a real venue; magnitude is small at 0.1 sizing.

For the publication record: treat `orders.jsonl` (post-`8005f94`) as the decision+fill
forensics source and the Alpaca account statements as the PnL truth.

**⚠️ Open defect (2026-10-30) — signal→order pipeline stalls. FIXED same day, see below.**
Sessions log `desired_position=1` with `pending={}` and no `orders.jsonl` entries, while the
earlier session's pre-open `next_open_fill` booking (07:42 UTC, before the auction) suggests
the engine believes an entry already happened. Broker truth: flat, $100k, zero fills.

**Root cause (2026-10-30, three stacked bugs in the daily-restart path):**

1. **`_save_state` stripped the order identity from pending orders** (kept only
   `desired`/`reason`). Every daily restart downgraded a working `opg` order into a bare
   intent with no `submitted` flag / `order_id`.
2. **The resubmit branch booked a phantom fill**: `target_position` on a queueing broker
   *submits* and returns a stroke record; the code booked it as `next_open_fill`
   (29 ADBE @ 255.775 on Oct 28 — an order that was merely queued) and consumed the pending.
   Engine book: "ADBE open". Broker: flat, cash untouched (cash stayed exactly $100,000).
   With `desired=1`, no pending, and no broker position, nothing ever re-triggers — the
   decision layer only acts on transitions. Permanent stall from Oct 28 onward.
3. **Alpaca paper never fills `opg` orders** — the Oct 27 order sat unfilled and was
   canceled (auction orders unfilled after the open are canceled by the venue). So even an
   honest intent, resubmitted pre-open only, never converges on paper.

Evidence chain: Oct 27 `signal_submit` order `305575d4…` ≠ Oct 28 fill order `792f3616…`
(resubmit path booked the stroke); `state.json` cash $100,000.00 at every checkpoint
(a real 7,417 fill would have drawn it down); buying power 392,820 on Oct 28 (an order was
working) → 400,000 from Oct 29 (canceled unfilled).

**Fix (2026-10-30, `market_live/trader.py`, `brokers.py`, workflow):**

- Pending records round-trip verbatim through `state.json` (order id, submitted flag,
  target qty survive restarts).
- A queueing-broker submission is never booked as a fill (`BrokerFill.filled`); the real
  fill reconciles from `order_status` on the next bar at the venue's average price.
- A canceled/unfilled order is resubmitted with the pending kept alive; a stale opg found
  open during market hours is canceled and resubmitted as a day market order at the real
  price; an engine flip cancels the working order.
- `prepare()` adopts orphaned working orders (belt-and-braces recovery).
- Every `order_status` check is journaled (`orders.jsonl` events `order_status`,
  `order_cancel_stale`, `order_adopted`) so forensics no longer depend on GH run logs.
- Workflow adds 13:35 + 14:35 UTC crons (one of the two is always ~9:35 ET year-round) to
  give paper entries their market-hours fill window.

Tests: `market_live/test_opg_lifecycle.py` (11 lifecycle cases) + `test_late_fill.py` (paper
parity unchanged) — 19/19 green; snapshot fingerprint untouched (no re-warm on deploy).

**Second fix (2026-10-31, `4100f80`):** the venue-truth check lived inside `step_bar`, so it
only ran when a new daily bar closed — but the market-hours runs (13:35/14:35 UTC) usually
process ZERO bars, so a stale opg could never be canceled and resubmitted as a day order at
the live price, and an open-auction fill was not booked until the next session. The pending
resolution is now `_resolve_pending`, and `_poll_pending_orders()` runs at the start of every
poll. Also: after a flip-cancel the dead order can still be listed as open for a moment — it
is excluded from the duplicate-submit guard so the exit order is actually sent.

**Campaign restart decision (2026-10-30):** the persisted stall (9 desired, no pending,
flat broker) is cleared by flipping `warm_sync` to `true` in `alpaca_paper.json` — the
session adopts the engine's current book at the broker with real orders (opg/day per market
state), and sync submissions register as `state_sync` pending so fills reconcile at venue
prices. Consequence: the account becomes the engine's book from the next session; the missed
Oct-26 ADBE entry is written off as a bug casualty (re-entering 4 days late would poison
entry-price parity). The statistical clock (Section 5) restarts from the first real fill.

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
