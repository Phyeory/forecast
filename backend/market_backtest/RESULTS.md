# Market Backtest Systems — Results (V2M clone)

**Branch**: `market-backtest-systems` · **Date**: 2026-10-18
**Memecoin engine**: untouched (`strategy_engineV2.py` sha `7ae0aa73…` verified
at every batch run; clone diff = docstring + `zeta` default only).

## What was built

- **`strategy_engineV2M.py`** — structural clone of the V2 engine for market
  tapes (`MarketStrategyEngine` / `StrategyEngineV2MAdapter`). Fork delta:
  `zeta = 0.0` default (ℓ jump-decay destabilized multi-year tapes; root-caused
  on AAPL: posterior pinned at every clamp after the first 3×-volume day).
- **`market_calibration.py`** — causal per-tape calibration: per-intake
  coefficient rescale, KDE memory in intake units, interval-class τ
  (1d→40, 1h→20; τ≥60 lets the Kramers down-escape dominate hourly exit books),
  posterior-σ entry gate (0.35×p10 from a bounded throwaway replay).
- **`runner.py`** — full-tape replay via ForwardTester (4-state expansion,
  complete decision stream, `recording_ended` force-close) with **train/test
  split**: engine params frozen from the TRAIN prefix only
  (`calib = candles[:split_idx]`), stats partitioned by entry time.
- **`stats_report.py`** — pooled per-trade bootstrap CI + Wilcoxon signed-rank +
  symbol breadth (binomial); mark-to-market portfolio equity (open positions
  marked at every candle close — no booked-at-exit artifacts); FTMO-classic
  prop-rule simulation (deterministic + 2000-sim block-bootstrap MC at
  k ∈ {0.25…5}) and funded-phase 1-year survivorship sims.

## Data

| tape | symbols | history | bars/symbol | train | unseen test |
|---|---|---|---|---|---|
| stocks 1d | 67 (mega→broad factors) | 10y (2016-10 → 2026-10) | 2513 | ~70% (→ ~2023-10) | ~3.0y |
| perp 1d | 44 USDT-M perps | 2020-01/2023-06 → 2026-09 | 1.0–2.5k | ~70% | ~2.8y max |
| perp 1h | 44 USDT-M perps | 2020-01/2023-06 → 2026-09 | 25k–59k | ~70% | ~2.8y max |

Sources: Nasdaq API (split+div-adjusted daily), Binance Vision (real taker-buy
split). DB: `backend/data/market_data.db` (isolated; `git`-ignored data dir).

## Headline results (UNSEEN window)

| market | trades | WR | PnL (booked) | MTM PnL | per-trade mean | 95% CI | Wilcoxon p | breadth |
|---|---|---|---|---|---|---|---|---|
| stocks 1d | 174 | 69.0% | −0.067 | −0.054 (−1.8%/yr) | −0.038% | [−0.149, +0.059]% | 2.2e-4 | 27/67 (p=0.14) |
| perp 1h | 2933 | 51.8% | −5.60 | −0.141 (−5.1%/yr) | −0.191% | [−0.215, −0.167]% | 7.4e-5 | **0/44** |
| perp 1d | 58 | 25.9% | −0.222 | −0.170 (−6.1%/yr) | −0.382% | [−0.784, −0.018]% | 2.8e-3 | 6/44 |

- **Every unseen-window per-trade mean is statistically ≲ 0** (perps: CI fully
  below zero, breadth 0/44 hourly). Win rates are high but the loss tail is
  ~4× the win size (giveback exits vs slow state-based loss book).
- Train segments agree in sign (stocks train PnL −0.203, WR 67.4%; perps 1h
  −6.71, WR 48.4%) — no train→test regime flip; the bleed is structural.
- MTM portfolio: stocks maxDD 24.2% / worst day −4.4%; perp 1h maxDD **71.5%**
  (44 symbols × 0.1 notional on one account) / worst day −22.7%.

## Prop-firm evaluation (FTMO-classic: +10% P1, +5% P2, 5% daily / 10% total)

Phase-1 pass rates (block-bootstrap over the MTM daily PnL):

| k (size) | stocks P1-60d | stocks funded blown | perp1h P1-60d | perp1h funded blown | perp1d P1-60d |
|---|---|---|---|---|---|
| 0.25 | 0.0% | 0.3% | 7.3% | 67.8% | 0.0% |
| 0.5 | 0.0% | 21.2% | 30.6% | 95.7% | 0.0% |
| 1.0 | 7.3% | 70.5% | 34.5% | 99.8% | 1.2% |
| 2.0 | 31.8% | 96.8% | 31.8% | 100% | 12.7% |
| 3.0 | 34.2% | 99.5% | 29.2% | 100% | 20.6% |
| 5.0 | 34.0% | 100% | 24.9% | 100% | 26.8% |

**Verdict: cannot pass prop-firm challenges in any tested configuration.**
Pass rates exist only where variance is cranked up (k≥2), and every one of
those configurations blows a funded account with 97–100% probability within a
year because the out-of-sample expectancy is negative. The deterministic run
of the actual test-window equity path fails on the total-loss rule.

2-step pass ≈ P1(30d) × P2|P1 — e.g. stocks k=3: 28.1% × 48.1% ≈ 13.5%,
but funded P(blow) 99.5% ⇒ negative campaign EV after fees.

## How much money it can actually make

At the tested configuration (0.1 notional/position, 1.0 account):

- stocks: **≈ −1.8%/yr** → −$1,785/yr per $100k
- perps 1h: **≈ −5.1%/yr** → −$5,074/yr per $100k
- perps 1d: **≈ −6.1%/yr** → −$6,110/yr per $100k

The honest expected value on unseen data is **negative** in all three tapes.
The systems are decision-complete and fully operational; the V2M adaptation is
not a profitable market strategy as calibrated.

## Failure mechanisms found (why)

1. **ζ-collapse** (fixed): one-sided liquidity-jump decay kills the RBPF on
   long tapes → `zeta=0` in the clone.
2. **KDE-geometry stall**: when price sits below the volume-mode, "up-escape to
   the mode" dominates (`k_down → 0.0` exactly) — the down-exits are
   geometrically inaudible while price grinds −48% (NKE held 374d). This is the
   "silent hold" failure class of production, at daily cadence.
3. **Long-hold tail by design**: hold durations span 9d median → 513d max;
   losses p90 ≈ 115d — no stop in the cascade; the exit book reconciles late.
4. **Exit asymmetry**: avg win 0.15–0.22% vs avg loss −0.55–−0.61% (balance
   units). At 51–69% WR this nets negative per trade, statistically confirmed.

## Reproduce

```bash
cd backend
./.venv/bin/python -m market_backtest.batch run --market stocks --max-workers 8
./.venv/bin/python -m market_backtest.batch run --market perps --intervals 1d,1h --max-workers 8
./.venv/bin/python -m market_backtest.stats_report --dir market_backtest/results --market stocks --save stats_stocks
./.venv/bin/python -m market_backtest.stats_report --dir market_backtest/results --market crypto_perp --save stats_perps
```

Per-artifact JSONs in `market_backtest/results/` (`*_1d/1h.json`,
`stats_*.json`, `batch_*.json`).
