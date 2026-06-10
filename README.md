# Pump-Chart Dashboard Architecture & Documentation

## 1. Overview
The **Pump-Chart Dashboard** is a high-performance, real-time analytics and trading platform tailored explicitly for evaluating and trading volatile tokens (e.g. memecoins) on the Solana blockchain. 

It unifies a reactive web frontend with a powerful asynchronous Python backend. At the core is a physics-based **Strategy Engine** that detects price action regimes, applies Kalman filters to trace actual momentum versus market noise, and issues buy/exit signals synchronously. The platform supports robust backtesting, forward "paper" testing with realistic execution slippage and fees, and completely autonomous, immediate real-world asset trading.

---

## 2. System Architecture Map

The project strictly follows a disconnected client-server model communicating concurrently through rapid WebSockets and REST APIs.

### A. Frontend (`frontend/`)
The visual dashboard where users analyze, tweak, and start active sessions.
* **`index.html` & `css/`**: Provides the structural layout, styling panels for settings, metrics, data visualizations, and trades.
* **`js/app.js`**: Core UI script handling state. 
    * Creates the interactive charts utilizing LightweightCharts.
    * Uses Canvas projections to stack complex custom visualizations like Volume Profiles, ROC, and Regime Bars concurrently. 
    * Establishes real-time persistent connections to the Python backend (`/ws/{mint}` for analytics or `/ws/live/{mint}` for live execution pipelines).

### B. Backend API / Router (`backend/main.py`)
Served via `FastAPI` + `uvicorn`, executing under `asyncio` for non-blocking I/O.
* **REST APIs**: Used to retrieve recordings from the database, manually start/stop continuous data aggregators, and invoke heavy bulk algorithms like `run_backtest_batch`.
* **WebSocket Endpoints**: Multiplexes incoming streams onto the frontend allowing users to watch the exact data the server processes in real-time.

### C. Data Engine (`backend/pumpfun_client.py` & `backend/candle_aggregator.py`)
Responsible for feeding data safely without encountering severe network bottlenecks or rate-limiting bans.
* **Resolver**: Initially detects whether the user put a direct token mint address, a Raydium pair, or a pump.fun direct asset, and decides the most viable streaming origin.
* **`CandleAggregator`**: Absorbs unstructured tick/trade messages and dynamically constructs consistent OHLCV (Open/High/Low/Close/Volume) containers bound to accurate rolling timeframes (i.e. `1s`, `5s`, `15s`, `1m`...).

---

## 3. Execution Modules & Trading Lifecycle
The system employs three completely independent data pipelines scaling from pure research to live mainnet execution, all querying the exact same underlying `StrategyEngine`.

1. **Backtester (`backend/backtester.py`)** 
   Reads strictly offline historical data from the local SQLite storage (`candles.db`). Maps each historical timeframe into 4-state expansions allowing algorithm states to replicate live ticks accurately without risking zero lookahead bias. Simulates 1-bar execution delay.
2. **Forward Tester (`backend/forward_tester.py`)**
   Provides paper-trading facilities by receiving a stream of live WebSockets frames, running the signals through an execution simulator matching actual Solana latency logic (injected entry delays representing priority fees and AMM slippage configurations).
3. **Live Trader (`backend/live_trader.py`)** 
   The flagship fully autonomous trading interface tracking memory-optimized metrics locally for instant response. The moment the Strategy Engine signals on candle boundary closure, it formulates blockchain instructions instantly utilizing base58 private keys and bypasses UI latency to fire asynchronous payloads (fire and forget executions) scaling on the Jupiter or pump platforms directly. Assures identical mathematical states with backtester logic leveraging a pre-warmup pass to synchronize all EMAs and ATR arrays.

---

## 4. Service / External Integrations
The backend orchestrates queries into several distinct market services dynamically.

* **PumpPortal API (`wss://pumpportal.fun/api/data`)**: Streams live, low-latency buys/sells directly emitted from `pump.fun`'s internal bonding curves before market migration.
* **Pump.fun V3 REST (`https://frontend-api-v3.pump.fun`)**: Initial data provider returning immediate token metadata (descriptions, logos) and establishing if the token has already fulfilled the curve metrics.
* **Solana RPC HTTP/WSS (`api.mainnet-beta.solana.com`)**: When tokens successfully migrate from pump.fun over to standard automated market makers, the tool uses direct `accountSubscribe` on Solana nodes natively to track reserves within the active vault, dynamically reverse computing Spot pricing.
* **DexScreener API (`api.dexscreener.com`)**: Serves as the backup fallback poller resolving pair-pool addresses externally when standard lookup paths fail or liquidity pools do not fit standard signatures.
* **SQLite Database (`candles.db`)**: Self-contained tick retention database handled through `backend/data_store.py` for aggregating thousands of multi-asset history series to construct robust strategy validations offline.
