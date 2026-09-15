"""
AutoFeed — feeds clean, organic, non-bundled pump.fun-migrated memecoins into
the Live Trading pipeline the same way a manual "Start Trading" click does.

Design contract (must never be violated):
  * AutoFeed performs NO trading, NO swaps, NO order placing.
  * AutoFeed does NOT touch LiveTrader internals, StrategyEngine, or keys.
  * AutoFeed is purely a *discovery* + *filter* + *feed* loop:
        gmgn-cli  →  organic / non-bundled / mcap≥15k filter  →  WS push
  * The frontend receives the push and triggers `startLiveTrader(mint)` —
    exactly mirroring the manual button click path through `/ws/live/{mint}`.

Data source — `npx gmgn-cli` (read-only command, GMGN_API_KEY only):
  We use `market trending` for the broad pump.fun migrated pool. Why trending:
    - `market trenches --type completed` returns recently-graduated tokens,
      but its `data.completed` array often contains *very* fresh graduates
      that are still high-risk (right at the migration boundary).
    - `market trending --platform pool_pump_amm` (PumpSwapDEX = Pump.fun's
      own AMM) returns already-migrated, **actively-traded** Pump.fun tokens
      with full risk metrics (rug_ratio, bundler_rate, is_wash_trading, …) —
      exactly the clean / organic signal OP spec asks for.

The CLI is invoked via `npx -y gmgn-cli@<version>` so it auto-installs on first
run. Output is single-line JSON with `--raw`. We shell out, capture stdout,
parse, and run a strict per-row organic filter using the full GMGN response.

GMGN rate limits (per `docs/cli-usage.md`):
    market trending:  weight=1  →  ~20 req/s burst capacity 20.
    market trenches:   weight=3 →  ~6 req/s burst capacity 6.
Default poll cadence (60s) is well inside trending's safety band.

Config is hot-swappable at runtime via REST.  All numeric gates are exposed
to the dashboard so the user can tune them in real-time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Callable, Awaitable, Iterable

import rate_limiter as rl

logger = logging.getLogger("autofeed")


def Path_exists(p: str) -> bool:
    """True if a file at relative-or-absolute path p exists."""
    try: return Path(p).exists()
    except Exception: return False


def Path_exists_env_gmgn() -> bool:
    """True if GMGN config is detectable in any of the documented locations."""
    candidates = [
        os.path.expanduser("~/.config/gmgn/.env"),
        ".env",
        os.path.join(os.path.dirname(__file__), ".env"),
        os.path.join(os.path.dirname(__file__), "..", ".env"),
    ]
    for c in candidates:
        if Path_exists(c):
            # Best effort: peek for a non-comment GMGN_API_KEY line
            try:
                with open(c) as fh:
                    for line in fh:
                        s = line.strip()
                        if s.startswith("#") or not s: continue
                        if s.startswith("GMGN_API_KEY") and "=" in s and s.split("=", 1)[1].strip():
                            return True
            except Exception:
                return True  # file present at least
    return False

# ── gmgn-cli invocation ─────────────────────────────────────────────────────

# Pin to a known-good version. `npx -y` auto-installs at this version.
GMGN_CLI_VERSION = "1.5.2"
GMGN_BIN = os.environ.get("GMGN_CLI_BIN", f"npx -y gmgn-cli@{GMGN_CLI_VERSION}")
NODE_BIN = os.environ.get("NODE_BIN", "node")
NPX_BIN  = os.environ.get("NPX_BIN",  "npx")
# On macOS / Linux the underlying `npx` is found on PATH so we just call it.
GMGN_COMMAND   = os.environ.get("GMGN_CLI_CMD", f"npx -y gmgn-cli@{GMGN_CLI_VERSION}")
GMGN_RAINDOW   = os.environ.get("GMGN_CLI_RAW",  "--raw")  # always pass --raw

# ── Defaults (tuned for "clean, organic, non-bundled, >15k mcap") ──────────

# ── Market-condition auto-tune (2026-09-05) ──────────────────────────────────
#
# The USD gates were static while the market floats:
#   * pump.fun graduation is a FIXED ~85.005 SOL raised on the bonding curve
#     (notes/pumpfun_internals_2026-09.md §2) — the graduation mcap in USD is
#     therefore LINEAR in the SOL price (≈ $69k at SOL=$168 ⇒
#     grad_mcap_usd ≈ 410.7 × sol_usd).  A fixed $20k floor silently targets a
#     different token class at every SOL level ($20k at SOL=$103 admits deep
#     post-grad bleeders; the same floor at SOL=$240 would block half the
#     fresh-graduate population).
#   * Motion gates ($ volume / swaps / liquidity) calibrated for a normal
#     PumpSwap tape starve the feed to zero in a cold venue and under-select
#     in a hot one.
#
# Tuned levers (the ONLY values auto-tune may touch):
#   min_mcap_usd      → max(user floor, grad_mcap_usd × mcap_grad_discount)
#   min_volume_usd    → user gate × motion_scale
#   min_swaps         → user gate × motion_scale
#   min_liquidity_usd → user gate × motion_scale
#
#   motion_scale = clamp(venue_ratio × momentum_adj, 0.5, 1.5)
#     venue_ratio  = PumpSwap latest-day DEX volume / median of the prior 7
#                    settled days (DeFiLlama /overview/dexs/solana breakdown)
#     momentum_adj = 1 + 0.3 × clamp(sol_24h_chg / 10%, −1, +1)
#                    — compensates the ≤24h lag of the daily volume series
#
# Everything else — risk ratios, renounced/wash/honeypot gates, holders,
# smart money, ranking, cooldowns — is NEVER modified: auto-tune adapts
# QUANTITY to market conditions while QUALITY stays at the user's settings.
# All tuned values are clamped to absolute safety floors so no regime can
# starve the feed into inactivity or admit motionless tokens.  Escape hatch:
# `auto_tune_enabled=False` restores user-only gates byte-exactly.
GRAD_MCAP_USD_PER_SOL        = 410.7    # $69k grad mcap at SOL=$168 (85-SOL curve)
AUTO_TUNE_MAX_CONDITION_AGE  = 6 * 3600.0   # older conditions → user-only gates
AUTO_TUNE_MOMENTUM_WEIGHT    = 0.3
AUTO_TUNE_RATIO_MIN          = 0.5
AUTO_TUNE_RATIO_MAX          = 1.5
AUTO_TUNE_VOL_FLOOR_USD      = 5_000.0
AUTO_TUNE_VOL_CAP_USD        = 200_000.0
AUTO_TUNE_SWAPS_FLOOR        = 120
AUTO_TUNE_SWAPS_CAP          = 5_000
AUTO_TUNE_LIQ_FLOOR_USD      = 2_500.0
AUTO_TUNE_LIQ_CAP_USD        = 100_000.0
COINGEKO_SOL_CHART_URL       = "https://api.coingecko.com/api/v3/coins/solana/market_chart"
LLAMA_DEXS_SOLANA_URL        = "https://api.llama.fi/overview/dexs/solana"
# DeFiLlama daily rows are UTC-midnight anchored and keep updating intraday;
# rows at least this old are treated as settled for the median baseline.
LLAMA_SETTLED_MIN_AGE_S      = 26 * 3600.0

DEFAULT_AUTO_TUNE_ENABLED            = True
DEFAULT_AUTO_TUNE_REFRESH_SECONDS    = 300.0
DEFAULT_AUTO_TUNE_MCAP_GRAD_DISCOUNT = 0.5

DEFAULT_POLL_SECONDS              = 60.0
# Spec: feed pump.fun migrated tokens above 15k mcap.
DEFAULT_MIN_MCAP_USD              = 20_000.0
DEFAULT_MAX_MCAP_USD              = 10_000_000.0   # OP: allow up to $10M
DEFAULT_MIN_LIQUIDITY_USD         = 5_000.0        # GMGN pass-tier > $50k; we ease to broaden
DEFAULT_MIN_HOLDERS               = 50             # organic metabolic mass
DEFAULT_MIN_SMART_DEGEN_COUNT     = 1            # >=1 smart money wallet enters (organic validation)
# OP: "lots of motion - trending coins" — require minimum USD volume over the
# trending interval to ensure a token is actively traded (not idle rope).
DEFAULT_MIN_VOLUME_USD            = 25_000.0
# Trending activity gate — require minimum swaps in the interval for real motion.
DEFAULT_MIN_SWAPS                 = 900
DEFAULT_ORDER_BY                  = "volume"     # rank by motion (highest traded volume first)
DEFAULT_MAX_TOP10_HOLDER_RATE     = 0.50         # GMGN skip-tier (0-1)
DEFAULT_MAX_RUG_RATIO            = 0.30         # GMGN skip-tier (0-1)
DEFAULT_MAX_BUNDLER_RATE         = 0.30         # bundle-bot trading ratio (0-1) [OP: "non-bundled"]
DEFAULT_MAX_INSIDER_RATE          = 0.50         # insider / sneak trading volume ratio (0-1)
DEFAULT_MAX_RAT_TRADER_RATE       = 0.50
DEFAULT_MAX_ENTRAPMENT_RATIO      = 0.50
# Organic-only booleans: ALL must be False / renounced.
DEFAULT_REQUIRE_RENOUNCED_MINT        = True
DEFAULT_REQUIRE_RENOUNCED_FREEZE      = True
DEFAULT_REJECT_WASH_TRADING           = True
DEFAULT_REJECT_HONEYPOT               = True
DEFAULT_MAX_BOT_DEGEN_RATE        = 0.80
DEFAULT_REQUIRE_HAS_SOCIAL        = False        # too strict on Pump.fun — many clean tokens lack social
DEFAULT_MAX_CREATED_AGE           = ""           # no age cap by default (PumpSwap tokens can be days old)
# OP: only pump.fun migrated coins. gmgn-cli's `--platform` filters by the
# token's *launchpad origin* (Pump.fun), so it captures tokens launched on
# pump.fun but does NOT distinguish bonding-curve vs graduated.  We add a
# local allow-list of EXCHANGE venues that indicate an actual migration to
# an open-market AMM (PumpSwap / Raydium / Meteora).  Empty string disables
# the exchange check (allow any exchange).
DEFAULT_REQUIRE_MIGRATION_EXCHANGE = True
DEFAULT_MIGRATION_EXCHANGES         = "pump_amm,meteora_dlmm,meteora_damm_v2,meteora whirlpool,raydium,raydium_cpmm,orca,orca_whirlpool"
# OP: only pump.fun migrated coins — `--platform` in gmgn-cli `market trending`
# filters by the *launchpad* origin (Pump.fun), not the current DEX venue.
# Per the gmgn-skills SKILL.md, `Pump.fun` is the canonical sol platform tag
# for tokens launched on pump.fun.  Trending returns tokens that have
# graduated/migrated (since we require motion + smart-money which only appear
# post-graduation on the open market).
DEFAULT_PLATFORMS                 = "Pump.fun"
DEFAULT_INTERVAL                  = "1h"
DEFAULT_MAX_CONCURRENT_FEED         = 100
DEFAULT_COOLDOWN_AFTER_FEED_MINUTES = 1.0
DEFAULT_EXCLUDE_MINTS              = ""   # comma-separated manual exclusion
# ── Liveness + dead-mint quality gates (2026-09-15) ─────────────────────
# GMGN trending stats are trailing-window aggregates (default interval 1h):
# a token can pass $25k/900-swaps on the hour while having zero trades NOW.
# Those dead-on-arrival mints then die to the 120s no-motion stop and get
# re-fed every poll (Sep 13-15: 51% of sessions no-motion deaths, top mints
# re-fed 4-6× each).  These gates prove life between polls instead:
DEFAULT_REQUIRE_MOTION_PRESENCE   = True   # reject rows with vol<=0 or swaps<=0 (unverifiable motion)
DEFAULT_MIN_SWAPS_GROWTH          = 25     # swaps increase required since last poll (proof of life)
DEFAULT_MIN_VOLUME_GROWTH_USD     = 1_000.0  # ...or volume increase in USD
DEFAULT_HOT_BYPASS_MULTIPLE       = 2.0    # first sighting forwards iff swaps AND vol ≥ mult × gates
DEFAULT_DEAD_MINT_COOLDOWN_HOURS  = 12.0   # no-motion/0-trade deaths stay out of the feed this long


# ── Candidate record ────────────────────────────────────────────────────────

@dataclass
class Candidate:
    mint:             str
    name:             str = ""
    symbol:           str = ""
    market_cap:       float = 0.0
    liquidity:        float = 0.0
    holders:          int = 0
    swaps:            int = 0
    volume:           float = 0.0
    price_change_1h:  float = 0.0
    price_change_5m:  float = 0.0
    price_change_1m:  float = 0.0
    top10_pct:        float = 0.0
    rug_ratio:        float = 0.0
    bundler_rate:     float = 0.0
    insider_rate:     float = 0.0
    rat_trader_rate:  float = 0.0
    entrapment_ratio: float = 0.0
    bot_degen_rate:   float = 0.0
    is_wash_trading:  bool = False
    is_honeypot:      bool = False
    renounced_mint:   bool = False
    renounced_freeze: bool = False
    smart_degen_count: int = 0
    renowned_count:    int = 0
    creator_token_status: str = ""
    launchpad_platform:   str = ""
    exchange:             str = ""
    creation_timestamp:   float = 0.0
    open_timestamp:       float = 0.0
    twitter:               str = ""
    telegram:              str = ""
    website:               str = ""
    reason:                str = "gmgn_organic"
    first_seen:            float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.first_seen) / 60.0


# ── Settings ────────────────────────────────────────────────────────────────

@dataclass
class AutofeedConfig:
    enabled:                bool = False
    poll_seconds:           float = DEFAULT_POLL_SECONDS
    interval:               str = DEFAULT_INTERVAL              # 1m/5m/1h/6h/24h
    platforms:              str = DEFAULT_PLATFORMS             # comma-separated sol platform list
    min_mcap_usd:           float = DEFAULT_MIN_MCAP_USD
    max_mcap_usd:           float = DEFAULT_MAX_MCAP_USD
    min_liquidity_usd:      float = DEFAULT_MIN_LIQUIDITY_USD
    min_holders:            int = DEFAULT_MIN_HOLDERS
    min_smart_degen_count:  int = DEFAULT_MIN_SMART_DEGEN_COUNT
    min_volume_usd:         float = DEFAULT_MIN_VOLUME_USD       # OP: motion gate (USD traded)
    min_swaps:              int = DEFAULT_MIN_SWAPS              # OP: motion gate (swap count)
    require_motion_presence: bool = DEFAULT_REQUIRE_MOTION_PRESENCE  # reject vol<=0/swaps<=0 rows
    min_swaps_growth:        int = DEFAULT_MIN_SWAPS_GROWTH      # liveness: swaps growth since last poll
    min_volume_growth_usd:   float = DEFAULT_MIN_VOLUME_GROWTH_USD  # liveness: ...or volume growth (USD)
    hot_bypass_multiple:     float = DEFAULT_HOT_BYPASS_MULTIPLE  # first sighting forwards iff ≥mult×gates
    dead_mint_cooldown_hours: float = DEFAULT_DEAD_MINT_COOLDOWN_HOURS  # no-motion deaths held out (h)
    order_by:               str = DEFAULT_ORDER_BY               # server-side ranking key
    require_migration_exchange: bool = DEFAULT_REQUIRE_MIGRATION_EXCHANGE
    migration_exchanges:    str = DEFAULT_MIGRATION_EXCHANGES    # comma-separated exchange allow-list
    max_top10_holder_rate:  float = DEFAULT_MAX_TOP10_HOLDER_RATE
    max_rug_ratio:          float = DEFAULT_MAX_RUG_RATIO
    max_bundler_rate:       float = DEFAULT_MAX_BUNDLER_RATE
    max_insider_rate:       float = DEFAULT_MAX_INSIDER_RATE
    max_rat_trader_rate:    float = DEFAULT_MAX_RAT_TRADER_RATE
    max_entrapment_ratio:   float = DEFAULT_MAX_ENTRAPMENT_RATIO
    max_bot_degen_rate:     float = DEFAULT_MAX_BOT_DEGEN_RATE
    require_renounced_mint:  bool = DEFAULT_REQUIRE_RENOUNCED_MINT
    require_renounced_freeze: bool = DEFAULT_REQUIRE_RENOUNCED_FREEZE
    reject_wash_trading:     bool = DEFAULT_REJECT_WASH_TRADING
    reject_honeypot:         bool = DEFAULT_REJECT_HONEYPOT
    require_has_social:      bool = DEFAULT_REQUIRE_HAS_SOCIAL
    max_created_age:         str = DEFAULT_MAX_CREATED_AGE
    max_concurrent_feed:     int = DEFAULT_MAX_CONCURRENT_FEED
    cooldown_after_feed_minutes: float = DEFAULT_COOLDOWN_AFTER_FEED_MINUTES
    exclude_mints:           str = DEFAULT_EXCLUDE_MINTS
    # Market-condition auto-tune (see §"auto-tune" constants above)
    auto_tune_enabled:       bool = DEFAULT_AUTO_TUNE_ENABLED
    auto_tune_refresh_seconds: float = DEFAULT_AUTO_TUNE_REFRESH_SECONDS
    auto_tune_mcap_grad_discount: float = DEFAULT_AUTO_TUNE_MCAP_GRAD_DISCOUNT

    def to_dict(self) -> dict:
        return asdict(self)


# ── AutoFeed Engine ─────────────────────────────────────────────────────────

class AutoFeed:
    """
    Polling-only discovery loop.

    Lifecycle:
        start(forward_fn, active_count_fn) -> spawns the polling task
        stop()                              -> cancels the polling task
        set_config(partial_dict)            -> hot-updates settings
    """

    def __init__(
        self,
        config: Optional[AutofeedConfig] = None,
        forward_fn: Optional[Callable[[Candidate], Awaitable[None]]] = None,
        active_count_fn: Optional[Callable[[], int]] = None,
    ):
        self.config = config or AutofeedConfig()
        self._forward_fn = forward_fn
        self._active_count_fn = active_count_fn
        self._task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()

        # State
        self._seen: dict[str, Candidate] = {}
        self._last_fed_at: float = 0.0
        # Liveness memory: mint -> (swaps, volume_usd, ts) at last poll sighting
        self._last_motion: dict[str, tuple[int, float, float]] = {}
        # Dead-mint memory: mint -> ts when a live session died no-motion/0-trade
        self._dead_mints: dict[str, float] = {}
        self.last_error: str = ""
        self.last_poll_at: float = 0.0
        self.total_seen: int = 0
        self.total_fed: int = 0
        # Auto-tune state
        self._market_conditions: Optional[dict] = None
        self._last_tuned_gates: Optional[dict] = None
        self._last_tune_at: float = 0.0
        self._last_tune_refresh_at: float = 0.0
        self._tune_history: list[dict] = []

    # -- lifecycle ────────────────────────────────────────────────────────────
    def start(self, forward_fn=None, active_count_fn=None):
        if forward_fn is not None:
            self._forward_fn = forward_fn
        if active_count_fn is not None:
            self._active_count_fn = active_count_fn

        if self._task and not self._task.done():
            logger.warning("[AutoFeed] Already running — ignoring duplicate start()")
            return False

        self._stop_evt.clear()
        self._task = asyncio.ensure_future(self._loop())
        logger.info(
            f"[AutoFeed] Started  interval={self.config.interval} "
            f"poll={self.config.poll_seconds}s "
            f"mcap≥${self.config.min_mcap_usd:.0f} liq≥${self.config.min_liquidity_usd:.0f} "
            f"rug≤{self.config.max_rug_ratio} bundled≤{self.config.max_bundler_rate} "
            f"insider≤{self.config.max_insider_rate} smart≤{self.config.min_smart_degen_count}"
        )
        return True

    async def stop(self):
        self._stop_evt.set()
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass
        self._task = None
        logger.info("[AutoFeed] Stopped")

    def is_running(self) -> bool:
        return bool(self._task and not self._task.done() and self.config.enabled)

    # -- hot config update ────────────────────────────────────────────────────
    def set_config(self, partial: dict):
        changed = []
        for k, v in partial.items():
            if hasattr(self.config, k) and v is not None:
                old = getattr(self.config, k)
                if old != v:
                    setattr(self.config, k, v)
                    changed.append(k)
        return changed

    # ── Market-condition auto-tune ──────────────────────────────────────────

    async def _fetch_market_conditions(self) -> Optional[dict]:
        """Pull SOL price/momentum (CoinGecko) + venue volume (DeFiLlama).

        Returns a dict with sol_usd / sol_chg_24h_pct / venue_vol_usd /
        venue_ratio / fetched_at, or None when nothing usable came back.
        Never raises — a failed refresh just keeps the last good snapshot.
        """
        import aiohttp

        now = time.time()
        out: dict = {"fetched_at": now}

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=8)
            ) as s:
                # -- SOL spot + 24h momentum (one call, 8 days of hourly points)
                async with s.get(
                    COINGEKO_SOL_CHART_URL,
                    params={"vs_currency": "usd", "days": "8"},
                ) as r:
                    if r.status == 200:
                        prices = (await r.json(content_type=None)).get("prices") or []
                        if prices:
                            sol_now = float(prices[-1][1])
                            p24 = [x for x in prices if x[0] / 1000 <= now - 86_400]
                            if sol_now > 0 and p24:
                                out["sol_usd"] = sol_now
                                out["sol_chg_24h_pct"] = (sol_now / float(p24[-1][1]) - 1.0) * 100.0
                # -- PumpSwap daily volume series (per-DEX breakdown)
                async with s.get(LLAMA_DEXS_SOLANA_URL) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        rows = [
                            (int(pt[0]), float((pt[1] or {}).get("PumpSwap", 0.0)))
                            for pt in (data.get("totalDataChartBreakdown") or [])
                        ]
                        parsed = self._parse_llama_breakdown(rows, now)
                        if parsed:
                            out.update(parsed)
        except Exception as e:
            logger.debug(f"[AutoFeed] market-conditions fetch error: {e}")

        if "sol_usd" not in out and "venue_ratio" not in out:
            return None
        return out

    @staticmethod
    def _parse_llama_breakdown(rows: list, now: float) -> Optional[dict]:
        """(ts, pumpswap_vol) daily rows → venue_ratio inputs.

        DeFiLlama quirks this compensates (measured 2026-09-05, byte-identical
        duplicate frozen across 3+ polls):
          * The API appends an EXACT DUPLICATE of the previous day's value
            as the newest (today's) row — a copy-forward placeholder carrying
            no fresh data. Projecting it would inflate a real settled day
            2–4x, so an exact-equal latest is used as-is (it IS the newest
            information: yesterday's settled day).
          * A genuinely fresh partial day (value ≠ predecessor) IS filling
            intraday — it is projected to a full-day estimate by
            24h / elapsed-since-UTC-midnight, clamped to ≥6h elapsed so the
            first minutes after midnight don't overproject.
          * Baseline = settled rows (≥26h old); a value echoed on a row that
            is itself settled counts the day exactly once.
        """
        pts = [(ts, v) for ts, v in rows if v > 0]
        if not pts:
            return None
        latest_ts, latest = pts[-1]
        prev_val = pts[-2][1] if len(pts) >= 2 else None
        is_echo = prev_val is not None and prev_val == latest
        if latest_ts > now - 24 * 3600.0 and not is_echo:
            day_span = 24 * 3600.0
            elapsed = min(max(now - latest_ts, 0.25 * day_span), day_span)
            latest = latest * day_span / elapsed
        settled: list[float] = []
        for ts, v in pts:
            if now - ts < LLAMA_SETTLED_MIN_AGE_S:
                continue
            if settled and settled[-1] == v:
                continue    # echoed day already counted
            settled.append(v)
        if not settled or latest <= 0:
            return None
        settled_sorted = sorted(settled)
        n = len(settled_sorted)
        mid = n // 2
        median = (
            settled_sorted[mid]
            if n % 2
            else (settled_sorted[mid - 1] + settled_sorted[mid]) / 2.0
        )
        if median <= 0:
            return None
        return {"venue_vol_usd": latest, "venue_ratio": latest / median}

    @staticmethod
    def _compute_tuned_gates(
        conditions: Optional[dict],
        cf: "AutofeedConfig",
        now: Optional[float] = None,
    ) -> dict:
        """Map market conditions → effective discovery gates (pure function).

        Quality gates (risk ratios / booleans / holders / smart money) are
        never touched — only the four market-floating USD/swap levers move,
        and every lever is clamped to an absolute floor/cap so no regime can
        starve the feed or admit motionless tokens.  Stale or missing
        conditions degrade to the user's static gates exactly.
        """
        now = now if now is not None else time.time()

        def clampf(x: float, lo: float, hi: float) -> float:
            return max(lo, min(hi, x))

        # 1) SOL-anchored graduation mcap floor.
        #    Anchors min_mcap to the USD graduation level that floats with SOL
        #    (fixed 85-SOL curve event ⇒ grad_mcap ≈ 410.7 × sol_usd), scaled
        #    by the user's discount so the feed stays slightly below the
        #    graduate frontier by default.  The user's own floor still wins
        #    when it is the stricter of the two.
        min_mcap = cf.min_mcap_usd
        if conditions and "sol_usd" in conditions:
            grad_mcap = GRAD_MCAP_USD_PER_SOL * float(conditions["sol_usd"])
            min_mcap = max(cf.min_mcap_usd, grad_mcap * cf.auto_tune_mcap_grad_discount)

        # 2) Motion gates scaled by venue volume ratio ± SOL momentum.
        scale = 1.0
        if conditions and "venue_ratio" in conditions:
            ratio = float(conditions["venue_ratio"])
            momentum_adj = 1.0
            if "sol_chg_24h_pct" in conditions:
                momentum_adj = 1.0 + AUTO_TUNE_MOMENTUM_WEIGHT * clampf(
                    float(conditions["sol_chg_24h_pct"]) / 10.0, -1.0, 1.0
                )
            scale = clampf(ratio * momentum_adj, AUTO_TUNE_RATIO_MIN, AUTO_TUNE_RATIO_MAX)

        return {
            "min_mcap_usd":      float(min_mcap),
            "min_volume_usd":    clampf(cf.min_volume_usd * scale, AUTO_TUNE_VOL_FLOOR_USD, AUTO_TUNE_VOL_CAP_USD),
            "min_swaps":         int(round(clampf(cf.min_swaps * scale, AUTO_TUNE_SWAPS_FLOOR, AUTO_TUNE_SWAPS_CAP))),
            "min_liquidity_usd": clampf(cf.min_liquidity_usd * scale, AUTO_TUNE_LIQ_FLOOR_USD, AUTO_TUNE_LIQ_CAP_USD),
        }

    def effective_gates(self) -> dict:
        """The gates actually enforced right now (auto-tuned when live).

        Stale conditions (older than AUTO_TUNE_MAX_CONDITION_AGE) fall back to
        the user's static gates — a dead price feed must never freeze tuning
        at an old SOL level or starve the feed.
        """
        cf = self.config
        user_gates = {
            "min_mcap_usd": float(cf.min_mcap_usd),
            "min_volume_usd": float(cf.min_volume_usd),
            "min_swaps": int(cf.min_swaps),
            "min_liquidity_usd": float(cf.min_liquidity_usd),
        }
        if not cf.auto_tune_enabled or not self._market_conditions:
            return user_gates
        if time.time() - float(self._market_conditions.get("fetched_at", 0.0)) > AUTO_TUNE_MAX_CONDITION_AGE:
            return user_gates
        return self._compute_tuned_gates(self._market_conditions, cf)

    async def refresh_market_conditions(self) -> bool:
        """Fetch fresh conditions + recompute effective gates. Called by the
        poll loop every `auto_tune_refresh_seconds` and by /api/autofeed/tune_now.
        Returns True when new conditions landed."""
        if not self.config.auto_tune_enabled:
            return False
        try:
            cond = await self._fetch_market_conditions()
        except Exception as e:
            logger.warning(f"[AutoFeed] auto-tune conditions fetch failed: {e}")
            return False
        if not cond:
            return False
        self._market_conditions = cond
        gates = self._compute_tuned_gates(cond, self.config)
        self._last_tuned_gates = gates
        self._last_tune_at = time.time()
        # Tune history ring (cap 50 entries) — lets the dashboard and any
        # later research audit how the gates moved with the market.
        self._tune_history.append({"ts": self._last_tune_at, "conditions": cond, "gates": gates})
        if len(self._tune_history) > 50:
            self._tune_history = self._tune_history[-50:]
        logger.info(
            f"[AutoFeed] Auto-tune: SOL=${cond.get('sol_usd', 0):.1f} "
            f"24h={cond.get('sol_chg_24h_pct', 0):+.1f}% "
            f"venue_ratio={cond.get('venue_ratio', 0):.2f} → "
            f"mcap≥${gates['min_mcap_usd']:.0f} vol≥${gates['min_volume_usd']:.0f} "
            f"swaps≥{gates['min_swaps']} liq≥${gates['min_liquidity_usd']:.0f}"
        )
        return True

    async def tune_now(self) -> dict:
        """Manual refresh hook for POST /api/autofeed/tune_now — forces a
        conditions fetch + recompute regardless of the refresh cadence."""
        ok = await self.refresh_market_conditions()
        return {
            "refreshed": ok,
            "auto_tune_enabled": self.config.auto_tune_enabled,
            "conditions": self._market_conditions,
            "effective_gates": self.effective_gates(),
        }


    # ── gmgn-cli invocation ──────────────────────────────────────────────────

    def _build_cli_args(self) -> list[str]:
        """Build the gmgn-cli `market trending` argv. Run server-side filters.

        Only flags that the gmgn-cli actually accepts are sent. Risk metrics
        not exposed as server-side flags (rug_ratio, rat_trader_amount_rate,
        bot_degen_rate) are filtered locally in `_to_candidate` instead.
        """
        cf = self.config
        eff = self.effective_gates()   # auto-tuned when enabled & conditions live
        args = ["market", "trending", "--chain", "sol", "--raw"]

        args += ["--interval", cf.interval or DEFAULT_INTERVAL]
        # OP: "trending coins with lots of motion" — rank by USD trading volume
        # ensures the top results are the most actively traded right now.
        args += ["--order-by", cf.order_by or "volume", "--direction", "desc"]
        args += ["--limit", "100"]

        # Server-side numeric range filters (supported by gmgn-cli).
        # The four market-floating levers use the AUTO-TUNED effective gates;
        # every other server-side flag (holders / smart money / risk ratios)
        # passes the user's static value straight through.
        args += ["--min-marketcap", str(int(eff["min_mcap_usd"]))]
        args += ["--max-marketcap", str(int(cf.max_mcap_usd))]
        args += ["--min-liquidity", str(int(eff["min_liquidity_usd"]))]
        args += ["--min-holder-count", str(int(cf.min_holders))]
        args += ["--min-smart-degen-count", str(int(cf.min_smart_degen_count))]
        # Motion gates (OP: "lots of motion - trending coins"):
        args += ["--min-volume", str(int(eff["min_volume_usd"]))]
        args += ["--min-swaps", str(int(eff["min_swaps"]))]
        args += ["--max-top10-holder-rate", str(cf.max_top10_holder_rate)]
        args += ["--max-insider-rate", str(cf.max_insider_rate)]
        args += ["--max-bundler-rate", str(cf.max_bundler_rate)]
        args += ["--max-entrapment-ratio", str(cf.max_entrapment_ratio)]

        # Server-side boolean filter tags (sol defaults already include
        # `renounced frozen`; we add `not_wash_trading has_social` here).
        args += ["--filter", "renounced", "--filter", "frozen"]
        if cf.reject_wash_trading:
            args += ["--filter", "not_wash_trading"]
        if cf.require_has_social:
            args += ["--filter", "has_social"]

        # Age window — `6h`/`7d` style. Only set if user configured a value.
        if cf.max_created_age:
            args += ["--max-created", cf.max_created_age]

        # Platform filter — comma-separated list. We split to repeatable --platform.
        platforms_raw = cf.platforms or DEFAULT_PLATFORMS
        for p in [s.strip() for s in platforms_raw.split(",") if s.strip()]:
            args += ["--platform", p]

        return args

    async def _run_cli(self, args: list[str]) -> Optional[dict]:
        """Run `npx -y gmgn-cli@<v> <args>`, capture stdout JSON."""
        # Build the actual shell command. We use `npx -y gmgn-cli@<v>` so it
        # auto-installs if missing. The shell parses the full command string.
        cmd = f"{GMGN_COMMAND} " + " ".join(args)
        logger.info(f"[AutoFeed] $ {cmd}")
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=45)
            stdout = stdout_b.decode("utf-8", errors="replace").strip()
            stderr = stderr_b.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                self.last_error = f"CLI exit {proc.returncode}: {stderr[:300]}"
                logger.error(f"[AutoFeed] CLI exit {proc.returncode} — stderr[:400]={stderr[:400]}")
                # Detect GMGN rate-limit bans in CLI stderr so we stop
                # re-triggering bans while holder_flow is also being limited.
                if "429" in stderr or "RATE_LIMIT" in stderr or "rate limit" in stderr.lower():
                    reset = rl.parse_gmgn_reset_time(stderr)
                    if reset is None:
                        rem = rl.parse_gmgn_remaining_seconds(stderr)
                        if rem is not None:
                            reset = time.time() + rem
                    rl.note_gmgn_429(reset, source="autofeed_cli")
                return None
            if not stdout:
                self.last_error = "Empty CLI stdout"
                logger.error(f"[AutoFeed] Empty CLI stdout — stderr[:400]={stderr[:400]}")
                return None
            try:
                return json.loads(stdout)
            except json.JSONDecodeError as e:
                self.last_error = f"JSON parse error: {e}  in={stdout[:300]}"
                logger.error(f"[AutoFeed] JSON parse error: {e}  stdout[:400]={stdout[:400]}")
                return None
        except asyncio.TimeoutError:
            self.last_error = "CLI timed out (45s)"
            logger.error(f"[AutoFeed] CLI timed out (45s) — cmd={cmd}")
            return None
        except Exception as e:
            self.last_error = f"CLI error: {type(e).__name__}: {e}"
            logger.error(f"[AutoFeed] CLI error: {e}", exc_info=True)
            return None

    async def _fetch_candidates(self) -> list[dict]:
        """Fetch raw token list from gmgn.ai. Returns raw rank rows on any failure."""
        # Skip the CLI invocation entirely while GMGN is rate-limited —
        # calling `npx gmgn-cli` during a ban just extends it.
        if rl.gmgn_banned():
            rem = rl.gmgn_ban_remaining()
            logger.info(f"[AutoFeed] Skipping poll — GMGN rate-limited ({rem:.0f}s remaining)")
            self.last_error = f"GMGN rate-limited ({rem:.0f}s remaining)"
            return []
        data = await self._run_cli(self._build_cli_args())
        if not data:
            return []
        # gmgn-cli `--raw` convention: { code:0, data: { rank: [...] } }
        if not isinstance(data, dict):
            return []
        if data.get("code") not in (0, None, 200):
            self.last_error = f"gmgn code={data.get('code')} msg={data.get('error') or data.get('message')}"
            logger.error(f"[AutoFeed] gmgn error: {self.last_error}")
            return []
        payload = data.get("data") or {}
        rank = payload.get("rank") if isinstance(payload, dict) else None
        if not rank and isinstance(payload, list):
            rank = payload
        return rank or []

    # ── Filter (organic / non-bundled) ──────────────────────────────────────

    def _to_candidate(self, raw: dict) -> Optional[Candidate]:
        """Convert raw gmgn row → Candidate. Returns None if hard-fails any non-server gate."""
        mint = (raw.get("address") or raw.get("mint") or raw.get("token_address") or "").strip()
        if not mint or len(mint) < 32 or len(mint) > 44:
            return None
        # NOTE: do NOT lower-case.  Solana addresses are base58 and case-sensitive —
        # lower-casing corrupts the encoding (this was causing "Invalid Solana address"
        # errors when the frontend tried to startlive-trading the auto-fed mint).
        # Compare excluded-set case-insensitively so users can type mints either way.
        if mint.lower() in self._excluded_set():
            return None
        # Dead-mint memory: no-motion/0-trade deaths stay out for the cooldown.
        if self._is_dead(mint, time.time()):
            return None

        # Numeric helpers — accept None / 0 / float<string>; coerce safely
        def f(v) -> float:
            try: return float(v)
            except (TypeError, ValueError): return 0.0
        def i(v) -> int:
            try: return int(v)
            except (TypeError, ValueError): return 0
        def b(v, default=False) -> bool:
            if v is None: return default
            if isinstance(v, bool): return v
            if isinstance(v, str): return v.lower() in ("yes", "true", "1")
            try: return bool(int(v))
            except (TypeError, ValueError): return default

        mcap   = f(raw.get("market_cap") or raw.get("usd_market_cap"))
        liq_usd = f(raw.get("liquidity"))
        holders = i(raw.get("holder_count"))
        smart   = i(raw.get("smart_degen_count"))
        renowned = i(raw.get("renowned_count"))
        swaps   = i(raw.get("swaps"))
        vol     = f(raw.get("volume"))
        exchange = (raw.get("exchange") or "").strip().lower()
        launchpad_platform = (raw.get("launchpad_platform") or raw.get("platform") or "").strip()
        top10   = f(raw.get("top_10_holder_rate"))
        rug     = f(raw.get("rug_ratio"))
        bundler = f(raw.get("bundler_rate"))
        insider = f(raw.get("insider_rate"))
        rat     = f(raw.get("rat_trader_amount_rate"))
        entrap  = f(raw.get("entrapment_ratio"))
        bot_degen = f(raw.get("bot_degen_rate"))
        is_wash = b(raw.get("is_wash_trading"))
        is_honey = b(raw.get("is_honeypot"))
        ren_mint = b(raw.get("renounced_mint"), default=True)
        ren_freeze = b(raw.get("renounced_freeze_account"), default=True)

        # Hard gates that may bypass server (defense in depth)
        cf = self.config
        eff = self.effective_gates()   # auto-tuned when enabled & conditions live
        if mcap < eff["min_mcap_usd"] or mcap > cf.max_mcap_usd:
            return None
        if liq_usd > 0 and liq_usd < eff["min_liquidity_usd"]:
            return None
        if holders > 0 and holders < cf.min_holders:
            return None
        if smart > -1 and smart < cf.min_smart_degen_count:
            return None
        if 0 < top10 > cf.max_top10_holder_rate: return None
        if 0 < rug    > cf.max_rug_ratio:        return None
        if 0 < bundler > cf.max_bundler_rate:    return None
        if 0 < insider > cf.max_insider_rate:    return None
        if 0 < rat     > cf.max_rat_trader_rate: return None
        if 0 < entrap  > cf.max_entrapment_ratio: return None
        if 0 < bot_degen and bot_degen > cf.max_bot_degen_rate: return None

        # OP: "only pump.fun migrated coins" — pump.fun-origin tokens that have
        # actually migrated to an open-market AMM. We verify by checking the
        # current exchange venue is in the migration allow-list (i.e. NOT still
        # on pump.fun's bonding curve, which uses exchange="pump").
        if cf.require_migration_exchange:
            allowed = {s.strip().lower() for s in cf.migration_exchanges.split(",") if s.strip()}
            if allowed and exchange not in allowed:
                return None

        # OP: "coins that got fed should have lots of motion - trending coins" —
        # Enforce motion locally too (defense in depth; auto-tuned effective gates).
        # Missing motion data (vol/swaps ≤ 0) is unverifiable motion — reject it
        # outright instead of letting zero-stat rows sail through the `> 0` guards.
        if cf.require_motion_presence and (vol <= 0 or swaps <= 0):
            return None
        if vol > 0 and vol < eff["min_volume_usd"]: return None
        if swaps > 0 and swaps < eff["min_swaps"]:  return None

        # Boolean gates — wash trading & honeypot are non-negotiable rejections.
        if cf.reject_wash_trading and is_wash: return None
        if cf.reject_honeypot and is_honey:   return None
        if cf.require_renounced_mint and not ren_mint:   return None
        if cf.require_renounced_freeze and not ren_freeze: return None

        # Has social — organic validation
        has_social = bool(
            raw.get("twitter_username") or raw.get("twitter") or
            raw.get("telegram") or raw.get("website") or
            raw.get("has_at_least_one_social")
        )
        if cf.require_has_social and not has_social:
            return None

        return Candidate(
            mint=mint,
            name = (raw.get("name") or "").strip(),
            symbol = (raw.get("symbol") or raw.get("ticker") or "").strip(),
            market_cap=mcap, liquidity=liq_usd, holders=holders,
            swaps=swaps, volume=vol,
            price_change_1h=f(raw.get("price_change_percent1h") or raw.get("price_change_percent")),
            price_change_5m=f(raw.get("price_change_percent5m")),
            price_change_1m=f(raw.get("price_change_percent1m")),
            top10_pct=top10, rug_ratio=rug, bundler_rate=bundler,
            insider_rate=insider, rat_trader_rate=rat, entrapment_ratio=entrap,
            bot_degen_rate=bot_degen, is_wash_trading=is_wash, is_honeypot=is_honey,
            renounced_mint=ren_mint, renounced_freeze=ren_freeze,
            smart_degen_count=smart, renowned_count=renowned,
            creator_token_status=raw.get("creator_token_status") or "",
            launchpad_platform=raw.get("launchpad_platform") or "",
            exchange=raw.get("exchange") or "",
            creation_timestamp=f(raw.get("creation_timestamp")),
            open_timestamp=f(raw.get("open_timestamp")),
            twitter=raw.get("twitter_username") or raw.get("twitter") or "",
            telegram=raw.get("telegram") or "",
            website=raw.get("website") or "",
        )

    # ── Liveness + dead-mint gates ────────────────────────────────────────

    def note_dead_mint(self, mint: str, now: Optional[float] = None) -> None:
        """Feedback from live sessions: a no-motion, zero-trade death means
        the candidate was dead on arrival — hold it out of the feed for
        `dead_mint_cooldown_hours` instead of re-feeding it every poll."""
        if not mint:
            return
        self._dead_mints[mint] = now if now is not None else time.time()

    def _is_dead(self, mint: str, now: float) -> bool:
        ts = self._dead_mints.get(mint)
        if ts is None:
            return False
        if now - ts > self.config.dead_mint_cooldown_hours * 3600.0:
            del self._dead_mints[mint]
            return False
        return True

    def _passes_liveness(self, mint: str, swaps: int, vol: float,
                         eff_min_swaps: int, eff_min_vol: float,
                         now: float) -> bool:
        """Proof-of-life between polls.  Trailing-window stats can look hot
        while nothing trades NOW, so forward only when the numbers GREW
        since the last sighting.  First sightings park for one poll —
        unless roaring (swaps AND vol ≥ bypass× the effective gates), in
        which case they forward immediately.  Sticky-hot rows (unchanged
        but still ≥ bypass×) also pass — the token was roaring ≤1 poll ago.
        Always records this sighting as the next baseline."""
        prev = self._last_motion.get(mint)
        self._last_motion[mint] = (swaps, vol, now)
        if len(self._last_motion) > 500:
            cutoff = now - 2 * 3600.0
            self._last_motion = {k: v for k, v in self._last_motion.items() if v[2] >= cutoff}
        mult = self.config.hot_bypass_multiple
        hot = mult > 0 and swaps >= mult * eff_min_swaps and vol >= mult * eff_min_vol
        if prev is None:
            return hot
        dswaps = swaps - prev[0]
        dvol = vol - prev[1]
        if dswaps >= self.config.min_swaps_growth or dvol >= self.config.min_volume_growth_usd:
            return True
        return bool(hot and dswaps >= 0 and dvol >= 0)

    def _excluded_set(self) -> set[str]:
        raw = (self.config.exclude_mints or "").strip()
        return {x.strip().lower() for x in raw.split(",") if x.strip()}

    # ── Polling loop ────────────────────────────────────────────────────────

    async def _loop(self):
        logger.info("[AutoFeed] Poll loop entered")
        # Prime conditions once at loop start so the very first poll already
        # runs against tuned gates (no partial-config poll before data lands).
        try:
            await self.refresh_market_conditions()
        except Exception as e:
            logger.warning(f"[AutoFeed] initial auto-tune refresh failed: {e}")
        while not self._stop_evt.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"[AutoFeed] tick error: {e}", exc_info=True)
                self.last_error = f"tick error: {e}"
            # Periodic market-condition refresh (auto-tune heartbeat)
            if self.config.auto_tune_enabled and (
                time.time() - self._last_tune_refresh_at >= self.config.auto_tune_refresh_seconds
            ):
                self._last_tune_refresh_at = time.time()
                try:
                    await self.refresh_market_conditions()
                except Exception as e:
                    logger.warning(f"[AutoFeed] periodic auto-tune refresh failed: {e}")
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self.config.poll_seconds)
            except asyncio.TimeoutError:
                pass
        logger.info("[AutoFeed] Poll loop exited")

    async def _tick(self):
        self.last_poll_at = time.time()
        rows = await self._fetch_candidates()
        if not rows:
            return

        total_parsed = 0
        new_count = 0
        new_cands: list[Candidate] = []
        eff = self.effective_gates()   # liveness baselines (auto-tuned when live)
        now = time.time()
        for row in rows:
            cand = self._to_candidate(row)
            if cand is None:
                continue
            total_parsed += 1
            # Liveness: trailing stats can look hot while nothing trades NOW —
            # forward only on proven between-poll growth (or a roaring debut).
            if not self._passes_liveness(cand.mint, cand.swaps, cand.volume,
                                         int(eff["min_swaps"]), float(eff["min_volume_usd"]), now):
                continue
            # Dedup — never re-feed within sane age window
            existing = self._seen.get(cand.mint)
            if existing is not None:
                if existing.age_minutes < max(1.0, self.config.cooldown_after_feed_minutes):
                    continue
                cand.first_seen = time.time()
                self._seen[cand.mint] = cand
                continue
            cand.first_seen = time.time()
            self._seen[cand.mint] = cand
            new_count += 1
            new_cands.append(cand)

        logger.info(f"[AutoFeed] Tick: raw={len(rows)} parsed={total_parsed} new={new_count}")

        if not new_cands:
            return

        # Cooldown — don't feed during the configured gap
        if self._last_fed_at > 0:
            ago_min = (time.time() - self._last_fed_at) / 60.0
            if ago_min < self.config.cooldown_after_feed_minutes:
                return

        # Backpressure — `active_count_fn` is async (returns count of live traders).
        if self._active_count_fn is not None:
            try:
                active = int(await self._active_count_fn())
            except Exception:
                active = 0
        else:
            active = 0

        # Re-rank by motion first (per OP spec: "trending coins with lots of motion"):
        # volume desc → swaps desc → smart_degen_count desc → rug asc (organic tiebreak)
        new_cands.sort(key=lambda c: (-c.volume, -c.swaps, -c.smart_degen_count, c.rug_ratio))

        for cand in new_cands:
            if active >= self.config.max_concurrent_feed:
                break
            if self._forward_fn is None:
                continue
            try:
                await self._forward_fn(cand)
                self.total_fed += 1
                self._last_fed_at = time.time()
                logger.info(
                    f"[AutoFeed] FED  ${cand.symbol}  mint={cand.mint[:8]}… "
                    f"mcap=${cand.market_cap:.0f} liq=${cand.liquidity:.0f} "
                    f"smart={cand.smart_degen_count} rug={cand.rug_ratio:.2f} "
                    f"bundler={cand.bundler_rate:.2f} insider={cand.insider_rate:.2f}"
                )
                active += 1
            except Exception as e:
                logger.warning(f"[AutoFeed] forward_fn error: {e}")

        # Drop stale tracked candidates from `_seen` cache
        if len(self._seen) > 200:
            self._seen = {k: v for k, v in self._seen.items() if v.age_minutes < 60}

    # ── Snapshot (REST/WS UI) ────────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "enabled":   self.config.enabled,
            "is_running": self.is_running(),
            "cli_version": GMGN_CLI_VERSION,
            "cli_command": GMGN_COMMAND,
            "cli_configured": bool(os.environ.get("GMGN_API_KEY")) or
                              Path_exists(".env") or Path_exists_env_gmgn(),
            "poll_seconds": self.config.poll_seconds,
            "interval":  self.config.interval,
            "platforms": [s.strip() for s in (self.config.platforms or "").split(",") if s.strip()],
            "exclude_mints": [s.strip() for s in (self.config.exclude_mints or "").split(",") if s.strip()],
            "min_mcap_usd": self.config.min_mcap_usd,
            "max_mcap_usd": self.config.max_mcap_usd,
            "min_liquidity_usd": self.config.min_liquidity_usd,
            "min_holders": self.config.min_holders,
            "min_smart_degen_count": self.config.min_smart_degen_count,
            "min_volume_usd": self.config.min_volume_usd,
            "min_swaps": self.config.min_swaps,
            "order_by": self.config.order_by,
            "require_migration_exchange": self.config.require_migration_exchange,
            "migration_exchanges": [s.strip() for s in (self.config.migration_exchanges or "").split(",") if s.strip()],
            "max_top10_holder_rate": self.config.max_top10_holder_rate,
            "max_rug_ratio": self.config.max_rug_ratio,
            "max_bundler_rate": self.config.max_bundler_rate,
            "max_insider_rate": self.config.max_insider_rate,
            "max_rat_trader_rate": self.config.max_rat_trader_rate,
            "max_entrapment_ratio": self.config.max_entrapment_ratio,
            "max_bot_degen_rate": self.config.max_bot_degen_rate,
            "require_renounced_mint": self.config.require_renounced_mint,
            "require_renounced_freeze": self.config.require_renounced_freeze,
            "reject_wash_trading": self.config.reject_wash_trading,
            "reject_honeypot": self.config.reject_honeypot,
            "require_has_social": self.config.require_has_social,
            "max_created_age": self.config.max_created_age,
            "max_concurrent_feed": self.config.max_concurrent_feed,
            "cooldown_after_feed_minutes": self.config.cooldown_after_feed_minutes,
            "require_motion_presence": self.config.require_motion_presence,
            "min_swaps_growth": self.config.min_swaps_growth,
            "min_volume_growth_usd": self.config.min_volume_growth_usd,
            "hot_bypass_multiple": self.config.hot_bypass_multiple,
            "dead_mint_cooldown_hours": self.config.dead_mint_cooldown_hours,
            "dead_mint_count": len(self._dead_mints),
            "motion_tracked": len(self._last_motion),
            # Auto-tune block — conditions, effective gates, and provenance
            "auto_tune_enabled": self.config.auto_tune_enabled,
            "auto_tune_refresh_seconds": self.config.auto_tune_refresh_seconds,
            "auto_tune_mcap_grad_discount": self.config.auto_tune_mcap_grad_discount,
            "market_conditions": self._market_conditions,
            "effective_gates": self.effective_gates(),
            "last_tune_at": self._last_tune_at,
            "tune_history": self._tune_history[-10:],
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "total_seen": self.total_seen,
            "total_fed":  self.total_fed,
            "active_tracked": len(self._seen),
            "recent_candidates": [c.to_dict() for c in list(self._seen.values())[-10:]],
        }
