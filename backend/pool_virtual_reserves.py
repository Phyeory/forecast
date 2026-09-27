"""PumpSwap virtual-quote-reserves basis fix (iter94, 2026-09-26).

Post-graduation PumpSwap pools price swaps on

    effective_quote_reserves = quote_vault + virtual_quote_reserves

where ``virtual_quote_reserves`` (V) is a protocol-level virtual liquidity
credit carried on current pools (measured constant V = 17.5845 SOL across all
2026-09 pools; stored as an appended i128 at pool-account offset 245).
PumpPortal's pumpswap trade events (``vSolInBondingCurve`` /
``vTokensInBondingCurve``) and PumpSwapRPCClient's vault-diff feed both report
the RAW VAULT ratio, so every tape price for a graduated token sat below the
executable price by (vault + V)/vault — the entire "live fill premium" of
iter91/iter93 with ZERO true slippage: on-chain, every live fill landed at
tape × wedge × fee (fee ≈ 1.25%/side) plus a one-time WSOL/token ATA rent on
a session's first buy (forensics: analysis/iter94_wedge_study.py — 94 fills,
sells median residual 0.9877 = fee, buys 1.0126 = fee).

This module resolves V per mint (DexScreener pumpswap pair → pool account
decode, cached in-process AND in a price_data.db sidecar table for
determinism across backtest pool workers) and is consumed by:

  - pumpfun_client.PumpFunWSClient._normalise / _SharedPumpPortalHub — live
    PumpPortal feed priced on the effective basis;
  - pumpfun_client.PumpSwapRPCClient._current_trade — vault-diff feed priced
    on the effective basis;
  - data_store.get_recording_candles — recordings started BEFORE
    PUMPSWAP_VR_FIX_EPOCH (recorded on the phantom vault basis) are
    corrected at load time using their stored per-candle vault depth
    (pool_sol), graduation-aware via the pool's pairCreatedAt.

Kill-switch: PUMPCHART_DISABLE_VR_FIX=1 disables every correction (stream,
vault-diff and loader) and restores the pre-iter94 phantom basis.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import struct
import time
import urllib.request
from typing import Optional

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
PRICE_DB_PATH = os.path.join(BACKEND_DIR, "data", "price_data.db")

# Deploy moment of the effective-basis feed (2026-09-26, set between the
# newest pre-fix recording and the first post-fix deploy).  Recordings
# started before this were stored on the phantom vault basis and are
# corrected at load time; recordings started after it were recorded on
# the effective basis already (stream fix live) and are never re-corrected.
PUMPSWAP_VR_FIX_EPOCH = 1790395000.0

# Measured protocol constant for current pools (sanity band for decodes).
PUMPSWAP_VR_EXPECTED_SOL = 17.584505637

DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search?q={q}"
RPC_URLS = [
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
]

POOL_DISCRIMINATOR = bytes.fromhex("f19a6d0411b16dbc")
# PumpSwap Pool layout (borsh, packed): disc(8) bump u8, index u16, creator,
# base_mint, quote_mint, lp_mint, base_vault, quote_vault (6×32), lp_supply
# u64, coin_creator(32), is_mayhem u8, is_cashback u8,
# virtual_quote_reserves i128 — total 261 bytes (accounts are rent-padded).
_POOL_VQ_OFFSET = 8 + 1 + 2 + 32 * 6 + 8 + 32 + 2
_POOL_MIN_LEN = _POOL_VQ_OFFSET + 16
_POOL_BASE_MINT_OFFSET = 8 + 1 + 2 + 32

_DISABLE = os.environ.get("PUMPCHART_DISABLE_VR_FIX", "") not in ("", "0", "false", "False")


def vr_fix_enabled() -> bool:
    return not _DISABLE


# ── Pool account decode ─────────────────────────────────────────────────────

def decode_virtual_quote_lamports(pool_data: bytes) -> int:
    """Return the pool's virtual_quote_reserves in lamports (0 when absent).

    Pools created before the virtual-liquidity rollout (and any account with
    a shorter layout) have no appended i128 — treat as 0, never negative.
    """
    if len(pool_data) < _POOL_MIN_LEN or not pool_data.startswith(POOL_DISCRIMINATOR):
        return 0
    vq = struct.unpack_from("<q", pool_data, _POOL_VQ_OFFSET)[0]
    if vq <= 0 or vq > 10_000 * 1e9:  # sanity: 10k SOL of virtual credit
        return 0
    return int(vq)


def _pool_base_mint(pool_data: bytes) -> Optional[str]:
    """Best-effort base58 of the pool's base_mint (layout validation)."""
    if len(pool_data) < _POOL_BASE_MINT_OFFSET + 32:
        return None
    raw = pool_data[_POOL_BASE_MINT_OFFSET:_POOL_BASE_MINT_OFFSET + 32]
    try:
        from solders.pubkey import Pubkey
        return str(Pubkey.from_bytes(raw))
    except Exception:
        return None


# ── Resolution (DexScreener pair → pool account → V) ─────────────────────────

_cache: dict[str, Optional[dict]] = {}
_resolve_ts: dict[str, float] = {}
_NEG_TTL_S = 6 * 3600.0     # retry failed resolves after 6 h
_POS_TTL_S = 14 * 86400.0   # re-confirm positive resolves after 2 weeks


def _http_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(
        url, headers={"Content-Type": "application/json",
                      "User-Agent": "pump-chart/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _rpc_get_account_b64(address: str) -> Optional[bytes]:
    for url in RPC_URLS:
        try:
            res = _post_json(url, "getAccountInfo", [address, {"encoding": "base64"}])
            val = (res or {}).get("result", {}).get("value")
            if val and val.get("data"):
                return base64.b64decode(val["data"][0])
        except Exception:
            continue
    return None


def _post_json(url: str, method: str, params: list) -> Optional[dict]:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "pump-chart/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _sidecar_load(mint: str) -> Optional[dict]:
    """Load a previously resolved row from the price_data sidecar table."""
    try:
        conn = sqlite3.connect(f"file:{PRICE_DB_PATH}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT v_sol, pool_address, pair_created_at FROM pool_virtual_resolves "
                "WHERE mint = ?", (mint,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "pool_address": row["pool_address"],
            "v_sol": float(row["v_sol"] or 0.0),
            "pair_created_at": float(row["pair_created_at"]) if row["pair_created_at"] else None,
            "source": "sidecar",
        }
    except Exception:
        return None


def _sidecar_store(mint: str, info: dict):
    """Persist a resolved row so backtest pool workers stay deterministic."""
    try:
        conn = sqlite3.connect(PRICE_DB_PATH, timeout=10)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pool_virtual_resolves (
                    mint             TEXT PRIMARY KEY,
                    v_sol            REAL NOT NULL,
                    pool_address     TEXT DEFAULT '',
                    pair_created_at   REAL,
                    resolved_at       REAL NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO pool_virtual_resolves "
                "(mint, v_sol, pool_address, pair_created_at, resolved_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (mint, float(info["v_sol"]), info.get("pool_address") or "",
                 info.get("pair_created_at"), time.time()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # sidecar is a cache, never a hard dependency


def _dex_pumpswap_pair_sync(mint: str) -> Optional[dict]:
    try:
        d = _http_json(DEXSCREENER_SEARCH.format(q=mint))
        for pair in (d.get("pairs") or []):
            if pair.get("dexId") == "pumpswap":
                created = pair.get("pairCreatedAt")
                return {
                    "pair_address": pair.get("pairAddress") or "",
                    "pair_created_at": (float(created) / 1000.0
                                        if created else None),
                }
    except Exception:
        pass
    return None


def resolve_mint_pool(mint: str, force: bool = False) -> Optional[dict]:
    """Resolve a mint's PumpSwap pool virtual quote reserves.

    Returns {"pool_address": str, "v_sol": float, "pair_created_at": float|None,
    "source": str} for graduated mints (v_sol may be 0.0 for pools created
    before the virtual-liquidity rollout — the correction is then a no-op),
    or None when the mint has no pumpswap pool yet / nothing resolvable.

    Cached in-process and in the price_data.db sidecar; negative results are
    retried after _NEG_TTL_S.
    """
    if not vr_fix_enabled():
        return None
    now = time.time()
    if not force and mint in _cache:
        ts = _resolve_ts.get(mint, 0.0)
        cached = _cache[mint]
        ttl = _POS_TTL_S if (cached and cached.get("v_sol")) else _NEG_TTL_S
        if now - ts < ttl:
            return cached

    info = _sidecar_load(mint)
    if info is not None and not force:
        _cache[mint] = info
        _resolve_ts[mint] = now
        # sidecar rows are positive resolves only — honor the long TTL
        return info

    resolved: Optional[dict] = None
    pair = _dex_pumpswap_pair_sync(mint)
    if pair and pair.get("pair_address"):
        data = _rpc_get_account_b64(pair["pair_address"])
        if data is not None:
            vq = decode_virtual_quote_lamports(data)
            resolved = {
                "pool_address": pair["pair_address"],
                "v_sol": vq / 1e9,
                "pair_created_at": pair.get("pair_created_at"),
                "source": "chain",
            }
            if vq > 0:
                # sanity: the pool must actually be for this mint
                base = _pool_base_mint(data)
                if base and base != mint:
                    resolved = None
    if resolved is not None:
        _sidecar_store(mint, resolved)
    _cache[mint] = resolved
    _resolve_ts[mint] = now
    return resolved


async def resolve_mint_pool_async(mint: str, session=None,
                                  force: bool = False) -> Optional[dict]:
    """Async flavour for live paths — sync-resolves in a thread executor,
    sharing the same cache/sidecar semantics as resolve_mint_pool.  Live
    migration polling passes force=True so a batch-written negative sidecar
    row can never mask a mid-session graduation."""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, resolve_mint_pool, mint, force)


# ── Batch pre-warm (backtest batches: resolve before pool workers spawn) ────

DEXSCREENER_TOKENS = "https://api.dexscreener.com/tokens/v1/solana/{mints}"


def prewarm_mints(mints, chunk: int = 30) -> int:
    """Resolve pool V for many mints with batched calls and cache results
    (positive AND negative) in the sidecar, so pool workers never touch the
    network.  Called by run_backtest_batch before spawning workers.

    Negative rows (no pumpswap pool as of resolve time) are permanent for
    replay purposes: a pool that appears later belongs to post-FIX_EPOCH
    recordings, which the loader never corrects anyway.

    Returns the number of newly sidecar-cached mints.
    """
    if not vr_fix_enabled():
        return 0
    fresh = sorted({m for m in mints if m and m not in _cache})
    if not fresh:
        return 0
    stored = 0
    for i in range(0, len(fresh), chunk):
        group = fresh[i:i + chunk]
        pairs_by_mint: dict[str, dict] = {}
        try:
            data = _http_json(DEXSCREENER_TOKENS.format(
                mints=",".join(group)), timeout=25)
            if isinstance(data, list):
                for pair in data:
                    if pair.get("dexId") == "pumpswap":
                        mint = pair.get("baseToken", {}).get("address", "")
                        if mint and mint not in pairs_by_mint:
                            pairs_by_mint[mint] = pair
        except Exception:
            pass
        # one RPC batch for every pool address we learned
        addrs = [pairs_by_mint[m].get("pairAddress") or ""
                 for m in group if m in pairs_by_mint]
        vq_by_addr: dict[str, int] = {}
        if addrs:
            for j in range(0, len(addrs), 100):
                try:
                    res = _post_json(RPC_URLS[0], "getMultipleAccounts",
                                     [addrs[j:j + 100], {"encoding": "base64"}])
                    vals = (res or {}).get("result", {}).get("value") or []
                    for addr, val in zip(addrs[j:j + 100], vals):
                        if val and val.get("data"):
                            vq_by_addr[addr] = decode_virtual_quote_lamports(
                                base64.b64decode(val["data"][0]))
                except Exception:
                    pass
        for m in group:
            pair = pairs_by_mint.get(m)
            if pair and pair.get("pairAddress"):
                vq = vq_by_addr.get(pair["pairAddress"], 0)
                created = pair.get("pairCreatedAt")
                info = {
                    "pool_address": pair["pairAddress"],
                    "v_sol": vq / 1e9,
                    "pair_created_at": (float(created) / 1000.0
                                        if created else None),
                    "source": "batch",
                }
            else:
                # negative cache: no pumpswap pool at resolve time (dead /
                # never-graduated mint) — v_sol 0 makes every later
                # correction a fast sidecar no-op.
                info = {"pool_address": "", "v_sol": 0.0,
                        "pair_created_at": None,
                        "source": "batch-negative"}
            _cache[m] = info
            _resolve_ts[m] = time.time()
            _sidecar_store(m, info)
            stored += 1
        time.sleep(0.25)  # DexScreener politeness
    return stored


# ── Corrections ─────────────────────────────────────────────────────────────

def wedge_factor(vault_sol: float, v_sol: float) -> float:
    """Multiplicative tape→executable correction for one vault depth."""
    if vault_sol <= 0 or v_sol <= 0:
        return 1.0
    return (vault_sol + v_sol) / vault_sol


def _graduation_boundary(pair_created_at: Optional[float]) -> Optional[float]:
    """Wall-clock second from which the tape is on the pool (vault) basis.

    DexScreener's pairCreatedAt (pool creation = migration moment) is the
    only trustworthy boundary.  When it is missing we conservatively refuse
    to correct anything — a wrong boundary would scale curve-era candles
    (whose pool_sol is bonding-curve virtual SOL) by a phantom wedge.
    """
    if pair_created_at:
        return pair_created_at - 1.0
    return None


def correct_candles_to_effective_basis(candles: list[dict], mint: str,
                                       started_at: float) -> list[dict]:
    """Load-time correction of a phantom-basis recording (pre-FIX_EPOCH).

    Multiplies each pool-era candle's O/H/L/C and market_cap_usd by
    (pool_sol + V)/pool_sol — the exact executable-basis wedge — leaving the
    curve-era prefix untouched.  Volume fields are already real SOL flows.
    Returns the ORIGINAL list object when no correction applies.
    """
    if not vr_fix_enabled() or not candles or not mint:
        return candles
    if started_at and started_at >= PUMPSWAP_VR_FIX_EPOCH:
        return candles  # recorded on the effective basis already
    info = resolve_mint_pool(mint)
    if not info or not info.get("v_sol"):
        return candles
    v_sol = float(info["v_sol"])
    boundary = _graduation_boundary(info.get("pair_created_at"))
    if boundary is None:
        return candles
    corrected = 0
    for c in candles:
        if c["time"] < boundary:
            continue
        vault = c.get("pool_sol") or 0.0
        k = wedge_factor(vault, v_sol)
        if k == 1.0:
            continue
        c["open"] *= k
        c["high"] *= k
        c["low"] *= k
        c["close"] *= k
        if c.get("market_cap_usd"):
            c["market_cap_usd"] *= k
        corrected += 1
    if corrected:
        # observability tag (not persisted; callers see it on the dicts)
        for c in candles:
            c["price_basis"] = "effective"
    return candles
