"""market_live — decision-identical live/paper execution for the market engine.

The one invariant that matters
------------------------------
`MarketEngineHost` does NOT re-implement strategy logic.  It constructs the
exact same `MarketForwardTester` (hosting `StrategyEngineV2M`) that
`market_backtest.runner.run_market_backtest` uses, and feeds each closed bar
through the exact same four `update()` calls in the same order:

    state 1  open tick
    state 2  first extreme   (high first on bull bars, low first on bear bars)
    state 3  second extreme
    state 4  close tick      (volume lands here)

Fills are produced by the same `_open_long` / `_close_long` code with the same
`exec_model="instant"` and the same COSTS table, so entry/exit prices, sizes,
reasons and PnL are bit-identical to the backtester.  `test_live_parity.py`
is the gate that proves it on real tapes.

Position sizing is the ONLY thing the live layer adds: the backtest trades
`buy_size` of a 1.0 balance (10% notional); live trades
`k * buy_size * account_equity`.  A sizing multiplier never changes a decision.

Modules
-------
host.py        MarketEngineHost — bars in, decisions/fills out
feeds.py       closed-bar feeds (Yahoo daily/1h, local tape replay)
brokers.py     PaperBroker (ledger), AlpacaBroker (REST), bridge brokers for MT5
prop_guard.py  firm rule templates + pre-trade/post-close enforcement
journal.py     JSONL session journal (same shape as the memecoin sessions)
trader.py      MarketLiveTrader — feed -> hosts -> guard -> broker -> journal
run_live.py    CLI
"""
