# Testing and Live Architecture

The `pump-chart` trading system is built around a single, unified `StrategyEngine`. To ensure consistency between research, simulation, and real-world trading, the project uses three distinct execution modes that all wrap this common strategy core. 

These three systems—the **Backtester**, **ForwardTester**, and **LiveTrader**—each serve specific roles in the lifecycle of algorithm development and live deployment.

---

## 1. Backtester (`backtester.py`)

The Backtester is designed to run historical price recordings through the forward testing framework dynamically, providing accurate simulations of how the algorithm would have behaved on past data.

### Key Functional Features:
- **Intra-Candle State Expansion:** Stored OHLCV data is expanded into four accumulated-candle sub-states (open, first extreme, second extreme, close). This ensures that the simulated tick behavior matches exactly what the `CandleAggregator` provides in live conditions.
- **Identical State Evolution:** Because the `ForwardTester` is fed the same sequence of accumulated intra-candle data streams, all rolling buffers, ATRs, EMAs, and Kalman Filters evolve identically to the live system without lookahead bias.
- **Execution Model:** It preserves a **1-bar-delay** execution model—signals queued on candle $N$ are executed at the open (State 1) of candle $N+1$.
- **Performance Optimization:** Bypasses deep dictionary generation on intermediate tick states utilizing a fast path (`_build_full_result=False`), vastly speeding up the testing overhead without affecting accuracy. Capable of switching between sequential and parallel workloads automatically based on backtest batch sizing.

---

## 2. Forward Tester (`forward_tester.py`)

The Forward Tester is the core simulation layer used extensively by both paper-trading systems and the Backtester. Its primary job is to inject hyper-realistic constraints (such as execution delays, fees, and slippage) into long-only algorithm signals.

### Key Functional Features:
- **Hyper-Realistic Execution Delay:** Models execution capability based on configured priority network fees.
  - On a long entry, the "delay penalty" effectively pushes your fill price upwards from the open towards the candle's High.
  - On an exit, the penalty pushes the transaction price downward towards the Low. 
- **Slippage Modeling:** Once the algorithm arrives at the baseline delayed price, a flat slippage percentage (e.g., 1% or 10%) is applied, representing the liquidity loss typical in volatile AMMs. 
- **Realistic PnL Tracking:** Because fills occur at heavily "slipped" prices, the Forward Tester accurately registers the SOL lost in transition, keeping the success rate grounded in reality rather than theoretical precision. 
- **1-Bar-Delay Queue:** Matches the engine's strict signaling: entry and exit operations fire at the execution phase of the contiguous forward candle after a signal is queued.

---

## 3. Live Trader (`live_trader.py`)

The Live Trader is the flagship execution engine processing completely autonomous, server-side swaps on the Solana blockchain.

### Key Functional Features:
- **Immediate Execution model:** Unlike the backtested and forward-tested variants that queue for the next bar, the Live Trader executes **IMMEDIATELY** once a candle finalizes or an immediate conditional limit is tripped.
- **Server-Side Transaction Processing:** Instead of utilizing front-end browser extensions or Phantom intercepts, trades are formulated directly traversing the Jupiter V1 Lite API (specifically bypassing V6 for standard Token-2022 compatibility) and rapidly signed with a `solders` implementation utilizing the server’s base58 private key.
- **Asynchronous Flow:** Swaps operate asynchronously ("fire-and-forget"). Quotes, transaction builds, block simulation (avoidable via a hot-path), transaction broadcasting, and multi-threaded RPC polling occur continuously in the background avoiding system blockages. 
- **Market Cap Safety Floor:** Implements an emergency `min_market_cap_usd` threshold check against new data ticks. If the asset experiences a flash crash bringing external USD metrics beneath the configured guardrails, the algorithm completely bypasses strategy rulesets and forces an emergency market sell.
