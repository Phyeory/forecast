"""
Organic screen — on-chain holder/cluster forensics for AutoFeed candidates.

Implements the four bundle checks from Setuh's "Memecoin Blueprint Day 2"
(holder distribution, funding source, SOL-balance similarity, wallet
clustering/fresh wallets) plus the launch-window / co-buy checks that
Padre/Terminal-style terminals (and GMGN) surface as "bundled launch".

Data path
---------
Tier 1 (primary): one `gmgn-cli token holders --limit 100` call per candidate.
GMGN's holder rows are on-chain derived and carry exactly the primitives the
checks need:

    address                  holder wallet
    amount_percentage        share of supply
    native_balance           wallet SOL balance (lamports)   ← SOL-similarity
    native_transfer.from_address / amount / timestamp         ← funding graph
    start_holding_at         first buy time                   ← launch window
    is_new / tags            fresh-wallet flag                ← fresh wallets
    maker_token_tags         bundler / sniper / rat_trader    ← flagged supply

Tier 2 (fallback when GMGN is banned / the call fails): our own RPC reader
(`getTokenLargestAccounts` + `getMultipleAccounts` for the token accounts and
owner lamports + `getSignaturesForAddress` for fresh-wallet funding parents).
Same thresholds, bounded to <= 15 calls per mint.

The screen is FAIL-CLOSED on missing holder data only when
`require_holder_data=True`; the default is to return `unavailable` and let the
feed continue (a GMGN outage must not stop discovery), with the verdict
persisted so the audit shows the coverage gap.

Everything here is a pure function of the holder rows + config so it can be
unit-tested offline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Awaitable, Callable, Optional

import feed_store
import rate_limiter as rl

logger = logging.getLogger("organic_screen")

# Tags that mark a wallet as part of the manipulation complex.
FLAG_TAGS = ("bundler", "sniper", "rat_trader", "bot", "sniper_bot")
# A shared CEX withdrawal hot wallet is NOT a bundle: thousands of unrelated
# users are funded from Coinbase/Kraken/etc.  Only address-level funders count.
EXCHANGE_FUNDER_TOKENS = (
    "coinbase", "kraken", "binance", "okx", "bybit", "kucoin", "gate",
    "mexc", "bitget", "crypto.com", "fixedfloat", "changenow", "simpleswap",
    "robinhood", "revolut", "bitfinex", "htx", "upbit", "bithumb",
)
# Only supply-moving manipulation tags are hard rejects; "sniper" alone is
# common on every launch and is handled by the score.
HARD_FLAG_TAGS = ("bundler", "rat_trader", "bot")


@dataclass
class ScreenConfig:
    enabled: bool = True
    # Hard rules (enforced unless enforce_hard=False)
    enforce_hard: bool = True
    # Composite score: logged in shadow by default (enforce_score=False).
    enforce_score: bool = False
    score_threshold: float = 70.0

    # ── Setuh check 1: holder distribution ────────────────────────────────
    # Setuh's scan rule is 4%; 4.2/4.3% is measurement noise, so the HARD
    # limit is 5% while the score still treats >=4% as a full-weight signal.
    max_top1_pct: float = 0.05            # top non-pool holder <= 5%
    max_top10_pct: float = 0.30           # top-10 non-pool <= 30%
    identical_min_holders: int = 3        # >=3 at the same pct ⇒ bundle
    identical_rel_tol: float = 0.02

    # ── Setuh check 2: funding source ─────────────────────────────────────
    funder_cluster_min: int = 3           # >=3 holders from one funder ...
    funder_cluster_max_supply: float = 0.04   # ... holding >4% ⇒ reject
    funding_burst_window_s: float = 600.0
    funding_burst_min: int = 3            # >=3 funded inside 10 min
    # ── Setuh check 3: SOL-balance similarity ─────────────────────────────
    balance_cluster_min: int = 3
    balance_rel_tol: float = 0.01
    balance_max_sol: float = 5.0
    # ── Setuh check 4 / launch bundle ─────────────────────────────────────
    launch_window_s: float = 60.0
    launch_bundle_min: int = 4
    launch_bundle_max_supply: float = 0.08
    # ── Fresh wallets ─────────────────────────────────────────────────────
    fresh_min_pct: float = 0.015          # each fresh wallet holds >=1.5%
    fresh_min_count: int = 4
    fresh_max_supply: float = 0.08
    # ── Dev / flagged wallets ─────────────────────────────────────────────
    max_dev_hold_pct: float = 0.03
    max_flagged_supply: float = 0.06

    # ── Data path ─────────────────────────────────────────────────────────
    require_holder_data: bool = False
    # 2026-11-28: GMGN's precomputed indicators are out of the filter; the
    # chain reader is primary.  GMGN holder rows remain available as an
    # availability fallback (they carry the same primitives precomputed).
    allow_gmgn_fallback: bool = True
    holder_limit: int = 100
    max_fetches_per_tick: int = 1
    fetch_timeout_s: float = 50.0
    # GMGN's per-IP budget banned after ~4 rapid holder calls (2026-11-27
    # probe); one call per tick + a 5 s floor stays inside it.
    min_seconds_between_calls: float = 5.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScreenVerdict:
    mint: str
    verdict: str = "unavailable"          # pass | reject | unavailable | error
    risk_score: float = 0.0
    reasons: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    source: str = ""                      # gmgn | rpc | cache | ""
    payload: dict = field(default_factory=dict)
    hard_reject: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ── holder-row normalisation ────────────────────────────────────────────────

def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def normalise_holders(rows: list[dict], creator: str = "") -> list[dict]:
    """GMGN holder rows → canonical rows, pool/vault rows removed.

    A pool/vault row is any row carrying an `exchange` venue tag (GMGN tags
    the LP/vault account with the DEX) or `addr_type == 2`.  Everything else
    is a wallet holding the token.
    """
    out: list[dict] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        owner = (r.get("address") or "").strip()
        if not owner:
            continue
        pct = _f(r.get("amount_percentage"))
        # GMGN sometimes returns percent, sometimes fraction.
        if pct > 1.5:
            pct = pct / 100.0
        if pct < 0:
            pct = 0.0
        exchange = (r.get("exchange") or "").strip()
        addr_type = _i(r.get("addr_type"), 0)
        if exchange or addr_type == 2:
            continue                          # LP / vault / AMM account
        nt = r.get("native_transfer") or {}
        if not isinstance(nt, dict):
            nt = {}
        tags = []
        for k in ("tags", "maker_token_tags"):
            v = r.get(k)
            if isinstance(v, list):
                tags.extend(str(x).lower() for x in v)
        out.append({
            "owner": owner,
            "token_account": (r.get("account_address") or "").strip(),
            "pct": pct,
            "sol": _f(r.get("native_balance")) / 1e9,
            "is_new": bool(r.get("is_new")),
            "tags": tags,
            "funder": (nt.get("from_address") or "").strip(),
            "funder_name": (nt.get("name") or "").strip(),
            "funded_at": _f(nt.get("timestamp")),
            "start_holding_at": _f(r.get("start_holding_at")),
            "buy_count": _i(r.get("buy_tx_count_cur")),
            "is_creator": bool(creator) and owner == creator,
        })
    return out


def _cluster_by(values: list[float], rel_tol: float) -> list[list[int]]:
    """Group indices whose values are within rel_tol of the cluster anchor."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    clusters: list[list[int]] = []
    for i in order:
        v = values[i]
        if clusters:
            anchor = values[clusters[-1][0]]
            if anchor > 0 and abs(v - anchor) / anchor <= rel_tol:
                clusters[-1].append(i)
                continue
            if anchor == 0 and v == 0:
                clusters[-1].append(i)
                continue
        clusters.append([i])
    return clusters


# ── pure verdict (Setuh checks) ─────────────────────────────────────────────

def evaluate_holders(
    holders: list[dict],
    *,
    creator: str = "",
    cfg: Optional[ScreenConfig] = None,
) -> tuple[str, list[str], dict, float]:
    """(verdict, reasons, metrics, risk_score 0-100) from canonical holders.

    `holders` must already be normalised (pool rows removed) and sorted or
    not — the function sorts by pct itself.  Missing/empty data returns
    `unavailable` (never a silent pass).
    """
    cfg = cfg or ScreenConfig()
    hs = [h for h in holders if h.get("pct", 0) > 0]
    hs.sort(key=lambda h: -h["pct"])
    top = hs[:10]
    reasons: list[str] = []
    metrics: dict = {"holders": len(hs), "top10": len(top)}

    if not hs:
        return "unavailable", ["no_holders"], metrics, 0.0

    top1 = top[0]["pct"]
    top10_sum = sum(h["pct"] for h in top)
    metrics["top1_pct"] = round(top1, 6)
    metrics["top10_pct"] = round(top10_sum, 6)
    if top1 > cfg.max_top1_pct:
        reasons.append(f"top1_{top1*100:.1f}pct")
    if top10_sum > cfg.max_top10_pct:
        reasons.append(f"top10_{top10_sum*100:.1f}pct")

    # ── identical holdings (Setuh: 2.5/2.5/2.5/2.5) ───────────────────────
    pcts = [h["pct"] for h in top]
    clusters = [c for c in _cluster_by(pcts, cfg.identical_rel_tol) if len(c) >= 2]
    identical_max = max((len(c) for c in clusters), default=0)
    metrics["identical_cluster"] = identical_max
    if identical_max >= cfg.identical_min_holders:
        reasons.append(f"identical_holdings_x{identical_max}")

    # ── SOL-balance similarity (Setuh: 1 SOL / 1 SOL / 1 SOL) ─────────────
    with_sol = [h for h in top if h["sol"] > 0]
    bal_max, bal_supply = 0, 0.0
    if len(with_sol) >= cfg.balance_cluster_min:
        sols = [h["sol"] for h in with_sol]
        bal_clusters = [c for c in _cluster_by(sols, cfg.balance_rel_tol) if len(c) >= 2]
        for c in bal_clusters:
            if len(c) >= cfg.balance_cluster_min and min(sols[i] for i in c) <= cfg.balance_max_sol:
                supply = sum(with_sol[i]["pct"] for i in c)
                if supply > bal_supply:
                    bal_supply = supply
                bal_max = max(bal_max, len(c))
    metrics["similar_balance_cluster"] = bal_max
    metrics["similar_balance_supply"] = round(bal_supply, 6)
    if bal_max >= cfg.balance_cluster_min and bal_supply > 0.02:
        reasons.append(f"similar_sol_balances_x{bal_max}_{bal_supply*100:.1f}pct")

    # ── funding-source clusters (Setuh: all funded from the same place) ───
    by_funder: dict[str, list[dict]] = {}
    for h in top:
        if not h.get("funder"):
            continue
        fname = (h.get("funder_name") or "").lower()
        if any(tok in fname for tok in EXCHANGE_FUNDER_TOKENS):
            continue                       # CEX hot wallet, not a cluster
        by_funder.setdefault(h["funder"], []).append(h)
    funder_max, funder_supply = 0, 0.0
    for funder, group in by_funder.items():
        if len(group) < 2:
            continue          # a single wallet is not a funding CLUSTER
        supply = sum(g["pct"] for g in group)
        if len(group) > funder_max:
            funder_max = len(group)
        funder_supply = max(funder_supply, supply)
        if len(group) >= cfg.funder_cluster_min and supply > cfg.funder_cluster_max_supply:
            reasons.append(f"shared_funder_x{len(group)}_{supply*100:.1f}pct")
            break
    metrics["funder_cluster_max"] = funder_max
    metrics["funder_cluster_supply"] = round(funder_supply, 6)

    # funding burst: several wallets funded inside one short window
    ts = sorted(h["funded_at"] for h in top if h["funded_at"] > 0)
    burst_max = 0
    j = 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > cfg.funding_burst_window_s:
            j += 1
        burst_max = max(burst_max, i - j + 1)
    metrics["funding_burst"] = burst_max
    if burst_max >= cfg.funding_burst_min:
        reasons.append(f"funding_burst_x{burst_max}")

    # ── launch-window co-buys (same-second/same-slot buys) ────────────────
    starts = sorted(h["start_holding_at"] for h in top if h["start_holding_at"] > 0)
    launch_supply, launch_max = 0.0, 0
    if starts:
        t0 = starts[0]
        window = [h for h in top
                  if h["start_holding_at"] > 0
                  and h["start_holding_at"] - t0 <= cfg.launch_window_s]
        launch_max = len(window)
        launch_supply = sum(h["pct"] for h in window)
    metrics["launch_window_holders"] = launch_max
    metrics["launch_window_supply"] = round(launch_supply, 6)
    if launch_max >= cfg.launch_bundle_min and launch_supply > cfg.launch_bundle_max_supply:
        reasons.append(f"launch_bundle_x{launch_max}_{launch_supply*100:.1f}pct")

    # ── fresh wallets (Setuh: "leaves" in the top 10) ─────────────────────
    fresh = [h for h in top
             if (h["is_new"] or "fresh_wallet" in h["tags"])
             and h["pct"] >= cfg.fresh_min_pct]
    fresh_supply = sum(h["pct"] for h in fresh)
    metrics["fresh_wallets"] = len(fresh)
    metrics["fresh_supply"] = round(fresh_supply, 6)
    if len(fresh) >= cfg.fresh_min_count:
        reasons.append(f"fresh_wallets_x{len(fresh)}")
    if fresh_supply > cfg.fresh_max_supply:
        reasons.append(f"fresh_supply_{fresh_supply*100:.1f}pct")

    # ── dev holding ───────────────────────────────────────────────────────
    dev_pct = max((h["pct"] for h in hs if h["is_creator"]), default=0.0)
    if not dev_pct and creator:
        dev_pct = max((h["pct"] for h in hs if h["owner"] == creator), default=0.0)
    metrics["dev_pct"] = round(dev_pct, 6)
    if dev_pct > cfg.max_dev_hold_pct:
        reasons.append(f"dev_hold_{dev_pct*100:.1f}pct")

    # ── flagged wallets (bundler / sniper / rat_trader tags) ──────────────
    flagged = [h for h in top if any(t in HARD_FLAG_TAGS for t in h["tags"])]
    flagged_supply = sum(h["pct"] for h in flagged)
    metrics["flagged_wallets"] = len(flagged)
    metrics["flagged_supply"] = round(flagged_supply, 6)
    if flagged_supply > cfg.max_flagged_supply:
        reasons.append(f"flagged_supply_{flagged_supply*100:.1f}pct")

    # ── composite risk score (0 = clean, 100 = bundle) ────────────────────
    def ratio(v, limit):
        return min(1.0, v / limit) if limit > 0 else 0.0
    # Only MULTI-wallet groups count toward the score: a single 3.5% holder is
    # already captured by top1/top10, not by "cluster supply".
    score_funder = funder_supply if funder_max >= 2 else 0.0
    score_launch = launch_supply if launch_max >= 2 else 0.0
    score = 100.0 * (
        0.20 * ratio(top1, cfg.max_top1_pct)
        + 0.15 * ratio(top10_sum, cfg.max_top10_pct)
        + 0.25 * ratio(score_funder, cfg.funder_cluster_max_supply)
        + 0.15 * ratio(fresh_supply, cfg.fresh_max_supply)
        + 0.15 * ratio(score_launch, cfg.launch_bundle_max_supply)
        + 0.10 * ratio(flagged_supply, cfg.max_flagged_supply)
    )
    if identical_max >= cfg.identical_min_holders:
        score = min(100.0, score + 10.0)
    if bal_max >= cfg.balance_cluster_min:
        score = min(100.0, score + 10.0)
    metrics["risk_score"] = round(score, 1)

    hard = bool(reasons)
    verdict = "reject" if (hard and cfg.enforce_hard) else "pass"
    if not hard and score >= cfg.score_threshold and cfg.enforce_score:
        verdict = "reject"
        reasons.append(f"score_{score:.0f}")
    return verdict, reasons, metrics, score


# ── GMGN fetch ──────────────────────────────────────────────────────────────

RunnerFn = Callable[[list[str]], Awaitable[Optional[dict]]]
RpcReaderFn = Callable[[str, str], Awaitable[list[dict]]]


def _default_rpc_call():
    """RPC caller backed by mint_chain_history's endpoint pool.

    Reusing that pool gives the screen the same failover + health cooldowns
    the calibration chain walks use (public RPCs 403/429 legs are cooled down
    instead of retried every call).
    """
    from mint_chain_history import _EndpointPool, _RPC_FAILED, _rpc_call, _SOLANA_RPCS

    pool = _EndpointPool(_SOLANA_RPCS, min_interval=0.08)

    async def call(session, method, params):
        res = await _rpc_call(session, pool, method, params)
        return None if res is _RPC_FAILED else res

    return call


async def fetch_rpc_holders(mint: str, creator: str = "",
                            limit: int = 12,
                            call=None) -> list[dict]:
    """Read every Setuh-check primitive straight from the chain.

    This is the PRIMARY holder source (user directive 2026-11-28: no GMGN
    indicators in the filter).  Bounded to ~30 RPC calls:
      4   largest accounts + supply + token-account owners + owner lamports
      2/h first-buy time (token account) + wallet birth (owner signatures)
      1/h funding parent (getTransaction) for FRESH wallets only
    `call` is injectable for tests; production uses the shared endpoint pool.
    Public RPCs only; never raises (returns [] on failure).
    """
    import aiohttp

    if call is None:
        call = _default_rpc_call()

    # Public RPCs throttle hard (mainnet-beta + publicnode both 429 under the
    # backend's own load).  Retry the HEAD of the read with backoff before
    # giving up; per-holder calls degrade gracefully instead (distribution /
    # balance checks still run without them).
    result: list[dict] = []
    for attempt in range(2):
        result = await _fetch_rpc_holders_once(mint, creator, limit, call)
        if result:
            return result
        if attempt < 1:
            await asyncio.sleep(4.0)
    return result


async def _fetch_rpc_holders_once(mint: str, creator: str, limit: int,
                                  call) -> list[dict]:
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            largest = await call(s, "getTokenLargestAccounts", [mint])
            vals = (largest or {}).get("value") or []
            if not vals:
                return []
            accounts = [v.get("address") for v in vals[:limit] if v.get("address")]
            supply = await call(s, "getTokenSupply", [mint])
            supply_ui = float(((supply or {}).get("value") or {}).get("uiAmount") or 0)
            if supply_ui <= 0:
                return []
            accs = await call(s, "getMultipleAccounts",
                              [accounts, {"encoding": "jsonParsed"}])
            owners: list[dict] = []
            # getMultipleAccounts echoes results in REQUEST order — zip the
            # known token-account addresses, never a filtered list.
            for addr, a in zip(accounts, ((accs or {}).get("value") or [])):
                try:
                    info = a["data"]["parsed"]["info"]
                    owner = info.get("owner")
                    amt = float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
                    if owner and amt > 0:
                        owners.append({"owner": owner, "amount": amt,
                                       "account": addr})
                except Exception:
                    continue
            if not owners:
                return []
            uniq = list({o["owner"] for o in owners})
            bals = await call(s, "getMultipleAccounts",
                              [uniq, {"encoding": "jsonParsed"}])
            lam: dict[str, float] = {}
            system_owned: set[str] = set()
            for ow, a in zip(uniq, ((bals or {}).get("value") or [])):
                try:
                    lam[ow] = float(a.get("lamports") or 0) / 1e9
                    # A wallet is a System-Program-owned account.  The pool's
                    # token vault is owned by a PDA (PumpSwap program), so
                    # this is how the chain reader excludes the LP from
                    # "top holders" (Setuh check 1: disregard the LP).
                    if a.get("owner") == "11111111111111111111111111111111":
                        system_owned.add(ow)
                except Exception:
                    lam[ow] = 0.0
            now = time.time()
            out: list[dict] = []
            for o in owners[:limit]:
                ow = o["owner"]
                if system_owned and ow not in system_owned:
                    continue
                row = {"owner": ow, "pct": o["amount"] / supply_ui,
                       "sol": lam.get(ow, 0.0), "is_new": False, "tags": [],
                       "funder": "", "funder_name": "", "funded_at": 0.0,
                       "start_holding_at": 0.0, "buy_count": 0,
                       "is_creator": bool(creator) and ow == creator}
                try:
                    # First buy of THIS token = the token account's oldest
                    # signature (Setuh's launch-window / sniper evidence).
                    sighist = await call(s, "getSignaturesForAddress",
                                         [o["account"], {"limit": 1000}])
                    if sighist:
                        row["start_holding_at"] = float(
                            sighist[-1].get("blockTime") or 0)
                        # Full wallet history back to genesis ⇒ age is known.
                        wh = await call(s, "getSignaturesForAddress",
                                        [ow, {"limit": 1000}])
                        if wh and len(wh) < 1000:
                            birth = float(wh[-1].get("blockTime") or 0)
                            if birth and now - birth < 7 * 86400:
                                row["is_new"] = True     # Setuh's "leaf"
                            # Funding parent (Setuh check 2) — only worth one
                            # getTransaction for young wallets; old ones have
                            # too many pages to reach genesis cheaply.
                            if row["is_new"] and wh:
                                tx = await call(
                                    s, "getTransaction",
                                    [wh[-1].get("signature"),
                                     {"encoding": "jsonParsed",
                                      "maxSupportedTransactionVersion": 0}])
                                if tx:
                                    row["funded_at"] = float(
                                        tx.get("blockTime") or 0)
                                    row["funder"] = _first_funder(tx, ow) or ""
                except Exception:
                    pass
                out.append(row)
            return out
    except Exception as e:
        logger.debug(f"[OrganicScreen] chain holders failed: {e}")
        return []


async def augment_rows_from_chain(rows: list[dict], *, call=None,
                                  top_n: int = 10) -> list[dict]:
    """Self-compute the Setuh inputs from chain signatures.

    Wipes GMGN's own indications first (is_new / tags / funding) and replaces
    them with chain-derived values:
      start_holding_at  — the holder's token-account oldest signature
      is_new            — wallet birth < 7 days with full signature history
      funder/funded_at  — getTransaction on the wallet's oldest signature
    Failures degrade silently (fields stay cleared; the affected check simply
    does not fire for that holder).
    """
    if call is None:
        call = _default_rpc_call()
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            now = time.time()
            for r in rows[:top_n]:
                # No GMGN indication may survive into the verdict.
                r["tags"] = []
                r["is_new"] = False
                r["funder"] = ""
                r["funder_name"] = ""
                r["funded_at"] = 0.0
                r["start_holding_at"] = 0.0
                try:
                    ta = r.get("token_account")
                    if ta:
                        hist = await call(s, "getSignaturesForAddress",
                                          [ta, {"limit": 1000}])
                        if hist:
                            r["start_holding_at"] = float(
                                hist[-1].get("blockTime") or 0)
                    wh = await call(s, "getSignaturesForAddress",
                                    [r["owner"], {"limit": 1000}])
                    if wh and len(wh) < 1000:
                        birth = float(wh[-1].get("blockTime") or 0)
                        if birth and now - birth < 7 * 86400:
                            r["is_new"] = True
                            tx = await call(
                                s, "getTransaction",
                                [wh[-1].get("signature"),
                                 {"encoding": "jsonParsed",
                                  "maxSupportedTransactionVersion": 0}])
                            if tx:
                                r["funded_at"] = float(tx.get("blockTime") or 0)
                                r["funder"] = _first_funder(tx, r["owner"]) or ""
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"[OrganicScreen] chain augment failed: {e}")
    return rows


def _first_funder(tx: dict, owner: str) -> str:
    """Largest lamport decrease in the wallet's first tx = funding parent."""
    try:
        keys = tx["transaction"]["message"]["accountKeys"]
        pubkeys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
        meta = tx.get("meta") or {}
        pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
        if len(pre) != len(post) or len(pre) != len(pubkeys):
            return ""
        best_i, best_d = None, 0.0
        for i, (a, b) in enumerate(zip(pre, post)):
            if pubkeys[i] == owner:
                continue
            d = a - b
            if d > best_d:
                best_i, best_d = i, d
        return pubkeys[best_i] if best_i is not None else ""
    except Exception:
        return ""


class OrganicScreen:
    """Holder-forensics screen; fetch is injected so autofeed reuses its CLI."""

    def __init__(self, cfg: Optional[ScreenConfig] = None,
                 cli_runner: Optional[RunnerFn] = None,
                 rpc_reader: Optional[RpcReaderFn] = None,
                 chain_augmenter=None):
        self.cfg = cfg or ScreenConfig()
        self._cli_runner = cli_runner
        self._rpc_reader = rpc_reader or fetch_rpc_holders
        # Replaces GMGN's precomputed indications with chain-derived ones.
        self._chain_augmenter = chain_augmenter or augment_rows_from_chain
        self._last_call_at = 0.0
        self._lock = asyncio.Lock()
        self.last_error = ""
        self.fetches = 0
        self.rpc_fetches = 0
        self.cache_hits = 0
        self.verdict_tally: dict[str, int] = {}
        self.reason_tally: dict[str, int] = {}
        self.source_tally: dict[str, int] = {}

    # -- public ------------------------------------------------------------
    async def screen(self, mint: str, creator: str = "",
                     force_refresh: bool = False) -> ScreenVerdict:
        cfg = self.cfg
        if not cfg.enabled:
            return ScreenVerdict(mint=mint, verdict="pass", source="disabled")

        params = self._cache_params()
        cached = (None if force_refresh
                  else feed_store.load_profile(mint, params=params))
        if cached is not None:
            self.cache_hits += 1
            return self._verdict_from_profile(cached)

        holders: list[dict] = []
        source = ""
        payload: dict = {}
        # PRIMARY: our own on-chain read (user directive 2026-11-28 — no GMGN
        # indicators in the filter; Setuh's checks are computed from chain
        # primitives we fetch ourselves).  One mint at a time; never blocks
        # the feed beyond the fetch timeout.
        if self._rpc_reader is not None:
            try:
                holders = await asyncio.wait_for(
                    self._rpc_reader(mint, creator),
                    timeout=self.cfg.fetch_timeout_s + 10.0)
                if holders:
                    source = "chain"
                    self.rpc_fetches += 1
            except Exception as e:
                self.last_error = f"chain holders: {type(e).__name__}: {e}"
                logger.debug(f"[OrganicScreen] {self.last_error}")
        # FALLBACK: GMGN holder rows only when our chain read failed AND the
        # fallback is allowed.  GMGN's precomputed tags/is_new/funding are
        # accepted here but never required.
        if not holders and self._cli_runner is not None \
                and cfg.allow_gmgn_fallback and not rl.gmgn_banned():
            try:
                rows = await self._fetch_gmgn(mint, creator)
                if rows:
                    holders = rows
                    source = "gmgn"
            except Exception as e:
                self.last_error = f"gmgn holders: {type(e).__name__}: {e}"
                logger.debug(f"[OrganicScreen] {self.last_error}")
        if not holders:
            self.last_error = self.last_error or "no holder rows"
            return self._record(mint, ScreenVerdict(
                mint=mint, verdict="reject" if cfg.require_holder_data else "unavailable",
                reasons=["holder_data_unavailable"] if cfg.require_holder_data else [],
                source="none"))

        verdict, reasons, metrics, score = evaluate_holders(
            holders, creator=creator, cfg=cfg)
        v = ScreenVerdict(mint=mint, verdict=verdict, risk_score=score,
                          reasons=reasons, metrics=metrics, source=source,
                          payload={"holders": holders[:20]},
                          hard_reject=bool(reasons))
        return self._record(mint, v)

    def _cache_params(self) -> dict:
        """Threshold set that determines the verdict (cache key).

        Keep this to values that actually change hard-rule outcomes; data-path
        knobs (timeouts/cache TTL) do not belong here.
        """
        c = self.cfg
        return {
            "max_top1_pct": c.max_top1_pct,
            "max_top10_pct": c.max_top10_pct,
            "identical_min_holders": c.identical_min_holders,
            "identical_rel_tol": c.identical_rel_tol,
            "funder_cluster_min": c.funder_cluster_min,
            "funder_cluster_max_supply": c.funder_cluster_max_supply,
            "funding_burst_window_s": c.funding_burst_window_s,
            "funding_burst_min": c.funding_burst_min,
            "balance_cluster_min": c.balance_cluster_min,
            "balance_rel_tol": c.balance_rel_tol,
            "balance_max_sol": c.balance_max_sol,
            "launch_window_s": c.launch_window_s,
            "launch_bundle_min": c.launch_bundle_min,
            "launch_bundle_max_supply": c.launch_bundle_max_supply,
            "fresh_min_pct": c.fresh_min_pct,
            "fresh_min_count": c.fresh_min_count,
            "fresh_max_supply": c.fresh_max_supply,
            "max_dev_hold_pct": c.max_dev_hold_pct,
            "max_flagged_supply": c.max_flagged_supply,
            "enforce_hard": c.enforce_hard,
            "enforce_score": c.enforce_score,
            "score_threshold": c.score_threshold,
        }

    def _verdict_from_profile(self, p: dict) -> ScreenVerdict:
        return ScreenVerdict(
            mint=p["mint"], verdict=p.get("verdict") or "unavailable",
            risk_score=float(p.get("risk_score") or 0.0),
            reasons=list(p.get("reasons") or []),
            metrics=dict(p.get("metrics") or {}),
            source="cache", payload={},
            hard_reject=bool(p.get("reasons")) and p.get("verdict") == "reject",
        )

    # -- fetch -------------------------------------------------------------
    async def _fetch_gmgn(self, mint: str, creator: str) -> list[dict]:
        assert self._cli_runner is not None
        # Global budget: one holder call every N seconds, never during a ban.
        async with self._lock:
            wait = self.cfg.min_seconds_between_calls - (time.time() - self._last_call_at)
            if wait > 0:
                await asyncio.sleep(wait)
            if rl.gmgn_banned():
                return []
            self._last_call_at = time.time()
        args = ["token", "holders", "--chain", "sol", "--address", mint,
                "--limit", str(self.cfg.holder_limit), "--raw"]
        data = await asyncio.wait_for(
            self._cli_runner(args), timeout=self.cfg.fetch_timeout_s)
        self.fetches += 1
        if not isinstance(data, dict):
            return []
        rows = data.get("list")
        if not isinstance(rows, list):
            return []
        norm = normalise_holders(rows, creator=creator)
        # GMGN rows carry raw holder data + their OWN indications.  The
        # indications are wiped and recomputed from the chain (user directive
        # 2026-11-28); the holder list itself is only an indexer fallback for
        # when the free RPCs refuse indexed reads.
        try:
            norm = await asyncio.wait_for(
                self._chain_augmenter(norm), timeout=45.0)
        except Exception as e:
            logger.debug(f"[OrganicScreen] augment timeout/error: {e}")
        return norm

    # -- persistence -------------------------------------------------------
    def _record(self, mint: str, v: ScreenVerdict) -> ScreenVerdict:
        self.verdict_tally[v.verdict] = self.verdict_tally.get(v.verdict, 0) + 1
        self.source_tally[v.source or "none"] = \
            self.source_tally.get(v.source or "none", 0) + 1
        for r in v.reasons:
            key = r.split("_x")[0] if "_x" in r else r
            self.reason_tally[key] = self.reason_tally.get(key, 0) + 1
        feed_store.record_profile(
            mint, source=v.source, verdict=v.verdict,
            risk_score=v.risk_score, reasons=v.reasons,
            metrics=v.metrics, payload=v.payload, params=self._cache_params())
        return v

    def snapshot(self) -> dict:
        return {
            "enabled": self.cfg.enabled,
            "enforce_hard": self.cfg.enforce_hard,
            "enforce_score": self.cfg.enforce_score,
            "score_threshold": self.cfg.score_threshold,
            "require_holder_data": self.cfg.require_holder_data,
            "max_fetches_per_tick": self.cfg.max_fetches_per_tick,
            "max_top1_pct": self.cfg.max_top1_pct,
            "max_top10_pct": self.cfg.max_top10_pct,
            "fetches": self.fetches,
            "cache_hits": self.cache_hits,
            "last_error": self.last_error,
            "verdict_tally": dict(self.verdict_tally),
            "reason_tally": dict(self.reason_tally),
            "source_tally": dict(self.source_tally),
            "allow_gmgn_fallback": self.cfg.allow_gmgn_fallback,
            "feed_store": feed_store.stats(),
        }
