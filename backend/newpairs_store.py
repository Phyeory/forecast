"""
NewPairs Store — SQLite persistence for NEW-PAIR (pre-migration pump.fun)
price-action recordings.

Deliberately a SEPARATE database (backend/data/newpairs_data.db) from the
migrated-token research corpus (price_data.db): newly-born bonding-curve
tokens are a different data regime (86%+ die within hours) and must never
contaminate the production backtest corpus.

The recordings/candles schema mirrors the price-data tables exactly (same
OHLCV + buy/sell volume + pool_sol + market_cap_usd columns) so the existing
candle-replay / backtest machinery can consume these recordings unmodified
the day an engine is attached to this tab (explicitly NOT wired yet).

Extra recording metadata columns (creator / socials / dev-buy / initial
market cap) capture the birth event that PumpPortal's subscribeNewToken
feed delivers — information that no longer exists after the token dies.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

NEWPAIRS_DB = DATA_DIR / "newpairs_data.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(NEWPAIRS_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _get_read_conn() -> sqlite3.Connection:
    """Open a read-only connection (mirrors data_store's isolation pattern)."""
    conn = sqlite3.connect(f"file:{NEWPAIRS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def init_newpairs_db():
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS recordings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            mint        TEXT NOT NULL,
            token_name  TEXT DEFAULT '',
            token_symbol TEXT DEFAULT '',
            timeframe   TEXT NOT NULL,
            started_at  REAL NOT NULL,
            stopped_at  REAL,
            candle_count INTEGER DEFAULT 0,
            status      TEXT DEFAULT 'recording',
            -- Birth-event metadata from PumpPortal subscribeNewToken
            creator          TEXT DEFAULT '',
            twitter          TEXT DEFAULT '',
            telegram         TEXT DEFAULT '',
            website          TEXT DEFAULT '',
            initial_sol      REAL DEFAULT 0,   -- dev's initial buy size (SOL)
            market_cap_sol0  REAL DEFAULT 0,   -- market cap (SOL) at creation
            global_fees_sol  REAL DEFAULT 0    -- cumulative fees paid at spawn time
        );

        CREATE TABLE IF NOT EXISTS candles (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            recording_id INTEGER NOT NULL,
            time         INTEGER NOT NULL,
            open         REAL NOT NULL,
            high         REAL NOT NULL,
            low          REAL NOT NULL,
            close        REAL NOT NULL,
            volume       REAL DEFAULT 0,
            buy_volume   REAL DEFAULT 0,
            sell_volume  REAL DEFAULT 0,
            pool_sol     REAL DEFAULT 0,
            market_cap_usd REAL DEFAULT 0,
            FOREIGN KEY (recording_id) REFERENCES recordings(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_np_candles_rec_time ON candles(recording_id, time);
    """)
    # Migrate DBs created before the fees-qualification gate existed
    try:
        conn.execute("ALTER TABLE recordings ADD COLUMN global_fees_sol REAL DEFAULT 0")
    except Exception:
        pass  # column already exists
    conn.commit()
    conn.close()


def create_recording(
    mint: str,
    timeframe: str,
    token_name: str = "",
    token_symbol: str = "",
    *,
    creator: str = "",
    twitter: str = "",
    telegram: str = "",
    website: str = "",
    initial_sol: float = 0.0,
    market_cap_sol0: float = 0.0,
    global_fees_sol: float = 0.0,
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO recordings
           (mint, token_name, token_symbol, timeframe, started_at,
            creator, twitter, telegram, website, initial_sol, market_cap_sol0, global_fees_sol)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (mint, token_name, token_symbol, timeframe, time.time(),
         creator, twitter, telegram, website, initial_sol, market_cap_sol0, global_fees_sol),
    )
    rec_id = cur.lastrowid
    conn.commit()
    conn.close()
    return rec_id


def stop_recording(recording_id: int):
    """Finalise a recording: stamp stop time, persist the candle count."""
    conn = _get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM candles WHERE recording_id = ?", (recording_id,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE recordings SET stopped_at = ?, candle_count = ?, status = 'completed' WHERE id = ?",
        (time.time(), count, recording_id),
    )
    conn.commit()
    conn.close()


def insert_candle(
    recording_id: int, t: int,
    o: float, h: float, l: float, c: float,
    vol: float,
    buy_vol: float = 0.0,
    sell_vol: float = 0.0,
    pool_sol: float = 0.0,
    market_cap_usd: float = 0.0,
):
    """Upsert a single candle row (delete-then-insert, same semantics as
    data_store.insert_candle so only one row ever exists per time bucket)."""
    conn = _get_conn()
    conn.execute(
        "DELETE FROM candles WHERE recording_id = ? AND time = ?",
        (recording_id, t),
    )
    conn.execute(
        "INSERT INTO candles (recording_id, time, open, high, low, close, volume, buy_volume, sell_volume, pool_sol, market_cap_usd)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (recording_id, t, o, h, l, c, vol, buy_vol, sell_vol, pool_sol, market_cap_usd),
    )
    conn.commit()
    conn.close()


def insert_candles_batch(recording_id: int, candles: list[dict]):
    conn = _get_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO candles"
        " (recording_id, time, open, high, low, close, volume, buy_volume, sell_volume, pool_sol, market_cap_usd)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                recording_id,
                c["time"], c["open"], c["high"], c["low"], c["close"],
                c.get("volume", 0),
                c.get("buy_volume", 0.0),
                c.get("sell_volume", 0.0),
                c.get("pool_sol", 0.0),
                c.get("market_cap_usd", 0.0),
            )
            for c in candles
        ],
    )
    conn.commit()
    conn.close()


def list_recordings() -> list[dict]:
    conn = _get_read_conn()
    rows = conn.execute("SELECT * FROM recordings ORDER BY started_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recording(recording_id: int) -> Optional[dict]:
    conn = _get_read_conn()
    row = conn.execute("SELECT * FROM recordings WHERE id = ?", (recording_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_recording_candles(recording_id: int) -> list[dict]:
    """One candle per time bucket, highest-id row per bucket (mirrors
    data_store.get_recording_candles dedupe semantics)."""
    conn = _get_read_conn()
    rows = conn.execute(
        """
        SELECT time, open, high, low, close, volume,
               COALESCE(buy_volume, 0.0)  AS buy_volume,
               COALESCE(sell_volume, 0.0) AS sell_volume,
               COALESCE(pool_sol, 0.0)    AS pool_sol,
               COALESCE(market_cap_usd, 0.0) AS market_cap_usd
        FROM   candles
        WHERE  id IN (
            SELECT MAX(id)
            FROM   candles
            WHERE  recording_id = ?
            GROUP BY time
        )
        ORDER BY time
        """,
        (recording_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_recording(recording_id: int):
    conn = _get_conn()
    conn.execute("DELETE FROM candles WHERE recording_id = ?", (recording_id,))
    conn.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
    conn.commit()
    conn.close()


def cleanup_small_recordings(min_candles: int = 30) -> int:
    """Delete recordings with fewer than `min_candles` candles.  New-pair
    recordings are cheap but numerous (dead-on-arrival tokens); the default
    is lower than the price-data store's 100."""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT r.id
        FROM recordings r
        LEFT JOIN (
            SELECT recording_id, COUNT(DISTINCT time) as cnt
            FROM candles
            GROUP BY recording_id
        ) c ON r.id = c.recording_id
        WHERE COALESCE(c.cnt, r.candle_count, 0) < ?
        """,
        (min_candles,),
    ).fetchall()
    deleted_ids = [row[0] for row in rows]
    if deleted_ids:
        placeholders = ",".join("?" * len(deleted_ids))
        conn.execute(f"DELETE FROM candles WHERE recording_id IN ({placeholders})", deleted_ids)
        conn.execute(f"DELETE FROM recordings WHERE id IN ({placeholders})", deleted_ids)
        conn.commit()
    conn.close()
    return len(deleted_ids)


# ── Init on import (mirrors data_store) ────────────────────────────────────

init_newpairs_db()
