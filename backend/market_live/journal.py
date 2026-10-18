"""journal.py — JSONL session journal (same conventions as the memecoin stack).

Every live session writes `backend/data/live_market/<session>/`:

  session_open.json   config + code_commit/code_dirty (stale-process tripwire)
  bars.jsonl          every closed bar the engine consumed
  decisions.jsonl     per-bar desired position + fills (engine truth)
  orders.jsonl        broker fills (account truth)
  equity.jsonl        per-bar equity, guard status
  guard.jsonl         guard events (phase pass, halts, consistency holds)
  heartbeat.jsonl     periodic liveness + position snapshot

Audits read these files, never dashboard counters.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "live_market"


def _code_state() -> tuple[str, bool]:
    try:
        root = Path(__file__).resolve().parents[2]
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return commit, bool(dirty)
    except Exception:
        return "", True


class SessionJournal:
    def __init__(self, session_id: Optional[str] = None, root: Optional[Path] = None):
        self.session_id = session_id or time.strftime("%Y%m%d_%H%M%S")
        self.dir = Path(root or DEFAULT_ROOT) / self.session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._files: dict[str, object] = {}

    def open_session(self, config: dict) -> None:
        commit, dirty = _code_state()
        payload = {"session": self.session_id, "opened_at": time.time(),
                   "code_commit": commit, "code_dirty": dirty, "config": config}
        (self.dir / "session_open.json").write_text(json.dumps(payload, indent=1, default=str))

    def _fh(self, name: str):
        fh = self._files.get(name)
        if fh is None:
            fh = open(self.dir / f"{name}.jsonl", "a", buffering=1)
            self._files[name] = fh
        return fh

    def write(self, stream: str, payload: dict) -> None:
        rec = {"t": time.time(), "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               **payload}
        self._fh(stream).write(json.dumps(rec, default=str) + "\n")

    def bar(self, payload: dict) -> None:
        self.write("bars", payload)

    def decision(self, payload: dict) -> None:
        self.write("decisions", payload)

    def order(self, payload: dict) -> None:
        self.write("orders", payload)

    def equity(self, payload: dict) -> None:
        self.write("equity", payload)

    def guard(self, payload: dict) -> None:
        self.write("guard", payload)

    def heartbeat(self, payload: dict) -> None:
        self.write("heartbeat", payload)

    def close(self) -> None:
        for fh in self._files.values():
            try:
                fh.close()
            except Exception:
                pass
        self._files.clear()
