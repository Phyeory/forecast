"""Shared lifecycle base for candidate feeds: start, stop, config, status, backpressure."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional


class CandidateFeed:
    """Base class owning the shared candidate-feed lifecycle and status surface."""

    #: Feed config dataclass; each subclass assigns it before start().
    config: Any
    #: Bracketed log prefix label, e.g. "AutoFeed".
    _label: str = "Feed"

    def __init__(
        self,
        forward_fn: Optional[Callable[[Any], Awaitable[None]]] = None,
        active_count_fn: Optional[Callable[[], int]] = None,
    ) -> None:
        self._forward_fn = forward_fn
        self._active_count_fn = active_count_fn
        self._task: Optional[asyncio.Task] = None
        self._stop_evt = asyncio.Event()
        self._seen: dict[str, Any] = {}   # mint -> candidate (dedupe/UI)
        self.last_error: str = ""
        self.total_seen: int = 0
        self.total_fed: int = 0
        # The logger name is observable log output, so subclasses bind their
        # own module logger here instead of this file using one fixed name.
        self._log: logging.Logger = logging.getLogger(__name__)

    # Lifecycle

    def start(
        self,
        forward_fn: Optional[Callable[[Any], Awaitable[None]]] = None,
        active_count_fn: Optional[Callable[[], int]] = None,
    ) -> bool:
        """Spawn the discovery task and return False when already running."""
        if forward_fn is not None:
            self._forward_fn = forward_fn
        if active_count_fn is not None:
            self._active_count_fn = active_count_fn
        if self._task and not self._task.done():
            # Message text is observable behaviour; keep it byte-identical.
            self._log.warning(
                f"[{self._label}] Already running — ignoring duplicate start()"
            )
            return False
        self._stop_evt.clear()
        self._task = asyncio.ensure_future(self._loop())
        self._on_started()
        self._log_started()
        return True

    async def stop(self) -> None:
        """Signal every loop, await each task up to 10 s, then cancel on timeout."""
        self._stop_evt.set()
        for task in self._stoppable_tasks():
            if task and not task.done():
                try:
                    await asyncio.wait_for(task, timeout=10.0)
                except asyncio.TimeoutError:
                    task.cancel()
                except Exception:
                    # A draining loop can fail in any way; stop must not raise.
                    pass
        self._clear_tasks()
        self._log.info(f"[{self._label}] Stopped")

    def is_running(self) -> bool:
        """True while the discovery task is alive and the feed is enabled."""
        return bool(self._task and not self._task.done() and self.config.enabled)

    # Hooks each feed supplies or overrides

    async def _loop(self) -> None:
        """Discovery loop body; each feed owns its own cadence and source."""
        raise NotImplementedError

    def _on_started(self) -> None:
        """Hook run after the discovery task spawns and before the start banner."""

    def _log_started(self) -> None:
        """Emit the feed-specific start banner."""
        raise NotImplementedError

    def _stoppable_tasks(self) -> tuple[Optional[asyncio.Task], ...]:
        """Tasks awaited by stop(), in order."""
        return (self._task,)

    def _clear_tasks(self) -> None:
        """Drop task references once the loops have drained."""
        self._task = None

    # Config

    def set_config(self, partial: dict) -> list[str]:
        """Hot-merge a partial config dict and return the names of changed keys."""
        changed = []
        for k, v in partial.items():
            if hasattr(self.config, k) and v is not None:
                old = getattr(self.config, k)
                if old != v:
                    setattr(self.config, k, v)
                    changed.append(k)
        return changed

    # Backpressure

    def _active_count(self) -> int:
        """Backpressure count from a sync callback; 0 when absent or failing."""
        if self._active_count_fn is None:
            return 0
        try:
            return int(self._active_count_fn())
        except Exception:
            return 0

    async def _active_count_async(self) -> int:
        """Backpressure count from an awaited callback; 0 when absent or failing."""
        if self._active_count_fn is None:
            return 0
        try:
            return int(await self._active_count_fn())
        except Exception:
            return 0

    # Exclusions and status

    def _excluded_set(self) -> set[str]:
        """Lower-cased mint exclusion set parsed from the comma-separated config."""
        raw = (self.config.exclude_mints or "").strip()
        return {x.strip().lower() for x in raw.split(",") if x.strip()}

    def snapshot(self) -> dict:
        """Status snapshot: feed head keys, shared state block, feed tail keys."""
        snap = self._snapshot_head()
        snap.update(self._state_fields())
        snap.update(self._snapshot_tail())
        return snap

    def _snapshot_head(self) -> dict:
        """Feed-specific status keys that precede the shared state block."""
        raise NotImplementedError

    def _state_fields(self) -> dict:
        """Shared status fields in the JSON key order both feeds expose."""
        return {
            "last_error": self.last_error,
            "total_seen": self.total_seen,
            "total_fed": self.total_fed,
        }

    def _snapshot_tail(self) -> dict:
        """Feed-specific status keys that follow the shared state block."""
        raise NotImplementedError
