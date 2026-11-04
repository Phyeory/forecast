"""
process_watchdog: self-terminating guard for pool worker processes.

ProcessPoolExecutor / multiprocessing.Pool workers are reparented to
launchd/init when the parent Python process dies, and then keep burning
CPU forever.  Every pool worker starts a daemon thread that polls
``os.getppid()`` and hard-exits the worker the instant the parent is
gone, so a killed or crashed batch run can never leak workers.

Guarantees: a live parent is never affected (results are byte-identical
with or without the guard); idempotent; a no-op in the MainProcess, so
it can be called unconditionally or passed as the pool ``initializer``.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time

_POLL_INTERVAL_SECONDS = 1.0

_watcher_started = False
_watcher_lock = threading.Lock()


def _watch(parent_pid: int) -> None:
    while True:
        time.sleep(_POLL_INTERVAL_SECONDS)
        # When the spawn parent dies the worker is reparented to launchd
        # (PID 1) on macOS / init on Linux, so a ppid change == orphaned.
        if os.getppid() != parent_pid:
            os._exit(0)


def guard_parent() -> None:
    """Start the parent-death watcher in THIS process (idempotent)."""
    global _watcher_started
    with _watcher_lock:
        if _watcher_started:
            return
        if multiprocessing.current_process().name == "MainProcess":
            return
        _watcher_started = True
        parent_pid = os.getppid()
        if parent_pid <= 1:
            return
        t = threading.Thread(
            target=_watch,
            args=(parent_pid,),
            name="parent-watchdog",
            daemon=True,
        )
        t.start()