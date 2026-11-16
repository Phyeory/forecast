"""
Feed Store — SQLite persistence for AutoFeed candidates, outcomes, and
calibration audit (new-feed branch, 2026-11-16).

Deliberately a SEPARATE database (backend/data/feed_data.db) from both
recording corpora (unseen_database.db / price_data.db): feed telemetry must
never contaminate the research corpora (AGENTS.md invariant 8).  Outcome
computation READS the unseen corpus through data_store's corpus selector so
the join always sees the same tapes the backtests replay.

This is the prerequisite for feed-gate auto-calibration: nothing before
2026-11-16 persisted what the feed actually saw (candidates, gate decisions,
vectors), so outcomes could never be joined back to gates.  Layout:

    feed_events    — every candidate the feed processed that passed its
                     source's gates (gmgn `_to_candidate` pass or gecko
                     gate+vet pass), plus the forward decision (fed /
                     skipped_dedup / skipped_backpressure / skipped_cooldown)
                     and the full vector as JSON.
    feed_outcomes  — per fed mint: tape-health metrics from the recording's
                     own first 30 minutes (max drawdown, worst 1-s dump,
                     price-death flag, forward return).  CAUSAL: computed
                     only from candles at/after the feed event, only from
                     COMPLETED recordings.
    feed_calibration_audit — structured gate-change events for the future
                     gate fitter (mirrors engine `recalibration` discipline).

Graveyard guard: this is FEED-GATE calibration from TAPE HEALTH, not engine
SDE-coefficient recalibration (iter84b/85: quality up, tail exposure down,
PnL flat — do not resurrect).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

import data_store

logger = logging.getLogger("feed_store")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

FEED_DB = DATA_DIR / "feed_data.db"

# Outcome window (seconds after the feed event)
OUTCOME_WINDOW_S = 1800.0
# A recording must be at least this old (since its first candle) before the
# outcome is computed — gives the tape time to finish the window.
OUTCOME_MIN_AGE_S = OUTCOME_WINDOW_S + 120.0
# Recording candidates are joined by mint with this slack around event ts.
JOIN_SLACK_S = 600.0


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(FEED_DB), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _get_read_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{FEED_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def init_feed_db() -> None:
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS feed_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          REAL NOT NULL,
                mint        TEXT NOT NULL,
                source      TEXT NOT NULL,
                decision    TEXT NOT NULL,
                reason      TEXT DEFAULT '',
                symbol      TEXT DEFAULT '',
                market_cap  REAL DEFAULT 0,
                risk_score  REAL DEFAULT 0,
                vector_json TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_feed_events_mint_ts
                ON feed_events(mint, ts);
            CREATE INDEX IF NOT EXISTS idx_feed_events_decision
                ON feed_events(decision);

            CREATE TABLE IF NOT EXISTS feed_outcomes (
                mint                   TEXT NOT NULL,
                event_ts               REAL NOT NULL,
                recording_id           INTEGER,
                computed_at            REAL,
                candle_count_window    INTEGER DEFAULT 0,
                max_drawdown_pct_30m   REAL,
                max_1s_dump_pct        REAL,
                price_death            INTEGER DEFAULT 0,
                forward_return_pct_30m REAL,
                PRIMARY KEY (mint, event_ts)
            );

            CREATE TABLE IF NOT EXISTS feed_calibration_audit (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL NOT NULL,
                kind         TEXT NOT NULL,
                payload_json TEXT DEFAULT '{}'
            );
        """)


# ── writes ───────────────────────────────────────────────────────────────────

def record_event(
    mint: str,
    source: str,
    decision: str,
    reason: str = "",
    symbol: str = "",
    market_cap: float = 0.0,
    risk_score: float = 0.0,
    vector: Optional[dict] = None,
    ts: Optional[float] = None,
) -> None:
    """Persist one feed decision.  Never raises — telemetry must not break the
    feed path."""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO feed_events (ts, mint, source, decision, reason, symbol,"
                " market_cap, risk_score, vector_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (ts if ts is not None else time.time(), mint, source, decision, reason,
                 symbol, market_cap, risk_score, json.dumps(vector or {}, default=str)),
            )
    except Exception as e:
        logger.warning(f"[FeedStore] record_event failed: {e}")


def record_outcome(
    mint: str,
    event_ts: float,
    recording_id: Optional[int],
    metrics: dict,
) -> None:
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO feed_outcomes (mint, event_ts, recording_id,"
                " computed_at, candle_count_window, max_drawdown_pct_30m, max_1s_dump_pct,"
                " price_death, forward_return_pct_30m) VALUES (?,?,?,?,?,?,?,?,?)",
                (mint, event_ts, recording_id, time.time(),
                 metrics.get("candle_count_window"),
                 metrics.get("max_drawdown_pct_30m"),
                 metrics.get("max_1s_dump_pct"),
                 1 if metrics.get("price_death") else 0,
                 metrics.get("forward_return_pct_30m")),
            )
    except Exception as e:
        logger.warning(f"[FeedStore] record_outcome failed: {e}")


def audit(kind: str, payload: dict) -> None:
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO feed_calibration_audit (ts, kind, payload_json) VALUES (?,?,?)",
                (time.time(), kind, json.dumps(payload, default=str)),
            )
    except Exception as e:
        logger.warning(f"[FeedStore] audit failed: {e}")


# ── outcome math (pure, testable) ────────────────────────────────────────────

def compute_tape_outcome(candles: list[dict], event_ts: float) -> dict:
    """Tape-health metrics over the first OUTCOME_WINDOW_S after the event.

    `candles`: [{time, open, high, low, close, ...}] ascending, 1 s cadence.
    Baseline = the open of the first candle at/after the event (the price the
    feed decided at).  STRICTLY CAUSAL: candles before the event are ignored.
    """
    window = [c for c in candles if float(c.get("time") or 0) >= event_ts
              and float(c.get("time") or 0) <= event_ts + OUTCOME_WINDOW_S]
    out: dict = {"candle_count_window": len(window), "max_drawdown_pct_30m": None,
                 "max_1s_dump_pct": None, "price_death": False,
                 "forward_return_pct_30m": None}
    if not window:
        return out
    base = float(window[0].get("open") or 0)
    if base <= 0:
        return out

    lows = [float(c.get("low") or 0) for c in window if float(c.get("low") or 0) > 0]
    if lows:
        out["max_drawdown_pct_30m"] = round((min(lows) / base - 1.0) * 100.0, 2)

    worst_1s = None
    flat_streak = 0
    max_flat = 0
    for c in window:
        o, cl = float(c.get("open") or 0), float(c.get("close") or 0)
        if o > 0 and cl > 0:
            chg = (cl / o - 1.0) * 100.0
            worst_1s = chg if worst_1s is None else min(worst_1s, chg)
        if o > 0 and abs(cl - o) / o <= 1e-9 and float(c.get("volume") or 0) == 0:
            flat_streak += 1
            max_flat = max(max_flat, flat_streak)
        else:
            flat_streak = 0
    out["max_1s_dump_pct"] = round(worst_1s, 2) if worst_1s is not None else None
    out["price_death"] = max_flat >= 60

    last = float(window[-1].get("close") or 0)
    if last > 0:
        out["forward_return_pct_30m"] = round((last / base - 1.0) * 100.0, 2)
    return out


# ── reaper (join fed events to recordings, compute outcomes) ─────────────────

def _find_recording_for_event(conn: sqlite3.Connection, mint: str, event_ts: float) -> Optional[int]:
    """Recording whose tape covers the event (mint match, started_at within
    slack before the event).  Reads the UNSEEN corpus (the live feed's corpus)
    through the corpus selector, read-only."""
    rec_db = data_store.recordings_db_path(data_store.normalize_recordings_db("unseen"))
    try:
        rconn = sqlite3.connect(f"file:{rec_db}?mode=ro", uri=True)
        rconn.row_factory = sqlite3.Row
        try:
            row = rconn.execute(
                "SELECT id FROM recordings WHERE mint = ? AND started_at <= ? "
                "AND started_at >= ? ORDER BY started_at DESC LIMIT 1",
                (mint, event_ts + 60.0, event_ts - JOIN_SLACK_S),
            ).fetchone()
            return row["id"] if row else None
        finally:
            rconn.close()
    except Exception as e:
        logger.debug(f"[FeedStore] recording lookup failed: {e}")
        return None


def _load_recording_candles(recording_id: int) -> Optional[list[dict]]:
    rec_db = data_store.recordings_db_path(data_store.normalize_recordings_db("unseen"))
    try:
        rconn = sqlite3.connect(f"file:{rec_db}?mode=ro", uri=True)
        rconn.row_factory = sqlite3.Row
        try:
            rows = rconn.execute(
                "SELECT time, open, high, low, close, volume FROM candles "
                "WHERE recording_id = ? ORDER BY time ASC",
                (recording_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            rconn.close()
    except Exception as e:
        logger.debug(f"[FeedStore] candle load failed: {e}")
        return None


def run_outcome_reaper(limit: int = 200) -> int:
    """Compute outcomes for fed events that lack them and whose window has
    fully elapsed.  Returns the number computed."""
    now = time.time()
    computed = 0
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT e.id, e.mint, e.ts FROM feed_events e LEFT JOIN feed_outcomes o "
            "ON o.mint = e.mint AND o.event_ts = e.ts "
            "WHERE e.decision = 'fed' AND o.mint IS NULL AND e.ts <= ? "
            "ORDER BY e.ts ASC LIMIT ?",
            (now - OUTCOME_MIN_AGE_S, limit),
        ).fetchall()
        for r in rows:
            rec_id = _find_recording_for_event(conn, r["mint"], r["ts"])
            if rec_id is None:
                # no recording covers this event — mark as computed-empty so
                # the reaper doesn't retry forever
                record_outcome(r["mint"], r["ts"], None,
                               {"candle_count_window": 0})
                continue
            candles = _load_recording_candles(rec_id)
            # only complete-enough tapes: last candle beyond the window
            if not candles:
                continue
            last_t = float(candles[-1].get("time") or 0)
            if last_t < r["ts"] + OUTCOME_WINDOW_S:
                continue   # window not yet covered — retry later
            metrics = compute_tape_outcome(candles, r["ts"])
            record_outcome(r["mint"], r["ts"], rec_id, metrics)
            computed += 1
    if computed:
        logger.info(f"[FeedStore] outcome reaper computed {computed} tape outcomes")
    return computed


def stats() -> dict:
    try:
        with _get_read_conn() as conn:
            ev = conn.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(CASE WHEN decision='fed' THEN 1 ELSE 0 END) AS fed "
                "FROM feed_events").fetchone()
            oc = conn.execute("SELECT COUNT(*) AS n FROM feed_outcomes").fetchone()
            last = conn.execute("SELECT MAX(ts) AS t FROM feed_events").fetchone()
            with_outcome = conn.execute(
                "SELECT COUNT(*) AS n FROM feed_events e JOIN feed_outcomes o "
                "ON o.mint = e.mint AND o.event_ts = e.ts WHERE e.decision='fed'"
            ).fetchone()
            return {
                "events": ev["n"] or 0,
                "events_fed": ev["fed"] or 0,
                "outcomes": oc["n"] or 0,
                "fed_with_outcome": with_outcome["n"] or 0,
                "last_event_ts": last["t"],
                "db": str(FEED_DB),
            }
    except Exception as e:
        return {"error": str(e), "db": str(FEED_DB)}


# init on import (idempotent, mirrors newpairs_store)
init_feed_db()
