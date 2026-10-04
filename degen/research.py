"""DEGEN research toolkit — the trader's "tabs", all computed by our code.

Every tool returns a compact JSON-able dict.  Vendor risk scores (RugCheck,
GoPlus) ride along as CROSS-CHECK signals only — our own forensics decide.
The decision model consumes the merged report, never raw vendor verdicts.

Forensics implemented here (Setuh's manual checks → programmatic):
  * holder distribution: >4% single non-LP holder, top-10 share,
    identical-remaining-% fingerprint, identical-SOL-balance fingerprint
  * sellability: real Jupiter sell quote + price-impact sanity (honeypot)
  * organic: DexScreener txn mix, volume vs liquidity, pair age
  * dev history: prior tokens by creator (experimental — best effort)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from solders.pubkey import Pubkey

from live_trader import SOLANA_RPCS

logger = logging.getLogger("degen.research")

RPC = SOLANA_RPCS[0]
PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}
RUGCHECK_SUMMARY = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
GOPLUS_SOL = "https://api.gopluslabs.io/api/v1/solana/token_security?contract_addresses={mint}"

TOOL_TIMEOUT_S = 8.0


# ── low-level RPC helpers ────────────────────────────────────────────────────

async def _rpc(session: aiohttp.ClientSession, method: str, params: list) -> Optional[dict]:
    payload = {"jsonrpc": "2.0", "id": "degen-research", "method": method, "params": params}
    for attempt in range(2):
        try:
            async with session.post(RPC, json=payload) as resp:
                data = await resp.json(content_type=None)
                if "error" in data:
                    logger.debug("[DegenResearch] rpc %s error: %s", method, data["error"])
                    return None
                return data.get("result")
        except Exception as e:
            if attempt == 1:
                logger.debug("[DegenResearch] rpc %s failed: %s", method, e)
                return None
            await asyncio.sleep(0.3)
    return None


def bonding_curve_pda(mint: str) -> str:
    try:
        addr, _ = Pubkey.find_program_address([b"bonding-curve", Pubkey.from_string(mint)], PUMP_PROGRAM)
        return str(addr)
    except Exception:
        return ""


# ── token meta ───────────────────────────────────────────────────────────────

async def token_meta(mint: str) -> dict:
    """Name/symbol/socials/supply via the repo's resolver (DexScreener + pump.fun)."""
    from pumpfun_client import get_token_info
    try:
        info = await asyncio.wait_for(get_token_info(mint), timeout=TOOL_TIMEOUT_S)
    except Exception:
        info = None
    if not info:
        return {"mint": mint, "available": False}
    return {
        "mint": mint,
        "available": True,
        "name": info.get("name", ""),
        "symbol": info.get("symbol", ""),
        "socials": {k: info.get(k, "") for k in ("twitter", "telegram", "website")},
        "mcap_usd": info.get("mcap_usd") or info.get("marketCap") or 0,
        "price_usd": info.get("price_usd") or 0,
        "source": info.get("_live_source", ""),
    }


# ── holder forensics (ours — the Setuh checks) ───────────────────────────────

async def holder_forensics(mint: str, bonding_curve_key: str = "") -> dict:
    """Top-holder distribution + bundle fingerprints from raw chain data.

    Setuh's checks, programmatic:
      * any single non-LP holder > 4%          → hard_reject
      * top-10 non-LP share > 50%              → warning
      * ≥3 top holders with identical remaining-% (±0.05pp) → bundle fingerprint
      * ≥3 top holders with identical SOL balance (±0.001 SOL) → bundle fingerprint
    """
    out: dict = {"tool": "holder_forensics", "available": False,
                 "hard_rejects": [], "warnings": [], "fingerprints": []}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TOOL_TIMEOUT_S)) as sess:
        supply_res = await _rpc(sess, "getTokenSupply", [mint])
        if not supply_res:
            return out
        decimals = int(supply_res["value"]["decimals"])
        supply_raw = int(supply_res["value"]["amount"])

        largest = await _rpc(sess, "getTokenLargestAccounts", [mint])
        if not largest:
            return out
        accounts = largest.get("value", [])[:20]
        if not accounts:
            return out

        owners = []
        for acc in accounts:
            owners.append(acc["address"])
        infos = await _rpc(sess, "getMultipleAccounts", [owners, {"encoding": "jsonParsed", "dataSlice": {"offset": 0, "length": 0}}]) or {}
        acct_list = infos.get("value", [])

        exclude = {bonding_curve_key, bonding_curve_pda(mint)} | BURN_ADDRESSES
        rows = []  # (token_account, owner, amount_raw, owner_sol)
        for acc, ainfo in zip(accounts, acct_list):
            if acc["address"] in exclude:
                continue
            amount_raw = int(acc.get("amount", "0") or acc.get("uiAmount") or 0)
            owner = ""
            owner_sol = None
            if ainfo:
                parsed = (ainfo.get("data") or {}).get("parsed")
                if parsed and isinstance(parsed, dict):
                    owner = ((parsed.get("info") or {}).get("owner")) or ""
                owner_sol = (ainfo.get("lamports") or 0) / 1e9
            rows.append((acc["address"], owner, amount_raw, owner_sol))

        if supply_raw <= 0 or not rows:
            return out

        pcts = [amt / supply_raw * 100 for _, _, amt, _ in rows]
        top10 = pcts[:10]
        top10_share = sum(top10)
        max_single = pcts[0] if pcts else 0.0

        # fingerprint: identical remaining-% cohorts
        pct_cohorts = 1
        s = sorted(top10)
        run = 1
        for a, b in zip(s, s[1:]):
            run = run + 1 if abs(a - b) <= 0.05 else 1
            pct_cohorts = max(pct_cohorts, run)

        # fingerprint: identical SOL balances across top holders
        sols = sorted([r[3] for r in rows[:10] if r[3] is not None])
        sol_cohorts = 1
        run = 1
        for a, b in zip(sols, sols[1:]):
            run = run + 1 if abs(a - b) <= 0.001 else 1
            sol_cohorts = max(sol_cohorts, run)

        out.update({
            "available": True,
            "supply": supply_raw,
            "decimals": decimals,
            "top10_share_pct": round(top10_share, 2),
            "max_single_non_lp_pct": round(max_single, 2),
            "holders_sampled": len(rows),
            "owner_sol_balances": [round(x, 3) for x in sols[:10]],
        })
        if max_single > 4.0:
            out["hard_rejects"].append(f"holder holds {max_single:.1f}% (>4% Setuh rule)")
        if top10_share > 50.0:
            out["warnings"].append(f"top-10 hold {top10_share:.0f}% of supply")
        if pct_cohorts >= 3:
            out["fingerprints"].append(f"{pct_cohorts} top holders with identical remaining-%")
        if sol_cohorts >= 3:
            out["fingerprints"].append(f"{sol_cohorts} top holders with identical SOL balances")
        return out


# ── sellability (honeypot check via a real sell quote) ───────────────────────

async def sellability(mint: str, supply_raw: Optional[int] = None) -> dict:
    """Quote selling ~1% of supply back to SOL.  No route or absurd impact = reject."""
    out = {"tool": "sellability", "available": False}
    amount_raw = int((supply_raw or 1_000_000_000_000_000) * 0.01) or 1_000_000_000
    params = {
        "inputMint": mint,
        "outputMint": "So11111111111111111111111111111111111111112",
        "amount": str(amount_raw),
        "slippageBps": "5000",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TOOL_TIMEOUT_S)) as sess:
            async with sess.get("https://api.jup.ag/swap/v1/quote", params=params) as resp:
                if resp.status != 200:
                    out["available"] = True
                    out["sellable"] = False
                    out["reason"] = f"no sell route (HTTP {resp.status})"
                    return out
                q = await resp.json()
        impact = float(q.get("priceImpactPct") or 0) * 100
        out.update({
            "available": True,
            "sellable": impact < 50.0,
            "price_impact_pct": round(impact, 3),
            "out_lamports": int(float(q.get("outAmount") or 0)),
        })
        if impact >= 50.0:
            out["reason"] = f"sell impact {impact:.0f}% — effectively unsellable"
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


# ── vendor cross-checks (signals only — never the verdict) ───────────────────

async def rugcheck_crosscheck(mint: str) -> dict:
    out = {"tool": "rugcheck", "available": False, "role": "cross-check"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TOOL_TIMEOUT_S)) as sess:
            async with sess.get(RUGCHECK_SUMMARY.format(mint=mint)) as resp:
                if resp.status != 200:
                    return out
                data = await resp.json()
        out["available"] = True
        out["score"] = data.get("score")
        out["score_normalised"] = data.get("score_normalised")
        out["risks"] = [r.get("name") for r in (data.get("risks") or [])][:8]
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


async def goplus_crosscheck(mint: str) -> dict:
    out = {"tool": "goplus", "available": False, "role": "cross-check"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TOOL_TIMEOUT_S)) as sess:
            async with sess.get(GOPLUS_SOL.format(mint=mint)) as resp:
                if resp.status != 200:
                    return out
                body = await resp.json()
        items = ((body or {}).get("result") or {})
        info = items.get(mint) or (next(iter(items.values())) if isinstance(items, dict) and items else None)
        if not info:
            return out
        out["available"] = True
        out.update({
            "honeypot": info.get("is_honeypot"),
            "transfer_fee": info.get("transfer_fee"),
            "metadata_modifiable": info.get("metadata_modifiable"),
            "mint_authority": info.get("mint_authority"),
            "freeze_authority": info.get("freeze_authority"),
        })
        return out
    except Exception as e:
        out["error"] = str(e)
        return out


# ── organic heuristics (DexScreener) ─────────────────────────────────────────

async def dexscreener_organic(mint: str) -> dict:
    out = {"tool": "dexscreener", "available": False, "warnings": []}
    from pumpfun_client import get_ds_pairs_by_mints
    try:
        pairs_map = await asyncio.wait_for(get_ds_pairs_by_mints([mint]), timeout=TOOL_TIMEOUT_S)
    except Exception:
        return out
    pairs = pairs_map.get(mint) or []
    if not pairs:
        return out
    best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    liq = float((best.get("liquidity") or {}).get("usd") or 0)
    vol = float((best.get("volume") or {}).get("h1") or 0)
    txns = (best.get("txns") or {}).get("h1") or {}
    buys, sells = int(txns.get("buys") or 0), int(txns.get("sells") or 0)
    created = best.get("pairCreatedAt") or 0
    out.update({
        "available": True,
        "liquidity_usd": liq,
        "volume_h1_usd": vol,
        "txns_h1": {"buys": buys, "sells": sells},
        "pair_age_min": (time.time() * 1000 - created) / 60000 if created else None,
        "price_change_h1": (best.get("priceChange") or {}).get("h1"),
    })
    if liq > 0 and vol / liq > 8.0:
        out["warnings"].append(f"vol/liq {vol / liq:.1f}x in 1h — churn heavy")
    if buys + sells > 0 and buys < sells * 0.25:
        out["warnings"].append("heavy sell skew in last hour")
    return out


# ── dev history (experimental — best effort, degrades to unavailable) ────────

async def dev_history(creator: str) -> dict:
    """Prior tokens by the creator.  pump.fun frontend APIs need JWT most days;
    try the public endpoints, else mark unavailable (tracked-dev lane needs this
    before it will arm)."""
    out = {"tool": "dev_history", "available": False, "creator": creator}
    if not creator:
        return out
    urls = [
        f"https://frontend-api-v3.pump.fun/coins/user-created-coins/{creator}?limit=10&offset=0",
        f"https://frontend-api.pump.fun/coins/user-created-coins/{creator}?limit=10&offset=0",
    ]
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TOOL_TIMEOUT_S),
                                     headers={"User-Agent": "Mozilla/5.0"}) as sess:
        for url in urls:
            try:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
                coins = data if isinstance(data, list) else (data or {}).get("coins") or []
                if coins is None:
                    continue
                prior = [{
                    "mint": c.get("mint"),
                    "symbol": c.get("symbol"),
                    "usd_market_cap": c.get("usd_market_cap") or c.get("market_cap"),
                    "complete": c.get("complete"),
                    "created_timestamp": c.get("created_timestamp"),
                } for c in coins[:10]]
                out["available"] = True
                out["prior_tokens"] = prior
                out["prior_count"] = len(prior)
                out["prior_graduated"] = sum(1 for p in prior if p.get("complete"))
                return out
            except Exception:
                continue
    return out


# ── orchestrator ─────────────────────────────────────────────────────────────

async def run_forensics(mint: str, birth: Optional[dict] = None,
                        creator: str = "") -> dict:
    """Run every tool concurrently, merge into one report for the decision model.

    `our_verdict` is OURS: hard rejects from our own forensics; vendor scores
    ride along as cross-checks only.
    """
    bc_key = (birth or {}).get("bonding_curve_key", "")
    tasks = {
        "meta": token_meta(mint),
        "holders": holder_forensics(mint, bonding_curve_key=bc_key),
        "sellability": sellability(mint),
        "rugcheck": rugcheck_crosscheck(mint),
        "goplus": goplus_crosscheck(mint),
        "organic": dexscreener_organic(mint),
        "dev": dev_history(creator or ((birth or {}).get("creator") or "")),
    }
    keys = list(tasks)
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    report = {"mint": mint, "ts": time.time(), "role": "forensics", "tools": {}}
    hard_rejects: list[str] = []
    warnings: list[str] = []
    for key, res in zip(keys, results):
        if isinstance(res, Exception):
            report["tools"][key] = {"tool": key, "available": False, "error": str(res)}
            continue
        report["tools"][key] = res
        hard_rejects.extend(res.get("hard_rejects") or [])
        warnings.extend(res.get("warnings") or [])

    # independent mint/freeze authority read — ours, not vendor's
    try:
        authorities = await _mint_freeze_authority(mint)
        report["authorities"] = authorities
        if authorities.get("mint_authority_active"):
            hard_rejects.append("mint authority still active")
        if authorities.get("freeze_authority_active"):
            hard_rejects.append("freeze authority still active")
    except Exception as e:
        report["authorities"] = {"error": str(e)}

    sell = report["tools"].get("sellability") or {}
    if sell.get("available") and not sell.get("sellable"):
        hard_rejects.append(sell.get("reason", "unsellable"))

    report["our_verdict"] = {
        "hard_rejects": hard_rejects,
        "warnings": warnings,
        "clean": not hard_rejects,
    }
    return report


async def _mint_freeze_authority(mint: str) -> dict:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TOOL_TIMEOUT_S)) as sess:
        res = await _rpc(sess, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    if not res or not res.get("value"):
        return {"available": False}
    data = res["value"].get("data") or {}
    parsed = (data.get("parsed") or {}).get("info") or {}
    return {
        "available": True,
        "mint_authority_active": bool(parsed.get("mintAuthority")),
        "freeze_authority_active": bool(parsed.get("freezeAuthority")),
        "decimals": parsed.get("decimals"),
    }
