#!/usr/bin/env python
"""Acceptance-gate checks for the behaviour-preserving refactor.

Implements the gates that can be checked programmatically (REFACTOR_PROMPT.md
section 11, gates 3 to 10).  Gate 1 (golden corpus) is golden_verify.py,
gate 12 is a live paper session, gate 11 is a human walkthrough.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES: list[str] = []


def gate(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def py_files() -> list[str]:
    out = []
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in (".venv", "__pycache__", "data",
                                               "golden", "v2_results",
                                               "backtest_results", "live_logs")]
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(root, f))
    return out


def gate_size_limits() -> None:
    bad_fn, bad_mod, bad_cls = [], [], []
    for path in py_files():
        rel = os.path.relpath(path, BACKEND)
        n_lines = len(open(path, errors="replace").read().split("\n"))
        if n_lines > 800:
            bad_mod.append(f"{rel}={n_lines}")
        try:
            tree = ast.parse(open(path, errors="replace").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = node.end_lineno - node.lineno + 1
                if length > 60:
                    bad_fn.append(f"{rel}:{node.name}={length}")
            elif isinstance(node, ast.ClassDef):
                length = node.end_lineno - node.lineno + 1
                if length > 400:
                    bad_cls.append(f"{rel}:{node.name}={length}")
    gate("no function > 60 lines", not bad_fn, ", ".join(bad_fn[:6]))
    gate("no class > 400 lines", not bad_cls, ", ".join(bad_cls[:6]))
    gate("no module > 800 lines", not bad_mod, ", ".join(bad_mod[:6]))


def grep_gate(name: str, pattern: str, allow: str = "") -> None:
    # Production modules only: the brief's gates cover backend/*.py, and the
    # refactor's new packages.  analysis/ and tools/ are separate projects.
    targets = ["--include=*.py",
               "main.py", "app/", "engines/", "core/", "feeds/", "stores/",
               "data_store.py", "newpairs_store.py", "forward_tester.py",
               "backtester.py", "live_trader.py", "pumpfun_client.py",
               "holder_flow.py", "candle_aggregator.py", "engine_factory.py",
               "signal_capture.py", "session_calibrator.py",
               "calibration_startup.py", "mint_chain_history.py",
               "pool_virtual_reserves.py", "trade_report.py",
               "live_session_logger.py", "rate_limiter.py",
               "process_watchdog.py", "autofeed.py", "newpairs.py"]
    proc = subprocess.run(["grep", "-rnE", pattern] + targets,
                          cwd=BACKEND, capture_output=True, text=True)
    hits = [l for l in proc.stdout.splitlines()
            if ".venv" not in l and "tools/" not in l
            and (not allow or allow not in l)]
    gate(name, not hits, f"{len(hits)} hits: {hits[:3]}")


def gate_no_swallowed_exceptions() -> None:
    targets = ["--include=*.py", "main.py", "app/", "engines/", "core/",
               "feeds/", "stores/", "data_store.py", "newpairs_store.py",
               "forward_tester.py", "backtester.py", "live_trader.py",
               "pumpfun_client.py", "holder_flow.py", "candle_aggregator.py",
               "engine_factory.py", "signal_capture.py",
               "session_calibrator.py", "calibration_startup.py",
               "mint_chain_history.py", "pool_virtual_reserves.py",
               "trade_report.py", "live_session_logger.py",
               "rate_limiter.py", "process_watchdog.py", "autofeed.py",
               "newpairs.py"]
    proc = subprocess.run(["grep", "-rnA1", "except Exception:"] + targets,
                          cwd=BACKEND, capture_output=True, text=True)
    lines = proc.stdout.splitlines()
    swallows = []
    for i, line in enumerate(lines):
        if line.endswith("except Exception:") and ".venv" not in line \
                and "tools/" not in line:
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            if next_line.split("-", 2)[-1].strip() == "pass":
                swallows.append(line)
    gate("no 'except Exception:' followed by pass", not swallows,
         f"{len(swallows)} hits: {swallows[:3]}")


def gate_import_isolation() -> None:
    code = (
        "import sys;"
        "sys.path.insert(0, '.');"
        "import strategy_engine, strategy_engineV2, strategy_engineV3,"
        "strategy_engineV4, strategy_engineV6, engine_factory;"
        "from engine_factory import create_engine;"
        "[create_engine(v) for v in (1, 2, 3, 4, 6)];"
        "banned = [m for m in sys.modules"
        " if m.split('.')[0] in ('fastapi', 'aiohttp', 'solders', 'uvicorn')];"
        "print('BANNED:' + ','.join(banned))"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=BACKEND,
                          capture_output=True, text=True)
    banned = [l for l in proc.stdout.splitlines() if l.startswith("BANNED:")]
    gate("engines import without fastapi/aiohttp/solders",
         proc.returncode == 0 and banned and banned[0] == "BANNED:",
         (banned[0] if banned else proc.stderr.strip()[:120]))


def main() -> int:
    gate_size_limits()
    grep_gate("no iterNN markers", r"iter[0-9]+")
    grep_gate("no ISO dates in code", r"20[0-9]{2}-[0-9]{2}-[0-9]{2}")
    gate_no_swallowed_exceptions()
    grep_gate("no global statements", r"^\s*global ")
    proc = subprocess.run(
        ["grep", "-c", "", os.path.join(BACKEND, "main.py")],
        capture_output=True, text=True)
    lines = int(proc.stdout.strip() or 0)
    gate("main.py under 400 lines", lines < 400, f"{lines} lines")
    gate_import_isolation()
    print(f"\n{'ALL GATES PASS' if not FAILURES else str(len(FAILURES)) + ' GATES FAIL'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
