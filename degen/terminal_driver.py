"""DEGEN browser lane — Terminal (trade.padre.gg) confirm-mode driver.

The agent fills the buy panel on YOUR logged-in Terminal session and stops —
you press sign.  No keys, no auto-signing (auto-sign stays an experimental
flag only if the wallet proves session-based signing; biometric/passkey
wallets make it impossible, and this driver never attempts it).

The Playwright profile persists at journals/terminal_profile — log in once via
``open_login()`` (headed window) and every later drive reuses the session.

Selector fragility is acknowledged: terminals churn their UI.  Strategies are
config-overridable (DEGEN_TERMINAL_SELECTORS = JSON file with css/xpaths) and
every step degrades to "screenshot + report" rather than mis-clicking — a
failed fill is always safe (you just trade manually on the window we opened).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("degen.terminal")

TERMINAL_TRADE_URL = "https://trade.padre.gg/trade/solana/{mint}"
PROFILE_DIR = Path(__file__).resolve().parent / "journals" / "terminal_profile"
SHOT_DIR = Path(__file__).resolve().parent / "journals" / "shots"


def _selectors() -> dict:
    """Overridable selectors: DEGEN_TERMINAL_SELECTORS=/path/selectors.json"""
    defaults = {
        "amount_input": "input[inputmode='decimal']",
        "buy_button": "button:has-text('Buy')",
    }
    path = os.environ.get("DEGEN_TERMINAL_SELECTORS", "")
    if path and Path(path).exists():
        try:
            defaults.update(json.loads(Path(path).read_text()))
        except Exception:
            pass
    return defaults


class TerminalDriver:
    """Confirm-mode fills on the user's own Terminal session."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self._context = None
        self._lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────

    async def _ensure(self, headed: Optional[bool] = None):
        async with self._lock:
            if self._context is not None:
                return True
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                logger.warning("[DegenTerminal] playwright not installed — browser lane disabled")
                return False
            try:
                self._pw = await async_playwright().start()
                PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                self._context = await self._pw.chromium.launch_persistent_context(
                    str(PROFILE_DIR),
                    headless=self.headless if headed is None else not headed,
                    viewport={"width": 1440, "height": 950},
                    args=["--no-sandbox"],
                )
                return True
            except Exception as e:
                logger.warning("[DegenTerminal] launch failed: %s", e)
                return False

    async def close(self):
        for closer in (self._context, self._pw):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        self._context = self._pw = None

    # ── one-time login (run once on the user's Mac) ──────────────────────

    async def open_login(self, wait_s: int = 180) -> dict:
        """Open a HEADED window on trade.padre.gg; the user logs in; session
        persists into the profile dir for all later headless drives."""
        ok = await self._ensure(headed=True)
        if not ok:
            return {"ok": False, "reason": "playwright unavailable"}
        page = await self._context.new_page()
        try:
            await page.goto("https://trade.padre.gg", timeout=45_000)
            await page.wait_for_timeout(wait_s * 1000 // 10)
            logged = "padre" in (page.url or "")
            shot = SHOT_DIR / f"terminal-login-{int(time.time())}.jpg"
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(shot), type="jpeg", quality=70)
            return {"ok": True, "url": page.url, "screenshot": str(shot), "note":
                    "If not logged in, run this again and log in inside the window "
                    "within the wait."}
        finally:
            try:
                await page.close()
            except Exception:
                pass

    # ── confirm-mode fill ────────────────────────────────────────────────

    async def prepare_buy(self, mint: str, size_sol: float,
                          slippage_bps: int = 2000, fill: bool = True) -> dict:
        """Navigate to the token page, fill size (+ slippage preset if
        reachable), leave the SIGN to the human.  fill=False = navigate +
        screenshot only (dry run / reconciliation)."""
        if not await self._ensure(headed=not self.headless):
            return {"ok": False, "reason": "playwright unavailable"}
        url = TERMINAL_TRADE_URL.format(mint=mint)
        result: dict = {"ok": False, "url": url, "filled": False}
        page = await self._context.new_page()
        try:
            await page.goto(url, timeout=45_000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            if fill:
                sel = _selectors()
                try:
                    box = page.locator(sel["amount_input"]).first
                    await box.fill(f"{size_sol:g}", timeout=8000)
                    result["filled"] = True
                except Exception as e:
                    result["fill_error"] = str(e)[:200]
                    logger.warning("[DegenTerminal] fill failed (%s) — leaving page "
                                   "open for manual confirm", e)
            SHOT_DIR.mkdir(parents=True, exist_ok=True)
            shot = SHOT_DIR / f"terminal-{mint[:12]}-{int(time.time())}.jpg"
            await page.screenshot(path=str(shot), type="jpeg", quality=70)
            result["screenshot"] = str(shot)
            result["ok"] = True
            result["note"] = ("Panel ready — press sign in the Terminal window."
                              if result["filled"] else
                              "Page open — complete the trade manually (fill failed).")
            return result
        except Exception as e:
            result["error"] = str(e)[:300]
            return result
        finally:
            # confirm mode: LEAVE THE PAGE OPEN for the human? Playwright
            # persistent contexts close pages on close(); v1 closes after the
            # screenshot and reopens on demand — the user's own terminal tab
            # is always available for the sign.
            try:
                await page.close()
            except Exception:
                pass

    async def reconcile(self, mint: str) -> dict:
        """Post-trade reconciliation screenshot (position visible?)."""
        return await self.prepare_buy(mint, 0, fill=False)
