"""
Organic gate — in-house evidence that a candidate is NOT a bundled / inorganic launch.

Why this exists
---------------
`autofeed._to_candidate` screened GMGN's *aggregate* risk numbers (bundler_rate,
rug_ratio, …).  Those aggregates are computed by the vendor from data it will
not show, they are sparse, and they are easy to game: a launch tool that spreads
supply over 20 wallets reports a low `bundler_rate` while still controlling 20%
of the float.  Measured on the recorded corpus (iter102): admissions that passed
the aggregator still produced tapes the tape-integrity audit classifies as
mechanical (rug collapse / nuke dump / wash echo).  The vendor score is not the
thing we actually care about — the thing we care about is **who owns the supply
and whether those owners are one entity**.

This module computes that from the holder table itself, which is the set of
checks experienced trench traders actually run by hand:

  Setuh, "The Memecoin Blueprint Day 2: Bundles" — four manual checks
    1. holder distribution: any non-LP holder > 4% ⇒ leave; identical lot sizes
       (2.5 / 2.5 / 2.5) ⇒ bundle;
    2. SOL balance of the top holders: all holding the same tiny balance ⇒
       bundle ("1 SOL, 1 SOL, 1 SOL");
    3. "funded buy" tab: all top holders funded from the same place at the same
       time (Coinbase 1 day ago ×8) ⇒ bundle;
    4. every top holder using the same terminal ⇒ multi-wallet;
    plus fresh wallets ("leaves") in the top 10, and cross-holding overlap.
  Setuh, "Day 3: Scanning" — 2-3 tracked whales in a sub-$10k coin ⇒ skip;
    already-bonded ticker (relaunch) ⇒ skip.
  r/CryptoTrenching "What I learned after 6 months trenching" — top holders
    above $50k mcap sitting on 0.0x SOL balances is "almost always a bundler".
  r/CryptoTrenching "In-depth guide to spotting bundled coins" — the bundled
    chart fingerprint is a one-candle pump (30k→50k) that is later sold, not a
    steady organic ramp.
  r/solana "What is bundling and how can you spot it?" — JITO bundles put the
    dev's own buys in the same bundle as the liquidity tx, then the tokens are
    diluted across wallets so the biggest-holder number looks innocent.

Every signal below is derived from per-wallet rows returned by GMGN's
`/v1/market/token_top_holders` (one request per candidate), which carries the
raw quantities needed: `amount_percentage` (current supply share),
`native_balance` (SOL), `created_at` (wallet age), `start_holding_at` (first
buy), `native_transfer.from_address/.timestamp` (the funder and when), and the
`maker_token_tags` / `tags` labels (bundler, sniper, fresh_wallet, terminal).

Design contract
---------------
  * Pure function first: `evaluate()` takes rows + context and returns a verdict.
    No I/O, fully deterministic, unit-testable on captured payloads.
  * `verify()` is the async I/O shell: one holders fetch, cached per mint,
    respecting the process-wide GMGN rate-limit state.
  * FAIL-CLOSED: a candidate whose holder table cannot be fetched is *not*
    forwarded.  "Could not verify" is never treated as "verified clean" —
    that was the exact failure mode of the 2026-11-16 aggregation gate.
  * Every rejection carries machine-readable `reasons` plus the metric vector,
    so a reviewer can see which evidence drove it and the live feed can be
    tuned against outcomes instead of intuition.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

import rate_limiter as rl

logger = logging.getLogger("organic_gate")

GMGN_HOST = "https://openapi.gmgn.ai"
HOLDERS_PATH = "/v1/market/token_top_holders"
# Cloudflare in front of openapi.gmgn.ai rejects the default urllib/aiohttp UA
# with "error code: 1010", so every request carries a browser UA.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")

# Funders that are exchanges / bridges / mixers: a group of holders sharing one
# of these is organically funded, not a bundle (Setuh's own advice is to treat
# "all Coinbase 1 day ago" as suspicious, but a CEX hot wallet funds thousands
# of unrelated users — clustering on it would reject the whole market).
_CEX_FUNDER_NAMES = {
    "coinbase", "binance", "kraken", "okx", "bybit", "bitget", "kucoin",
    "gate", "gate.io", "htx", "huobi", "mexc", "crypto.com", "gemini",
    "fixed float", "fixedfloat", "changelly", "changenow", "simple swap",
    "simpleswap", "swapspace", "phantom", "solflare", "backpack", "trust",
    "moonpay", "ramp", "transak", "mercuryo", "stripe", "revolut", "wirex",
    "binance.us", "robinhood", "upbit", "bithumb", "bitfinex", "bitstamp",
    "tornado", "wormhole", "layerzero", "debridge", "jupiter", "relay",
}
# Terminal / trading-app wallet tags.  A top-10 where nearly every wallet was
# trading through the *same* app is Setuh's "all on Terminal" multi-wallet tell.
_TERMINAL_TAGS = {
    "axiom", "photon", "trojan", "bullx", "gmgn", "padre", "bloom", "nova",
    "terminal", "fomo", "pepeboost", "maestro", "banana", "solsniffer",
    "debot", "fluxbeam", "jupiter", "phantom",
}


# ── config ──────────────────────────────────────────────────────────────────

@dataclass
class GateConfig:
    """Thresholds.  Defaults are the research-backed starting points; every one
    is hot-swappable from the dashboard (`/api/autofeed/config`)."""

    enabled: bool = True
    # "enforce"  — reject candidates that fail (default)
    # "shadow"   — compute + record the verdict, still feed (rollout / tuning)
    mode: str = "enforce"

    def __post_init__(self) -> None:
        # An unknown mode must never silently disable the block: fail safe.
        m = str(self.mode or "enforce").strip().lower()
        if m not in ("enforce", "shadow", "off"):
            logger.warning(f"[OrganicGate] unknown mode={self.mode!r} — using 'enforce'")
            m = "enforce"
        self.mode = m

    # ── supply concentration (Setuh check 1 + 2) ──
    # Setuh's video rule is "any non-LP holder > 4% ⇒ leave".  That is a curve/
    # new-pair rule; this feed trades tokens AFTER migration, where 50-60% of
    # supply sits in the LP and the tradable float is correspondingly small, so
    # a literal 4-5% cap rejects essentially the whole post-graduation market
    # (measured 2026-11-18: 9/9 live candidates blocked, mostly on a 3-5% early
    # wallet).  10% is the same "one entity can nuke the chart" bound applied
    # to the post-migration float; everything above that is a hard reject.
    max_single_holder_pct: float = 0.10
    max_top10_holder_pct: float = 0.35    # top-10 (ex LP) above 35% ⇒ bundled float
    max_dev_hold_pct: float = 0.03        # creator still holding >3% ⇒ exit liquidity
    # Pool / float sanity: a "pool" that holds nearly the whole supply leaves no
    # tradable float (measured live: GTASZN pool=99.5%, i.e. $2.2k of a $168k
    # mcap circulating).  Buying that is exit liquidity by construction.
    max_pool_supply_pct: float = 0.85
    min_float_usd: float = 25_000.0
    max_dev_team_hold_pct: float = 0.08  # dev-team wallets' share (GMGN field)
    max_top70_sniper_hold_pct: float = 0.08  # snipers inside the top 70 wallets

    # ── launch-window buying (the bundle itself) ──
    launch_window_seconds: float = 120.0  # "bought in the first 2 minutes"
    max_launch_bundle_pct: float = 0.20   # launch-window wallets still hold ≥20%
    max_launch_single_pct: float = 0.05   # one launch-window wallet ≥5%

    # ── wallet cohorts (Setuh check 3/4, fresh wallets) ──
    max_funder_cluster_n: int = 4         # wallets funded from one source…
    funding_window_hours: float = 24.0    # …inside one funding window…
    max_funder_cluster_pct: float = 0.12  # …still holding ≥12% of supply
    fresh_wallet_days: float = 3.0        # wallet created ≤3 days before launch
    max_created_cohort_n: int = 4         # ≥4 pre-launch-created wallets…
    max_created_cohort_pct: float = 0.10  # …holding ≥10%
    max_fresh_tag_pct: float = 0.35       # GMGN "fresh_wallet" tagged share
    max_bundler_tag_pct: float = 0.25     # wallets GMGN tags *bundler* still hold
    max_sniper_holder_pct: float = 0.30   # sniper-tagged wallets' share

    # ── dormant / matched-wallet tells (Setuh "advanced bundles") ──
    min_median_top10_sol: float = 0.02    # dust balances ⇒ prepared wallets
    lot_tolerance: float = 0.02           # 2% relative ⇒ "identical" lots
    max_matched_lot_pairs: int = 6        # ≥6 identical pairs in top-10 ⇒ ladder
    max_terminal_dominance: int = 9       # ≥9/10 top wallets on one terminal
    max_mean_suspicious: float = 0.34     # GMGN is_suspicious share

    # ── deployer / social hygiene (row-level, from the trending payload) ──
    max_creator_created_count: int = 3    # serial launcher
    # `image_dup`, `website_dup` and `twitter_create_token_count` are ambiguous
    # in GMGN's payload: they count how many tokens *point at* this asset, so a
    # successful original gets a huge count from its own copycats (the real
    # GTA VI token measured website_dup=801, image_dup=3).  Gate only when set.
    max_twitter_create_token_count: Optional[int] = None
    max_image_dup: Optional[int] = None
    max_website_dup: Optional[int] = None
    max_ath_drawdown_pct: float = 0.75    # now < 25% of all-time-high mcap
    reject_dev_holding: bool = True       # creator_token_status == creator_hold
    reject_unburned_lp: bool = True       # burn_status present and != burn
    reject_cto: bool = False              # community takeover (dev dumped)

    # ── stacked-soft-signal rule ──
    # No single weak tell is proof, but several together are (coin_filtering_prompt:
    # "each one alone is weak, the convergence is what matters").
    soft_reject_score: int = 3

    # ── I/O ──
    holder_rows: int = 100
    fetch_timeout: float = 20.0
    cache_seconds: float = 900.0          # per-mint holder-table cache
    max_fetch_per_tick: int = 6           # protects the shared GMGN key quota
    fetch_wait_budget: float = 25.0       # max seconds to wait out a 429 mid-tick


CRITICAL = "critical"
SOFT = "soft"


@dataclass
class GateResult:
    passed: bool
    mode: str = "enforce"
    mint: str = ""
    symbol: str = ""
    reasons: list[str] = field(default_factory=list)      # failing rule ids
    critical: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    score: int = 0
    metrics: dict = field(default_factory=dict)
    error: str = ""                                       # "" | fetch failure kind
    fetched: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def blocked(self) -> bool:
        """True when this verdict must prevent a session from being opened."""
        return (self.mode == "enforce" and not self.passed) or bool(self.error)

    def summary(self, limit: int = 4) -> str:
        if self.error:
            return f"unverified:{self.error}"
        return ",".join(self.reasons[:limit]) or "clean"


# ── numeric helpers ─────────────────────────────────────────────────────────

def _num(v, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _lamports_sol(v) -> float:
    try:
        return int(v) / 1e9
    except (TypeError, ValueError):
        return 0.0


def _pool_index(rows: list[dict]) -> Optional[int]:
    """Index of the liquidity-pool row, if the payload includes it.

    GMGN sometimes lists the pool as a holder (large share, no wallet creation
    time, no funder) and sometimes filters it out.  Only a row that looks like a
    pool by *all three* criteria is excluded, so a real whale is never dropped.
    """
    best, best_pct = None, 0.0
    for i, r in enumerate(rows):
        pct = _num(r.get("amount_percentage"), 0.0) or 0.0
        funder = (r.get("native_transfer") or {}).get("from_address")
        if pct >= 0.20 and not r.get("created_at") and not funder:
            if pct > best_pct:
                best, best_pct = i, pct
    return best


def _funder_group(rows: list[dict], window_s: float) -> tuple[int, float, str]:
    """Largest cluster of wallets funded from one address inside one window.

    Returns (wallet_count, share_of_supply, funder_address).  CEX / bridge
    funders are skipped: they fund unrelated users, so sharing one is not a
    bundle (see _CEX_FUNDER_NAMES).
    """
    groups: dict[str, list[tuple[float, dict]]] = {}
    for r in rows:
        nt = r.get("native_transfer") or {}
        funder = (nt.get("from_address") or "").strip()
        if not funder:
            continue
        name = (nt.get("name") or "").strip().lower()
        if name and any(cex in name for cex in _CEX_FUNDER_NAMES):
            continue
        ts = _num(nt.get("timestamp"), 0.0) or 0.0
        groups.setdefault(funder, []).append((ts, r))
    best_n, best_pct, best_funder = 0, 0.0, ""
    for funder, items in groups.items():
        items.sort(key=lambda x: x[0])
        i = 0
        while i < len(items):
            j = i
            while j + 1 < len(items) and items[j + 1][0] - items[i][0] <= window_s:
                j += 1
            block = [r for _, r in items[i:j + 1]]
            share = sum(_num(r.get("amount_percentage"), 0.0) or 0.0 for r in block)
            if len(block) > best_n or (len(block) == best_n and share > best_pct):
                best_n, best_pct, best_funder = len(block), share, funder
            i += 1
    return best_n, best_pct, best_funder


def _matched_lot_pairs(pcts: list[float], tol: float) -> int:
    """Count pairs of near-identical supply shares (the 2.5/2.5/2.5 ladder)."""
    pairs = 0
    vals = [p for p in pcts if p >= 0.002]
    for i in range(len(vals)):
        for j in range(i + 1, len(vals)):
            a, b = vals[i], vals[j]
            if a > 0 and abs(a - b) <= tol * a:
                pairs += 1
    return pairs


# ── pure evaluation ─────────────────────────────────────────────────────────

def evaluate(
    rows: list[dict],
    *,
    created_ts: float,
    cfg: GateConfig,
    row: Optional[dict] = None,
    mint: str = "",
    symbol: str = "",
    live_supply: Optional[float] = None,
) -> GateResult:
    """Score a holder table against the bundle evidence rules.

    `rows` is the raw GMGN holder list; `created_ts` the token creation time;
    `row` the GMGN trending row (deployer/social hygiene checks).
    """
    res = GateResult(passed=True, mode=cfg.mode, mint=mint, symbol=symbol, fetched=True)
    row = row or {}
    if not rows:
        res.passed = False
        res.error = "empty_holder_table"
        res.reasons = ["unverified_holders"]
        return res

    pool_i = _pool_index(rows)
    pool_pct = (_num(rows[pool_i].get("amount_percentage"), 0.0) or 0.0) if pool_i is not None else 0.0
    holders = [r for i, r in enumerate(rows) if i != pool_i]
    if not holders:
        res.passed = False
        res.error = "no_holders_after_pool"
        res.reasons = ["unverified_holders"]
        return res

    # Ignore rows that received tokens by transfer from another wallet when
    # measuring float ownership *unless* they are large — transferred supply is
    # still dumpable, so keep them in every concentration metric.
    pcts = [_num(r.get("amount_percentage"), 0.0) or 0.0 for r in holders]
    top10_pct = sum(pcts[:10])

    # ── float sanity ──
    float_pct = max(0.0, 1.0 - pool_pct)
    mcap_row = _num(row.get("market_cap"), 0.0) or 0.0
    float_usd = mcap_row * float_pct if mcap_row > 0 else 0.0

    # ── toward the biggest single non-LP holder ──
    max_i = max(range(len(holders)), key=lambda i: pcts[i]) if pcts else 0
    max_single = pcts[max_i] if pcts else 0.0
    max_single_addr = holders[max_i].get("address") or "" if holders else ""

    # ── launch-window buying: the bundle set-up itself ──
    launch_cut = (created_ts + cfg.launch_window_seconds) if created_ts else None
    launch_rows = []
    if launch_cut:
        for r in holders:
            s = _num(r.get("start_holding_at"), 0.0) or 0.0
            if s and s <= launch_cut:
                launch_rows.append(r)
    launch_pct = sum(_num(r.get("amount_percentage"), 0.0) or 0.0 for r in launch_rows)
    launch_single = max((_num(r.get("amount_percentage"), 0.0) or 0.0 for r in launch_rows), default=0.0)

    # ── wallet cohorts ──
    funder_n, funder_pct, funder_addr = _funder_group(holders, cfg.funding_window_hours * 3600.0)
    cohort_rows = []
    if created_ts:
        cutoff = created_ts - cfg.fresh_wallet_days * 86400.0
        for r in holders:
            c = _num(r.get("created_at"), 0.0) or 0.0
            if c and cutoff <= c <= created_ts:
                cohort_rows.append(r)
    cohort_pct = sum(_num(r.get("amount_percentage"), 0.0) or 0.0 for r in cohort_rows)

    fresh_tag_pct = sum(p for r, p in zip(holders, pcts)
                        if "fresh_wallet" in (r.get("tags") or []))
    bundler_tag_pct = sum(p for r, p in zip(holders, pcts)
                          if "bundler" in (r.get("maker_token_tags") or []))
    sniper_tag_pct = sum(p for r, p in zip(holders, pcts)
                         if "sniper" in (r.get("maker_token_tags") or []))
    suspicious_share = (sum(1 for r in holders if r.get("is_suspicious")) / len(holders))

    # ── dormant wallets / matched lots / terminal concentration ──
    top10_sol = [_lamports_sol(r.get("native_balance")) for r in holders[:10]]
    median_top10_sol = statistics.median(top10_sol) if top10_sol else 0.0
    lot_pairs = _matched_lot_pairs(pcts[:10], cfg.lot_tolerance)
    term_counts: dict[str, int] = {}
    for r in holders[:10]:
        for t in (r.get("tags") or []):
            tl = str(t).lower()
            if tl in _TERMINAL_TAGS:
                term_counts[tl] = term_counts.get(tl, 0) + 1
    term_tag, term_n = (max(term_counts.items(), key=lambda kv: kv[1])
                        if term_counts else ("", 0))

    dev_hold = _num(row.get("creator_hold_rate"))
    if dev_hold is None:
        dev_hold = _num(row.get("stat_creator_hold_rate"), 0.0) or 0.0

    metrics = {
        "pool_pct": round(pool_pct, 4),
        "float_pct": round(float_pct, 4),
        "float_usd": round(float_usd, 2),
        "holders_seen": len(holders),
        "top10_pct": round(top10_pct, 4),
        "max_single_pct": round(max_single, 4),
        "max_single_addr": max_single_addr,
        "launch_rows": len(launch_rows),
        "launch_bundle_pct": round(launch_pct, 4),
        "launch_single_pct": round(launch_single, 4),
        "funder_cluster_n": funder_n,
        "funder_cluster_pct": round(funder_pct, 4),
        "funder": funder_addr[:12],
        "created_cohort_n": len(cohort_rows),
        "created_cohort_pct": round(cohort_pct, 4),
        "fresh_tag_pct": round(fresh_tag_pct, 4),
        "bundler_tag_pct": round(bundler_tag_pct, 4),
        "sniper_tag_pct": round(sniper_tag_pct, 4),
        "suspicious_share": round(suspicious_share, 4),
        "median_top10_sol": round(median_top10_sol, 4),
        "matched_lot_pairs": lot_pairs,
        "terminal_tag": term_tag,
        "terminal_n": term_n,
        "dev_hold_pct": round(dev_hold, 4),
        "live_supply": live_supply,
    }
    res.metrics = metrics

    def fail(rule: str, severity: str = CRITICAL) -> None:
        res.reasons.append(rule)
        (res.critical if severity == CRITICAL else res.soft).append(rule)
        if severity == SOFT:
            res.score += 1

    # ── hard, structural failures ──
    if pool_pct > cfg.max_pool_supply_pct:
        fail("supply_in_pool")
    if pool_pct > 0 and mcap_row > 0 and float_usd < cfg.min_float_usd:
        fail("thin_float")
    if max_single > cfg.max_single_holder_pct:
        fail("single_holder_concentration")
    if top10_pct > cfg.max_top10_holder_pct:
        fail("top10_concentration")
    if launch_single > cfg.max_launch_single_pct:
        fail("launch_wallet_concentration")
    if launch_pct > cfg.max_launch_bundle_pct:
        fail("launch_bundle_supply")
    if funder_n >= cfg.max_funder_cluster_n and funder_pct > cfg.max_funder_cluster_pct:
        fail("funder_cluster")
    if len(cohort_rows) >= cfg.max_created_cohort_n and cohort_pct > cfg.max_created_cohort_pct:
        fail("prelaunch_wallet_cohort")
    if bundler_tag_pct > cfg.max_bundler_tag_pct:
        fail("bundler_tagged_supply")
    if sniper_tag_pct > cfg.max_sniper_holder_pct:
        fail("sniper_tagged_supply")

    # ── deployer / social hygiene (only when the field is present) ──
    creator_status = str(row.get("creator_token_status") or "").strip().lower()
    # A creator who still holds is a soft tell, not a hard one: 19/100 trending
    # rows are `creator_hold` and their median mcap is 50x the `creator_close`
    # cohort — i.e. this field fires most on *successful* tokens whose dev kept
    # a bag.  The quantified variants (dev_hold_rate / dev_team_hold_rate) are
    # the ones worth rejecting on.
    if creator_status in ("creator_hold", "creator_holding"):
        fail("dev_still_holding", SOFT)
    if cfg.reject_dev_holding and dev_hold > cfg.max_dev_hold_pct:
        fail("dev_hold_rate")
    dev_team_hold = _num(row.get("dev_team_hold_rate"))
    if dev_team_hold is not None:
        metrics["dev_team_hold_pct"] = round(dev_team_hold, 4)
        if dev_team_hold > cfg.max_dev_team_hold_pct:
            fail("dev_team_hold")
    sniper_hold = _num(row.get("top70_sniper_hold_rate"))
    if sniper_hold is not None:
        metrics["top70_sniper_hold_pct"] = round(sniper_hold, 4)
        if sniper_hold > cfg.max_top70_sniper_hold_pct:
            fail("sniper_hold_rate")
    burn_status = str(row.get("burn_status") or "").strip().lower()
    if cfg.reject_unburned_lp and burn_status and burn_status != "burn":
        fail("lp_not_burned")
    created_count = _num(row.get("creator_created_count"))
    if created_count is not None and created_count > cfg.max_creator_created_count:
        fail("serial_creator")
    tw_create = _num(row.get("twitter_create_token_count"))
    if (cfg.max_twitter_create_token_count is not None and tw_create is not None
            and tw_create > cfg.max_twitter_create_token_count):
        fail("twitter_token_spam")
    img_dup = _num(row.get("image_dup"))
    if cfg.max_image_dup is not None and img_dup is not None and img_dup > cfg.max_image_dup:
        fail("image_reuse")
    web_dup = _num(row.get("website_dup"))
    if cfg.max_website_dup is not None and web_dup is not None and web_dup > cfg.max_website_dup:
        fail("website_reuse")
    if cfg.reject_cto and bool(row.get("cto_flag")):
        fail("cto_flag")

    ath = _num(row.get("history_highest_market_cap"), 0.0) or 0.0
    mcap = _num(row.get("market_cap"), 0.0) or 0.0
    if ath > 0 and mcap > 0:
        drawdown = 1.0 - (mcap / ath)
        metrics["ath_drawdown_pct"] = round(drawdown, 4)
        if drawdown > cfg.max_ath_drawdown_pct:
            fail("round_tripped")

    # ── soft tells: individually weak, decisive in combination ──
    if fresh_tag_pct > cfg.max_fresh_tag_pct:
        fail("fresh_wallet_share", SOFT)
    if median_top10_sol < cfg.min_median_top10_sol:
        fail("dust_top_holders", SOFT)
    if lot_pairs >= cfg.max_matched_lot_pairs:
        fail("matched_lot_sizes", SOFT)
    if term_n >= cfg.max_terminal_dominance:
        fail("terminal_dominance", SOFT)
    if suspicious_share > cfg.max_mean_suspicious:
        fail("flagged_wallets", SOFT)

    if res.score >= cfg.soft_reject_score:
        fail("stacked_risk_signals")

    res.passed = not res.critical and res.score < cfg.soft_reject_score
    return res


# ── I/O shell ───────────────────────────────────────────────────────────────

class OrganicGate:
    """Async verifier with a per-mint cache and shared rate-limit awareness."""

    def __init__(self, config: Optional[GateConfig] = None, api_key: str = ""):
        self.config = config or GateConfig()
        self.api_key = api_key or ""
        self._cache: dict[str, tuple[float, GateResult]] = {}
        self._tick_calls = 0
        self._tick_started = 0.0
        # telemetry
        self.total_checked = 0
        self.total_passed = 0
        self.total_rejected = 0
        self.total_unverified = 0
        self.reason_totals: dict[str, int] = {}
        self.last_reject: dict = {}
        self.last_result: dict = {}

    # -- public ------------------------------------------------------------
    def verdict_for(self, mint: str) -> Optional[GateResult]:
        """Cached verdict for a mint (None when never verified).  Used by the
        session-spawn path as a second, independent block."""
        hit = self._cache.get(mint)
        return hit[1] if hit else None

    def set_config(self, partial: dict) -> list[str]:
        changed = []
        for k, v in partial.items():
            if hasattr(self.config, k) and v is not None:
                if getattr(self.config, k) != v:
                    setattr(self.config, k, v)
                    changed.append(k)
        return changed

    def snapshot(self) -> dict:
        return {
            "mode": self.config.mode,
            "enabled": self.config.enabled,
            "checked": self.total_checked,
            "passed": self.total_passed,
            "rejected": self.total_rejected,
            "unverified": self.total_unverified,
            "reason_totals": dict(self.reason_totals),
            "last_reject": self.last_reject,
            "last_result": self.last_result,
            "cached_mints": len(self._cache),
            "thresholds": {
                "max_single_holder_pct": self.config.max_single_holder_pct,
                "max_top10_holder_pct": self.config.max_top10_holder_pct,
                "max_launch_bundle_pct": self.config.max_launch_bundle_pct,
                "launch_window_seconds": self.config.launch_window_seconds,
                "max_funder_cluster_pct": self.config.max_funder_cluster_pct,
                "max_bundler_tag_pct": self.config.max_bundler_tag_pct,
                "max_fresh_tag_pct": self.config.max_fresh_tag_pct,
                "max_creator_created_count": self.config.max_creator_created_count,
            },
        }

    async def verify(self, cand, row: Optional[dict] = None) -> GateResult:
        """Fetch + score one candidate.  Never raises.

        `cand` is a `autofeed.Candidate`; `row` the raw GMGN row it came from.
        """
        cfg = self.config
        if not cfg.enabled or cfg.mode == "off":
            return GateResult(passed=True, mode="off", mint=getattr(cand, "mint", ""),
                              symbol=getattr(cand, "symbol", ""), metrics={"skipped": True})

        mint = getattr(cand, "mint", "")
        symbol = getattr(cand, "symbol", "")
        cached = self._cache.get(mint)
        if cached and time.time() - cached[0] < cfg.cache_seconds:
            return cached[1]

        now = time.time()
        if now - self._tick_started > 60.0:
            self._tick_started, self._tick_calls = now, 0
        if self._tick_calls >= cfg.max_fetch_per_tick:
            return self._record(GateResult(
                passed=False, mode=cfg.mode, mint=mint, symbol=symbol,
                error="fetch_budget_exhausted", reasons=["unverified_budget"]))

        if rl.gmgn_banned():
            # Shared key: a ban at this point is a holder_flow-driven 429, not
            # our own.  Wait it out inside the tick when it is short enough —
            # deferring instead would starve the feed while the quota resets.
            remaining = rl.gmgn_ban_remaining() or 15.0
            if 0 < remaining <= cfg.fetch_wait_budget:
                logger.info(f"[OrganicGate] GMGN ban — waiting {remaining:.0f}s to "
                            f"verify ${symbol or '?'}")
                await asyncio.sleep(remaining + 0.75)
            if rl.gmgn_banned():
                return self._record(GateResult(
                    passed=False, mode=cfg.mode, mint=mint, symbol=symbol,
                    error="rate_limited", reasons=["unverified_rate_limited"]))

        self._tick_calls += 1
        created = _num(getattr(cand, "creation_timestamp", 0.0), 0.0) or 0.0
        rows, err = await self._fetch_holders(mint)
        if err == "rate_limited" and cfg.fetch_wait_budget > 0:
            # The GMGN key is shared with holder_flow's polling, so a 429 is
            # routine rather than exceptional.  Waiting out the (short) ban
            # window inside the tick is far better than starving the feed:
            # the poll loop has nothing else to do until this tick returns.
            wait = min(rl.gmgn_ban_remaining() or 15.0, cfg.fetch_wait_budget)
            if wait > 0:
                logger.info(f"[OrganicGate] rate-limited — waiting {wait:.0f}s to verify "
                            f"${symbol or '?'}")
                await asyncio.sleep(wait + 0.75)
                rows, err = await self._fetch_holders(mint)
        if err:
            return self._record(GateResult(
                passed=False, mode=cfg.mode, mint=mint, symbol=symbol,
                error=err, reasons=[f"unverified_{err}"]))

        res = evaluate(rows, created_ts=created, cfg=cfg, row=row, mint=mint, symbol=symbol)
        self._cache[mint] = (time.time(), res)
        if len(self._cache) > 500:
            cutoff = time.time() - cfg.cache_seconds
            self._cache = {k: v for k, v in self._cache.items() if v[0] >= cutoff}
        return self._record(res)

    # -- internals ---------------------------------------------------------
    def _record(self, res: GateResult) -> GateResult:
        self.total_checked += 1
        for r in res.reasons:
            self.reason_totals[r] = self.reason_totals.get(r, 0) + 1
        if res.error:
            self.total_unverified += 1
        elif res.passed:
            self.total_passed += 1
        else:
            self.total_rejected += 1
            self.last_reject = {
                "ts": time.time(), "mint": res.mint, "symbol": res.symbol,
                "reasons": res.reasons, "metrics": res.metrics,
            }
        self.last_result = res.to_dict()
        if res.error:
            logger.warning(f"[OrganicGate] ${res.symbol or '?'} {res.mint[:8]}… "
                           f"UNVERIFIED ({res.error}) — not fed")
        elif not res.passed:
            logger.info(f"[OrganicGate] ${res.symbol or '?'} {res.mint[:8]}… REJECT "
                        f"{res.summary(6)} score={res.score} "
                        f"single={res.metrics.get('max_single_pct')} "
                        f"top10={res.metrics.get('top10_pct')} "
                        f"launch={res.metrics.get('launch_bundle_pct')} "
                        f"bundler={res.metrics.get('bundler_tag_pct')}")
        return res

    async def _fetch_holders(self, mint: str) -> tuple[Optional[list[dict]], str]:
        """One authenticated holders GET.  Returns (rows, error_kind)."""
        if not self.api_key:
            return None, "no_api_key"
        try:
            import aiohttp
        except Exception:                                   # pragma: no cover
            return None, "aiohttp_missing"
        params = {
            "chain": "sol", "address": mint, "limit": str(self.config.holder_rows),
            "timestamp": str(int(time.time())), "client_id": str(uuid.uuid4()),
        }
        headers = {"X-APIKEY": self.api_key, "Content-Type": "application/json",
                   "User-Agent": _UA, "Accept": "application/json"}
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.config.fetch_timeout)
            ) as sess:
                async with sess.get(f"{GMGN_HOST}{HOLDERS_PATH}",
                                    params=params, headers=headers) as resp:
                    if resp.status == 429:
                        body = await resp.text()
                        rl.note_gmgn_429(rl.parse_gmgn_reset_time(body), source="organic_gate")
                        return None, "rate_limited"
                    if resp.status != 200:
                        return None, f"http_{resp.status}"
                    data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            return None, "timeout"
        except Exception as e:                             # network / DNS / TLS
            return None, f"network_{type(e).__name__}"

        if not isinstance(data, dict):
            return None, "bad_shape"
        code = data.get("code")
        if code not in (0, None, 200):
            msg = str(data.get("message") or data.get("error") or "")[:80]
            return None, f"api_code_{code}:{msg}" if msg else f"api_code_{code}"
        payload = data.get("data") or {}
        rows = payload.get("list") if isinstance(payload, dict) else payload
        if not isinstance(rows, list) or not rows:
            return None, "empty_holders"
        return rows, ""


def build_config(autofeed_cfg) -> GateConfig:
    """Map flat `AutofeedConfig.organic_*` attributes onto a GateConfig."""
    cfg = GateConfig()
    for f in GateConfig.__dataclass_fields__:
        v = getattr(autofeed_cfg, f"organic_{f}", None)
        if v is not None:
            setattr(cfg, f, v)
    # Explicit aliases so the dashboard can use short, readable keys.
    for short, full in (("mode", "mode"),):
        v = getattr(autofeed_cfg, f"organic_gate_{short}", None)
        if v is not None:
            setattr(cfg, full, v)
    return cfg
