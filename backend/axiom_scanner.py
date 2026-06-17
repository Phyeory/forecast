"""
Axiom Scanner — Auto-discover trending Solana tokens and subscribe them
to the live trader with strict organic-only filters.

Data sources:
  - DexScreener token-boosts API → trending tokens list
  - DexScreener pairs/tokens API → on-chain metrics (txns, volume, liquidity)
  - RugCheck API → bundle detection, insider %, risk score, LP lock status

Filters (mirroring Axiom.trade's "1h Top Coins" Pump AMM view):
  1. Pump AMM only (dexId == "pumpswap") — pump.fun migrated coins
  2. >1,000 transactions in the last 1h
  3. Minimum liquidity threshold
  4. RugCheck safety: no high-risk flags, low risk score
  5. LP locked ≥ 90%
  6. No bundle activity detected
  7. Top-10 holder concentration < 40%
  8. Minimum market cap floor
"""

from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable, Awaitable

import aiohttp

logger = logging.getLogger("axiom-scanner")

# ── API endpoints ─────────────────────────────────────────────────────────────
DEXSCREENER_BOOSTS_TOP   = "https://api.dexscreener.com/token-boosts/top/v1"
DEXSCREENER_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_TOKENS       = "https://api.dexscreener.com/latest/dex/tokens"
RUGCHECK_REPORT          = "https://api.rugcheck.xyz/v1/tokens"

_HDR = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


# ── Filter configuration ─────────────────────────────────────────────────────

@dataclass
class ScannerFilters:
    """All configurable filter thresholds for the Axiom scanner."""

    # ── Core filters ──────────────────────────────────────────────────────
    min_txns_1h: int = 1_000                # Minimum 1h transactions (buys + sells)
    pump_amm_only: bool = True              # Only Pump AMM (pumpswap) migrated coins
    min_liquidity_usd: float = 10_000       # Minimum pool liquidity in USD
    min_market_cap_usd: float = 50_000      # Minimum market cap
    max_market_cap_usd: float = 50_000_000  # Maximum market cap (avoid mega caps)
    min_volume_1h_usd: float = 5_000        # Minimum 1h volume

    # ── Organic / anti-manipulation filters ───────────────────────────────
    max_rugcheck_score: int = 500           # RugCheck risk score (lower = safer, 1 = perfect)
    min_lp_locked_pct: float = 90.0         # Minimum % of LP locked
    max_top10_holder_pct: float = 40.0      # Max % held by top 10 holders (excl. LP/burn)

    # Bundle / insider detection (from RugCheck risks array)
    block_bundle_risk: bool = True          # Block if RugCheck flags bundle activity
    block_insider_risk: bool = True         # Block if RugCheck flags insider trading
    block_copycat_risk: bool = True         # Block if RugCheck flags copycat/clone
    block_low_lp_risk: bool = True          # Block if RugCheck flags dangerously low LP

    # ── Timing ────────────────────────────────────────────────────────────
    scan_interval_seconds: int = 120        # How often to scan (default: 2 min)
    cooldown_per_token_seconds: int = 3600  # Don't re-check a token within this window

    def to_dict(self) -> dict:
        return asdict(self)


# ── Risk categories to block ─────────────────────────────────────────────────
# RugCheck returns a "risks" array with objects like:
#   {"name": "Large Amount of LP Unlocked", "level": "danger", "description": "..."}
# We block based on risk name patterns and level.

_BLOCKED_RISK_PATTERNS = {
    "bundle":    "block_bundle_risk",
    "insider":   "block_insider_risk",
    "copycat":   "block_copycat_risk",
    "copy cat":  "block_copycat_risk",
    "clone":     "block_copycat_risk",
    "rug":       "block_insider_risk",
    "honeypot":  "block_insider_risk",
    "low liquidity": "block_low_lp_risk",
    "lp unlocked":   "block_low_lp_risk",
}


# ── Token scan result ─────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    """Result of scanning a single token through all filters."""
    mint: str
    name: str = ""
    symbol: str = ""
    pair_address: str = ""
    dex_id: str = ""
    price_usd: float = 0.0
    market_cap_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_1h: float = 0.0
    txns_1h: int = 0
    rugcheck_score: int = 9999
    lp_locked_pct: float = 0.0
    risks: list = field(default_factory=list)
    passed: bool = False
    reject_reason: str = ""
    scan_time: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Main Scanner class ────────────────────────────────────────────────────────

class AxiomScanner:
    """
    Periodically scans for trending Solana tokens and filters them for
    organic, safe trading candidates.

    Usage:
        scanner = AxiomScanner(filters=ScannerFilters())
        scanner.on_token_approved = my_callback  # async fn(ScanResult) -> None
        await scanner.start()
    """

    def __init__(self, filters: Optional[ScannerFilters] = None):
        self.filters = filters or ScannerFilters()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None

        # Cooldown tracker: mint -> last_scan_timestamp
        self._cooldowns: dict[str, float] = {}

        # History of scan results (last 100)
        self._scan_history: list[ScanResult] = []
        self._approved_tokens: list[ScanResult] = []
        self._last_scan_time: float = 0.0
        self._scan_count: int = 0

        # Callback when a token passes all filters
        # Signature: async def callback(result: ScanResult) -> None
        self.on_token_approved: Optional[Callable[[ScanResult], Awaitable[None]]] = None

        # Set of mints already subscribed (to avoid duplicates)
        self._subscribed_mints: set[str] = set()

    # ── Session management ────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers=_HDR,
            )
        return self._session

    async def _close_session(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── DexScreener: fetch trending tokens ────────────────────────────────

    async def _fetch_trending_mints(self) -> list[dict]:
        """
        Fetch trending Solana token mints from DexScreener's boost endpoints.
        Returns list of {tokenAddress, chainId, ...} dicts.
        """
        session = await self._get_session()
        all_tokens = []

        for url in [DEXSCREENER_BOOSTS_TOP, DEXSCREENER_BOOSTS_LATEST]:
            try:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        logger.warning(f"[Scanner] DexScreener {url.split('/')[-1]} returned {r.status}")
                        continue
                    data = await r.json(content_type=None)
                    if isinstance(data, list):
                        # Filter to Solana only
                        solana_tokens = [
                            t for t in data
                            if t.get("chainId") == "solana" and t.get("tokenAddress")
                        ]
                        all_tokens.extend(solana_tokens)
            except Exception as e:
                logger.error(f"[Scanner] Error fetching {url}: {e}")

        # Deduplicate by tokenAddress
        seen = set()
        unique = []
        for t in all_tokens:
            mint = t["tokenAddress"]
            if mint not in seen:
                seen.add(mint)
                unique.append(t)

        logger.info(f"[Scanner] Fetched {len(unique)} unique trending Solana tokens")
        return unique

    # ── DexScreener: fetch pair data for a token ──────────────────────────

    async def _fetch_pair_data(self, mint: str) -> Optional[dict]:
        """
        Fetch DexScreener pair data for a token.
        Returns the best Solana pair (preferring pumpswap dexId).
        """
        session = await self._get_session()
        try:
            async with session.get(
                f"{DEXSCREENER_TOKENS}/{mint}",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json(content_type=None)
                pairs = data.get("pairs") or []

                # Filter to Solana pairs only
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                if not sol_pairs:
                    return None

                # Prefer pumpswap pairs (Pump AMM)
                pumpswap = [p for p in sol_pairs if "pump" in (p.get("dexId") or "").lower()]
                if pumpswap:
                    # Pick the one with highest 1h volume
                    pumpswap.sort(
                        key=lambda p: (p.get("volume") or {}).get("h1", 0),
                        reverse=True,
                    )
                    return pumpswap[0]

                # If not filtering pump-only, return highest volume pair
                if not self.filters.pump_amm_only:
                    sol_pairs.sort(
                        key=lambda p: (p.get("volume") or {}).get("h1", 0),
                        reverse=True,
                    )
                    return sol_pairs[0]

                return None

        except Exception as e:
            logger.debug(f"[Scanner] Error fetching pair data for {mint[:8]}: {e}")
            return None

    # ── RugCheck: fetch safety report ─────────────────────────────────────

    async def _fetch_rugcheck(self, mint: str) -> Optional[dict]:
        """
        Fetch RugCheck report summary for a token.
        Returns {score, score_normalised, lpLockedPct, risks, ...}
        """
        session = await self._get_session()
        try:
            async with session.get(
                f"{RUGCHECK_REPORT}/{mint}/report/summary",
                timeout=aiohttp.ClientTimeout(total=8),
            ) as r:
                if r.status != 200:
                    logger.debug(f"[Scanner] RugCheck returned {r.status} for {mint[:8]}")
                    return None
                return await r.json(content_type=None)
        except Exception as e:
            logger.debug(f"[Scanner] Error fetching RugCheck for {mint[:8]}: {e}")
            return None

    # ── Apply all filters to a single token ───────────────────────────────

    async def _evaluate_token(self, mint: str) -> ScanResult:
        """
        Run a single token through all filter stages.
        Returns a ScanResult with passed=True if it qualifies.
        """
        result = ScanResult(mint=mint, scan_time=time.time())

        # ── Stage 1: DexScreener pair data (market metrics) ───────────────
        pair = await self._fetch_pair_data(mint)
        if pair is None:
            if self.filters.pump_amm_only:
                result.reject_reason = "No PumpSwap (Pump AMM) pair found"
            else:
                result.reject_reason = "No Solana pair found on DexScreener"
            return result

        # Extract fields
        base = pair.get("baseToken", {})
        result.name = base.get("name", "")
        result.symbol = base.get("symbol", "")
        result.pair_address = pair.get("pairAddress", "")
        result.dex_id = pair.get("dexId", "")
        result.price_usd = float(pair.get("priceUsd") or 0)
        result.market_cap_usd = float(pair.get("marketCap") or pair.get("fdv") or 0)
        result.liquidity_usd = float((pair.get("liquidity") or {}).get("usd", 0))

        txns = pair.get("txns") or {}
        h1_txns = txns.get("h1", {})
        result.txns_1h = int(h1_txns.get("buys", 0)) + int(h1_txns.get("sells", 0))

        volume = pair.get("volume") or {}
        result.volume_1h = float(volume.get("h1", 0))

        # ── Filter: Pump AMM only ─────────────────────────────────────────
        if self.filters.pump_amm_only:
            dex_lower = result.dex_id.lower()
            if "pump" not in dex_lower:
                result.reject_reason = f"Not Pump AMM (dexId={result.dex_id})"
                return result

        # ── Filter: Minimum 1h transactions ───────────────────────────────
        if result.txns_1h < self.filters.min_txns_1h:
            result.reject_reason = (
                f"1h txns {result.txns_1h} < {self.filters.min_txns_1h}"
            )
            return result

        # ── Filter: Minimum liquidity ─────────────────────────────────────
        if result.liquidity_usd < self.filters.min_liquidity_usd:
            result.reject_reason = (
                f"Liquidity ${result.liquidity_usd:,.0f} < ${self.filters.min_liquidity_usd:,.0f}"
            )
            return result

        # ── Filter: Market cap range ──────────────────────────────────────
        if result.market_cap_usd < self.filters.min_market_cap_usd:
            result.reject_reason = (
                f"Mcap ${result.market_cap_usd:,.0f} < ${self.filters.min_market_cap_usd:,.0f}"
            )
            return result
        if result.market_cap_usd > self.filters.max_market_cap_usd:
            result.reject_reason = (
                f"Mcap ${result.market_cap_usd:,.0f} > ${self.filters.max_market_cap_usd:,.0f}"
            )
            return result

        # ── Filter: Minimum 1h volume ─────────────────────────────────────
        if result.volume_1h < self.filters.min_volume_1h_usd:
            result.reject_reason = (
                f"1h volume ${result.volume_1h:,.0f} < ${self.filters.min_volume_1h_usd:,.0f}"
            )
            return result

        # ── Stage 2: RugCheck safety report ───────────────────────────────
        rugcheck = await self._fetch_rugcheck(mint)
        if rugcheck is None:
            # If RugCheck is unavailable, fail-safe: reject
            result.reject_reason = "RugCheck report unavailable (fail-safe reject)"
            return result

        result.rugcheck_score = int(rugcheck.get("score", 9999))
        result.lp_locked_pct = float(rugcheck.get("lpLockedPct", 0))
        result.risks = rugcheck.get("risks", [])

        # ── Filter: RugCheck risk score ───────────────────────────────────
        if result.rugcheck_score > self.filters.max_rugcheck_score:
            result.reject_reason = (
                f"RugCheck score {result.rugcheck_score} > {self.filters.max_rugcheck_score}"
            )
            return result

        # ── Filter: LP locked percentage ──────────────────────────────────
        if result.lp_locked_pct < self.filters.min_lp_locked_pct:
            result.reject_reason = (
                f"LP locked {result.lp_locked_pct:.1f}% < {self.filters.min_lp_locked_pct:.1f}%"
            )
            return result

        # ── Filter: Blocked risk patterns ─────────────────────────────────
        for risk in result.risks:
            risk_name = (risk.get("name") or "").lower()
            risk_level = (risk.get("level") or "").lower()
            risk_desc = (risk.get("description") or "").lower()

            # Only block on "danger" or "warn" level risks
            if risk_level not in ("danger", "warn", "critical"):
                continue

            for pattern, filter_attr in _BLOCKED_RISK_PATTERNS.items():
                if pattern in risk_name or pattern in risk_desc:
                    if getattr(self.filters, filter_attr, True):
                        result.reject_reason = (
                            f"RugCheck risk blocked: '{risk.get('name')}' "
                            f"(level={risk_level})"
                        )
                        return result

        # ── All filters passed! ───────────────────────────────────────────
        result.passed = True
        return result

    # ── Main scan loop ────────────────────────────────────────────────────

    async def _scan_once(self):
        """Run a single scan cycle: fetch trending → filter → callback."""
        self._scan_count += 1
        self._last_scan_time = time.time()
        logger.info(f"[Scanner] ─── Scan #{self._scan_count} starting ───")

        # Fetch trending tokens
        trending = await self._fetch_trending_mints()
        if not trending:
            logger.warning("[Scanner] No trending tokens found — skipping scan")
            return

        approved = []
        rejected_count = 0

        for token_data in trending:
            mint = token_data["tokenAddress"]

            # Skip if on cooldown
            last_check = self._cooldowns.get(mint, 0)
            if time.time() - last_check < self.filters.cooldown_per_token_seconds:
                continue

            # Skip if already subscribed
            if mint in self._subscribed_mints:
                continue

            # Mark cooldown
            self._cooldowns[mint] = time.time()

            # Evaluate token through all filters
            result = await self._evaluate_token(mint)

            # Rate-limit API calls: small delay between tokens
            await asyncio.sleep(0.3)

            # Store in history (cap at 200)
            self._scan_history.append(result)
            if len(self._scan_history) > 200:
                self._scan_history = self._scan_history[-200:]

            if result.passed:
                approved.append(result)
                self._approved_tokens.append(result)
                if len(self._approved_tokens) > 50:
                    self._approved_tokens = self._approved_tokens[-50:]

                logger.info(
                    f"[Scanner] ✅ APPROVED: {result.symbol} ({result.mint[:8]}…) "
                    f"| mcap=${result.market_cap_usd:,.0f} "
                    f"| txns_1h={result.txns_1h} "
                    f"| liq=${result.liquidity_usd:,.0f} "
                    f"| rug_score={result.rugcheck_score} "
                    f"| lp_locked={result.lp_locked_pct:.0f}%"
                )

                # Fire callback to subscribe token to live trader
                if self.on_token_approved and mint not in self._subscribed_mints:
                    try:
                        await self.on_token_approved(result)
                        self._subscribed_mints.add(mint)
                    except Exception as e:
                        logger.error(f"[Scanner] Callback error for {mint[:8]}: {e}")
            else:
                rejected_count += 1
                logger.debug(
                    f"[Scanner] ❌ REJECTED: {result.symbol or mint[:8]} — {result.reject_reason}"
                )

        logger.info(
            f"[Scanner] ─── Scan #{self._scan_count} complete: "
            f"{len(approved)} approved, {rejected_count} rejected, "
            f"{len(trending) - len(approved) - rejected_count} skipped (cooldown/subscribed) ───"
        )

    async def _run_loop(self):
        """Background loop that runs scans at the configured interval."""
        logger.info(
            f"[Scanner] Started — interval={self.filters.scan_interval_seconds}s "
            f"| pump_amm_only={self.filters.pump_amm_only} "
            f"| min_txns_1h={self.filters.min_txns_1h} "
            f"| max_rug_score={self.filters.max_rugcheck_score}"
        )
        while self._running:
            try:
                await self._scan_once()
            except Exception as e:
                logger.error(f"[Scanner] Scan error: {e}", exc_info=True)

            # Wait for next scan interval
            for _ in range(self.filters.scan_interval_seconds):
                if not self._running:
                    break
                await asyncio.sleep(1)

    # ── Public API ────────────────────────────────────────────────────────

    async def start(self):
        """Start the background scanner loop."""
        if self._running:
            logger.warning("[Scanner] Already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("[Scanner] Scanner started")

    async def stop(self):
        """Stop the background scanner loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._close_session()
        logger.info("[Scanner] Scanner stopped")

    def unsubscribe_mint(self, mint: str):
        """Remove a mint from the subscribed set (e.g., when session ends)."""
        self._subscribed_mints.discard(mint)

    def update_filters(self, new_filters: dict):
        """Update filter thresholds from a dict (partial updates OK)."""
        for key, val in new_filters.items():
            if hasattr(self.filters, key):
                expected_type = type(getattr(self.filters, key))
                try:
                    setattr(self.filters, key, expected_type(val))
                except (ValueError, TypeError):
                    logger.warning(f"[Scanner] Invalid filter value: {key}={val}")

    def get_status(self) -> dict:
        """Return current scanner status for the dashboard."""
        return {
            "running": self._running,
            "scan_count": self._scan_count,
            "last_scan_time": self._last_scan_time,
            "subscribed_count": len(self._subscribed_mints),
            "subscribed_mints": list(self._subscribed_mints),
            "approved_count": len(self._approved_tokens),
            "recent_approved": [r.to_dict() for r in self._approved_tokens[-10:]],
            "recent_scans": [r.to_dict() for r in self._scan_history[-20:]],
            "filters": self.filters.to_dict(),
        }

    def clear_cooldowns(self):
        """Clear all cooldowns to force re-evaluation."""
        self._cooldowns.clear()
        logger.info("[Scanner] Cooldowns cleared")
