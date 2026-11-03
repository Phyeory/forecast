#!/usr/bin/env python
"""Write golden baseline fixtures for the behaviour-preserving refactor.

Modes:
  --scan     run a V2/instant probe over stratified candidate recordings and
             write a scan report (trade counts, exit reasons, calibration
             audit) used to pick the corpus.
  --select   apply the corpus selection rules to a scan report and write
             golden/recordings.json.
  --capture  run every corpus cell and write golden/baseline/ fixtures
             (backtest cells, pure-function fixtures, reason sets).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import goldenlib as gl  # noqa: E402

VR_FIX_EPOCH = 1790395000.0


def _load_recordings_rows():
    import sqlite3
    con = sqlite3.connect(os.path.join(gl.BACKEND_DIR, "data", "price_data.db"))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT r.id, r.mint, r.token_symbol, r.candle_count, r.started_at, "
        "       s.pair_created_at "
        "FROM recordings r "
        "LEFT JOIN pool_virtual_resolves s ON s.mint = r.mint "
        "WHERE r.status = 'completed' AND r.candle_count >= 100"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _era(row: dict) -> str:
    started_at = float(row["started_at"] or 0.0)
    pair_t = row["pair_created_at"]
    if pair_t and started_at < float(pair_t) - 60.0:
        return "curve_prefix"
    if started_at < VR_FIX_EPOCH:
        return "pool_pre_fix"
    return "pool_post_fix"


def cmd_scan(out_path: str, per_band: int) -> None:
    rows = _load_recordings_rows()
    bands = {
        "low": lambda n: 100 <= n <= 250,
        "mid": lambda n: 400 <= n <= 2000,
        "high": lambda n: n >= 5000,
    }
    candidates: dict[int, dict] = {}
    for era in ("curve_prefix", "pool_pre_fix", "pool_post_fix"):
        for band, test in bands.items():
            pool = [r for r in rows if _era(r) == era and test(r["candle_count"])]
            pool.sort(key=lambda r: r["id"])
            for r in pool[:per_band]:
                candidates[r["id"]] = r
    for r in sorted(rows, key=lambda r: r["candle_count"], reverse=True)[:2]:
        candidates[r["id"]] = r

    ordered = sorted(candidates.values(), key=lambda r: r["id"])
    print(f"scanning {len(ordered)} candidates (V2/instant)", flush=True)
    report = []
    for i, row in enumerate(ordered):
        started = time.time()
        try:
            result = gl.run_cell((row["id"], 2, "instant"))
        except Exception as exc:  # noqa: BLE001 - scan reports, never dies
            report.append({
                "id": row["id"], "mint": row["mint"],
                "token_symbol": row["token_symbol"],
                "candle_count": row["candle_count"], "era": _era(row),
                "error": f"{type(exc).__name__}: {exc}",
            })
            print(f"[{i + 1}/{len(ordered)}] rec{row['id']} ERROR {exc}",
                  flush=True)
            continue
        reasons = gl.collect_reasons([result])
        entry = {
            "id": row["id"], "mint": row["mint"],
            "token_symbol": row["token_symbol"],
            "candle_count": row["candle_count"], "era": _era(row),
            "trade_count": result["trade_count"],
            "exit_reasons": reasons["exit_reasons"],
            "ended_open": any(t.get("exit_reason") == "recording_ended"
                              for t in result["trades"]),
            "chain_attempted": bool(
                (result.get("calibration_audit") or {}).get("chain_attempted")),
            "seconds": round(time.time() - started, 1),
        }
        report.append(entry)
        print(f"[{i + 1}/{len(ordered)}] rec{row['id']} "
              f"{row['candle_count']}c {entry['trade_count']}t "
              f"{entry['seconds']}s "
              f"{'CHAIN ' if entry['chain_attempted'] else ''}"
              f"{','.join(reasons['exit_reasons'][:3])}", flush=True)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    print(f"scan report written: {out_path}", flush=True)


def cmd_select(scan_path: str) -> None:
    with open(scan_path) as f:
        scan = json.load(f)
    usable = [s for s in scan if "error" not in s and not s["chain_attempted"]]
    chosen: list[dict] = []
    taken: set[int] = set()
    warnings: list[str] = []

    def take(rule: str, pool: list[dict], warn: str) -> None:
        for row in pool:
            if row["id"] not in taken:
                taken.add(row["id"])
                chosen.append({**row, "reason": rule})
                return
        warnings.append(warn)

    by_id = sorted(usable, key=lambda s: s["id"])
    by_size = sorted(usable, key=lambda s: (s["candle_count"], s["id"]))
    by_volume = sorted(usable, key=lambda s: (-s["candle_count"], s["id"]))
    median = by_size[len(by_size) // 2]["candle_count"] if by_size else 0
    mid_band = sorted(
        (s for s in usable if 400 <= s["candle_count"] <= 2000),
        key=lambda s: (abs(s["candle_count"] - median), s["id"]),
    )

    take("curve_prefix", [s for s in by_id if s["era"] == "curve_prefix"],
         "no curve-prefix recording in scan")
    take("pool_post_fix", [s for s in by_id if s["era"] == "pool_post_fix"],
         "no post-fix recording in scan")
    take("pool_pre_fix", [s for s in by_id if s["era"] == "pool_pre_fix"],
         "no pre-fix recording in scan")
    take("top_volume", by_volume, "no top-volume candidate")
    take("top_volume", by_volume, "only one top-volume candidate")
    take("mid_volume", mid_band, "no mid-volume candidate")
    take("mid_volume", mid_band, "only one mid-volume candidate")
    take("low_volume", by_size, "no low-volume candidate")
    take("low_volume", by_size, "only one low-volume candidate")
    take("zero_trades", [s for s in by_id if s["trade_count"] == 0],
         "no zero-trades candidate")
    take("gain_retrace",
         [s for s in by_id if "gain_retrace" in s["exit_reasons"]],
         "no gain_retrace candidate")
    take("recording_ended", [s for s in by_id if s["ended_open"]],
         "no recording_ended candidate")
    for row in by_id:
        if len(chosen) >= 12:
            break
        if row["id"] not in taken:
            taken.add(row["id"])
            chosen.append({**row, "reason": "diversity"})
    chosen.sort(key=lambda r: r["id"])

    payload = {
        "engines": list(gl.ENGINES),
        "exec_models": list(gl.EXEC_MODELS),
        "warnings": warnings,
        "recordings": chosen,
    }
    os.makedirs(os.path.dirname(gl.RECORDINGS_JSON), exist_ok=True)
    with open(gl.RECORDINGS_JSON, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    print(f"corpus of {len(chosen)} written: {gl.RECORDINGS_JSON}")
    for r in chosen:
        print(f"  rec{r['id']:>6}  {r['reason']:<16} {r['era']:<14} "
              f"{r['candle_count']:>6}c  {r['trade_count']}t  "
              f"{r['token_symbol']}")
    for w in warnings:
        print(f"  WARNING: {w}")


def cmd_capture(only_recs, only_engines, only_exec) -> None:
    with open(gl.RECORDINGS_JSON) as f:
        corpus = json.load(f)
    recs = [r["id"] for r in corpus["recordings"]]
    if only_recs:
        recs = [r for r in recs if r in only_recs]
    engines = [e for e in corpus["engines"]
               if not only_engines or e in only_engines]
    execs = [e for e in corpus["exec_models"]
             if not only_exec or e in only_exec]

    all_results = []
    for rec in recs:
        for version in engines:
            for model in execs:
                started = time.time()
                result = gl.run_cell((rec, version, model))
                path = gl.cell_path(rec, version, model)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write(gl.canonical_json(result) + "\n")
                all_results.append(result)
                print(f"rec{rec} v{version} {model}: "
                      f"{result['trade_count']}t "
                      f"{round(time.time() - started, 1)}s", flush=True)

    for extra in corpus.get("extra_cells", []):
        task = (extra["recording_id"], extra["engine_version"],
                extra["exec_model"])
        result = gl.run_cell(task)
        path = gl.cell_path(*task)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(gl.canonical_json(result) + "\n")
        all_results.append(result)
        print(f"extra rec{task[0]} v{task[1]} {task[2]}: "
              f"{result['trade_count']}t ({extra['reason']})", flush=True)

    pure = gl.capture_pure()
    with open(os.path.join(gl.BASELINE_DIR, "pure.json"), "w") as f:
        f.write(gl.canonical_json(pure) + "\n")
    reasons = gl.collect_reasons(all_results)
    with open(os.path.join(gl.BASELINE_DIR, "exit_reasons.json"), "w") as f:
        f.write(gl.canonical_json(reasons) + "\n")
    print(f"captured {len(all_results)} cells + pure fixtures")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--select", action="store_true")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--scan-out",
                        default=os.path.join(gl.GOLDEN_DIR, "scan_report.json"))
    parser.add_argument("--per-band", type=int, default=5)
    parser.add_argument("--recs", default="")
    parser.add_argument("--engines", default="")
    parser.add_argument("--exec", default="")
    args = parser.parse_args()

    only_recs = {int(x) for x in args.recs.split(",") if x}
    only_engines = {int(x) for x in args.engines.split(",") if x}
    only_exec = {x for x in args.exec.split(",") if x}
    if args.scan:
        cmd_scan(args.scan_out, args.per_band)
    if args.select:
        cmd_select(args.scan_out)
    if args.capture:
        cmd_capture(only_recs, only_engines, only_exec)
    if not (args.scan or args.select or args.capture):
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
