"""market_backtest — backtest systems for markets other than pump.fun memecoins.

Two systems live in this package:

  1. US-stock backtest  — 10 years of daily, split/dividend-adjusted OHLCV
     from Nasdaq's public chart API (free, no key) across a 30-liquid-name
     universe.  Daily bars; meaning engine "seconds" (4-state intakes)
     rescale to bar cadence via the market calibrator.
  2. Crypto-perpetual-futures backtest — Binance USDT-M perpetual klines
     (data.binance.vision monthly zips, free, no key, includes the real
     taker-buy volume split) for 14 majors.  1h and 1d intervals.

Both reuse the production V2 strategy engine (`strategy_engineV2`) through
the shared `engine_factory` and `ForwardTester` executor — pipeline parity
is preserved by construction: the SAME engine code, the SAME execution
model (signal-instant fills ± slippage, 4-state intra-candle expansion,
force-close at tape end), only the tape and the calibration differ.

Nothing in `backtester.py` / `forward_tester.py` / `strategy_engineV2.py`
/ the booking system is modified.  All market data lives in
`backend/data/market_data.db` (a new store; `price_data.db` untouched) and
per-run artifacts in `backend/market_backtest/results/<batch>/`.
"""
