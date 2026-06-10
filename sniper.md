## PART 1: UPDATED STRATEGY DESIGN

### Reading the Market Pattern (What the Charts Show)

The images make the pattern unambiguous. Every viable pump.fun token follows a predictable
three-act structure:

**Act 1 — Launch Spike (T+0 to T+~2 min)**
The developer and bundlers execute pre-coordinated buys the moment the token is created.
This pushes the market cap from its genesis ~$2–4k rapidly to $6–10k. These are not
organic buyers — they are the creator's own wallets, multi-wallet scripts, or rented
bundle services accumulating a position before any real retail sees the coin.

**Act 2 — Sell-Off Dip (T+~1 min to T+~4 min)**
The dev and bundlers sell. The dip is brutal and fast — 50 to 60% from the spike high
is typical. A coin that peaked at 8k MC falls to 3–4k MC. Most competing sniper bots
that bought into the spike are now at a loss. This is where they capitulate, accelerating
the dip. The floor is established when there are no more large sellers and price stabilizes.

**Act 3 — Organic Recovery (T+~3 min to T+~15 min)**
IF the token has genuine narrative, meme quality, or timing alignment with something
trending, retail buyers begin re-entering after the dip. Volume slowly picks up on the
buy side. Buy pressure begins exceeding sell pressure. Price starts making higher lows
then higher highs. THIS is the entry point.

The strategy is to sit out Acts 1 and 2 entirely — let the dev and bundlers dump on
whoever rushed in — and only enter at the start of Act 3 when organic buy pressure is
demonstrably returning.

---

### Why This Works Better Than Buying at Launch

Buying at launch means competing against:
- Other sniper bots with superior hardware and co-location
- Bundled buys from the dev who will dump on you
- MEV sandwichers eating your slippage

Buying the recovery means competing against:
- Retail traders who are slower and less systematic
- The emotional cycle: people who sold the dip now have FOMO as price recovers

The risk profile also inverts. Launch buyers face unlimited downside (the coin can go to
zero instantly if the dev rugs immediately). Dip-recovery buyers enter after the worst
selling has already happened. The floor is visible. Downside is structurally limited
because the big sellers have already sold.

---

### The Lifecycle Model

```
Launch                  Spike High     Floor        ENTRY          Exit
   |                        |              |            |              |
   2K -----> 3K ----> 7-9K MC      3-4K MC    4-5K MC --> 7-9K MC (exit on sell pressure)
   ^dev buy  ^bundle run   ^bundle sell-off  ^organic recovery
```

Target entry zone: approximately $3,500–$5,500 MC during the recovery phase
Target exit: dynamic — when buy pressure measurably fades, not at a fixed price

---

### Hard Filters (Token Eligibility Gate)

These run before any further analysis. Any single failure = hard reject, no trade.

**Filter 1 — Global Fees Paid ≥ 0.5 SOL**
Pump.fun charges a 1% fee on every buy. For fees to reach 0.5 SOL, the token must have
generated at least 50 SOL in cumulative buy volume. This is the most important filter.
It proves:
- Real trading activity has occurred (not just a dev buy and nothing else)
- The token survived long enough to attract volume
- The bonding curve has meaningful liquidity depth
Tokens that do not meet this threshold are still in Act 1 or dead. We have no interest.

**Filter 2 — Not a Fake Chart (Real vs Manipulated Pattern)**
The chart shape itself is a signal. Fake/manipulated charts share common signatures:
- Pure staircase upward movement: every candle green, no retracements > 5%
  (price is being fed in mechanically — bots maintaining a facade)
- The initial dev buy candle is the single largest candle in the entire history
  and nothing since matches its size (no organic volume has materialized)
- Buy/sell transactions are suspiciously uniform in size (bot activity pattern)
- The sell-off dip never happened — price only went up from creation (still in Act 1,
  not ready for our strategy)

Real charts have:
- A discernible spike high followed by a real dip of ≥ 35% from that high
- Candles of varied sizes (organic buyer/seller psychology)
- Clear floor formation at the dip low (price bouncing at a consistent level)
- Volume picking up on buy side during recovery (not just single large buys)

**Filter 3 — Not a Spam Deploy / Vamp**
- Token name/ticker matches a currently active token name exactly = reject (vamp)
- Token was deployed by a wallet that has created >5 tokens in the past 24 hours = reject
- Token creator wallet has ≥ 1 confirmed rug in our database = reject (blacklist)
- Token has zero social metadata (no description, no image) AND fees < 1 SOL = reject

**Filter 4 — Bonding Curve Not Near Completion**
- If real SOL reserves > 75 SOL (>88% to graduation/migration): reject
  We are targeting pre-migration bonding curve tokens only. Near-graduation tokens have
  different price dynamics and migration risk.

---

### Entry Signal — Detecting Organic Buy Pressure Recovery

All entry conditions must be simultaneously true. These are evaluated on a rolling
basis, reassessed every 1-second candle close.

**Condition 1 — The Dip Has Occurred**
`spikeHigh` = highest MC observed in the first 3 minutes after the fees threshold was crossed
`currentMC` = present market cap
`dip_depth = (spikeHigh - lowestMC) / spikeHigh`
Requirement: `dip_depth >= 0.35` (at least 35% dip from spike high has occurred)

This confirms Act 2 happened. Without this, we are in Act 1 and the sell-off may still come.

**Condition 2 — The Floor Has Formed**
`floorWindow` = last 10 one-second candles
Requirement: price has not made a new low in the past 10 seconds
AND standard deviation of lows in floorWindow is < 3% of current price
(price is stable at the bottom, not still falling)

**Condition 3 — Buy Pressure Ratio Turning Positive**
Using the per-candle buy/sell volume separation from the CandleAggregator:
`buyRatio_1s` = buyVolume / (buyVolume + sellVolume) for the current 1s candle
`buyRatio_5s` = rolling 5-candle average of buyRatio_1s

Requirement:
- `buyRatio_1s >= 0.60` (this candle is majority buy volume)
- `buyRatio_5s >= 0.50` (the last 5 seconds average is at or above neutral)
- `buyRatio_5s` is INCREASING (not declining — trend direction matters)

**Condition 4 — Momentum Turning Positive**
Using the StrategyEngine's existing Kalman filter and ROC:
- Rate of Change (ROC) has crossed from negative to positive in the last 2 candles
- The Kalman filter price estimate is trending upward (last Kalman estimate > prior estimate)
- At least 2 consecutive candles with close > open (two green candles confirming)

**Condition 5 — Volume Expansion on the Buy Side**
`avgBuyVol` = average buy volume per candle over the floor window (last 10 candles)
`currentBuyVol` = buy volume in the most recent candle
Requirement: `currentBuyVol >= avgBuyVol * 1.5`
(current buy candle is meaningfully above the floor's average buy activity — demand is accelerating)

**Condition 6 — Price Above Floor**
`floorLevel` = lowest observed price during floor formation window
`currentPrice` = latest trade price
Requirement: `currentPrice >= floorLevel * 1.04`
(price has actually recovered at least 4% off the floor — not a false signal at the very bottom)

**Entry is triggered when ALL 6 conditions are simultaneously true on a candle close.**

---

### Exit Signal — Dynamic Sell Pressure Detection (No Fixed Take Profit)

There is no price target. The bot holds until the buy pressure that is driving the
recovery visibly degrades. Exit is triggered by ANY of these conditions:

**Exit Trigger 1 — Buy Pressure Collapse (Primary)**
`buyRatio_3s` = 3-candle rolling average of buyRatio_1s
If `buyRatio_3s < 0.38` for 2 consecutive candles:
→ SELL 100%. The buying side has definitively flipped to selling-dominant.

**Exit Trigger 2 — Momentum Reversal**
- ROC crosses from positive to negative
- AND the Kalman filter estimate turns downward (current < prior)
→ SELL 100%. Momentum has reversed.

**Exit Trigger 3 — Large Sell Spike**
`avgTotalVol` = average total volume per candle over the last 10 candles
Current candle's sellVolume > `avgTotalVol * 3.0`
(a large single seller has appeared — likely a wallet with accumulated position dumping)
→ SELL 100% immediately (do not wait for candle close — trigger on tick)

**Exit Trigger 4 — Upper Wick Exhaustion Pattern**
Last 3 consecutive candles all have upper wick > body (buying attempts repeatedly rejected)
AND price has not made a new high in these 3 candles
→ SELL 100%. Classic topping pattern — supply is overwhelming demand.

**Exit Trigger 5 — Time-Based Stop**
If position has been held for > 5 minutes AND price has not exceeded entry price by > 15%:
→ SELL 100%. The recovery thesis has not materialized.

**Exit Trigger 6 — Hard Price Stop**
If price drops > 30% from entry price at any point:
→ SELL 100% immediately. The floor failed.

Note: There is intentionally no "take profit at X%" target. The bot rides the recovery
as long as buy pressure sustains. A strong recovery might reach 8k, 10k, or 15k MC —
cutting it short at 7k with a fixed TP leaves money on the table. The dynamic exit
captures the actual wave rather than a predetermined slice of it.

---

### Forward Test Module

In place of backtesting, the system uses a **Forward Tester** that runs in real-time
on live market data but executes no actual blockchain transactions.

**What it does:**
- Subscribes to the live PumpPortal WebSocket for all new token events
- Runs every token through the same full filter and signal pipeline as live trading
- When all entry conditions are met, records a "paper buy" at the current bonding
  curve price (computed from virtualSolReserves/virtualTokenReserves at that moment)
- Tracks the paper position in real time as prices move
- When any exit trigger fires, records a "paper sell" at the current bonding curve price
- Applies realistic cost simulation:
  - 1% pump.fun protocol fee on both buy and sell
  - 0.25% estimated slippage (you are a price-taker on the bonding curve)
  - 0.001 SOL fixed transaction cost per trade

**What it records (in the SQLite forward_test_trades table):**
- Token mint, name, symbol
- Detection time, entry time, exit time
- Entry MC, exit MC, entry price SOL, exit price SOL
- Paper P&L in SOL and % terms
- Which exit trigger fired
- All signal values at entry moment (for calibration)
- Whether the real chart filter would have caught this as fake

**Why forward testing is superior to backtesting here:**
Pump.fun token data from even a few weeks ago reflects different market conditions
(SOL price, retail interest level, competition from other bots). The bonding curve
dynamics and MC levels that defined "a good entry" at $100/SOL are different from
those at $175/SOL. Forward testing always reflects current market reality. Run it
for a minimum of 500 simulated trades before enabling live execution.

---

### Risk Management (Adjusted for Dip-Recovery Approach)

```
Per-Trade:
  Position size:              0.10–0.25 SOL (scaled by conviction score, see below)
  Hard stop loss:             –30% from entry price
  Time stop:                  5 minutes with <15% gain

Per-Session:
  Max concurrent positions:   3 (dip-recovery setups take longer to develop)
  Max new trades per hour:    10 (quality over quantity — this is a selective strategy)
  Intraday SOL drawdown:      2.0 SOL → pause for 60 minutes
  Weekly SOL drawdown:        8.0 SOL → full stop, manual review

Conviction Scaling:
  If buyRatio_5s > 0.70 AND volume expansion > 2.0×:  use 0.25 SOL
  If buyRatio_5s > 0.60 AND volume expansion > 1.5×:  use 0.15 SOL
  Otherwise:                                           use 0.10 SOL
  (Never bet maximum size on a marginal signal)
```

---

## PART 2: BUILD PROMPT — INTEGRATION WITH PUMP-CHART DASHBOARD

---

### Context for the AI Coding Agent

```
You are extending the existing Pump-Chart Dashboard — a FastAPI + asyncio Python backend
with a LightweightCharts frontend. The existing system already handles:
  - Live token data streaming via PumpPortal WebSocket
  - OHLCV candle aggregation via CandleAggregator
  - Physics-based StrategyEngine with Kalman filters and ROC
  - Forward testing and live trading execution modules
  - SQLite persistence via candles.db

Your task is to add a SNIPER subsystem that implements the dip-recovery entry strategy
described in this document. The sniper must integrate cleanly into the existing codebase
without breaking existing functionality. All new components live in backend/sniper/.
```

---

### SNIPER Section — New Module: `backend/sniper/`

```
backend/
├── sniper/
│   ├── __init__.py
│   ├── sniper_engine.py          # Main orchestration: per-token sniper lifecycle
│   ├── launch_detector.py        # Detects Act 1 spike and Act 2 dip on new tokens
│   ├── pressure_analyzer.py      # Buy/sell pressure ratio calculations
│   ├── chart_validator.py        # Real vs fake chart classification
│   ├── entry_signal.py           # All 6 entry conditions evaluated per candle
│   ├── exit_signal.py            # All 6 exit triggers evaluated per tick and per candle
│   ├── fee_filter.py             # Global fees paid filter (≥ 0.5 SOL)
│   ├── forward_tester.py         # Paper trading module for the sniper strategy
│   └── sniper_router.py          # FastAPI router: new REST + WebSocket endpoints
```

---

### Component Specifications

---

#### `backend/sniper/launch_detector.py`

```python
"""
IMPLEMENTATION SPEC

Purpose:
  Tracks every new token from the moment it is created and classifies which
  lifecycle act it is currently in. Feeds the SniperEngine with act-transition events.

Class: LaunchDetector(mint: str, candle_aggregator: CandleAggregator)

Internal State:
  genesis_mc: float               # MC at creation (from first bonding curve read)
  spike_high_mc: float            # Highest MC observed so far
  spike_high_time: float          # Unix timestamp when spike high was set
  lowest_mc_since_spike: float    # Lowest MC after spike_high was set
  lowest_mc_time: float           # Timestamp of lowest MC post-spike
  current_act: Literal[1, 2, 3]  # Which act the token is currently in
  fees_paid_sol: float            # Cumulative pump.fun fees in SOL

Act Transition Logic:
  Act 1 → Act 2 (spike occurred):
    spike_high_mc must be > genesis_mc * 1.60  (price rose 60%+ from creation)
    This threshold catches the dev/bundle run-up while ignoring noise.
    Transition fires when current_mc drops > 15% from spike_high_mc.

  Act 2 → Act 3 (floor forming):
    dip_depth = (spike_high_mc - lowest_mc_since_spike) / spike_high_mc
    dip_depth >= 0.35  (minimum 35% dip from the spike high)
    Price has been within 5% of lowest_mc_since_spike for ≥ 8 consecutive seconds
    (floor is stable, not still falling)

Events Emitted (via asyncio.Queue):
  ActTransitionEvent:
    mint: str
    new_act: int
    timestamp: float
    spike_high_mc: float
    floor_mc: float
    dip_depth: float
    fees_paid_sol: float

Update Method:
  async def on_trade(trade: TradeEvent) -> None
    Called by PumpPortal stream handler for every buy/sell on this token.
    Updates all internal state. Emits ActTransitionEvent when act changes.

  Fees Calculation:
    fees_paid_sol += trade.sol_amount * 0.01  (only for BUY trades — pump.fun charges 1% on buys)
    Source: compute locally from PumpPortal trade events, OR fetch from pump.fun V3 REST
    endpoint GET /coins/{mint} which returns a `total_tx_count` and related metrics.
    Cross-reference: the bondingCurve account's realSolReserves grows by 0.99× every buy,
    so fees = realSolReserves × (0.01/0.99) as an on-chain derivation.

IMPORTANT — Time Window for Act 1 Detection:
  If no spike_high transition occurs within 5 minutes of genesis, the token is
  either dead or a very slow mover. Mark it STALE and stop tracking it.
  Stale tokens are not eligible for the sniper strategy.
"""
```

---

#### `backend/sniper/pressure_analyzer.py`

```python
"""
IMPLEMENTATION SPEC

Purpose:
  Computes buy/sell pressure metrics from the per-candle data produced by
  the EXTENDED CandleAggregator (see CandleAggregator extension below).
  Returns a PressureSnapshot object used by entry_signal.py and exit_signal.py.

REQUIRED EXTENSION TO CandleAggregator:
  The existing CandleAggregator builds OHLCV candles from trade ticks.
  Extend each OHLCV record with two additional fields:
    buy_volume: float   # sum of sol_amount for trades where side == "buy"
    sell_volume: float  # sum of sol_amount for trades where side == "sell"
  The PumpPortal trade event already includes a `is_buy` boolean field —
  use this to classify each tick. Do NOT use price direction as a proxy;
  use the explicit buy/sell flag from the event.

@dataclass
class PressureSnapshot:
    buy_ratio_1s: float      # buyVol / totalVol for the most recent 1s candle
    buy_ratio_5s: float      # rolling 5-candle average of buy_ratio_1s
    buy_ratio_10s: float     # rolling 10-candle average of buy_ratio_1s
    buy_ratio_trend: float   # buy_ratio_5s minus buy_ratio_10s (positive = improving)
    current_buy_vol: float   # buyVolume in most recent 1s candle (SOL)
    avg_buy_vol: float       # average buyVolume over the last 10 candles (SOL)
    volume_expansion: float  # current_buy_vol / avg_buy_vol (>1.0 means above average)
    net_pressure: float      # buyVolume - sellVolume over last 3 candles (SOL)
    large_sell_spike: bool   # True if current candle sellVolume > avg_total_vol * 3.0
    upper_wick_count: int    # count of last 3 candles where upper_wick > body

def compute_pressure(candles: list[OHLCVExtended]) -> PressureSnapshot:
    """
    candles is the last 10 one-second candles, most recent last.
    Returns a PressureSnapshot computed from this window.
    Handles edge cases: if fewer than 3 candles available, return None
    (not enough data — do not trigger entry).
    """

Upper Wick Calculation:
  For a candle: upper_wick = high - max(open, close)
                body = abs(close - open)
  upper_wick > body means rejection — supply is beating demand at the high.

Avg Total Volume:
  avg_total_vol = mean of (buy_volume + sell_volume) for the candle window
  Used for the large_sell_spike threshold in Exit Trigger 3.
"""
```

---

#### `backend/sniper/chart_validator.py`

```python
"""
IMPLEMENTATION SPEC

Purpose:
  Classify a token's chart as REAL (organic) or FAKE (manipulated/bundled)
  using the first 30 seconds of candle data after Act 1 detection.
  Returns a ChartClassification with a confidence score.

@dataclass
class ChartClassification:
    is_real: bool
    confidence: float    # 0.0 to 1.0
    flags: list[str]     # human-readable explanation of what triggered classification

def classify_chart(
    candles: list[OHLCVExtended],   # first 30–60s of candles for this token
    launch_detector: LaunchDetector
) -> ChartClassification:

FAKE indicators (each adds to a fake_score; fake_score >= 3 → is_real = False):

1. STAIRCASE PATTERN (weight: 2)
   In the Act 1 spike candles: count how many are green (close > open).
   If >= 90% of candles in the spike are green with no red candle > 2% body:
   → staircase_pattern = True, fake_score += 2
   Real price discovery has back-and-forth movement even on the way up.

2. DOMINANT FIRST CANDLE (weight: 2)
   first_candle_body = abs(candles[0].close - candles[0].open)
   max_subsequent_body = max(abs(c.close - c.open) for c in candles[1:10])
   If first_candle_body > max_subsequent_body * 3.0:
   → dev_dominated_launch = True, fake_score += 2
   If the dev buy is 3× larger than any subsequent activity, it's a solo dev launch.

3. UNIFORM TRADE SIZES (weight: 1)
   Compute standard deviation of per-trade sol_amounts in first 60 seconds.
   If std_dev < mean_trade_size * 0.15 (trades are suspiciously uniform):
   → uniform_sizing = True, fake_score += 1
   Bot activity produces uniform-size transactions.

4. NO GENUINE DIP (weight: 2)
   If launch_detector.current_act == 1 (spike has not been followed by any dip):
   → no_dip_occurred = True, fake_score += 2
   A token that has only gone up is still in the bundler phase.

5. SELL VOLUME ABSENT IN DIP (weight: 1)
   During the dip period (Act 2 candles), if total sell_volume / total_buy_volume < 0.3:
   → dip_without_sellers = True, fake_score += 1
   A real dip has sellers — fake dips are controlled pullbacks.

REAL indicators (each subtracts from fake_score):

R1. Dip_depth >= 0.40: fake_score -= 1
R2. Multiple distinct wallet sizes in buys (high variance): fake_score -= 1
R3. Green and red candles roughly alternating in the spike (not pure stairs): fake_score -= 1

Final Classification:
  fake_score >= 3: is_real = False
  fake_score <= 1: is_real = True, confidence = 0.9
  fake_score == 2: is_real = False (borderline cases default to rejection for safety)
"""
```

---

#### `backend/sniper/entry_signal.py`

```python
"""
IMPLEMENTATION SPEC

Purpose:
  Evaluates all 6 entry conditions on each 1-second candle close during Act 3.
  Returns an EntrySignal when all conditions are simultaneously satisfied.

@dataclass
class EntrySignal:
    triggered: bool
    timestamp: float
    mint: str
    current_mc: float
    entry_price_sol: float   # price per token at signal time (for paper trade recording)
    pressure: PressureSnapshot
    conditions_met: dict[str, bool]  # each condition's pass/fail for logging
    conviction: Literal["high", "medium", "low"]  # maps to position size

def evaluate_entry(
    mint: str,
    launch_detector: LaunchDetector,
    pressure: PressureSnapshot,
    strategy_engine: StrategyEngine,   # existing engine — call its get_current_state()
    candles: list[OHLCVExtended]       # last 15 one-second candles
) -> EntrySignal:

CONDITION 1 — Dip Has Occurred:
  dip_depth = (launch_detector.spike_high_mc - launch_detector.lowest_mc_since_spike) \
              / launch_detector.spike_high_mc
  c1 = dip_depth >= 0.35

CONDITION 2 — Floor Has Formed:
  last_10_lows = [c.low for c in candles[-10:]]
  floor_low = min(last_10_lows)
  lows_std = std_dev(last_10_lows)
  c2 = (
    candles[-1].low > floor_low * 0.97  # not making new lows this candle
    and lows_std < floor_low * 0.03     # lows are stable (< 3% deviation)
  )

CONDITION 3 — Buy Pressure Ratio Turning Positive:
  c3 = (
    pressure.buy_ratio_1s >= 0.60
    and pressure.buy_ratio_5s >= 0.50
    and pressure.buy_ratio_trend > 0     # trend direction positive
  )

CONDITION 4 — Momentum Turning Positive:
  engine_state = strategy_engine.get_current_state(mint)
  roc_positive = engine_state.roc > 0
  roc_was_negative = engine_state.prior_roc <= 0   # crossed zero recently
  kalman_up = engine_state.kalman_estimate > engine_state.prior_kalman_estimate
  two_green = candles[-1].close > candles[-1].open and candles[-2].close > candles[-2].open
  c4 = roc_positive and kalman_up and two_green

CONDITION 5 — Volume Expansion:
  c5 = pressure.volume_expansion >= 1.5

CONDITION 6 — Price Above Floor:
  floor_price = min(c.low for c in candles[-10:])
  current_price = candles[-1].close
  c6 = current_price >= floor_price * 1.04

Conviction Mapping:
  if pressure.buy_ratio_5s > 0.70 and pressure.volume_expansion > 2.0:
      conviction = "high"      → position_size = 0.25 SOL
  elif pressure.buy_ratio_5s > 0.60 and pressure.volume_expansion > 1.5:
      conviction = "medium"    → position_size = 0.15 SOL
  else:
      conviction = "low"       → position_size = 0.10 SOL

Return EntrySignal with triggered = (c1 and c2 and c3 and c4 and c5 and c6)
"""
```

---

#### `backend/sniper/exit_signal.py`

```python
"""
IMPLEMENTATION SPEC

Purpose:
  Evaluates all 6 exit triggers continuously while a position is open.
  Some triggers fire on tick (intra-candle), others on candle close.
  Returns an ExitSignal the moment any trigger fires.

@dataclass
class ExitSignal:
    triggered: bool
    trigger_name: str     # which of the 6 triggers fired
    timestamp: float
    current_mc: float
    exit_price_sol: float
    urgency: Literal["immediate", "on_close"]
    # "immediate" = fire sell right now (tick-level), do not wait for candle close
    # "on_close"  = wait for current 1s candle to close, then sell

def evaluate_exit(
    position: OpenPosition,
    pressure: PressureSnapshot,
    strategy_engine: StrategyEngine,
    candles: list[OHLCVExtended],
    current_tick_price: float   # most recent trade price (intra-candle)
) -> ExitSignal:

TRIGGER 1 — Buy Pressure Collapse (candle close):
  buy_ratio_3s = mean(pressure.buy_ratio_1s for last 3 candles)
  KEEP a rolling deque of the last 3 buy_ratio_1s values.
  If buy_ratio_3s < 0.38 for 2 consecutive candle closes:
  → ExitSignal(trigger="buy_pressure_collapse", urgency="on_close")

TRIGGER 2 — Momentum Reversal (candle close):
  engine_state = strategy_engine.get_current_state(mint)
  roc_crossed_negative = (engine_state.roc < 0 and engine_state.prior_roc >= 0)
  kalman_turned_down = engine_state.kalman_estimate < engine_state.prior_kalman_estimate
  if roc_crossed_negative and kalman_turned_down:
  → ExitSignal(trigger="momentum_reversal", urgency="on_close")

TRIGGER 3 — Large Sell Spike (IMMEDIATE, tick-level):
  avg_total_vol = mean of (buy_vol + sell_vol) for last 10 candles
  current_candle_sell_vol = running sell_vol accumulation for current intra-candle
  if current_candle_sell_vol > avg_total_vol * 3.0:
  → ExitSignal(trigger="large_sell_spike", urgency="immediate")
  IMPORTANT: This must be evaluated on EVERY trade tick, not just candle closes.
  Use the PumpPortal raw trade stream, not the aggregated candles.

TRIGGER 4 — Upper Wick Exhaustion (candle close):
  if pressure.upper_wick_count >= 3:  # 3 consecutive rejection candles
    # additionally confirm no new high in those 3 candles
    last3_highs = [c.high for c in candles[-3:]]
    no_new_high = last3_highs[-1] <= max(last3_highs[:-1])
    if no_new_high:
    → ExitSignal(trigger="wick_exhaustion", urgency="on_close")

TRIGGER 5 — Time Stop (candle close):
  time_in_trade = current_time - position.entry_time
  pct_gain = (current_tick_price - position.entry_price) / position.entry_price
  if time_in_trade > 300 and pct_gain < 0.15:
  → ExitSignal(trigger="time_stop", urgency="on_close")

TRIGGER 6 — Hard Price Stop (IMMEDIATE, tick-level):
  pct_loss = (current_tick_price - position.entry_price) / position.entry_price
  if pct_loss <= -0.30:
  → ExitSignal(trigger="hard_stop", urgency="immediate")
  This must also be evaluated on every trade tick.

Precedence: Triggers 3 and 6 (urgency="immediate") take absolute priority.
If any immediate trigger fires, cancel any pending on_close evaluation.
"""
```

---

#### `backend/sniper/fee_filter.py`

```python
"""
IMPLEMENTATION SPEC

Purpose:
  Implements the global fees paid hard filter. Provides both a live-computed
  value and an API-sourced value, with automatic fallback.

async def get_fees_paid_sol(mint: str, bonding_curve_address: str) -> float:

  METHOD 1 — On-Chain Computation (preferred, most accurate):
    Fetch the bondingCurve account data via Solana RPC getAccountInfo.
    Decode the BondingCurveState struct.
    real_sol_reserves = state.realSolReserves / 1e9  (in SOL)
    fees_paid = real_sol_reserves * (0.01 / 0.99)
    (Because every buy deposits 99% to the curve; 1% goes as fee.
     So if curve has R SOL, total buys = R/0.99, fees = R/0.99 * 0.01 = R*0.01/0.99)

  METHOD 2 — PumpPortal Running Accumulation (fast, slightly delayed):
    LaunchDetector already accumulates fees_paid_sol from the trade stream.
    Use this value as the primary live check (updated on every trade event).
    No RPC call needed — instant.

  METHOD 3 — pump.fun V3 REST API (fallback):
    GET https://frontend-api-v3.pump.fun/coins/{mint}
    Parse response for the relevant field (total volume or transaction count).
    Approximate fees from total_buy_volume * 0.01.
    Use only if Methods 1 and 2 are unavailable.

def passes_fee_filter(fees_paid_sol: float, threshold: float = 0.5) -> bool:
    return fees_paid_sol >= threshold

Integration:
  In sniper_engine.py, check passes_fee_filter(launch_detector.fees_paid_sol)
  BEFORE running chart_validator, entry_signal, or any other analysis.
  It is the first gate after act_transition to Act 2.
  Re-check every 15 seconds for tokens still accumulating fees (they might
  cross the threshold later — maintain a watching list).
"""
```

---

#### `backend/sniper/forward_tester.py`

```python
"""
IMPLEMENTATION SPEC

Purpose:
  Paper-trades the sniper strategy on live data with zero real capital at risk.
  This is the validation layer — run it for at minimum 500 simulated trades
  before enabling live execution.

DATABASE SCHEMA (add to candles.db):

CREATE TABLE forward_test_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mint            TEXT NOT NULL,
    token_name      TEXT,
    token_symbol    TEXT,
    detected_at     REAL,             -- unix timestamp
    entry_time      REAL,
    exit_time       REAL,
    entry_mc        REAL,
    exit_mc         REAL,
    spike_high_mc   REAL,
    floor_mc        REAL,
    dip_depth       REAL,
    fees_at_entry   REAL,             -- SOL fees paid when we entered
    entry_price_sol REAL,
    exit_price_sol  REAL,
    position_size   REAL,             -- simulated SOL spent
    gross_pnl_sol   REAL,             -- before fees
    net_pnl_sol     REAL,             -- after simulated fees + slippage
    pnl_pct         REAL,
    exit_trigger    TEXT,             -- which exit trigger fired
    conviction      TEXT,             -- high/medium/low
    buy_ratio_at_entry  REAL,
    volume_expansion_at_entry REAL,
    all_conditions  TEXT,             -- JSON of all 6 condition booleans at entry
    created_at      REAL DEFAULT (unixepoch())
);

CREATE INDEX idx_ftt_mint ON forward_test_trades(mint);
CREATE INDEX idx_ftt_created ON forward_test_trades(created_at DESC);

class ForwardTester:
    def __init__(self, db_path: str, paper_size_sol: float = 0.15):
        ...

    async def record_entry(
        self,
        entry_signal: EntrySignal,
        launch_detector: LaunchDetector
    ) -> int:
        # Returns the trade ID. Stores entry data; exit fields NULL until closed.

    async def record_exit(
        self,
        trade_id: int,
        exit_signal: ExitSignal,
        position_size_sol: float
    ) -> None:
        # Completes the record with exit data and computes P&L:
        # gross_pnl = (exit_price - entry_price) / entry_price * position_size
        # costs = position_size * 0.01  (buy fee)
              + position_size * (exit_price/entry_price) * 0.01  (sell fee)
              + position_size * 0.0025  (slippage in)
              + position_size * (exit_price/entry_price) * 0.0025  (slippage out)
              + 0.001  (fixed tx cost SOL)
        # net_pnl = gross_pnl - costs

METRICS ENDPOINT:
  Add REST endpoint GET /api/sniper/forward-test/stats that returns:
  {
    "total_trades": int,
    "win_rate": float,           -- trades with net_pnl > 0
    "avg_net_pnl_sol": float,
    "total_net_pnl_sol": float,
    "avg_hold_time_seconds": float,
    "exit_trigger_breakdown": dict,  -- count per trigger type
    "conviction_breakdown": dict,    -- win rate per conviction level
    "avg_dip_depth_winners": float,  -- average dip depth on winning trades
    "avg_dip_depth_losers": float,
    "best_trade": dict,
    "worst_trade": dict
  }
  Use this output to calibrate all thresholds before going live.
"""
```

---

#### `backend/sniper/sniper_engine.py`

```python
"""
IMPLEMENTATION SPEC

Purpose:
  Main orchestrator for the sniper strategy. One SniperEngine instance exists
  globally and manages all per-token LaunchDetector instances.
  Subscribes to the existing PumpPortal trade stream.
  Coordinates filters → detection → signal → forward test OR live execution.

class SniperEngine:
    def __init__(
        self,
        candle_aggregator_factory,  # callable that creates a CandleAggregator for a mint
        strategy_engine: StrategyEngine,  # existing StrategyEngine instance
        forward_tester: ForwardTester,
        live_trader,                # existing LiveTrader instance (None if forward-only mode)
        config: SniperConfig
    ):
        self.detectors: dict[str, LaunchDetector] = {}
        self.open_positions: dict[str, OpenPosition] = {}
        self.watching: set[str] = set()   # tokens past fee threshold, awaiting entry signal
        self.stale: set[str] = set()      # tokens timed out, no longer tracking

MAIN FLOW:

  on_new_token(token_event):
    # Called by PumpPortal handler when a new token is created
    if token_event.creator in blacklist: return
    if token_event.creator_launch_count_24h > 5: return
    detector = LaunchDetector(token_event.mint, ...)
    self.detectors[token_event.mint] = detector
    # Schedule stale check: if no Act 1 transition in 5 min, mark stale and remove

  on_trade(trade_event):
    # Called by PumpPortal handler for every buy/sell on any token
    mint = trade_event.mint
    if mint not in self.detectors: return
    if mint in self.stale: return

    detector = self.detectors[mint]
    await detector.on_trade(trade_event)    # updates LaunchDetector state
    await candle_aggregator[mint].on_trade(trade_event)  # updates OHLCV + buy/sell vols

    # Gate 1: Fee filter (re-evaluate on every trade for tokens in watching)
    if not passes_fee_filter(detector.fees_paid_sol): return

    # Gate 2: Must be in Act 3 to look for entry signals
    if detector.current_act < 3:
        # Still in Act 1 or 2 — just accumulate data, check for act transitions
        if detector.current_act == 2 and mint not in self.watching:
            # Validate chart now that we have dip data
            classification = classify_chart(candles, detector)
            if not classification.is_real: return   # hard reject fake charts
            self.watching.add(mint)
        return

    # Act 3 — evaluate entry
    if mint in self.open_positions: 
        # Already in a position — evaluate exits
        await self._evaluate_exit(mint, trade_event)
        return

    if mint not in self.watching: return

    candles = candle_aggregator[mint].get_last_n(15)
    if len(candles) < 10: return  # insufficient data

    pressure = compute_pressure(candles)
    entry = evaluate_entry(mint, detector, pressure, strategy_engine, candles)

    if entry.triggered:
        await self._execute_entry(entry, detector)

  async def _execute_entry(self, entry: EntrySignal, detector: LaunchDetector):
    position = OpenPosition(
        mint=entry.mint,
        entry_price=entry.entry_price_sol,
        entry_mc=entry.current_mc,
        entry_time=entry.timestamp,
        size_sol=entry.conviction_to_size(),
        peak_mc=entry.current_mc
    )
    self.open_positions[entry.mint] = position
    if config.mode == "forward_test":
        trade_id = await forward_tester.record_entry(entry, detector)
        position.forward_test_id = trade_id
    elif config.mode == "live":
        await live_trader.execute_buy(entry.mint, position.size_sol)
    # Broadcast to WebSocket subscribers (sniper_router.py)
    await broadcast_sniper_event(SniperEvent(type="entry", ...))

  async def _evaluate_exit(self, mint: str, trade_event: TradeEvent):
    position = self.open_positions[mint]
    candles = candle_aggregator[mint].get_last_n(15)
    pressure = compute_pressure(candles)
    engine_state = strategy_engine.get_current_state(mint)
    exit_signal = evaluate_exit(position, pressure, engine_state, candles,
                                trade_event.price)
    if exit_signal.triggered:
        if exit_signal.urgency == "immediate":
            await self._execute_exit(mint, exit_signal)
        else:  # on_close — wait for candle boundary
            position.pending_exit = exit_signal  # checked in on_candle_close

  async def on_candle_close(self, mint: str):
    # Called by CandleAggregator at each 1s boundary
    if mint in self.open_positions:
        pos = self.open_positions[mint]
        if pos.pending_exit:
            await self._execute_exit(mint, pos.pending_exit)
        else:
            # Re-evaluate on_close triggers
            candles = candle_aggregator[mint].get_last_n(15)
            pressure = compute_pressure(candles)
            ...

@dataclass
class SniperConfig:
    mode: Literal["forward_test", "live"]
    fee_threshold_sol: float = 0.5
    max_concurrent_positions: int = 3
    daily_loss_limit_sol: float = 2.0

@dataclass
class OpenPosition:
    mint: str
    entry_price: float
    entry_mc: float
    entry_time: float
    size_sol: float
    peak_mc: float
    forward_test_id: int | None = None
    pending_exit: ExitSignal | None = None
"""
```

---

#### `backend/sniper/sniper_router.py`

```python
"""
IMPLEMENTATION SPEC

Purpose:
  FastAPI router that exposes new HTTP and WebSocket endpoints for the sniper.
  Mounts at prefix /sniper on the existing FastAPI app.

REST ENDPOINTS:

  GET /sniper/status
    Returns current SniperEngine state:
    {
      "mode": "forward_test" | "live",
      "tracking_count": int,
      "open_positions": [...],
      "today_pnl_sol": float,
      "today_trade_count": int,
      "forward_test_stats": {...}
    }

  POST /sniper/mode
    Body: {"mode": "forward_test" | "live"}
    Switches between paper trading and live execution.
    Returns 400 if trying to go live without >= 200 forward test trades in DB.

  GET /sniper/forward-test/trades
    Query params: limit (default 50), offset (default 0)
    Returns paginated forward test trade records from the DB.

  GET /sniper/forward-test/stats
    Returns the metrics object from ForwardTester (win rate, avg PnL, etc.)

  GET /sniper/watching
    Returns list of tokens currently being watched (past fee threshold, in Act 3)
    with their current detector state.

WEBSOCKET ENDPOINT:

  WS /ws/sniper
    Authenticates connection (check API key if applicable).
    Sends a stream of SniperEvent objects as JSON:

    Types of SniperEvent:
      { "type": "token_watching",   "mint": str, "fees_sol": float, "dip_depth": float }
      { "type": "entry",            "mint": str, "entry_mc": float, "conviction": str, "size_sol": float }
      { "type": "exit",             "mint": str, "exit_mc": float, "trigger": str, "net_pnl_sol": float }
      { "type": "act_transition",   "mint": str, "new_act": int, "spike_high_mc": float }
      { "type": "filter_rejected",  "mint": str, "reason": str }

    The frontend (js/app.js) subscribes to this endpoint and renders:
      - A live feed of tokens being tracked
      - Entry/exit markers on the chart (reuse existing B/S marker rendering from LightweightCharts)
      - Running P&L display (forward test mode)

MOUNTING IN main.py:
  from sniper.sniper_router import router as sniper_router
  app.include_router(sniper_router)
  # Also initialize SniperEngine and pass it to the router on startup:
  @app.on_event("startup")
  async def startup():
      ...existing startup code...
      app.state.sniper = SniperEngine(...)
      asyncio.create_task(app.state.sniper.run())
"""
```

---

### Integration Map: How Sniper Connects to Existing Architecture

```
PumpPortal WS (existing)
    │
    ├──► [existing handlers for candles, analytics, backtester]
    │
    └──► SniperEngine.on_new_token() / on_trade()
              │
              ├── LaunchDetector (per token)     ──► FeeFilter
              ├── CandleAggregator (extended)    ──► PressureAnalyzer
              ├── ChartValidator                 ──► hard reject fake charts
              ├── EntrySignal ◄─── StrategyEngine (existing Kalman/ROC)
              ├── ExitSignal  ◄─── StrategyEngine (existing Kalman/ROC)
              │
              ├── ForwardTester ──► SQLite forward_test_trades table
              └── LiveTrader (existing) ──► Solana RPC / Jupiter / pump.fun

New WebSocket /ws/sniper ──► Frontend js/app.js
  (entry/exit events, act transitions, filter rejections)
```

---

### Environment Configuration Additions

```
# Add to existing .env:
SNIPER_MODE=forward_test          # "forward_test" or "live"
SNIPER_FEE_THRESHOLD_SOL=0.5
SNIPER_MAX_POSITIONS=3
SNIPER_DAILY_LOSS_LIMIT_SOL=2.0
SNIPER_POSITION_SIZE_HIGH=0.25
SNIPER_POSITION_SIZE_MED=0.15
SNIPER_POSITION_SIZE_LOW=0.10
SNIPER_MIN_FORWARD_TRADES_FOR_LIVE=200  # must complete this many paper trades first
```

---

### Frontend Additions (js/app.js)

```javascript
/**
 * NEW: Sniper panel in the dashboard UI
 *
 * 1. Add a "SNIPER" tab/panel to the existing layout.
 *
 * 2. Connect to /ws/sniper WebSocket on panel open.
 *
 * 3. Render a live feed table:
 *    Columns: Token | Act | Fees Paid (SOL) | Dip Depth | Status | Action
 *    Rows update in real-time as SniperEvents arrive.
 *
 * 4. When an "entry" event arrives for the currently viewed chart:
 *    - Place a green "B" marker on the LightweightCharts candlestick series
 *      at the entry candle's timestamp.
 *    - Show a horizontal line at entry_price (labeled "Entry")
 *
 * 5. When an "exit" event arrives:
 *    - Place a red "S" marker at the exit candle's timestamp.
 *    - Remove the entry line.
 *    - Show a P&L flash notification (green if profit, red if loss).
 *
 * 6. Add a "Forward Test Stats" section showing the running metrics from
 *    GET /sniper/forward-test/stats, auto-refreshed every 30 seconds.
 *
 * 7. Add a mode toggle: "Paper Trading" ↔ "Live Trading" switch.
 *    On switching to "Live", POST /sniper/mode with confirmation dialog.
 *    Display warning if forward test trade count < 200.
 */
```

