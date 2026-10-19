"""forward_monitor.py — is the live system actually making money?

Reads a live session's journals and answers three questions with numbers:

  1. Realised vs expected — cumulative return vs the backtest's +0.043%/day
     (the arm14 out-of-sample mean), with a t-test on the daily series.
  2. Risk vs expected — max drawdown, worst day, daily σ. If realised σ >
     1.5x the backtest's 0.92%/day, sizing `k` is too high for live gaps.
  3. Prop-rule proximity — how close the equity path came to the daily and
     total floors (from equity.jsonl + guard.jsonl).

Verdicts
    ON TRACK       realised mean within the backtest CI
    DEGRADING      mean positive but < half expected, or t < 0
    FAILING        mean <= 0 over >= 20 trading days
    TOO EARLY      < 20 trading days

Usage:
    python -m market_live.forward_monitor \
        --session data/live_market/20261019_163500
    python -m market_live.forward_monitor --latest
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "live_market"

# arm14 out-of-sample reference (754 days)
REF_MEAN_PCT = 0.0432
REF_SD_PCT = 0.919
REF_SHARPE = 0.75


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _daily_equity(session: Path) -> list[tuple[str, float]]:
    """Session open equity + every `bar` equity record, collapsed per day."""
    eqs = [r for r in _read_jsonl(session / "equity.jsonl")
           if r.get("event") == "bar" and r.get("equity")]
    if not eqs:
        return []
    # last equity observation per calendar day (UTC)
    per_day: dict[str, float] = {}
    for r in eqs:
        t = r.get("time")
        if isinstance(t, (int, float)) and t > 0:
            day = _dt.datetime.fromtimestamp(t, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
        else:
            day = str(r.get("ts") or "")[:10]
        if not day:
            continue
        per_day[day] = float(r["equity"])
    start = None
    so = session / "session_open.json"
    if so.exists():
        cfg = json.loads(so.read_text()).get("config", {})
        start = float(cfg.get("starting_equity") or 0.0) or None
    days = sorted(per_day)
    return [(d, per_day[d]) for d in days]


def analyse(session: Path, starting_equity: float | None = None) -> dict:
    days = _daily_equity(session)
    if not days:
        return {"session": str(session), "error": "no equity records"}

    so = session / "session_open.json"
    cfg = json.loads(so.read_text()) if so.exists() else {}
    start = starting_equity or float(
        (cfg.get("config") or {}).get("starting_equity") or days[0][1])

    eqs = [e for _, e in days]
    rets = [(eqs[i] / eqs[i - 1] - 1.0) * 100.0 for i in range(1, len(eqs))]
    n = len(rets)
    mean = sum(rets) / n if n else 0.0
    sd = (sum((r - mean) ** 2 for r in rets) / (n - 1)) ** 0.5 if n > 1 else 0.0
    tstat = (mean / (sd / math.sqrt(n))) if n > 1 and sd > 0 else 0.0

    peak = eqs[0]
    maxdd = 0.0
    for e in eqs:
        peak = max(peak, e)
        maxdd = max(maxdd, (peak - e) / peak)

    total = (eqs[-1] / start - 1.0) * 100.0
    verdict = "TOO EARLY"
    if n >= 20:
        if mean <= 0:
            verdict = "FAILING"
        elif mean < REF_MEAN_PCT * 0.5 or tstat < 0:
            verdict = "DEGRADING"
        else:
            verdict = "ON TRACK"

    guard_events = [r for r in _read_jsonl(session / "guard.jsonl")]
    halts = [g for g in guard_events if g.get("kind") in ("fail_daily", "fail_total")]
    passes = [g for g in guard_events if g.get("kind") in ("pass", "phase_pass")]

    return {
        "session": session.name,
        "days": len(days),
        "start_equity": start,
        "equity": eqs[-1],
        "total_pct": total,
        "mean_daily_pct": mean,
        "sd_daily_pct": sd,
        "sharpe_ann": (mean / sd * math.sqrt(252)) if sd else 0.0,
        "t_stat": tstat,
        "maxdd_pct": maxdd * 100,
        "worst_day_pct": min(rets) if rets else 0.0,
        "best_day_pct": max(rets) if rets else 0.0,
        "reference": {"mean_daily_pct": REF_MEAN_PCT, "sd_daily_pct": REF_SD_PCT,
                      "sharpe": REF_SHARPE},
        "sd_ratio_vs_ref": (sd / REF_SD_PCT) if REF_SD_PCT else 0.0,
        "guard_halts": len(halts),
        "guard_passes": len(passes),
        "verdict": verdict,
        "first_day": days[0][0],
        "last_day": days[-1][0],
    }


def print_report(r: dict) -> None:
    if r.get("error"):
        print(f"[monitor] {r['session']}: {r['error']}")
        return
    print(f"\n[monitor] session {r['session']}  {r['first_day']} → {r['last_day']}")
    print(f"  days        {r['days']}")
    print(f"  equity      ${r['start_equity']:,.0f} → ${r['equity']:,.0f} "
          f"({r['total_pct']:+.2f}%)")
    print(f"  mean/day    {r['mean_daily_pct']:+.4f}%   "
          f"(backtest {REF_MEAN_PCT:+.4f}%)")
    print(f"  σ/day       {r['sd_daily_pct']:.3f}%   "
          f"(backtest {REF_SD_PCT:.3f}% — ratio {r['sd_ratio_vs_ref']:.2f}x)")
    print(f"  Sharpe      {r['sharpe_ann']:.2f}   t={r['t_stat']:+.2f}")
    print(f"  maxDD       {r['maxdd_pct']:.2f}%   worst day {r['worst_day_pct']:+.2f}%")
    print(f"  prop        {r['guard_passes']} phase pass(es), "
          f"{r['guard_halts']} halt(s)")
    print(f"  verdict     {r['verdict']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    root = Path(args.root)
    if args.latest:
        cands = sorted([p for p in root.iterdir() if p.is_dir()])
        if not cands:
            raise SystemExit(f"no sessions in {root}")
        sessions = [cands[-1]]
    elif args.session:
        sessions = [Path(args.session)]
    else:
        sessions = sorted([p for p in root.iterdir() if p.is_dir()])

    results = []
    for s in sessions:
        r = analyse(s)
        results.append(r)
        print_report(r)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
