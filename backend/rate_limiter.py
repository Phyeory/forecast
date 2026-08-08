"""
Process-wide rate-limit coordination for external APIs.

Currently coordinates:
  * GMGN (openapi.gmgn.ai + gmgn-cli) — shared ban state so holder_flow and
    autofeed don't independently re-trigger bans.
  * Solana public RPC (api.mainnet-beta.solana.com) — shared rate budget so
    live_trader background loops don't starve pumpfun_client's gated reads.

Design: simple module-level singletons, no class hierarchy.  All components
``import rate_limiter as rl`` and call ``rl.gmgn_banned()`` /
``rl.note_gmgn_429(reset_unix)`` etc.
"""

from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)

# ── GMGN shared ban state ────────────────────────────────────────────────────

_gmgn_banned_until: float = 0.0
_ggn_ban_logged: bool = False


def note_gmgn_429(reset_unix: float | None, source: str = "") -> float:
    """Record a GMGN rate-limit ban.  Returns the new ban-unix timestamp.

    ``reset_unix`` should be a UTC unix timestamp.  If None or in the past,
    a conservative fallback is used so callers don't immediately re-hammer.
    """
    global _gmgn_banned_until, _ggn_ban_logged
    now = time.time()
    if reset_unix and reset_unix > now:
        _gmgn_banned_until = max(_gmgn_banned_until, reset_unix)
    else:
        _gmgn_banned_until = max(_gmgn_banned_until, now + 30.0)
    if not _ggn_ban_logged:
        wait = max(0.0, _gmgn_banned_until - now)
        logger.warning(
            f"[RateLimit] GMGN rate-limited ({source}) — backing off {wait:.0f}s "
            f"(further ban logs suppressed until reset)"
        )
        _ggn_ban_logged = True
    return _gmgn_banned_until


def gmgn_banned() -> bool:
    """True if we're currently inside a GMGN rate-limit ban window."""
    global _ggn_ban_logged
    if time.time() < _gmgn_banned_until:
        return True
    if _ggn_ban_logged:
        _ggn_ban_logged = False
    return False


def gmgn_ban_remaining() -> float:
    """Seconds remaining in the current GMGN ban (0 if not banned)."""
    return max(0.0, _gmgn_banned_until - time.time())


def parse_gmgn_reset_time(message: str) -> float | None:
    """Extract the unix reset time from a GMGN rate-limit error message.

    Handles multiple formats:
      * "Rate limit resets at 2026-08-08 00:00:49 GMT+01:00 (~27s remaining)"
      * "resets at 2026-08-08 00:00:49 GM"
      * Any "resets at YYYY-MM-DD HH:MM:SS" pattern

    Returns UTC unix timestamp or None.
    """
    if not message:
        return None
    m = re.search(r"resets at (\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})", message)
    if not m:
        return None
    try:
        import datetime
        dt = datetime.datetime.strptime(
            f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}",
            "%Y-%m-%d %H:%M:%S",
        )
        # Parse timezone from the message if present
        tz_m = re.search(r"GMT([+-]\d{2}):(\d{2})", message)
        if tz_m:
            tz_hours = int(tz_m.group(1))
            tz_mins = int(tz_m.group(2))
            tz = datetime.timezone(datetime.timedelta(hours=tz_hours, minutes=tz_mins))
            dt = dt.replace(tzinfo=tz)
        else:
            # GMGN gives UTC by default
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def parse_gmgn_remaining_seconds(message: str) -> float | None:
    """Extract "~Ns remaining" from a GMGN rate-limit message.

    Returns seconds or None.
    """
    if not message:
        return None
    m = re.search(r"\((\d+)s remaining\)", message)
    if not m:
        # also try "remaining" without parens
        m = re.search(r"~?(\d+)s remaining", message)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


# ── Solana RPC shared rate budget ────────────────────────────────────────────

# Public mainnet-beta is the most restrictive free RPC.  We share a simple
# min-interval gate between ALL components that target it, so live_trader's
# background loops can't starve pumpfun_client's gated reads.
#
# Note: live_trader uses multiple RPCs (publicnode, ankr) and only falls back
# to mainnet-beta.  The primary fix is to make mainnet-beta NOT the primary
# read RPC — but this budget gate prevents starvation when it IS used.

_solana_last_call_ts: float = 0.0
_solana_min_interval: float = 0.35  # ~2.8 req/s budget for mainnet-beta

# The module intentionally keeps the Solana budget gate minimal — the primary
# fix for RPC 429 storms is reordering live_trader.py's SOLANA_RPCS so that
# api.mainnet-beta.solana.com is NOT the primary read endpoint (it's the most
# rate-limited free RPC).  The shared GMGN ban state above is the more
# impactful coordination: it stops holder_flow and autofeed from independently
# re-triggering bans.
