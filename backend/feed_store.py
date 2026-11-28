"""
Feed Store — SQLite persistence for AutoFeed candidates, organic-screen
verdicts, outcomes, and calibration audit.

Deliberately a SEPARATE database (backend/data/feed_data.db) from both
recording corpora (unseen_database.db / price_data.db): feed telemetry must
never contaminate the research corpora (AGENTS.md invariant 8).  Outcome
computation READS the unseen corpus through data_store's corpus selector so
the join always sees the same tapes the backtests replay.

Ported from branch `new-feed` (2026-11-16) and extended with
`organic_profiles` (the holder/funding forensic payload per mint) so every
gate decision and its evidence is replayable.

Layout:

    feed_events            — every candidate the feed processed, its screen
                             verdict + forward decision (fed / rejected /
                             skipped_dedup / dropped_stale / ...) and the
                             full vector as JSON.
    feed_outcomes          — per fed mint: tape-health metrics from the
                             recording's own first 30 minutes.  CAUSAL: only
                             candles at/after the feed event, only COMPLETED
                             recordings.
    organic_profiles       — raw holder-forensics payload + verdict per mint
                             (cache + audit trail; 10-min feed cache).
    feed_calibration_audit — structured gate-change events for the gate
                             fitter.

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
# Holder-profile cache TTL (organic screen).  Holder state moves, but a
# 10-minute-old funding cluster is still the same cluster.
PROFILE_TTL_S = 600.0


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

            CREATE TABLE IF NOT EXISTS organic_profiles (
                mint          TEXT PRIMARY KEY,
                fetched_at    REAL NOT NULL,
                source        TEXT DEFAULT '',
                verdict       TEXT DEFAULT '',
                risk_score    REAL DEFAULT 0,
                reasons_json  TEXT DEFAULT '[]',
                metrics_json  TEXT DEFAULT '{}',
                payload_json  TEXT DEFAULT '{}',
                params_json   TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_organic_profiles_fetched
                ON organic_profiles(fetched_at);

            CREATE TABLE IF NOT EXISTS feed_calibration_audit (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           REAL NOT NULL,
                kind         TEXT NOT NULL,
                payload_json TEXT DEFAULT '{}'
            );
        """)
        # Additive migration for databases created before the threshold-set
        # cache key existed (2026-11-27).
        try:
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(organic_profiles)")}
            if "params_json" not in cols:
                conn.execute(
                    "ALTER TABLE organic_profiles ADD COLUMN params_json TEXT DEFAULT '{}'")
        except Exception:
            pass


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


def record_profile(
    mint: str,
    *,
    source: str,
    verdict: str,
    risk_score: float,
    reasons: list,
    metrics: dict,
    payload: Optional[dict] = None,
    params: Optional[dict] = None,
    fetched_at: Optional[float] = None,
) -> None:
    """Persist the organic-screen evidence + verdict for one mint."""
    try:
        with _get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO organic_profiles (mint, fetched_at, source,"
                " verdict, risk_score, reasons_json, metrics_json, payload_json,"
                " params_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (mint, fetched_at if fetched_at is not None else time.time(),
                 source, verdict, risk_score,
                 json.dumps(reasons or [], default=str),
                 json.dumps(metrics or {}, default=str),
                 json.dumps(payload or {}, default=str),
                 json.dumps(params or {}, default=str)),
            )
    except Exception as e:
        logger.warning(f"[FeedStore] record_profile failed: {e}")


def load_profile(mint: str, ttl_s: float = PROFILE_TTL_S,
                 params: Optional[dict] = None) -> Optional[dict]:
    """Fresh profile row (verdict + metrics + payload) or None.

    `params` is the threshold set that produced the verdict.  A cached row
    evaluated under DIFFERENT thresholds is a cache miss: changing the screen
    config must not keep re-applying an old verdict (2026-11-27).
    """
    try:
        with _get_read_conn() as conn:
            row = conn.execute(
                "SELECT * FROM organic_profiles WHERE mint=?", (mint,)).fetchone()
        if row is None:
            return None
        if time.time() - float(row["fetched_at"]) > ttl_s:
            return None
        if params is not None:
            stored = json.loads(row["params_json"] or "{}")
            if stored != params:
                return None
        return {
            "mint": row["mint"],
            "fetched_at": row["fetched_at"],
            "source": row["source"],
            "verdict": row["verdict"],
            "risk_score": row["risk_score"],
            "reasons": json.loads(row["reasons_json"] or "[]"),
            "metrics": json.loads(row["metrics_json"] or "{}"),
            "payload": json.loads(row["payload_json"] or "{}"),
            "params": json.loads(row["params_json"] or "{}"),
        }
    except Exception as e:
        logger.debug(f"[FeedStore] load_profile failed: {e}")
        return None


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
            # The recording starts at `calibration_started_at`, which can be
            # minutes AFTER the feed event (autofeed queue + serialized chain
            # walk).  Use the same slack on both sides — the outcome math is
            # still strictly causal (only candles at/after event_ts count).
            row = rconn.execute(
                "SELECT id FROM recordings WHERE mint = ? AND started_at <= ? "
                "AND started_at >= ? ORDER BY started_at DESC LIMIT 1",
                (mint, event_ts + JOIN_SLACK_S, event_ts - JOIN_SLACK_S),
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


# ── stats ────────────────────────────────────────────────────────────────────

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
            prof = conn.execute(
                "SELECT COUNT(*) AS n FROM organic_profiles").fetchone()
            prof_rej = conn.execute(
                "SELECT COUNT(*) AS n FROM organic_profiles WHERE verdict='reject'"
            ).fetchone()
            return {
                "events": ev["n"] or 0,
                "events_fed": ev["fed"] or 0,
                "outcomes": oc["n"] or 0,
                "fed_with_outcome": with_outcome["n"] or 0,
                "profiles": prof["n"] or 0,
                "profiles_rejected": prof_rej["n"] or 0,
                "last_event_ts": last["t"],
                "db": str(FEED_DB),
            }
    except Exception as e:
        return {"error": str(e), "db": str(FEED_DB)}


# init on import (idempotent)
init_feed_db()
