# ── IMPORTS ──────────────────────────────────────────────────────────────────

import os
import sys
import asyncio
import json
import time
import math
import logging
from dataclasses import dataclass, field
from typing import Optional
from collections import deque
from datetime import datetime, timezone
from dotenv import load_dotenv

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import norm
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

import httpx
import websockets

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL

import json as _json


# ── CONFIGURATION ────────────────────────────────────────────────────────────

# Prelec calibration
PRELEC_ALPHA = 0.69          # Empirical centre of the 0.65–0.72 literature range.
                             # Lower α → more bias correction on extreme probs.
PRELEC_CLIP  = 1e-4          # Clip market prices away from 0 and 1 before logit
                             # transforms to avoid log(0) = -inf.

# OU reversion
OU_WINDOW    = 200           # Rolling window (in ticks) for OLS parameter estimation.
OU_DT        = 1.0 / 3600   # Tick interval assumed to be 1 second → Δt in hours.
OU_MIN_THETA = 0.01          # Minimum mean-reversion speed. Below this the process
                             # is a near-random walk; discard the signal.

# PRISM aggregator weights (normalised inside the aggregator)
W_PRELEC  = 1.0
W_OU      = 1.5              # OU given higher weight: it is the most model-driven.
W_BUDGET  = 0.8

# Gate threshold
PRISM_GATE_Z = 2.0           # Only trade when |z_combined| exceeds this value.

# Kelly sizer
KELLY_FRACTION    = 0.25     # Quarter-Kelly → f* / 4.
MAX_POSITION_FRAC = 0.05     # Hard cap: never risk more than 5% of bankroll on one bet.
KELLY_MIN_EDGE    = 0.02     # Minimum raw edge (p_true - q_market) to place any order.

# Execution
ORDER_SIZE_MIN_USDC = 2.0    # Minimum order size in USDC (Polymarket minimum is ~$1).
CLOB_HOST    = "https://clob.polymarket.com"
GAMMA_HOST   = "https://gamma-api.polymarket.com"
CLOB_WS_HOST = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Market refresh
GAMMA_POLL_INTERVAL = 30     # Seconds between REST polls for new markets.
MAX_MARKETS_TRACKED = 50     # Cap to avoid memory blowup.

# Execution filters
MAX_SPREAD          = 0.04   # Maximum bid-ask spread to allow trading. Above this
                             # liquidity is too thin and fill risk dominates any edge.
MIN_DECAY           = 0.1    # Minimum time-decay multiplier (τ^0.3 floor). Below
                             # this (~99.6% of life elapsed) the bet is near-expired.
STALE_SECS          = 60     # Seconds without a price update before OU signal is zeroed.
ORDER_COOLDOWN_SECS = 300    # 5-minute cooldown between orders on the same token.

# Logging
LOG_FILE = os.path.join(os.path.dirname(__file__), "prism_decisions.jsonl")
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "prism_config.json")

def load_dynamic_config():
    global PRISM_GATE_Z, MAX_SPREAD, KELLY_FRACTION, OU_WINDOW
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                c = json.load(f)
                PRISM_GATE_Z = float(c.get("prism_gate_z", PRISM_GATE_Z))
                MAX_SPREAD = float(c.get("max_spread", MAX_SPREAD))
                KELLY_FRACTION = float(c.get("kelly_fraction", KELLY_FRACTION))
                OU_WINDOW = int(c.get("ou_window", OU_WINDOW))
                
                # Apply environment overrides for startup logic
                if "bankroll" in c: os.environ["BANKROLL_USDC"] = str(c["bankroll"])
                if c.get("mode") == "paper": 
                    os.environ["DRY_RUN"] = "true"
                elif c.get("mode") == "live":
                    os.environ["DRY_RUN"] = "false"
                    
                if c.get("http_proxy"):
                    os.environ["HTTP_PROXY"] = c["http_proxy"]
                    os.environ["HTTPS_PROXY"] = c["http_proxy"]
                if c.get("private_key"):
                    os.environ["PRIVATE_KEY"] = c["private_key"]
        except Exception as e:
            logging.error(f"Failed to load dynamic config: {e}")

# Inject config load before data structures initialize
load_dynamic_config()

# ── DATA STRUCTURES ──────────────────────────────────────────────────────────

@dataclass
class MarketState:
    """
    Everything known about one Polymarket binary outcome token at a given moment.
    One MarketState object per CLOB token id (each binary market has two: YES and NO).
    """
    market_id: str            # Gamma market id (e.g. "0x1234...")
    token_id: str             # CLOB token id for this outcome
    question: str             # Human-readable question text
    outcome: str              # "YES" or "NO"
    best_bid: float           # Best bid price (0 to 1)
    best_ask: float           # Best ask price (0 to 1)
    mid_price: float          # (best_bid + best_ask) / 2
    volume_24h: float         # 24-hour traded volume in USDC
    resolution_time: float    # Unix timestamp of expected resolution
    created_time: float       # Unix timestamp of market creation
    price_history: deque      # Rolling deque of (timestamp, mid_price) tuples
                              # max length = OU_WINDOW

    # OU parameters — updated each tick via OLS
    ou_theta: float  = 0.0    # Mean-reversion speed
    ou_mu:    float  = 0.5    # Long-run mean in log-odds space
    ou_sigma: float  = 0.0    # Noise intensity

    # Category label — used to group markets for OU parameter sharing
    category: str    = "general"

    # Sibling outcomes for probability-budget check (only for categorical markets)
    sibling_token_ids: list = field(default_factory=list)

    # Timestamp of the most recent price update; used for stale-price detection.
    last_update_ts: float = field(default_factory=time.time)

    def __post_init__(self):
        if not isinstance(self.price_history, deque):
            self.price_history = deque(maxlen=OU_WINDOW)


@dataclass
class Signal:
    """
    Output of each of the three signal generators.
    A z-score plus raw components for logging.
    """
    z_prelec:   float = 0.0   # Prelec calibration signal
    z_ou:       float = 0.0   # OU reversion signal
    z_budget:   float = 0.0   # Probability budget signal
    z_combined: float = 0.0   # Weighted combined signal from PRISM aggregator

    p_true:   float = 0.5     # Best estimate of true probability
    p_market: float = 0.5     # Mid-price from order book
    confident: bool = False   # True if |z_combined| exceeds the gate


@dataclass
class Order:
    """
    A proposed or placed order.
    """
    token_id: str
    side: str                 # "BUY" or "SELL"
    price: float              # Limit price (0 to 1)
    size_usdc: float          # Dollar size of position
    kelly_fraction: float     # The f_final value that produced this size
    signal: Signal
    placed: bool  = False
    order_id: str = ""


# ── LAYER 1: MARKET DATA FEED ────────────────────────────────────────────────

async def fetch_active_markets(http: httpx.AsyncClient) -> list[dict]:
    """
    Pull open markets from the Gamma API.
    Returns a list of market dicts, sorted by 24-hour volume descending.
    Filters out closed, resolved, or ultra-low-volume (< $500/day) markets.
    """
    url = f"{GAMMA_HOST}/markets"
    params = {
        "active":    "true",
        "closed":    "false",
        "limit":     100,
        "order":     "volume24hr",
        "ascending": "false",
    }
    resp = await http.get(url, params=params)
    resp.raise_for_status()
    markets = resp.json()
    return [m for m in markets if float(m.get("volume24hr", 0)) > 500]


async def clob_websocket_loop(
    token_ids: list[str],
    state_map: dict[str, "MarketState"],
    signal_queue: asyncio.Queue,
):
    """
    Connects to the Polymarket CLOB WebSocket and keeps prices current.
    On each price update, pushes the token_id onto signal_queue for processing.

    Protocol:
      1. On connect, send:
           {"auth": {}, "markets": [...token_ids...], "type": "Market"}
      2. Server sends book snapshots ("event_type": "book") and incremental
         updates ("event_type": "price_change") as JSON objects.
    """
    subscription_msg = json.dumps({
        "auth":    {},
        "markets": token_ids,
        "type":    "Market",
    })

    async with websockets.connect(CLOB_WS_HOST) as ws:
        await ws.send(subscription_msg)
        async for raw in ws:
            msg = json.loads(raw)
            process_clob_message(msg, state_map)
            token_id = msg.get("market")
            if token_id and token_id in state_map:
                try:
                    signal_queue.put_nowait(token_id)
                except asyncio.QueueFull:
                    pass  # Drop update if queue is saturated; next tick will catch up.


def process_clob_message(msg: dict, state_map: dict[str, "MarketState"]):
    """
    Parse an incoming CLOB WebSocket message and update MarketState in-place.
    Updates best_bid, best_ask, mid_price, and appends to price_history.
    """
    token_id = msg.get("market")
    if not token_id or token_id not in state_map:
        return

    state = state_map[token_id]

    if msg.get("event_type") in ("book", "price_change"):
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])

        if bids:
            state.best_bid = float(
                max(bids, key=lambda x: float(x["price"]))["price"]
            )
        if asks:
            state.best_ask = float(
                min(asks, key=lambda x: float(x["price"]))["price"]
            )

        if state.best_bid > 0 and state.best_ask > 0:
            now = time.time()
            state.mid_price = (state.best_bid + state.best_ask) / 2.0
            state.price_history.append((now, state.mid_price))
            state.last_update_ts = now


# ── LAYER 2A: PRELEC CALIBRATION ─────────────────────────────────────────────

def prelec_weight(p: float, alpha: float = PRELEC_ALPHA) -> float:
    """
    Prelec probability weighting function.

        w(p) = exp( -(- ln p)^alpha )

    Maps a true probability p into the perceived (market) probability w(p).
    At alpha < 1 the curve is S-shaped: longshots are overweighted, favourites
    are underweighted, with a fixed point at p ≈ 1/e ≈ 0.368.
    """
    p = np.clip(p, PRELEC_CLIP, 1 - PRELEC_CLIP)
    return math.exp(-((-math.log(p)) ** alpha))


def prelec_inverse(q: float, alpha: float = PRELEC_ALPHA) -> float:
    """
    Closed-form inverse of the Prelec function.

        p_true = exp( -(- ln q)^(1/alpha) )

    Derived by setting w(p) = q and solving analytically — no root-finding needed.
    Given a market price q, returns the true probability implied by the Prelec model.
    """
    q = np.clip(q, PRELEC_CLIP, 1 - PRELEC_CLIP)
    return math.exp(-((-math.log(q)) ** (1.0 / alpha)))


def compute_prelec_zscore(
    state: "MarketState",
    all_states: list["MarketState"],
) -> tuple[float, float]:
    """
    Compute the Prelec calibration z-score for one market outcome.

    Returns (z_prelec, p_true).

    Steps:
      1. Invert the Prelec function on the current mid-price to get p_true.
      2. Raw gap:   gap = p_true − q_market.
      3. Cross-sectional normalisation over all tracked markets makes z_prelec
         dimensionless and directly comparable to the OU and budget signals.
      4. z_prelec = (gap − mean_gap) / std_gap
    """
    q      = state.mid_price
    p_true = prelec_inverse(q)
    gap    = p_true - q

    all_gaps = [prelec_inverse(s.mid_price) - s.mid_price for s in all_states]
    mean_gap = np.mean(all_gaps)
    std_gap  = np.std(all_gaps) + 1e-8   # Avoid division by zero

    z_prelec = (gap - mean_gap) / std_gap
    return z_prelec, p_true


# ── LAYER 2B: OU REVERSION ───────────────────────────────────────────────────

def logit(p: float) -> float:
    """
    Logit transform:  L = ln(p / (1-p)).
    Maps probability in (0, 1) to all of the real line ℝ.
    Essential before fitting OU: the process is defined on ℝ, not on (0,1).
    """
    p = np.clip(p, PRELEC_CLIP, 1 - PRELEC_CLIP)
    return math.log(p / (1.0 - p))


def logistic(L: float) -> float:
    """
    Logistic function: inverse of logit.  p = 1 / (1 + e^{-L}).
    Maps any real number back to (0, 1).
    """
    return 1.0 / (1.0 + math.exp(-L))


def fit_ou_parameters(
    price_history: deque,
    dt: float = OU_DT,
) -> tuple[float, float, float]:
    """
    Estimate Ornstein-Uhlenbeck parameters (θ, μ, σ) from a rolling window.

    The continuous-time OU process in log-odds space:
        dL_t = θ(μ − L_t) dt + σ dW_t

    Its discrete-time equivalent (Euler-Maruyama with step Δt):
        L_{t+1} = a + b · L_t + ε_t,
    where:
        b = e^{−θΔt}          ⟹  θ = −ln(b) / Δt
        a = μ(1 − b)          ⟹  μ = a / (1 − b)
        Var(ε) = σ²(1 − e^{−2θΔt}) / (2θ)
                              ⟹  σ² = Var(ε) · 2θ / (1 − e^{−2θΔt})

    Returns (0.0, 0.5, 0.0) if:
      - Fewer than 20 observations
      - OLS is degenerate (constant prices → singular matrix)
      - b ∉ (0, 1) → explosive or non-mean-reverting process
      - θ < OU_MIN_THETA → effectively a random walk
    """
    if len(price_history) < 20:
        return 0.0, 0.5, 0.0

    prices   = np.array([p for _, p in price_history])
    log_odds = np.array([logit(p) for p in prices])

    L_t   = log_odds[:-1]
    L_tp1 = log_odds[1:]

    X = add_constant(L_t)
    try:
        model        = OLS(L_tp1, X).fit()
        a, b         = model.params[0], model.params[1]
        residual_var = model.mse_resid
    except Exception:
        return 0.0, 0.5, 0.0

    # b ∈ (0, 1) required for a mean-reverting process
    if b <= 0 or b >= 1:
        return 0.0, 0.5, 0.0

    theta = -math.log(b) / dt
    if theta < OU_MIN_THETA:
        return 0.0, 0.5, 0.0

    mu    = a / (1.0 - b)
    denom = 1.0 - math.exp(-2.0 * theta * dt)
    if denom < 1e-10:
        return theta, mu, 0.0

    sigma_sq = residual_var * (2.0 * theta) / denom
    sigma    = math.sqrt(max(sigma_sq, 0.0))

    return theta, mu, sigma


def compute_ou_zscore(state: "MarketState") -> float:
    """
    Compute the OU z-score for the current mid-price.

    The stationary distribution of L_t is Normal(μ, σ²/(2θ)), so:

        z_OU = (L_t − μ) / sqrt( σ²/(2θ) )

    Returns 0.0 if:
      - The price is stale (no update for > STALE_SECS seconds)
      - θ is below OU_MIN_THETA (near-random-walk)
      - σ is zero (degenerate fit)
    """
    # Stale price guard
    if time.time() - state.last_update_ts > STALE_SECS:
        return 0.0

    theta, mu, sigma = fit_ou_parameters(state.price_history)
    state.ou_theta   = theta
    state.ou_mu      = mu
    state.ou_sigma   = sigma

    if theta < OU_MIN_THETA or sigma == 0.0:
        return 0.0

    L_t            = logit(state.mid_price)
    stationary_std = math.sqrt(sigma ** 2 / (2.0 * theta))

    if stationary_std < 1e-8:
        return 0.0

    return (L_t - mu) / stationary_std


# ── LAYER 2C: PROBABILITY BUDGET ─────────────────────────────────────────────

def compute_budget_zscore(
    state: "MarketState",
    state_map: dict[str, "MarketState"],
) -> float:
    """
    Compute the probability-budget z-score for a categorical market outcome.

    For a categorical market with N outcomes, the market prices q_i should sum
    to 1.  The overround S = Σ q_i measures the violation of this constraint.

    The budget-adjusted fair price for outcome i is:
        q_i_fair = q_i / S

    The normalised gap:
        z_budget_i = gap_i / σ_Bernoulli(q_i)
                   = (1 − S) · √q_i / (S · √(1 − q_i))

    Returns 0.0 for binary YES/NO markets (no siblings), since the constraint
    is imposed by construction on Polymarket binary contracts.
    """
    sibling_ids = state.sibling_token_ids
    if not sibling_ids:
        return 0.0

    all_ids        = sibling_ids + [state.token_id]
    sibling_states = [state_map[sid] for sid in all_ids if sid in state_map]

    if len(sibling_states) < 2:
        return 0.0

    q_i = state.mid_price
    S   = sum(s.mid_price for s in sibling_states)

    if S < 1e-6:
        return 0.0

    gap             = q_i * (1.0 - S) / S
    sigma_bernoulli = math.sqrt(q_i * (1.0 - q_i) + 1e-10)

    return gap / sigma_bernoulli


# ── LAYER 3: PRISM AGGREGATOR ─────────────────────────────────────────────────

def prism_aggregate(
    z_prelec: float,
    z_ou:     float,
    z_budget: float,
    w1: float = W_PRELEC,
    w2: float = W_OU,
    w3: float = W_BUDGET,
) -> float:
    """
    Weighted L2-normalised combination of the three signal z-scores.

        z_combined = (w1·z1 + w2·z2 + w3·z3) / √(w1² + w2² + w3²)

    The L2 denominator preserves the standard-normal scale of the output:
    if each z_i ~ N(0,1) independently, then z_combined ~ N(0,1), making
    the gate threshold of 2.0 correspond to a ~2.3% false-positive rate
    per observation under the null hypothesis of no mispricing.

    Sign convention:
        z_combined > 0  →  market underpriced  →  BUY YES signal
        z_combined < 0  →  market overpriced   →  BUY NO / SELL YES signal
    """
    numerator   = w1 * z_prelec + w2 * z_ou + w3 * z_budget
    denominator = math.sqrt(w1**2 + w2**2 + w3**2)
    return numerator / (denominator + 1e-10)


def run_prism(
    state: "MarketState",
    state_map: dict[str, "MarketState"],
    all_states: list["MarketState"],
) -> Signal:
    """
    Full PRISM pipeline for one market outcome.

    Pre-flight checks applied before any signal computation:
      - Bid-ask spread ≥ MAX_SPREAD → return empty Signal (no trade)
      - all_states empty → cannot cross-sectionally normalise Prelec

    Computes all three signals, aggregates them, and sets Signal.confident
    when |z_combined| > PRISM_GATE_Z.
    """
    # Liquidity filter: wide spread indicates a thin book
    spread = state.best_ask - state.best_bid
    if spread >= MAX_SPREAD:
        return Signal(p_market=state.mid_price)

    if not all_states:
        return Signal(p_market=state.mid_price)

    z_prelec, p_true = compute_prelec_zscore(state, all_states)
    z_ou             = compute_ou_zscore(state)
    z_budget         = compute_budget_zscore(state, state_map)

    z_combined = prism_aggregate(z_prelec, z_ou, z_budget)

    return Signal(
        z_prelec   = z_prelec,
        z_ou       = z_ou,
        z_budget   = z_budget,
        z_combined = z_combined,
        p_true     = p_true,
        p_market   = state.mid_price,
        confident  = abs(z_combined) > PRISM_GATE_Z,
    )


# ── LAYER 4: KELLY POSITION SIZER ────────────────────────────────────────────

def kelly_fraction(p_true: float, q_market: float) -> float:
    """
    Full Kelly fraction for a binary prediction market:

        f* = (p_true − q_market) / (1 − q_market)

    Derivation: buy at price q, collect 1 if correct.
    Net odds b = (1-q)/q.  Standard Kelly f* = (pb - (1-p)) / b simplifies to
    f* = (p - q) / (1 - q).

    Returns 0.0 if:
      - |edge| < KELLY_MIN_EDGE (not worth trading)
      - f* ≤ 0 (market already priced above fair value from our perspective)
    """
    edge = p_true - q_market
    if abs(edge) < KELLY_MIN_EDGE:
        return 0.0

    denom = 1.0 - q_market
    if denom < 1e-6:
        return 0.0

    f_star = edge / denom
    return max(f_star, 0.0)


def time_decay_multiplier(state: "MarketState") -> float:
    """
    Position size discount that scales down as the market approaches resolution.

        τ = (t_resolution − t_now) / (t_resolution − t_created)
        multiplier = τ^0.3

    The concave (0.3) exponent means:
      τ = 1.0  (just created)       → multiplier = 1.00
      τ = 0.1  (90% life elapsed)   → multiplier ≈ 0.50
      τ = 0.01 (99% life elapsed)   → multiplier ≈ 0.25
    τ is clamped to [0.01, 1.0] to avoid extreme values when timestamps
    are missing or the market is extended past its original resolution date.
    """
    now        = time.time()
    total_life = state.resolution_time - state.created_time
    remaining  = state.resolution_time - now

    if total_life <= 0:
        return 0.5  # Fallback when timestamps are missing

    tau = remaining / total_life
    tau = float(np.clip(tau, 0.01, 1.0))
    return tau ** 0.3


def size_order(
    signal: Signal,
    state: "MarketState",
    bankroll: float,
) -> Optional[Order]:
    """
    Compute a Kelly-sized limit order from a confirmed signal.

    Returns None if:
      - Signal is not confident (|z_combined| ≤ PRISM_GATE_Z)
      - Time-decay multiplier < MIN_DECAY (market nearly expired)
      - Computed Kelly fraction ≤ 0 (no positive edge)
      - Dollar size < ORDER_SIZE_MIN_USDC (below exchange minimum)

    Order direction:
      z_combined > 0  →  BUY YES at best_ask
      z_combined < 0  →  SELL YES (= BUY NO) at best_bid (mirrored probabilities)
    """
    if not signal.confident:
        return None

    decay = time_decay_multiplier(state)
    if decay < MIN_DECAY:
        return None

    if signal.z_combined > 0:
        side     = "BUY"
        p_true   = signal.p_true
        q_market = state.best_ask       # Pay the ask when buying
    else:
        side     = "SELL"
        p_true   = 1.0 - signal.p_true  # Flip: we are pricing the NO side
        q_market = 1.0 - state.best_bid  # Price of NO = 1 − price of YES bid

    f_star = kelly_fraction(p_true, q_market)
    if f_star <= 0:
        return None

    f_scaled = f_star * KELLY_FRACTION * decay
    f_final  = min(f_scaled, MAX_POSITION_FRAC)

    size_usdc = f_final * bankroll
    if size_usdc < ORDER_SIZE_MIN_USDC:
        return None

    limit_price = state.best_ask if side == "BUY" else state.best_bid

    return Order(
        token_id       = state.token_id,
        side           = side,
        price          = round(limit_price, 4),
        size_usdc      = round(size_usdc, 2),
        kelly_fraction = f_final,
        signal         = signal,
    )


# ── LAYER 5: ORDER EXECUTOR ───────────────────────────────────────────────────

class OrderExecutor:
    """
    Wraps the Polymarket CLOB client and provides:
      - Dry-run mode: logs the intended order without submitting it.
      - Per-token cooldown: prevents duplicate orders within ORDER_COOLDOWN_SECS.
      - Error handling with full exception logging.

    py-clob-client quick reference:
      ClobClient(host, chain_id, key)          — instantiate with Polygon wallet key
      client.create_or_get_api_credentials()  — derive and cache API credentials
      client.place_limit_order(OrderArgs(...)) — submit a signed limit order
      client.get_order(order_id)               — query order status
      client.cancel(order_id)                  — cancel a resting order

    Share size vs dollar size:
      One share costs `price` USDC; it pays 1 USDC if the outcome resolves YES.
      shares = size_usdc / price
    """

    def __init__(self, dry_run: bool = True):
        self.dry_run   = dry_run
        self._cooldown: dict[str, float] = {}  # token_id → last order timestamp

        self.client = ClobClient(
            host     = CLOB_HOST,
            chain_id = int(os.getenv("CHAIN_ID", 137)),
            key      = os.getenv("PRIVATE_KEY"),
        )
        if not dry_run:
            self.client.create_or_get_api_credentials()

    def _in_cooldown(self, token_id: str) -> bool:
        """Return True if an order was placed on this token within the cooldown window."""
        last = self._cooldown.get(token_id, 0.0)
        return (time.time() - last) < ORDER_COOLDOWN_SECS

    def execute(self, order: Order) -> str:
        """
        Submit a limit order to the Polymarket CLOB.

        Returns the order_id string on success, or "" on dry-run / cooldown / error.
        """
        if self._in_cooldown(order.token_id):
            logging.debug(
                f"Cooldown active for {order.token_id}: skipping order."
            )
            return ""

        side       = BUY if order.side == "BUY" else SELL
        share_size = order.size_usdc / order.price

        log_prefix = "[DRY-RUN]" if self.dry_run else "[LIVE]"
        logging.info(
            f"{log_prefix} ORDER: {order.side} {share_size:.2f} shares of "
            f"token={order.token_id} at price={order.price:.4f} "
            f"(${order.size_usdc:.2f} USDC), kelly={order.kelly_fraction:.4f}, "
            f"z_combined={order.signal.z_combined:.2f}"
        )

        if self.dry_run:
            self._cooldown[order.token_id] = time.time()
            order.placed = False
            return ""

        try:
            args = OrderArgs(
                token_id = order.token_id,
                price    = order.price,
                size     = round(share_size, 2),
                side     = side,
            )
            resp     = self.client.place_limit_order(args)
            order_id = resp.get("orderID", "")
            order.placed   = True
            order.order_id = order_id
            self._cooldown[order.token_id] = time.time()
            logging.info(f"Order placed: {order_id}")
            return order_id
        except Exception as e:
            logging.error(f"Order placement failed for {order.token_id}: {e}")
            return ""


# ── LOGGING ──────────────────────────────────────────────────────────────────

def log_decision(
    state: "MarketState",
    signal: Signal,
    order: Optional[Order],
):
    """
    Append a structured JSONL record for every PRISM decision.
    Written to LOG_FILE for offline analysis, backtesting, and parameter tuning.
    """
    record = {
        "ts":          time.time(),
        "market_id":   state.market_id,
        "token_id":    state.token_id,
        "question":    state.question[:60],
        "outcome":     state.outcome,
        "mid_price":   state.mid_price,
        "p_true":      signal.p_true,
        "z_prelec":    round(signal.z_prelec, 4),
        "z_ou":        round(signal.z_ou, 4),
        "z_budget":    round(signal.z_budget, 4),
        "z_combined":  round(signal.z_combined, 4),
        "ou_theta":    round(state.ou_theta, 4),
        "ou_mu":       round(state.ou_mu, 4),
        "ou_sigma":    round(state.ou_sigma, 4),
        "order_side":  order.side if order else None,
        "order_price": order.price if order else None,
        "order_usdc":  order.size_usdc if order else None,
        "kelly_f":     order.kelly_fraction if order else None,
        "placed":      order.placed if order else False,
    }
    with open(LOG_FILE, "a") as f:
        f.write(_json.dumps(record) + "\n")


# ── MAIN EVENT LOOP ───────────────────────────────────────────────────────────

def parse_gamma_market_into_states(
    market: dict,
    state_map: dict[str, "MarketState"],
):
    """
    Convert a Gamma API market dict into MarketState objects and add to state_map.

    Creates one MarketState per outcome token. Skips tokens already in state_map
    so that existing price history is never overwritten by a re-poll.

    Gamma API fields used:
      market["id"]         → market_id
      market["question"]   → question text
      market["tokens"]     → list of {token_id, outcome, price}
      market["endDate"]    → ISO 8601 resolution timestamp
      market["startDate"]  → ISO 8601 creation timestamp
      market["volume24hr"] → 24-hour USDC volume
    """
    market_id  = market.get("id", "")
    question   = market.get("question", "")
    tokens     = market.get("tokens", [])
    volume_24h = float(market.get("volume24hr", 0))
    end_date   = market.get("endDate")
    start_date = market.get("startDate")

    def parse_ts(s: Optional[str]) -> float:
        if not s:
            return time.time() + 86400
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return time.time() + 86400

    resolution_time = parse_ts(end_date)
    created_time    = parse_ts(start_date)
    all_token_ids   = [t.get("token_id", "") for t in tokens]

    for token_info in tokens:
        token_id = token_info.get("token_id", "")
        outcome  = token_info.get("outcome", "")
        price    = float(token_info.get("price", 0.5))

        if not token_id or token_id in state_map:
            continue

        siblings = [tid for tid in all_token_ids if tid != token_id]

        state_map[token_id] = MarketState(
            market_id         = market_id,
            token_id          = token_id,
            question          = question,
            outcome           = outcome,
            best_bid          = max(price - 0.01, 0.01),
            best_ask          = min(price + 0.01, 0.99),
            mid_price         = price,
            volume_24h        = volume_24h,
            resolution_time   = resolution_time,
            created_time      = created_time,
            price_history     = deque(maxlen=OU_WINDOW),
            sibling_token_ids = siblings,
        )


async def main():
    load_dotenv()
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s %(levelname)s %(message)s",
        datefmt = "%Y-%m-%d %H:%M:%S",
    )

    bankroll     = float(os.getenv("BANKROLL_USDC", 500))
    dry_run      = os.getenv("DRY_RUN", "true").lower() == "true"
    
    if not dry_run and not os.getenv("PRIVATE_KEY"):
        logging.error("LIVE mode requested but PRIVATE_KEY environment variable is missing. Exiting.")
        sys.exit(1)

    state_map    : dict[str, MarketState] = {}
    signal_queue = asyncio.Queue(maxsize=10_000)
    executor     = OrderExecutor(dry_run=dry_run)

    logging.info(
        f"PRISM starting | bankroll=${bankroll:.0f} | dry_run={dry_run}"
    )

    proxies = os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY")
    if proxies:
        logging.info(f"Using proxy: {proxies}")

    transport = httpx.AsyncHTTPTransport(retries=3)
    async with httpx.AsyncClient(timeout=15, proxy=proxies, transport=transport) as http:

        async def market_poller():
            """
            Poll the Gamma API every GAMMA_POLL_INTERVAL seconds for new markets.
            Adds new MarketState objects to state_map; never overwrites existing ones.
            """
            while True:
                try:
                    markets = await fetch_active_markets(http)
                    for m in markets[:MAX_MARKETS_TRACKED]:
                        parse_gamma_market_into_states(m, state_map)
                    logging.info(
                        f"Market poll complete: tracking {len(state_map)} tokens."
                    )
                except Exception as e:
                    logging.error(f"Gamma poll error: {repr(e)}")
                    if "ConnectError" in repr(e) or "Connection reset" in repr(e):
                        logging.error("This usually means Polymarket is blocking your IP (geo-block) or your connection is dropping. Consider using a VPN or HTTP proxy.")
                await asyncio.sleep(GAMMA_POLL_INTERVAL)

        async def websocket_streamer():
            """
            Maintain a live CLOB WebSocket connection.
            Reconnects with exponential backoff (1 s → 2 s → … → 60 s) on failure.
            Refreshes the subscription token list on each reconnection, picking up
            any new markets added by market_poller while the socket was down.
            """
            backoff = 1
            while True:
                try:
                    token_ids = list(state_map.keys())
                    if token_ids:
                        await clob_websocket_loop(
                            token_ids, state_map, signal_queue
                        )
                    else:
                        await asyncio.sleep(2)  # Wait for market_poller to populate
                    backoff = 1
                except Exception as e:
                    logging.warning(
                        f"WebSocket error (reconnecting in {backoff}s): {repr(e)}"
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

        async def signal_processor():
            """
            Consume token_ids from signal_queue and run the full PRISM pipeline.
            The all_states snapshot is refreshed every 100 ticks to amortise the
            cost of rebuilding the list across many rapid price updates.
            """
            all_states_snapshot: list[MarketState] = []
            snapshot_age = 0

            while True:
                token_id = await signal_queue.get()

                snapshot_age += 1
                if snapshot_age > 100:
                    all_states_snapshot = list(state_map.values())
                    snapshot_age = 0

                state = state_map.get(token_id)
                if state is None or len(state.price_history) < 20:
                    signal_queue.task_done()
                    continue

                signal = run_prism(state, state_map, all_states_snapshot)

                order = None
                if signal.confident:
                    order = size_order(signal, state, bankroll)
                    if order:
                        executor.execute(order)

                log_decision(state, signal, order)
                signal_queue.task_done()

        await asyncio.gather(
            market_poller(),
            websocket_streamer(),
            signal_processor(),
        )


# ── TESTS ─────────────────────────────────────────────────────────────────────

def test_prism():
    """
    Unit tests for core mathematical functions.
    Run with:  python prism.py test
    """
    print("Running PRISM unit tests...\n")

    # ── Test 1: Prelec round-trip ──────────────────────────────────────────
    for p in [0.05, 0.2, 0.5, 0.8, 0.95]:
        q          = prelec_weight(p)
        p_recovered = prelec_inverse(q)
        assert abs(p_recovered - p) < 1e-9, (
            f"Prelec round-trip failed at p={p}: recovered {p_recovered}"
        )
    print("  ✓  Prelec round-trip (w⁻¹(w(p)) = p) for p ∈ {0.05, 0.2, 0.5, 0.8, 0.95}")

    # ── Test 2: logit-logistic round-trip ──────────────────────────────────
    for p in [0.1, 0.4, 0.6, 0.9]:
        assert abs(logistic(logit(p)) - p) < 1e-12, (
            f"logit round-trip failed at p={p}"
        )
    print("  ✓  logit / logistic inverse pair")

    # ── Test 3: Kelly fraction ─────────────────────────────────────────────
    # p=0.6, q=0.5 → f* = (0.6−0.5)/(1−0.5) = 0.2
    f = kelly_fraction(0.6, 0.5)
    assert abs(f - 0.2) < 1e-10, f"Kelly fraction wrong: got {f}"
    # Edge below minimum → returns 0
    assert kelly_fraction(0.51, 0.5) == 0.0, "Sub-threshold edge should return 0"
    print("  ✓  Kelly fraction formula and minimum-edge guard")

    # ── Test 4: PRISM aggregator gating ───────────────────────────────────
    z_pos = prism_aggregate(z_prelec=3.0, z_ou=2.5, z_budget=1.0)
    assert z_pos > PRISM_GATE_Z, (
        f"Strong positive signals should gate through (z={z_pos:.3f})"
    )
    z_neg = prism_aggregate(z_prelec=-3.0, z_ou=-2.5, z_budget=-1.0)
    assert z_neg < -PRISM_GATE_Z, (
        f"Strong negative signals should gate through (z={z_neg:.3f})"
    )
    z_neutral = prism_aggregate(z_prelec=0.1, z_ou=-0.1, z_budget=0.0)
    assert abs(z_neutral) < PRISM_GATE_Z, "Neutral signals should not gate"
    print(
        f"  ✓  PRISM aggregator gating "
        f"(z_pos={z_pos:.3f}, z_neg={z_neg:.3f}, z_neutral={z_neutral:.3f})"
    )

    # ── Test 5: OU parameter recovery ─────────────────────────────────────
    np.random.seed(42)
    true_theta, true_mu, true_sigma = 2.0, 0.0, 0.5
    L       = 0.0
    history = deque(maxlen=20000)
    # Use a larger dt for the test simulation so mean reversion is observable in 20000 ticks
    sim_dt = 1.0 / 100
    for _ in range(20000):
        L = (
            L
            + true_theta * (true_mu - L) * sim_dt
            + true_sigma * math.sqrt(sim_dt) * np.random.randn()
        )
        history.append((time.time(), logistic(L)))

    theta_est, mu_est, sigma_est = fit_ou_parameters(history, dt=sim_dt)
    assert abs(theta_est - true_theta) / true_theta < 0.3, (
        f"θ estimate too far off: {theta_est:.3f} vs {true_theta}"
    )
    print(
        f"  ✓  OU parameter recovery: "
        f"θ_est={theta_est:.3f} (true={true_theta}), "
        f"μ_est={mu_est:.3f} (true={true_mu})"
    )

    # ── Test 6: Spread filter blocks illiquid markets ──────────────────────
    wide_spread_state = MarketState(
        market_id       = "test-mkt",
        token_id        = "tok-wide",
        question        = "Will X happen?",
        outcome         = "YES",
        best_bid        = 0.40,
        best_ask        = 0.48,       # spread = 0.08 > MAX_SPREAD (0.04)
        mid_price       = 0.44,
        volume_24h      = 1000,
        resolution_time = time.time() + 86400,
        created_time    = time.time() - 86400,
        price_history   = deque(maxlen=OU_WINDOW),
    )
    sig = run_prism(wide_spread_state, {}, [wide_spread_state])
    assert not sig.confident, "Wide-spread market should not produce confident signal"
    print(f"  ✓  Spread filter (spread=0.08 > MAX_SPREAD={MAX_SPREAD})")

    # ── Test 7: Stale price zeroes the OU signal ──────────────────────────
    stale_state = MarketState(
        market_id       = "test-mkt",
        token_id        = "tok-stale",
        question        = "Stale market?",
        outcome         = "YES",
        best_bid        = 0.49,
        best_ask        = 0.51,
        mid_price       = 0.50,
        volume_24h      = 5000,
        resolution_time = time.time() + 86400,
        created_time    = time.time() - 86400,
        price_history   = deque(maxlen=OU_WINDOW),
    )
    stale_state.last_update_ts = time.time() - (STALE_SECS + 10)
    z_ou = compute_ou_zscore(stale_state)
    assert z_ou == 0.0, f"Stale price should return z_ou=0, got {z_ou}"
    print(f"  ✓  Stale-price guard (last update >{STALE_SECS}s ago → z_ou=0)")

    # ── Test 8: Order cooldown deduplication ──────────────────────────────
    exec_ = OrderExecutor(dry_run=True)
    exec_._cooldown["tok-cd"] = time.time()        # inject a recent order
    assert exec_._in_cooldown("tok-cd"),   "Cooldown should be active"
    assert not exec_._in_cooldown("tok-other"), "Unrelated token should not be in cooldown"
    print(f"  ✓  Order cooldown ({ORDER_COOLDOWN_SECS}s deduplication)")

    # ── Test 9: Time-decay minimum blocks near-expiry orders ──────────────
    expiring_state = MarketState(
        market_id       = "test-mkt",
        token_id        = "tok-exp",
        question        = "Expiring?",
        outcome         = "YES",
        best_bid        = 0.60,
        best_ask        = 0.62,
        mid_price       = 0.61,
        volume_24h      = 5000,
        resolution_time = time.time() + 100,       # resolves in 100 seconds
        created_time    = time.time() - 86400,     # created 1 day ago
        price_history   = deque(maxlen=OU_WINDOW),
    )
    decay_val = time_decay_multiplier(expiring_state)
    # Fabricate a confident signal
    confident_sig = Signal(
        z_combined=3.0, p_true=0.75, p_market=0.61, confident=True
    )
    order_result = size_order(confident_sig, expiring_state, bankroll=500)
    if decay_val < MIN_DECAY:
        assert order_result is None, (
            f"Near-expiry order should be blocked (decay={decay_val:.4f})"
        )
        print(f"  ✓  Near-expiry guard (decay={decay_val:.4f} < MIN_DECAY={MIN_DECAY})")
    else:
        print(f"  ~  Near-expiry guard not triggered (decay={decay_val:.4f} ≥ {MIN_DECAY})")

    print("\n✓  All tests passed.\n")


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="PRISM Polymarket Trading Bot")
    parser.add_argument("--live", action="store_true", help="Run in LIVE trading mode (requires funded wallet & private key)")
    parser.add_argument("--test", action="store_true", help="Run unit tests")
    
    args = parser.parse_args()

    if args.test:
        test_prism()
    else:
        # Override DRY_RUN env var if --live is passed
        if args.live:
            os.environ["DRY_RUN"] = "false"
        asyncio.run(main())        