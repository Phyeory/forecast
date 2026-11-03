#!/usr/bin/env python
"""Re-run golden cells and diff against the committed baseline.

Exit 0 only when every cell, the pure-function fixtures and the reason sets
are bit-identical to golden/baseline/.  Prints the first differing JSON path
per failing cell.  Never re-baseline on failure: revert the change.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import goldenlib as gl  # noqa: E402


def _worker_init() -> None:
    from process_watchdog import guard_parent
    guard_parent()


def _load_baseline(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def verify_cell(task: tuple) -> tuple:
    rec, version, model = task
    path = gl.cell_path(rec, version, model)
    label = f"rec{rec}/v{version}/{model}"
    if not os.path.exists(path):
        return task, f"FAIL baseline missing: {path}", None
    try:
        fresh = gl.run_cell(task)
    except Exception as exc:  # noqa: BLE001 - verify reports, never dies
        return task, f"FAIL raised {type(exc).__name__}: {exc}", None
    diff = gl.first_diff(_load_baseline(path), fresh)
    if diff:
        return task, f"FAIL {diff}", None
    return task, "PASS", fresh


def _subset_tasks(recs: list[int], engines: list[int],
                  execs: list[str]) -> list[tuple]:
    sizes = {r["id"]: r["candle_count"] for r in _corpus()["recordings"]}
    smallest = sorted(recs, key=lambda r: sizes.get(r, 0))[:3]
    return [(r, v, m) for r in smallest for v in engines for m in execs]


def _corpus() -> dict:
    with open(gl.RECORDINGS_JSON) as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subset", action="store_true",
                        help="3 smallest recordings x all engines/models")
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--recs", default="")
    parser.add_argument("--engines", default="")
    parser.add_argument("--exec", default="")
    args = parser.parse_args()

    corpus = _corpus()
    recs = [r["id"] for r in corpus["recordings"]]
    engines = [int(e) for e in corpus["engines"]]
    execs = list(corpus["exec_models"])
    if args.recs:
        recs = [r for r in recs if r in {int(x) for x in args.recs.split(",")}]
    if args.engines:
        engines = [e for e in engines if e in {int(x) for x in args.engines.split(",")}]
    if args.exec:
        execs = [e for e in execs if e in set(args.exec.split(","))]

    if args.subset:
        tasks = _subset_tasks(recs, engines, execs)
    else:
        tasks = [(r, v, m) for r in recs for v in engines for m in execs]
    for extra in corpus.get("extra_cells", []):
        tasks.append((extra["recording_id"], extra["engine_version"],
                      extra["exec_model"]))

    passed = failed = 0
    fresh_results = []
    with ProcessPoolExecutor(max_workers=max(1, args.jobs),
                             initializer=_worker_init) as pool:
        for task, verdict, fresh in pool.map(verify_cell, tasks):
            print(f"{verdict:<120} rec{task[0]}/v{task[1]}/{task[2]}",
                  flush=True)
            if verdict == "PASS":
                passed += 1
                if fresh is not None:
                    fresh_results.append(fresh)
            else:
                failed += 1

    pure_path = os.path.join(gl.BASELINE_DIR, "pure.json")
    fresh_pure = gl.capture_pure()
    diff = gl.first_diff(_load_baseline(pure_path), fresh_pure)
    label = "pure fixtures"
    if diff:
        print(f"FAIL {diff:<100} {label}")
        failed += 1
    else:
        print(f"PASS {'':<118} {label}")
        passed += 1

    reasons_path = os.path.join(gl.BASELINE_DIR, "exit_reasons.json")
    if not args.subset:
        diff = gl.first_diff(_load_baseline(reasons_path),
                             gl.collect_reasons(fresh_results))
        label = "exit/entry reason sets"
        if diff:
            print(f"FAIL {diff:<100} {label}")
            failed += 1
        else:
            print(f"PASS {'':<118} {label}")
            passed += 1

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
