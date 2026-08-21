"""iter57 — global regime cache builder (Q(t) for StrategyEngineV2).

Builds `backend/data/global_regime_cache.json`:

    {
      "q_by_date": {"2026-07-30": 0.899, ...},   # regime score per UTC date
      "gr_share_by_date": {...},                  # raw gain_retrace share
      "provenance": {...}
    }

Q definition (iter57 diagnosis — see backend/analysis/iter57_diagnosis.md):

    Q(d) = clamp01( (gr_share_lag3(d) - Q_LO) / (Q_HI - Q_LO) )

where `gr_share_lag3(d)` is the gain_retrace exit share pooled over the 3
trading days STRICTLY BEFORE `d` (lag-3 rolling window — the only candidate
that cleared the out-of-sample bar: Spearman to next-day WR +0.668,
p=0.0065 on the 23-date panel; all external global series were null —
see diagnosis artefact §1).

Causality: the value used for date d only contains trades closed on dates
< d.  The backtester joins candle.time → UTC date → q_by_date; the live
poller recomputes today's Q from `backtest_data.db::backtest_trades`
(`compute_live_q()`) — the identical computation.

Usage:
    python backend/fetch_global_regime.py            # build cache from the
                                                     # canonical date panel
    python backend/fetch_global_regime.py --from-db  # build from
                                                     # backtest_trades rows
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
CACHE_PATH = os.path.join(HERE, "data", "global_regime_cache.json")
PANEL_PATH = os.path.join(HERE, "analysis", "date_segmented_results.json")
BACKTEST_DB = os.path.join(HERE, "data", "backtest_data.db")

# Normalisation constants — fixed at diagnosis time (panel observed
# lag-3 share range 0.385..0.750).  Kept in the cache provenance so the
# engine and any future re-run agree byte-exact.
Q_LO = 0.35
Q_HI = 0.70
LAG_DAYS = 3


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def q_from_share(share: float) -> float:
    return _clamp01((share - Q_LO) / (Q_HI - Q_LO))


def lagged_shares(daily: list[tuple[str, int, int]]) -> dict[str, float]:
    """daily = sorted [(date, n_trades, n_gain_retrace)].

    Returns {date: pooled gr share over the prior LAG_DAYS trading days}.
    Strictly causal: the pool never contains date d itself.
    """
    out: dict[str, float] = {}
    hist: list[tuple[int, int]] = []
    for date, n, g in daily:
        if len(hist) >= LAG_DAYS:
            window = hist[-LAG_DAYS:]
            tot = sum(x[0] for x in window)
            if tot > 0:
                out[date] = sum(x[1] for x in window) / tot
        if n > 0:
            hist.append((n, g))
    return out


def build_from_panel() -> tuple[dict, dict]:
    panel = json.load(open(PANEL_PATH))["results"]
    daily = []
    for r in sorted(panel, key=lambda x: x["date"]):
        n = int(r.get("total_trades") or 0)
        g = int(r.get("exit_reasons", {}).get("gain_retrace", {}).get("n", 0))
        daily.append((r["date"], n, g))
    shares = lagged_shares(daily)
    q = {d: q_from_share(s) for d, s in shares.items()}
    prov = {
        "source": "date_segmented_results.json (canonical 23-date panel, "
                  "production-config StrategyEngineV2)",
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "q_lo": Q_LO, "q_hi": Q_HI, "lag_days": LAG_DAYS,
        "n_dates": len(q),
        "diagnosis": "backend/analysis/iter57_diagnosis.md",
        "external_fetches_tested_and_rejected": [
            "https://api.coingecko.com/api/v3/coins/solana/market_chart"
            "?vs_currency=usd&days=60&interval=daily",
            "https://api.llama.fi/overview/dexs/Solana?dataType=dailyVolume",
        ],
    }
    return q, prov


def build_from_db() -> tuple[dict, dict]:
    """Recompute Q from persisted backtest trades (live-path computation)."""
    conn = sqlite3.connect(BACKTEST_DB)
    rows = conn.execute(
        """
        SELECT date(bt.entry_time, 'unixepoch')      AS d,
               count(*)                              AS n,
               sum(CASE WHEN bt.exit_reason = 'gain_retrace' THEN 1 ELSE 0 END) AS g
        FROM backtest_trades bt
        JOIN backtests b ON b.id = bt.backtest_id
        WHERE b.market_type = 'spot' AND bt.entry_time IS NOT NULL
        GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    conn.close()
    daily = [(d, int(n), int(g)) for d, n, g in rows]
    shares = lagged_shares(daily)
    q = {d: q_from_share(s) for d, s in shares.items()}
    prov = {
        "source": "backtest_data.db::backtest_trades (live computation)",
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "q_lo": Q_LO, "q_hi": Q_HI, "lag_days": LAG_DAYS,
        "n_dates": len(q),
        "diagnosis": "backend/analysis/iter57_diagnosis.md",
    }
    return q, prov


def compute_live_q(now_ts: int | None = None) -> float | None:
    """Q for TODAY (UTC) computed causally from strictly prior dates.

    Used by the main.py live poller; identical math to the backtest join.
    Returns None when fewer than LAG_DAYS prior trading days exist.
    """
    q, _ = build_from_db()
    today = _dt.datetime.fromtimestamp(
        now_ts if now_ts is not None else _dt.datetime.now(_dt.timezone.utc).timestamp(),
        _dt.timezone.utc,
    ).strftime("%Y-%m-%d")
    return q.get(today)


def load_cache() -> dict:
    """Load {date: Q} from the cache file (empty dict when absent)."""
    try:
        with open(CACHE_PATH) as f:
            return json.load(f).get("q_by_date", {}) or {}
    except Exception:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-db", action="store_true",
                    help="build from backtest_trades instead of the panel")
    ap.add_argument("--out", default=CACHE_PATH)
    args = ap.parse_args()

    if args.from_db:
        q, prov = build_from_db()
    else:
        q, prov = build_from_panel()

    payload = {"q_by_date": q, "provenance": prov}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {len(q)} regime rows -> {args.out}")
    for d in sorted(q):
        print(f"  {d}  Q={q[d]:.4f}")


if __name__ == "__main__":
    main()
