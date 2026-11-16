"""
Feed sources — GeckoTerminal discovery + multi-source vetting for the AutoFeed
(new-feed branch, 2026-11-16).

Replaces the gmgn-cli trending poll as the DEFAULT discovery source with a
stack that is free, keyless, and — critically — FAIL-CLOSED end to end:

    GeckoTerminal /networks/solana/new_pools   (PumpSwap migrations, per-pool
                                                motion incl. unique buyers)
      → DexScreener batch enrichment (GT leaves fdv/mcap null on fresh
        PumpSwap pools; DexScreener supplies mcap/liquidity/pair age)
      → motion + age gates (pure functions, unit-testable)
      → VettingClient:
          • Solana RPC (shared process-wide budget, mirrors pumpfun_client's
            pacing): mint/freeze authority (exact renounced) +
            getTokenLargestAccounts ⇒ REAL top-10/20 supply concentration —
            the direct measure of "bundled", versus GMGN's launch-time proxy
            rate which manipulators split across wallets to defeat
          • RugCheck report (free, keyless, best-effort, paced + cached):
            score, risks, rugged flag, creator serial-launch counts,
            insider-network graphs
      → composite risk score (ranking) + hard gates (fail-closed)

Fail-closed policy: a candidate whose REQUIRED security vector (RPC) is
unavailable is rejected (`vetting_fail_policy="reject"`, the default) —
ABSENT must never again mean PASS.  RugCheck is optional/best-effort: when
unavailable the score gate degrades to not-applied and the gap is recorded.

The repaired gmgn-cli path stays available as `feed_source="gmgn"` fallback
(autofeed.py).  AutoFeed performs NO trading on either path (unchanged
design contract).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp

import pumpfun_client as _pfc
from pumpfun_client import get_ds_pairs_by_mints

logger = logging.getLogger("feed_sources")

# ── Endpoint / pacing constants ──────────────────────────────────────────────

GECKO_NEW_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
GECKO_TRENDING_URL = "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools"
GECKO_MIN_INTERVAL = 2.5          # free tier ≈ 30 req/min; poll uses ~4-6 calls/min
RUGCHECK_REPORT_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
RUGCHECK_MIN_INTERVAL = 6.0       # report GENERATION ceiling ~15/min (2026-09 study)
RUGCHECK_TTL = 600.0              # cached verdicts
VET_RPC_TIMEOUT = 10.0

# GT dex slugs observed on solana (probe 2026-11-16): pump-fun (bonding-curve
# pools), pumpswap (migrations), meteora-dbc, meteora-damm-v2, stonkfun.
# PumpSwap is THE pump.fun migration venue; the allow-list is config-driven.


# ── GeckoTerminal pool row ───────────────────────────────────────────────────

@dataclass
class GeckoPool:
    mint: str
    pool_address: str
    dex: str
    name: str = ""
    created_at_unix: float = 0.0
    age_seconds: float = 0.0
    reserve_in_usd: float = 0.0
    fdv_usd: Optional[float] = None
    mcap_usd: Optional[float] = None
    base_price_usd: float = 0.0
    vol_h1: float = 0.0
    vol_h24: float = 0.0
    txns_h1_buys: int = 0
    txns_h1_sells: int = 0
    txns_h1_buyers: int = 0
    txns_h1_sellers: int = 0
    txns_h1_unique: int = 0
    price_change_h1: float = 0.0

    @property
    def wash_proxy_usd(self) -> float:
        """USD volume per unique h1 buyer — extreme values scream wash tape."""
        buyers = max(self.txns_h1_buyers, 1)
        return self.vol_h1 / buyers


def parse_iso_ts(s: str) -> float:
    """GT `pool_created_at` ('2026-11-16T15:34:29Z', sometimes with .fff) → unix."""
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _fnum(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _inum(v) -> int:
    try:
        return 0 if v is None else int(v)
    except (TypeError, ValueError):
        return 0


def parse_gecko_pool(node: dict) -> Optional[GeckoPool]:
    """GT pool node → GeckoPool.  Mint from the base_token relationship id
    (`solana_<mint>`); None when the node is unusable."""
    attrs = node.get("attributes") or {}
    rel = node.get("relationships") or {}
    pool_id = str(node.get("id") or "")
    if not pool_id.startswith("solana_"):
        return None
    pool_address = pool_id.split("_", 1)[1]
    base_rel = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
    mint = base_rel.split("_", 1)[1] if base_rel.startswith("solana_") else ""
    if not mint or len(mint) < 32 or len(mint) > 44:
        return None
    dex = (((rel.get("dex") or {}).get("data") or {}).get("id") or "").strip()
    created = parse_iso_ts(attrs.get("pool_created_at") or "")
    now = time.time()
    vol = attrs.get("volume_usd") or {}
    txns = (attrs.get("transactions") or {}).get("h1") or {}
    h1 = txns if isinstance(txns, dict) else {}
    return GeckoPool(
        mint=mint,
        pool_address=pool_address,
        dex=dex,
        name=(attrs.get("name") or "").strip(),
        created_at_unix=created,
        age_seconds=max(0.0, now - created) if created else 0.0,
        reserve_in_usd=_fnum(attrs.get("reserve_in_usd")) or 0.0,
        fdv_usd=_fnum(attrs.get("fdv_usd")),
        mcap_usd=_fnum(attrs.get("market_cap_usd")),
        base_price_usd=_fnum(attrs.get("base_token_price_usd")) or 0.0,
        vol_h1=_fnum(vol.get("h1")) or 0.0,
        vol_h24=_fnum(vol.get("h24")) or 0.0,
        txns_h1_buys=_inum(h1.get("buys")),
        txns_h1_sells=_inum(h1.get("sells")),
        txns_h1_buyers=_inum(h1.get("buyers")),
        txns_h1_sellers=_inum(h1.get("sellers")),
        txns_h1_unique=_inum(h1.get("unique_traders")),
        price_change_h1=_fnum((attrs.get("price_change_percentage") or {}).get("h1")) or 0.0,
    )


# ── Motion gates (pure) ──────────────────────────────────────────────────────

def filter_gecko_motion(
    pools: list[GeckoPool],
    *,
    dex_allowlist: set[str],
    min_age_s: float,
    max_age_s: float,          # 0 = no cap
    min_liquidity_usd: float,
    min_volume_usd: float,     # h1 window
    min_swaps: int,            # h1 buys+sells
    min_buyers_h1: int,
    max_volume_per_buyer_usd: float,   # 0 = disabled
    reject_tally: Optional[dict] = None,
) -> tuple[list[GeckoPool], list[GeckoPool]]:
    """Split pools into (passing, rejected).  Rejects carry reasons via tally.

    Fail-closed on missing data: pools without a usable h1 window (all-zero
    txns AND volume) cannot prove motion and are rejected when the gates
    demand motion.
    """
    passing: list[GeckoPool] = []
    rejected: list[GeckoPool] = []

    def rej(p: GeckoPool, reason: str) -> None:
        rejected.append(p)
        if reject_tally is not None:
            reject_tally[reason] = reject_tally.get(reason, 0) + 1

    for p in pools:
        if dex_allowlist and p.dex.lower() not in dex_allowlist:
            rej(p, "dex_not_allowed"); continue
        if min_age_s > 0 and p.age_seconds < min_age_s:
            rej(p, "min_pool_age"); continue
        if max_age_s > 0 and p.age_seconds > max_age_s:
            rej(p, "max_pool_age"); continue
        if p.reserve_in_usd < min_liquidity_usd:
            rej(p, "min_liquidity_usd"); continue
        if p.vol_h1 < min_volume_usd:
            rej(p, "min_volume_usd"); continue
        if (p.txns_h1_buys + p.txns_h1_sells) < min_swaps:
            rej(p, "min_swaps"); continue
        if p.txns_h1_buyers < min_buyers_h1:
            rej(p, "min_buyers_h1"); continue
        if max_volume_per_buyer_usd > 0 and p.wash_proxy_usd > max_volume_per_buyer_usd:
            rej(p, "max_volume_per_buyer_usd"); continue
        passing.append(p)
    return passing, rejected


# ── DexScreener enrichment (pure mapping) ────────────────────────────────────

def ds_enrich_mint(pairs: Optional[list[dict]], now: Optional[float] = None) -> dict:
    """DexScreener pairs for a mint → {mcap_usd, liquidity_usd, pair_age_s}.

    Picks the highest-liquidity pair (the PumpSwap migration pool dominates).
    Empty/absent pairs → all-None: caller decides (fail-closed).
    """
    now = now if now is not None else time.time()
    best = None
    best_liq = -1.0
    for p in pairs or []:
        try:
            liq = float((((p.get("liquidity") or {}).get("usd")) or 0))
        except (TypeError, ValueError):
            liq = 0.0
        if liq > best_liq:
            best_liq = liq
            best = p
    if not best:
        return {"mcap_usd": None, "liquidity_usd": None, "pair_age_s": None}
    fd = best.get("fdv") or None
    mc = best.get("marketCap") or None
    created_ms = best.get("pairCreatedAt")
    age = (now - created_ms / 1000.0) if isinstance(created_ms, (int, float)) and created_ms > 0 else None
    return {
        "mcap_usd": _fnum(mc) if mc is not None else _fnum(fd),
        "liquidity_usd": best_liq,
        "pair_age_s": max(0.0, age) if age is not None else None,
    }


# ── Vetting result ───────────────────────────────────────────────────────────

@dataclass
class VettingResult:
    mint: str
    rpc_ok: bool = False
    renounced_mint: Optional[bool] = None      # None = unknown (RPC failed)
    renounced_freeze: Optional[bool] = None
    supply: float = 0.0
    top10_share: Optional[float] = None        # REAL supply concentration
    top20_share: Optional[float] = None
    rc_ok: bool = False                        # RugCheck reachable
    rugcheck_score: Optional[float] = None     # score_normalised 0-100
    rugcheck_rugged: bool = False
    rugcheck_risks: list[str] = field(default_factory=list)
    creator_token_count: Optional[int] = None  # serial-launcher signal
    insider_graphs: bool = False               # RugCheck insider network detected
    detail: dict = field(default_factory=dict)


def composite_risk_score(v: VettingResult, top10_soft: float = 0.30) -> float:
    """0.0 = clean … higher = riskier.  Pure, config-free weights documented
    inline; gates stay hard floors — this orders survivors for ranking and
    gives the persistence layer a single comparable number."""
    s = 0.0
    if v.top10_share is not None:
        # concentration beyond the soft threshold, normalized to 0..1 → 0..2.5
        s += 2.5 * max(0.0, v.top10_share - top10_soft) / max(1e-9, (1.0 - top10_soft))
    if v.rugcheck_score is not None:
        # lenient: below 20 free, linear to 1.5 at 100
        s += 1.5 * min(max(v.rugcheck_score - 20.0, 0.0) / 80.0, 1.0)
    if v.rugcheck_rugged:
        s += 2.0
    if v.insider_graphs:
        s += 1.0
    return round(min(s, 8.0), 3)


# ── RPC (shares pumpfun_client's process-wide pacing) ────────────────────────

# Endpoint fanout for vetting reads (2026-11-16): publicnode 429s
# getTokenLargestAccounts under the shared IP budget even with pacing —
# mirror live_trader.SOLANA_RPCS ordering so vetting degrades to the next
# endpoint instead of failing the candidate.
VET_RPC_ENDPOINTS = [
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
    "https://api.tatum.io/v3/blockchain/node/solana-mainnet",
]


async def _rpc_call(session: aiohttp.ClientSession, method: str, params: list) -> dict:
    """Solana JSON-RPC with the SAME global semaphore/min-interval discipline
    as pumpfun_client's `_rpc` — one process-wide budget — plus endpoint
    fanout on 429/error."""
    import pumpfun_client as pfc  # module attrs for the shared mutable state
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    last_err: Optional[Exception] = None
    for url in VET_RPC_ENDPOINTS:
        for _attempt in range(2):     # 2 tries per endpoint before fanout
            async with pfc._get_rpc_semaphore():
                async with pfc._get_rpc_lock():
                    now = time.time()
                    wait = pfc._RPC_MIN_INTERVAL - (now - pfc._rpc_last_call_ts)
                    if wait > 0:
                        await asyncio.sleep(wait)
                    pfc._rpc_last_call_ts = time.time()
                try:
                    async with session.post(
                        url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=VET_RPC_TIMEOUT),
                    ) as r:
                        if r.status == 429:
                            retry_after = float(r.headers.get("Retry-After", 2.0))
                            last_err = RuntimeError(f"429 @ {url.split('//')[1][:24]}")
                            await asyncio.sleep(min(retry_after, 8.0))
                            continue
                        r.raise_for_status()
                        data = await r.json(content_type=None)
                        if "error" in data:
                            raise RuntimeError(f"RPC {method}: {data['error']}")
                        return data["result"]
                except Exception as e:
                    last_err = e
                    await asyncio.sleep(0.5)
        logger.debug(f"[FeedSources] RPC {method} failed on {url}: {last_err}")
    raise last_err or RuntimeError(f"RPC {method} failed on all endpoints")


def parse_mint_account(info: dict) -> dict:
    """getAccountInfo(jsonParsed) value for a SPL mint → authority/supply map."""
    parsed = ((info or {}).get("data") or {})
    if isinstance(parsed, dict):
        parsed = parsed.get("parsed") or {}
    info_d = (parsed or {}).get("info") or {}
    return {
        "mint_authority": info_d.get("mintAuthority"),
        "freeze_authority": info_d.get("freezeAuthority"),
        "supply": float(info_d.get("supply") or 0) / (10 ** int(info_d.get("decimals") or 0)),
    }


def parse_largest_accounts(value: list) -> list[float]:
    """getTokenLargestAccounts result.value → list of uiAmounts (desc)."""
    out = []
    for a in value or []:
        try:
            out.append(float(a.get("uiAmount") or 0))
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def concentration_shares(supply: float, amounts: list[float]) -> tuple[Optional[float], Optional[float]]:
    """(top10_share, top20_share) of total supply.  None when inputs unusable."""
    if supply <= 0 or not amounts:
        return None, None
    top10 = sum(amounts[:10]) / supply
    top20 = sum(amounts[:20]) / supply
    return round(top10, 4), round(top20, 4)


# ── VettingClient ────────────────────────────────────────────────────────────

class VettingClient:
    """Per-candidate security vetting: RPC (required) + RugCheck (optional).

    One instance per AutoFeed; pacing state is instance-local, RPC budget is
    process-global (shared with the trading stack via pumpfun_client).
    """

    def __init__(self):
        self._rc_cache: dict[str, tuple[float, Optional[dict]]] = {}
        self._last_rc_at: float = 0.0
        self._last_gt_at: float = 0.0
        self._pace_lock: Optional[asyncio.Lock] = None

    def _get_pace_lock(self) -> asyncio.Lock:
        if self._pace_lock is None:
            self._pace_lock = asyncio.Lock()
        return self._pace_lock

    async def _pace(self, attr: str, min_interval: float) -> None:
        last = getattr(self, attr)
        wait = min_interval - (time.time() - last)
        if wait > 0:
            await asyncio.sleep(wait)
        setattr(self, attr, time.time())

    async def _pace_slot(self, attr: str, min_interval: float) -> None:
        """Reserve the next free pacing slot (monotonic) and sleep until it.

        Unlike serial `_pace`, N concurrent callers wait on SHARED slots so
        N vetting calls complete in ~ceil(N/concurrency)×interval wall time
        instead of N×interval — a 12-candidate vet no longer exceeds the
        60 s poll cadence."""
        async with self._get_pace_lock():
            now = time.time()
            slot = max(now, getattr(self, attr))
            setattr(self, attr, slot + min_interval)
        wait = slot - time.time()
        if wait > 0:
            await asyncio.sleep(wait)

    # -- GeckoTerminal discovery ------------------------------------------
    async def _fetch_gt(self, url: str, params: dict, pages: int, label: str) -> list[GeckoPool]:
        pools: list[GeckoPool] = []
        async with aiohttp.ClientSession(
            headers={"accept": "application/json", "user-agent": "pump-chart/feed-sources"}
        ) as session:
            for page in range(1, max(1, pages) + 1):
                await self._pace("_last_gt_at", GECKO_MIN_INTERVAL)
                try:
                    async with session.get(
                        url, params={**params, "page": page},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as r:
                        if r.status != 200:
                            logger.warning(f"[FeedSources] GT {label} page {page}: HTTP {r.status}")
                            break
                        data = await r.json(content_type=None)
                except Exception as e:
                    logger.warning(f"[FeedSources] GT {label} page {page} failed: {e}")
                    break
                for node in data.get("data") or []:
                    p = parse_gecko_pool(node)
                    if p is not None:
                        pools.append(p)
        return pools

    async def poll_universe(
        self,
        pages_new: int = 1,
        pages_trending: int = 1,
        duration: str = "1h",
    ) -> list[GeckoPool]:
        """Discovery universe = trending pools (mature movers, the GMGN
        'trending 1h' analog — new_pools alone starves: its newest-first list
        ages PumpSwap pools out before the min-age gate admits them) UNION
        new pools (fresh migrations).  Deduped by mint, trending wins."""
        trending = await self._fetch_gt(
            GECKO_TRENDING_URL, {"duration": duration}, pages_trending, "trending")
        fresh = await self._fetch_gt(
            GECKO_NEW_POOLS_URL, {}, pages_new, "new_pools")
        by_mint: dict[str, GeckoPool] = {}
        for p in trending + fresh:
            by_mint.setdefault(p.mint, p)   # trending (first) wins
        return list(by_mint.values())

    async def poll_new_pools(self, pages: int = 2) -> list[GeckoPool]:
        """Legacy entry (kept for tests/tools): fresh pools only."""
        return await self._fetch_gt(GECKO_NEW_POOLS_URL, {}, pages, "new_pools")

    # -- RugCheck (optional, paced + cached) -------------------------------
    async def _rugcheck(self, session: aiohttp.ClientSession, mint: str) -> Optional[dict]:
        now = time.time()
        hit = self._rc_cache.get(mint)
        if hit and now - hit[0] < RUGCHECK_TTL:
            return hit[1]
        await self._pace_slot("_last_rc_at", RUGCHECK_MIN_INTERVAL)
        try:
            async with session.get(
                RUGCHECK_REPORT_URL.format(mint=mint),
                timeout=aiohttp.ClientTimeout(total=VET_RPC_TIMEOUT),
            ) as r:
                if r.status != 200:
                    data = None
                else:
                    raw = await r.json(content_type=None)
                    # RugCheck quirk (2026-09 study): errors can arrive inside
                    # HTTP-200 bodies — validate the shape before trusting.
                    data = raw if isinstance(raw, dict) and ("score" in raw or "risks" in raw) else None
        except Exception as e:
            logger.debug(f"[FeedSources] rugcheck {mint[:8]} failed: {e}")
            data = None
        self._rc_cache[mint] = (now, data)
        if len(self._rc_cache) > 500:
            cutoff = now - RUGCHECK_TTL
            self._rc_cache = {m: v for m, v in self._rc_cache.items() if v[0] > cutoff}
        return data

    @staticmethod
    def _parse_rugcheck(report: Optional[dict]) -> dict:
        if not report:
            return {"score": None, "rugged": False, "risks": [], "creator_tokens": None, "insider_graphs": False}
        risks = []
        for r in report.get("risks") or []:
            if isinstance(r, dict):
                risks.append(str(r.get("name") or r.get("risk") or ""))
            else:
                risks.append(str(r))
        creator = report.get("creator") or {}
        creator_tokens = creator.get("tokens") if isinstance(creator, dict) else None
        return {
            "score": _fnum(report.get("score_normalised")),
            "rugged": bool(report.get("rugged")),
            "risks": [x for x in risks if x],
            "creator_tokens": _inum(creator_tokens) if creator_tokens is not None else None,
            "insider_graphs": bool(report.get("graphInsidersDetected")),
        }

    # -- full vet ----------------------------------------------------------
    async def vet(self, mint: str) -> VettingResult:
        v = VettingResult(mint=mint)
        async with aiohttp.ClientSession() as session:
            # 1) RPC: mint authority + supply (jsonParsed) — REQUIRED source.
            try:
                acc = await _rpc_call(session, "getAccountInfo",
                                      [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
                mint_info = parse_mint_account(acc.get("value") or {})
                v.rpc_ok = True
                v.renounced_mint = mint_info["mint_authority"] is None
                v.renounced_freeze = mint_info["freeze_authority"] is None
                v.supply = mint_info["supply"]
            except Exception as e:
                logger.warning(f"[FeedSources] RPC mint-info {mint[:8]} failed: {e}")
                v.detail["rpc_error"] = str(e)[:200]
                return v   # fail-closed: caller rejects on rpc_ok False
            # 2) RPC: real concentration
            try:
                la = await _rpc_call(session, "getTokenLargestAccounts",
                                     [mint, {"commitment": "confirmed"}])
                amounts = parse_largest_accounts(la.get("value") or [])
                v.top10_share, v.top20_share = concentration_shares(v.supply, amounts)
            except Exception as e:
                logger.warning(f"[FeedSources] RPC largest-accounts {mint[:8]} failed: {e}")
                v.detail["largest_accounts_error"] = str(e)[:200]
                # concentration missing but authority proven — gates decide
            # 3) RugCheck (optional, best-effort)
            rc = self._parse_rugcheck(await self._rugcheck(session, mint))
            v.rc_ok = rc["score"] is not None or bool(rc["risks"]) or rc["rugged"]
            v.rugcheck_score = rc["score"]
            v.rugcheck_rugged = rc["rugged"]
            v.rugcheck_risks = rc["risks"]
            v.creator_token_count = rc["creator_tokens"]
            v.insider_graphs = rc["insider_graphs"]
        v.detail["score"] = composite_risk_score(v)
        return v


def apply_gecko_quality_gates(
    pool: GeckoPool,
    vet: VettingResult,
    *,
    require_renounced_mint: bool,
    require_renounced_freeze: bool,
    max_top20_holder_rate: Optional[float],
    max_rugcheck_score: Optional[float],
    vetting_fail_policy: str,
    reject_tally: Optional[dict] = None,
) -> Optional[str]:
    """Post-vetting gates.  Returns None when the candidate passes, else the
    reject reason.  FAIL-CLOSED: unknown authority (RPC failed) rejects under
    the default `reject` policy; unknown concentration only rejects when a
    ceiling is configured (can't compare against unknown)."""
    def rej(reason: str) -> str:
        if reject_tally is not None:
            reject_tally[reason] = reject_tally.get(reason, 0) + 1
        return reason

    if not vet.rpc_ok:
        if vetting_fail_policy == "reject":
            return rej("vetting_rpc_unavailable")
    else:
        if require_renounced_mint and vet.renounced_mint is not True:
            return rej("not_renounced_mint_onchain")
        if require_renounced_freeze and vet.renounced_freeze is not True:
            return rej("not_renounced_freeze_onchain")
    if max_top20_holder_rate is not None:
        if vet.top20_share is None:
            if vetting_fail_policy == "reject":
                return rej("vetting_concentration_unknown")
        elif vet.top20_share > max_top20_holder_rate:
            return rej("max_top20_holder_rate")
    if max_rugcheck_score is not None and vet.rugcheck_score is not None:
        if vet.rugcheck_score > max_rugcheck_score:
            return rej("max_rugcheck_score")
    if vet.rugcheck_rugged:
        return rej("rugcheck_rugged")
    return None
