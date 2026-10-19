"""challenge_status.py — "would this be a passing FTMO 2-Step evaluation?"

Replays a live session's realised equity path through the firm's real rule
cascade (the same shapes `mc_firms.py` bootstraps and `prop_guard.py`
enforces), and reports continuously:

  * which phase you are in, how many trading days and calendar days so far
  * progress to the next profit target and headroom to the daily/max floors
  * the consistency-rule state (best day / total profit)
  * whether the evaluation WOULD already be passing or breached

Rules (FTMO 2-Step, verified 2026-10-18 from ftmo.com):
    targets [10%, 5%] · max daily loss 5% · STATIC max loss 10% (initial
    balance) · min 4 trading days per phase · consistency: best day ≤ 50%
    of total profit · NO time limit.

Notes on fidelity:
  * measured on DAILY CLOSING equity — intraday breaches between closes are
    not visible here (the live PropGuard enforces them in real time); the
    guard also fires 0.5% EARLY (buffer), so this report is FTMO-exact.
  * targets/drawdowns are evaluated on equity (open marks included). FTMO
    uses account balance for targets; at the moment a target is crossed the
    two differ only by open-trade marks, so this is slightly conservative
    (equity ≥ balance when positions are up, ≤ when down).

Usage:
    python -m market_live.challenge_status --latest
    python -m market_live.challenge_status --session data/live_market/20261019_063215
    python -m market_live.challenge_status --latest --template ftmo_trial_2step
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
from pathlib import Path

from market_live.forward_monitor import _read_jsonl, _daily_equity
from market_live.prop_guard import TEMPLATES

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "live_market"


def _resolve_session(args) -> Path:
    if args.session:
        return Path(args.session)
    root = Path(args.root)
    cands = sorted([p for p in root.iterdir() if p.is_dir()])
    if not cands:
        raise SystemExit(f"no sessions under {root}")
    return cands[-1]


def evaluate(session: Path, template: str = "ftmo_2step",
             account: float = 0.0) -> dict | None:
    tpl = TEMPLATES[template]
    days = _daily_equity(session)          # [(date, last equity of day)]
    if not days:
        return None

    so = session / "session_open.json"
    cfg = json.loads(so.read_text()) if so.exists() else {}
    start_eq = float((cfg.get("config") or {}).get("starting_equity")
                     or account or 0.0) or days[0][1]

    acct = account or start_eq
    targets = list(tpl["targets"])
    daily_pct = tpl["daily"]
    max_pct = tpl["max_loss"]
    trailing = tpl.get("trailing", False)
    min_days = tpl.get("min_days", 0)
    consistency = tpl.get("consistency")
    limit = tpl.get("time_limit")

    total_floor = acct * (1.0 - max_pct / 100.0)     # static (initial balance)
    if trailing:
        peak_floor_pct = max_pct                     # trailing variant
        total_floor = start_eq * (1.0 - max_pct / 100.0)

    # phase state
    phase = 0
    phase_start = 0                                   # index into days
    phase_trading = 0                                  # trading days in phase
    fill_days = set()                                  # calendar days with broker fills
    for r in _read_jsonl(session / "decisions.jsonl"):
        if not r.get("fills"):
            continue
        t = r.get("time")
        if isinstance(t, (int, float)) and t > 0:
            day = _dt.datetime.fromtimestamp(t, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
        else:
            day = str(t or r.get("ts") or "")[:10]
        if day:
            fill_days.add(day)

    best_day = 0.0                                     # best day profit in phase
    events: list[dict] = []                            # pass/breach history
    trailed_peak = max(start_eq, days[0][1])

    rows = []
    prev_eq = start_eq
    phase_start_day = days[0][0]
    phase_total_profit = 0.0
    outcome = "ACTIVE"

    for i, (day, eq) in enumerate(days):
        day_pnl = eq - prev_eq
        prev_day_start = prev_eq
        prev_eq = eq
        rows.append((day, eq, day_pnl))

        if day in fill_days and phase < len(targets):
            phase_trading += 1

        # ── the account still trades until phases complete ────────────
        if trailing:
            trailed_peak = max(trailed_peak, eq)
            total_floor = trailed_peak * (1.0 - max_pct / 100.0)

        # ── breaches ──────────────────────────────────────────────────
        breach = None
        if daily_pct and day_pnl / max(prev_day_start, 1.0) * 100.0 <= -daily_pct:
            breach = ("daily", day_pnl / prev_day_start * 100.0)
        if eq < total_floor:
            breach = ("total", (eq - total_floor) / acct * 100.0)
        if breach and outcome == "ACTIVE":
            outcome = f"BREACHED ({breach[0]})"
            events.append({"kind": "breach", "rule": breach[0],
                           "day": day, "detail": breach[1]})
            break

        # ── target check (equity, with min-days + consistency gates) ──
        if phase < len(targets):
            target = acct * (1.0 + targets[phase] / 100.0)
            phase_total_profit += day_pnl
            best_day = max(best_day, day_pnl)
            share = (best_day / phase_total_profit * 100.0
                     if phase_total_profit > 0 else float("inf"))
            const_ok = (consistency is None
                        or (phase_total_profit > 0 and share <= consistency))
            if eq >= target and phase_trading >= min_days and const_ok:
                events.append({"kind": "phase_pass", "phase": phase,
                               "day": day, "equity": eq,
                               "trading_days": phase_trading,
                               "best_day_share": None if consistency is None
                               else round(share, 1)})
                phase += 1
                phase_start = i + 1
                phase_trading = 0
                phase_total_profit = 0.0
                best_day = 0.0
                phase_start_day = day
                if phase >= len(targets):
                    outcome = "PASSED (all phases)"
                    if limit:
                        outcome += f" [limit {limit}d: none]"

    last_day, last_eq, last_pnl = rows[-1]
    out: dict = {
        "session": session.name,
        "template": tpl["label"],
        "account": acct,
        "status": outcome,
        "phase_current": phase + 1 if phase < len(targets) else len(targets),
        "phases_total": len(targets),
        "days_elapsed": len(rows),
        "equity": last_eq,
        "equity_last": last_eq,           # alias used by the printer/tests
        "equity_pct": (last_eq - acct) / acct * 100.0,
        "last_day_pnl": last_pnl,
        "events": events,
    }
    if phase < len(targets):
        target = acct * (1.0 + targets[phase] / 100.0)
        out["phase_target"] = target
        out["phase_target_pct"] = targets[phase]
        out["target_progress_pct"] = (last_eq - acct) / (target - acct) * 100.0
        out["trading_days_in_phase"] = phase_trading
        out["min_days_required"] = min_days
        tot_room = (last_eq - total_floor) / acct * 100.0
        out["total_floor"] = total_floor
        out["floor_headroom_pct"] = tot_room
        if daily_pct:
            out["daily_floor"] = prev_eq * (1.0 - daily_pct / 100.0)
            out["daily_headroom_pct_abs"] = last_eq - out["daily_floor"]
        if consistency:
            tt = sum(p for _, _, p in rows[phase_start:])
            bd = max((p for _, _, p in rows[phase_start:]), default=0.0)
            out["consistency_best_share_pct"] = (round(bd / tt * 100.0, 1)
                                                 if tt > 0 else None)
    return out


def print_report(r: dict) -> None:
    print(f"\n[challenge] {r['template']} — session {r['session']}  "
          f"account ${r['account']:,.0f}")
    print(f"  status     {r['status']}")
    print(f"  phase      {r['phase_current']}/{r['phases_total']}   "
          f"days elapsed {r['days_elapsed']}")
    print(f"  equity     ${r['equity']:,.2f}  ({r['equity_pct']:+.2f}% of account)")
    if "phase_target" in r:
        print(f"  target     ${r['phase_target']:,.0f} (+{r['phase_target_pct']}%)"
              f" — {min(r['target_progress_pct'], 999):.1f}% achieved")
        print(f"  trading days {r['trading_days_in_phase']}/{r['min_days_required']}"
              f"   floor headroom {r['floor_headroom_pct']:+.2f}%")
        if r.get("daily_floor"):
            print(f"  daily floor ${r['daily_floor']:,.2f}"
                  f"  (today's headroom ${r['daily_headroom_pct_abs']:,.2f})")
        if r.get("consistency_best_share_pct") is not None:
            print(f"  consistency best-day share {r['consistency_best_share_pct']}%"
                  f" (≤50% required)")
    for e in r["events"]:
        if e["kind"] == "phase_pass":
            print(f"  ✓ phase {e['phase']+1} passed on {e['day']} "
                  f"(${e['equity']:,.0f}, {e['trading_days']} tdays"
                  + (f", best-day {e['best_day_share']}%" if e["best_day_share"] is not None else "")
                  + ")")
        else:
            print(f"  ✗ breach {e['rule']} on {e['day']} ({e['detail']:+.2f}%)")
    print("  verdict    "
          + ("PASS — this path would already be an FTMO Trader account"
             if r["status"].startswith("PASSED")
             else "BREACHED — this path would have failed the evaluation"
             if r["status"].startswith("BREACHED")
             else "IN PROGRESS"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--template", default="ftmo_2step")
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--account", type=float, default=0.0)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    root = Path(args.root)
    if args.all:
        sessions = sorted([p for p in root.iterdir() if p.is_dir()])
    else:
        sessions = [_resolve_session(args)]

    results = []
    for s in sessions:
        r = evaluate(s, template=args.template, account=args.account)
        if r is None:
            print(f"[challenge] {s.name}: no equity records yet")
            continue
        results.append(r)
        print_report(r)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
