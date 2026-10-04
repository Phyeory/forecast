"""DEGEN vision — the agent's eyes on the chart.

Screenshots the token's public chart page (pump.fun; DexScreener fallback) and
asks the vision model for a typed visual verdict.  Implements the checks that
cannot be coded (Setuh's visual edge): real-chart personality vs the fake
patterns (bundle staircase, dev-buy-dominant, only-up grind), whether the dip
looks like a real shakeout or a falling knife, toppy-vs-alive.

Graceful degradation: without Playwright installed the module reports
unavailable and the decision model simply decides without eyes (the decision
vector marks it).  No logins here — Terminal screenshots live in
terminal_driver.py.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("degen.vision")

PUMPFUN_URL = "https://pump.fun/{mint}"
DEXSCREENER_URL = "https://dexscreener.com/solana/{mint}"

VISUAL_VERDICTS = ("real", "fake", "unclear")
VISUAL_PATTERNS = [
    "staircase_only_up", "dev_buy_dominant", "bundle_big_green_small_sells",
    "real_dips_present", "heterogeneous_candles", "toppy", "consolidating",
    "falling_knife", "floor_holding", "volume_drying", "banner_stretched",
    "socials_visible", "unclear",
]

SHOT_DIR = Path(__file__).resolve().parent / "journals" / "shots"


class ChartVision:
    def __init__(self, decision_engine):
        self.engine = decision_engine          # DecisionEngine (uses its analyst endpoint)
        self._playwright = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    def available(self) -> bool:
        try:
            import playwright.async_api  # noqa: F401
            return True
        except ImportError:
            return False

    # ── browser lifecycle ────────────────────────────────────────────────

    async def _ensure_browser(self):
        async with self._lock:
            if self._browser is not None:
                return True
            if not self.available():
                return False
            try:
                from playwright.async_api import async_playwright
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True, args=["--no-sandbox"])
                self._context = await self._browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")
                return True
            except Exception as e:
                logger.warning("[DegenVision] browser launch failed: %s", e)
                return False

    async def close(self):
        for closer in (self._context, self._browser, self._playwright):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        self._context = self._browser = self._playwright = None

    # ── screenshots ──────────────────────────────────────────────────────

    async def screenshot_chart(self, mint: str, timeout_s: float = 20.0) -> Optional[bytes]:
        """Screenshot the pump.fun token page; DexScreener as fallback."""
        if not await self._ensure_browser():
            return None
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        for url in (PUMPFUN_URL.format(mint=mint), DEXSCREENER_URL.format(mint=mint)):
            page = None
            try:
                page = await self._context.new_page()
                await page.goto(url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
                await page.wait_for_timeout(3500)   # let the chart render
                png = await page.screenshot(type="jpeg", quality=72)
                (SHOT_DIR / f"{mint[:12]}-{int(time.time())}.jpg").write_bytes(png)
                return png
            except Exception as e:
                logger.debug("[DegenVision] %s failed: %s", url, e)
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
        return None

    # ── typed visual verdict ─────────────────────────────────────────────

    async def read_chart(self, mint: str) -> dict:
        """Returns {available, visual_verdict, patterns[], notes, latency_s}.

        Output contract is the same idea as the decision model: enums only,
        bounded text — feeds the decision vector as facts, not prose.
        """
        out: dict = {"tool": "vision", "available": False}
        png = await self.screenshot_chart(mint)
        if not png:
            return out
        b64 = base64.b64encode(png).decode()
        prompt = {
            "instructions": (
                "You are reading a memecoin chart screenshot for a trader. "
                "Classify it. Return ONLY JSON: "
                '{"visual_verdict": "real|fake|unclear", '
                '"patterns": [enum…], "notes": "<=200 chars"}. '
                f"patterns enum: {VISUAL_PATTERNS}. "
                "Fake = staircase-only-up, dev-buy-dominant, or bundle-style "
                "big-green/tiny-sell loops. Real = heterogeneous candles with "
                "genuine dips from human two-way trading."
            ),
            "image_mime": "image/jpeg",
        }
        try:
            import aiohttp
            sess = await self.engine._get_session()
            body = {
                "model": self.engine.analyst_model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": json_dumps(prompt)},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }],
                "temperature": 0.0,
                "reasoning": {"max_tokens": 400},   # glm-5.3-flash reasons before answering
                "max_tokens": 1200,
            }
            t0 = time.time()
            async with sess.post(f"{self.engine.base_url}/chat/completions", json=body) as resp:
                if resp.status != 200:
                    out["error"] = f"vision HTTP {resp.status}"
                    return out
                data = await resp.json()
            text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
            from .decision import _extract_json
            parsed = _extract_json(text) or {}
            verdict = parsed.get("visual_verdict")
            out.update({
                "available": True,
                "visual_verdict": verdict if verdict in VISUAL_VERDICTS else "unclear",
                "patterns": [p for p in (parsed.get("patterns") or []) if p in VISUAL_PATTERNS][:6],
                "notes": str(parsed.get("notes") or "")[:200],
                "latency_s": round(time.time() - t0, 2),
            })
            return out
        except Exception as e:
            out["error"] = str(e)
            return out


def json_dumps(obj: dict) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
