# CLAUDE.md

This file provides comprehensive guidance and extensive internal details to Claude Code (claude.ai/code) when working with code in this repository. It covers both the codebase execution architecture and the underlying mathematics/logic of the Strategy Engine in **VERY DETAIL**.

## Commands

All commands assume the `backend/` Python venv (created by `start.sh` on first run). Python 3.13 is required by the venv.

```bash
# Run the full app (FastAPI + uvicorn on :8000, serves frontend)
./start.sh

# Or manually:
cd backend && source .venv/bin/activate && python main.py

# Install deps (venv lives at backend/.venv)
cd backend && pip install -r requirements.txt

# Run a backtest batch using stored best params
python run_batch.py

# Run a single test file (pytest is configured; see .pytest_cache/ roots)
cd backend && python -m pytest ../test_gamma.py -q

# Or run a test file directly:
python backend/test_gamma.py
```

*Note: No `lint`/`format` config exists; respect existing style (no formatter is wired up).*

---

## Conventions & Invariants

- **The Single Most Important Invariant**: The Backtester, ForwardTester, and LiveTrader **must** evolve `StrategyEngine` state identically. Any change to `strategy_engine.py` that diverges between these three paths is a critical bug.
- **4-State Expansion**: They achieve this parity by feeding the same 4-state intra-candle expansion (`open → first extreme → second extreme → close`) into `engine.update()` per candle. Do not "simplify" this to a single update per candle; signals fire at very specific intra-candle moments.
- **Endpoints**: Most endpoint logic is in `backend/main.py`. New REST/WS routes go there unless they belong to the sniper router.
- **Database**: `candles.db` at `backend/` root is empty/legacy; real data is in `backend/data/*.db`. Don't write to `candles.db`.
- **Determinism**: Keep `StrategyEngine` perfectly deterministic across the three pipelines. If you change `update()` semantics, all three callers must be updated together or forward/test/live drift will appear as silent signal-timing bugs.

---

## Codebase Architecture (In Very Detail)

The Pump-Chart Dashboard is a high-performance, real-time analytics and trading platform tailored explicitly for volatile tokens (memecoins) on Solana. It unifies a reactive web frontend with a powerful asynchronous Python backend.

### 1. Frontend (`frontend/`)
- static `index.html` + `js/app.js` + `js/sniper.js` + `polymarket.html`.
- Uses LightweightCharts with complex Canvas overlays for Volume Profiles, ROC, and Regime Bars. 
- Real-time persistent connections to the Python backend via WebSockets (`/ws/{mint}` for analytics, `/ws/live/{mint}` for live trades).
- Explicit no-cache headers apply to the JS-serving route (`main.py`) to defeat stale code loading.

### 2. Backend & Data Engine (`backend/`)
- **`main.py`** (~60KB): Single FastAPI app serving REST APIs and WebSocket multiplexers.
- **REST Endpoints**: `/api/token/{mint}`, `/api/recorder/*`, `/api/backtest/*`, `/api/live/*`, `/api/scanner/*`, `/api/sniper/*`. WebSockets: `/ws/{mint}`, `/ws/live/{mint}`, `/ws/sniper`.
- **`pumpfun_client.py`**: Resolves if a token is a mint, Raydium pair, or pump.fun asset, and decides the stream origin (PumpPortal WS, Pump.fun V3 REST, Solana RPC `accountSubscribe`, or DexScreener API fallback). Concurrency is gated globally via `asyncio.Semaphore(8)`.
- **`candle_aggregator.py`**: Absorbs unstructured trades and dynamically constructs valid OHLCV (Open/High/Low/Close/Volume) based on exact sliding timeframe seconds (1s to 1h). The `is_new` flag on candle close triggers pipeline processing.
- **`data_store.py`**: Two main SQLite DBs under `backend/data/`: `price_data.db` (raw OHLCV with `buy_volume`/`sell_volume`) and `backtest_data.db` (records candles, signals, regimes, trades). A third DB, `sniper.db`, handles the separate sniper state.

### 3. Execution Pipelines (The "Three Amigos")
All use the exact same underlying `StrategyEngine`.

- **Backtester (`backend/backtester.py`)**: Runs purely on offline data from `price_data.db`. Expands historically stored candles into 4-state intra-candle sub-ticks. Enforces a **1-bar execution delay**: Signals queued on candle N fire at State 1 of candle N+1 (next open).
- **Forward Tester (`backend/forward_tester.py`)**: The paper-trade core. Uses live WebSocket frames. Models realistic execution-delay penalties (entry slips toward High, exit toward Low) and flat slippage %. The Backtester and LiveTrader both heavily share logic with this.
- **Live Trader (`backend/live_trader.py`)**: Executes **immediately** on live candle close on the actual mainnet via Jupiter V1 Lite APIs using standard `solders` tx signing from base58 keys. Features a market-cap safety floor preventing holding through flash-crashes. Mirrors backtester seeding via `update_historical_candle()` during warmup.

### 4. Sniper Module (`backend/sniper/`)
A completely separate strategy mounted into FastAPI via its own router. Uses `SniperEngine` mapping a pipeline: `launch_detector → pressure_analyzer → chart_validator → entry_signal → exit_signal`. Operates via `/api/sniper/start` manually.

---

## The Strategy Engine (In Very Detail)

**File:** `backend/strategy_engine.py`

This engine is a physics-based regime detector operating as a rapid state machine that applies a Langevin dynamics analogy (particles moving through viscous fluid) to price data. By separating true momentum from background noise (ATR), it strictly regulates risk via signal confidence checks.

### 1. Core Physics Analogy & Observables
- **Position & Momentum ($p, m$)** $\rightarrow$ Estimated using a 2-State Kalman Filter ($\hat{p}, \hat{m}$)
- **Trend Direction** $\rightarrow$ Fast/Slow Moving Averages (EMA 3 vs EMA 7)
- **Damping ($\gamma$)** $\rightarrow$ Contraction rate of the EMA spread
- **Noise/Friction ($\sigma$)** $\rightarrow$ Average True Range (ATR)
- **Potential Energy Landscape ($U(p)$)** $\rightarrow$ Fixed-range Volume Profile (High Volume Nodes act as barrier resistance).
- **External Force** $\rightarrow$ Cumulative Delta Volume (buy pressure vs sell pressure).

### 2. Core Signal Formulation
The fundamental signal strength ($S$) is based on the Signal-to-Noise Ratio (SNR):
$$ S = \frac{|\hat{m}|}{ATR_{floor}} $$
$ATR_{floor}$ is dynamically bounded by a rolling median to avoid zero-division and properly ground baseline market volatility.

**Barrier-Adjusted Signal**: Entering into high resistance dampens the signal heavily.
$$ S_{effective} = \frac{S}{\Delta U} $$
where $\Delta U$ is the relative distance to the nearest Volume Profile High Volume Node (HVN).

### 3. Kalman Filter Momentum Estimation
A purely real-time 2-state Kalman filter continuously estimates actual price/momentum ignoring sudden tick noise:
- Predict: $p_{pred} = p_{prev} + m_{prev}$ and $m_{pred} = (1 - \gamma) \cdot m_{prev}$
- Measurement Update: Filter adjusts its measurement variance dynamically against recent price variance. Results in a highly robust $[\hat{p}, \hat{m}]$ that suffers dramatically less lag than standard MAs.

### 4. State Machine (Regime Detection)
State cycle: `IDLE → TREND → EXHAUSTION → REVERSAL/CONTINUATION → TREND`
- **TREND**: Reached when momentum persistence, volatility expansion, and EMA separation exceed thresholds.
- **EXHAUSTION**: Triggered when the trend "loses energy". $|\hat{m}|$ decays, EMA spread shrinks, and price action stalls relative to the ATR.
- **REVERSAL/CONTINUATION**: Leaving exhaustion, incoming forces (Delta Volume) determine if the asset flips or pushes another leg.

### 5. Signal Confidence Scoring
Global trend confidence $C \in [0, 1]$ dictates whether a trade is permitted. It is the weighted sum of four pillars:
1. **Persistence (30%)**: Fraction of consecutive bars keeping identical momentum signs.
2. **Normalized Momentum (25%)**: Present $|\hat{m}|$ relative to macro moving average.
3. **Volatility Expansion (25%)**: Present ATR relative to rolling median ATR.
4. **EMA Separation (20%)**: Normalized magnitude of the fast/slow EMA divergence.
Confidence must cross `confidence_high` to approve an entry signal, otherwise it resides in the "ambiguous zone" which strictly blocks entries.

### 6. Advanced Entry Gates & Risk Safeguards
- **Top-Blast Prevention (Blow-Off Top Guard)**: 
  - If actual price aggressively exceeds Kalman estimate ($p > \hat{p} \times (1 + k)$) AND signal $S$ is gigantic, momentum holds are suspended (blocks tops).
  - Explicit multi-bar decline of $|\hat{m}|$ prevents buying late.
  - Long-baseline filters detect abnormal current candles against a mathematically "calm" historical window.
- **Anti-Chop Filter**: Rejects N-bar boxes of `< 2%` moves if price sits at the 35%-65% midline mark. Reject heavily mixed momentum signature sign-flips.
- **First-Leg Breakout (Cold Starts)**: 
  - Breakouts normally demand $N$ stable bars. If $C > 0.67$ (supreme confidence), the threshold is dynamically dropped to purely 1 bar to grab highly profitable initial explosive breakouts. 
  - Employs lowered requirements on long `IDLE` state exits.

### 7. Regime Sensitivity Tuning
Direct knobs found inside `backend/strategy_engine.py`:
- `confidence_high` (e.g. 0.79): Global gatekeeper for transitions. Lowering this (e.g., 0.60-0.65) allows greater strategy sensitivity to shifts.
- `regime_lookback` (e.g. 6): Controls the memory length of momentum persistence and volatility expansion. Lower to react faster.
- `kalman_gamma` (e.g. 0.1): The momentum estimation learning rate. Increase (0.15-0.25) to bind $\hat{m}$ faster to the newest prices.
- `persistence_threshold` (e.g. 2 bars): How many consecutive bars a sign must hold prior to trend declaration.
- `exhaustion_persist_bars` & `reversal_confirm_bars`: Enforced physical delays to certify macro phase shifts. Lowering them accelerates phase transitions.
