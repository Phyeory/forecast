"""
Per-session persistent logging for LiveTrader.

Every live-trader session gets its own directory under ``backend/data/live_logs/``
containing two physical files:

  * ``console.log``  — every line the ``live-trader`` logger emits for THIS
    session (i.e. everything that would otherwise only appear on the console),
    timestamped, including tracebacks.
  * ``trades.jsonl`` — a structured JSON-lines trade ledger.  One record per
    trade event: entry attempt, broadcast, on-chain confirmation, failure,
    exit attempt, exit confirmation, partial fills, adopted bags, manual
    overrides, emergency cleanup, session start/end.  Each record carries
    timestamps (unix + ISO-8601), tx hashes, amounts, reasons and a snapshot
    of the trade object.

Usage: LiveTrader creates the journal in __init__ and closes it in close().
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

LOG_ROOT = Path(__file__).parent / "data" / "live_logs"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _safe(name: str) -> str:
    """Filesystem-safe short name component."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


class SessionJournal:
    """Owns one session's log files.  Not shared between sessions."""

    def __init__(self, token_mint: str, wallet: str, meta: Optional[dict] = None):
        ts = datetime.now()
        self.session_id = ts.strftime("%Y%m%d_%H%M%S")
        self.dir = LOG_ROOT / f"{self.session_id}_{_safe(token_mint[:12])}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.console_path = self.dir / "console.log"
        self.trades_path = self.dir / "trades.jsonl"

        # ── Mirror this session's console output into console.log ─────────
        # Attach a per-session handler to the shared "live-trader" logger and
        # filter so only records created by THIS trader instance land in the
        # file (live_trader.py stamps every LogRecord with _sid, see below).
        self._handler = logging.FileHandler(self.console_path, encoding="utf-8")
        self._handler.setLevel(logging.DEBUG)
        self._handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        self._handler.addFilter(self._only_this_session)
        self._logger = logging.getLogger("live-trader")
        self._logger.addHandler(self._handler)

        # ── Structured trade ledger ────────────────────────────────────────
        self._lock = threading.Lock()
        self._ledger = open(self.trades_path, "a", encoding="utf-8", buffering=1)
        self.event("session_open", {
            "token_mint": token_mint,
            "wallet": wallet,
            **(meta or {}),
        })

    # ── Console mirroring ─────────────────────────────────────────────────

    def _only_this_session(self, record: logging.LogRecord) -> bool:
        # Records emitted before __init__ finishes (or from non-session code)
        # have no _sid — exclude them from the per-session file.
        return getattr(record, "_sid", None) is self

    def bind_record(self, record: logging.LogRecord) -> None:
        """Stamp a log record as belonging to this session (called by LiveTrader)."""
        record._sid = self

    # ── Structured trade ledger ───────────────────────────────────────────

    def event(self, kind: str, data: Optional[dict] = None) -> None:
        """Append one JSON record to trades.jsonl (fsync'd immediately)."""
        now = datetime.now(timezone.utc)
        record = {
            "ts": now.timestamp(),
            "iso": now.isoformat(timespec="milliseconds"),
            "event": kind,
        }
        if data:
            record.update(data)
        line = json.dumps(record, default=str)
        try:
            with self._lock:
                self._ledger.write(line + "\n")
                self._ledger.flush()
        except Exception:
            # Logging must never break trading.
            pass

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def close(self, summary: Optional[dict] = None) -> None:
        try:
            self.event("session_close", summary or {})
        except Exception:
            pass
        try:
            self._logger.removeHandler(self._handler)
            self._handler.close()
        except Exception:
            pass
        try:
            self._ledger.close()
        except Exception:
            pass
