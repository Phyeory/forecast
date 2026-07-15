# Performance Report — Big-Loss Analysis (2026-07-13)

## Baseline (current `best_params.json`)
- 331 trades · 189 W / 142 L · **57.1 % winrate** · **+1.52505 SOL PnL**
- 31 trades have **pnl_pct < −15 %** (the "big losses") — they account for most of the negative contribution.

## Big-Loss Pattern (31 worst trades, all > 15 % loss)

Aggregated `entry_params` across every trade with `outcome == "L"` and `pnl_pct < -15`:

| Attribute                | Big-Loss value                  | Healthy-trade value       |
| ------------------------ | ------------------------------- | ------------------------- |
| `regime`                 | **29 / 31 `continuation`**      | mix of `trend`/`continuation` |
| `entry_reason`           | **31 / 31 `buy_continuation`**  | mix                      |
| `exit_reason`            | **29 / 31 `reversal_exit`**     | 50/50 split              |
| `momentum_past_peak`     | **30 / 31 `True`**              | usually `False`          |
| `spread_expanding`       | **28 / 31 `False`**             | usually `True`           |
| `ema_cross_valid`        | **28 / 31 `False`**             | usually `True`           |
| `pre_entry_stable`       | **31 / 31 `False`**             | usually `True`           |
| `is_trending`            | **31 / 31 `False`**             | usually `True`           |
| `reversal_bar_count`     | long-tailed (1 → 38)            | short                    |
| `stoploss_pct`           | confidence-scaled **12–20 %** | 12–20 %                 |
| `effective_takeprofit_pct` | very high (≈188 %)            | 30–200                   |

The single most consistent feature: **every big loss entered via a `continuation`
buy where `momentum_past_peak=True`, `spread_expanding=False`, and `is_trending=False`.**
In other words, the engine is buying a "continuation" signal emitted from inside
an EXHAUSTION → CONTINUATION transition **after** momentum has already crested —
the bar that fires the BUY is the very tail end of the move.

## Root-Cause Mechanism

### Code problem 1 — `EXHAUSTION → CONTINUATION` fires too eagerly
`_detect_regime()` (`strategy_engine.py:1250-1323`) transitions EXHAUSTION →
CONTINUATION when:

```
spread_expanding AND S > transition_threshold AND not momentum_decay
```

The subsequent BUY is gated only by `S > S_weak` and `m_hat > 0` plus a 3/6
`entry_conds` count. **None of those gates reject a spike-then-fade bar.**
Specifically:

* `momentum_past_peak=True` (the FIX-A flag) is **not consulted** in the
  EXHAUSTION → CONTINUATION buy branch, even though it is consulted in
  `_passes_entry_gate()` only on `direction == Direction.UP` (which fires only
  in the REVERSAL → CONTINUATION path). The exhaustion-continuation path skips
  the FIX-A guards because `_passes_entry_gate()` is called *inside* the
  branch already, but the actual block happens too late: the FIX-A
  `momentum_past_peak` check is inside an `if direction == Direction.UP:` clause
  that *is* reached in this path — so why do these buys still go through?

  Looking at the trace: by the time the trade is filled (1-bar delay), the
  engine has already advanced one more bar. `_momentum_peak_declining_count`
  has ticked up to ≥1 BEFORE the fill snapshot, but the **signal was queued
  on the previous bar** when the count was 0. The pre-fill snapshot (line 553
  of `forward_tester.py`) fires the engine.update() for the entry bar and
  then captures `momentum_past_peak=True` — but the BUY signal was already
  queued on the prior bar's state when it was `False`. The FIX-A gate
  effectively lags by one bar.

  A second related issue: `ema_cross_valid=False` and `pre_entry_stable=False`
  on 28 / 31 big losses means these were weak-momentum "continuation" signals
  that **should have been filtered by the entry-gate stability check** — but
  stability is only consulted in `_passes_entry_gate()` when `direction ==
  Direction.UP`, and the EXHAUSTION → CONTINUATION path **does** call
  `_passes_entry_gate()` already — yet these trades still fired. The reason
  is that `_passes_entry_gate()` returns True for the wrong reason: the
  macro-trend gate (`c < ema_macro`) only checks the close vs EMA, but during
  the spike-and-collapse pattern `ema_macro` has been dragged up by the spike
  so `c >= ema_macro` even though the move is exhausted.

### Code problem 2 — `reversal_exit` is the dominant exit but fires too late
`_check_exit()` (`strategy_engine.py:1576-1580`) only emits `reversal_exit`
when BOTH:

```
regime == REVERSAL AND
reversal_bar_count >= reversal_exit_confirm_bars (default 0) AND
signal_strength > S_noise
```

**The problem**: by the time `regime` flips to REVERSAL the price has usually
already traversed 15–40 % against us (see sample: `DRIPZ` −30.5 %,
`LAMBO` −33.6 %, `MONEY` −70.9 %). The regime state machine takes several
bars to confirm a reversal (needs `_ema_cross_valid`, `rev_met >= 5`, then
`reversal_confirm_bars`), so the SL/trailing mechanism is the only thing
protecting the position — and it's set to scale 12–20 %, which is too wide
for tokens that move 30–80 % on a single bar.

**Crucially**: the static `stoploss_pct` (the only true hard stop) is `0.0`
in `best_params.json`. The 12–20 % numbers you see are the
confidence-scaled `_effective_stoploss_pct()` — these are TRAILING stops
(`stoploss_pct_low=12, stoploss_pct_high=20` are positive = trailing),
**and the trailing stop is dormant until the peak reaches `arm = entry·(1+pct)·1.05`**.

So on tokens that pump-then-dump in 5 bars, the trailing stop never arms
(the peak must climb ≥ 12·1.05 = 12.6 % + the 5 % activation buffer) — and
when the dump comes, the position exits via the slow `reversal_exit` path
at a -15 % to -50 % loss instead.

### The fix is on two fronts

1. **Cut the bad entries** — block EXHAUSTION → CONTINUATION buys when
   `momentum_past_peak=True` *on the bar that's about to issue the signal*
   OR `spread_expanding=False` OR `!is_trending`. These are precisely the
   signals that turn into big losers.

2. **Cut the bad exits earlier** — add a true hard stop loss, and tighten
   `reversal_exit` so it fires on the first sign of trouble even when the
   regime hasn't fully flipped to REVERSAL. The trailing stop's arm buffer
   (the extra 5 %) should also be removed when the trade has not yet moved
   into profit (it currently allows losers to ride freely while the trade
   is underwater).

## Verifying exit-quality vs win-quality

```
exit_reason         winrate     pnl-mix
take_profit       100 % (21/21)   — all winners, because TP fires high
trailing_stop      81 % (35/43)   — winners (trades where TS armed)
reversal_exit      50 % (125/249) — coin-flip, EXITS LATE on downtrend
continuation_exit  44 % (8/18)    — exits late
```

`reversal_exit` is firing 249 times — 75 % of all trades — at a coin-flip
winrate. It needs to be replaced with a smarter, faster exit on losing
trades so the 124 losers in that bucket don't slide from −5 % to −30 %.
