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
        self.last_error: str = ""
        self.last_poll_at: float = 0.0
        self.total_seen: int = 0
        self.total_fed: int = 0

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

    # ── gmgn-cli invocation ──────────────────────────────────────────────────

    def _build_cli_args(self) -> list[str]:
        """Build the gmgn-cli `market trending` argv. Run server-side filters.

        Only flags that the gmgn-cli actually accepts are sent. Risk metrics
        not exposed as server-side flags (rug_ratio, rat_trader_amount_rate,
        bot_degen_rate) are filtered locally in `_to_candidate` instead.
        """
        cf = self.config
        args = ["market", "trending", "--chain", "sol", "--raw"]

        args += ["--interval", cf.interval or DEFAULT_INTERVAL]
        # OP: "trending coins with lots of motion" — rank by USD trading volume
        # ensures the top results are the most actively traded right now.
        args += ["--order-by", cf.order_by or "volume", "--direction", "desc"]
        args += ["--limit", "100"]

        # Server-side numeric range filters (supported by gmgn-cli).
        args += ["--min-marketcap", str(int(cf.min_mcap_usd))]
        args += ["--max-marketcap", str(int(cf.max_mcap_usd))]
        args += ["--min-liquidity", str(int(cf.min_liquidity_usd))]
        args += ["--min-holder-count", str(int(cf.min_holders))]
        args += ["--min-smart-degen-count", str(int(cf.min_smart_degen_count))]
        # Motion gates (OP: "lots of motion - trending coins"):
        args += ["--min-volume", str(int(cf.min_volume_usd))]
        args += ["--min-swaps", str(int(cf.min_swaps))]
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
        if mcap < cf.min_mcap_usd or mcap > cf.max_mcap_usd:
            return None
        if liq_usd > 0 and liq_usd < cf.min_liquidity_usd:
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
        # Enforce motion locally too (defense in depth):
        if vol > 0 and vol < cf.min_volume_usd: return None
        if swaps > 0 and swaps < cf.min_swaps:  return None

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

    def _excluded_set(self) -> set[str]:
        raw = (self.config.exclude_mints or "").strip()
        return {x.strip().lower() for x in raw.split(",") if x.strip()}

    # ── Polling loop ────────────────────────────────────────────────────────

    async def _loop(self):
        logger.info("[AutoFeed] Poll loop entered")
        while not self._stop_evt.is_set():
            try:
                await self._tick()
            except Exception as e:
                logger.error(f"[AutoFeed] tick error: {e}", exc_info=True)
                self.last_error = f"tick error: {e}"
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
        for row in rows:
            cand = self._to_candidate(row)
            if cand is None:
                continue
            total_parsed += 1
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
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "total_seen": self.total_seen,
            "total_fed":  self.total_fed,
            "active_tracked": len(self._seen),
            "recent_candidates": [c.to_dict() for c in list(self._seen.values())[-10:]],
        }
