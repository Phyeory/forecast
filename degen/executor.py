"""DEGEN executor — latency-critical local swaps (code-signed, broadcast <1s).

Mirrors the repo's proven live_trader path: Jupiter quote → swap TX → sign with
the local keypair → broadcast to Solana RPC.  Used ONLY for reflex-tier trap
fires (nuke snipes); the browser lane handles slow plays.

NOTE (2026-10-04): repo-wide Jupiter migration — lite-api is retired; the v1
paths now live on api.jup.ag (same shapes, verified live). Set JUP_API_KEY for
paid-tier headroom. When Jupiter retires /swap/v1 entirely, flip to /swap/v2
here and in live_trader.py together.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from typing import Optional

import aiohttp
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from live_trader import SOLANA_RPCS  # shared free-RPC list (publicnode first)

logger = logging.getLogger("degen.executor")

JUP_HOST = os.environ.get("JUP_HOST", "https://api.jup.ag")
JUP_QUOTE_URL = f"{JUP_HOST}/swap/v1/quote"
JUP_SWAP_URL = f"{JUP_HOST}/swap/v1/swap"
JUP_API_KEY = os.environ.get("JUP_API_KEY", "")
SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000

PRIORITY_FEE_MICRO_LAMPORTS = 100_000     # same as live_trader default
CONFIRM_TIMEOUT_S = 10.0
CONFIRM_POLL_S = 0.4


def _jup_headers() -> dict:
    return {"x-api-key": JUP_API_KEY} if JUP_API_KEY else {}


class DegenExecutor:
    """Code-signed swaps for the reflex tier.  One instance per wallet."""

    def __init__(self, keypair: Keypair,
                 priority_fee_micro_lamports: int = PRIORITY_FEE_MICRO_LAMPORTS):
        self.keypair = keypair
        self.pubkey = str(keypair.pubkey())
        self.priority_fee = priority_fee_micro_lamports

    # ── swap plumbing ────────────────────────────────────────────────────

    async def _quote(self, sess: aiohttp.ClientSession, input_mint: str,
                     output_mint: str, amount_raw: int, slippage_bps: int) -> Optional[dict]:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_raw),
            "slippageBps": str(slippage_bps),
        }
        async with sess.get(JUP_QUOTE_URL, params=params, headers=_jup_headers()) as resp:
            if resp.status != 200:
                logger.warning("[DegenExec] quote %s → %s", resp.status, await resp.text())
                return None
            return await resp.json()

    async def _swap_tx(self, sess: aiohttp.ClientSession, quote: dict) -> Optional[str]:
        body = {
            "quoteResponse": quote,
            "userPublicKey": self.pubkey,
            "wrapAndUnwrapSol": True,
            "computeUnitPriceMicroLamports": self.priority_fee,
        }
        async with sess.post(JUP_SWAP_URL, json=body, headers=_jup_headers()) as resp:
            if resp.status != 200:
                logger.warning("[DegenExec] swap-tx %s → %s", resp.status, await resp.text())
                return None
            data = await resp.json()
            return data.get("swapTransaction")

    async def _broadcast(self, swap_tx_b64: str) -> Optional[str]:
        raw = base64.b64decode(swap_tx_b64)
        tx = VersionedTransaction.deserialize(raw)
        signed = VersionedTransaction(tx.message, [self.keypair])
        payload = {
            "jsonrpc": "2.0",
            "id": "degen",
            "method": "sendTransaction",
            "params": [base64.b64encode(bytes(signed)).decode(),
                       {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}],
        }
        for rpc in SOLANA_RPCS:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as sess:
                    async with sess.post(rpc, json=payload) as resp:
                        data = await resp.json(content_type=None)
                        sig = (data or {}).get("result")
                        if sig:
                            return sig
                        logger.warning("[DegenExec] broadcast %s → %s", rpc, data.get("error"))
            except Exception as e:
                logger.warning("[DegenExec] broadcast %s failed: %s", rpc, e)
        return None

    async def _confirm(self, sig: str, timeout_s: float = CONFIRM_TIMEOUT_S) -> Optional[dict]:
        deadline = time.time() + timeout_s
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as sess:
            while time.time() < deadline:
                try:
                    payload = {
                        "jsonrpc": "2.0", "id": "degen",
                        "method": "getSignatureStatuses",
                        "params": [[sig], {"searchTransactionHistory": False}],
                    }
                    async with sess.post(SOLANA_RPCS[0], json=payload) as resp:
                        data = await resp.json(content_type=None)
                        arr = ((data or {}).get("result") or {}).get("value") or []
                        st = arr[0] if arr else None
                        if st:
                            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                                return {"status": st["confirmationStatus"],
                                        "err": st.get("err")}
                            if st.get("err"):
                                return {"status": "failed", "err": st["err"]}
                except Exception:
                    pass
                await asyncio.sleep(CONFIRM_POLL_S)
        return {"status": "timeout", "err": None}

    # ── public actions ───────────────────────────────────────────────────

    async def buy(self, mint: str, size_sol: float, slippage_bps: int = 2000) -> dict:
        """SOL → token.  Returns a compact result dict for the journal."""
        t0 = time.time()
        amount_raw = int(size_sol * LAMPORTS_PER_SOL)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
            quote = await self._quote(sess, SOL_MINT, mint, amount_raw, slippage_bps)
            if not quote:
                return {"ok": False, "stage": "quote", "latency_s": round(time.time() - t0, 3)}
            swap_b64 = await self._swap_tx(sess, quote)
        if not swap_b64:
            return {"ok": False, "stage": "swap_tx", "latency_s": round(time.time() - t0, 3)}
        sig = await self._broadcast(swap_b64)
        if not sig:
            return {"ok": False, "stage": "broadcast", "latency_s": round(time.time() - t0, 3)}
        conf = await self._confirm(sig)
        out = quote.get("outAmount")
        return {
            "ok": bool(conf and conf.get("status") in ("confirmed", "finalized") and not (conf or {}).get("err")),
            "stage": "done",
            "signature": sig,
            "confirm": conf,
            "expected_out_raw": out,
            "latency_s": round(time.time() - t0, 3),
        }

    async def sell(self, mint: str, token_amount_raw: Optional[int] = None,
                   slippage_bps: int = 5000) -> dict:
        """Token → SOL.  Defaults to the full balance (Setuh sells by judgment,
        whole position).  Sell slippage 50% per the playbook."""
        bal = token_amount_raw
        if bal is None:
            bal = await self._token_balance_raw(mint)
            if not bal:
                return {"ok": False, "stage": "balance", "error": "no token balance"}
        t0 = time.time()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
            quote = await self._quote(sess, mint, SOL_MINT, int(bal), slippage_bps)
            if not quote:
                return {"ok": False, "stage": "quote", "latency_s": round(time.time() - t0, 3)}
            swap_b64 = await self._swap_tx(sess, quote)
        if not swap_b64:
            return {"ok": False, "stage": "swap_tx", "latency_s": round(time.time() - t0, 3)}
        sig = await self._broadcast(swap_b64)
        if not sig:
            return {"ok": False, "stage": "broadcast", "latency_s": round(time.time() - t0, 3)}
        conf = await self._confirm(sig)
        return {
            "ok": bool(conf and conf.get("status") in ("confirmed", "finalized") and not (conf or {}).get("err")),
            "stage": "done",
            "signature": sig,
            "confirm": conf,
            "latency_s": round(time.time() - t0, 3),
        }

    async def _token_balance_raw(self, mint: str) -> Optional[int]:
        payload = {
            "jsonrpc": "2.0", "id": "degen",
            "method": "getTokenAccountsByOwner",
            "params": [self.pubkey, {"mint": mint}, {"encoding": "jsonParsed"}],
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as sess:
                async with sess.post(SOLANA_RPCS[0], json=payload) as resp:
                    data = await resp.json(content_type=None)
                    accounts = ((data or {}).get("result") or {}).get("value") or []
                    total = 0
                    for acc in accounts:
                        info = acc.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                        total += int(info.get("tokenAmount", {}).get("amount", "0"))
                    return total or None
        except Exception as e:
            logger.warning("[DegenExec] balance fetch failed: %s", e)
            return None
