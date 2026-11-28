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

Shape probe 2026-11-16 (gmgn-cli@1.5.2 + GMGN_API_KEY from the repo-root .env,
loaded by main.py:12-18): `market trending --raw` returns 95 fields and the
keyed CLI works (exit 0, code 0).  `insider_rate` was RETIRED upstream
(absent in 5/5 probed rows) — its old gate silently no-op'd under fail-open
parsing (`0 < x > limit` never fires for a field coerced to 0.0), which is
the degradation this repair closes.  `_to_candidate` is now FAIL-CLOSED: a
row missing/unparseable for any required field is rejected with a tallyable
reason, never passed.  New supplementary fields consumed (optional):
sniper_count, top70_sniper_hold_rate, dev_team_hold_rate, cto_flag,
twitter_create/del_post/rename counts, image/telegram/website dup flags,
buy/sell tax, history_highest_market_cap.  `token security` / `token holders`
subcommands verified for per-mint backfill (launch-time-stable vector).

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
import feed_store
from organic_screen import OrganicScreen, ScreenConfig

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
DEFAULT_AUTO_TUNE_MCAP_GRAD_DISCOUNT = 1.0

DEFAULT_POLL_SECONDS              = 60.0
# Spec: feed pump.fun migrated tokens above 15k mcap.
# Absolute floor only — the live floor is the SOL-anchored graduation mcap
# (GRAD_MCAP_USD_PER_SOL × SOL × discount), see _compute_tuned_gates.  The old
# static $20k floor silently sat 32% BELOW the graduation mcap when SOL fell
# to ~$72 (grad ≈ $29.5k), so the feed deliberately targeted post-graduation
# bleeders.  Setuh's blueprint uses the same line: migrated mcap ≥ $30k.
DEFAULT_MIN_MCAP_USD              = 10_000.0
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
# GMGN manipulation-INDICATOR defaults are RETIRED (user directive
# 2026-11-28): the organic screen computes holder forensics from the chain
# itself, so GMGN's rug/bundler/insider/top10 scores no longer gate anything.
# The constants are kept as legacy overrides (config can re-enable them).
DEFAULT_MAX_TOP10_HOLDER_RATE     = None
DEFAULT_MAX_RUG_RATIO            = None
DEFAULT_MAX_BUNDLER_RATE         = None
DEFAULT_MAX_INSIDER_RATE          = None
DEFAULT_MAX_RAT_TRADER_RATE       = None
DEFAULT_MAX_ENTRAPMENT_RATIO      = None
DEFAULT_MAX_BOT_DEGEN_RATE        = None
# Organic-only booleans: renounced/freeze stay (on-chain FACTS).  Wash-trading
# was GMGN's own classification — the screen + tape tripwire replace it.
DEFAULT_REQUIRE_RENOUNCED_MINT        = True
DEFAULT_REQUIRE_RENOUNCED_FREEZE      = True
DEFAULT_REJECT_WASH_TRADING           = False
DEFAULT_REJECT_HONEYPOT               = True
DEFAULT_REQUIRE_HAS_SOCIAL        = False        # too strict on Pump.fun — many clean tokens lack social
DEFAULT_MAX_CREATED_AGE           = ""           # no age cap by default (PumpSwap tokens can be days old)
# Lifecycle window: feed recently-migrated tokens, not hours-old chases.
# Setuh's "soon" page is age <= 30 min; we allow a wider recording window.
DEFAULT_MIN_MIGRATED_AGE_MINUTES  = 2.0
DEFAULT_MAX_MIGRATED_AGE_MINUTES  = 0.0   # 0 = no cap
# Don't chase a token that already pumped this much in the trending interval
# (the 6-slot feed was selecting exactly these at the top of the volume list).
DEFAULT_MAX_PRICE_CHANGE_1H_PCT   = 300.0
# ...
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
# Concurrency budget (2026-11-27).  This is the number of CONCURRENT sessions,
# not the per-tick burst.  The Nov-24 calibration storm was 19 PARALLEL chain
# walks; that is now impossible (`_CALIBRATION_CONCURRENCY=1` serializes every
# tick-0 chain walk), and each session's steady-state cost is two shared-WS
# vault subscriptions.  20 keeps the public WS at ~40 subscriptions while
# restoring overnight throughput (6 slots × ~39 min median session ≈ 3/h was
# the Nov-24..27 collapse: 18-22 recordings/night vs 113-114 on Nov 15-16).
# Raise to 30 only after watching WS/RPC health; 100 exceeds the public WS
# subscription comfort zone.
DEFAULT_MAX_CONCURRENT_FEED         = 20
# A single tick nominating 20 sessions would queue ~13 min of serialized
# calibration and feed tokens that have already moved.  Smooth the burst.
DEFAULT_MAX_FEEDS_PER_TICK          = 4
# A candidate that cannot be started within this window is stale (the pump it
# was selected for is over).  Dropped and released, never silently burned.
DEFAULT_FEED_CANDIDATE_TTL_SECONDS  = 300.0
DEFAULT_COOLDOWN_AFTER_FEED_MINUTES = 1.0
DEFAULT_EXCLUDE_MINTS              = ""   # comma-separated manual exclusion


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
    # Supplementary risk vector (GMGN fields verified 2026-11-16; optional in
    # responses — neutral defaults when absent).  Feeds dashboard + persistence.
    sniper_count:      int = 0
    top70_sniper_hold_rate: float = 0.0
    dev_team_hold_rate: float = 0.0
    cto_flag:          bool = False
    twitter_create_token_count: int = 0
    twitter_del_post_token_count: int = 0
    twitter_rename_count: int = 0
    dup_count:         int = 0   # image/telegram/website duplicate flags (serial-scammer signal)
    buy_tax:           float = 0.0
    sell_tax:          float = 0.0
    is_open_source:    Optional[bool] = None
    history_highest_market_cap: float = 0.0
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
    creator:               str = ""   # dev wallet (holder forensics / blacklist)
    reason:                str = "gmgn_organic"
    first_seen:            float = field(default_factory=time.time)
    # Preserved across re-parses so the feed TTL measures true candidate age,
    # not "time since the last trending row mentioned it".
    first_discovered:      float = field(default_factory=time.time)

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
    order_by:               str = DEFAULT_ORDER_BY               # server-side ranking key
    require_migration_exchange: bool = DEFAULT_REQUIRE_MIGRATION_EXCHANGE
    migration_exchanges:    str = DEFAULT_MIGRATION_EXCHANGES    # comma-separated exchange allow-list
    # GMGN manipulation-INDICATOR gates: None = disabled (2026-11-28 — the
    # organic screen's on-chain forensics is authoritative; these remain as
    # configurable overrides).
    max_top10_holder_rate:  Optional[float] = DEFAULT_MAX_TOP10_HOLDER_RATE
    max_rug_ratio:          Optional[float] = DEFAULT_MAX_RUG_RATIO
    max_bundler_rate:       Optional[float] = DEFAULT_MAX_BUNDLER_RATE
    max_insider_rate:       Optional[float] = DEFAULT_MAX_INSIDER_RATE
    max_rat_trader_rate:    Optional[float] = DEFAULT_MAX_RAT_TRADER_RATE
    max_entrapment_ratio:   Optional[float] = DEFAULT_MAX_ENTRAPMENT_RATIO
    max_bot_degen_rate:     Optional[float] = DEFAULT_MAX_BOT_DEGEN_RATE
    max_dev_team_hold_rate:      Optional[float] = None
    max_top70_sniper_hold_rate:  Optional[float] = None
    max_twitter_create_token_count: Optional[int] = None
    reject_cto_flag:             bool = False
    max_buy_sell_tax_pct:        Optional[float] = None
    require_renounced_mint:  bool = DEFAULT_REQUIRE_RENOUNCED_MINT
    require_renounced_freeze: bool = DEFAULT_REQUIRE_RENOUNCED_FREEZE
    reject_wash_trading:     bool = False   # GMGN indicator; screen + tripwire instead
    reject_honeypot:         bool = DEFAULT_REJECT_HONEYPOT
    require_has_social:      bool = DEFAULT_REQUIRE_HAS_SOCIAL
    max_created_age:         str = DEFAULT_MAX_CREATED_AGE
    # Lifecycle band (2026-11-27): 0 disables a bound.
    min_migrated_age_minutes: float = DEFAULT_MIN_MIGRATED_AGE_MINUTES
    max_migrated_age_minutes: float = DEFAULT_MAX_MIGRATED_AGE_MINUTES
    max_price_change_1h_pct:  Optional[float] = DEFAULT_MAX_PRICE_CHANGE_1H_PCT
    max_concurrent_feed:     int = DEFAULT_MAX_CONCURRENT_FEED
    max_feeds_per_tick:      int = DEFAULT_MAX_FEEDS_PER_TICK
    feed_candidate_ttl_seconds: float = DEFAULT_FEED_CANDIDATE_TTL_SECONDS
    cooldown_after_feed_minutes: float = DEFAULT_COOLDOWN_AFTER_FEED_MINUTES
    exclude_mints:           str = DEFAULT_EXCLUDE_MINTS
    # Market-condition auto-tune (see §"auto-tune" constants above)
    auto_tune_enabled:       bool = DEFAULT_AUTO_TUNE_ENABLED
    auto_tune_refresh_seconds: float = DEFAULT_AUTO_TUNE_REFRESH_SECONDS
    auto_tune_mcap_grad_discount: float = DEFAULT_AUTO_TUNE_MCAP_GRAD_DISCOUNT
    # Organic holder forensics (organic_screen.py).  Hard bundle rules enforce;
    # the composite score is logged in shadow until its thresholds are fitted
    # from the outcome loop.
    organic_screen_enabled:      bool = True
    organic_screen_enforce_hard: bool = True
    organic_screen_enforce_score: bool = False
    organic_screen_score_threshold: float = 70.0
    organic_screen_require_data: bool = False
    organic_screen_max_fetches_per_tick: int = 1

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

        # Organic holder-forensics screen (Setuh/Padre-style bundle checks).
        # Reuses this module's gmgn-cli runner so there is ONE rate-limit
        # surface for trending + holders.
        self._screen = OrganicScreen(
            cfg=self._screen_config(), cli_runner=self._run_cli_for_screen)
        self._screen_scores: dict[str, float] = {}

        # State
        self._seen: dict[str, Candidate] = {}
        # Candidates that passed the cheap gates but have not reached the
        # holder-screen budget yet.  Kept across ticks so a rotating trending
        # list cannot lose them (2026-11-27 throughput fix).
        self._pending_screen: dict[str, Candidate] = {}
        self._last_fed_at: float = 0.0
        self.last_error: str = ""
        self.last_poll_at: float = 0.0
        self.total_seen: int = 0
        self.total_fed: int = 0
        # Feed accounting (2026-11-27): a forward that could not start a
        # session must never be counted as fed.
        self.total_rejected_screen: int = 0
        self.total_fed_not_started: int = 0
        self.total_dropped_stale: int = 0
        self._fed_not_started_tally: dict[str, int] = {}
        self._screen_reject_tally: dict[str, int] = {}
        # Auto-tune state
        self._market_conditions: Optional[dict] = None
        self._last_tuned_gates: Optional[dict] = None
        self._last_tune_at: float = 0.0
        self._last_tune_refresh_at: float = 0.0
        self._tune_history: list[dict] = []
        # Shape/health telemetry (fail-closed repair 2026-11-16): per-tick
        # shape report + cumulative totals so GMGN response drift is loud.
        self._shape_report: dict = {}
        self._shape_totals: dict = {"raw_rows": 0, "rows_missing_required": 0}
        self._reject_tally_total: dict = {}

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
            f"screen={'chain' if self.config.organic_screen_enabled else 'off'} "
            f"smart≤{self.config.min_smart_degen_count}"
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
        if changed:
            self._sync_screen_config()
        return changed

    # -- organic screen plumbing ──────────────────────────────────────────────
    def _screen_config(self) -> ScreenConfig:
        cf = self.config
        return ScreenConfig(
            enabled=cf.organic_screen_enabled,
            enforce_hard=cf.organic_screen_enforce_hard,
            enforce_score=cf.organic_screen_enforce_score,
            score_threshold=cf.organic_screen_score_threshold,
            require_holder_data=cf.organic_screen_require_data,
            max_fetches_per_tick=cf.organic_screen_max_fetches_per_tick,
        )

    def _sync_screen_config(self) -> None:
        """Copy the hot-swappable screen knobs onto the live screen object."""
        self._screen.cfg = self._screen_config()

    async def _run_cli_for_screen(self, args: list[str]) -> Optional[dict]:
        """gmgn-cli runner for the holder screen (same CLI/env as trending)."""
        return await self._run_cli(args)

    async def screen_candidate(self, cand: Candidate,
                               creator: str = "") -> "object":
        """Screen one candidate through the organic holder forensics."""
        return await self._screen.screen(cand.mint, creator=creator)

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
        #    (fixed 85-SOL curve event ⇒ grad_mcap ≈ 410.7 × sol_usd).  The
        #    user value is an ABSOLUTE FLOOR only; the frontier target wins
        #    whenever it is higher.  (The old max(user, grad×0.5) let a static
        #    $20k sit 32% below a $29.5k graduation mcap at SOL=$72, so the
        #    feed targeted post-graduation bleeders — 2026-11-27 quality fix.)
        min_mcap = cf.min_mcap_usd
        if conditions and "sol_usd" in conditions:
            grad_mcap = GRAD_MCAP_USD_PER_SOL * float(conditions["sol_usd"])
            min_mcap = clampf(
                max(cf.min_mcap_usd,
                    grad_mcap * cf.auto_tune_mcap_grad_discount),
                0.0, 250_000.0)

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

        # Server-side numeric range filters (supported by gmgn-cli): market
        # SHAPE only.  GMGN's manipulation indicators (top10/rug/bundler/
        # insider/entrapment/bot) are deliberately NOT sent — per user
        # directive 2026-11-28 the filtering is done by our own on-chain
        # holder forensics (organic_screen.py), not by GMGN's scores.
        args += ["--min-marketcap", str(int(eff["min_mcap_usd"]))]
        args += ["--max-marketcap", str(int(cf.max_mcap_usd))]
        args += ["--min-liquidity", str(int(eff["min_liquidity_usd"]))]
        args += ["--min-holder-count", str(int(cf.min_holders))]
        args += ["--min-smart-degen-count", str(int(cf.min_smart_degen_count))]
        # Motion gates (OP: "lots of motion - trending coins"):
        args += ["--min-volume", str(int(eff["min_volume_usd"]))]
        args += ["--min-swaps", str(int(eff["min_swaps"]))]

        # On-chain FACTS (mint/freeze authority — verifiable, not scores).
        args += ["--filter", "renounced", "--filter", "frozen"]
        if cf.reject_wash_trading:            # OFF by default
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
        proc = None
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
            # Do not leave a stray npx/gmgn-cli process behind: the holder
            # screen wraps this call with a shorter timeout and cancels it.
            try:
                proc.kill()
            except Exception:
                pass
            return None
        except Exception as e:
            self.last_error = f"CLI error: {type(e).__name__}: {e}"
            logger.error(f"[AutoFeed] CLI error: {e}", exc_info=True)
            return None
        finally:
            # Cancellation (the holder screen's outer wait_for) must not leave
            # an npx/gmgn-cli process behind.
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass

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

    # Fields every GMGN trending row MUST carry for the organic filter to be
    # meaningful (probed 2026-11-16, gmgn-cli@1.5.2 with GMGN_API_KEY from the
    # repo-root .env — all present 5/5).  A row missing/unparseable for any of
    # these is REJECTED, never passed: under the previous fail-open parsing a
    # single GMGN shape change silently turned every affected gate into a
    # no-op (`0 < x > limit` never fires for a field coerced to 0.0).  That
    # exact regression shipped unnoticed — `insider_rate` was retired upstream
    # and its gate died silently — which is what this fail-closed repair
    # closes.
    # 2026-11-28 (user directive): GMGN's manipulation INDICATORS are out of
    # the filter — the organic screen computes holder forensics from the chain
    # itself.  Required rows now carry only market-shape data, on-chain facts
    # (mint/freeze authority) and the honeypot flag.
    REQUIRED_FIELDS = (
        "market_cap", "liquidity", "holder_count", "smart_degen_count",
        "swaps", "volume", "exchange",
        "is_honeypot", "renounced_mint",
        "renounced_freeze_account", "creation_timestamp",
    )
    # `insider_rate` was retired from trending responses (absent in 5/5 probed
    # rows, 2026-11-16).  Kept as an OPTIONAL gate — enforced only when GMGN
    # returns the field again.
    OPTIONAL_RISK_FIELDS = ("insider_rate",)

    def _to_candidate(self, raw: dict,
                      reject_tally: Optional[dict] = None) -> Optional[Candidate]:
        """Convert raw gmgn row → Candidate (FAIL-CLOSED).

        Returns None unless the row is verifiably organic: a required field
        missing/unparseable (`shape_missing` — ABSENT is treated as NOT
        organic-verified, never as a pass), a risk gate trips, the venue is
        not a migration exchange, motion gates fail, or a boolean gate trips.
        `reject_tally` (optional dict) accumulates reject-reason counts for
        the tick shape report (`_field_missing:<field>` keys carry per-field
        detail).
        """
        def reject(reason: str) -> None:
            if reject_tally is not None:
                reject_tally[reason] = reject_tally.get(reason, 0) + 1

        mint = (raw.get("address") or raw.get("mint") or raw.get("token_address") or "").strip()
        if not mint or len(mint) < 32 or len(mint) > 44:
            reject("bad_mint")
            return None
        # NOTE: do NOT lower-case.  Solana addresses are base58 and case-sensitive —
        # lower-casing corrupts the encoding (this was causing "Invalid Solana address"
        # errors when the frontend tried to startlive-trading the auto-fed mint).
        # Compare excluded-set case-insensitively so users can type mints either way.
        if mint.lower() in self._excluded_set():
            reject("excluded")
            return None

        # ── Shape validation (fail-closed) ──
        missing = [f for f in self.REQUIRED_FIELDS if raw.get(f) is None]
        if missing:
            reject("shape_missing")
            for f in missing:
                if reject_tally is not None:
                    k = f"_field_missing:{f}"
                    reject_tally[k] = reject_tally.get(k, 0) + 1
            return None

        # Strict readers — absent/unparseable ⇒ reject (never a silent 0.0 pass).
        def fnum(key: str) -> Optional[float]:
            try:
                v = raw.get(key)
                return None if v is None else float(v)
            except (TypeError, ValueError):
                reject(f"shape_missing:{key}")
                return None

        def inum(key: str) -> Optional[int]:
            try:
                v = raw.get(key)
                return None if v is None else int(v)
            except (TypeError, ValueError):
                reject(f"shape_missing:{key}")
                return None

        def breq(key: str) -> Optional[bool]:
            v = raw.get(key)
            if v is None:
                reject(f"shape_missing:{key}")
                return None
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            s = str(v).strip().lower()
            if s in ("yes", "true", "1"):
                return True
            if s in ("no", "false", "0"):
                return False
            reject(f"shape_missing:{key}")
            return None

        # Tolerant readers for SUPPLEMENTARY fields — absent/unparseable ⇒
        # neutral default, never a reject (these gate only when present).
        def fopt(key: str) -> float:
            try:
                v = raw.get(key)
                return 0.0 if v is None else float(v)
            except (TypeError, ValueError):
                return 0.0

        def iopt(key: str) -> int:
            try:
                v = raw.get(key)
                return 0 if v is None else int(v)
            except (TypeError, ValueError):
                return 0

        def bopt(key: str) -> bool:
            v = raw.get(key)
            if isinstance(v, bool):
                return v
            if isinstance(v, (int, float)):
                return bool(v)
            if isinstance(v, str):
                return v.strip().lower() in ("yes", "true", "1")
            return False

        mcap    = fnum("market_cap")
        liq     = fnum("liquidity")
        holders = inum("holder_count")
        smart   = inum("smart_degen_count")
        swaps   = inum("swaps")
        vol     = fnum("volume")
        # GMGN indicator fields: telemetry only (never gates), so a missing
        # field cannot fail-closed a row that is otherwise market-verifiable.
        top10   = fopt("top_10_holder_rate")
        rug     = fopt("rug_ratio")
        bundler = fopt("bundler_rate")
        rat     = fopt("rat_trader_amount_rate")
        entrap  = fopt("entrapment_ratio")
        bot_degen = fopt("bot_degen_rate")
        created = fnum("creation_timestamp")
        is_wash  = bopt("is_wash_trading")
        is_honey = breq("is_honeypot")
        ren_mint = breq("renounced_mint")
        ren_freeze = breq("renounced_freeze_account")
        # Optional (retired upstream 2026-11-16): gate applies only when present.
        insider = fnum("insider_rate")

        required_vals = (mcap, liq, holders, smart, swaps, vol, created,
                         is_honey, ren_mint, ren_freeze)
        if any(v is None for v in required_vals):
            return None
        exchange = (raw.get("exchange") or "").strip().lower()

        # ── Risk / motion gates (presence already proven — plain compares) ──
        cf = self.config
        eff = self.effective_gates()   # auto-tuned when enabled & conditions live
        if mcap < eff["min_mcap_usd"] or mcap > cf.max_mcap_usd:
            reject("mcap_window"); return None
        if liq < eff["min_liquidity_usd"]:
            reject("min_liquidity_usd"); return None
        if holders < cf.min_holders:
            reject("min_holders"); return None
        if smart < cf.min_smart_degen_count:
            reject("min_smart_degen_count"); return None
        # Indicator gates: OFF by default (None/<=0) — the organic screen's
        # on-chain holder forensics is the authoritative check.  Kept as
        # configurable overrides for cross-checking GMGN's own numbers.
        if cf.max_top10_holder_rate is not None and cf.max_top10_holder_rate > 0 \
                and top10 > cf.max_top10_holder_rate:
            reject("max_top10_holder_rate"); return None
        if cf.max_rug_ratio is not None and cf.max_rug_ratio > 0 \
                and rug > cf.max_rug_ratio:
            reject("max_rug_ratio"); return None
        if cf.max_bundler_rate is not None and cf.max_bundler_rate > 0 \
                and bundler > cf.max_bundler_rate:
            reject("max_bundler_rate"); return None
        if insider is not None and cf.max_insider_rate is not None \
                and cf.max_insider_rate > 0 and insider > cf.max_insider_rate:
            reject("max_insider_rate"); return None
        if cf.max_rat_trader_rate is not None and cf.max_rat_trader_rate > 0 \
                and rat > cf.max_rat_trader_rate:
            reject("max_rat_trader_rate"); return None
        if cf.max_entrapment_ratio is not None and cf.max_entrapment_ratio > 0 \
                and entrap > cf.max_entrapment_ratio:
            reject("max_entrapment_ratio"); return None
        if cf.max_bot_degen_rate is not None and cf.max_bot_degen_rate > 0 \
                and bot_degen > cf.max_bot_degen_rate:
            reject("max_bot_degen_rate"); return None

        # OP: "only pump.fun migrated coins" — verify the current venue is in
        # the migration allow-list (NOT still on pump.fun's bonding curve,
        # which uses exchange="pump").
        if cf.require_migration_exchange:
            allowed = {s.strip().lower() for s in cf.migration_exchanges.split(",") if s.strip()}
            if allowed and exchange not in allowed:
                reject("migration_exchange"); return None

        # OP: "coins that got fed should have lots of motion - trending coins" —
        # motion gates locally too (defense in depth; auto-tuned effective gates).
        if vol < eff["min_volume_usd"]:
            reject("min_volume_usd"); return None
        if swaps < eff["min_swaps"]:
            reject("min_swaps"); return None

        # ── Lifecycle band (2026-11-27 quality fix) ────────────────────────
        # Feed recently-migrated tokens, not hours-old chases.  `open_timestamp`
        # is the pool-open/migration time on PumpSwap rows; fall back to the
        # mint's creation time.  A row with neither is a shape reject.
        open_ts = fopt("open_timestamp") or created
        age_min = (time.time() - open_ts) / 60.0 if open_ts > 0 else -1.0
        if age_min >= 0:
            if cf.min_migrated_age_minutes > 0 and age_min < cf.min_migrated_age_minutes:
                reject("too_fresh"); return None
            if cf.max_migrated_age_minutes > 0 and age_min > cf.max_migrated_age_minutes:
                reject("too_old"); return None
        if cf.max_price_change_1h_pct is not None:
            chg_1h = fopt("price_change_percent1h") or fopt("price_change_percent")
            if chg_1h > cf.max_price_change_1h_pct:
                reject("already_pumped"); return None

        # Boolean gates — wash trading & honeypot are non-negotiable rejections.
        if cf.reject_wash_trading and is_wash:
            reject("wash_trading"); return None   # OFF by default (see config)
        if cf.reject_honeypot and is_honey:
            reject("honeypot"); return None
        if cf.require_renounced_mint and not ren_mint:
            reject("not_renounced_mint"); return None
        if cf.require_renounced_freeze and not ren_freeze:
            reject("not_renounced_freeze"); return None

        # Has social — organic validation
        has_social = bool(
            raw.get("twitter_username") or raw.get("telegram") or
            raw.get("website") or raw.get("has_at_least_one_social")
        )
        if cf.require_has_social and not has_social:
            reject("no_social"); return None

        # ── Supplementary risk vector (probed 2026-11-16; optional fields) ──
        dev_team_hold = fopt("dev_team_hold_rate")
        top70_sniper  = fopt("top70_sniper_hold_rate")
        sniper_count  = iopt("sniper_count")
        cto_flag      = bopt("cto_flag")
        tw_create     = iopt("twitter_create_token_count")
        tw_del_post   = iopt("twitter_del_post_token_count")
        tw_rename     = iopt("twitter_rename_count")
        dup_count     = (int(bopt("image_dup")) + int(bopt("telegram_dup"))
                         + int(bopt("website_dup")))
        buy_tax       = fopt("buy_tax")
        sell_tax      = fopt("sell_tax")
        open_source_v = raw.get("is_open_source")
        open_source   = None if open_source_v is None else bopt("is_open_source")
        hist_high_mcap = fopt("history_highest_market_cap")

        # Optional extended gates — default OFF (None disables); supplementary
        # fields are enforced only when the config sets a ceiling.
        if cf.max_dev_team_hold_rate is not None and dev_team_hold > cf.max_dev_team_hold_rate:
            reject("max_dev_team_hold_rate"); return None
        if cf.max_top70_sniper_hold_rate is not None and top70_sniper > cf.max_top70_sniper_hold_rate:
            reject("max_top70_sniper_hold_rate"); return None
        if cf.max_twitter_create_token_count is not None and tw_create > cf.max_twitter_create_token_count:
            reject("max_twitter_create_token_count"); return None
        if cf.reject_cto_flag and cto_flag:
            reject("cto_flag"); return None
        if cf.max_buy_sell_tax_pct is not None and max(buy_tax, sell_tax) > cf.max_buy_sell_tax_pct:
            reject("max_buy_sell_tax_pct"); return None

        return Candidate(
            mint=mint,
            name=(raw.get("name") or "").strip(),
            symbol=(raw.get("symbol") or raw.get("ticker") or "").strip(),
            market_cap=mcap, liquidity=liq, holders=holders,
            swaps=swaps, volume=vol,
            price_change_1h=fopt("price_change_percent1h") or fopt("price_change_percent"),
            price_change_5m=fopt("price_change_percent5m"),
            price_change_1m=fopt("price_change_percent1m"),
            top10_pct=top10, rug_ratio=rug, bundler_rate=bundler,
            insider_rate=insider if insider is not None else 0.0,
            rat_trader_rate=rat, entrapment_ratio=entrap,
            bot_degen_rate=bot_degen, is_wash_trading=is_wash, is_honeypot=is_honey,
            renounced_mint=ren_mint, renounced_freeze=ren_freeze,
            smart_degen_count=smart,
            renowned_count=iopt("renowned_count"),
            creator_token_status=raw.get("creator_token_status") or "",
            launchpad_platform=raw.get("launchpad_platform") or "",
            exchange=raw.get("exchange") or "",
            creation_timestamp=created,
            open_timestamp=fopt("open_timestamp") or created,
            twitter=raw.get("twitter_username") or "",
            telegram=raw.get("telegram") or "",
            website=raw.get("website") or "",
            creator=raw.get("creator") or "",
            sniper_count=sniper_count,
            top70_sniper_hold_rate=top70_sniper,
            dev_team_hold_rate=dev_team_hold,
            cto_flag=cto_flag,
            twitter_create_token_count=tw_create,
            twitter_del_post_token_count=tw_del_post,
            twitter_rename_count=tw_rename,
            dup_count=dup_count,
            buy_tax=buy_tax, sell_tax=sell_tax,
            is_open_source=open_source,
            history_highest_market_cap=hist_high_mcap,
        )

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
            # While GMGN is in a ban window, retry sooner than the full poll
            # interval so the first open slot is not wasted (the shared ban
            # state is cleared by whichever consumer notes the reset).
            wait_s = self.config.poll_seconds
            if rl.gmgn_banned():
                wait_s = min(15.0, wait_s)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                pass
        logger.info("[AutoFeed] Poll loop exited")

    async def _tick(self):
        self.last_poll_at = time.time()
        rows = await self._fetch_candidates()
        if not rows:
            return
        await self._process_rows(rows)

    async def _process_rows(self, rows: list[dict]):
        """Parse + shape-report + dedup + rank + forward one batch of raw rows.

        Split out of `_tick` so tests can drive the full row pipeline (shape
        telemetry, dedup, ranking) without invoking the CLI.
        """
        total_parsed = 0
        new_count = 0
        new_cands: list[Candidate] = []
        reject_tally: dict[str, int] = {}
        for row in rows:
            cand = self._to_candidate(row, reject_tally)
            if cand is None:
                continue
            total_parsed += 1
            # Dedup — never re-feed within sane age window
            existing = self._seen.get(cand.mint)
            if existing is not None:
                if existing.age_minutes < max(1.0, self.config.cooldown_after_feed_minutes):
                    continue
                # Re-parse after the cooldown: keep the ORIGINAL discovery
                # time so the feed TTL is honest about how long the token has
                # been waiting to start.
                cand.first_discovered = existing.first_discovered
                cand.first_seen = time.time()
                self._seen[cand.mint] = cand
                # Keep the queue entry's metrics fresh (same discovery age).
                if cand.mint in self._pending_screen:
                    self._pending_screen[cand.mint] = cand
                continue
            cand.first_seen = time.time()
            self._seen[cand.mint] = cand
            self.total_seen += 1
            new_count += 1
            new_cands.append(cand)

        # Shape report (fail-closed repair 2026-11-16): track WHY rows were
        # rejected and whether GMGN's response shape drifted.  A missing-field
        # row is a hard reject — if >50% of a tick's rows lack required fields
        # the API shape has drifted and the feed is fail-closed starving.
        field_missing = {k[len("_field_missing:"):]: v
                         for k, v in reject_tally.items() if k.startswith("_field_missing:")}
        reasons = {k: v for k, v in reject_tally.items() if not k.startswith("_field_missing:")}
        shape = {
            "ts": time.time(),
            "raw_rows": len(rows),
            "rows_rejected": len(rows) - total_parsed,
            "rows_missing_required": reasons.get("shape_missing", 0),
            "field_missing_counts": field_missing,
            "reject_reasons": reasons,
        }
        self._shape_report = shape
        self._shape_totals["raw_rows"] += len(rows)
        self._shape_totals["rows_missing_required"] += shape["rows_missing_required"]
        for k, v in reasons.items():
            self._reject_tally_total[k] = self._reject_tally_total.get(k, 0) + v
        if rows and shape["rows_missing_required"] / len(rows) > 0.5:
            logger.warning(
                f"[AutoFeed] SHAPE DRIFT: {shape['rows_missing_required']}/{len(rows)} rows missing "
                f"required fields {field_missing} — GMGN response shape changed; "
                f"feed is fail-closed and starving"
            )
        logger.info(
            f"[AutoFeed] Tick: raw={len(rows)} parsed={total_parsed} new={new_count} "
            f"rejected={shape['rows_rejected']}"
        )
        if rows and total_parsed == 0:
            # Gate starvation is distinct from shape drift: GMGN answered but
            # every row failed our (tightened) quality gates.  Loud so a
            # threshold that is too strict for the current regime is visible.
            logger.warning(
                f"[AutoFeed] GATE STARVATION: 0/{len(rows)} rows passed the "
                f"organic gates — {reasons}")

        if not new_cands and not self._pending_screen:
            return

        # Cooldown is evaluated after screening (below): it must gate FEEDING,
        # never the holder-forensics pass, otherwise every feed also delays
        # the pending queue by a minute.

        # Backpressure — `active_count_fn` is async (returns count of live traders).
        # Checked BEFORE the holder screen: spending a GMGN call for a token
        # that cannot start is waste (and risks the shared rate limit).
        if self._active_count_fn is not None:
            try:
                active = int(await self._active_count_fn())
            except Exception:
                active = 0
        else:
            active = 0
        if active >= self.config.max_concurrent_feed:
            for cand in new_cands:
                self._pending_screen[cand.mint] = cand
            return

        budget = max(1, min(
            int(self.config.max_feeds_per_tick),
            int(self.config.max_concurrent_feed) - active,
        ))

        # ── Rank BEFORE screening so the holder calls go to the best rows ──
        # Cheap quality proxy first (lower = cleaner), then the freshest
        # migration, then motion.  Volume-first ordering was the pump-chasing
        # bug: the 6-slot feed waited at the top of the volume list and fed
        # whatever was mid-manipulation when a slot freed.
        def _proxy(c: Candidate) -> float:
            return (2.0 * c.rug_ratio + c.bundler_rate
                    + 0.5 * c.top10_pct + 0.25 * c.rat_trader_rate)

        # ── Pending-screen queue ──────────────────────────────────────────
        # The trending list rotates every poll.  Releasing unscreened
        # candidates (the old behaviour) lost them before the per-tick holder
        # budget could reach them: only 4 profiles in 50 min despite 15
        # distinct candidates.  Every Tier0 survivor is kept here until it is
        # screened, fed, rejected, or TTL-expired.
        now = time.time()
        ttl_s = float(self.config.feed_candidate_ttl_seconds or 0.0)
        for mint, c in list(self._pending_screen.items()):
            if ttl_s > 0 and now - float(c.first_discovered or c.first_seen) > ttl_s:
                self._pending_screen.pop(mint, None)
                self._seen.pop(mint, None)
                self.total_dropped_stale += 1
        for c in new_cands:
            self._pending_screen[c.mint] = c

        ordered = sorted(self._pending_screen.values(),
                         key=lambda c: (_proxy(c), -c.open_timestamp, -c.volume))

        # ── Organic holder forensics (Setuh / Padre-style) ────────────────
        # Hard bundle rules enforce; the composite score is logged in shadow
        # until its thresholds are fitted from the outcome loop.
        screened: list[tuple[Candidate, float, bool]] = []
        rejected_mints: set[str] = set()
        screen_fetches = 0
        for cand in ordered:
            if self.config.organic_screen_enabled and screen_fetches >= max(
                    1, int(self.config.organic_screen_max_fetches_per_tick)):
                break
            if self.config.organic_screen_enabled:
                screen_fetches += 1
                try:
                    v = await asyncio.wait_for(
                        self.screen_candidate(cand, creator=cand.creator),
                        # chain head (retried) + GMGN fallback + chain augment
                        timeout=max(90.0, self.config.organic_screen_max_fetches_per_tick * 40.0),
                    )
                except Exception as e:
                    logger.debug(f"[AutoFeed] screen error {cand.mint[:8]}…: {e}")
                    v = None
                if v is not None and getattr(v, "verdict", "") == "reject":
                    self.total_rejected_screen += 1
                    rejected_mints.add(cand.mint)
                    for r in (v.reasons or ["unknown"]):
                        key = r.split("_x")[0] if "_x" in r else r
                        self._screen_reject_tally[key] = \
                            self._screen_reject_tally.get(key, 0) + 1
                    logger.info(
                        f"[AutoFeed] SCREEN REJECT ${cand.symbol} "
                        f"mint={cand.mint[:8]}… score={v.risk_score:.0f} "
                        f"reasons={v.reasons}"
                    )
                    feed_store.record_event(
                        cand.mint, "autofeed", "rejected",
                        reason=",".join(v.reasons or []),
                        symbol=cand.symbol, market_cap=cand.market_cap,
                        risk_score=float(v.risk_score or 0.0),
                        vector={"candidate": cand.to_dict(),
                                "screen": v.metrics, "source": v.source})
                    # Keep it in `_seen` (negative cache) but never forward
                    # it, and never re-queue it for screening.
                    self._pending_screen.pop(cand.mint, None)
                    continue
                score = float(getattr(v, "risk_score", 0.0) or 0.0) if v else 0.0
                unavailable = v is None or getattr(v, "verdict", "") == "unavailable"
                self._screen_scores[cand.mint] = score
                self._pending_screen.pop(cand.mint, None)
                screened.append((cand, score, unavailable))
            else:
                self._pending_screen.pop(cand.mint, None)
                screened.append((cand, 0.0, False))

        # Final rank: screened-and-known first, then holder risk, then the
        # freshest migration, then motion.  An `unavailable` profile must not
        # outrank a verified-clean one just because its score defaults to 0.
        screened.sort(key=lambda t: (t[2], t[1], -t[0].open_timestamp, -t[0].volume))

        cooldown_ok = True
        if self._last_fed_at > 0:
            cooldown_ok = ((time.time() - self._last_fed_at) / 60.0
                           >= self.config.cooldown_after_feed_minutes)

        fed_mints: set[str] = set()
        for cand, score, _unavail in screened:
            if not cooldown_ok:
                break
            if active >= self.config.max_concurrent_feed:
                break
            if len(fed_mints) >= budget:
                break
            if self._forward_fn is None:
                continue
            # Stale-candidate guard: if it waited past its TTL, the pump it was
            # selected for is over.  Do not start a session on a dead chase.
            waited = time.time() - float(cand.first_discovered or cand.first_seen)
            if self.config.feed_candidate_ttl_seconds > 0 and \
                    waited > self.config.feed_candidate_ttl_seconds:
                self.total_dropped_stale += 1
                feed_store.record_event(
                    cand.mint, "autofeed", "dropped_stale",
                    reason=f"waited_{waited:.0f}s", symbol=cand.symbol,
                    market_cap=cand.market_cap, risk_score=score,
                    vector={"candidate": cand.to_dict()})
                self._seen.pop(cand.mint, None)
                continue
            try:
                ok = await self._forward_fn(cand)
            except Exception as e:
                logger.warning(f"[AutoFeed] forward_fn error: {e}")
                self._seen.pop(cand.mint, None)
                continue
            if ok is False:
                # The backend refused / failed to start the session.  Do NOT
                # count it as fed (the old bug: 26 fed vs 7 recordings) and
                # release it so a later tick can retry after the cooldown.
                self.total_fed_not_started += 1
                reason = "not_started"
                self._fed_not_started_tally[reason] = \
                    self._fed_not_started_tally.get(reason, 0) + 1
                feed_store.record_event(
                    cand.mint, "autofeed", "fed_not_started",
                    reason=reason, symbol=cand.symbol, market_cap=cand.market_cap,
                    risk_score=score, vector={"candidate": cand.to_dict()})
                self._seen.pop(cand.mint, None)
                continue
            fed_mints.add(cand.mint)
            self.total_fed += 1
            self._last_fed_at = time.time()
            feed_store.record_event(
                cand.mint, "autofeed", "fed",
                reason=("organic_screen" if self.config.organic_screen_enabled
                        else "gate_only"),
                symbol=cand.symbol, market_cap=cand.market_cap,
                risk_score=score, vector={"candidate": cand.to_dict()})
            logger.info(
                f"[AutoFeed] FED  ${cand.symbol}  mint={cand.mint[:8]}… "
                f"mcap=${cand.market_cap:.0f} liq=${cand.liquidity:.0f} "
                f"smart={cand.smart_degen_count} rug={cand.rug_ratio:.2f} "
                f"bundler={cand.bundler_rate:.2f} insider={cand.insider_rate:.2f} "
                f"risk={score:.0f}"
            )
            active += 1

        # Candidates the concurrency cap / per-tick budget kept out of this
        # tick stay in the pending-screen queue (not dropped): the next tick
        # can feed the best of them.  Screen-rejected mints are never
        # re-queued (the negative verdict is authoritative for the cooldown).
        for cand, _score, _unavail in screened:
            if cand.mint not in fed_mints and cand.mint not in rejected_mints:
                self._pending_screen[cand.mint] = cand

        # Drop stale tracked candidates from `_seen` cache
        if len(self._seen) > 200:
            self._seen = {k: v for k, v in self._seen.items() if v.age_minutes < 60}
        if len(self._screen_scores) > 500:
            self._screen_scores = dict(list(self._screen_scores.items())[-200:])

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
            # Optional extended risk gates (fail-closed repair 2026-11-16)
            "max_dev_team_hold_rate": self.config.max_dev_team_hold_rate,
            "max_top70_sniper_hold_rate": self.config.max_top70_sniper_hold_rate,
            "max_twitter_create_token_count": self.config.max_twitter_create_token_count,
            "reject_cto_flag": self.config.reject_cto_flag,
            "max_buy_sell_tax_pct": self.config.max_buy_sell_tax_pct,
            "require_renounced_mint": self.config.require_renounced_mint,
            "require_renounced_freeze": self.config.require_renounced_freeze,
            "reject_wash_trading": self.config.reject_wash_trading,
            "reject_honeypot": self.config.reject_honeypot,
            "require_has_social": self.config.require_has_social,
            "max_created_age": self.config.max_created_age,
            "min_migrated_age_minutes": self.config.min_migrated_age_minutes,
            "max_migrated_age_minutes": self.config.max_migrated_age_minutes,
            "max_price_change_1h_pct": self.config.max_price_change_1h_pct,
            "max_concurrent_feed": self.config.max_concurrent_feed,
            "max_feeds_per_tick": self.config.max_feeds_per_tick,
            "feed_candidate_ttl_seconds": self.config.feed_candidate_ttl_seconds,
            "cooldown_after_feed_minutes": self.config.cooldown_after_feed_minutes,
            # Graduation-frontier mcap (the live floor target), for the UI.
            "grad_mcap_usd": (
                GRAD_MCAP_USD_PER_SOL * float((self._market_conditions or {})["sol_usd"])
                if (self._market_conditions or {}).get("sol_usd") else None),
            # Organic holder forensics (Setuh / Padre-style bundle checks)
            "organic_screen": self._screen.snapshot(),
            "total_rejected_screen": self.total_rejected_screen,
            "total_fed_not_started": self.total_fed_not_started,
            "total_dropped_stale": self.total_dropped_stale,
            "screen_reject_tally": dict(self._screen_reject_tally),
            "fed_not_started_tally": dict(self._fed_not_started_tally),
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
            # Shape/health telemetry (fail-closed repair 2026-11-16)
            "last_shape_report": self._shape_report,
            "shape_totals": dict(self._shape_totals),
            "reject_tally_total": dict(self._reject_tally_total),
        }
