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
# Bans for a key other than the shared `GMGN_API_KEY` (e.g. a dedicated
# `GMGN_API_KEY_AUTOFEED`).  The default "shared" slot keeps the historical
# module-level single state so existing callers/tests are unchanged; extra
# slots let a dedicated autofeed key have its OWN quota window instead of
# inheriting holder_flow's 429 ban.
_gmgn_banned_until_by_slot: dict[str, float] = {}
_ggn_ban_logged: bool = False
_ggn_ban_logged_slots: set[str] = set()


def note_gmgn_429(reset_unix: float | None, source: str = "",
                  slot: str = "shared") -> float:
    """Record a GMGN rate-limit ban.  Returns the new ban-unix timestamp.

    ``reset_unix`` should be a UTC unix timestamp.  If None or in the past,
    a conservative fallback is used so callers don't immediately re-hammer.
    ``slot`` separates independent keys: the default "shared" slot is the
    historical `GMGN_API_KEY` state; a dedicated autofeed key should use its
    own slot so a holder_flow ban does not block it (and vice versa).
    """
    global _gmgn_banned_until, _ggn_ban_logged
    now = time.time()
    until = reset_unix if (reset_unix and reset_unix > now) else now + 30.0
    if not slot or slot == "shared":
        _gmgn_banned_until = max(_gmgn_banned_until, until)
        current = _gmgn_banned_until
        logged = _ggn_ban_logged
    else:
        current = max(_gmgn_banned_until_by_slot.get(slot, 0.0), until)
        _gmgn_banned_until_by_slot[slot] = current
        logged = slot in _ggn_ban_logged_slots
    if not logged:
        wait = max(0.0, current - now)
        logger.warning(
            f"[RateLimit] GMGN rate-limited ({source}, slot={slot or 'shared'}) "
            f"— backing off {wait:.0f}s (further ban logs suppressed until reset)"
        )
        if not slot or slot == "shared":
            _ggn_ban_logged = True
        else:
            _ggn_ban_logged_slots.add(slot)
    return current


def gmgn_banned(slot: str = "shared") -> bool:
    """True if the given key slot is inside a GMGN rate-limit ban window."""
    global _ggn_ban_logged
    if not slot or slot == "shared":
        if time.time() < _gmgn_banned_until:
            return True
        if _ggn_ban_logged:
            _ggn_ban_logged = False
        return False
    if time.time() < _gmgn_banned_until_by_slot.get(slot, 0.0):
        return True
    _ggn_ban_logged_slots.discard(slot)
    return False


def gmgn_ban_remaining(slot: str = "shared") -> float:
    """Seconds remaining in the given GMGN slot's ban (0 if not banned)."""
    until = (_gmgn_banned_until if not slot or slot == "shared"
             else _gmgn_banned_until_by_slot.get(slot, 0.0))
    return max(0.0, until - time.time())


# ── GMGN autofeed slot reservation ──────────────────────────────────────────
# The shared key is 5 req/min.  holder_flow polls every 15 s (4/min) and the
# autofeed trending CLI uses 1/min, leaving zero for the organic gate's
# holder-table call.  When the gate must use the shared key, AutoFeed reserves
# one slot and holder_flow stretches its poll to 20 s (3/min): 3 + 1 + 1 = 5.
# With a dedicated GMGN_API_KEY_AUTOFEED the flag stays False and holder_flow
# keeps its 15 s production cadence.
_gmgn_gate_slot_reserved: bool = False


def reserve_gmgn_gate_slot(reserved: bool) -> None:
    global _gmgn_gate_slot_reserved
    _gmgn_gate_slot_reserved = bool(reserved)


def gmgn_gate_slot_reserved() -> bool:
    return _gmgn_gate_slot_reserved


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
